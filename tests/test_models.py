"""
Unit tests for data models and immutability guarantees.
"""

from dataclasses import FrozenInstanceError
import unittest
from lib.models import CoreStats, MceEvent, PhysicalCore, RunResult, StretchSample, TestRequest


class TestModels(unittest.TestCase):

    def test_physical_core_frozen(self):
        core = PhysicalCore(core_idx=0, hardware_core_id=0, ccd_id=0, logical_cpus=[0, 12])
        with self.assertRaises(FrozenInstanceError):
            core.core_idx = 1

    def test_mce_event_frozen(self):
        event = MceEvent(cpu=0, message="Hardware Error", is_active_core=True)
        with self.assertRaises(FrozenInstanceError):
            event.cpu = 1

    def test_stretch_sample_frozen(self):
        sample = StretchSample(cpu=0, target_mhz=4800.0, effective_mhz=4400.0, stretch_mhz=400.0, stretch_pct=8.3)
        with self.assertRaises(FrozenInstanceError):
            sample.cpu = 1

    def test_core_stats_defaults(self):
        stats = CoreStats()
        self.assertEqual(stats.passes, 0)
        self.assertEqual(stats.failures, 0)
        self.assertFalse(stats.stretching_detected)
        self.assertEqual(stats.median_stretch_mhz, 0.0)

    def test_run_result_attributes(self):
        res = RunResult(
            passed=True,
            status="PASS",
            tested_cpus=[0, 12],
            completed_tests=1,
            elapsed_seconds=10.5,
        )
        self.assertTrue(res.passed)
        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.completed_tests, 1)
        self.assertEqual(res.median_stretch_mhz, 0.0)

    def test_test_request(self):
        req = TestRequest(cpus=[0, 12], duration_seconds=60.0)
        self.assertEqual(req.cpus, [0, 12])
        self.assertEqual(req.duration_seconds, 60.0)
        self.assertTrue(req.graceful)


if __name__ == "__main__":
    unittest.main()

