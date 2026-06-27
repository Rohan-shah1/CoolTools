"""
Spotify Ad Restarter -- Configuration
=====================================
Adjust these values to fine-tune the tool's behavior.
"""

# --- Timing -------------------------------------------------------
POLL_INTERVAL = 1.5       # Seconds between window title checks
RESTART_DELAY = 0         # Seconds to wait after killing Spotify before relaunch (0 = immediate)
LOAD_WAIT_TIME = 5        # Max seconds to wait for Spotify window to appear (polls every 0.5s)
COOLDOWN_PERIOD = 240     # Seconds (4 min) cooldown after a restart to avoid loops

# --- Ad Detection -------------------------------------------------
# Window titles that indicate an ad is playing.
# When music plays, title is "Song Name - Artist Name".
# When an ad plays, title becomes one of these or contains "Advertisement".
AD_TITLE_EXACT = [
    "Listen to music, ad-free.",
    "Spotify Free",
    "Join Premium",    
	
]

AD_TITLE_CONTAINS = [
    "advertisement",
    "Sponsored",
    "Listen to music, ad-free.",

]

# --- Spotify Path -------------------------------------------------
# Set to None for auto-detection, or provide a full path like:
# SPOTIFY_EXE_PATH = r"C:\Users\YourName\AppData\Roaming\Spotify\Spotify.exe"
SPOTIFY_EXE_PATH = None
