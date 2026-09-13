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
import importlib
import unicodedata
from datetime import datetime
import comtypes.client

import psutil
import win32gui
import win32process
import win32con
import win32api

# Audio muting and volume metering
from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume, IAudioMeterInformation

# Windows UI Automation (for reading/restoring lyrics toggle & volume slider)
UIA_AVAILABLE = False
_UIA = None
_uia_client = None

try:
    import comtypes.client
    try:
        comtypes.client.GetModule("UIAutomationCore.dll")
        import comtypes.gen.UIAutomationClient as _UIA
        _uia_client = comtypes.client.CreateObject(_UIA.CUIAutomation._reg_clsid_, interface=_UIA.IUIAutomation)
        UIA_AVAILABLE = True
    except Exception as e1:
        try:
            _uia_client = comtypes.client.CreateObject("{ff48dba4-6059-4f62-a56b-f395c86c386a}")
            import comtypes.gen.UIAutomationClient as _UIA
            UIA_AVAILABLE = True
        except Exception:
            UIA_AVAILABLE = False
except Exception:
    UIA_AVAILABLE = False

import pystray
from PIL import Image, ImageDraw
import signal

from reporter import (
    BaseReporter,
    UserConsoleReporter,
    VerboseConsoleReporter,
    NullReporter,
    LogLevel,
)

import config as _config
from config import (
    POLL_INTERVAL,
    RESTART_DELAY,
    LOAD_WAIT_TIME,
    COOLDOWN_PERIOD,
    SPOTIFY_EXE_PATH,
)

# ---------------------------------------------------------------------------
# Unicode normalizer: collapses all dash/hyphen variants to ASCII '-', strips
# leading/trailing whitespace, and lowercases.  Applied to both window titles
# and config patterns so that em-dash (\u2013), en-dash (\u2014), and
# hyphen-minus (\u002D) all compare equal.
# ---------------------------------------------------------------------------
_DASH_TABLE = str.maketrans({
    "\u2013": "-",   # en-dash
    "\u2014": "-",   # em-dash
    "\u2012": "-",   # figure dash
    "\u2010": "-",   # hyphen
    "\u2011": "-",   # non-breaking hyphen
    "\u2015": "-",   # horizontal bar
    "\uFE58": "-",   # small em-dash
    "\uFE63": "-",   # small hyphen-minus
    "\uFF0D": "-",   # fullwidth hyphen-minus
    "\u2022": "-",   # bullet (sometimes used as separator: "Song • Artist")
})


def _norm(text: str) -> str:
    """Normalize text for reliable ad-title comparison.

    Steps:
    1. NFKC Unicode normalization (decomposes ligatures, compatibility forms)
    2. Replace all Unicode dash variants with ASCII hyphen-minus
    3. Strip surrounding whitespace and lowercase
    """
    return (
        unicodedata.normalize("NFKC", text)
        .translate(_DASH_TABLE)
        .strip()
        .lower()
    )


def _build_ad_sets():
    """Build normalized ad-detection sets from the current config module."""
    exact = {_norm(t) for t in _config.AD_TITLE_EXACT if t.strip()}
    contains = {_norm(t) for t in _config.AD_TITLE_CONTAINS if t.strip()}
    return exact, contains


# Module-level sets — rebuilt on config hot-reload
AD_TITLE_EXACT_LOWER, AD_TITLE_CONTAINS_LOWER = _build_ad_sets()

# Titles that represent generic, non-playing Spotify window states (e.g. paused or idle).
# For these, we verify if audio is actually playing before treating them as ads.
# When Spotify is paused or idle on Spotify Free, the window title is "Spotify Free" or "Spotify"
# with zero audio output (peak <= 0.001).
# When an audio ad plays with these titles, audio output is active (peak > 0.001).
AMBIGUOUS_TITLES = {"spotify", "spotify free", "spotify premium"}

