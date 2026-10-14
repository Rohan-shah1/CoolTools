"""
Spotify Ad Restarter -- Configuration
=====================================
Adjust these values to fine-tune the tool's behavior.
"""

# --- Timing -------------------------------------------------------
POLL_INTERVAL = 0.25       # Seconds between window title checks (fast sub-second detection)
RESTART_DELAY = 0         # Seconds to wait after killing Spotify before relaunch (0 = immediate)
LOAD_WAIT_TIME = 12       # Max seconds to wait for Spotify window to appear (polls every 0.5s)
COOLDOWN_PERIOD = 10      # Seconds anti-loop cooldown after a restart (resumes immediately when music plays)
SONG_TRANSITION_GRACE_SECONDS = 12.0 # Seconds after a song ends to ignore transient "Spotify Free" titles during track buffering/crossfade
AMBIGUOUS_AD_DURATION_SECONDS = 10.0 # How long "Spotify Free" must persist WITH audio AFTER transition grace before it's treated as an ad.
                                     # Song transitions last 1-8s; real Spotify audio ads last 15-60s.

# --- Volume & UI State on Restart ---------------------------------
# Fixed volume level (0–100) to set after every ad restart.
# Spotify always comes back at this volume regardless of what it was before.
RESTART_VOLUME = 80

# Fixed lyrics state to set after every ad restart (True = ON).
# Spotify always comes back with lyrics ON regardless of what it was before.
RESTART_LYRICS_ON = True

# Prompt for volume/lyrics interactively on console startup.
# Set to False (default) to start immediately and seamlessly without blocking.
# Set to True if you want an interactive prompt every time you start from a terminal.
PROMPT_STARTUP_SETTINGS = False

# --- Ad Detection -------------------------------------------------
# Window titles that indicate an ad is playing.
# When music plays, title is "Song Name - Artist Name".
# When an ad plays, title becomes one of these or contains one of these strings.
#
# NOTE: All comparisons are case-insensitive and Unicode-normalized (NFKD),
# so you do NOT need to add separate entries for em-dash (–) vs hyphen (-).
# Just use a regular hyphen-minus (-) in your patterns.
AD_TITLE_EXACT = [
    "Listen to music, ad-free.",
    "Join Premium",
    "Advertisement",
]

AD_TITLE_CONTAINS = [
    # Core ad indicators
    "advertisement",         # Most common: title is just "Advertisement"
    "audio ad",              # Some builds use "Audio Ad" as the title
    "ad from",               # e.g. "Ad from BrandName"
    "sponsored",             # "Sponsored" label used in some regions

    # Full title variants (use regular hyphens; em-dashes are auto-normalized)
    "listen to music, ad-free.",   # Prompt to upgrade
    "advertisement - spotify",     # "Advertisement - Spotify" (with any dash)

    # NOTE: Do NOT add "spotify" or "spotify free" here!
    # Spotify displays "Spotify Free" when paused or idle.
    # Those ambiguous titles are verified dynamically via audio detection in monitor.py.
]

# --- Reliability -------------------------------------------------
# Automatically reload config every CONFIG_HOT_RELOAD_INTERVAL polls.
# Set to True so that changes to this file take effect without restarting.
CONFIG_HOT_RELOAD = True
CONFIG_HOT_RELOAD_INTERVAL = 20   # polls (× POLL_INTERVAL seconds per reload cycle)

# Print the normalized title and each tested pattern when no match is found.
# Useful for diagnosing why a new ad format is not being caught.
# Set to True temporarily while debugging; False for normal operation.
VERBOSE_AD_DETECTION = False

# Enable verbose console output for developers (shows internal debug traces,
# window tracking, UIA control lookups, audio peak logs, etc.).
# When False (default), only clean user-relevant events are printed.
VERBOSE_CONSOLE = False

# --- Spotify Path -------------------------------------------------
# Set to None for auto-detection, or provide a full path like:
# SPOTIFY_EXE_PATH = r"C:\Users\YourName\AppData\Roaming\Spotify\Spotify.exe"
SPOTIFY_EXE_PATH = None
