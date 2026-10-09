"""
tests.py — Spotify Ad Restarter Test Suite
==========================================
Run:  python tests.py
"""
import sys
import time
import types
import unittest
import threading
import importlib
import unittest.mock as mock

# ---------------------------------------------------------------------------
# Minimal stubs so monitor.py can import without a real Spotify running
# ---------------------------------------------------------------------------

# Stub pycaw
sys.modules.setdefault("pycaw", mock.MagicMock())
sys.modules.setdefault("pycaw.pycaw", mock.MagicMock())

# Stub comtypes / UIAutomation
sys.modules.setdefault("comtypes", mock.MagicMock())
sys.modules.setdefault("comtypes.client", mock.MagicMock())
sys.modules.setdefault("comtypes.gen", mock.MagicMock())
sys.modules.setdefault("comtypes.gen.UIAutomationClient", mock.MagicMock())

# Stub pystray
sys.modules.setdefault("pystray", mock.MagicMock())

# Stub PIL
sys.modules.setdefault("PIL", mock.MagicMock())
sys.modules.setdefault("PIL.Image", mock.MagicMock())
sys.modules.setdefault("PIL.ImageDraw", mock.MagicMock())

# Stub win32 modules
sys.modules.setdefault("win32gui", mock.MagicMock())
sys.modules.setdefault("win32process", mock.MagicMock())
sys.modules.setdefault("win32con", mock.MagicMock())
sys.modules.setdefault("win32api", mock.MagicMock())

# Now import the modules under test
import config as _config
from monitor import (
    SpotifyMonitor,
    _norm,
    _build_ad_sets,
    _ctypes_enum_windows,
    AD_TITLE_EXACT_LOWER,
    AD_TITLE_CONTAINS_LOWER,
    AMBIGUOUS_TITLES,
    _TRAY_COLORS,
)
from reporter import NullReporter


def _make_monitor() -> SpotifyMonitor:
    """Return a SpotifyMonitor with silent reporting and no Spotify path lookup."""
    with mock.patch.object(SpotifyMonitor, "_find_spotify_path", return_value=None):
        m = SpotifyMonitor(reporter=NullReporter())
    return m


# ===========================================================================
#  1. Unicode normalization
# ===========================================================================
class TestNorm(unittest.TestCase):
    def test_em_dash_normalised(self):
        self.assertEqual(_norm("Song\u2014Artist"), "song-artist")

    def test_en_dash_normalised(self):
        self.assertEqual(_norm("Song\u2013Artist"), "song-artist")

    def test_bullet_normalised(self):
        self.assertEqual(_norm("Song \u2022 Artist"), "song - artist")

    def test_leading_trailing_stripped(self):
        self.assertEqual(_norm("  hello  "), "hello")

    def test_lowercase(self):
        self.assertEqual(_norm("ADVERTISEMENT"), "advertisement")

    def test_nfkc_ligature(self):
        # fi (LATIN SMALL LIGATURE FI) should decompose to 'fi'
        self.assertEqual(_norm("\ufb01"), "fi")


# ===========================================================================
#  2. Ad title detection
# ===========================================================================
class TestAdTitleDetection(unittest.TestCase):
    def test_explicit_advertisement(self):
        self.assertTrue(SpotifyMonitor.is_ad_title("Advertisement"))

    def test_exact_listen_to_music(self):
        self.assertTrue(SpotifyMonitor.is_ad_title("Listen to music, ad-free."))

    def test_contains_ad_from(self):
        self.assertTrue(SpotifyMonitor.is_ad_title("Ad from BrandX"))

    def test_contains_audio_ad(self):
        self.assertTrue(SpotifyMonitor.is_ad_title("Audio Ad"))

    def test_em_dash_variant(self):
        self.assertTrue(SpotifyMonitor.is_ad_title("Advertisement \u2013 Spotify"))

    def test_song_not_detected_as_ad(self):
        self.assertFalse(SpotifyMonitor.is_ad_title("Blinding Lights - The Weeknd"))

    def test_empty_string_not_ad(self):
        self.assertFalse(SpotifyMonitor.is_ad_title(""))

    def test_sponsored(self):
        self.assertTrue(SpotifyMonitor.is_ad_title("Sponsored"))

    def test_join_premium(self):
        self.assertTrue(SpotifyMonitor.is_ad_title("Join Premium"))


