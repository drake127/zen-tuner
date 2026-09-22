"""
Abstract base class and event listener protocols for pluggable stress test runners.
Decouples stress engines (Prime95, stress-ng, etc.) from presentation and orchestration.
"""

import abc
import argparse
import signal
import subprocess
from typing import Protocol

from lib.models import MceEvent, RunResult, StretchSample, TestRequest


class TestEventListener(Protocol):
    """Callback protocol for receiving real-time events during a test run."""
    __test__ = False

    def on_output_line(self, line: str) -> None:
        """Called when a raw or sanitized stdout line is received from the runner."""
        ...

    def on_test_verified(self, iteration_name: str, completed_count: int) -> None:
        """Called when a self-test or iteration is mathematically validated."""
        ...

    def on_stretching_detected(self, sample: StretchSample) -> None:
        """Called when hardware clock stretching exceeds the configured threshold."""
        ...

    def on_hardware_error(self, event: MceEvent) -> None:
        """Called when an active hardware error or idle MCE is detected."""
        ...

    def on_grace_period_started(self, max_duration_s: float) -> None:
        """Called when the time target is reached and graceful completion begins."""
        ...


class StressRunner(abc.ABC):
    """Abstract base class for stress test execution engines."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable unique identifier for this runner (e.g. 'prime95', 'stress-ng')."""
        ...

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Checks if the required binary and dependencies are installed and executable."""
        ...

    @classmethod
    @abc.abstractmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Registers runner-specific CLI parameters into an argparse argument parser."""
        ...

    @abc.abstractmethod
    def run_test(
        self,
        request: TestRequest,
        listener: TestEventListener | None = None,
    ) -> RunResult:
        """
        Executes a stress run on requested logical CPUs according to parameters in request.
        Dispatches real-time progress to listener if provided. Returns structured RunResult.
        """
        ...


def parse_time(time_str: str) -> float:
    """Parses time strings like '300', '300s', '5m', '1h' into seconds as float."""
    time_str = time_str.strip().lower()
    if time_str.endswith("s"):
        return float(time_str[:-1])
    if time_str.endswith("m"):
        return float(time_str[:-1]) * 60.0
    if time_str.endswith("h"):
        return float(time_str[:-1]) * 3600.0
    return float(time_str)


def terminate_process(proc: subprocess.Popen, graceful: bool = True) -> None:
    """Gracefully sends SIGINT to child process, falling back to SIGTERM/SIGKILL after timeout."""
    if proc.poll() is not None:
        return

    sig = signal.SIGINT if graceful else signal.SIGTERM
    try:
        proc.send_signal(sig)
    except OSError:
        return

    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2.0)
        except OSError:
            pass
