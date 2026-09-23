"""
Stress runner plugin interface and the shared process supervision loop.
Runners only describe how to configure an engine (_prepare) and how to interpret its output (OutputParser);
spawning, output reading, telemetry sampling, stopping and exit classification are common to all engines.
"""

import abc
import argparse
import codecs
from collections import deque
from dataclasses import dataclass, field
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, ClassVar

from lib.events import NullListener, TestEventListener
from lib.models import RunResult, RunStatus, TestRequest
from lib.monitors import CoreTelemetryMonitor
from lib.sched import inherited_scheduling
from lib.smu import RyzenSmuMonitor
from lib.ui import strip_ansi

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

POLL_INTERVAL_S = 0.1
STOP_TIMEOUT_S = 5.0
DRAIN_TIMEOUT_S = 1.0
OUTPUT_HISTORY_LINES = 5000

LINE_SPLIT_PATTERN = re.compile(r"[\r\n]+")


@dataclass
class RunContext:
    """Environment of a single test run: event sink, cancellation token and optional SMU telemetry source."""
    listener: TestEventListener = field(default_factory=NullListener)
    cancel: threading.Event = field(default_factory=threading.Event)
    smu_monitor: RyzenSmuMonitor | None = None


class RunnerUnavailableError(RuntimeError):
    """Raised when a stress engine binary is missing or not executable."""


class OutputParser(abc.ABC):
    """Interprets the output of a single stress process run and tracks verified iterations and errors."""

    def __init__(self, target_iterations: int, listener: TestEventListener):
        self.target_iterations = target_iterations
        self.listener = listener
        self.completed_iterations = 0
        self.steps_verified = 0
        self.errors: list[str] = []
        self.output: deque[str] = deque(maxlen=OUTPUT_HISTORY_LINES)

    @property
    def done(self) -> bool:
        return self.completed_iterations >= self.target_iterations

    def clean_line(self, line: str) -> str:
        """Normalizes a raw output line for display and parsing."""
        return strip_ansi(line).strip()

    def handle_line(self, raw_line: str) -> None:
        line = self.clean_line(raw_line)
        if line:
            self.output.append(line)
            self.listener.on_output_line(line)
            self.feed(line)

    @abc.abstractmethod
    def feed(self, line: str) -> None:
        """Parses one cleaned output line."""

    def poll(self) -> None:
        """Hook for reading side channels (e.g. result files); called on every supervision tick and after exit."""

    def close(self) -> None:
        """Releases resources held by the parser."""

    def add_error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def step_verified(self, step_name: str, completes_iteration: bool) -> None:
        self.steps_verified += 1
        if completes_iteration:
            self.completed_iterations += 1
        self.listener.on_test_verified(step_name, self.completed_iterations, self.target_iterations)


class _ProcessOutput:
    """Non-blocking line reader over a pipe or PTY master file descriptor."""

    def __init__(self, fd: int):
        self.fd = fd
        self.eof = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        os.set_blocking(fd, False)

    def read_lines(self, timeout: float) -> list[str]:
        if self.eof:
            return []
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return []
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            except OSError:
                # A PTY master reports EIO once the slave side is closed by the exited child.
                chunk = b""
            if not chunk:
                self.eof = True
                break
            self._pending += self._decoder.decode(chunk)
        parts = LINE_SPLIT_PATTERN.split(self._pending)
        self._pending = parts.pop()
        if self.eof:
            parts.append(self._pending + self._decoder.decode(b"", final=True))
            self._pending = ""
        return parts

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def parse_duration(value: str) -> float:
    """Parses durations like '300', '300s', '5m', '1h' into seconds; a bare number means seconds."""
    text = str(value).strip().lower()
    multipliers = {"s": 1.0, "m": 60.0, "h": 3600.0}
    unit = text[-1:] if text[-1:] in multipliers else "s"
    number = text[:-1] if text[-1:] in multipliers else text
    try:
        seconds = float(number) * multipliers[unit]
    except ValueError as exc:
        raise ValueError(f"Invalid duration: '{value}' (expected e.g. '90', '90s', '2m', '1h')") from exc
    if seconds <= 0:
        raise ValueError(f"Duration must be positive: '{value}'")
    return seconds


