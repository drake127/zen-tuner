"""
Unit tests for ZenTunerOrchestrator: cycle iterations, SMT strategies, engine cycling, stats, hardware error
attribution and signal handling.
"""

import os
import signal
import time

import pytest

from conftest import CapturingPresenter, make_result, make_summary
from lib.models import MceEvent, RunResult, RunStatus, TestRequest
from lib.orchestrator import ZenTunerOrchestrator
from lib.presenter import Presenter
from runners.base import Engine, RunContext, StressRunner


class MockRunner(StressRunner):
    """Runner returning scripted results and recording requests."""

    name = "mock"
    bundled_binary = "none"
    binary_name = "none"

    def __init__(self, results=None, on_run=None, name="mock"):
        super().__init__()
        self.name = name
        self.results = results or {}
        self.on_run = on_run
        self.requests: list[TestRequest] = []

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
        if self.on_run:
            self.on_run(request, context)
        result = self.results.get(tuple(request.cpus), make_result(RunStatus.PASS))
        return result() if callable(result) else result


def orchestrate(cores, runner=None, engines=None, kmsg_path="/nonexistent/kmsg", **run_kwargs):
    engines = engines or [Engine(runner or MockRunner(), None, "mock")]
    presenter = CapturingPresenter(cores)
    orch = ZenTunerOrchestrator(engines, cores, presenter=presenter, kmsg_path=kmsg_path)
    stats = orch.run(selected_cores=run_kwargs.pop("selected", cores), **run_kwargs)
    return orch, stats, presenter


@pytest.mark.parametrize("mode, expected", [("on", [[0, 12], [1, 13]]), ("off", [[0], [1]])])
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


def test_session_info_and_requests(two_cores):
    runner = MockRunner()
    _, _, presenter = orchestrate(two_cores, runner, target_iterations=3, cycles=2, hyperthreading_mode="off")
    session = presenter.session
    assert (session.profile, session.target_iterations, session.total_cycles, session.hyperthreading_mode) == \
        ("mock", 3, 2, "off")
    assert [(r.core_idx, r.target_iterations) for r in runner.requests][:2] == [(0, 3), (1, 3)]


def test_invalid_arguments(two_cores):
    orch = ZenTunerOrchestrator([Engine(MockRunner(), None, "mock")], two_cores)
    with pytest.raises(ValueError):
        orch.run(two_cores, hyperthreading_mode="rr")
    with pytest.raises(ValueError):
        orch.run(two_cores, target_iterations=0)
    with pytest.raises(ValueError):
        ZenTunerOrchestrator([], two_cores)


def test_stops_on_error(two_cores):
    failure = make_result(RunStatus.ERROR, error_message="FATAL ERROR: Rounding")
    runner = MockRunner({(0, 12): failure})
    _, stats, presenter = orchestrate(two_cores, runner, continue_on_error=False, cycles=2)
    assert len(runner.requests) == 1
    assert stats[0].failures == 1
    run = stats[0].failed_runs[0]
    assert (run.cycle_num, run.runner_name, run.result) == (1, "mock", failure)
    assert (stats[1].passes, stats[1].failures) == (0, 0)
    assert "[!] Stopping cycle run due to error (--stop-on-error)." in presenter.texts()


def test_interrupted_run_is_not_a_failure(two_cores):
    runner = MockRunner(on_run=lambda req, ctx: ctx.cancel.set(), results={(0, 12): make_result(RunStatus.INTERRUPTED)})
    _, stats, presenter = orchestrate(two_cores, runner, continue_on_error=False, cycles=3)
    assert len(runner.requests) == 1
    assert all((s.passes, s.failures) == (0, 0) for s in stats.values())
    assert presenter.texts()[-1] == "[!] Interrupt received. Tests stopped."
    assert presenter.session_end is not None


def test_failed_core_is_skipped_in_later_cycles(two_cores):
    runner = MockRunner({(0, 12): make_result(RunStatus.CRASH)})
    _, stats, presenter = orchestrate(two_cores, runner, cycles=2)
    assert [r.cpus for r in runner.requests] == [[0, 12], [1, 13], [1, 13]]
    assert stats[0].failures == 1
    assert stats[1].passes == 2
    assert any("[Cycle 2] Skipping Core 0" in text for text in presenter.texts())


def test_stops_when_all_cores_failed(two_cores):
    runner = MockRunner({cpus: make_result(RunStatus.ERROR) for cpus in ((0, 12), (1, 13))})
    _, stats, presenter = orchestrate(two_cores, runner, cycles=3)
    assert len(runner.requests) == 2
    assert [stats[i].failures for i in (0, 1)] == [1, 1]
    assert "[!] All selected cores have failed. Stopping cycle runs." in presenter.texts()


