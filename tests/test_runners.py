"""
Tests for runner parameter validation, output parsers, and the shared process supervision loop.

Supervision tests replace the engine binary with a lightweight Python script that mimics the observed engine
behaviour (output format, stop signal handling) while going through the production PTY/pipe, scheduling and
signalling code paths, so they neither take long nor load the CPU.
"""

import argparse
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from lib.models import RunStatus, TestRequest
from runners import RUNNER_CLASSES, RunnerUnavailableError, get_runner, parse_duration
from runners.base import RunContext
from runners.prime95 import (
    PRIME95_MAX_FFT_K,
    FFTConfig,
    Prime95OutputParser,
    Prime95Params,
    Prime95Runner,
    build_fft_config,
    parse_fft_size_k,
    strip_worker_prefix,
)
from runners.y_cruncher import YCruncherOutputParser, YCruncherParams, YCruncherRunner, parse_algorithms

ZEN_TUNER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "zen_tuner.py")
ONE_CPU = [min(os.sched_getaffinity(0))]
SUPPORTED_MODES = frozenset({"sse", "avx", "avx2"})

YC_PARAMS = YCruncherParams(algorithms=("BBP",), seconds_per_test=1, memory_mb=None)
PRIME95_PARAMS = Prime95Params(fft=FFTConfig(4, 4, 0, "4K"), test_time_min=1, mode=None)


def parse_cli(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    for cls in RUNNER_CLASSES.values():
        cls.add_cli_arguments(parser)
    return parser.parse_args(argv)


def feed(parser, lines: list[str]) -> None:
    for line in lines:
        parser.handle_line(line)


def run(runner, params, listener, target: int = 1, context: RunContext | None = None):
    context = context or RunContext()
    context.listener = listener
    request = TestRequest(cpus=ONE_CPU, target_iterations=target, core_idx=0, parameters=params)
    return runner.run_test(request, context)


# Parameters

@pytest.mark.parametrize("text, seconds", [("300", 300.0), ("10", 10.0), ("45s", 45.0), ("5m", 300.0), ("1h", 3600.0)])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "0", "-5m"])
def test_parse_duration_rejects_invalid(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_build_fft_config_presets_and_ranges():
    assert (build_fft_config().min_fft, build_fft_config().max_fft) == (4, 21)
    assert build_fft_config("small", mode="avx").min_fft == 36
    assert build_fft_config("small", mode="sse").min_fft == 40
    rng = build_fft_config("36-248")
    assert (rng.min_fft, rng.max_fft, rng.mem_mb) == (36, 248, 0)
    assert build_fft_config("large").mem_mb == 2048
    assert build_fft_config("large", memory_mb=0).mem_mb == 0


def test_build_fft_config_explicit_bounds():
    only_min = build_fft_config(min_fft=36)
    assert (only_min.min_fft, only_min.max_fft) == (36, PRIME95_MAX_FFT_K)
    only_max = build_fft_config(max_fft=248)
    assert (only_max.min_fft, only_max.max_fft) == (4, 248)
    custom = build_fft_config(min_fft=128, max_fft=256, memory_mb=1024)
    assert (custom.min_fft, custom.max_fft, custom.mem_mb) == (128, 256, 1024)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"preset": "smal"},
        {"preset": "248-36"},
        {"min_fft": 300, "max_fft": 200},
        {"max_fft": PRIME95_MAX_FFT_K + 1},
        {"min_fft": 1},
        {"preset": "small", "min_fft": 36},
        {"memory_mb": -1},
    ],
)
def test_build_fft_config_rejects_invalid_input(kwargs):
    with pytest.raises(ValueError):
        build_fft_config(**kwargs)


@pytest.fixture
def cpu_modes(monkeypatch):
    monkeypatch.setattr("runners.prime95.detect_cpu_instruction_sets", lambda: SUPPORTED_MODES)


def test_prime95_parse_parameters(cpu_modes):
    args = parse_cli(["--prime-fft", "small", "--prime-test-time", "2m", "--prime-memory", "1024"])
    params, desc = Prime95Runner.parse_parameters(args)
    assert params.test_time_min == 2
    assert params.fft.mem_mb == 1024
    assert params.mode == "avx2"
    assert "Small FFTs" in desc


