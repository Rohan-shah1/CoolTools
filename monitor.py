"""
Spotify Ad Restarter -- Monitor
================================
Monitors Spotify's window title to detect audio ads, instantly mutes,
kills & restarts Spotify, then resumes playback.

Runs as a system tray icon for unobtrusive background operation.
"""

import os
import sys
import time
import subprocess
import ctypes
import threading
from datetime import datetime
import comtypes.client

import psutil
import win32gui
import win32process
import win32con

# Audio muting and volume metering
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume, IAudioMeterInformation

# Windows UI Automation (for reading/restoring lyrics toggle & volume slider)
try:
    comtypes.client.GetModule("UIAutomationCore.dll")
    import comtypes.gen.UIAutomationClient as _UIA
    _uia_client = comtypes.client.CreateObject(_UIA.CUIAutomation._reg_clsid_, interface=_UIA.IUIAutomation)
    UIA_AVAILABLE = True
except Exception:
    _UIA = None
    _uia_client = None
    UIA_AVAILABLE = False

# System tray
import pystray
from PIL import Image, ImageDraw
import signal

from config import (
    POLL_INTERVAL,
    RESTART_DELAY,
    LOAD_WAIT_TIME,
    COOLDOWN_PERIOD,
    AD_TITLE_EXACT,
    AD_TITLE_CONTAINS,
    SPOTIFY_EXE_PATH,
)

# Normalize configurations to lowercase sets to prevent case mismatch bugs
AD_TITLE_EXACT_LOWER = {t.strip().lower() for t in AD_TITLE_EXACT if t.strip()}
AD_TITLE_CONTAINS_LOWER = {t.strip().lower() for t in AD_TITLE_CONTAINS if t.strip()}

# Titles that can represent generic, non-playing Spotify window states (e.g. paused)
# For these, we will verify if audio is actually playing before treating them as ads.
AMBIGUOUS_TITLES = {"spotify", "spotify free", "spotify premium", "join premium"}



# ==========================================================
#  SpotifyMonitor -- core monitoring engine
# ==========================================================

