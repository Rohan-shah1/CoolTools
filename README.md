# 🎵 Spotify Ad Restarter

A lightweight Windows tool that detects Spotify audio ads, instantly mutes them, restarts Spotify, and resumes your music — automatically restoring your **volume level** and **lyrics view** exactly as you left them.

## ✨ Features

- 🔇 **Instant mute** the moment an ad is detected — no audio bleeds through
- 💀 **Kills & restarts** Spotify to skip the ad entirely
- 🔊 **Restores volume** to the exact level it was before the restart
- 🎤 **Restores lyrics view** — if you had lyrics open, they'll be open again
- ⚡ **Smart load detection** — resumes as soon as Spotify is ready, not after a fixed delay
- ⏭️ **Instant same-song skip** — if Spotify reloads the same song that was playing, it skips to the next one immediately
- 🛡️ **Cooldown period** (4 min) to prevent restart loops
- 🖥️ **System tray icon** — runs silently in the background

## How It Works

1. **Monitors** Spotify's window title every 1.5 seconds
2. **Detects** when the title changes from `Song - Artist` to an ad indicator (`Spotify Free`, `Advertisement`, etc.)
3. **Captures** current UI state — volume level and lyrics on/off — via Windows UI Automation
4. **Instantly mutes** Spotify's audio session so you hear nothing
5. **Kills & restarts** the Spotify process immediately
6. **Polls** for the new Spotify window every 0.5s (no fixed wait — proceeds as soon as it's ready)
7. **Presses Play** to resume your music
8. **Restores** volume and lyrics to exactly what they were before
9. **Skips** the track immediately if Spotify reloads the same song
10. **Enters cooldown** (4 minutes) to avoid restart loops

## Setup

### Prerequisites
- **Windows 10/11**
- **Python 3.8+**
- **Spotify Desktop App** (Microsoft Store version is supported)

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Run

```bash
python monitor.py
```

A green play icon will appear in your **system tray** (bottom-right of taskbar). Right-click it to check status or quit.

## Configuration

Edit [`config.py`](config.py) to adjust behaviour:

| Setting | Default | Description |
|---|---|---|
| `POLL_INTERVAL` | `1.5s` | How often to check the window title |
| `RESTART_DELAY` | `0s` | Extra wait after killing before relaunching (0 = immediate) |
| `LOAD_WAIT_TIME` | `5s` | Max time to wait for Spotify window to appear (polls every 0.5s) |
| `COOLDOWN_PERIOD` | `240s` | Cooldown after each restart to avoid loops |
| `SPOTIFY_EXE_PATH` | Auto-detect | Override path to `Spotify.exe` |

## Console Output

```
[19:30:01] ==================================================
[19:30:01] Spotify Ad Restarter -- Active
[19:30:01] Path: C:\Users\...\Spotify.exe
[19:30:01] Poll 1.5s  |  Cooldown 240s
[19:30:01] ==================================================
[19:30:05] [PLAY] Now playing: i hate u, i love u - gnash
[19:33:42] [AD] >>> AD DETECTED <<<  Title: "Spotify Free"
[19:33:42]      Last song: "i hate u, i love u - gnash"
[19:33:42] [STATE] Capturing UI state before restart...
[19:33:42] [STATE] Lyrics is ON
[19:33:42] [STATE] Volume captured: 0.80
[19:33:42] [MUTE] Spotify muted
[19:33:42] [KILL] Spotify processes terminated
[19:33:42] [LAUNCH] Started: C:\Users\...\Spotify.exe
[19:33:44] [WAIT] Waiting for Spotify to load...
[19:33:46] [PLAY] Play key sent
[19:33:47] [UNMUTE] Spotify unmuted
[19:33:47] [RESTORE] Volume unchanged at 0.80
[19:33:47] [RESTORE] Lyrics already ON, no change needed
[19:33:48] [CHECK] Verifying song after restart...
[19:33:49] [COOLDOWN] Started (240s)  |  Total ads skipped: 1
```

## Troubleshooting

| Problem | Fix |
|---|---|
| Spotify not detected | Make sure Spotify is running. Set `SPOTIFY_EXE_PATH` manually in `config.py` if needed |
| Music doesn't resume | Increase `LOAD_WAIT_TIME` in `config.py` |
| Volume/lyrics not restored | Ensure Spotify is fully visible (not minimised to taskbar) when the ad hits |
| False positives on pause | This shouldn't happen — the tool only triggers when actively transitioning from a playing song to an ad title |

## Dependencies

| Package | Purpose |
|---|---|
| `psutil` | Process detection and management |
| `pywin32` | Windows window enumeration |
| `pycaw` | Audio session muting/volume |
| `comtypes` | Windows UI Automation (state capture & restore) |
| `pystray` | System tray icon |
| `Pillow` | Tray icon rendering |

## ⚠️ Disclaimer

This tool is for educational purposes. Restarting Spotify to skip ads may violate Spotify's Terms of Service. Consider supporting artists by subscribing to [Spotify Premium](https://www.spotify.com/premium/).
