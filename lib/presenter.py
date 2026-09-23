"""
Presentation layer base: turns orchestration and runner events into tone-annotated log messages.
Concrete presenters only decide where messages go (plain console, curses dashboard); the base presenter
itself writes messages only to the session log file and prints just the final summary.
"""

from dataclasses import dataclass
import sys
import time

from lib.models import CoreStats, MceEvent, PhysicalCore, RunResult, RunStatus, TelemetrySample
from lib.smu import RyzenSmuMonitor
from lib.ui import RESET, Logger, get_iso_timestamp, strip_ansi
from lib.views import ANSI_TONES, Tone, ccd_label, render_summary


@dataclass(frozen=True)
class SessionInfo:
    """Static description of the test session shown by presenters."""
    profile: str = ""
    hyperthreading_mode: str = "on"
    target_iterations: int = 1
    total_cycles: int = 1


class Presenter:
    """Event sink for the orchestrator and runners (implements runners.base.TestEventListener)."""

    def __init__(
        self,
        all_cores: list[PhysicalCore],
        session: SessionInfo | None = None,
        logger: Logger | None = None,
        smu_monitor: RyzenSmuMonitor | None = None,
    ):
        self.all_cores = all_cores
        self.session = session or SessionInfo()
        self.logger = logger
        self.smu_monitor = smu_monitor
        self.stats: dict[int, CoreStats] = {c.core_idx: CoreStats() for c in all_cores}
        self.cpu_to_core = {cpu: c for c in all_cores for cpu in c.logical_cpus}
        self.cycle_num = 1
        self.current_core: PhysicalCore | None = None
        self.ht_label = ""
        self.core_start_time = 0.0

    def __enter__(self) -> "Presenter":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def start(self) -> None:
        """Acquires the output device; the base presenter has none."""

    def close(self) -> None:
        """Releases the output device; must be idempotent."""

    def attach_stats(self, stats: dict[int, CoreStats]) -> None:
        """Binds the statistics owned by the orchestrator for display."""
        self.stats = stats

    def _emit(self, text: str, tone: Tone = Tone.DEFAULT) -> None:
        if self.logger:
            self.logger.write(text)

    def _event(self, tag: str, text: str, tone: Tone) -> None:
        self._emit(f"[{get_iso_timestamp()}] [{tag}] {text}", tone)

    # Runner events (TestEventListener)

    def on_output_line(self, line: str) -> None:
        self._emit(line)

    def on_test_verified(self, step_name: str, completed_iterations: int) -> None:
        self._event("VERIFIED", f"Self-test {step_name} passed! (Iterations: {completed_iterations})", Tone.PASS)

    def on_stretching_detected(self, sample: TelemetrySample) -> None:
        drop_pct = sample.stretch_mhz / sample.target_mhz * 100.0 if sample.target_mhz else 0.0
        self._event(
            "STRETCH",
            f"Tgt {sample.target_mhz:.0f} vs Eff {sample.effective_mhz:.0f} "
            f"(-{sample.stretch_mhz:.0f} MHz / -{drop_pct:.1f}%)",
            Tone.WARN,
        )

    def on_hardware_error(self, event: MceEvent) -> None:
        if event.cpu is None:
            where = "CPU ?"
        else:
            core = self.cpu_to_core.get(event.cpu)
            where = f"CPU {event.cpu}" + (f" (Core {core.core_idx})" if core else "")
            if event.on_tested_cpu:
                where += " [TESTED CORE]"
        self._event("MCE", f"!!! HARDWARE ERROR {where}: {event.message}", Tone.FAIL)

    # Orchestrator events

    def on_notice(self, text: str) -> None:
        self._emit(f"[!] {text}", Tone.WARN)

    def on_cycle_start(self, cycle_num: int, total_cycles: int, runner_name: str) -> None:
        self.cycle_num = cycle_num
        total = f" of {total_cycles}" if total_cycles > 0 else ""
        self._emit(f"▶ Starting Cycle {cycle_num}{total} [{runner_name}]", Tone.HEADER)

    def on_core_start(self, cycle_num: int, core: PhysicalCore, ht_label: str) -> None:
        self.current_core = core
        self.ht_label = ht_label
        self.core_start_time = time.monotonic()
        self._emit(
            f"┌── [{get_iso_timestamp()}] [Cycle {cycle_num}] Testing Core {core.core_idx} ({ccd_label(core)}) - {ht_label}",
            Tone.ACTIVE,
        )

    def on_core_skip(self, cycle_num: int, core: PhysicalCore, reason: str) -> None:
        self._emit(
            f"├── [{get_iso_timestamp()}] [Cycle {cycle_num}] Skipping Core {core.core_idx} ({ccd_label(core)}) - {reason}",
            Tone.WARN,
        )

    def on_core_result(self, core: PhysicalCore, result: RunResult) -> None:
        self.current_core = None
        prefix = f"└── [{get_iso_timestamp()}] Core {core.core_idx}"
        if result.status == RunStatus.INTERRUPTED:
            self._emit(f"{prefix} INTERRUPTED (Cancelled by user)", Tone.WARN)
        elif result.passed:
            self._emit(
                f"{prefix} PASS ({result.completed_iterations} iterations, {result.elapsed_seconds:.1f}s)", Tone.PASS
            )
        else:
            self._emit(f"{prefix} FAIL ({result.status}): {result.error_message}", Tone.FAIL)

    def on_session_end(self, cores: list[PhysicalCore], stats: dict[int, CoreStats]) -> None:
        """Releases the output device and prints the summary table to stdout and the log file."""
        self.close()
        co_offsets = self.smu_monitor.co_offsets_by_core() if self.smu_monitor and self.smu_monitor.is_available() else {}
        colored = sys.stdout.isatty()
        for line in [""] + render_summary(cores, stats, co_offsets) + [""]:
            if self.logger:
                self.logger.write(line)
            print(line if colored else strip_ansi(line))


class ConsolePresenter(Presenter):
    """Line-oriented presenter for non-interactive output (pipes, redirected stdout)."""

    def _emit(self, text: str, tone: Tone = Tone.DEFAULT) -> None:
        super()._emit(text, tone)
        if sys.stdout.isatty() and ANSI_TONES[tone]:
            text = f"{ANSI_TONES[tone]}{text}{RESET}"
        print(text, flush=True)