def test_engines_alternate_per_cycle(two_cores):
    first, second = MockRunner(), MockRunner(name="second")
    engines = [Engine(first, "p1", "first"), Engine(second, "p2", "second")]
    _, stats, presenter = orchestrate(two_cores, engines=engines, selected=[two_cores[0]], cycles=3)
    assert [r.parameters for r in first.requests] == ["p1", "p1"]
    assert [r.parameters for r in second.requests] == ["p2"]
    assert [t for t in presenter.texts() if t.startswith("▶")] == [
        "▶ Starting Cycle 1 of 3 [mock]", "▶ Starting Cycle 2 of 3 [second]", "▶ Starting Cycle 3 of 3 [mock]",
    ]
    assert presenter.session.profile == "first / second"
    assert stats[0].passes == 3


def test_telemetry_accumulates(two_cores):
    results = iter([
        make_result(RunStatus.PASS, telemetry=make_summary(4850.0, 4850.0)),
        make_result(RunStatus.PASS, telemetry=make_summary(4850.0, 4765.0)),
        make_result(RunStatus.INTERRUPTED, telemetry=make_summary(4850.0, 4650.0)),
    ])
    runner = MockRunner(results={(0, 12): lambda: next(results)})
    orch = ZenTunerOrchestrator([Engine(runner, None, "mock")], two_cores)
    for cycle in (1, 2, 3):
        orch._accumulate_stats(0, runner.run_test(TestRequest(cpus=[0, 12])), cycle, "mock")

    st = orch.stats[0]
    assert (st.passes, st.failures, st.verified_iterations) == (2, 0, 2)
    # Interrupted runs do not count
    assert len(st.runs_telemetry) == 2
    assert st.telemetry.stretch_mhz == 85.0
    assert st.stretching_detected


def test_hardware_errors_are_attributed_by_timestamp(two_cores, monkeypatch):
    orch = ZenTunerOrchestrator([Engine(MockRunner(), None, "mock")], two_cores, presenter=CapturingPresenter(two_cores))
    clock = iter([100.0, 200.0])
    monkeypatch.setattr("lib.orchestrator.kernel_clock", lambda: next(clock))
    orch._record_run_start(0)
    orch._record_run_start(1)

    orch._on_kernel_event(MceEvent(cpu=None, message="early", timestamp_s=50.0))
    orch._on_kernel_event(MceEvent(cpu=13, message="during core 0", timestamp_s=150.0))
    # Logged after core 1 started, e.g. a delayed report: blamed on the latest run
    orch._on_kernel_event(MceEvent(cpu=0, message="late", timestamp_s=250.0))

    assert [(e.cpu, e.core_idx, e.tested_core_idx) for e in orch.mce_events] == [
        (None, None, None), (13, 1, 0), (0, 0, 1),
    ]
    assert sum("HARDWARE ERROR" in text for text in orch.presenter.texts()) == 3


def test_hardware_errors_from_kernel_log(two_cores, tmp_path):
    fifo = tmp_path / "kmsg"
    os.mkfifo(fifo)
    runner = MockRunner()
    orch = ZenTunerOrchestrator([Engine(runner, None, "mock")], two_cores, presenter=CapturingPresenter(two_cores),
                                kmsg_path=str(fifo))

    def log_hardware_error(req, ctx):
        fifo.write_text("0,1,1,-;mce: [Hardware Error]: CPU 1: Machine Check: 0 Bank 5: bea0000000000108\n")
        deadline = time.monotonic() + 5.0
        while not orch.mce_events and time.monotonic() < deadline:
            time.sleep(0.01)

    runner.on_run = log_hardware_error
    orch.run(selected_cores=[two_cores[0]], cycles=1)

    (event,) = orch.presenter.session_end[2]
    assert (event.cpu, event.core_idx) == (1, 1)


def test_unreadable_kernel_log_is_announced(two_cores):
    _, _, presenter = orchestrate(two_cores, cycles=1)
    assert "[!] /nonexistent/kmsg is not readable; hardware errors are not monitored." in presenter.texts()


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
    orchestrate(two_cores, MockRunner(on_run=lambda req, ctx: seen.update({s: signal.getsignal(s) for s in before})),
                cycles=1)
    assert all(handler != before[sig] for sig, handler in seen.items())
    assert {sig: signal.getsignal(sig) for sig in before} == before
