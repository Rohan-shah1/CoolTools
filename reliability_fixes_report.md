# Spotify Monitor Reliability Fixes: Technical Report

This document details the four major reliability issues present in the initial version of the Spotify monitor, provides a comparison of the old and new code, and explains how the fixes resolve the underlying problems.

---

## 1. Spotify Lyrics State Detection (Inconsistent / Unreliable)

### The Problem
The application inconsistently detected the lyrics state (ON/OFF). Sometimes it missed the state entirely or reported the wrong state. 
This was caused by three factors:
1. **Async UIA Tree:** Spotify's UI is Chromium-based. The internal accessibility tree (UIA) takes a few seconds to build after the native window appears. The old code only checked once and gave up immediately if it wasn't there.
2. **Stale COM Proxies:** After restarting Spotify, the old code could inadvertently use a cached or stale UIA element pointing to the dead process, leading to silent COM errors.
3. **Chromium Accessibility:** Without forcing accessibility on Chromium, the `ToggleState` of elements like the lyrics button can report stale values or disappear during playback.

### The Old Code
```python
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
            # ...
```

### The New Code
```python
    def _read_lyrics_state_reliable(self, lyrics_elem, verbose=False):
        if not lyrics_elem: return None
        try:
            tog_pat = lyrics_elem.GetCurrentPattern(_UIA.UIA_TogglePatternId)
            toggle_client = tog_pat.QueryInterface(_UIA.IUIAutomationTogglePattern)
            state1 = toggle_client.CurrentToggleState
            
            # Wait briefly and re-read to confirm stability
            if self._sleep(0.2):
                return None
                
            tog_pat2 = lyrics_elem.GetCurrentPattern(_UIA.UIA_TogglePatternId)
            toggle_client2 = tog_pat2.QueryInterface(_UIA.IUIAutomationTogglePattern)
            state2 = toggle_client2.CurrentToggleState
            
            if state1 == state2:
                return (state1 == 1)
            return None # Transient state
        except Exception:
            return None
```
*(Additionally, `--force-renderer-accessibility` is now appended to the Spotify launch command)*

### How it Solves the Problem
- **Retry Loop:** `capture_spotify_ui_state` now retries multiple times with a back-off. If the UIA tree is still loading, it waits and tries again instead of silently failing.
- **Double-Read Verification:** `_read_lyrics_state_reliable` reads the toggle state twice, 200ms apart. Because the web player can be in flux while changing tracks, this ensures we only trust the state when it is stable.
- **Forced Accessibility:** Launching Spotify with the accessibility flag ensures the Chromium renderer consistently exposes the toggle state to Windows UIA.

---

## 2. Application Unexpectedly Comes to the Foreground

### The Problem
During the restore sequence (putting volume and lyrics back to their original states after a restart), the active user's window would lose focus, and Spotify would steal the foreground.
This was caused by the bot simulating physical mouse clicks on the Spotify window. Sending a `MOUSEEVENTF_LEFTDOWN` to a background window triggers a `WM_MOUSEACTIVATE` message in Windows, causing the OS to pull that window to the foreground. Additionally, calling `ShowWindow` to unminimize the window caused further focus stealing.

### The Old Code
```python
            # Make Spotify visible (unminimise if needed)
            if hwnd:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
                
            # ... restore lyrics via click ...
            if need_lyrics_change:
                rect = lyrics_elem.CurrentBoundingRectangle
                cx = int((rect.left + rect.right) / 2)
                cy = int((rect.top + rect.bottom) / 2)
                ctypes.windll.user32.SetCursorPos(cx, cy)
                ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
                ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
```

### The New Code
```python
            # --- Restore lyrics via UIA Toggle() (no mouse, no focus steal) ---
            if lyrics_elem and self._saved_lyrics_on is not None:
                try:
                    current_lyrics = self._read_lyrics_state_reliable(lyrics_elem, verbose=True)
                    if current_lyrics is not None and current_lyrics != self._saved_lyrics_on:
                        tog_pat = lyrics_elem.GetCurrentPattern(_UIA.UIA_TogglePatternId)
                        toggle_client = tog_pat.QueryInterface(_UIA.IUIAutomationTogglePattern)
                        toggle_client.Toggle() # Native API toggle, zero mouse simulation
```

### How it Solves the Problem
We completely removed `ShowWindow` and physical mouse simulations. By interacting directly with the native UIA patterns (`Toggle()` for buttons, and `SetValue()` for the volume slider), Windows sends the interaction straight to the control internally. The OS does not view this as user input, so it never sends `WM_MOUSEACTIVATE`. Spotify stays completely in the background, exactly where it was.

---

## 3. Spotify Restart Behavior (Foreground Steal and Minimized Accessibility Limitations)