@dataclass
class _Supervision:
    returncode: int | None
    stop_reason: RunStatus | None
    elapsed_seconds: float


class StressRunner(abc.ABC):
    """
    Base class for stress test execution engines.
    Subclasses describe how to configure and launch the engine (_prepare) and how to interpret its output
    (_create_parser); run_test supervises the process uniformly for all engines.
    """

    name: ClassVar[str]
    # Engine binary bundled under contrib/ (relative to the repository root) and its name on PATH as a fallback.
    bundled_binary: ClassVar[str]
    binary_name: ClassVar[str]
    # Signal asking the engine to stop gracefully; delivered to the whole process group.
    stop_signal: ClassVar[int] = signal.SIGINT
    # Engines that block-buffer or suppress output when stdout is not a terminal run on a PTY.
    use_pty: ClassVar[bool] = False

    def __init__(self, binary_path: str | None = None, base_work_dir: str | None = None):
        self.binary = self._resolve_binary(binary_path)
        self.base_work_dir = base_work_dir

    @classmethod
    def _resolve_binary(cls, binary_path: str | None) -> str | None:
        if binary_path:
            return os.path.abspath(binary_path)
        bundled = os.path.join(REPO_ROOT, cls.bundled_binary)
        return bundled if os.access(bundled, os.X_OK) else shutil.which(cls.binary_name)

    def is_available(self) -> bool:
        """Checks if the engine binary is installed and executable."""
        return self.binary is not None and os.access(self.binary, os.X_OK)

    @classmethod
    @abc.abstractmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Registers runner-specific CLI parameters into an argparse argument parser."""

    @classmethod
    @abc.abstractmethod
    def parse_parameters(cls, args: argparse.Namespace) -> tuple[Any, str]:
        """Validates runner CLI args, returning (parameters, profile description). Raises ValueError on bad input."""

    @abc.abstractmethod
    def _prepare(self, request: TestRequest, work_dir: str) -> list[str]:
        """Writes engine configuration into work_dir and returns the command line to execute."""

    @abc.abstractmethod
    def _create_parser(self, request: TestRequest, work_dir: str, listener: TestEventListener) -> OutputParser:
        """Creates the output parser for one run."""

    def run_test(self, request: TestRequest, context: RunContext | None = None) -> RunResult:
        """Executes one stress run on the requested logical CPUs and returns its classified result."""
        if not self.is_available():
            raise RunnerUnavailableError(f"{self.name} binary is not available")
        context = context or RunContext()

        work_dir = tempfile.mkdtemp(prefix=f"zen_tuner_{self.name}_", dir=self.base_work_dir)
        keep_work_dir = False
        try:
            cmd = self._prepare(request, work_dir)
            parser = self._create_parser(request, work_dir, context.listener)
            telemetry = None
            if context.smu_monitor is not None and request.core_idx is not None and context.smu_monitor.is_available():
                telemetry = CoreTelemetryMonitor(context.smu_monitor, request.core_idx)
            try:
                run = self._supervise(cmd, work_dir, request, context, parser, telemetry)
            finally:
                parser.close()

            status, message = self._classify(run, parser, context)
            keep_work_dir = status in (RunStatus.ERROR, RunStatus.CRASH, RunStatus.UNVERIFIED)
            return RunResult(
                status=status,
                completed_iterations=parser.completed_iterations,
                elapsed_seconds=run.elapsed_seconds,
                error_message=message,
                telemetry=telemetry.summary if telemetry else None,
                output=list(parser.output),
                work_dir=work_dir if keep_work_dir else None,
            )
        finally:
            if not keep_work_dir:
                shutil.rmtree(work_dir, ignore_errors=True)

    def _spawn(self, cmd: list[str], work_dir: str) -> tuple[subprocess.Popen, _ProcessOutput]:
        # The child gets its own process group: terminal Ctrl+C reaches only the controller, which then stops
        # the engine deliberately (and the whole group, including helper processes the engine may fork).
        if self.use_pty:
            master_fd, slave_fd = pty.openpty()
            try:
                proc = subprocess.Popen(cmd, cwd=work_dir, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                                        process_group=0)
            except BaseException:
                os.close(master_fd)
                raise
            finally:
                os.close(slave_fd)
            return proc, _ProcessOutput(master_fd)

        proc = subprocess.Popen(cmd, cwd=work_dir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, process_group=0)
        return proc, _ProcessOutput(os.dup(proc.stdout.fileno()))

    def _supervise(
        self,
        cmd: list[str],
        work_dir: str,
        request: TestRequest,
        context: RunContext,
        parser: OutputParser,
        telemetry: CoreTelemetryMonitor | None,
    ) -> _Supervision:
        """Runs the engine until it exits; stops it once done, on error, or on cancellation."""
        start = time.monotonic()
        with inherited_scheduling(request.cpus):
            proc, output = self._spawn(cmd, work_dir)
        stop_reason: RunStatus | None = None
        stop_deadline = 0.0
        try:
            while True:
                for line in output.read_lines(POLL_INTERVAL_S):
                    parser.handle_line(line)
                parser.poll()

                if telemetry is not None:
                    sample = telemetry.poll()
                    if sample is not None:
                        context.listener.on_telemetry_sample(sample)

                if proc.poll() is not None:
                    drain_deadline = time.monotonic() + DRAIN_TIMEOUT_S
                    while not output.eof and time.monotonic() < drain_deadline:
                        for line in output.read_lines(POLL_INTERVAL_S):
                            parser.handle_line(line)
                    parser.poll()
                    break

                if stop_reason is None:
                    if context.cancel.is_set():
                        stop_reason = RunStatus.INTERRUPTED
                    elif parser.errors:
                        stop_reason = RunStatus.ERROR
                    elif parser.done:
                        stop_reason = RunStatus.PASS
                    if stop_reason is not None:
                        _signal_group(proc, self.stop_signal)
                        stop_deadline = time.monotonic() + STOP_TIMEOUT_S
                elif time.monotonic() > stop_deadline:
                    _signal_group(proc, signal.SIGKILL)
        finally:
            # Kill the whole group, including helper processes the engine may have left behind, then reap.
            _signal_group(proc, signal.SIGKILL)
            proc.wait()
            output.close()
            if proc.stdout is not None:
                proc.stdout.close()
        return _Supervision(proc.returncode, stop_reason, time.monotonic() - start)

    @staticmethod
    def _classify(run: _Supervision, parser: OutputParser, context: RunContext) -> tuple[RunStatus, str | None]:
        if run.stop_reason == RunStatus.INTERRUPTED or context.cancel.is_set():
            return RunStatus.INTERRUPTED, "Test interrupted by user"
        if parser.errors:
            return RunStatus.ERROR, parser.errors[0]
        if run.stop_reason is None and run.returncode:
            if run.returncode < 0:
                sig = -run.returncode
                return RunStatus.CRASH, f"Process terminated by signal {sig} ({signal.strsignal(sig)})"
            return RunStatus.CRASH, f"Process exited abnormally with code {run.returncode}"
        if parser.done:
            return RunStatus.PASS, None
        return RunStatus.UNVERIFIED, (
            f"Process ended after {parser.completed_iterations} of {parser.target_iterations} verified iterations"
        )


@dataclass(frozen=True)
class Engine:
    """A stress runner paired with its validated parameters."""
    runner: StressRunner
    parameters: Any
    profile: str