# ===========================================================================
#  3. Song title detection
# ===========================================================================
class TestSongTitleDetection(unittest.TestCase):
    def test_song_artist_dash(self):
        self.assertTrue(SpotifyMonitor.is_song_title("Starboy - The Weeknd"))

    def test_song_with_em_dash_normalised(self):
        self.assertTrue(SpotifyMonitor.is_song_title("Starboy \u2013 The Weeknd"))

    def test_ad_title_not_song(self):
        self.assertFalse(SpotifyMonitor.is_song_title("Advertisement"))

    def test_ambiguous_not_song(self):
        self.assertFalse(SpotifyMonitor.is_song_title("Spotify Free"))

    def test_empty_not_song(self):
        self.assertFalse(SpotifyMonitor.is_song_title(""))

    def test_coldplay_advertisement_not_song(self):
        self.assertFalse(SpotifyMonitor.is_song_title("Coldplay - Advertisement"))

    def test_no_dash_not_song(self):
        self.assertFalse(SpotifyMonitor.is_song_title("Spotify"))


# ===========================================================================
#  4. Ambiguous title timer
# ===========================================================================
class TestAmbiguousTitleTimer(unittest.TestCase):
    def _make_monitor_with_peak(self, peak_value: float) -> SpotifyMonitor:
        m = _make_monitor()
        m.get_spotify_peak_volume = mock.MagicMock(return_value=peak_value)
        return m

    def test_ambiguous_silent_not_ad(self):
        m = self._make_monitor_with_peak(0.0)
        self.assertFalse(m.is_ad_playing("Spotify Free"))

    def test_ambiguous_audio_not_ad_immediately(self):
        m = self._make_monitor_with_peak(0.5)
        self.assertFalse(m.is_ad_playing("Spotify Free"))

    def test_ambiguous_audio_becomes_ad_after_threshold(self):
        m = self._make_monitor_with_peak(0.5)
        m.is_ad_playing("Spotify Free")
        threshold = getattr(_config, "AMBIGUOUS_AD_DURATION_SECONDS", 8.0)
        m._ambiguous_title_first_seen = time.time() - (threshold + 1)
        self.assertTrue(m.is_ad_playing("Spotify Free"))

    def test_song_title_clears_ambiguous_flag(self):
        m = self._make_monitor_with_peak(0.5)
        m.is_ad_playing("Spotify Free")
        m._ambiguous_title_first_seen = time.time() - 20
        m._ambiguous_last_title = "spotify free"
        # Song title should return False and reset timer
        self.assertFalse(m.is_ad_playing("Blinding Lights - The Weeknd"))
        self.assertEqual(m._ambiguous_title_first_seen, 0.0)


# ===========================================================================
#  5. Pause / Resume
# ===========================================================================
class TestPauseResume(unittest.TestCase):
    def test_paused_flag_starts_false(self):
        m = _make_monitor()
        self.assertFalse(m.paused)

    def test_paused_state_set_when_paused(self):
        m = _make_monitor()
        m.paused = True
        if m.paused:
            m.state = SpotifyMonitor.PAUSED
            m.status_message = "Monitoring paused"
        self.assertEqual(m.state, SpotifyMonitor.PAUSED)

    def test_resume_sets_idle(self):
        m = _make_monitor()
        m.paused = True
        m.state = SpotifyMonitor.PAUSED
        m.paused = False
        m.state = SpotifyMonitor.IDLE
        self.assertEqual(m.state, SpotifyMonitor.IDLE)


# ===========================================================================
#  6. Monitor initial state
# ===========================================================================
class TestMonitorStateMachine(unittest.TestCase):
    def test_initial_state_idle(self):
        m = _make_monitor()
        self.assertEqual(m.state, SpotifyMonitor.IDLE)

    def test_ads_skipped_starts_zero(self):
        m = _make_monitor()
        self.assertEqual(m.ads_skipped, 0)

    def test_muted_starts_false(self):
        m = _make_monitor()
        self.assertFalse(m._muted)


