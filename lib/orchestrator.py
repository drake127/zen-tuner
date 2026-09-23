"""
Zen Tuner multi-core stress testing orchestrator.
Manages cycle iterations, SMT thread strategies, signal handling, and statistics accumulation.
Independent of the underlying stress engine.
"""

from collections.abc import Sequence
import signal
import threading

from lib.models import CoreStats, PhysicalCore, RunResult, RunStatus, TestRequest
from lib.presenter import Presenter
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
    ):
        if not engines:
            raise ValueError("At least one stress engine is required")
        self.engines = list(engines)
        self.all_cores = all_cores
        self.presenter = presenter or Presenter(all_cores)
        self.smu_monitor = smu_monitor
        self.stats: dict[int, CoreStats] = {c.core_idx: CoreStats() for c in all_cores}
        self.presenter.attach_stats(self.stats)
        self.cancel = threading.Event()

    def _handle_stop_signal(self, signum, frame) -> None:
        # Runs in the main thread between arbitrary bytecodes: only flag the request, never take presenter locks.
        # A second signal aborts immediately; the runners kill their child processes while unwinding.
        if self.cancel.is_set():
            raise KeyboardInterrupt
        self.cancel.set()

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

        old_handlers = {sig: signal.signal(sig, self._handle_stop_signal) for sig in STOP_SIGNALS}
        context = RunContext(listener=self.presenter, cancel=self.cancel, smu_monitor=self.smu_monitor)
        try:
            with thread_rt_priority(CONTROLLER_RT_PRIORITY):
                self._run_cycles(selected_cores, target_iterations, hyperthreading_mode, cycles, continue_on_error,
                                 context)
            if self.cancel.is_set():
                self.presenter.on_notice("Interrupt received. Tests stopped.")
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
            self.presenter.on_session_end(selected_cores, self.stats)

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
            self.presenter.on_cycle_start(cycle_num, cycles, engine.runner.name)

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
                result = engine.runner.run_test(request, context)
                self._accumulate_stats(core.core_idx, result)
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

    def _accumulate_stats(self, core_idx: int, result: RunResult) -> None:
        st = self.stats[core_idx]
        st.mce_events.extend(result.mce_events)
        if result.status == RunStatus.INTERRUPTED:
            return

        st.total_duration += result.elapsed_seconds
        st.verified_iterations += result.completed_iterations
        if result.telemetry is not None:
            st.runs_telemetry.append(result.telemetry)

        if result.passed:
            st.passes += 1
        else:
            st.failures += 1
            st.errors.extend(result.errors or [result.error_message or result.status])
