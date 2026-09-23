"""
Zen Tuner multi-core stress testing orchestrator.
Manages cycle iterations, SMT thread strategies, signal handling, hardware error attribution and statistics.
Independent of the underlying stress engine.
"""

import bisect
from collections.abc import Sequence
import dataclasses
import signal
import threading

from lib.models import CoreStats, FailedRun, MceEvent, PhysicalCore, RunResult, RunStatus, TestRequest
from lib.monitors import KernelErrorMonitor, kernel_clock
from lib.presenter import Presenter, SessionInfo
from lib.sched import CONTROLLER_RT_PRIORITY, thread_rt_priority
from lib.smu import RyzenSmuMonitor
from runners.base import Engine, RunContext

HYPERTHREADING_MODES = ("off", "on", "cycle")
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class ZenTunerOrchestrator:
    """Runs stress engines sequentially across physical CPU cores, cycle after cycle."""

    def __init__(
        self,
        engines: Sequence[Engine],
        all_cores: list[PhysicalCore],
        presenter: Presenter | None = None,
        smu_monitor: RyzenSmuMonitor | None = None,
        kmsg_path: str | None = None,
    ):
        if not engines:
            raise ValueError("At least one stress engine is required")
        self.engines = list(engines)
        self.all_cores = all_cores
        self.presenter = presenter or Presenter(all_cores)
        self.smu_monitor = smu_monitor
        self.kmsg_path = kmsg_path
        self.stats: dict[int, CoreStats] = {c.core_idx: CoreStats() for c in all_cores}
        self.presenter.attach_stats(self.stats)
        self.mce_events: list[MceEvent] = []
        self.cancel = threading.Event()
        self._cpu_to_core = {cpu: c.core_idx for c in all_cores for cpu in c.logical_cpus}
        # Start times (kernel_clock) and cores of all runs in order; shared with the kernel monitor thread.
        self._run_starts: list[float] = []
        self._run_cores: list[int] = []
        self._timeline_lock = threading.Lock()

    def _handle_stop_signal(self, signum, frame) -> None:
        # Runs in the main thread between arbitrary bytecodes: only flag the request, never take presenter locks.
        # A second signal aborts immediately; the runners kill their child processes while unwinding.
        if self.cancel.is_set():
            raise KeyboardInterrupt
        self.cancel.set()

    def _record_run_start(self, core_idx: int) -> None:
        with self._timeline_lock:
            self._run_starts.append(kernel_clock())
            self._run_cores.append(core_idx)

    def _on_kernel_event(self, event: MceEvent) -> None:
        """
        Attributes a kernel hardware error by its own timestamp and CPU (called from the monitor thread).
        Errors logged between runs are attributed to the run that started last, since the kernel may report
        them with a delay after the error occurred.
        """
        timestamp = event.timestamp_s if event.timestamp_s is not None else kernel_clock()
        with self._timeline_lock:
            pos = bisect.bisect_right(self._run_starts, timestamp)
            tested = self._run_cores[pos - 1] if pos else None
            event = dataclasses.replace(
                event,
                core_idx=self._cpu_to_core.get(event.cpu) if event.cpu is not None else None,
                tested_core_idx=tested,
            )
            self.mce_events.append(event)
        self.presenter.on_hardware_error(event)

    def run(
        self,
        selected_cores: list[PhysicalCore],
        target_iterations: int = 1,
        hyperthreading_mode: str = "on",
        cycles: int = 1,
        continue_on_error: bool = True,
    ) -> dict[int, CoreStats]:
        """
        Executes stress testing across selected cores for the configured number of cycles (0 = until stopped).
        Engines alternate per cycle when more than one is configured. Returns stats keyed by core index.
        """
        if hyperthreading_mode not in HYPERTHREADING_MODES:
            raise ValueError(f"Unknown hyperthreading mode '{hyperthreading_mode}'")
        if target_iterations < 1:
            raise ValueError(f"Target iterations must be at least 1: {target_iterations}")

        self.presenter.on_session_start(SessionInfo(
            profile=" / ".join(e.profile for e in self.engines),
            hyperthreading_mode=hyperthreading_mode,
            target_iterations=target_iterations,
            total_cycles=cycles,
        ))
        old_handlers = {sig: signal.signal(sig, self._handle_stop_signal) for sig in STOP_SIGNALS}
        context = RunContext(listener=self.presenter, cancel=self.cancel, smu_monitor=self.smu_monitor)
        try:
            with KernelErrorMonitor(self._on_kernel_event, self.kmsg_path) as kernel_mon, \
                    thread_rt_priority(CONTROLLER_RT_PRIORITY):
                if not kernel_mon.available:
                    self.presenter.on_notice(f"{kernel_mon.kmsg_path} is not readable; hardware errors are not monitored.")
                self._run_cycles(selected_cores, target_iterations, hyperthreading_mode, cycles, continue_on_error,
                                 context)
            if self.cancel.is_set():
                self.presenter.on_notice("Interrupt received. Tests stopped.")
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            with self._timeline_lock:
                mce_events = list(self.mce_events)
            self.presenter.on_session_end(selected_cores, self.stats, mce_events)

        return self.stats

    def _run_cycles(
        self,
        selected_cores: list[PhysicalCore],
        target_iterations: int,
        hyperthreading_mode: str,
        cycles: int,
        continue_on_error: bool,
        context: RunContext,
    ) -> None:
        cycle_num = 1
        while not self.cancel.is_set():
            if all(self.stats[c.core_idx].failures > 0 for c in selected_cores):
                self.presenter.on_notice("All selected cores have failed. Stopping cycle runs.")
                return

            engine = self.engines[(cycle_num - 1) % len(self.engines)]
            self.presenter.on_cycle_start(cycle_num, engine.runner.name)

            for core in selected_cores:
                if self.cancel.is_set():
                    return
                if self.stats[core.core_idx].failures > 0:
                    self.presenter.on_core_skip(cycle_num, core, reason="already failed")
                    continue

                test_cpus, ht_label = self._resolve_test_cpus(core, hyperthreading_mode, cycle_num)
                self.presenter.on_core_start(cycle_num, core, ht_label)
                request = TestRequest(
                    cpus=test_cpus,
                    target_iterations=target_iterations,
                    core_idx=core.core_idx,
                    parameters=engine.parameters,
                )
                self._record_run_start(core.core_idx)
                result = engine.runner.run_test(request, context)
                self._accumulate_stats(core.core_idx, result, cycle_num, engine.runner.name)
                self.presenter.on_core_result(core, result)

                if not result.passed and result.status != RunStatus.INTERRUPTED and not continue_on_error:
                    self.presenter.on_notice("Stopping cycle run due to error (--stop-on-error).")
                    return

            if cycles > 0 and cycle_num >= cycles:
                return
            cycle_num += 1

    @staticmethod
    def _resolve_test_cpus(core: PhysicalCore, mode: str, cycle_num: int) -> tuple[list[int], str]:
        cpus = core.logical_cpus
        if mode == "off" or len(cpus) == 1:
            return [cpus[0]], f"1T (CPU {cpus[0]})"
        if mode == "on":
            return cpus, f"2T (CPUs {cpus[0]}+{cpus[1]})"

        phase = (cycle_num - 1) % 3
        if phase < 2:
            return [cpus[phase]], f"Phase {phase + 1}/3: 1T (CPU {cpus[phase]})"
        return cpus, f"Phase 3/3: 2T (CPUs {cpus[0]}+{cpus[1]})"

    def _accumulate_stats(self, core_idx: int, result: RunResult, cycle_num: int, runner_name: str) -> None:
        if result.status == RunStatus.INTERRUPTED:
            return

        st = self.stats[core_idx]
        st.total_duration += result.elapsed_seconds
        st.verified_iterations += result.completed_iterations
        if result.telemetry is not None:
            st.runs_telemetry.append(result.telemetry)

        if result.passed:
            st.passes += 1
        else:
            st.failed_runs.append(FailedRun(cycle_num=cycle_num, runner_name=runner_name, result=result))
