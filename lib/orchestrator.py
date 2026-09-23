"""
Zen Tuner multi-core stress testing orchestrator.
Manages cycle iterations, SMT thread strategies, signal handling, and telemetry accumulation.
Independent of the underlying stress engine.
"""

import signal
from typing import Any, Protocol

from lib.models import CoreStats, PhysicalCore, RunResult, TestRequest
from runners.base import StressRunner, TestEventListener


class PresenterProtocol(TestEventListener, Protocol):
    stats: dict[int, CoreStats]

    def _write(self, text: str, to_stderr: bool = False) -> None:
        ...

    def print_banner(self, **kwargs) -> None:
        ...

    def print_cycle_start(self, cycle_num: int, total_cycles: int, runner_name: str | None = None) -> None:
        ...

    def print_core_start(self, cycle_num: int, core: PhysicalCore, ht_label: str) -> None:
        ...

    def print_core_result(self, core: PhysicalCore, result: RunResult, idle_core_name: str | None = None) -> None:
        ...

    def print_summary_table(self, all_cores: list[PhysicalCore], stats: dict[int, CoreStats]) -> None:
        ...


class ZenTunerOrchestrator:
    """Orchestrates sequential and round-robin stress testing across physical CPU cores."""

    def __init__(
        self,
        runner: StressRunner | dict[str, StressRunner],
        all_cores: list[PhysicalCore],
        presenter: PresenterProtocol | Any | None = None,
        runner_mode: str = "single",
    ):
        self.runner = runner
        self.all_cores = all_cores
        self.presenter = presenter
        self.runner_mode = runner_mode
        self.cpu_to_core_map = {cpu: core for core in all_cores for cpu in core.logical_cpus}
        self.stats: dict[int, CoreStats] = {c.core_idx: CoreStats() for c in all_cores}
        if self.presenter and hasattr(self.presenter, "stats"):
            self.presenter.stats = self.stats
        self.interrupted = False

    def _sigint_handler(self, signum, frame) -> None:
        if self.interrupted:
            raise KeyboardInterrupt
        self.interrupted = True
        if self.presenter and hasattr(self.presenter, "_write"):
            self.presenter._write("\n[!] Interrupt signal received! Stopping tests and displaying summary...")

    def run(
        self,
        selected_cores: list[PhysicalCore],
        duration_per_core: float | None = None,
        target_tests_per_core: int | None = None,
        hyperthreading_mode: str = "on",
        cycles: int = 1,
        continue_on_error: bool = True,
        graceful: bool = True,
        runner_parameters: dict[str, Any] | None = None,
    ) -> dict[int, CoreStats]:
        """
        Executes stress testing across selected cores for the configured number of cycles.
        Returns accumulated stats mapping core index to CoreStats.
        """
        old_sigint = signal.signal(signal.SIGINT, self._sigint_handler)
        cycle_num = 1

        try:
            while not self.interrupted:
                # Stop if all selected cores have already failed
                active_candidates = [c for c in selected_cores if self.stats[c.core_idx].failures == 0]
                if not active_candidates:
                    if self.presenter and hasattr(self.presenter, "_write"):
                        self.presenter._write("\n[!] All selected cores have failed. Stopping cycle runs.")
                    break

                # Resolve active runner and parameters for this cycle
                if self.runner_mode == "cycle" and isinstance(self.runner, dict):
                    engine_name = "prime95" if (cycle_num % 2 == 1) else "y-cruncher"
                    active_runner = self.runner.get(engine_name, list(self.runner.values())[0])
                    cyc_params = runner_parameters.get(engine_name) if runner_parameters else None
                    params = cyc_params if isinstance(cyc_params, dict) else (runner_parameters or {})
                else:
                    active_runner = self.runner if isinstance(self.runner, StressRunner) else list(self.runner.values())[0]
                    params = runner_parameters or {}

                if self.presenter and hasattr(self.presenter, "print_cycle_start"):
                    try:
                        self.presenter.print_cycle_start(cycle_num, cycles, runner_name=active_runner.name)
                    except TypeError:
                        self.presenter.print_cycle_start(cycle_num, cycles)

                for core in selected_cores:
                    if self.interrupted:
                        break

                    # Skip cores that have already failed in a previous cycle
                    if self.stats[core.core_idx].failures > 0:
                        if self.presenter and hasattr(self.presenter, "print_core_skip"):
                            self.presenter.print_core_skip(cycle_num, core, reason="already failed")
                        continue

                    test_cpus, ht_label = self._resolve_test_cpus(core, hyperthreading_mode, cycle_num)

                    if self.presenter and hasattr(self.presenter, "print_core_start"):
                        self.presenter.print_core_start(cycle_num, core, ht_label)

                    core_params = dict(params)
                    core_params["core_idx"] = core.core_idx
                    if self.presenter and hasattr(self.presenter, "smu_monitor"):
                        core_params["smu_monitor"] = self.presenter.smu_monitor

                    request = TestRequest(
                        cpus=test_cpus,
                        duration_seconds=duration_per_core,
                        target_iterations=target_tests_per_core,
                        graceful=graceful,
                        parameters=core_params,
                    )

                    result: RunResult = active_runner.run_test(request, listener=self.presenter)
                    if self.interrupted or result.status == "INTERRUPTED":
                        result.status = "INTERRUPTED"
                        result.passed = False
                        result.active_errors.clear()

                    self._accumulate_stats(core.core_idx, result)

                    idle_name = None
                    if result.idle_mce_errors:
                        first_mce = result.idle_mce_errors[0]
                        idle_c = self.cpu_to_core_map.get(first_mce.cpu) if first_mce.cpu is not None else None
                        if idle_c:
                            idle_name = f"Core {idle_c.core_idx} (HW ID {idle_c.hardware_core_id})"
                        else:
                            idle_name = f"CPU {first_mce.cpu}"

                    if self.presenter and hasattr(self.presenter, "print_core_result"):
                        self.presenter.print_core_result(core, result, idle_core_name=idle_name)

                    if self.interrupted:
                        break

                    if not result.passed and not continue_on_error:
                        if self.presenter and hasattr(self.presenter, "_write"):
                            self.presenter._write("\n[!] Stopping cycle run due to error (stopped by --stop-on-error).")
                        self.interrupted = True
                        break

                if cycles > 0 and cycle_num >= cycles:
                    break
                cycle_num += 1

        finally:
            signal.signal(signal.SIGINT, old_sigint)
            if self.presenter and hasattr(self.presenter, "print_summary_table"):
                self.presenter.print_summary_table(selected_cores, self.stats)

        return self.stats

    @staticmethod
    def _resolve_test_cpus(core: PhysicalCore, mode: str, cycle_num: int) -> tuple[list[int], str]:
        if mode == "off":
            cpus = [core.logical_cpus[0]]
            label = f"1T (CPU {cpus[0]})"
        elif mode == "on":
            cpus = core.logical_cpus
            label = f"2T (CPUs {cpus})"
        elif mode in ("cycle", "rr"):
            if len(core.logical_cpus) > 1:
                phase = (cycle_num - 1) % 3
                if phase == 0:
                    cpus = [core.logical_cpus[0]]
                    label = f"Phase 1/3: 1T (CPU {cpus[0]})"
                elif phase == 1:
                    cpus = [core.logical_cpus[1]]
                    label = f"Phase 2/3: 1T (CPU {core.logical_cpus[1]})"
                else:
                    cpus = core.logical_cpus
                    label = f"Phase 3/3: 2T (CPUs {cpus[0]}+{cpus[1]})"
            else:
                cpus = core.logical_cpus
                label = f"1T (CPU {cpus[0]})"
        else:
            cpus = core.logical_cpus
            label = str(cpus)
        return cpus, label

    def _accumulate_stats(self, core_idx: int, result: RunResult) -> None:
        if result.status == "INTERRUPTED":
            return

        st = self.stats[core_idx]
        st.total_duration += result.elapsed_seconds
        st.verified_tests += result.completed_tests
        if result.avg_target_mhz:
            st.avg_target_mhz = result.avg_target_mhz
        if result.avg_effective_mhz:
            st.avg_effective_mhz = result.avg_effective_mhz
        if result.max_stretch_mhz > st.max_stretch_mhz:
            st.max_stretch_mhz = result.max_stretch_mhz
        if result.stretching_detected:
            st.stretching_detected = True

        # Running average and maximum median stretch across cycles
        calc_median = result.median_stretch_mhz if result.median_stretch_mhz > 0 else result.avg_stretch_mhz
        prev_cycles = st.passes + st.failures
        if prev_cycles == 0:
            st.avg_stretch_mhz = result.avg_stretch_mhz
            st.median_stretch_mhz = calc_median
        else:
            st.avg_stretch_mhz = (st.avg_stretch_mhz * prev_cycles + result.avg_stretch_mhz) / (prev_cycles + 1)
            st.median_stretch_mhz = max(st.median_stretch_mhz, calc_median)

        if st.median_stretch_mhz <= 0 and st.avg_target_mhz and st.avg_effective_mhz:
            diff = st.avg_target_mhz - st.avg_effective_mhz
            if diff >= 50.0:
                st.median_stretch_mhz = diff

        if st.median_stretch_mhz >= 50.0:
            st.stretching_detected = True

        if result.passed:
            st.passes += 1
        else:
            st.failures += 1
            if result.status in ("ACTIVE_ERROR", "PROCESS_CRASH"):
                st.active_errors.extend(result.active_errors)
            if result.idle_mce_errors:
                for ev in result.idle_mce_errors:
                    st.idle_mce_errors.append(ev.message)

