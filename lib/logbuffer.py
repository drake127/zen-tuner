"""
Thread-safe bounded log line store with a scroll position, shared by the producer (orchestrator/runner thread)
and the UI thread that renders it.
"""

from collections import deque
import threading

from lib.ui import strip_ansi
from lib.views import Tone


class LogBuffer:
    """Keeps the newest log lines; offset 0 follows the tail, a positive offset counts lines scrolled up."""

    def __init__(self, maxlen: int = 4096):
        self._lines: deque[tuple[str, Tone]] = deque(maxlen=maxlen)
        self._offset = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._lines)

    def __getitem__(self, index: int) -> tuple[str, Tone]:
        with self._lock:
            return self._lines[index]

    def append(self, text: str, tone: Tone = Tone.DEFAULT) -> None:
        with self._lock:
            self._lines.append((strip_ansi(text), tone))
            if self._offset > 0:
                # Keep the scrolled-up view anchored while new lines arrive
                self._offset = min(self._offset + 1, len(self._lines) - 1)

    def _clamp(self, offset: int, visible: int) -> int:
        return max(0, min(offset, len(self._lines) - visible))

    def scroll(self, delta: int, visible: int) -> None:
        """Positive delta scrolls towards older lines, negative towards the tail."""
        with self._lock:
            self._offset = self._clamp(self._offset + delta, visible)

    def scroll_to_oldest(self, visible: int) -> None:
        with self._lock:
            self._offset = self._clamp(len(self._lines), visible)

    def follow(self) -> None:
        with self._lock:
            self._offset = 0

    def view(self, visible: int) -> tuple[list[tuple[str, Tone]], int]:
        """Returns the lines of a window of the given height and the effective scroll offset."""
        with self._lock:
            self._offset = self._clamp(self._offset, visible)
            end = len(self._lines) - self._offset
            return list(self._lines)[max(0, end - visible):end], self._offset
