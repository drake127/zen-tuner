"""
Unit tests for runner engines, parameter builders, and listener protocols.
"""

import os
import unittest
from zen_tuner import build_fft_config
from runners import Prime95Runner, YCruncherRunner, get_runner, list_runners, parse_step_time, parse_time
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
        self.assertEqual(smallest.max_fft, 21)

        custom = build_fft_config("smallest", min_fft=128, max_fft=256, memory=1024)
        self.assertEqual(custom.min_fft, 128)
        self.assertEqual(custom.max_fft, 256)
        self.assertEqual(custom.mem_mb, 1024)

        range_cfg = build_fft_config("36-248")
        self.assertEqual(range_cfg.min_fft, 36)
        self.assertEqual(range_cfg.max_fft, 248)

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
        self.assertIn("--runner", res.stdout)
        self.assertIn("--prime-fft", res.stdout)
        self.assertIn("--prime-memory", res.stdout)
        self.assertIn("--prime-test-time", res.stdout)
        self.assertIn("--yc-algorithms", res.stdout)
        self.assertIn("--yc-memory", res.stdout)
        self.assertIn("--yc-test-time", res.stdout)
        self.assertNotIn("--mprime", res.stdout)

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


    def test_prime95_write_config_instruction_modes(self):
        import tempfile
        from runners.prime95 import FFTConfig

        cfg = FFTConfig(min_fft=4, max_fft=4, mem_mb=0, desc="Smallest")
        runner = Prime95Runner()

        with tempfile.TemporaryDirectory() as tmpdir:
            # SSE mode
            runner.write_config(tmpdir, cfg, num_threads=1, test_time_min=1, mode="sse")
            with open(os.path.join(tmpdir, "prime.txt")) as f:
                content = f.read()
            self.assertIn("CpuSupportsAVX=0", content)
            self.assertIn("CpuSupportsAVX2=0", content)
            self.assertIn("CpuSupportsAVX512F=0", content)
            self.assertIn("CpuSupportsFMA3=0", content)

            with open(os.path.join(tmpdir, "local.txt")) as f:
                local_content = f.read()
            self.assertIn("CpuSupportsAVX=0", local_content)
            self.assertIn("CpuSupportsAVX2=0", local_content)

        with tempfile.TemporaryDirectory() as tmpdir:
            # AVX mode
            runner.write_config(tmpdir, cfg, num_threads=1, test_time_min=1, mode="avx")
            with open(os.path.join(tmpdir, "prime.txt")) as f:
                content = f.read()
            self.assertIn("CpuSupportsAVX=1", content)
            self.assertIn("CpuSupportsAVX2=0", content)

        with tempfile.TemporaryDirectory() as tmpdir:
            # AVX2 mode
            runner.write_config(tmpdir, cfg, num_threads=1, test_time_min=1, mode="avx2")
            with open(os.path.join(tmpdir, "prime.txt")) as f:
                content = f.read()
            self.assertIn("CpuSupportsAVX=1", content)
            self.assertIn("CpuSupportsAVX2=1", content)
            self.assertIn("CpuSupportsAVX512F=0", content)
            self.assertIn("CpuSupportsFMA3=1", content)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Default / auto mode (mode=None)
            runner.write_config(tmpdir, cfg, num_threads=1, test_time_min=1, mode=None)
            with open(os.path.join(tmpdir, "prime.txt")) as f:
                content = f.read()
            self.assertNotIn("CpuSupportsAVX", content)
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "local.txt")))

    def test_cli_instruction_modes(self):
        import argparse
        parser = argparse.ArgumentParser()
        Prime95Runner.add_cli_arguments(parser)

        args_sse = parser.parse_args(["--prime-mode", "sse"])
        self.assertEqual(args_sse.prime_mode, "sse")

        args_avx = parser.parse_args(["--prime-mode", "avx"])
        self.assertEqual(args_avx.prime_mode, "avx")

        args_avx2 = parser.parse_args(["--prime-mode", "avx2"])
        self.assertEqual(args_avx2.prime_mode, "avx2")

        args_avx512 = parser.parse_args(["--prime-mode", "avx512"])
        self.assertEqual(args_avx512.prime_mode, "avx512")

        args_default = parser.parse_args([])
        self.assertIsNone(args_default.prime_mode)

    def test_sse_small_fft_fallback(self):
        # AVX modes small preset uses 36K-248K
        cfg_avx = build_fft_config("small", mode="avx")
        self.assertEqual(cfg_avx.min_fft, 36)
        self.assertEqual(cfg_avx.max_fft, 248)

        # SSE mode small preset uses 40K-248K
        cfg_sse = build_fft_config("small", mode="sse")
        self.assertEqual(cfg_sse.min_fft, 40)
        self.assertEqual(cfg_sse.max_fft, 248)

        # write_config should write the exact resolved bounds
        import tempfile
        runner = Prime95Runner()
        with tempfile.TemporaryDirectory() as tmpdir:
            runner.write_config(tmpdir, cfg_sse, num_threads=1, test_time_min=1, mode="sse")
            with open(os.path.join(tmpdir, "prime.txt")) as f:
                content = f.read()
            self.assertIn("MinTortureFFT=40", content)
            self.assertIn("MaxTortureFFT=248", content)

    def test_matrix_explicit_modes(self):
        from runners import FFT_PRESET_MATRIX
        for preset_name, modes in FFT_PRESET_MATRIX.items():
            for mode_name in ("sse", "avx", "avx2", "avx512"):
                self.assertIn(mode_name, modes, f"Preset {preset_name} missing {mode_name}")
                cfg = modes[mode_name]
                self.assertGreater(cfg.max_fft, 0)
                self.assertGreater(cfg.min_fft, 0)
                self.assertLessEqual(cfg.min_fft, cfg.max_fft)

    def test_set_based_test_completion(self):
        import os
        import tempfile
        import unittest.mock
        from lib.models import TestRequest

        # Test that sub-FFT steps do not complete a test until the range completes
        r_fd, w_fd = os.pipe()
        with os.fdopen(w_fd, "w") as w:
            w.write(
                "[2026-09-22T22:47:45] Self-test 36K passed!\n"
                "[2026-09-22T22:48:00] Self-test 40K passed!\n"
                "[2026-09-22T22:48:30] Self-test 248K passed!\n"
            )

        fake_proc = unittest.mock.MagicMock()
        fake_proc.stdout = os.fdopen(r_fd, "r")
        fake_proc.pid = 99998
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
            cfg = build_fft_config("small", mode="avx2")  # 36K-248K
            req = TestRequest(cpus=[0], target_iterations=1, graceful=True, parameters={"fft_config": cfg})
            listener = DummyListener()
            res = runner.run_test(req, listener=listener)

            self.assertTrue(res.passed)
            self.assertEqual(res.status, "PASS")
            # All 3 FFTs were verified, but only 1 full set completed
            self.assertEqual(res.completed_tests, 1)
            self.assertEqual(len(res.verified_ffts), 3)
            self.assertEqual(listener.verified, [("36K", 0), ("40K", 0), ("248K", 1)])

    def test_parse_step_time(self):
        self.assertEqual(parse_step_time("30s"), 30.0)
        self.assertEqual(parse_step_time("1m"), 60.0)
        self.assertEqual(parse_step_time("2m"), 120.0)
        self.assertEqual(parse_step_time("1h"), 3600.0)
        self.assertEqual(parse_step_time(None, default_seconds=45.0), 45.0)
        # Integer <= 10 without unit treated as minutes for backward compatibility
        self.assertEqual(parse_step_time("1"), 60.0)
        self.assertEqual(parse_step_time("60"), 60.0)

    def test_y_cruncher_registry_and_discovery(self):
        self.assertIn("y-cruncher", list_runners())
        self.assertIn("ycruncher", list_runners())
        runner = get_runner("y-cruncher")
        self.assertEqual(runner.name, "y-cruncher")
        self.assertIsNotNone(runner.y_cruncher_bin)
        self.assertTrue(runner.is_available())

    def test_y_cruncher_write_config(self):
        import tempfile
        runner = YCruncherRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = runner.write_config(
                work_dir=tmpdir,
                cpus=[0, 12],
                algorithms=["BBP", "SFTv4", "VT3"],
                seconds_per_test=30,
                seconds_total=90,
                memory_mb=128,
            )
            self.assertTrue(os.path.exists(cfg_path))
            with open(cfg_path, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn('Action : "StressTest"', content)
            self.assertIn("LogicalCores : [0 12]", content)
            self.assertIn("TotalMemory : 134217728", content)  # 128 * 1024 * 1024
            self.assertIn("SecondsPerTest : 30", content)
            self.assertIn("SecondsTotal : 90", content)
            self.assertIn('"BBP"', content)
            self.assertIn('"SFTv4"', content)
            self.assertIn('"VT3"', content)

    def test_y_cruncher_parse_parameters(self):
        import argparse
        parser = argparse.ArgumentParser()
        YCruncherRunner.add_cli_arguments(parser)

        # Default args
        args = parser.parse_args([])
        params, desc = YCruncherRunner.parse_parameters(args)
        self.assertEqual(params["algorithms"], ["BBP", "SFTv4", "VT3"])
        self.assertEqual(params["seconds_per_test"], 60)
        self.assertIsNone(params["memory_mb"])
        self.assertIn("BBP,SFTv4,VT3", desc)

        # Custom args with --yc- prefixes
        args2 = parser.parse_args(["--yc-algorithms", "BKT,FFTv4", "--yc-test-time", "30s", "--yc-memory", "2048"])
        params2, desc2 = YCruncherRunner.parse_parameters(args2)
        self.assertEqual(params2["algorithms"], ["BKT", "FFTv4"])
        self.assertEqual(params2["seconds_per_test"], 30)
        self.assertEqual(params2["memory_mb"], 2048)
        self.assertIn("BKT,FFTv4", desc2)

        # Preset 'all'
        args3 = parser.parse_args(["--yc-algorithms", "all"])
        params3, desc3 = YCruncherRunner.parse_parameters(args3)
        self.assertEqual(len(params3["algorithms"]), 8)

    def test_prime95_parse_parameters(self):
        import argparse
        parser = argparse.ArgumentParser()
        Prime95Runner.add_cli_arguments(parser)

        args = parser.parse_args(["--prime-fft", "small", "--prime-test-time", "2m", "--prime-memory", "1024"])
        params, desc = Prime95Runner.parse_parameters(args)
        self.assertEqual(params["fft_preset"], "small")
        self.assertEqual(params["test_time_min"], 2)
        self.assertEqual(params["fft_config"].mem_mb, 1024)
        self.assertIn("Small FFTs", desc)

    def test_y_cruncher_run_test_mocked_success(self):
        import tempfile
        import unittest.mock
        from lib.models import TestRequest

        r_fd, w_fd = os.pipe()
        with os.fdopen(w_fd, "w") as w:
            w.write(
                "Running BBP: Passed  Test Speed: 1.23 * 10^07 bits / sec\n"
                "Running SFTv4: Passed  Test Speed: 4.56 * 10^07 bits / sec\n"
            )

        fake_proc = unittest.mock.MagicMock()
        fake_proc.stdout = os.fdopen(r_fd, "r")
        fake_proc.pid = 99997
        calls = 0

        def make_poll():
            nonlocal calls
            calls += 1
            return None if calls < 4 else 0

        fake_proc.poll.side_effect = make_poll

        with tempfile.TemporaryDirectory() as tmpdir, \
             unittest.mock.patch("subprocess.Popen", return_value=fake_proc), \
             unittest.mock.patch("os.sched_setaffinity"):
            runner = YCruncherRunner(base_work_dir=tmpdir)
            req = TestRequest(cpus=[0], target_iterations=2, graceful=True)
            listener = DummyListener()
            res = runner.run_test(req, listener=listener)

            self.assertTrue(res.passed)
            self.assertEqual(res.status, "PASS")
            self.assertEqual(res.completed_tests, 2)
            self.assertEqual(res.verified_ffts, ["BBP", "SFTv4"])
            self.assertEqual(listener.verified, [("BBP", 1), ("SFTv4", 2)])

    def test_y_cruncher_run_test_mocked_failure(self):
        import tempfile
        import unittest.mock
        from lib.models import TestRequest

        r_fd, w_fd = os.pipe()
        with os.fdopen(w_fd, "w") as w:
            w.write(
                "Running BBP: Failed  Test Speed: 0\n"
                "Stress test failed with 1 error.\n"
            )

        fake_proc = unittest.mock.MagicMock()
        fake_proc.stdout = os.fdopen(r_fd, "r")
        fake_proc.pid = 99996
        fake_proc.poll.return_value = 0

        with tempfile.TemporaryDirectory() as tmpdir, \
             unittest.mock.patch("subprocess.Popen", return_value=fake_proc), \
             unittest.mock.patch("os.sched_setaffinity"):
            runner = YCruncherRunner(base_work_dir=tmpdir)
            req = TestRequest(cpus=[0], target_iterations=1, graceful=True)
            listener = DummyListener()
            res = runner.run_test(req, listener=listener)

            self.assertFalse(res.passed)
            self.assertEqual(res.status, "ACTIVE_ERROR")
            self.assertGreater(len(res.active_errors), 0)

    def test_orchestrator_runner_cycling(self):
        from unittest.mock import MagicMock
        from lib.models import PhysicalCore, RunResult
        from lib.orchestrator import ZenTunerOrchestrator

        cores = [
            PhysicalCore(core_idx=0, hardware_core_id=0, ccd_id=0, logical_cpus=[0, 12]),
        ]

        r1_calls = []
        r2_calls = []

        class MockR1(Prime95Runner):
            def __init__(self):
                pass
            @property
            def name(self):
                return "prime95"
            def run_test(self, req, listener=None):
                r1_calls.append(req.cpus)
                return RunResult(passed=True, status="PASS", tested_cpus=req.cpus, completed_tests=1, elapsed_seconds=1.0)

        class MockR2(YCruncherRunner):
            def __init__(self):
                pass
            @property
            def name(self):
                return "y-cruncher"
            def run_test(self, req, listener=None):
                r2_calls.append(req.cpus)
                return RunResult(passed=True, status="PASS", tested_cpus=req.cpus, completed_tests=1, elapsed_seconds=1.0)

        r1 = MockR1()
        r2 = MockR2()
        presenter = MagicMock()

        orchestrator = ZenTunerOrchestrator(
            runner={"prime95": r1, "y-cruncher": r2},
            all_cores=cores,
            presenter=presenter,
            runner_mode="cycle",
        )

        stats = orchestrator.run(
            selected_cores=cores,
            duration_per_core=1.0,
            cycles=2,
            runner_parameters={"prime95": {"fft": "small"}, "y-cruncher": {"algorithms": ["BBP"]}},
        )

        self.assertEqual(stats[0].passes, 2)
        # Cycle 1 ran MockR1 (prime95), Cycle 2 ran MockR2 (y-cruncher)
        self.assertEqual(len(r1_calls), 1)
        self.assertEqual(len(r2_calls), 1)


