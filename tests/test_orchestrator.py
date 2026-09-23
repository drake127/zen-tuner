"""
Unit tests for ZenTunerOrchestrator: cycle iterations, SMT strategies, engine cycling, stats and signal handling.
"""

import signal

import pytest

from lib.models import MceEvent, RunResult, RunStatus, TelemetrySample, TelemetrySummary, TestRequest
from lib.orchestrator import ZenTunerOrchestrator
from lib.presenter import Presenter
from runners.base import Engine, RunContext, StressRunner


class MockRunner(StressRunner):
    """Runner returning scripted results and recording requests."""

    name = "mock"

    def __init__(self, results=None, on_run=None):
        super().__init__()
        self.results = results or {}
        self.on_run = on_run
        self.requests: list[TestRequest] = []
        self.contexts: list[RunContext] = []

    def is_available(self) -> bool:
        return True

    @classmethod
    def add_cli_arguments(cls, parser) -> None:
        pass

    @classmethod
    def parse_parameters(cls, args):
        return None, "mock"

    def _prepare(self, request, work_dir):
        raise NotImplementedError

    def _create_parser(self, request, work_dir, listener):
        raise NotImplementedError

    def run_test(self, request: TestRequest, context: RunContext | None = None) -> RunResult:
        self.requests.append(request)
        self.contexts.append(context)
        if self.on_run:
            self.on_run(request, context)
        result = self.results.get(tuple(request.cpus), result_of(RunStatus.PASS))
        return result() if callable(result) else result


def result_of(status: RunStatus, **kwargs) -> RunResult:
    return RunResult(status=status, tested_cpus=[], completed_iterations=1 if status == RunStatus.PASS else 0,
                     elapsed_seconds=5.0, **kwargs)


def telemetry(stretch_mhz: float) -> TelemetrySummary:
    return TelemetrySummary.from_samples(
        [TelemetrySample(target_mhz=4850.0, effective_mhz=4850.0 - stretch_mhz, voltage_v=1.3, power_w=15.0,
                         temp_c=70.0)]
    )


class RecordingPresenter(Presenter):

    def __init__(self, cores):
        super().__init__(cores)
        self.events: list[tuple] = []

    def on_cycle_start(self, cycle_num, total_cycles, runner_name):
        super().on_cycle_start(cycle_num, total_cycles, runner_name)
        self.events.append(("cycle", cycle_num, runner_name))

    def on_core_skip(self, cycle_num, core, reason):
        self.events.append(("skip", cycle_num, core.core_idx))

    def on_notice(self, text):
        self.events.append(("notice", text))

    def on_session_end(self, cores, stats):
        self.events.append(("end",))


def orchestrate(cores, runner=None, engines=None, **run_kwargs):
    engines = engines or [Engine(runner or MockRunner(), None, "mock")]
    presenter = RecordingPresenter(cores)
    orch = ZenTunerOrchestrator(engines, cores, presenter=presenter)
    stats = orch.run(selected_cores=run_kwargs.pop("selected", cores), **run_kwargs)
    return orch, stats, presenter


@pytest.mark.parametrize(
    "mode, expected",
    [("on", [[0, 12], [1, 13]]), ("off", [[0], [1]])],
)
def test_smt_modes(two_cores, mode, expected):
    runner = MockRunner()
    _, stats, _ = orchestrate(two_cores, runner, hyperthreading_mode=mode, cycles=1)
    assert [r.cpus for r in runner.requests] == expected
    assert [stats[i].passes for i in (0, 1)] == [1, 1]


def test_smt_cycle_phases(two_cores):
    runner = MockRunner()
    orchestrate(two_cores, runner, selected=[two_cores[0]], hyperthreading_mode="cycle", cycles=4)
    # T0 -> T1 -> both -> wrap to T0
    assert [r.cpus for r in runner.requests] == [[0], [12], [0, 12], [0]]


def test_request_carries_core_and_iterations(two_cores):
    runner = MockRunner()
    orchestrate(two_cores, runner, target_iterations=3, cycles=1)
    assert [(r.core_idx, r.target_iterations) for r in runner.requests] == [(0, 3), (1, 3)]


def test_invalid_arguments(two_cores):
    orch = ZenTunerOrchestrator([Engine(MockRunner(), None, "mock")], two_cores)
    with pytest.raises(ValueError):
        orch.run(two_cores, hyperthreading_mode="rr")
    with pytest.raises(ValueError):
        orch.run(two_cores, target_iterations=0)
    with pytest.raises(ValueError):
        ZenTunerOrchestrator([], two_cores)