class SpotifyMonitor:
    """Watches Spotify's window title and restarts on ad detection."""

    # State constants
    IDLE = "IDLE"
    MUSIC_PLAYING = "MUSIC_PLAYING"
    AD_DETECTED = "AD_DETECTED"
    RESTARTING = "RESTARTING"
    COOLDOWN = "COOLDOWN"

    def __init__(self):
        self.state = self.IDLE
        self.last_song_title = None
        self.last_seen_title = None
        self.last_restart_time = 0
        self.running = True
        self.ads_skipped = 0
        self.status_message = "Starting up..."
        self.spotify_path = self._find_spotify_path()
        # Saved pre-restart state
        self._saved_volume = None        # float 0.0–1.0
        self._saved_lyrics_on = None     # bool

    # -- Spotify path detection ----------------------------

    def _find_spotify_path(self):
        """Auto-detect Spotify's executable path."""
        # User override
        if SPOTIFY_EXE_PATH and os.path.exists(SPOTIFY_EXE_PATH):
            return SPOTIFY_EXE_PATH

        # Common install locations
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")

        candidates = [
            os.path.join(appdata, "Spotify", "Spotify.exe"),
            os.path.join(localappdata, "Spotify", "Spotify.exe"),
            os.path.join(
                localappdata,
                "Microsoft",
                "WindowsApps",
                "Spotify.exe",
            ),
        ]

        for path in candidates:
            if os.path.exists(path):
                return path

        # Try to find from a running process
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                if (
                    proc.info["name"]
                    and proc.info["name"].lower() == "spotify.exe"
                ):
                    return proc.info["exe"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        return None

    # -- Window detection ----------------------------------

    def find_spotify_window(self):
        """Return (hwnd, title) of Spotify's main window, or None."""
        results = []

        def _enum_cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                proc = psutil.Process(pid)
                if proc.name().lower() == "spotify.exe":
                    title = win32gui.GetWindowText(hwnd)
                    if title:  # skip blank child windows
                        results.append((hwnd, title))
            except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                pass

        win32gui.EnumWindows(_enum_cb, None)

        if not results:
            return None

        # The main window usually has the longest title
        return max(results, key=lambda r: len(r[1]))

    # -- Ad detection helpers ------------------------------

    @staticmethod
    def is_ad_title(title: str) -> bool:
        """Does this window title indicate an ad?"""
        if not title:
            return False
        t = title.strip().lower()

        if t in AD_TITLE_EXACT_LOWER:
            return True
        for indicator in AD_TITLE_CONTAINS_LOWER:
            if indicator in t:
                return True
        return False

    @staticmethod
    def is_song_title(title: str) -> bool:
        """Does this window title look like 'Song - Artist'?"""
        if not title:
            return False
        t = title.strip().lower()
        # Song titles contain ' - ' and are NOT the app name
        return " - " in title and t not in AD_TITLE_EXACT_LOWER

    def get_spotify_peak_volume(self) -> float:
        """Return the current peak volume level of Spotify (0.0 to 1.0), or 0.0 if not found/error."""
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                proc = session.Process
                if proc and proc.name().lower() == "spotify.exe":
                    meter = session._ctl.QueryInterface(IAudioMeterInformation)
                    return meter.GetPeakValue()
        except Exception:
            pass
        return 0.0

    def is_ad_playing(self, title: str) -> bool:
        """Determine if an ad is actively playing based on title and audio peak."""
        if not title:
            return False
        t = title.strip().lower()

        # Check if matches any ad title criteria
        matches_exact = t in AD_TITLE_EXACT_LOWER
        matches_contains = any(ind in t for ind in AD_TITLE_CONTAINS_LOWER)

        if not (matches_exact or matches_contains):
            return False

        # If it is an ambiguous title (like the idle app name), only treat as an ad if it's emitting sound
        if t in AMBIGUOUS_TITLES:
            peak = self.get_spotify_peak_volume()
            # If peak is > 0.001, it is playing audio (thus an ad, not paused)
            return peak > 0.001

        return True

    # -- Audio muting via pycaw ----------------------------

    def _set_spotify_mute(self, mute: bool):
        """Mute or unmute Spotify's audio session."""
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if (
                    session.Process
                    and session.Process.name().lower() == "spotify.exe"
                ):
                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    volume.SetMute(1 if mute else 0, None)
                    self.log("[MUTE] Spotify muted" if mute else "[UNMUTE] Spotify unmuted")
                    return True
        except Exception as e:
            self.log(f"[WARN] Audio control error: {e}")
        return False

    def mute_spotify(self):
        return self._set_spotify_mute(True)

    def unmute_spotify(self):
        return self._set_spotify_mute(False)

    # -- UI Automation helpers (lyrics + volume state) ------

    def _find_uia_player_controls(self):
        """Return (lyrics_elem, volume_elem) from Spotify's UIA tree, or (None, None)."""
        if not UIA_AVAILABLE:
            return None, None
        try:
            window = self.find_spotify_window()
            if not window:
                return None, None
            hwnd, _ = window
            root = _uia_client.ElementFromHandle(hwnd)

            # Create property conditions for Lyrics button
            cond_lyrics_name = _uia_client.CreatePropertyCondition(_UIA.UIA_NamePropertyId, "Lyrics")
            cond_lyrics_type = _uia_client.CreatePropertyCondition(_UIA.UIA_ControlTypePropertyId, 50000) # Button
            cond_lyrics = _uia_client.CreateAndCondition(cond_lyrics_name, cond_lyrics_type)

            # Create property conditions for Volume slider
            cond_volume_name = _uia_client.CreatePropertyCondition(_UIA.UIA_NamePropertyId, "Change volume")
            cond_volume_type = _uia_client.CreatePropertyCondition(_UIA.UIA_ControlTypePropertyId, 50015) # Slider
            cond_volume = _uia_client.CreateAndCondition(cond_volume_name, cond_volume_type)

            # Find elements natively in UIA (very fast)
            lyrics_elem = root.FindFirst(_UIA.TreeScope_Descendants, cond_lyrics)
            volume_elem = root.FindFirst(_UIA.TreeScope_Descendants, cond_volume)

            return lyrics_elem, volume_elem
        except Exception as e:
            self.log(f"[WARN] UIA search error: {e}")
            return None, None

    def capture_spotify_ui_state(self):
        """Save current lyrics toggle state and volume level before restart."""
        if not UIA_AVAILABLE:
            return
        try:
            lyrics_elem, volume_elem = self._find_uia_player_controls()

            if lyrics_elem:
                tog_pat = lyrics_elem.GetCurrentPattern(_UIA.UIA_TogglePatternId)
                toggle_client = tog_pat.QueryInterface(_UIA.IUIAutomationTogglePattern)
                lyrics_on = (toggle_client.CurrentToggleState == 1)
                if self._saved_lyrics_on is None or lyrics_on != self._saved_lyrics_on:
                    self._saved_lyrics_on = lyrics_on
                    self.log(f"[STATE] Lyrics is {'ON' if self._saved_lyrics_on else 'OFF'}")

            if volume_elem:
                range_pat = volume_elem.GetCurrentPattern(_UIA.UIA_RangeValuePatternId)
                range_client = range_pat.QueryInterface(_UIA.IUIAutomationRangeValuePattern)
                vol = range_client.CurrentValue
                if self._saved_volume is None or abs(vol - self._saved_volume) > 0.01:
                    self._saved_volume = vol
                    self.log(f"[STATE] Volume captured: {self._saved_volume:.2f}")
        except Exception:
            pass

    def restore_spotify_ui_state(self):
        """Restore lyrics toggle state and volume level after Spotify restarts."""
        if not UIA_AVAILABLE:
            return
        if self._saved_volume is None and self._saved_lyrics_on is None:
            return
        try:
            lyrics_elem, volume_elem = self._find_uia_player_controls()

            # Restore volume via SetValue (native) or click
            if volume_elem and self._saved_volume is not None:
                try:
                    range_pat = volume_elem.GetCurrentPattern(_UIA.UIA_RangeValuePatternId)
                    range_client = range_pat.QueryInterface(_UIA.IUIAutomationRangeValuePattern)
                    current_vol = range_client.CurrentValue
                    if abs(current_vol - self._saved_volume) > 0.01:
                        try:
                            # Try direct UIA SetValue
                            range_client.SetValue(self._saved_volume)
                            time.sleep(0.1)
                            if abs(range_client.CurrentValue - self._saved_volume) <= 0.02:
                                self.log(f"[RESTORE] Volume set to {self._saved_volume:.2f} via UIA SetValue")
                                volume_elem = None  # mark done
                        except Exception as e:
                            self.log(f"[RESTORE] SetValue failed: {e}. Falling back to click.")

                        if volume_elem is not None:
                            # Click the slider at the saved proportion
                            rect = volume_elem.CurrentBoundingRectangle
                            target_x = int(rect.left + (rect.right - rect.left) * self._saved_volume)
                            target_y = int((rect.top + rect.bottom) / 2)
                            
                            # Bring window to foreground to ensure click registers
                            hwnd, _ = self.find_spotify_window()
                            if hwnd:
                                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                                win32gui.SetForegroundWindow(hwnd)
                                time.sleep(0.1)
                                
                            ctypes.windll.user32.SetCursorPos(target_x, target_y)
                            time.sleep(0.05)
                            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
                            time.sleep(0.05)
                            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
                            time.sleep(0.2)
                            self.log(f"[RESTORE] Volume set to {self._saved_volume:.2f} via mouse click")
                    else:
                        self.log(f"[RESTORE] Volume unchanged at {current_vol:.2f}")
                except Exception as e:
                    self.log(f"[WARN] Volume restore error: {e}")

            # Restore lyrics toggle state
            if lyrics_elem and self._saved_lyrics_on is not None:
                try:
                    tog_pat = lyrics_elem.GetCurrentPattern(_UIA.UIA_TogglePatternId)
                    toggle_client = tog_pat.QueryInterface(_UIA.IUIAutomationTogglePattern)
                    current_lyrics_on = (toggle_client.CurrentToggleState == 1)
                    if current_lyrics_on != self._saved_lyrics_on:
                        try:
                            toggle_client.Toggle()
                            time.sleep(0.1)
                            if (toggle_client.CurrentToggleState == 1) == self._saved_lyrics_on:
                                self.log(f"[RESTORE] Lyrics toggled to {'ON' if self._saved_lyrics_on else 'OFF'} via UIA Toggle")
                                lyrics_elem = None  # mark done
                        except Exception as e:
                            self.log(f"[RESTORE] UIA Toggle failed: {e}. Falling back to click.")

                        if lyrics_elem is not None:
                            # Click the lyrics button to toggle it
                            rect = lyrics_elem.CurrentBoundingRectangle
                            cx = int((rect.left + rect.right) / 2)
                            cy = int((rect.top + rect.bottom) / 2)
                            
                            hwnd, _ = self.find_spotify_window()
                            if hwnd:
                                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                                win32gui.SetForegroundWindow(hwnd)
                                time.sleep(0.1)

                            ctypes.windll.user32.SetCursorPos(cx, cy)
                            time.sleep(0.05)
                            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
                            time.sleep(0.05)
                            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
                            time.sleep(0.2)
                            self.log(f"[RESTORE] Lyrics toggled to {'ON' if self._saved_lyrics_on else 'OFF'} via mouse click")
                    else:
                        self.log(f"[RESTORE] Lyrics already {'ON' if current_lyrics_on else 'OFF'}, no change needed")
                except Exception as e:
                    self.log(f"[WARN] Lyrics restore error: {e}")
        except Exception as e:
            self.log(f"[WARN] State restore error: {e}")

    # -- Process management --------------------------------

    def kill_spotify(self):
        """Terminate all Spotify.exe processes."""
        killed = False
        for proc in psutil.process_iter(["name"]):
            try:
                if (
                    proc.info["name"]
                    and proc.info["name"].lower() == "spotify.exe"
                ):
                    proc.kill()
                    killed = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if killed:
            self.log("[KILL] Spotify processes terminated")
        return killed

    def launch_spotify(self):
        """Start Spotify again."""
        if self.spotify_path and os.path.exists(self.spotify_path):
            subprocess.Popen(
                [self.spotify_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.log(f"[LAUNCH] Started: {self.spotify_path}")
        else:
            # Fallback: use the spotify: URI protocol
            os.startfile("spotify:")
            self.log("[LAUNCH] Started via spotify: protocol")

    # -- Media key -----------------------------------------

    @staticmethod
    def send_play_key():
        """Press the Play/Pause media key."""
        VK_MEDIA_PLAY_PAUSE = 0xB3
        KEYEVENTF_EXTENDEDKEY = 0x0001
        KEYEVENTF_KEYUP = 0x0002
        ctypes.windll.user32.keybd_event(
            VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_EXTENDEDKEY, 0
        )
        ctypes.windll.user32.keybd_event(
            VK_MEDIA_PLAY_PAUSE, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0
        )

    @staticmethod
    def send_next_key():
        """Press the Next Track media key."""
        VK_MEDIA_NEXT_TRACK = 0xB0
        KEYEVENTF_EXTENDEDKEY = 0x0001
        KEYEVENTF_KEYUP = 0x0002
        ctypes.windll.user32.keybd_event(
            VK_MEDIA_NEXT_TRACK, 0, KEYEVENTF_EXTENDEDKEY, 0
        )
        ctypes.windll.user32.keybd_event(
            VK_MEDIA_NEXT_TRACK, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0
        )

    # -- Logging -------------------------------------------

    def log(self, message: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}"
        # Safely print even on consoles that don't support Unicode (cp1252)
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", errors="replace").decode("ascii"))
        self.status_message = message

    # -- Main loop -----------------------------------------

    def run(self):
        """Polling loop - runs in a background thread."""
        self.log("=" * 50)
        self.log("Spotify Ad Restarter -- Active")
        self.log(
            f"Path: {self.spotify_path or 'protocol handler'}"
        )
        self.log(
            f"Poll {POLL_INTERVAL}s  |  Cooldown {COOLDOWN_PERIOD}s"
        )
        self.log("=" * 50)

        # Ensure Spotify is unmuted when monitor starts
        self.unmute_spotify()

        while self.running:
            try:
                self._tick()
            except Exception as e:
                self.log(f"[ERROR] {e}")
            time.sleep(POLL_INTERVAL)

        self.log("[STOP] Monitor stopped.")

    def _tick(self):
        """Single iteration of the monitor loop."""
        window = self.find_spotify_window()

        # -- No Spotify window -----------------------------
        if not window:
            if self.state not in (self.IDLE, self.RESTARTING):
                self.log("[WARN] Spotify window not found -- waiting...")
                self.state = self.IDLE
                self.status_message = "Waiting for Spotify..."
            return

        _, title = window

        if title != self.last_seen_title:
            self.last_seen_title = title
            self.log(f"[DEBUG] Spotify window title: \"{title}\"")

        # -- Cooldown check --------------------------------
        if self.state == self.COOLDOWN:
            elapsed = time.time() - self.last_restart_time
            remaining = COOLDOWN_PERIOD - elapsed
            if remaining > 0:
                mins, secs = divmod(int(remaining), 60)
                self.status_message = (
                    f"Cooldown: {mins}m {secs}s remaining"
                )
                return
            self.log("[OK] Cooldown complete -- resuming monitoring")
            self.state = (
                self.MUSIC_PLAYING
                if self.is_song_title(title)
                else self.IDLE
            )

        # -- Song playing ----------------------------------
        if self.is_song_title(title):
            if self.state != self.MUSIC_PLAYING:
                self.log(f"[PLAY] Now playing: {title}")
            self.state = self.MUSIC_PLAYING
            self.last_song_title = title
            self.status_message = f"Playing: {title}"
            self.capture_spotify_ui_state()
            return

        # -- Ad detected -----------------------------------
        if self.is_ad_playing(title):
            self.state = self.AD_DETECTED
            self.log(f'[AD] >>> AD DETECTED <<<  Title: "{title}"')
            self.log(f'     Last song: "{self.last_song_title}"')

            # 1. Instant mute
            self.mute_spotify()

            # 2. Kill immediately (no delay — state is already saved)
            self.kill_spotify()
            self.state = self.RESTARTING

            # 4. Short wait then relaunch
            if RESTART_DELAY > 0:
                self.log(f"[WAIT] Waiting {RESTART_DELAY}s before relaunch...")
                time.sleep(RESTART_DELAY)

            # 5. Relaunch
            self.launch_spotify()

            # 6. Wait for Spotify window to appear
            self.log("[WAIT] Waiting for Spotify window...")
            load_deadline = time.time() + LOAD_WAIT_TIME
            loaded = False
            while time.time() < load_deadline:
                time.sleep(0.5)
                if self.find_spotify_window():
                    loaded = True
                    break
            if not loaded:
                self.log("[WARN] Spotify window not detected within load window")

            # 7. Wait for the player UI to be fully ready
            #    (poll for UIA controls — the window shell appears fast but
            #     the internal web player takes longer to load)
            self.log("[WAIT] Waiting for player UI to be ready...")
            ui_ready = False
            ui_deadline = time.time() + 15  # max 15s total for UI
            while time.time() < ui_deadline:
                time.sleep(1.0)
                lyrics_elem, volume_elem = self._find_uia_player_controls()
                if lyrics_elem or volume_elem:
                    ui_ready = True
                    self.log("[OK] Player UI controls detected — Spotify is ready")
                    break
            if not ui_ready:
                self.log("[WARN] Player controls not found — continuing anyway")
                time.sleep(3)  # fallback fixed wait

            # 8. Resume playback — use audio peak to confirm it's actually playing
            for attempt in range(4):
                self.send_play_key()
                self.log(f"[PLAY] Play key sent (attempt {attempt + 1})")
                time.sleep(1.5)
                peak = self.get_spotify_peak_volume()
                if peak > 0.001:
                    self.log(f"[PLAY] Playback confirmed (audio peak: {peak:.4f})")
                    break
                self.log(f"[PLAY] No audio yet (peak: {peak:.4f}), retrying...")

            # 9. Unmute the new instance
            time.sleep(0.5)
            self.unmute_spotify()

            # 10. Restore saved UI state (volume + lyrics) with retries
            self.log("[RESTORE] Restoring UI state...")
            for restore_attempt in range(3):
                self.restore_spotify_ui_state()
                # Verify volume was restored
                if self._saved_volume is not None:
                    _, vol_elem = self._find_uia_player_controls()
                    if vol_elem:
                        try:
                            range_pat = vol_elem.GetCurrentPattern(_UIA.UIA_RangeValuePatternId)
                            range_client = range_pat.QueryInterface(_UIA.IUIAutomationRangeValuePattern)
                            current_vol = range_client.CurrentValue
                            if abs(current_vol - self._saved_volume) <= 0.05:
                                self.log(f"[RESTORE] Volume verified at {current_vol:.2f}")
                                break
                            else:
                                self.log(f"[RESTORE] Volume mismatch ({current_vol:.2f} vs {self._saved_volume:.2f}), retrying...")
                        except Exception:
                            pass
                else:
                    break
                time.sleep(1)

            # 11. Check if same song loaded — skip immediately if so
            self.log("[CHECK] Verifying song after restart...")
            for _ in range(5):
                time.sleep(1.0)
                new_window = self.find_spotify_window()
                if new_window:
                    _, new_title = new_window
                    if self.is_song_title(new_title):
                        if new_title == self.last_song_title:
                            self.log(f"[SKIP] Same song detected ('{new_title}') — skipping immediately")
                            self.send_next_key()
                        break

            # 12. Enter cooldown
            self.ads_skipped += 1
            self.last_restart_time = time.time()
            self.state = self.COOLDOWN
            self.log(
                f"[COOLDOWN] Started ({COOLDOWN_PERIOD}s)  |  "
                f"Total ads skipped: {self.ads_skipped}"
            )

    def stop(self):
        self.running = False
        # Ensure Spotify is unmuted when monitor stops
        self.unmute_spotify()


# ==========================================================
#  System tray icon
# ==========================================================

def _create_tray_image(color=(30, 215, 96)):
    """Generate a small Spotify-green icon with a play triangle."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Green circle background
    draw.ellipse([2, 2, size - 2, size - 2], fill=color)

    # White play triangle
    margin = 18
    draw.polygon(
        [
            (margin + 6, margin - 2),
            (margin + 6, size - margin + 2),
            (size - margin + 4, size // 2),
        ],
        fill="white",
    )
    return img


def _build_tray(monitor: SpotifyMonitor):
    """Create and return a pystray Icon."""

    def on_quit(icon, item):
        monitor.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(
            lambda text: f"Status: {monitor.state}",
            None,
            enabled=False,
        ),
        pystray.MenuItem(
            lambda text: f"Ads skipped: {monitor.ads_skipped}",
            None,
            enabled=False,
        ),
        pystray.MenuItem(
            lambda text: monitor.status_message,
            None,
            enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon(
        "spotify_ad_restarter",
        _create_tray_image(),
        "Spotify Ad Restarter",
        menu,
    )
    return icon


# ==========================================================
#  Entry point
# ==========================================================

def main():
    monitor = SpotifyMonitor()

    # Run the monitor loop in a daemon thread
    thread = threading.Thread(target=monitor.run, daemon=True)
    thread.start()

    # Build the system tray icon
    tray = _build_tray(monitor)

    # Install SIGINT handler so Ctrl+C stops the monitor and tray
    def _on_sigint(signum, frame):
        print("[INFO] SIGINT received -- shutting down")
        monitor.stop()
        try:
            tray.stop()
        except Exception:
            pass

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except Exception:
        # Some environments may not allow signal handling; ignore safely
        pass

    print("[INFO] System tray icon active -- right-click to quit.")
    try:
        tray.run()
    except KeyboardInterrupt:
        # Fallback if signals are delivered as KeyboardInterrupt
        print("[INFO] KeyboardInterrupt -- shutting down")
    finally:
        monitor.stop()
        thread.join(timeout=3)
        print("[INFO] Goodbye!")


if __name__ == "__main__":
    main()
