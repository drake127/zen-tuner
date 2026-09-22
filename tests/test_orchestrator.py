"""
Unit tests for ZenTunerOrchestrator, cycle iterations, and SMT strategies.
"""

import unittest
from typing import List, Optional

from lib.models import PhysicalCore, RunResult, TestRequest
from lib.orchestrator import ZenTunerOrchestrator
from runners.base import StressRunner, TestEventListener


class MockRunner(StressRunner):
    """Mock stress runner capturing test requests and returning pre-configured results."""

    def __init__(self, result_to_return: Optional[RunResult] = None):
        self.recorded_requests: List[TestRequest] = []
        self.result = result_to_return or RunResult(
            passed=True,
            status="PASS",
            tested_cpus=[0, 12],
            completed_tests=1,
            elapsed_seconds=5.0,
        )

    @property
    def name(self) -> str:
        return "mock"

    def is_available(self) -> bool:
        return True

    @classmethod
    def add_cli_arguments(cls, parser) -> None:
        pass

    def run_test(self, request: TestRequest, listener: Optional[TestEventListener] = None) -> RunResult:
        self.recorded_requests.append(request)
        return self.result


class TestOrchestrator(unittest.TestCase):

    def setUp(self):
        self.cores = [
            PhysicalCore(core_idx=0, hardware_core_id=0, ccd_id=0, logical_cpus=[0, 12]),
            PhysicalCore(core_idx=1, hardware_core_id=1, ccd_id=0, logical_cpus=[1, 13]),
        ]

    def test_orchestrator_smt_on(self):
        runner = MockRunner()
        orch = ZenTunerOrchestrator(runner=runner, all_cores=self.cores)
        stats = orch.run(
            selected_cores=self.cores,
            duration_per_core=5.0,
            hyperthreading_mode="on",
            cycles=1,
        )

        self.assertEqual(len(runner.recorded_requests), 2)
        self.assertEqual(runner.recorded_requests[0].cpus, [0, 12])
        self.assertEqual(runner.recorded_requests[1].cpus, [1, 13])
        self.assertEqual(stats[0].passes, 1)
        self.assertEqual(stats[1].passes, 1)

    def test_orchestrator_smt_off(self):
        runner = MockRunner()
        orch = ZenTunerOrchestrator(runner=runner, all_cores=self.cores)
        orch.run(
            selected_cores=self.cores,
            duration_per_core=5.0,
            hyperthreading_mode="off",
            cycles=1,
        )

        self.assertEqual(len(runner.recorded_requests), 2)
        self.assertEqual(runner.recorded_requests[0].cpus, [0])
        self.assertEqual(runner.recorded_requests[1].cpus, [1])

    def test_orchestrator_smt_cycle(self):
        runner = MockRunner()
        orch = ZenTunerOrchestrator(runner=runner, all_cores=self.cores)
        orch.run(
            selected_cores=[self.cores[0]],
            duration_per_core=5.0,
            hyperthreading_mode="cycle",
            cycles=4,
        )

        self.assertEqual(len(runner.recorded_requests), 4)
        # Cycle 1 (Phase 1/3): Thread 0 (CPU 0)
        self.assertEqual(runner.recorded_requests[0].cpus, [0])
        # Cycle 2 (Phase 2/3): Thread 1 (CPU 12)
        self.assertEqual(runner.recorded_requests[1].cpus, [12])
        # Cycle 3 (Phase 3/3): Both threads (CPUs [0, 12])
        self.assertEqual(runner.recorded_requests[2].cpus, [0, 12])
        # Cycle 4 (Phase 1/3 wrap): Thread 0 (CPU 0)
        self.assertEqual(runner.recorded_requests[3].cpus, [0])

    def test_orchestrator_smt_rr(self):
        runner = MockRunner()
        orch = ZenTunerOrchestrator(runner=runner, all_cores=self.cores)
        orch.run(
            selected_cores=[self.cores[0]],
            duration_per_core=5.0,
            hyperthreading_mode="rr",
            cycles=3,
        )

        self.assertEqual(len(runner.recorded_requests), 3)
        self.assertEqual(runner.recorded_requests[0].cpus, [0])
        self.assertEqual(runner.recorded_requests[1].cpus, [12])
        self.assertEqual(runner.recorded_requests[2].cpus, [0, 12])

    def test_orchestrator_stops_on_error(self):
        fail_res = RunResult(
            passed=False,
            status="ACTIVE_ERROR",
            tested_cpus=[0, 12],
            completed_tests=0,
            elapsed_seconds=2.0,
            active_errors=["Fatal rounding error"],
        )
        runner = MockRunner(result_to_return=fail_res)
        orch = ZenTunerOrchestrator(runner=runner, all_cores=self.cores)
        stats = orch.run(
            selected_cores=self.cores,
            duration_per_core=5.0,
            continue_on_error=False,
            cycles=2,
        )

        # Should halt after Core 0 failed
        self.assertEqual(len(runner.recorded_requests), 1)
        self.assertEqual(stats[0].failures, 1)
        self.assertEqual(stats[1].passes, 0)
        self.assertEqual(stats[1].failures, 0)

    def test_orchestrator_ctrl_c_does_not_fail(self):
        interrupted_res = RunResult(
            passed=False,
            status="INTERRUPTED",
            tested_cpus=[0, 12],
            completed_tests=0,
            elapsed_seconds=1.5,
            error_message="Test interrupted by user",
        )
        runner = MockRunner(result_to_return=interrupted_res)
        orch = ZenTunerOrchestrator(runner=runner, all_cores=self.cores)
        stats = orch.run(
            selected_cores=self.cores,
            duration_per_core=5.0,
            continue_on_error=False,
            cycles=1,
        )

        # Interrupted test must NOT count as a failure
        self.assertEqual(stats[0].failures, 0)
        self.assertEqual(stats[0].passes, 0)
        self.assertEqual(stats[1].failures, 0)
        self.assertEqual(stats[1].passes, 0)
        total_fails = sum(s.failures for s in stats.values())
        self.assertEqual(total_fails, 0)

    def test_orchestrator_shares_stats_with_presenter(self):
        from unittest.mock import MagicMock
        runner = MockRunner()
        presenter = MagicMock()
        presenter.stats = {}
        orch = ZenTunerOrchestrator(runner=runner, all_cores=self.cores, presenter=presenter)
        self.assertIs(presenter.stats, orch.stats)

    def test_orchestrator_skips_failed_core_in_subsequent_cycles(self):
        from unittest.mock import MagicMock

        fail_res = RunResult(
            passed=False,
            status="ACTIVE_ERROR",
            tested_cpus=[0, 12],
            completed_tests=0,
            elapsed_seconds=2.0,
            active_errors=["Fatal error"],
        )
        pass_res = RunResult(
            passed=True,
            status="PASS",
            tested_cpus=[1, 13],
            completed_tests=2,
            elapsed_seconds=5.0,
        )

        class SequenceRunner(StressRunner):
            def __init__(self):
                self.calls = []

            @property
            def name(self):
                return "seq"

            def is_available(self):
                return True

            @classmethod
            def add_cli_arguments(cls, parser):
                pass

            def run_test(self, req, listener=None):
                self.calls.append(req.cpus)
                if req.cpus == [0, 12]:
                    return fail_res
                return pass_res

        runner = SequenceRunner()
        presenter = MagicMock()
        orch = ZenTunerOrchestrator(runner=runner, all_cores=self.cores, presenter=presenter)
        stats = orch.run(
            selected_cores=self.cores,
            duration_per_core=5.0,
            continue_on_error=True,
            cycles=2,
        )

        # Cycle 1 ran core 0 (fail) and core 1 (pass). Cycle 2 ran only core 1 (pass).
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(runner.calls[0], [0, 12])
        self.assertEqual(runner.calls[1], [1, 13])
        self.assertEqual(runner.calls[2], [1, 13])

        self.assertEqual(stats[0].failures, 1)
        self.assertEqual(stats[0].passes, 0)
        self.assertEqual(stats[1].failures, 0)
        self.assertEqual(stats[1].passes, 2)

        presenter.print_core_skip.assert_called_once()
        skip_call = presenter.print_core_skip.call_args
        self.assertEqual(skip_call[0][0], 2)  # cycle_num = 2
        self.assertEqual(skip_call[0][1], self.cores[0])  # core 0

    def test_orchestrator_stops_when_all_cores_failed(self):
        fail_res = RunResult(
            passed=False,
            status="ACTIVE_ERROR",
            tested_cpus=[0, 12],
            completed_tests=0,
            elapsed_seconds=2.0,
            active_errors=["Fatal error"],
        )
        runner = MockRunner(result_to_return=fail_res)
        orch = ZenTunerOrchestrator(runner=runner, all_cores=self.cores)
        stats = orch.run(
            selected_cores=self.cores,
            duration_per_core=5.0,
            continue_on_error=True,
            cycles=3,
        )

        # Both cores failed in Cycle 1. Cycles 2 and 3 should not run.
        self.assertEqual(len(runner.recorded_requests), 2)
        self.assertEqual(stats[0].failures, 1)
        self.assertEqual(stats[1].failures, 1)


if __name__ == "__main__":
    unittest.main()


