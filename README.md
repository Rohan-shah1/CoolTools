# 🎵 Spotify Ad Restarter

A lightweight Windows tool that detects Spotify audio ads, instantly mutes them, restarts Spotify, and resumes your music — automatically restoring your **volume level** and **lyrics view** exactly as you left them.

## ✨ Features

- 🔇 **Instant mute** the moment an ad is detected — no audio bleeds through
- 💀 **Kills & restarts** Spotify to skip the ad entirely
- 🔊 **Restores volume** to the exact level it was before the restart
- 🎤 **Restores lyrics view** — if you had lyrics open, they'll be open again
- ⚡ **Smart load detection** — resumes as soon as Spotify is ready, not after a fixed delay
- ⏭️ **Instant same-song skip** — if Spotify reloads the same song that was playing, it skips to the next one immediately
- 🛡️ **Anti-loop cooldown** (10s) — prevents restart loops, resuming instantly once music plays
- 🖥️ **System tray icon** — runs silently in the background

## How It Works

1. **Monitors** Spotify's window title (sub-second polling)
2. **Detects** when an ad plays (`Advertisement`, `Listen to music, ad-free.`, or ambiguous titles with active audio)
3. **Captures** current UI state — volume level and lyrics on/off — via Windows UI Automation
4. **Instantly mutes** Spotify's audio session so you hear nothing
5. **Kills & restarts** the Spotify process immediately
6. **Polls** for the new Spotify window every 0.5s (proceeds as soon as it's ready)
7. **Presses Play** to resume your music
8. **Restores** volume and lyrics to exactly what they were before
9. **Skips** the track immediately if Spotify reloads the same song
10. **Enters anti-loop protection** (resumes monitoring immediately once a song is detected)

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

## Configuration & Startup Setup

When you start the program (`python monitor.py`), you are prompted to configure your session preferences:

```text
========================================
 Spotify Ad Restarter - Session Setup  
========================================
Enter restart volume (0-100) [default: 80]: 80
Enable lyrics on restart? (y/n) [default: y]: y
Session configured: Volume = 80% | Lyrics = ON
========================================
```

Pressing **Enter** directly accepts the default values configured in [`config.py`](config.py). These settings apply for the entire session. Pressing **Ctrl+C** exits cleanly and unmutes Spotify. When you start the application again, you can configure new settings.

### Default Configuration ([`config.py`](config.py))

| Setting | Default | Description |
|---|---|---|
| `POLL_INTERVAL` | `0.25s` | How often to check the window title |
| `RESTART_DELAY` | `0s` | Extra wait after killing before relaunching (0 = immediate) |
| `LOAD_WAIT_TIME` | `12s` | Max time to wait for Spotify window to appear (polls every 0.5s) |
| `COOLDOWN_PERIOD` | `10s` | Anti-loop cooldown (monitoring resumes immediately when music plays) |
| `SONG_TRANSITION_GRACE_SECONDS` | `6.0s` | Transition grace after song ends before checking ambiguous titles |
| `AMBIGUOUS_AD_DURATION_SECONDS` | `10.0s` | Time ambiguous titles ("Spotify Free" + audio) must persist before treating as an ad |
| `RESTART_VOLUME` | `80` | Default fixed volume level (0-100) set after restart |
| `RESTART_LYRICS_ON` | `True` | Default lyrics state (ON/OFF) set after restart |
| `VERBOSE_CONSOLE` | `False` | When False, prints only user-relevant notifications. Set True for full debug traces |
| `SPOTIFY_EXE_PATH` | Auto-detect | Override path to `Spotify.exe` |

## Console Output

By default, the console displays only what the user needs to know (current music playing and ad restart count; no lyrics or volume spam):

```text
[19:30:05] [Playing] Starboy - The Weeknd, Daft Punk
[19:33:48] [Ad Detected] Spotify restarted | Total ads skipped: 1
[19:33:50] [Playing] Blinding Lights - The Weeknd
[19:35:10] [Shutdown] Spotify Ad Restarter stopped. Goodbye!
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