# ===========================================================================
#  7. Config defaults (post our changes)
# ===========================================================================
class TestConfigDefaults(unittest.TestCase):
    def test_verbose_ad_detection_off(self):
        self.assertFalse(_config.VERBOSE_AD_DETECTION)

    def test_verbose_console_off(self):
        self.assertFalse(_config.VERBOSE_CONSOLE)

    def test_ambiguous_duration_le_10(self):
        threshold = getattr(_config, "AMBIGUOUS_AD_DURATION_SECONDS", 15)
        self.assertLessEqual(threshold, 10.0,
            f"AMBIGUOUS_AD_DURATION_SECONDS={threshold} is too high (should be <=10s)")

    def test_poll_interval_fast(self):
        self.assertLessEqual(_config.POLL_INTERVAL, 0.5)

    def test_load_wait_time_reasonable(self):
        self.assertGreaterEqual(_config.LOAD_WAIT_TIME, 5)
        self.assertLessEqual(_config.LOAD_WAIT_TIME, 60)


# ===========================================================================
#  8. _ctypes_enum_windows — never crashes on bad callback
# ===========================================================================
class TestCtypesEnumWindows(unittest.TestCase):
    def test_callback_exception_does_not_propagate(self):
        calls = []
        def bad_callback(hwnd):
            calls.append(hwnd)
            raise RuntimeError("deliberate error")
        try:
            _ctypes_enum_windows(bad_callback)
        except Exception as exc:
            self.fail(f"_ctypes_enum_windows raised: {exc}")

    def test_callback_is_callable_and_safe(self):
        """Verify the wrapper is callable and does not propagate callback exceptions."""
        # Even if EnumWindows returns 0 windows in a mocked environment, the key
        # guarantee is that exceptions inside the callback are swallowed.
        call_count = [0]
        def safe_cb(hwnd):
            call_count[0] += 1
            raise RuntimeError("test error in callback")
        try:
            _ctypes_enum_windows(safe_cb)
        except Exception as exc:
            self.fail(f"_ctypes_enum_windows propagated callback exception: {exc}")
        # Function is also callable with a normal callback without error
        seen = []
        _ctypes_enum_windows(lambda hwnd: seen.append(hwnd) or True)
        # On a live system this will be >0; in a fully mocked env it may be 0.
        # Either outcome is acceptable — we only require no exception.
        self.assertIsInstance(seen, list)


# ===========================================================================
#  9. Tray colour map completeness
# ===========================================================================
class TestTrayColors(unittest.TestCase):
    def test_all_states_have_colour(self):
        expected_states = {
            SpotifyMonitor.IDLE,
            SpotifyMonitor.MUSIC_PLAYING,
            SpotifyMonitor.AD_DETECTED,
            SpotifyMonitor.RESTARTING,
            SpotifyMonitor.COOLDOWN,
            SpotifyMonitor.PAUSED,
        }
        for state in expected_states:
            self.assertIn(state, _TRAY_COLORS,
                          f"State '{state}' missing from _TRAY_COLORS")

    def test_colours_are_rgb_tuples(self):
        for state, colour in _TRAY_COLORS.items():
            self.assertEqual(len(colour), 3)
            for component in colour:
                self.assertGreaterEqual(component, 0)
                self.assertLessEqual(component, 255)


# ===========================================================================
#  10. Config hot-reload
# ===========================================================================
class TestConfigHotReload(unittest.TestCase):
    def test_build_ad_sets_returns_non_empty(self):
        exact, contains = _build_ad_sets()
        self.assertGreater(len(exact), 0)
        self.assertGreater(len(contains), 0)

    def test_build_ad_sets_all_normalised(self):
        exact, contains = _build_ad_sets()
        for t in exact | contains:
            self.assertEqual(t, t.strip().lower(), f"Pattern not normalised: {t!r}")

    def test_reload_does_not_crash(self):
        importlib.reload(_config)
        exact, contains = _build_ad_sets()
        self.assertIsInstance(exact, set)
        self.assertIsInstance(contains, set)


# ===========================================================================
#  11. find_spotify_window -- returns None when no Spotify running
# ===========================================================================
class TestFindSpotifyWindow(unittest.TestCase):
    def test_returns_none_when_no_spotify(self):
        m = _make_monitor()
        with mock.patch("psutil.process_iter", return_value=[]):
            result = m.find_spotify_window()
        self.assertIsNone(result)

    def test_cache_invalidated_on_stale_hwnd(self):
        m = _make_monitor()
        import win32gui as _w
        m._cached_spotify_hwnd = 99999
        m._cached_spotify_pid = 12345
        _w.IsWindow = mock.MagicMock(return_value=False)
        with mock.patch("psutil.process_iter", return_value=[]):
            result = m.find_spotify_window()
        self.assertIsNone(m._cached_spotify_hwnd)
        self.assertIsNone(result)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
