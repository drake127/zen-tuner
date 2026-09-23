"""
Shared pytest fixtures and test doubles.
"""

import pytest

from lib.models import PhysicalCore


class RecordingListener:
    """TestEventListener recording every event for assertions."""

    def __init__(self):
        self.lines: list[str] = []
        self.verified: list[tuple[str, int]] = []
        self.hardware_errors = []
        self.stretching = []

    def on_output_line(self, line: str) -> None:
        self.lines.append(line)

    def on_test_verified(self, step_name: str, completed_iterations: int) -> None:
        self.verified.append((step_name, completed_iterations))

    def on_stretching_detected(self, sample) -> None:
        self.stretching.append(sample)

    def on_hardware_error(self, event) -> None:
        self.hardware_errors.append(event)


@pytest.fixture
def listener() -> RecordingListener:
    return RecordingListener()


@pytest.fixture
def two_cores() -> list[PhysicalCore]:
    return [
        PhysicalCore(core_idx=0, hardware_core_id=0, ccd_id=0, logical_cpus=[0, 12]),
        PhysicalCore(core_idx=1, hardware_core_id=1, ccd_id=0, logical_cpus=[1, 13]),
    ]


@pytest.fixture
def no_kmsg(monkeypatch):
    """Keeps the host's kernel log out of runner tests."""
    monkeypatch.setattr("lib.monitors.KMSG_PATH", "/nonexistent/kmsg")