def test_prime95_rejects_unsupported_mode(cpu_modes):
    with pytest.raises(ValueError, match="not supported"):
        Prime95Runner.parse_parameters(parse_cli(["--prime-mode", "avx512"]))


def test_prime95_test_time_in_whole_minutes(cpu_modes):
    with pytest.raises(ValueError, match="whole number of minutes"):
        Prime95Runner.parse_parameters(parse_cli(["--prime-test-time", "90s"]))
    params, _ = Prime95Runner.parse_parameters(parse_cli(["--prime-test-time", "180"]))
    assert params.test_time_min == 3


def test_parse_algorithms():
    assert parse_algorithms("breadpit") == ("BBP", "SFTv4", "VT3")
    assert len(parse_algorithms("ALL")) == 8
    assert parse_algorithms("bbp, sftv4") == ("BBP", "SFTv4")
    with pytest.raises(ValueError, match="FOO"):
        parse_algorithms("BBP,FOO")
    with pytest.raises(ValueError):
        parse_algorithms(" , ")


def test_y_cruncher_parse_parameters():
    params, desc = YCruncherRunner.parse_parameters(parse_cli([]))
    assert params == YCruncherParams(("BBP", "SFTv4", "VT3"), 60, None)
    assert "BBP,SFTv4,VT3" in desc

    args = parse_cli(["--yc-algorithms", "BKT,FFTv4", "--yc-test-time", "30", "--yc-memory", "2048"])
    assert YCruncherRunner.parse_parameters(args)[0] == YCruncherParams(("BKT", "FFTv4"), 30, 2048)


@pytest.mark.parametrize("argv", [["--yc-test-time", "1.5"], ["--yc-memory", "-1"]])
def test_y_cruncher_rejects_invalid_parameters(argv):
    with pytest.raises(ValueError):
        YCruncherRunner.parse_parameters(parse_cli(argv))


def test_runner_registry():
    assert sorted(RUNNER_CLASSES) == ["prime95", "y-cruncher"]
    assert get_runner("prime95").name == "prime95"
    assert get_runner("Y-Cruncher").name == "y-cruncher"
    with pytest.raises(ValueError):
        get_runner("non_existent_runner")


def test_bundled_binaries_are_resolved():
    assert Prime95Runner().is_available()
    assert YCruncherRunner().is_available()