def test_stops_on_error(two_cores):
    runner = MockRunner({(0, 12): result_of(RunStatus.ERROR, errors=["Fatal rounding error"])})
    _, stats, presenter = orchestrate(two_cores, runner, continue_on_error=False, cycles=2)
    assert len(runner.requests) == 1
    assert stats[0].failures == 1
    assert stats[0].errors == ["Fatal rounding error"]
    assert (stats[1].passes, stats[1].failures) == (0, 0)
    assert any(e[0] == "notice" and "--stop-on-error" in e[1] for e in presenter.events)


def test_interrupted_run_is_not_a_failure(two_cores):
    runner = MockRunner(on_run=lambda req, ctx: ctx.cancel.set(),
                        results={(0, 12): result_of(RunStatus.INTERRUPTED)})
    _, stats, presenter = orchestrate(two_cores, runner, continue_on_error=False, cycles=3)
    assert len(runner.requests) == 1
    assert all((s.passes, s.failures) == (0, 0) for s in stats.values())
    assert ("notice", "Interrupt received. Tests stopped.") in presenter.events
    assert presenter.events[-1] == ("end",)


def test_failed_core_is_skipped_in_later_cycles(two_cores):
    runner = MockRunner({(0, 12): result_of(RunStatus.CRASH, error_message="Process exited abnormally with code 3")})
    _, stats, presenter = orchestrate(two_cores, runner, cycles=2)
    assert [r.cpus for r in runner.requests] == [[0, 12], [1, 13], [1, 13]]
    assert (stats[0].failures, stats[0].errors) == (1, ["Process exited abnormally with code 3"])
    assert stats[1].passes == 2
    assert ("skip", 2, 0) in presenter.events


def test_stops_when_all_cores_failed(two_cores):
    runner = MockRunner({cpus: result_of(RunStatus.ERROR) for cpus in ((0, 12), (1, 13))})
    _, stats, presenter = orchestrate(two_cores, runner, cycles=3)
    assert len(runner.requests) == 2
    assert [stats[i].failures for i in (0, 1)] == [1, 1]
    assert any(e[0] == "notice" and "All selected cores have failed" in e[1] for e in presenter.events)


def test_engines_alternate_per_cycle(two_cores):
    first, second = MockRunner(), MockRunner()
    second.name = "second"
    engines = [Engine(first, "p1", "first"), Engine(second, "p2", "second")]
    _, stats, presenter = orchestrate(two_cores, engines=engines, selected=[two_cores[0]], cycles=3)
    assert [r.parameters for r in first.requests] == ["p1", "p1"]
    assert [r.parameters for r in second.requests] == ["p2"]
    assert [e[2] for e in presenter.events if e[0] == "cycle"] == ["mock", "second", "mock"]
    assert stats[0].passes == 3


def test_telemetry_and_hardware_errors_accumulate(two_cores):
    mce = MceEvent(cpu=0, message="mce: [Hardware Error]: CPU 0: Machine Check", on_tested_cpu=True)
    results = iter([
        result_of(RunStatus.PASS, telemetry=telemetry(0.0), mce_events=[mce]),
        result_of(RunStatus.PASS, telemetry=telemetry(85.0)),
        result_of(RunStatus.INTERRUPTED, telemetry=telemetry(200.0), mce_events=[mce]),
    ])
    runner = MockRunner({(0, 12): lambda: next(results)})
    orch = ZenTunerOrchestrator([Engine(runner, None, "mock")], two_cores, presenter=RecordingPresenter(two_cores))
    for _ in range(3):
        orch._accumulate_stats(0, runner.run_test(TestRequest(cpus=[0, 12])))

    st = orch.stats[0]
    assert (st.passes, st.failures, st.verified_iterations) == (2, 0, 2)
    # Interrupted runs do not count, but their hardware errors are kept
    assert len(st.runs_telemetry) == 2
    assert st.telemetry.stretch_mhz == 85.0
    assert st.stretching_detected
    assert st.mce_events == [mce, mce]


def test_stats_are_shared_with_presenter(two_cores):
    presenter = Presenter(two_cores)
    orch = ZenTunerOrchestrator([Engine(MockRunner(), None, "mock")], two_cores, presenter=presenter)
    assert presenter.stats is orch.stats


def test_stop_signals(two_cores):
    orch = ZenTunerOrchestrator([Engine(MockRunner(), None, "mock")], two_cores)
    orch._handle_stop_signal(signal.SIGTERM, None)
    assert orch.cancel.is_set()
    with pytest.raises(KeyboardInterrupt):
        orch._handle_stop_signal(signal.SIGINT, None)


def test_signal_handlers_restored(two_cores):
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    seen = {}

    def capture(req, ctx):
        seen.update({sig: signal.getsignal(sig) for sig in before})

    orchestrate(two_cores, MockRunner(on_run=capture), cycles=1)
    assert all(handler != before[sig] for sig, handler in seen.items())
    assert {sig: signal.getsignal(sig) for sig in before} == before
