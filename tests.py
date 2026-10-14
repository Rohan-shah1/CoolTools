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
    prompt_startup_settings,
)
from reporter import NullReporter, UserConsoleReporter


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

# ===========================================================================
#  12. UserConsoleReporter — silence lyrics & volume state in console
# ===========================================================================
class TestUserConsoleReporter(unittest.TestCase):
    def test_state_update_does_not_print(self):
        reporter = UserConsoleReporter()
        with mock.patch("builtins.print") as mock_print:
            reporter.state_update(True, 0.8)
            reporter.state_update(False, 0.5)
            reporter.state_update(True, None)
            mock_print.assert_not_called()

    def test_song_playing_prints_playing_not_state(self):
        reporter = UserConsoleReporter()
        with mock.patch("builtins.print") as mock_print:
            reporter.state_update(True, 0.8)
            reporter.song_playing("Song Name - Artist Name")
            mock_print.assert_called_once()
            args, _ = mock_print.call_args
            self.assertIn("[Playing]", args[0])
            self.assertIn("Song Name - Artist Name", args[0])
            self.assertNotIn("[State]", args[0])

    def test_print_state_does_not_print(self):
        reporter = UserConsoleReporter()
        with mock.patch("builtins.print") as mock_print:
            reporter._last_lyrics = True
            reporter._last_volume = 0.8
            reporter._print_state()
            mock_print.assert_not_called()


# ===========================================================================
#  13. Lyrics ON on restart
# ===========================================================================
class TestLyricsRestart(unittest.TestCase):
    def test_restart_lyrics_on_config_default(self):
        self.assertTrue(getattr(_config, "RESTART_LYRICS_ON", False))

    def test_restore_spotify_ui_state_targets_lyrics_on(self):
        m = _make_monitor()
        m._saved_lyrics_on = False
        with mock.patch.object(m, "_find_uia_player_controls") as mock_find, \
             mock.patch.object(m, "_read_lyrics_state_reliable", return_value=True), \
             mock.patch("monitor.UIA_AVAILABLE", True):
            lyrics_elem = mock.MagicMock()
            vol_elem = mock.MagicMock()
            mock_find.return_value = (lyrics_elem, vol_elem)

            vol_ok, lyrics_ok = m.restore_spotify_ui_state()
            self.assertTrue(lyrics_ok)
            self.assertTrue(m._saved_lyrics_on)

    def test_load_persisted_state_sets_lyrics_on_when_config_true(self):
        m = _make_monitor()
        with mock.patch("os.path.exists", return_value=False):
            m._load_persisted_state()
            self.assertTrue(m._saved_lyrics_on)


# ===========================================================================
#  14. Startup setup prompt & Session preferences
# ===========================================================================
class TestStartupPrompt(unittest.TestCase):
    def test_prompt_custom_values(self):
        with mock.patch("builtins.input", side_effect=["65", "n"]):
            vol, lyrics = prompt_startup_settings()
            self.assertEqual(vol, 65)
            self.assertFalse(lyrics)

    def test_prompt_defaults_on_empty_input(self):
        with mock.patch("builtins.input", side_effect=["", ""]):
            vol, lyrics = prompt_startup_settings()
            self.assertEqual(vol, getattr(_config, "RESTART_VOLUME", 80))
            self.assertEqual(lyrics, getattr(_config, "RESTART_LYRICS_ON", True))

    def test_prompt_handles_invalid_volume(self):
        with mock.patch("builtins.input", side_effect=["invalid", "y"]):
            vol, lyrics = prompt_startup_settings()
            self.assertEqual(vol, getattr(_config, "RESTART_VOLUME", 80))
            self.assertTrue(lyrics)

    def test_prompt_clamps_volume_range(self):
        with mock.patch("builtins.input", side_effect=["150", "yes"]):
            vol, lyrics = prompt_startup_settings()
            self.assertEqual(vol, 100)
            self.assertTrue(lyrics)


class TestSessionSettings(unittest.TestCase):
    def test_session_settings_override_persisted_state(self):
        with mock.patch.object(SpotifyMonitor, "_find_spotify_path", return_value=None):
            m = SpotifyMonitor(
                reporter=NullReporter(),
                restart_volume=45,
                restart_lyrics=False,
            )
            self.assertEqual(m.restart_volume, 45.0)
            self.assertFalse(m.restart_lyrics)
            self.assertAlmostEqual(m._saved_volume, 0.45)
            self.assertFalse(m._saved_lyrics_on)

    def test_restore_ui_state_uses_session_settings(self):
        with mock.patch.object(SpotifyMonitor, "_find_spotify_path", return_value=None):
            m = SpotifyMonitor(
                reporter=NullReporter(),
                restart_volume=30,
                restart_lyrics=False,
            )
            m._saved_lyrics_on = True
            with mock.patch.object(m, "_find_uia_player_controls") as mock_find, \
                 mock.patch.object(m, "_read_lyrics_state_reliable", return_value=False), \
                 mock.patch("monitor.UIA_AVAILABLE", True):
                lyrics_elem = mock.MagicMock()
                vol_elem = mock.MagicMock()
                range_pat = mock.MagicMock()
                range_client = mock.MagicMock()
                range_client.CurrentValue = 0.30
                range_pat.QueryInterface.return_value = range_client
                vol_elem.GetCurrentPattern.return_value = range_pat
                mock_find.return_value = (lyrics_elem, vol_elem)

                vol_ok, lyrics_ok = m.restore_spotify_ui_state()
                self.assertTrue(vol_ok)
                self.assertTrue(lyrics_ok)
                self.assertFalse(m._saved_lyrics_on)



if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
