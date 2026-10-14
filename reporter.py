"""
Spotify Ad Restarter -- Console & Status Reporting Abstraction
==============================================================
Provides an Object-Oriented Programming (OOP) abstraction for logging,
event notifications, and console presentation.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
import sys


class LogLevel(Enum):
    DEBUG = 0
    INFO = 1
    PLAYING = 2
    STATE = 3
    AD = 4
    RESUMED = 5
    WARNING = 6
    ERROR = 7
    SHUTDOWN = 8


class BaseReporter(ABC):
    """Abstract base class defining the reporting interface for Spotify events."""

    @abstractmethod
    def startup(self, app_name: str, details: dict | None = None) -> None:
        """Called when the application starts up."""
        pass

    @abstractmethod
    def song_playing(self, song_title: str) -> None:
        """Called when music starts playing or the song changes."""
        pass

    @abstractmethod
    def state_update(self, lyrics_on: bool | None, volume: float | None) -> None:
        """Called when lyrics status (ON/OFF) or volume level (0.0 - 1.0) is captured or updated."""
        pass

    @abstractmethod
    def ad_detected(self, ad_title: str, last_song: str | None = None) -> None:
        """Called when an advertisement is detected."""
        pass

    @abstractmethod
    def ad_skipped(self, ads_skipped: int, current_song: str | None = None) -> None:
        """Called when an ad has been bypassed and Spotify restarted."""
        pass

    @abstractmethod
    def status_change(self, status: str) -> None:
        """Called when the monitor status changes (e.g. Paused, Waiting)."""
        pass

    @abstractmethod
    def warning(self, message: str) -> None:
        """Called for actionable warning messages."""
        pass

    @abstractmethod
    def error(self, message: str) -> None:
        """Called for error conditions."""
        pass

    @abstractmethod
    def debug(self, message: str) -> None:
        """Called for low-level diagnostic and debug information."""
        pass

    @abstractmethod
    def shutdown(self, message: str = "Goodbye!") -> None:
        """Called when the application is shutting down."""
        pass

    @abstractmethod
    def log(self, message: str, level: LogLevel = LogLevel.INFO) -> None:
        """General logging dispatch."""
        pass


class UserConsoleReporter(BaseReporter):
    """Console reporter designed for end users.
    
    Strictly displays ONLY what the user needs:
    1. Current music playing
    2. Spotify restarted and total ads skipped when an ad is detected
    All internal traces, startup blurbs, background status, and idle notifications are silenced.
    """

    def __init__(self):
        self._last_song: str | None = None
        self._last_lyrics: bool | None = None
        self._last_volume: float | None = None

    def _write_line(self, tag: str, message: str) -> None:
        """Write a formatted message with a timestamp and tag safely to stdout."""
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] [{tag}] {message}"
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", errors="replace").decode("ascii"))

    def startup(self, app_name: str, details: dict | None = None) -> None:
        # Silenced: user only wants song and ad restart stats
        pass

    def song_playing(self, song_title: str) -> None:
        if song_title != self._last_song:
            self._last_song = song_title
            self._write_line("Playing", song_title)

    def state_update(self, lyrics_on: bool | None, volume: float | None) -> None:
        # Silenced: do not print state of lyrics and volume in console
        if lyrics_on is not None:
            self._last_lyrics = lyrics_on
        if volume is not None:
            self._last_volume = volume

    def _print_state(self) -> None:
        # Silenced: do not print state of lyrics and volume in console
        pass

    def ad_detected(self, ad_title: str, last_song: str | None = None) -> None:
        # Silenced pre-restart: user wants to see the restart message once skipped
        pass

    def ad_skipped(self, ads_skipped: int, current_song: str | None = None) -> None:
        # "if there is ads detected then show spotify restarted and total ads skipped"
        self._write_line("Ad Detected", f"Spotify restarted | Total ads skipped: {ads_skipped}")

    def status_change(self, status: str) -> None:
        # Silenced
        pass

    def warning(self, message: str) -> None:
        # Silenced
        pass

    def error(self, message: str) -> None:
        # Silenced
        pass

    def debug(self, message: str) -> None:
        # Silenced
        pass

    def shutdown(self, message: str = "Goodbye!") -> None:
        self._write_line("Shutdown", message)

    def log(self, message: str, level: LogLevel = LogLevel.INFO) -> None:
        pass


class VerboseConsoleReporter(UserConsoleReporter):
    """Verbose reporter that outputs all user events PLUS debug diagnostic traces.
    
    Used when VERBOSE_CONSOLE = True in config.py for development and troubleshooting.
    """

    def startup(self, app_name: str, details: dict | None = None) -> None:
        self._write_line("Startup", f"{app_name} is active")
        if details:
            self._write_line("Startup", f"Details: {details}")

    def status_change(self, status: str) -> None:
        self._write_line("Status", status)

    def ad_detected(self, ad_title: str, last_song: str | None = None) -> None:
        last = f" | Last song: '{last_song}'" if last_song else ""
        self._write_line("Ad Detected", f"Triggered by title: '{ad_title}'{last}")

    def warning(self, message: str) -> None:
        self._write_line("Warning", message)

    def error(self, message: str) -> None:
        self._write_line("Error", message)

    def debug(self, message: str) -> None:
        self._write_line("Debug", message)

    def shutdown(self, message: str = "Goodbye!") -> None:
        self._write_line("Shutdown", message)


class NullReporter(BaseReporter):
    """Silent reporter that suppresses all console output."""

    def startup(self, app_name: str, details: dict | None = None) -> None:
        pass

    def song_playing(self, song_title: str) -> None:
        pass

    def state_update(self, lyrics_on: bool | None, volume: float | None) -> None:
        pass

    def ad_detected(self, ad_title: str, last_song: str | None = None) -> None:
        pass

    def ad_skipped(self, ads_skipped: int, current_song: str | None = None) -> None:
        pass

    def status_change(self, status: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass

    def debug(self, message: str) -> None:
        pass

    def shutdown(self, message: str = "Goodbye!") -> None:
        pass

    def log(self, message: str, level: LogLevel = LogLevel.INFO) -> None:
        pass
