"""
Event contract between stress runners and presenters.
Runners report per-run progress through a TestEventListener; presenters implement it.
"""

from typing import Protocol

from lib.models import TelemetrySample


class TestEventListener(Protocol):
    """Callback protocol for receiving real-time events during a test run."""
    __test__ = False

    def on_output_line(self, line: str) -> None:
        """Called for every non-empty sanitized output line of the stress process."""
        ...

    def on_test_verified(self, step_name: str, completed_iterations: int, target_iterations: int) -> None:
        """Called when a test step (e.g. 'FFT 4.5K', 'BBP') is validated by the stress engine."""
        ...

    def on_telemetry_sample(self, sample: TelemetrySample) -> None:
        """Called for every SMU telemetry sample of the tested core taken under full load."""
        ...


class NullListener:
    """Listener ignoring all events."""

    def on_output_line(self, line: str) -> None:
        pass

    def on_test_verified(self, step_name: str, completed_iterations: int, target_iterations: int) -> None:
        pass

    def on_telemetry_sample(self, sample: TelemetrySample) -> None:
        pass