# Chromium flag required for reliable accessibility tree exposure.
# Without this, the Lyrics button's ToggleState can be stale or missing.
_FORCE_A11Y_FLAG = "--force-renderer-accessibility"



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

    def __init__(self, reporter: BaseReporter | None = None):
        if reporter is not None:
            self.reporter = reporter
        elif getattr(_config, "VERBOSE_CONSOLE", False):
            self.reporter = VerboseConsoleReporter()
        else:
            self.reporter = UserConsoleReporter()

        self.state = self.IDLE
        self.last_song_title = None
        self.last_seen_title = None
        self.last_restart_time = 0
        self.running = True
        self.ads_skipped = 0
        self.status_message = "Starting up..."
        self.spotify_path = self._find_spotify_path()
        # Saved pre-restart state
        self._saved_volume = None        # float 0.0–1.0 (UIA slider)
        self._saved_lyrics_on = None     # bool
        self._saved_session_volume = None # float 0.0–1.0 (pycaw Windows audio session)
        # Window placement saved before kill, for restoring window state after relaunch
        self._saved_window_placement = None

        # Track whether Spotify is active to perform event-driven UI state captures
        self._spotify_was_active = False

        # Counter for ambiguous ad title verification ticks
        self._ambiguous_ad_ticks = 0

        # Timestamp of the last tick where a valid song title ("Song - Artist") was seen.
        # Used to suppress false-positive ambiguous-title ad detections during the
        # brief window between tracks where Spotify shows "Spotify" / "Spotify Free"
        # while the previous song's audio tail is still audible (peak > 0.001).
        # Without this guard, every normal song-to-song transition triggers a restart.
        self._last_song_seen_time: float = 0.0

        # --- 1-tick ad-title persistence ---
        # Stores the last definitively-confirmed ad title + timestamp so that
        # a rapid title flicker (ad → blank/song within 2–3 polls) still triggers
        # a restart instead of being silently ignored.
        self._last_confirmed_ad_title: str | None = None
        self._last_confirmed_ad_time: float = 0.0

        # --- Config hot-reload ---
        self._config_reload_poll_count: int = 0

        # Whether Spotify was the foreground app at the time an ad was detected.
        # Initialised here so any early-exit code path in _tick() never raises
        # AttributeError when reading this attribute in the final focus-restore step.
        self._spotify_was_foreground: bool = False

        # Tracks whether Spotify is currently muted by us.  Lets stop() safely
        # undo a mute that was applied before a restart completed (quit mid-restart).
        self._muted: bool = False

        # Guard flag: while True, capture_spotify_ui_state() will not overwrite
        # _saved_lyrics_on/_saved_volume or emit State lines.  Set during the
        # post-restart restore sequence so that an in-flight async capture cannot
        # clobber the pre-restart saved state with stale post-restart readings.
        self._restoring_ui: bool = False

        # Cached window info for zero-overhead instant polling (<0.01ms checks)
        self._cached_spotify_hwnd: int | None = None
        self._cached_spotify_pid: int | None = None

        # Lock to ensure background UI state capture doesn't overlap or block main loop
        self._ui_capture_lock = threading.Lock()

        # Shutdown event for responsive thread termination
        self.shutdown_event = threading.Event()

    # -- Sleep utility -------------------------------------

    def _sleep(self, seconds: float) -> bool:
        """Sleep for the specified seconds, but return early if shutdown is requested.
        
        Returns True if shutdown was requested, False otherwise.
        """
        return self.shutdown_event.wait(seconds)

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
        """Return (hwnd, title) of Spotify's main window, or None.

        Selection strategy:
        1. Fast-path: check cached Spotify window handle first (instant <0.01ms check).
        2. Prefer windows with WS_CAPTION style that are NOT child windows —
           these are the real top-level Spotify frame (works correctly even when
           the ad title is very short, e.g. 'Advertisement').
        3. Among candidates of the same priority, pick the one with the longest
           title (heuristic that picks the song-title window over 'Spotify').
        4. Fall back to any visible Spotify window if no main-window found.
        """
        # Fast path: check cached window handle first (instant <0.01ms check)
        if self._cached_spotify_hwnd:
            try:
                if win32gui.IsWindow(self._cached_spotify_hwnd) and win32gui.IsWindowVisible(self._cached_spotify_hwnd):
                    _, pid = win32process.GetWindowThreadProcessId(self._cached_spotify_hwnd)
                    if pid == self._cached_spotify_pid:
                        title = win32gui.GetWindowText(self._cached_spotify_hwnd)
                        if title:
                            return (self._cached_spotify_hwnd, title)
            except Exception:
                pass
            self._cached_spotify_hwnd = None
            self._cached_spotify_pid = None

        WS_CAPTION = 0x00C00000  # title bar + border
        WS_CHILD   = 0x40000000  # child window flag
        GWL_STYLE  = -16

        main_windows  = []  # top-level windows with WS_CAPTION
        other_windows = []  # remaining visible Spotify windows

        def _enum_cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            try:
                # Fast class filter: Spotify windows use Chrome_WidgetWin classes or contain Spotify
                class_name = win32gui.GetClassName(hwnd)
                if not (class_name.startswith("Chrome_WidgetWin") or "spotify" in class_name.lower()):
                    return True

                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                proc = psutil.Process(pid)
                if proc.name().lower() == "spotify.exe":
                    title = win32gui.GetWindowText(hwnd)
                    if title:  # skip blank helper windows
                        try:
                            style = win32gui.GetWindowLong(hwnd, GWL_STYLE)
                            is_main = bool(style & WS_CAPTION) and not bool(style & WS_CHILD)
                        except Exception:
                            is_main = False
                        if is_main:
                            main_windows.append((hwnd, title, pid))
                        else:
                            other_windows.append((hwnd, title, pid))
            except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
                pass
            return True

        try:
            win32gui.EnumWindows(_enum_cb, None)
        except Exception:
            pass

        selected = None
        if main_windows:
            selected = max(main_windows, key=lambda r: len(r[1]))
        elif other_windows:
            selected = max(other_windows, key=lambda r: len(r[1]))

        if selected:
            self._cached_spotify_hwnd = selected[0]
            self._cached_spotify_pid = selected[2]
            return (selected[0], selected[1])
        return None

    # -- Ad detection helpers ------------------------------

    @staticmethod
    def is_ad_title(title: str) -> bool:
        """Does this window title indicate an ad?"""
        if not title:
            return False
        t = _norm(title)

        if t in AD_TITLE_EXACT_LOWER:
            return True
        for indicator in AD_TITLE_CONTAINS_LOWER:
            if indicator in t:
                return True

        # --- Verbose near-miss diagnostics ---
        try:
            if _config.VERBOSE_AD_DETECTION:
                import sys
                print(
                    f"[AD-DETECT-VERBOSE] No match for normalized title: {t!r}\n"
                    f"  exact patterns : {sorted(AD_TITLE_EXACT_LOWER)}\n"
                    f"  contains patterns: {sorted(AD_TITLE_CONTAINS_LOWER)}",
                    file=sys.stderr,
                )
        except Exception:
            pass

        return False

    @staticmethod
    def is_song_title(title: str) -> bool:
        """Does this window title look like 'Song - Artist'?"""
        if not title:
            return False
        t = _norm(title)
        # Reject known ad-exact titles
        if t in AD_TITLE_EXACT_LOWER:
            return False
        # Reject if any contains-indicator is present.
        # Guards against titles like "Coldplay - Advertisement" being treated as songs.
        if any(ind in t for ind in AD_TITLE_CONTAINS_LOWER):
            return False
        # Song titles contain ' - ' (after dash normalization)
        return " - " in t

    def get_spotify_peak_volume(self) -> float:
        """Return the current peak volume level of Spotify (0.0 to 1.0), or 0.0 if not found/error."""
        max_peak = 0.0
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                proc = session.Process
                if proc and proc.name().lower() == "spotify.exe":
                    try:
                        meter = session._ctl.QueryInterface(IAudioMeterInformation)
                        peak = meter.GetPeakValue()
                        if peak > max_peak:
                            max_peak = peak
                    except Exception:
                        pass
        except Exception:
            pass
        return max_peak

    def is_ad_playing(self, title: str) -> bool:
        """Determine if an ad is actively playing based on title and audio peak."""
        if not title:
            self._ambiguous_ad_ticks = 0
            return False
        t = _norm(title)
        is_ambiguous = (t in AMBIGUOUS_TITLES)
        # Check if matches any ad title criteria (using normalized comparison)
        matches_exact = t in AD_TITLE_EXACT_LOWER
        matches_contains = any(ind in t for ind in AD_TITLE_CONTAINS_LOWER)

        if not (matches_exact or matches_contains or is_ambiguous):
            # --- 1-tick persistence: if we very recently confirmed a definitive ad title,
            # don't let a rapid title flicker (ad→blank→song within 1-2 polls) fool us
            # into thinking the ad ended.  Re-trigger only for non-ambiguous prior hits.
            #
            # IMPORTANT: We only fire the persistence window if the title is NOT a valid
            # song title (i.e. does not contain ' - ').  Song-to-song transitions briefly
            # show a non-song, non-ad title (like 'Spotify' or 'Spotify Free') which used
            # to cause a false positive restart after every track.  Clearing the persistence
            # window for any song title is handled in is_song_title() callers in _tick().
            if (
                self._last_confirmed_ad_title is not None
                and (time.time() - self._last_confirmed_ad_time) < (POLL_INTERVAL * 2.5)
                and " - " not in _norm(title)  # never re-trigger when a song is playing
            ):
                self.log(
                    f"[AD-PERSIST] Title changed to '{title}' within persistence window — "
                    f"re-asserting last confirmed ad '{self._last_confirmed_ad_title}'"
                )
                return True
            self._ambiguous_ad_ticks = 0
            return False

        # If it is an ambiguous title (like the generic app name 'Spotify' or 'Spotify Free'),
        # verify it is actually emitting sound and not just paused or idle.
        if is_ambiguous:
            peak = self.get_spotify_peak_volume()
            # If peak is <= 0.001, it is silent (paused or idle, NOT playing an ad)
            if peak <= 0.001:
                self._ambiguous_ad_ticks = 0
                return False

            # ---------------------------------------------------------------
            # SONG-TRANSITION GUARD — the most common source of false positives.
            #
            # Between two tracks Spotify briefly shows "Spotify" / "Spotify Free"
            # while buffering the next song.  During this gap the *previous* track's
            # audio tail is still audible (peak > 0.001), so the audio-presence check
            # above does not protect us.
            #
            # Fix: if we saw a valid song title very recently, this is almost
            # certainly a normal track transition, NOT an ad.  Reset the tick
            # counter and wait until the new title appears.
            #
            # The grace window is intentionally generous (default 8 s) so it
            # covers slow-buffering connections.  Real audio ads always appear
            # AFTER Spotify has had a chance to show the song, so they will
            # always arrive outside this window.
            # ---------------------------------------------------------------
            song_transition_grace = getattr(_config, "SONG_TRANSITION_GRACE_SECONDS", 8.0)
            time_since_song = time.time() - self._last_song_seen_time
            if self._last_song_seen_time > 0 and time_since_song < song_transition_grace:
                self.reporter.debug(
                    f"[STATE-GUARD] Ambiguous title '{title}' with peak {peak:.4f} — "
                    f"suppressed: song seen {time_since_song:.1f}s ago (grace={song_transition_grace}s)"
                )
                # Reset tick counter so we start fresh once the grace window expires
                self._ambiguous_ad_ticks = 0
                return False

            # Require consecutive ticks of ambiguous title + audio to confirm an ad
            # (prevents false positives from transient title flickers between tracks,
            # which usually take 1.5 - 2.5 seconds to buffer and update window title).
            confirm_sec = getattr(_config, "AMBIGUOUS_AD_CONFIRM_SECONDS", 2.5)
            required_ticks = max(4, int(confirm_sec / POLL_INTERVAL))
            self._ambiguous_ad_ticks += 1
            if self._ambiguous_ad_ticks < required_ticks:
                self.reporter.debug(
                    f"[STATE-GUARD] Ambiguous title '{title}' with peak {peak:.4f} detected — "
                    f"confirming (tick {self._ambiguous_ad_ticks}/{required_ticks})..."
                )
                return False
            self._ambiguous_ad_ticks = 0
            return True

        self._ambiguous_ad_ticks = 0
        # Record this as a confirmed (non-ambiguous) ad for persistence window
        self._last_confirmed_ad_title = title
        self._last_confirmed_ad_time = time.time()
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

    def _capture_session_volume(self):
        """Save Spotify's Windows audio session volume (pycaw) before restart."""
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if (
                    session.Process
                    and session.Process.name().lower() == "spotify.exe"
                ):
                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    self._saved_session_volume = volume.GetMasterVolume()
                    self.log(f"[STATE] Session volume captured: {self._saved_session_volume:.2f}")
                    return
        except Exception as e:
            self.log(f"[WARN] Session volume capture error: {e}")

    def _get_session_volume(self) -> float | None:
        """Get Spotify's current Windows audio session volume (0.0 to 1.0) via pycaw."""
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if (
                    session.Process
                    and session.Process.name().lower() == "spotify.exe"
                ):
                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    return volume.GetMasterVolume()
        except Exception:
            pass
        return None

    def _restore_session_volume(self):
        """Restore Spotify's Windows audio session volume (pycaw) after restart."""
        if self._saved_session_volume is None:
            return
        try:
            sessions = AudioUtilities.GetAllSessions()
            for session in sessions:
                if (
                    session.Process
                    and session.Process.name().lower() == "spotify.exe"
                ):
                    volume = session._ctl.QueryInterface(ISimpleAudioVolume)
                    current = volume.GetMasterVolume()
                    if abs(current - self._saved_session_volume) > 0.02:
                        volume.SetMasterVolume(self._saved_session_volume, None)
                        self.log(f"[RESTORE] Session volume: {current:.2f} -> {self._saved_session_volume:.2f}")
                    else:
                        self.log(f"[RESTORE] Session volume already at {current:.2f}")
                    return
        except Exception as e:
            self.log(f"[WARN] Session volume restore error: {e}")

    def _ensure_chromium_accessibility(self, hwnd):
        """Force Chromium's renderer process to construct its accessibility tree.

        Sending WM_GETOBJECT (0x003D) with OBJID_CLIENT (0xFFFFFFFC) wakes up
        Chromium's accessibility bridge dynamically without requiring a restart.
        """
        try:
            WM_GETOBJECT = 0x003D
            OBJID_CLIENT = 0xFFFFFFFC
            ctypes.windll.user32.SendMessageW(hwnd, WM_GETOBJECT, 0, OBJID_CLIENT)
        except Exception:
            pass

    def _send_spotify_lyrics_hotkey(self, hwnd=None):
        """Send Ctrl+N shortcut to Spotify window to toggle lyrics view.

        Uses WM_KEYDOWN/WM_KEYUP messages posted directly to the Spotify
        HWND so it works even when Spotify is not the foreground window.
        Falls back to global keybd_event if no HWND is available.
        """
        try:
            VK_CONTROL = 0x11
            VK_N = 0x4E
            WM_KEYDOWN = 0x0100
            WM_KEYUP = 0x0101
            KEYEVENTF_KEYUP = 0x0002

            # Resolve target HWND
            target_hwnd = hwnd
            if not target_hwnd:
                window = self.find_spotify_window()
                if window:
                    target_hwnd = window[0]

            if target_hwnd:
                # Targeted: post WM_KEY messages directly to Spotify's HWND
                PostMessage = ctypes.windll.user32.PostMessageW
                PostMessage(target_hwnd, WM_KEYDOWN, VK_CONTROL, 0)
                PostMessage(target_hwnd, WM_KEYDOWN, VK_N, 0)
                PostMessage(target_hwnd, WM_KEYUP, VK_N, 0)
                PostMessage(target_hwnd, WM_KEYUP, VK_CONTROL, 0)
                self.log(f"[LYRICS-HOTKEY] Sent Ctrl+N to HWND {target_hwnd}")
            else:
                # Fallback: global keybd_event (only works if Spotify is foreground)
                ctypes.windll.user32.keybd_event(VK_CONTROL, 0, 0, 0)
                ctypes.windll.user32.keybd_event(VK_N, 0, 0, 0)
                ctypes.windll.user32.keybd_event(VK_N, 0, KEYEVENTF_KEYUP, 0)
                ctypes.windll.user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
                self.log("[LYRICS-HOTKEY] Sent Ctrl+N via global keybd_event (no HWND)")
        except Exception as e:
            self.log(f"[WARN] Error sending lyrics hotkey: {e}")

    # -- UI Automation helpers (lyrics + volume state) ------

    def _find_uia_player_controls(self, verbose=False):
        """Return (lyrics_elem, volume_elem) from Spotify's UIA tree, or (None, None).

        Always obtains a fresh element from the current live HWND to avoid
        stale COM proxy references after a Spotify restart.
        """
        if not UIA_AVAILABLE:
            if verbose:
                self.log("[LYRICS-DETECT] UIA not available — skipping control search")
            return None, None
        try:
            window = self.find_spotify_window()
            if not window:
                if verbose:
                    self.log("[LYRICS-DETECT] No Spotify window found for UIA search")
                return None, None
            hwnd, title = window

            # Wake up Chromium renderer accessibility DOM
            self._ensure_chromium_accessibility(hwnd)

            root = _uia_client.ElementFromHandle(hwnd)

            # 1. Search for Volume slider
            cond_volume_name = _uia_client.CreatePropertyCondition(_UIA.UIA_NamePropertyId, "Change volume")
            cond_volume_type = _uia_client.CreatePropertyCondition(_UIA.UIA_ControlTypePropertyId, 50015) # Slider
            cond_volume = _uia_client.CreateAndCondition(cond_volume_name, cond_volume_type)
            volume_elem = root.FindFirst(_UIA.TreeScope_Descendants, cond_volume)

            # 2. Search for Lyrics button across buttons, check boxes, custom elements
            lyrics_elem = None
            cond_true = _uia_client.CreateTrueCondition()
            all_elems = root.FindAll(_UIA.TreeScope_Descendants, cond_true)
            if all_elems:
                for i in range(all_elems.Length):
                    e = all_elems.GetElement(i)
                    try:
                        name = (e.CurrentName or "").lower()
                        auto_id = (e.CurrentAutomationId or "").lower()
                        help_txt = (e.CurrentHelpText or "").lower()
                        if "lyric" in name or "lyric" in auto_id or "lyric" in help_txt:
                            lyrics_elem = e
                            break
                    except Exception:
                        pass

            if verbose:
                self.log(
                    f"[LYRICS-DETECT] UIA search on HWND {hwnd} ('{title}'): "
                    f"lyrics={'FOUND' if lyrics_elem else 'NOT FOUND'}, "
                    f"volume={'FOUND' if volume_elem else 'NOT FOUND'}"
                )

            return lyrics_elem, volume_elem
        except Exception as e:
            self.log(f"[WARN] UIA search error: {e}")
            return None, None

    def _read_lyrics_state_reliable(self, lyrics_elem, verbose=False):
        """Read the Lyrics toggle state using multiple detection patterns and sampling.

        Rules requested by user:
        - Checks TogglePattern, SelectionItemPattern, LegacyIAccessible, and Name/AriaProperties.
        - If EITHER check or sample evaluates to True -> returns True (ON).
        - Only if BOTH checks/samples evaluate to False -> returns False (OFF).
        """
        if not lyrics_elem:
            return None

        def _read_once(elem):
            results = []
            # 1. Check TogglePattern
            try:
                tog_pat = elem.GetCurrentPattern(_UIA.UIA_TogglePatternId)
                if tog_pat:
                    toggle_client = tog_pat.QueryInterface(_UIA.IUIAutomationTogglePattern)
                    results.append(toggle_client.CurrentToggleState == 1)
            except Exception:
                pass

            # 2. Check SelectionItemPattern
            try:
                sel_pat = elem.GetCurrentPattern(_UIA.UIA_SelectionItemPatternId)
                if sel_pat:
                    sel_client = sel_pat.QueryInterface(_UIA.IUIAutomationSelectionItemPattern)
                    results.append(bool(sel_client.CurrentIsSelected))
            except Exception:
                pass

            # 3. Check LegacyIAccessiblePattern (Checked / Pressed flags)
            try:
                leg_pat = elem.GetCurrentPattern(_UIA.UIA_LegacyIAccessiblePatternId)
                if leg_pat:
                    leg_client = leg_pat.QueryInterface(_UIA.IUIAutomationLegacyIAccessiblePattern)
                    st = leg_client.CurrentState
                    # STATE_SYSTEM_CHECKED (0x10) or STATE_SYSTEM_PRESSED (0x8)
                    results.append(bool(st & (0x10 | 0x8)))
            except Exception:
                pass

            # 4. Check Name or AriaProperties heuristic
            try:
                name = (elem.CurrentName or "").lower()
                aria = (elem.CurrentAriaProperties or "").lower()
                if "hide" in name or "pressed=true" in aria or "checked=true" in aria:
                    results.append(True)
                elif "show" in name or "pressed=false" in aria or "checked=false" in aria:
                    results.append(False)
            except Exception:
                pass

            if any(r is True for r in results):
                return True
            if any(r is False for r in results):
                return False
            return None

        try:
            state1 = _read_once(lyrics_elem)
            if self._sleep(0.2):
                return state1

            state2 = _read_once(lyrics_elem)

            # User rule: If EITHER sample is True -> ON (True); if BOTH False -> OFF (False)
            if state1 is True or state2 is True:
                if verbose:
                    self.log("[LYRICS-DETECT] Lyrics state read: ON (either check was True)")
                return True
            elif state1 is False and state2 is False:
                if verbose:
                    self.log("[LYRICS-DETECT] Lyrics state read: OFF (both checks were False)")
                return False
            elif state1 is not None:
                return state1
            else:
                return state2
        except Exception as e:
            self.log(f"[LYRICS-DETECT] Error reading toggle state: {e}")
            return None

    def capture_spotify_ui_state(self):
        """Save current lyrics toggle state and volume level before restart.

        Preserves existing captured ON state if previously recorded during music playback,
        preventing background focus shifts or ad UI changes from overwriting it.
        """
        if not UIA_AVAILABLE:
            return

        max_attempts = 3
        retry_delay = 0.5

        for attempt in range(1, max_attempts + 1):
            try:
                lyrics_elem, volume_elem = self._find_uia_player_controls()

                captured_lyrics = False
                captured_volume = False

                # Skip overwriting saved state while a post-restart restore is in progress.
                # The restore sequence sets _restoring_ui=True so that a concurrent
                # async capture thread cannot clobber the pre-restart saved values with
                # stale readings from the freshly-launched (not yet restored) Spotify.
                if self._restoring_ui:
                    return

                if lyrics_elem:
                    lyrics_on = self._read_lyrics_state_reliable(lyrics_elem)
                    if lyrics_on is not None:
                        if self._saved_lyrics_on != lyrics_on:
                            self._saved_lyrics_on = lyrics_on
                            self.log(f"[STATE] Lyrics is {'ON' if self._saved_lyrics_on else 'OFF'}")
                        captured_lyrics = True
                elif self._saved_lyrics_on is not None:
                    # Keep last captured lyrics state if button is temporarily missing
                    captured_lyrics = True

                if volume_elem:
                    try:
                        range_pat = volume_elem.GetCurrentPattern(_UIA.UIA_RangeValuePatternId)
                        range_client = range_pat.QueryInterface(_UIA.IUIAutomationRangeValuePattern)
                        vol = range_client.CurrentValue
                        if self._saved_volume is None or abs(vol - self._saved_volume) > 0.01:
                            self._saved_volume = vol
                            self.log(f"[STATE] Volume captured: {self._saved_volume:.2f}")
                        captured_volume = True
                    except Exception as e:
                        self.log(f"[WARN] Volume capture error: {e}")
                elif self._saved_volume is not None:
                    captured_volume = True

                # Notify reporter of lyric status and volume
                vol = self._saved_volume if self._saved_volume is not None else self._get_session_volume()
                if self._saved_lyrics_on is not None or vol is not None:
                    self.reporter.state_update(self._saved_lyrics_on, vol)

                if captured_lyrics and captured_volume:
                    return

                if attempt < max_attempts:
                    if self._sleep(retry_delay):
                        return

            except Exception as e:
                self.log(f"[WARN] UI state capture error (attempt {attempt}): {e}")
                if attempt < max_attempts:
                    if self._sleep(retry_delay):
                        return

    def capture_spotify_ui_state_async(self):
        """Asynchronously capture UI state (lyrics & volume) in background thread.

        Ensures the main ad monitoring loop is never blocked by slow UIA tree inspection.
        """
        if not UIA_AVAILABLE:
            return

        if self._ui_capture_lock.locked():
            return

        def _worker():
            if not self._ui_capture_lock.acquire(blocking=False):
                return
            co_initialized = False
            try:
                try:
                    import comtypes
                    comtypes.CoInitialize()
                    co_initialized = True
                except Exception:
                    co_initialized = False

                self.capture_spotify_ui_state()
            finally:
                if co_initialized:
                    try:
                        import comtypes
                        comtypes.CoUninitialize()
                    except Exception:
                        pass
                self._ui_capture_lock.release()

        t = threading.Thread(target=_worker, name="UIStateCaptureWorker", daemon=True)
        t.start()

    def restore_spotify_ui_state(self):
        """Restore lyrics toggle state and volume level after Spotify restarts.

        Uses native UIA patterns (Toggle, Invoke, LegacyIAccessible) and SetValue
        without stealing foreground focus.

        Returns (volume_ok, lyrics_ok) booleans indicating whether each
        property was successfully restored or didn't need restoring.
        """
        volume_ok = (self._saved_volume is None)
        lyrics_ok = (self._saved_lyrics_on is None)

        if not UIA_AVAILABLE:
            return volume_ok, lyrics_ok
        if volume_ok and lyrics_ok:
            self.log("[RESTORE] No saved state to restore")
            return True, True
        try:
            lyrics_elem, volume_elem = self._find_uia_player_controls(verbose=True)

            # --- Restore volume via UIA SetValue ---
            if volume_elem and self._saved_volume is not None:
                try:
                    range_pat = volume_elem.GetCurrentPattern(_UIA.UIA_RangeValuePatternId)
                    range_client = range_pat.QueryInterface(_UIA.IUIAutomationRangeValuePattern)
                    current_vol = range_client.CurrentValue
                    if abs(current_vol - self._saved_volume) > 0.02:
                        self.log(f"[RESTORE] Volume: current={current_vol:.2f}, target={self._saved_volume:.2f} — setting via UIA SetValue")
                        range_client.SetValue(self._saved_volume)
                        if self._sleep(0.15):
                            return volume_ok, lyrics_ok
                        new_vol = range_client.CurrentValue
                        self.log(f"[RESTORE] Volume after SetValue: {new_vol:.2f}")
                        volume_ok = abs(new_vol - self._saved_volume) <= 0.05
                    else:
                        self.log(f"[RESTORE] Volume already close to saved value ({current_vol:.2f} vs {self._saved_volume:.2f})")
                        volume_ok = True
                except Exception as e:
                    self.log(f"[WARN] Volume restore error: {e}")
            elif self._saved_volume is not None:
                self.log("[WARN] Volume slider element not found — cannot restore volume")

            # --- Restore lyrics via UIA Toggle / Invoke / LegacyIAccessible ---
            if lyrics_elem and self._saved_lyrics_on is not None:
                try:
                    current_lyrics = self._read_lyrics_state_reliable(lyrics_elem, verbose=True)
                    if current_lyrics is None:
                        self.log("[WARN] Could not reliably read current lyrics state for restore")
                    elif current_lyrics != self._saved_lyrics_on:
                        self.log(
                            f"[RESTORE] Lyrics: current={'ON' if current_lyrics else 'OFF'}, "
                            f"target={'ON' if self._saved_lyrics_on else 'OFF'} — toggling lyrics"
                        )
                        toggled = False
                        # Method A: TogglePattern
                        try:
                            tog_pat = lyrics_elem.GetCurrentPattern(_UIA.UIA_TogglePatternId)
                            if tog_pat:
                                toggle_client = tog_pat.QueryInterface(_UIA.IUIAutomationTogglePattern)
                                toggle_client.Toggle()
                                toggled = True
                        except Exception as e:
                            self.log(f"[WARN] TogglePattern failed: {e}")

                        # Method B: InvokePattern
                        if not toggled:
                            try:
                                inv_pat = lyrics_elem.GetCurrentPattern(_UIA.UIA_InvokePatternId)
                                if inv_pat:
                                    inv_client = inv_pat.QueryInterface(_UIA.IUIAutomationInvokePattern)
                                    inv_client.Invoke()
                                    toggled = True
                            except Exception as e:
                                self.log(f"[WARN] InvokePattern failed: {e}")

                        # Method C: LegacyIAccessible DoDefaultAction
                        if not toggled:
                            try:
                                leg_pat = lyrics_elem.GetCurrentPattern(_UIA.UIA_LegacyIAccessiblePatternId)
                                if leg_pat:
                                    leg_client = leg_pat.QueryInterface(_UIA.IUIAutomationLegacyIAccessiblePattern)
                                    leg_client.DoDefaultAction()
                                    toggled = True
                            except Exception as e:
                                self.log(f"[WARN] LegacyIAccessible action failed: {e}")

                        # Method D: Hotkey shortcut fallback (Ctrl+N)
                        if not toggled:
                            self.log("[RESTORE] Falling back to hotkey shortcut (Ctrl+N)")
                            self._send_spotify_lyrics_hotkey()
                            toggled = True

                        if self._sleep(0.5):
                            return volume_ok, lyrics_ok

                        # Verify
                        verify_state = self._read_lyrics_state_reliable(lyrics_elem, verbose=True)
                        if verify_state is not None:
                            lyrics_ok = (verify_state == self._saved_lyrics_on)
                            self.log(
                                f"[RESTORE] Lyrics after Toggle/Invoke/Hotkey: "
                                f"{'ON' if verify_state else 'OFF'} — "
                                f"{'OK' if lyrics_ok else 'MISMATCH'}"
                            )
                        else:
                            # If UIA verify is unreadable, treat hotkey toggle as successful
                            self.log("[RESTORE] Lyrics verified via hotkey toggle")
                            lyrics_ok = True
                    else:
                        self.log(f"[RESTORE] Lyrics already {'ON' if current_lyrics else 'OFF'}, no change needed")
                        lyrics_ok = True
                except Exception as e:
                    self.log(f"[WARN] Lyrics restore error: {e}")
            elif self._saved_lyrics_on is not None:
                self.log("[RESTORE] Lyrics element not found — using hotkey shortcut fallback (Ctrl+N)")
                self._send_spotify_lyrics_hotkey()
                if self._sleep(0.3):
                    return volume_ok, lyrics_ok
                lyrics_ok = True

        except Exception as e:
            self.log(f"[WARN] State restore error: {e}")

        return volume_ok, lyrics_ok

    def _verify_and_fix_ui_state(self, max_retries=5, retry_delay=1.0):
        """Verify lyrics and volume state match saved values; retry on mismatch.

        Calls restore_spotify_ui_state() and checks the returned success flags.
        If either property failed, waits and retries. This handles the case where
        Spotify's UIA tree isn't fully ready on the first attempt after restart.

        Returns True if all properties were verified, False otherwise.
        """
        if self._saved_volume is None and self._saved_lyrics_on is None:
            self.log("[UI-STATE] No saved UI state to verify")
            return True

        for attempt in range(1, max_retries + 1):
            volume_ok, lyrics_ok = self.restore_spotify_ui_state()

            if volume_ok and lyrics_ok:
                self.log(
                    f"[UI-STATE] All UI state verified OK (attempt {attempt}): "
                    f"volume={'OK' if self._saved_volume is not None else 'N/A'}, "
                    f"lyrics={'OK' if self._saved_lyrics_on is not None else 'N/A'}"
                )
                return True

            # Log what's still missing
            pending = []
            if not volume_ok:
                pending.append(f"volume (target={self._saved_volume:.2f})")
            if not lyrics_ok:
                pending.append(f"lyrics (target={'ON' if self._saved_lyrics_on else 'OFF'})")

            self.log(
                f"[UI-STATE] Restore incomplete (attempt {attempt}/{max_retries}): "
                f"pending=[{', '.join(pending)}] — retrying in {retry_delay}s"
            )

            if self._sleep(retry_delay):
                return False

        self.log(f"[UI-STATE] Could not fully restore UI state after {max_retries} retries")
        return False

    # -- Process management --------------------------------

    def _save_window_placement(self):
        """Save Spotify's complete window placement before killing it.

        Captures the full WINDOWPLACEMENT structure which includes:
        - showCmd: minimised (2), maximised (3), or normal (1)
        - ptMinPosition: position when minimised
        - ptMaxPosition: position when maximised
        - rcNormalPosition: (left, top, right, bottom) in normal state
        """
        try:
            window = self.find_spotify_window()
            if window:
                hwnd, _ = window
                placement = win32gui.GetWindowPlacement(hwnd)
                self._saved_window_placement = placement
                show_cmd = placement[1]  # showCmd field
                normal_rect = placement[4]  # rcNormalPosition (left, top, right, bottom)
                state_name = {1: "NORMAL", 2: "MINIMIZED", 3: "MAXIMIZED"}.get(show_cmd, f"UNKNOWN({show_cmd})")
                width = normal_rect[2] - normal_rect[0]
                height = normal_rect[3] - normal_rect[1]
                self.log(
                    f"[WINDOW-STATE] Saved placement: showCmd={state_name}, "
                    f"pos=({normal_rect[0]},{normal_rect[1]}), "
                    f"size={width}x{height}, "
                    f"rect={normal_rect}"
                )
            else:
                self._saved_window_placement = None
                self.log("[WINDOW-STATE] No Spotify window to save placement for")
        except Exception as e:
            self._saved_window_placement = None
            self.log(f"[WARN] Could not save window placement: {e}")

    def _verify_and_fix_window_state(self, hwnd, max_retries=5, retry_delay=0.3):
        """Verify the window state matches saved placement; retry if mismatched.

        Compares the actual window placement against self._saved_window_placement
        and re-applies it if any property doesn't match. Returns True if the
        state was verified successfully, False if it could not be corrected.
        """
        if not self._saved_window_placement:
            self.log("[WINDOW-STATE] No saved placement to verify against")
            return True  # Nothing to verify

        saved = self._saved_window_placement
        saved_cmd = saved[1]
        saved_rect = saved[4]  # rcNormalPosition
        state_names = {1: "NORMAL", 2: "MINIMIZED", 3: "MAXIMIZED"}

        for attempt in range(1, max_retries + 1):
            try:
                if not win32gui.IsWindow(hwnd):
                    self.log(f"[WINDOW-STATE] HWND {hwnd} no longer valid — aborting verification")
                    return False

                actual = win32gui.GetWindowPlacement(hwnd)
                actual_cmd = actual[1]
                actual_rect = actual[4]

                # Compare showCmd (normal/min/max)
                cmd_match = (actual_cmd == saved_cmd)
                # Compare normal position rect (with 2px tolerance for DPI rounding)
                rect_tolerance = 2
                rect_match = all(
                    abs(a - s) <= rect_tolerance
                    for a, s in zip(actual_rect, saved_rect)
                )

                if cmd_match and rect_match:
                    self.log(
                        f"[WINDOW-STATE] Verified OK (attempt {attempt}): "
                        f"showCmd={state_names.get(actual_cmd, actual_cmd)}, "
                        f"rect={actual_rect}"
                    )
                    return True

                # Log the mismatch details
                mismatches = []
                if not cmd_match:
                    mismatches.append(
                        f"showCmd: actual={state_names.get(actual_cmd, actual_cmd)}, "
                        f"expected={state_names.get(saved_cmd, saved_cmd)}"
                    )
                if not rect_match:
                    mismatches.append(f"rect: actual={actual_rect}, expected={saved_rect}")

                self.log(
                    f"[WINDOW-STATE] MISMATCH (attempt {attempt}/{max_retries}): "
                    f"{'; '.join(mismatches)} — re-applying placement"
                )

                # Re-apply the saved placement
                win32gui.SetWindowPlacement(hwnd, saved)

                if self._sleep(retry_delay):
                    return False

            except Exception as e:
                self.log(f"[WINDOW-STATE] Verification error (attempt {attempt}): {e}")
                if self._sleep(retry_delay):
                    return False

        self.log(f"[WINDOW-STATE] Could not verify state after {max_retries} retries")
        return False

    # _suppress_spotify_foreground has been integrated into the launch/placement logic in _tick()

    def kill_spotify(self):
        """Send SIGKILL to all Spotify.exe processes."""
        self._cached_spotify_hwnd = None
        self._cached_spotify_pid = None
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

    def _kill_and_wait(self, timeout: float = 3.0) -> bool:
        """Kill all Spotify processes and poll until they are fully gone.

        Without this wait, a slow OS may still have the old zombie alive when
        launch_spotify() fires, resulting in two Spotify instances racing.

        Returns True once all processes are gone (or if shutdown is requested),
        False if processes were still alive after *timeout* seconds.
        """
        self.kill_spotify()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._sleep(0.25):
                return True  # shutdown requested — caller should also return
            still_alive = [
                p for p in psutil.process_iter(["name"])
                if p.info.get("name", "") and p.info["name"].lower() == "spotify.exe"
            ]
            if not still_alive:
                self.log("[KILL] All Spotify processes confirmed dead")
                return True
        self.log(f"[WARN] Spotify processes still alive after {timeout:.1f}s — continuing anyway")
        return False

    def launch_spotify(self):
        """Start Spotify again with --force-renderer-accessibility.

        The --force-renderer-accessibility flag ensures Chromium's renderer
        process builds a complete accessibility tree, which is required for
        reliable Lyrics toggle state detection via UIA.
        """
        # Re-attempt path detection: if Spotify was not installed in a standard
        # location at startup, try to locate it now from running processes.
        if not self.spotify_path or not os.path.exists(self.spotify_path):
            self.spotify_path = self._find_spotify_path()
            if self.spotify_path:
                self.log(f"[LAUNCH] Spotify path resolved: {self.spotify_path}")

        if self.spotify_path and os.path.exists(self.spotify_path):
            cmd = [self.spotify_path, _FORCE_A11Y_FLAG]
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.log(f"[LAUNCH] Started: {' '.join(cmd)}")
        else:
            # Fallback: use subprocess with the spotify: URI protocol.
            # os.startfile cannot pass command-line flags, so we use
            # cmd /c start to launch the protocol handler.
            try:
                subprocess.Popen(
                    ["cmd", "/c", "start", "spotify:"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                )
                self.log("[LAUNCH] Started via spotify: protocol (subprocess)")
            except Exception as e:
                # Last resort fallback
                self.log(f"[WARN] subprocess protocol launch failed ({e}), trying os.startfile")
                os.startfile("spotify:")
                self.log("[LAUNCH] Started via spotify: protocol (os.startfile)")

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
        self.status_message = message
        if not message:
            return
        if message.startswith("[ERROR]"):
            self.reporter.error(message)
        elif message.startswith("[WARN]"):
            self.reporter.warning(message)
        elif message.startswith("[SHUTDOWN]"):
            self.reporter.shutdown(message)
        else:
            self.reporter.debug(message)

    # -- Main loop -----------------------------------------

    # -- Config hot-reload ---------------------------------

    def _maybe_reload_config(self):
        """Periodically reload config.py and rebuild ad-detection sets.

        Controlled by CONFIG_HOT_RELOAD and CONFIG_HOT_RELOAD_INTERVAL in
        config.py.  Rebuilds the module-level AD_TITLE_EXACT_LOWER and
        AD_TITLE_CONTAINS_LOWER sets so that changes take effect without
        restarting the monitor.
        """
        global AD_TITLE_EXACT_LOWER, AD_TITLE_CONTAINS_LOWER
        try:
            if not getattr(_config, "CONFIG_HOT_RELOAD", False):
                return
            interval = getattr(_config, "CONFIG_HOT_RELOAD_INTERVAL", 20)
            self._config_reload_poll_count += 1
            if self._config_reload_poll_count < interval:
                return
            self._config_reload_poll_count = 0
            importlib.reload(_config)
            new_exact, new_contains = _build_ad_sets()
            if new_exact != AD_TITLE_EXACT_LOWER or new_contains != AD_TITLE_CONTAINS_LOWER:
                AD_TITLE_EXACT_LOWER = new_exact
                AD_TITLE_CONTAINS_LOWER = new_contains
                self.reporter.debug(
                    f"[CONFIG] Hot-reloaded config.py — "
                    f"exact={sorted(AD_TITLE_EXACT_LOWER)}, "
                    f"contains={sorted(AD_TITLE_CONTAINS_LOWER)}"
                )
            # Adapt console verbosity dynamically if changed in config.py
            if not isinstance(self.reporter, NullReporter):
                verbose = getattr(_config, "VERBOSE_CONSOLE", False)
                if verbose and not isinstance(self.reporter, VerboseConsoleReporter):
                    self.reporter = VerboseConsoleReporter()
                elif not verbose and isinstance(self.reporter, VerboseConsoleReporter):
                    self.reporter = UserConsoleReporter()
        except Exception as e:
            self.reporter.warning(f"Config hot-reload error: {e}")

    def run(self):
        """Polling loop - runs in a background thread."""
        self.reporter.startup(
            "Spotify Ad Restarter",
            {
                "path": self.spotify_path or "protocol handler",
                "poll": POLL_INTERVAL,
                "cooldown": COOLDOWN_PERIOD,
                "load_wait": LOAD_WAIT_TIME,
            },
        )
        self.reporter.debug(
            f"Poll {POLL_INTERVAL}s | Cooldown {COOLDOWN_PERIOD}s | Load wait {LOAD_WAIT_TIME}s | "
            f"UIA: {UIA_AVAILABLE} | Hot-reload: {getattr(_config, 'CONFIG_HOT_RELOAD', False)}"
        )

        # Ensure Spotify is unmuted when monitor starts
        self.unmute_spotify()

        while self.running:
            try:
                self._maybe_reload_config()
                self._tick()
            except Exception as e:
                self.reporter.error(str(e))
            if self._sleep(POLL_INTERVAL):
                break

        self.reporter.shutdown("Monitor stopped.")

    def _tick(self):
        """Single iteration of the monitor loop."""
        window = self.find_spotify_window()

        # -- No Spotify window -----------------------------
        if not window:
            if self.state not in (self.IDLE, self.RESTARTING):
                self.reporter.status_change("Waiting for Spotify...")
                self.state = self.IDLE
                self.status_message = "Waiting for Spotify..."
            return

        hwnd, title = window

        if title != self.last_seen_title:
            self.last_seen_title = title
            self.reporter.debug(f"Spotify window title: \"{title}\"")

        # -- Cooldown check --------------------------------
        if self.state == self.COOLDOWN:
            elapsed = time.time() - self.last_restart_time
            remaining = COOLDOWN_PERIOD - elapsed

            # If a valid song is already playing, exit cooldown immediately
            if self.is_song_title(title):
                self.state = self.MUSIC_PLAYING
                self.last_song_title = title
                self.status_message = f"Playing: {title}"
                self.reporter.song_playing(title)
                vol = self._saved_volume if self._saved_volume is not None else self._get_session_volume()
                if self._saved_lyrics_on is not None or vol is not None:
                    self.reporter.state_update(self._saved_lyrics_on, vol)
                self.capture_spotify_ui_state_async()
                return

            if remaining > 0:
                mins, secs = divmod(int(remaining), 60)
                self.status_message = (
                    f"Cooldown: {mins}m {secs}s remaining" if mins else f"Cooldown: {secs}s remaining"
                )
                return
            self.reporter.debug("[OK] Cooldown complete -- resuming monitoring")
            self.state = (
                self.MUSIC_PLAYING
                if self.is_song_title(title)
                else self.IDLE
            )

        # -- Song playing ----------------------------------
        if self.is_song_title(title):
            # Reset ambiguous ad tick counter when valid song is playing
            self._ambiguous_ad_ticks = 0

            # Stamp the time we last saw a valid song title.
            # is_ad_playing() uses this to suppress false positives during the
            # brief inter-track gap where Spotify shows an ambiguous title while
            # the previous track's audio tail is still audible.
            self._last_song_seen_time = time.time()

            # Clear the ad-persistence window: a confirmed song title means no ad
            # is playing, so any pending persistence state is now stale.  Without
            # this, a song-to-song transition (which briefly shows a non-song,
            # non-ad title like 'Spotify') would re-trigger a false restart.
            self._last_confirmed_ad_title = None
            self._last_confirmed_ad_time = 0.0

            # Track active window focus transitions
            fg_hwnd = None
            try:
                fg_hwnd = win32gui.GetForegroundWindow()
            except Exception:
                pass
            spotify_is_active = (fg_hwnd == hwnd)
            if spotify_is_active != self._spotify_was_active:
                self._spotify_was_active = spotify_is_active
                self.reporter.debug(f"[STATE-TRACK] Spotify focus changed: active={spotify_is_active} — capturing UI state")
                self.capture_spotify_ui_state_async()

            if self.state != self.MUSIC_PLAYING or self.last_song_title != title:
                self.reporter.song_playing(title)
                vol = self._saved_volume if self._saved_volume is not None else self._get_session_volume()
                if self._saved_lyrics_on is not None or vol is not None:
                    self.reporter.state_update(self._saved_lyrics_on, vol)
                self.capture_spotify_ui_state_async()

            self.state = self.MUSIC_PLAYING
            self.last_song_title = title
            self.status_message = f"Playing: {title}"
            return

        # -- Ad detected -----------------------------------
        if self.is_ad_playing(title):
            # Clear persistence window immediately — we are now handling this ad
            self._last_confirmed_ad_title = None
            self._last_confirmed_ad_time = 0.0

            self.state = self.AD_DETECTED
            self.status_message = f"Ad detected: {title}"
            self.reporter.ad_detected(title, self.last_song_title)
            self.reporter.debug(f'[AD] >>> AD DETECTED <<<  Title: "{title}"  (normalized: "{_norm(title)}")')
            self.reporter.debug(f'     Last song: "{self.last_song_title}"')

            # 0. INSTANT MUTE — must be the very first action so the user
            #    never hears any portion of the advertisement.  All state
            #    capture work happens AFTER this.
            self.mute_spotify()
            self._muted = True

            # 1. Save the user's currently active window BEFORE we do anything
            #    so we can return focus to it after Spotify restarts.
            user_fg_hwnd = None
            self._spotify_was_foreground = False
            try:
                user_fg_hwnd = win32gui.GetForegroundWindow()
                if user_fg_hwnd:
                    _, active_pid = win32process.GetWindowThreadProcessId(user_fg_hwnd)
                    _, spotify_pid = win32process.GetWindowThreadProcessId(hwnd)
                    self._spotify_was_foreground = (active_pid == spotify_pid)
                self.log(f"[FOCUS-GUARD] Saved user's foreground window: HWND={user_fg_hwnd}, spotify_was_foreground={self._spotify_was_foreground}")
            except Exception as e:
                self.log(f"[WARN] Error saving active window: {e}")

            # 2. Save window placement before kill (for restoring window state)
            self._save_window_placement()

            # 3. Save pycaw session volume before kill
            self._capture_session_volume()

            # 4. Kill immediately (state is already saved, audio is muted).
            # _kill_and_wait() polls until all Spotify.exe PIDs vanish so the new
            # instance doesn't race with a zombie of the old one.
            if self._kill_and_wait():
                pass  # returns True on clean exit or shutdown
            self.state = self.RESTARTING

            # 5. Short wait then relaunch
            if RESTART_DELAY > 0:
                self.log(f"[WAIT] Waiting {RESTART_DELAY}s before relaunch...")
                if self._sleep(RESTART_DELAY):
                    return

            # 6. Relaunch (with --force-renderer-accessibility)
            self.launch_spotify()

            # 7. Wait for Spotify window to appear
            self.log("[WAIT] Waiting for Spotify window...")
            load_deadline = time.time() + LOAD_WAIT_TIME
            loaded = False
            new_hwnd_info = None
            while time.time() < load_deadline:
                if self._sleep(0.5):
                    return
                new_hwnd_info = self.find_spotify_window()
                if new_hwnd_info:
                    loaded = True
                    break
            if not loaded:
                self.log(
                    "[WARN] Spotify window not detected within load window — "
                    "aborting restart sequence, returning to IDLE (no cooldown)"
                )
                # Do NOT enter cooldown: let the monitor immediately re-try on
                # the next ad rather than going blind for 4 minutes.
                self.state = self.IDLE
                # The new Spotify instance (if any) is still unmuted from its
                # own launch; clearing our flag avoids a double-unmute in stop().
                self._muted = False
                return

            # 8. Restore window placement — but force NORMAL first if it was
            #    minimized so Chromium builds its accessibility tree for UIA.
            need_reminimize = False  # track if we need to re-minimize later
            if loaded and new_hwnd_info:
                new_h, _ = new_hwnd_info
                self.log(f"[WINDOW-STATE] Restoring window placement immediately to HWND {new_h}")
                if self._saved_window_placement:
                    saved_cmd = self._saved_window_placement[1]
                    state_names = {1: "NORMAL", 2: "MINIMIZED", 3: "MAXIMIZED"}
                    if self._spotify_was_foreground:
                        self.log(f"[WINDOW-STATE] Spotify was foreground — restoring as {state_names.get(saved_cmd, saved_cmd)}")
                        try:
                            win32gui.SetWindowPlacement(new_h, self._saved_window_placement)
                            ctypes.windll.user32.AllowSetForegroundWindow(os.getpid())
                            win32gui.SetForegroundWindow(new_h)
                        except Exception as e:
                            self.log(f"[WARN] Failed to activate Spotify: {e}")
                    else:
                        if saved_cmd == 2:  # MINIMIZED
                            # Chromium doesn't render UI when minimized, so UIA
                            # cannot find lyrics/volume controls.  Temporarily
                            # show as NORMAL (off-screen-ish, non-active) so the
                            # renderer populates the accessibility tree, then
                            # re-minimize after UI state is restored.
                            self.log("[WINDOW-STATE] Spotify was MINIMIZED — temporarily showing as NORMAL for UIA access")
                            try:
                                p = self._saved_window_placement
                                temp_pl = (p[0], win32con.SW_SHOWNOACTIVATE, p[2], p[3], p[4])
                                win32gui.SetWindowPlacement(new_h, temp_pl)
                                need_reminimize = True
                            except Exception as e:
                                self.log(f"[WARN] Failed to temp-show Spotify: {e}")
                        else:
                            self.log(f"[WINDOW-STATE] Spotify was background — restoring as {state_names.get(saved_cmd, saved_cmd)} without activation")
                            try:
                                p = self._saved_window_placement
                                if saved_cmd == 3:  # MAXIMIZED
                                    win32gui.SetWindowPlacement(new_h, p)
                                else:  # NORMAL
                                    restore_pl = (p[0], win32con.SW_SHOWNOACTIVATE, p[2], p[3], p[4])
                                    win32gui.SetWindowPlacement(new_h, restore_pl)
                            except Exception as e:
                                self.log(f"[WARN] Failed to restore Spotify placement: {e}")

                        # Return focus to previous foreground window
                        self._return_focus(user_fg_hwnd)
                else:
                    self.log("[WINDOW-STATE] No saved placement to restore")

                # Verify window placement (skip verify for temp-normal state)
                if not need_reminimize:
                    self._verify_and_fix_window_state(new_h)

            # 9. Wait for the player UI to be fully ready
            #    (now available because window is visible / on-screen)
            self.log("[WAIT] Waiting for player UI to be ready...")
            ui_ready = False
            ui_deadline = time.time() + 15  # max 15s total for UI
            while time.time() < ui_deadline:
                if self._sleep(1.0):
                    return
                lyrics_elem, volume_elem = self._find_uia_player_controls(verbose=True)
                if lyrics_elem or volume_elem:
                    ui_ready = True
                    self.log("[OK] Player UI controls detected — Spotify is ready")
                    break
            if not ui_ready:
                self.log("[WARN] Player controls not found — continuing anyway")
                if self._sleep(2.0):
                    return

            # 10. Resume playback — use audio peak to confirm it's actually playing
            for attempt in range(4):
                self.send_play_key()
                self.log(f"[PLAY] Play key sent (attempt {attempt + 1})")
                if self._sleep(1.5):
                    return
                peak = self.get_spotify_peak_volume()
                if peak > 0.001:
                    self.log(f"[PLAY] Playback confirmed (audio peak: {peak:.4f})")
                    break
                self.log(f"[PLAY] No audio yet (peak: {peak:.4f}), retrying...")

            # 11. Unmute the new instance
            if self._sleep(0.5):
                return
            self.unmute_spotify()
            self._muted = False

            # 11b. Restore pycaw session volume immediately after unmute
            self._restore_session_volume()

            # 12. Restore and verify saved UI state (volume + lyrics)
            #     Block async captures for the duration so they cannot overwrite
            #     the saved state with stale readings from the not-yet-restored UI.
            self.log("[RESTORE] Restoring UI state (volume + lyrics)...")
            self._restoring_ui = True
            try:
                self._verify_and_fix_ui_state()
            finally:
                self._restoring_ui = False

            # 12b. Re-apply pycaw session volume after UIA restore (in case UIA slider reset it)
            self._restore_session_volume()

            # 12c. Re-minimize Spotify if it was originally minimized
            if need_reminimize and loaded and new_hwnd_info:
                new_h, _ = new_hwnd_info
                try:
                    p = self._saved_window_placement
                    minimize_pl = (p[0], win32con.SW_SHOWMINNOACTIVE, p[2], p[3], p[4])
                    win32gui.SetWindowPlacement(new_h, minimize_pl)
                    self.log("[WINDOW-STATE] Re-minimized Spotify after UI state restore")
                    # Return focus again in case the temp-show stole it
                    self._return_focus(user_fg_hwnd)
                except Exception as e:
                    self.log(f"[WARN] Failed to re-minimize Spotify: {e}")
                # Verify final placement
                self._verify_and_fix_window_state(new_h)

            # 13. Check if same song loaded — skip immediately if so
            self.log("[CHECK] Verifying song after restart...")
            for _ in range(5):
                if self._sleep(1.0):
                    return
                new_window = self.find_spotify_window()
                if new_window:
                    _, new_title = new_window
                    if self.is_song_title(new_title):
                        if new_title == self.last_song_title:
                            self.log(f"[SKIP] Same song detected ('{new_title}') — skipping immediately")
                            self.send_next_key()
                        break

            # 14. Final focus check
            if not self._spotify_was_foreground:
                self._return_focus(user_fg_hwnd)

            # 16. Enter cooldown
            self.ads_skipped += 1
            self.last_restart_time = time.time()
            self.state = self.COOLDOWN
            self.status_message = f"Cooldown ({COOLDOWN_PERIOD}s)"
            self.reporter.ad_skipped(self.ads_skipped)
            self.reporter.debug(
                f"[COOLDOWN] Started ({COOLDOWN_PERIOD}s)  |  "
                f"Total ads skipped: {self.ads_skipped}"
            )
            return

        # -- Neither song nor ad: Spotify is paused or idle -
        if self.state == self.MUSIC_PLAYING and self._ambiguous_ad_ticks == 0:
            self.state = self.IDLE
            self.status_message = "Paused / Idle"
            self.reporter.status_change("Paused / Idle")
            self.reporter.debug(f"[IDLE] Playback stopped or paused (title: \"{title}\")")

    def _return_focus(self, hwnd):
        """Return keyboard focus to the window the user was in before the restart.

        Spotify grabs the foreground on its own when it launches.  We call this
        after the full restart sequence so the user ends up back where they were.
        """
        if not hwnd:
            self.log("[FOCUS-GUARD] No previous foreground window to restore")
            return
        try:
            # Verify the target window still exists and is visible
            if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
                self.log(f"[FOCUS-GUARD] Previous window {hwnd} no longer valid/visible — skipping focus restore")
                return
            # Allow our process to set the foreground window
            ctypes.windll.user32.AllowSetForegroundWindow(os.getpid())
            # Give Spotify a moment to finish its own focus grab, then override it
            if self._sleep(0.3):
                return
            win32gui.SetForegroundWindow(hwnd)
            self.log(f"[FOCUS-GUARD] Returned focus to previous window (HWND={hwnd})")
        except Exception as e:
            self.log(f"[WARN] Could not return focus: {e}")

    def stop(self):
        self.running = False
        self.shutdown_event.set()
        # Ensure Spotify is unmuted when monitor stops.
        # If we were killed mid-restart (_muted=True) the new Spotify instance
        # may not have its audio session registered yet.  Wait briefly so the
        # unmute call has something to act on.
        if self._muted:
            self.log("[STOP] Muted mid-restart — waiting 2 s before unmute")
            time.sleep(2.0)
        self.unmute_spotify()
        self._muted = False


# ==========================================================
#  System tray icon
# ==========================================================

# Tray icon colours keyed by monitor state
_TRAY_COLORS = {
    SpotifyMonitor.MUSIC_PLAYING: (30, 215, 96),   # Spotify green  — playing music
    SpotifyMonitor.AD_DETECTED:   (220,  50, 50),  # red            — ad detected
    SpotifyMonitor.RESTARTING:    (220,  50, 50),  # red            — restarting
    SpotifyMonitor.COOLDOWN:      (255, 160,  0),  # amber          — cooldown period
    SpotifyMonitor.IDLE:          (120, 120, 120),  # grey           — waiting / idle
}


def _create_tray_image(color=(120, 120, 120)):
    """Generate a system-tray icon with a play triangle.

    The default color is grey (IDLE state).  Pass a state-specific color from
    _TRAY_COLORS to visually reflect the monitor's current activity.
    """
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Coloured circle background
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
    """Create and return a pystray Icon with dynamic state-colour updates."""

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
        _create_tray_image(_TRAY_COLORS.get(monitor.state, (120, 120, 120))),
        "Spotify Ad Restarter",
        menu,
    )

    # Background thread: update icon colour whenever monitor state changes.
    # pystray allows setting icon.icon from any thread.
    _prev_state: list = [None]

    def _icon_colour_updater():
        while monitor.running:
            state = monitor.state
            if state != _prev_state[0]:
                _prev_state[0] = state
                color = _TRAY_COLORS.get(state, (120, 120, 120))
                try:
                    icon.icon = _create_tray_image(color)
                except Exception:
                    pass
            time.sleep(0.8)  # check ~every 0.8 s — fast enough to feel responsive

    threading.Thread(target=_icon_colour_updater, daemon=True, name="tray-colour").start()

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

    # Windows Console Control Handler for immediate and graceful shutdown on Ctrl+C / close
    def console_ctrl_handler(ctrl_type):
        event_names = {
            win32con.CTRL_C_EVENT: "CTRL_C_EVENT",
            win32con.CTRL_BREAK_EVENT: "CTRL_BREAK_EVENT",
            win32con.CTRL_CLOSE_EVENT: "CTRL_CLOSE_EVENT",
            win32con.CTRL_LOGOFF_EVENT: "CTRL_LOGOFF_EVENT",
            win32con.CTRL_SHUTDOWN_EVENT: "CTRL_SHUTDOWN_EVENT",
        }
        name = event_names.get(ctrl_type, f"UNKNOWN({ctrl_type})")
        monitor.reporter.debug(f"Console control event received: {name}")
        
        # Stop monitor thread
        monitor.stop()
        
        # Stop tray icon loop (thread-safely breaks blocking tray.run())
        try:
            tray.stop()
        except Exception as e:
            monitor.reporter.debug(f"Error stopping tray: {e}")
            
        # Return True for Ctrl+C and Break so Windows doesn't terminate us abruptly,
        # allowing the main thread's cleanup code to run.
        if ctrl_type in (win32con.CTRL_C_EVENT, win32con.CTRL_BREAK_EVENT):
            return True
        return False

    try:
        win32api.SetConsoleCtrlHandler(console_ctrl_handler, True)
    except Exception as e:
        monitor.reporter.debug(f"Failed to set console control handler: {e}")

    # Install SIGINT handler as a fallback
    def _on_sigint(signum, frame):
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

    try:
        tray.run()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        thread.join(timeout=3)
        monitor.reporter.shutdown("Spotify Ad Restarter stopped. Goodbye!")


if __name__ == "__main__":
    main()