def test_cli_help_lists_options():
    res = subprocess.run([sys.executable, ZEN_TUNER, "--help"], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0
    for option in ("--test-iterations", "--stop-on-error", "--runner", "--prime-fft", "--prime-memory",
                   "--prime-test-time", "--yc-algorithms", "--yc-memory", "--yc-test-time"):
        assert option in res.stdout
    assert "--time " not in res.stdout


@pytest.mark.parametrize(
    "argv",
    [["--test-iterations", "0"], ["--prime-fft", "smal"], ["--runner", "y-cruncher", "--yc-algorithms", "FOO"]],
)
def test_cli_rejects_invalid_values(argv):
    # Validation fails before any engine starts; the timeout guards against accidentally launching a stress run
    res = subprocess.run([sys.executable, ZEN_TUNER, *argv], capture_output=True, text=True, timeout=30)
    assert res.returncode == 2


# Configuration files

@pytest.mark.parametrize(
    "mode, expected",
    [
        ("sse", ("CpuSupportsAVX=0", "CpuSupportsAVX2=0", "CpuSupportsAVX512F=0", "CpuSupportsFMA3=0")),
        ("avx", ("CpuSupportsAVX=1", "CpuSupportsAVX2=0")),
        ("avx2", ("CpuSupportsAVX2=1", "CpuSupportsAVX512F=0", "CpuSupportsFMA3=1")),
        ("avx512", ("CpuSupportsAVX512F=1", "CpuSupportsFMA4=0")),
    ],
)
def test_prime95_config_instruction_modes(tmp_path, mode, expected):
    Prime95Runner.write_config(str(tmp_path), FFTConfig(4, 4, 0, ""), num_threads=1, test_time_min=1, mode=mode)
    content = (tmp_path / "prime.txt").read_text()
    for item in expected:
        assert item in content
    assert not (tmp_path / "local.txt").exists()


def test_prime95_config_defaults(tmp_path):
    Prime95Runner.write_config(str(tmp_path), FFTConfig(40, 248, 0, ""), num_threads=2, test_time_min=3, mode=None)
    content = (tmp_path / "prime.txt").read_text()
    assert "CpuSupports" not in content
    for item in ("MinTortureFFT=40", "MaxTortureFFT=248", "TortureTime=3", "TortureHyperthreading=1"):
        assert item in content


def test_y_cruncher_config(tmp_path):
    cfg_path = YCruncherRunner.write_config(str(tmp_path), [0, 12], YCruncherParams(("BBP", "SFTv4", "VT3"), 30, 128))
    content = open(cfg_path, encoding="utf-8").read()
    assert 'Action : "StressTest"' in content
    assert "LogicalCores : [0 12]" in content
    assert "TotalMemory : 134217728" in content
    assert "SecondsPerTest : 30" in content
    # Runs until the supervisor stops it after the last requested iteration
    assert "SecondsTotal : 0" in content
    for algo in ("BBP", "SFTv4", "VT3"):
        assert f'"{algo}"' in content


# Output parsers

def prime95_parser(listener, fft: FFTConfig, target: int = 1) -> Prime95OutputParser:
    return Prime95OutputParser(fft, target, listener, "/nonexistent/results.txt")


@pytest.mark.parametrize(
    "line, expected",
    [
        ("[Worker 2026-09-22T16:30:00] Test 1", "[2026-09-22T16:30:00] Test 1"),
        ("[Worker #1 2026-09-22T16:30:00] Self-test 4K passed!", "[2026-09-22T16:30:00] Self-test 4K passed!"),
        ("[Main thread 2026-09-22T16:30:00] Starting", "[2026-09-22T16:30:00] Starting"),
        ("[2026-09-22T16:30:00] Clean line", "[2026-09-22T16:30:00] Clean line"),
    ],
)
def test_strip_worker_prefix(line, expected):
    assert strip_worker_prefix(line) == expected


@pytest.mark.parametrize("name, size_k", [("4608", 4.5), ("5K", 5.0), ("4M", 4096.0)])
def test_fft_sizes_in_elements_and_k(name, size_k):
    assert parse_fft_size_k(name) == size_k


def test_element_sized_fft_does_not_complete_range(listener):
    # mprime 30.19 output for a 4K-5K range: the 4.5K FFT is reported in elements as "4608"
    parser = prime95_parser(listener, FFTConfig(4, 5, 0, ""))
    feed(parser, ["[2026-09-23T15:02:44] Self-test 4608 passed!"])
    assert parser.completed_iterations == 0
    feed(parser, ["[2026-09-23T15:03:44] Self-test 5K passed!"])
    assert parser.completed_iterations == 1
    assert listener.verified == [("FFT 4.5K", 0, 1), ("FFT 5K", 1, 1)]


def test_range_pass_completes_on_wrap(listener):
    parser = prime95_parser(listener, FFTConfig(36, 248, 0, ""))
    feed(parser, [f"Self-test {size} passed!" for size in ("36K", "40K", "240K", "36K", "40K")])
    # 240K never reaches 248K: the pass completes when the size wraps back to 36K
    assert parser.completed_iterations == 1
    assert listener.verified[2:4] == [("FFT 240K", 0, 1), ("FFT 36K", 1, 1)]


def test_smt_steps_count_once_all_threads_passed(listener):
    parser = prime95_parser(listener, FFTConfig(36, 36, 0, ""), target=2)
    feed(parser, [
        "[2026-09-22T22:47:45] Self-test 36K (thread 1 of 2) passed!",
        "[2026-09-22T22:47:45] Self-test 36K (thread 2 of 2) passed!",
        "[2026-09-22T22:48:53] Self-test 36K (thread 2 of 2) passed!",
    ])
    assert parser.completed_iterations == 1
    feed(parser, ["[2026-09-22T22:48:53] Self-test 36K (thread 1 of 2) passed!"])
    assert parser.done
    assert listener.verified == [("FFT 36K", 1, 2), ("FFT 36K", 2, 2)]


def test_untagged_steps_count_individually(listener):
    parser = prime95_parser(listener, FFTConfig(4, 4, 0, ""), target=2)
    feed(parser, ["Self-test 4K passed!", "Self-test 4K passed!"])
    assert parser.completed_iterations == 2


def test_prime95_errors(listener):
    parser = prime95_parser(listener, FFTConfig(4, 21, 0, ""))
    feed(parser, ["[2026-09-23T15:01:48] Torture Test completed 3 tests in 3 minutes - 0 errors, 0 warnings."])
    assert parser.errors == []

    feed(parser, ["[2026-09-23T15:02:00] FATAL ERROR: Rounding was 0.5, expected less than 0.4",
                  "[2026-09-23T15:02:01] Torture Test completed 3 tests in 3 minutes - 1 errors, 0 warnings."])
    assert len(parser.errors) == 3
    assert parser.errors[0].startswith("FATAL ERROR")


def test_results_file_errors_are_deduplicated(tmp_path, listener):
    results = tmp_path / "results.txt"
    parser = Prime95OutputParser(FFTConfig(4, 21, 0, ""), 1, listener, str(results))
    parser.poll()
    message = "Hardware failure detected running 4K FFT size, consult stress.txt file."
    results.write_text(f"[2026-09-23T15:02:00]\n{message}\n")
    parser.poll()
    feed(parser, [f"[2026-09-23T15:02:00] {message}"])
    parser.close()
    assert parser.errors == [message]


def test_y_cruncher_single_algorithm_iterations(listener):
    parser = YCruncherOutputParser(algorithm_count=1, target_iterations=3, listener=listener)
    feed(parser, ["Running BBP: Passed  Test Speed:  1.62 * 10^08  terms / sec"] * 3)
    assert parser.done
    assert listener.verified == [("BBP", 1, 3), ("BBP", 2, 3), ("BBP", 3, 3)]


def test_y_cruncher_iteration_spans_all_algorithms(listener):
    parser = YCruncherOutputParser(algorithm_count=2, target_iterations=1, listener=listener)
    feed(parser, ["Iteration: 0  Total Elapsed Time: 0.000 seconds  ( 0.000 minutes )",
                  "Running BBP: Passed  Test Speed:  1.62 * 10^08  terms / sec"])
    assert not parser.done
    feed(parser, ["Running SFTv4: Passed  Test Speed: 3.65 * 10^09  bits / sec"])
    assert parser.done


@pytest.mark.parametrize("line", ["Running BBP: Failed  Test Speed: 0", "Exception Encountered: InvalidParametersException"])
def test_y_cruncher_errors(listener, line):
    parser = YCruncherOutputParser(algorithm_count=1, target_iterations=1, listener=listener)
    feed(parser, [line])
    assert len(parser.errors) == 1


# Supervision with scripted engines

def scripted(runner_cls: type, script: str, **kwargs):
    """Instance of runner_cls whose engine binary is replaced by a Python script."""

    class ScriptedRunner(runner_cls):
        def _prepare(self, request: TestRequest, work_dir: str) -> list[str]:
            return [sys.executable, "-u", "-c", textwrap.dedent(script)]

    return ScriptedRunner(binary_path=sys.executable, **kwargs)


# Mimics y-cruncher: ignores SIGINT, runs forever until SIGTERM
Y_CRUNCHER_SCRIPT = """
    import signal, time
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    print("Running from console...")
    for i in range({passes}):
        print(f"Iteration: {{i}}  Total Elapsed Time: 0.000 seconds  ( 0.000 minutes )")
        print("Running BBP: Passed  Test Speed:  1.62 * 10^08  terms / sec")
    time.sleep(60)
"""

# Mimics mprime: reports passes, stops cleanly with a summary on SIGINT
PRIME95_SCRIPT = """
    import signal, sys, time
    def stop(signum, frame):
        print("Torture Test completed {passes} tests in 1 minutes - 0 errors, 0 warnings.")
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    print("Worker starting")
    for _ in range({passes}):
        print("Self-test 4K passed!")
    time.sleep(60)
"""


def test_y_cruncher_stopped_after_last_iteration(listener):
    result = run(scripted(YCruncherRunner, Y_CRUNCHER_SCRIPT.format(passes=2)), YC_PARAMS, listener, target=2)
    assert result.status == RunStatus.PASS
    assert result.completed_iterations == 2
    assert listener.verified == [("BBP", 1, 2), ("BBP", 2, 2)]
    assert "Running from console..." in listener.lines
    assert result.elapsed_seconds < 5.0


def test_prime95_stopped_after_last_iteration(listener):
    result = run(scripted(Prime95Runner, PRIME95_SCRIPT.format(passes=1)), PRIME95_PARAMS, listener)
    assert result.status == RunStatus.PASS
    # Output printed while stopping is still collected
    assert result.output[-1] == "Torture Test completed 1 tests in 1 minutes - 0 errors, 0 warnings."
    assert result.work_dir is None


@pytest.mark.parametrize(
    "runner_cls, script, params",
    [(YCruncherRunner, Y_CRUNCHER_SCRIPT, YC_PARAMS), (Prime95Runner, PRIME95_SCRIPT, PRIME95_PARAMS)],
    ids=["y-cruncher", "prime95"],
)
def test_cancel_interrupts_run(listener, runner_cls, script, params):
    context = RunContext()
    threading.Timer(0.3, context.cancel.set).start()
    result = run(scripted(runner_cls, script.format(passes=0)), params, listener, context=context)
    assert result.status == RunStatus.INTERRUPTED
    assert result.elapsed_seconds < 5.0


@pytest.mark.parametrize(
    "script, status, message",
    [
        ('print("Self-test 4K passed!")', RunStatus.UNVERIFIED, "1 of 2"),
        ('print("Self-test 4K passed!"); print("FATAL ERROR: Rounding was 0.5, expected less than 0.4")',
         RunStatus.ERROR, "FATAL ERROR"),
        ("import os, signal; os.kill(os.getpid(), signal.SIGSEGV)", RunStatus.CRASH, "signal 11"),
        ("raise SystemExit(3)", RunStatus.CRASH, "code 3"),
    ],
    ids=["early-exit", "error-before-exit", "signal", "exit-code"],
)
def test_exit_classification(listener, script, status, message):
    result = run(scripted(Prime95Runner, script), PRIME95_PARAMS, listener, target=2)
    assert result.status == status
    assert message in result.error_message


def test_y_cruncher_config_error(listener):
    # Observed y-cruncher reaction to an unknown test name: message and exit code 0
    script = 'print("Exception Encountered: InvalidParametersException"); print("Unknown Test: FOO")'
    result = run(scripted(YCruncherRunner, script), YC_PARAMS, listener)
    assert result.status == RunStatus.ERROR


def test_stop_escalates_to_sigkill(listener, monkeypatch):
    monkeypatch.setattr("runners.base.STOP_TIMEOUT_S", 0.3)
    script = """
        import signal, time
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        print("Self-test 4K passed!")
        time.sleep(60)
    """
    start = time.monotonic()
    result = run(scripted(Prime95Runner, script), PRIME95_PARAMS, listener)
    assert time.monotonic() - start < 5.0
    assert result.status == RunStatus.PASS


def test_child_inherits_affinity_in_own_process_group(listener):
    script = """
        import os
        print("affinity", sorted(os.sched_getaffinity(0)))
        print("own_group", os.getpgid(0) == os.getpid())
        print("Self-test 4K passed!")
    """
    before = os.sched_getaffinity(0)
    result = run(scripted(Prime95Runner, script), PRIME95_PARAMS, listener)
    assert result.status == RunStatus.PASS
    assert f"affinity {ONE_CPU}" in listener.lines
    assert "own_group True" in listener.lines
    assert os.sched_getaffinity(0) == before


def test_work_dir_kept_only_on_failure(tmp_path, listener):
    run(scripted(Prime95Runner, 'print("Self-test 4K passed!")', base_work_dir=str(tmp_path)), PRIME95_PARAMS, listener)
    assert os.listdir(tmp_path) == []
    for script in ('print("FATAL ERROR: x")', "raise SystemExit(3)", "pass"):
        result = run(scripted(Prime95Runner, script, base_work_dir=str(tmp_path)), PRIME95_PARAMS, listener)
        assert result.status != RunStatus.PASS
        assert os.path.dirname(result.work_dir) == str(tmp_path)
    assert len(os.listdir(tmp_path)) == 3


def test_failed_run_carries_full_output(listener):
    script = 'print("Worker starting"); print("Unexpected message"); raise SystemExit(3)'
    result = run(scripted(Prime95Runner, script), PRIME95_PARAMS, listener)
    assert result.status == RunStatus.CRASH
    assert result.output == ["Worker starting", "Unexpected message"]


def test_unavailable_binary():
    runner = Prime95Runner(binary_path="/nonexistent/mprime")
    with pytest.raises(RunnerUnavailableError):
        runner.run_test(TestRequest(cpus=ONE_CPU, parameters=PRIME95_PARAMS))
