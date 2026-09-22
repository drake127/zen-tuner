"""
Unit tests for runner engines, parameter builders, and listener protocols.
"""

import unittest
from zen_tuner import build_fft_config
from runners import Prime95Runner, get_runner, list_runners, parse_time
from runners.base import TestEventListener
from runners.prime95 import strip_worker_prefix, PASSED_PATTERN


class DummyListener(TestEventListener):

    def __init__(self):
        self.lines = []
        self.verified = []

    def on_output_line(self, line: str) -> None:
        self.lines.append(line)

    def on_test_verified(self, iteration_name: str, completed_count: int) -> None:
        self.verified.append((iteration_name, completed_count))

    def on_stretching_detected(self, sample) -> None:
        pass

    def on_hardware_error(self, event) -> None:
        pass

    def on_grace_period_started(self, max_duration_s: float) -> None:
        pass


class TestRunners(unittest.TestCase):

    def test_parse_time(self):
        self.assertEqual(parse_time("300"), 300.0)
        self.assertEqual(parse_time("45s"), 45.0)
        self.assertEqual(parse_time("5m"), 300.0)
        self.assertEqual(parse_time("1h"), 3600.0)

    def test_build_fft_config(self):
        smallest = build_fft_config("smallest")
        self.assertEqual(smallest.min_fft, 4)
        self.assertEqual(smallest.max_fft, 4)

        custom = build_fft_config("smallest", min_fft=128, max_fft=256, memory=1024)
        self.assertEqual(custom.min_fft, 128)
        self.assertEqual(custom.max_fft, 256)
        self.assertEqual(custom.mem_mb, 1024)

    def test_strip_worker_prefix(self):
        self.assertEqual(strip_worker_prefix("[Worker 2026-09-22T16:30:00] Test 1"), "[2026-09-22T16:30:00] Test 1")
        self.assertEqual(
            strip_worker_prefix("[Worker #1 2026-09-22T16:30:00] Self-test 4K passed!"),
            "[2026-09-22T16:30:00] Self-test 4K passed!",
        )
        self.assertEqual(strip_worker_prefix("[Main thread 2026-09-22T16:30:00] Starting"), "[2026-09-22T16:30:00] Starting")
        self.assertEqual(strip_worker_prefix("[2026-09-22T16:30:00] Clean line"), "[2026-09-22T16:30:00] Clean line")

    def test_runner_registry(self):
        self.assertIn("prime95", list_runners())
        runner = get_runner("prime95")
        self.assertEqual(runner.name, "prime95")

        with self.assertRaises(ValueError):
            get_runner("non_existent_runner")

    def test_prime95_resolve_binary(self):
        runner = Prime95Runner()
        self.assertIsNotNone(runner.mprime_bin)
        self.assertTrue(runner.mprime_bin.endswith("contrib/prime95/mprime") or runner.mprime_bin.endswith("mprime"))
        self.assertTrue(runner.is_available())

    def test_listener_events(self):
        listener = DummyListener()
        listener.on_output_line("Test line 1")
        listener.on_test_verified("4K", 1)

        self.assertEqual(listener.lines, ["Test line 1"])
        self.assertEqual(listener.verified, [("4K", 1)])

    def test_cli_mutually_exclusive_time_and_tests(self):
        import subprocess
        import sys

        res = subprocess.run(
            [sys.executable, "zen_tuner.py", "--time", "60s", "--tests", "2"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("not allowed with argument", res.stderr)

    def test_cli_stop_on_error_flag(self):
        import subprocess
        import sys

        res = subprocess.run(
            [sys.executable, "zen_tuner.py", "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--stop-on-error", res.stdout)

    def test_passed_pattern_single_and_multithread(self):
        # Single-threaded
        m_single = PASSED_PATTERN.search("Self-test 4K passed!")
        self.assertIsNotNone(m_single)
        self.assertEqual(m_single.group(1), "4K")
        self.assertIsNone(m_single.group(2))
        self.assertIsNone(m_single.group(3))

        # Multi-threaded format from Prime95
        line_t1 = "[2026-09-22T22:47:45] Self-test 36K (thread 1 of 2) passed!"
        m_t1 = PASSED_PATTERN.search(line_t1)
        self.assertIsNotNone(m_t1)
        self.assertEqual(m_t1.group(1), "36K")
        self.assertEqual(m_t1.group(2), "1")
        self.assertEqual(m_t1.group(3), "2")

        line_t2 = "[2026-09-22T22:47:45] Self-test 36K (thread 2 of 2) passed!"
        m_t2 = PASSED_PATTERN.search(line_t2)
        self.assertIsNotNone(m_t2)
        self.assertEqual(m_t2.group(1), "36K")
        self.assertEqual(m_t2.group(2), "2")
        self.assertEqual(m_t2.group(3), "2")

    def test_multithread_run_test_target_iterations(self):
        import os
        import tempfile
        import unittest.mock
        from lib.models import TestRequest

        r_fd, w_fd = os.pipe()
        with os.fdopen(w_fd, "w") as w:
            w.write(
                "[2026-09-22T22:47:45] Self-test 36K (thread 1 of 2) passed!\n"
                "[2026-09-22T22:47:45] Self-test 36K (thread 2 of 2) passed!\n"
                "[2026-09-22T22:48:53] Self-test 36K (thread 1 of 2) passed!\n"
                "[2026-09-22T22:48:53] Self-test 36K (thread 2 of 2) passed!\n"
            )

        fake_proc = unittest.mock.MagicMock()
        fake_proc.stdout = os.fdopen(r_fd, "r")
        fake_proc.pid = 99999

        calls = 0

        def make_poll():
            nonlocal calls
            calls += 1
            return None if calls < 4 else 0

        fake_proc.poll.side_effect = make_poll

        with tempfile.TemporaryDirectory() as tmpdir, \
             unittest.mock.patch("subprocess.Popen", return_value=fake_proc), \
             unittest.mock.patch("os.sched_setaffinity"):
            runner = Prime95Runner(base_work_dir=tmpdir)
            req = TestRequest(cpus=[0, 12], target_iterations=2, graceful=True, parameters={})
            listener = DummyListener()
            res = runner.run_test(req, listener=listener)

            self.assertTrue(res.passed)
            self.assertEqual(res.status, "PASS")
            self.assertEqual(res.completed_tests, 2)
            self.assertEqual(len(listener.verified), 2)
            self.assertEqual(listener.verified[0], ("36K", 1))
            self.assertEqual(listener.verified[1], ("36K", 2))


if __name__ == "__main__":
    unittest.main()


