"""
Terminal utilities for Zen Tuner: session log file, ANSI colour constants, and helper functions.
"""

import datetime
import os
import re
import threading

# ANSI Color codes
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
WHITE = "\033[37m"
RESET = "\033[0m"

ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Removes all ANSI terminal control escape sequences from text."""
    return ANSI_ESCAPE_RE.sub("", text)


def get_iso_timestamp() -> str:
    """Returns the current local date and time formatted in ISO 8601 (%Y-%m-%dT%H:%M:%S)."""
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class Logger:
    """Persists an uncolored plain-text copy of session messages to a log file."""

    def __init__(self, log_path: str):
        self.log_path = log_path
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        self.file = open(log_path, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def __enter__(self) -> "Logger":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def write(self, text: str) -> None:
        with self._lock:
            if not self.file.closed:
                self.file.write(strip_ansi(text) + "\n")
                self.file.flush()

    def close(self) -> None:
        with self._lock:
            if not self.file.closed:
                self.file.close()