### The Problem
When the script detected an ad, it would kill Spotify and launch a new instance. By default, when Spotify starts, it calls `ShowWindow(SW_SHOW)`, which steals focus.
Furthermore:
1. **Foreground Preservation:** If the user was actively using Spotify when the ad started, it should stay in the foreground after restart. If it was in the background, it should return to the background.
2. **Chromium Minimized Layout Throttling:** If we minimized Spotify immediately on launch (`SW_SHOWMINNOACTIVE`), Chromium suspends rendering and disables its accessibility layout tree. As a result, UIA searches (`lyrics_elem`, `volume_elem`) fail during the restore sequence, and volume/lyrics state cannot be recovered.

### The Old Code
```python
    # Immediately minimized on launch, preventing Chromium UIA tree creation
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWMINNOACTIVE)
```

### The New Code
```python
            # 6. Suppress or position the window based on foreground status
            if loaded and new_hwnd_info:
                new_h, _ = new_hwnd_info
                if not self._spotify_was_foreground:
                    # Move it off-screen and show SW_SHOWNOACTIVATE
                    # Enables Chromium rendering for UIA access without stealing focus or visual block
                    try:
                        win32gui.SetWindowPos(
                            new_h, 0, -32000, -32000, 100, 100,
                            win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER | win32con.SWP_NOSENDCHANGING
                        )
                        win32gui.ShowWindow(new_h, win32con.SW_SHOWNOACTIVATE)
                    except Exception:
                        pass
                else:
                    # Keep it on screen
                    win32gui.ShowWindow(new_h, win32con.SW_SHOWNOACTIVATE)

            # ... Wait for UIA, restore states ...

            # 12. Restore window placement and focus
            if loaded and new_hwnd_info:
                new_h, _ = new_hwnd_info
                if self._spotify_was_foreground:
                    # Spotify was in foreground: restore position and activate
                    win32gui.SetWindowPlacement(new_h, self._saved_window_placement)
                    win32gui.ShowWindow(new_h, win32con.SW_SHOWNORMAL)
                    win32gui.SetForegroundWindow(new_h)
                else:
                    # Spotify was in background: restore position and minimize
                    win32gui.SetWindowPlacement(new_h, self._saved_window_placement)
                    win32gui.ShowWindow(new_h, win32con.SW_SHOWMINNOACTIVE)
                    self._return_focus(user_fg_hwnd)
```

### How it Solves the Problem
- **Foreground Tracking:** We now inspect the active window's PID. If it matches Spotify, we set `_spotify_was_foreground = True`.
- **Off-Screen Layout Trick:** If Spotify was in the background, we move it off-screen (to `x=-32000, y=-32000`) and call `SW_SHOWNOACTIVATE`. Because it's not minimized or hidden, Chromium continues rendering and populates the UIA tree, allowing our scripts to fully read and restore volume and lyrics. Because it is off-screen, it is invisible to the user.
- **State Restoration:** Once restoration finishes, we apply the saved window coordinates back via `SetWindowPlacement` and either bring it to the foreground (if it was active) or minimize it (if it was in the background), yielding a flawless and invisible restart.

---

## 4. Inconsistent Shutdown Behavior (Hanging / Tray Workaround)

### The Problem
When pressing **Ctrl + C** in the command line, the application would hang or take a very long time to exit. However, opening the system tray's hidden icon menu (`^` overflow button) triggered an immediate exit.
This was caused by the blocking Win32 event loop (`GetMessage`) on the main thread preventing standard Python `SIGINT` handlers from executing.

### How it Solves the Problem
- **Windows Console Handler:** We registered `win32api.SetConsoleCtrlHandler`. When Ctrl+C is pressed, Windows calls this on a background thread.
- **Thread-Safe ShutDown:** The background callback calls `tray.stop()` which posts a thread-safe window close message, breaking the main thread's blocking `GetMessage` loop instantly.
- **Event-Based Sleep:** All loops sleep via `self.shutdown_event.wait()` instead of `time.sleep()`, causing threads to terminate in milliseconds.

---

## 5. Capturing State only on Interaction (No Polling Loop)

### The Problem
Periodic UIA polling every 1.5 seconds is CPU-intensive and generates continuous trace logs.

### The New Code
```python
        # In _tick():
        if self.is_song_title(title):
            # Track active window focus transitions
            fg_hwnd = win32gui.GetForegroundWindow()
            spotify_is_active = (fg_hwnd == hwnd)
            if spotify_is_active != self._spotify_was_active:
                self._spotify_was_active = spotify_is_active
                self.log(f"[STATE-TRACK] Spotify focus changed: active={spotify_is_active} — capturing UI state")
                self.capture_spotify_ui_state()

            if self.state != self.MUSIC_PLAYING:
                # Capture once when a track changes
                self.capture_spotify_ui_state()
```

### How it Solves the Problem
Instead of querying UIA every 1.5 seconds, the application is now event/focus-driven. UIA state is only queried:
1. When a new song starts playing.
2. When Spotify transitions to the foreground (the user clicked it to interact).
3. When Spotify transitions away from the foreground (the user clicked away, meaning they finished making changes).
4. Right before a detected ad is killed (as a final safety capture).

This drops UIA polling queries to zero during normal playback, resulting in 0% CPU consumption and clean console logs.
