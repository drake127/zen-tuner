"""
Prime95 / mprime execution engine for single-core stability testing.
Configures mprime torture parameters, supervises execution, verifies mathematical outputs,
and dispatches events to listeners without any hardcoded presentation logic.

Prime95 (mprime) is proprietary freeware by Mersenne Research, Inc. / George Woltman
governed by the GIMPS EULA (see contrib/prime95/license.txt). It is invoked as an external process.
"""

import argparse
from dataclasses import dataclass
import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time

from lib.models import MceEvent, RunResult, TestRequest
from lib.monitors import CycleStretchingMonitor, KernelErrorMonitor
from runners.base import StressRunner, TestEventListener, terminate_process


@dataclass(frozen=True)
class FFTConfig:
    """Torture test FFT profile configuration."""
    min_fft: int
    max_fft: int
    mem_mb: int
    desc: str


FFT_PRESET_MATRIX: dict[str, dict[str, FFTConfig]] = {
    "smallest": {
        "sse": FFTConfig(4, 21, 0, "Smallest FFTs (4K-21K in-place, L1/L2, max boost)"),
        "avx": FFTConfig(4, 21, 0, "Smallest FFTs (4K-21K in-place, L1/L2, max boost)"),
        "avx2": FFTConfig(4, 21, 0, "Smallest FFTs (4K-21K in-place, L1/L2, max boost)"),
        "avx512": FFTConfig(4, 21, 0, "Smallest FFTs (4K-21K in-place, L1/L2, max boost)"),
    },
    "small": {
        "sse": FFTConfig(40, 248, 0, "Small FFTs (40K-248K in-place, L1/L2/L3, thermal stress)"),
        "avx": FFTConfig(36, 248, 0, "Small FFTs (36K-248K in-place, L1/L2/L3, thermal stress)"),
        "avx2": FFTConfig(36, 248, 0, "Small FFTs (36K-248K in-place, L1/L2/L3, thermal stress)"),
        "avx512": FFTConfig(36, 248, 0, "Small FFTs (36K-248K in-place, L1/L2/L3, thermal stress)"),
    },
    "large": {
        "sse": FFTConfig(426, 8192, 2048, "Large FFTs (426K-8192K, Memory Controller & RAM)"),
        "avx": FFTConfig(426, 8192, 2048, "Large FFTs (426K-8192K, Memory Controller & RAM)"),
        "avx2": FFTConfig(426, 8192, 2048, "Large FFTs (426K-8192K, Memory Controller & RAM)"),
        "avx512": FFTConfig(426, 8192, 2048, "Large FFTs (426K-8192K, Memory Controller & RAM)"),
    },
    "blend": {
        "sse": FFTConfig(4, 8192, 4096, "Blend (4K-8192K, CPU + RAM cycling)"),
        "avx": FFTConfig(4, 8192, 4096, "Blend (4K-8192K, CPU + RAM cycling)"),
        "avx2": FFTConfig(4, 8192, 4096, "Blend (4K-8192K, CPU + RAM cycling)"),
        "avx512": FFTConfig(4, 8192, 4096, "Blend (4K-8192K, CPU + RAM cycling)"),
    },
}

FFT_PRESETS: dict[str, FFTConfig] = {k: v["avx2"] for k, v in FFT_PRESET_MATRIX.items()}

RANGE_PATTERN = re.compile(r"^(\d+)[kK]?\s*-\s*(\d+)[kK]?$")


def parse_fft_size_k(fft_name: str) -> int:
    """Parses FFT size in K from string like '36K', '4M', '40'."""
    s = fft_name.strip().upper()
    if s.endswith("M"):
        return int(float(s[:-1]) * 1024)
    if s.endswith("K"):
        return int(s[:-1])
    return int(s)


def build_fft_config(
    preset_or_range: str,
    min_fft: int | None = None,
    max_fft: int | None = None,
    memory: int | None = None,
    mode: str = "avx2",
) -> FFTConfig:
    """Builds an FFTConfig from a preset alias, range string, or explicit bounds."""
    if min_fft is not None or max_fft is not None or memory is not None:
        effective_min = min_fft if min_fft is not None else 4
        effective_max = max_fft if max_fft is not None else 4
        effective_mem = memory if memory is not None else 0
        desc = f"Custom {effective_min}K-{effective_max}K (Mem: {effective_mem}MB)"
        return FFTConfig(effective_min, effective_max, effective_mem, desc)

    match = RANGE_PATTERN.match(preset_or_range.strip())
    if match:
        rmin = int(match.group(1))
        rmax = int(match.group(2))
        return FFTConfig(rmin, rmax, 0, f"Range {rmin}K-{rmax}K (in-place)")

    preset = preset_or_range.lower()
    mode_key = (mode or "avx2").lower()
    if preset in FFT_PRESET_MATRIX:
        mode_matrix = FFT_PRESET_MATRIX[preset]
        if mode_key in mode_matrix:
            return mode_matrix[mode_key]
        return mode_matrix.get("avx2", mode_matrix["sse"])

    return FFT_PRESET_MATRIX["smallest"].get(mode_key, FFT_PRESET_MATRIX["smallest"]["sse"])


PASSED_PATTERN = re.compile(
    r"Self-test\s+(\d+[kKmM]?)(?:\s*\(thread\s+(\d+)\s+of\s+(\d+)\))?\s+passed!",
    re.IGNORECASE,
)
WORKER_PREFIX_PATTERN = re.compile(r"^\[\s*(?:Worker(?:\s*#\d+)?|Main thread)\s*,?\s*", re.IGNORECASE)

ERROR_PATTERNS = [
    re.compile(r"FATAL ERROR.*", re.IGNORECASE),
    re.compile(r"Hardware failure detected.*", re.IGNORECASE),
    re.compile(r"TORTURE TEST FAILED.*", re.IGNORECASE),
    re.compile(r"Rounding was.*expected less than 0\.4", re.IGNORECASE),
    re.compile(r"SUM\(INPUTS\) != SUM\(OUTPUTS\)", re.IGNORECASE),
    re.compile(r"Illegal Sumout", re.IGNORECASE),
]


def strip_worker_prefix(line: str) -> str:
    r"""Removes 'Worker #\d+', 'Worker', 'Main thread' from bracketed prefix e.g. [Worker 2026-...] -> [2026-...]."""
    return WORKER_PREFIX_PATTERN.sub("[", line)


class Prime95Runner(StressRunner):
    """Prime95 (mprime) stress test runner implementation."""

    def __init__(self, mprime_path: str | None = None, base_work_dir: str | None = None):
        self.mprime_bin = self._resolve_mprime_bin(mprime_path)
        self.base_work_dir = base_work_dir or os.path.join(tempfile.gettempdir(), "zen_tuner_runs")
        os.makedirs(self.base_work_dir, exist_ok=True)

    @property
    def name(self) -> str:
        return "prime95"

    def is_available(self) -> bool:
        return self.mprime_bin is not None and os.path.exists(self.mprime_bin) and os.access(self.mprime_bin, os.X_OK)

    @staticmethod
    def _resolve_mprime_bin(mprime_path: str | None = None) -> str | None:
        if mprime_path:
            return os.path.abspath(mprime_path)
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel_dir in ("contrib/prime95", "prime95"):
            candidate = os.path.join(base_dir, rel_dir, "mprime")
            if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return shutil.which("mprime") or shutil.which("prime95")

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Registers Prime95 specific CLI arguments."""
        group = parser.add_argument_group("Prime95 Specific Options")
        group.add_argument(
            "--fft",
            type=str,
            default="smallest",
            help="FFT preset ('smallest', 'small', 'large', 'blend') or range (e.g. '36-248', default: 'smallest')",
        )
        group.add_argument("--min-fft", type=int, default=None, help="Custom minimum FFT size in K")
        group.add_argument("--max-fft", type=int, default=None, help="Custom maximum FFT size in K")
        group.add_argument("--memory", type=int, default=None, help="Memory in MB for torture test (0 = in-place)")
        group.add_argument("--test-time", type=int, default=1, help="Prime95 TortureTime in minutes (default: 1)")
        group.add_argument("--mprime", type=str, default=None, help="Path to mprime binary")

        group.add_argument(
            "--mode",
            type=str.lower,
            choices=["sse", "avx", "avx2", "avx512"],
            default=None,
            help="Instruction set mode: 'sse', 'avx', 'avx2', 'avx512' (default: auto)",
        )

    def write_config(
        self,
        work_dir: str,
        fft_cfg: FFTConfig,
        num_threads: int,
        test_time_min: int,
        mode: str | None = None,
    ) -> None:
        """Generates prime.txt and local.txt configuration ensuring single worker, error checks, and ISO timestamps."""
        prime_txt = os.path.join(work_dir, "prime.txt")
        local_txt = os.path.join(work_dir, "local.txt")
        ht_val = 1 if num_threads > 1 else 0

        mode_lines: list[str] = []
        m = (mode or "").lower()
        if m == "sse":
            mode_lines = [
                "CpuSupportsAVX=0",
                "CpuSupportsAVX2=0",
                "CpuSupportsAVX512F=0",
                "CpuSupportsFMA3=0",
                "CpuSupportsFMA4=0",
            ]
        elif m == "avx":
            mode_lines = [
                "CpuSupportsAVX=1",
                "CpuSupportsAVX2=0",
                "CpuSupportsAVX512F=0",
                "CpuSupportsFMA3=0",
                "CpuSupportsFMA4=0",
            ]
        elif m == "avx2":
            mode_lines = [
                "CpuSupportsAVX=1",
                "CpuSupportsAVX2=1",
                "CpuSupportsAVX512F=0",
                "CpuSupportsFMA3=1",
                "CpuSupportsFMA4=0",
            ]
        elif m == "avx512":
            mode_lines = [
                "CpuSupportsAVX=1",
                "CpuSupportsAVX2=1",
                "CpuSupportsAVX512F=1",
                "CpuSupportsFMA3=1",
            ]

        mode_block = ("\n".join(mode_lines) + "\n") if mode_lines else ""

        content = (
            "StressTester=1\n"
            "UsePrimenet=0\n"
            "TortureCores=1\n"
            f"TortureHyperthreading={ht_val}\n"
            f"MinTortureFFT={fft_cfg.min_fft}\n"
            f"MaxTortureFFT={fft_cfg.max_fft}\n"
            f"TortureMem={fft_cfg.mem_mb}\n"
            f"TortureTime={test_time_min}\n"
            "TortureWeak=0\n"
            f"{mode_block}"
            "ErrorCheck=1\n"
            "SumInputsErrorCheck=1\n"
            "NumWorkers=1\n"
            "EnableSetAffinity=0\n"
            "EnableSetPriority=0\n"
            "TimeStamp=7\n"
            "TimeStampFormat=%Y-%m-%dT%H:%M:%S\n"
            "LogTimeStamp=7\n"
            "LogTimeStampFormat=%Y-%m-%dT%H:%M:%S\n"
            "[Windows]\n"
            "MergeWindows=33\n"
            "[Internals]\n"
            "V30OptionsConverted=1\n"
        )
        with open(prime_txt, "w", encoding="utf-8") as f:
            f.write(content)

        if mode_lines:
            with open(local_txt, "w", encoding="utf-8") as f:
                f.write("\n".join(mode_lines) + "\n")

    def run_test(
        self,
        request_or_cpus: TestRequest | list[int],
        listener: TestEventListener | None = None,
        **legacy_kwargs,
    ) -> RunResult:
        """
        Executes a Prime95 torture run on requested CPUs.
        Supports both modern TestRequest objects and legacy kwargs for full backward compatibility.
        """
        if isinstance(request_or_cpus, TestRequest):
            req = request_or_cpus
        else:
            # Reconstruct TestRequest from legacy positional/keyword arguments
            cpus = list(request_or_cpus)
            duration = legacy_kwargs.get("target_time", 300.0)
            tests = legacy_kwargs.get("target_tests")
            graceful = legacy_kwargs.get("graceful", True)
            params = {
                "fft_preset": legacy_kwargs.get("fft_preset", "smallest"),
                "test_time_min": legacy_kwargs.get("test_time_min", 1),
                "custom_fft": legacy_kwargs.get("custom_fft"),
                "fft_config": legacy_kwargs.get("fft_config"),
                "mode": legacy_kwargs.get("mode"),
            }
            req = TestRequest(
                cpus=cpus,
                duration_seconds=duration,
                target_iterations=tests,
                graceful=graceful,
                parameters=params,
            )

        if not self.is_available():
            raise FileNotFoundError(f"mprime binary not found: {self.mprime_bin}")

        test_time_min = req.parameters.get("test_time_min", 1)
        mode = req.parameters.get("mode")

        # Resolve FFT configuration (pure range API)
        custom = req.parameters.get("fft_config") or req.parameters.get("custom_fft")
        if isinstance(custom, FFTConfig):
            fft_cfg = custom
        elif isinstance(custom, dict):
            fft_cfg = FFTConfig(
                min_fft=custom.get("min_fft", 4),
                max_fft=custom.get("max_fft", 21),
                mem_mb=custom.get("mem_mb", 0),
                desc=custom.get("desc", "Custom FFT"),
            )
        else:
            preset_name = req.parameters.get("fft_preset", "smallest")
            fft_cfg = build_fft_config(preset_name, mode=mode or "avx2")

        run_id = f"prime_{int(time.time() * 1000)}_{os.getpid()}"
        work_dir = os.path.join(self.base_work_dir, run_id)
        os.makedirs(work_dir, exist_ok=True)

        self.write_config(work_dir, fft_cfg, num_threads=len(req.cpus), test_time_min=test_time_min, mode=mode)

        # Elevate current Python process to SCHED_RR priority 50 if root
        is_root = os.geteuid() == 0
        if is_root:
            try:
                os.sched_setscheduler(0, os.SCHED_RR, os.sched_param(50))
            except OSError:
                pass

        cmd = [self.mprime_bin, f"-w{work_dir}", "-t"]
        active_errors: list[str] = []
        idle_mce_errors: list[MceEvent] = []
        verified_ffts: list[str] = []
        current_set_sizes: list[int] = []
        completed_tests = 0
        num_threads = max(1, len(req.cpus))
        thread_completed: dict[int, int] = {t: 0 for t in range(1, num_threads + 1)}
        raw_single_count = 0
        summary_line: str | None = None
        start_time = time.time()

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

        # Set affinity and SCHED_RR priority 40 for child
        try:
            os.sched_setaffinity(proc.pid, set(req.cpus))
            if is_root:
                os.sched_setscheduler(proc.pid, os.SCHED_RR, os.sched_param(40))
        except OSError:
            pass

        stopping = False
        was_interrupted = False
        grace_start: float | None = None
        in_flight_target: int | None = None
        max_grace_duration = max(60.0, float(test_time_min * 60) * 1.5)

        stdout_fd = proc.stdout.fileno() if proc.stdout else None
        if stdout_fd is not None:
            os.set_blocking(stdout_fd, False)

        results_path = os.path.join(work_dir, "results.txt")
        results_file = None
        smu_mon = req.parameters.get("smu_monitor")
        core_idx = req.parameters.get("core_idx")

        with KernelErrorMonitor(tested_cpus=req.cpus) as kernel_mon, \
             CycleStretchingMonitor(cpus=req.cpus, smu_monitor=smu_mon, core_idx=core_idx) as stretch_mon:
            try:
                while True:
                    elapsed = time.time() - start_time

                    # 1. Check process exit
                    ret = proc.poll()
                    if ret is not None:
                        if proc.stdout:
                            try:
                                rest = proc.stdout.read()
                                if rest and listener:
                                    for line in rest.splitlines():
                                        line_clean = strip_worker_prefix(line.strip())
                                        if line_clean:
                                            listener.on_output_line(line_clean)
                            except Exception:
                                pass

                        if ret < 0:
                            sig_num = -ret
                            if sig_num in (signal.SIGINT, signal.SIGTERM):
                                if not stopping:
                                    was_interrupted = True
                            else:
                                active_errors.append(
                                    f"Process crashed with signal {sig_num} ({signal.strsignal(sig_num)})"
                                )
                        elif ret in (130, 143):
                            if not stopping:
                                was_interrupted = True
                        elif ret != 0 and not stopping:
                            active_errors.append(f"Process exited abnormally with code {ret}")
                        break

                    # 2. Read stdout non-blockingly and drain all available lines
                    if stdout_fd is not None and proc.stdout:
                        r, _, _ = select.select([stdout_fd], [], [], 0.1)
                        if r:
                            while True:
                                try:
                                    line = proc.stdout.readline()
                                except (BlockingIOError, OSError):
                                    break
                                if not line:
                                    break

                                line_clean = strip_worker_prefix(line.strip())
                                if listener and line_clean:
                                    listener.on_output_line(line_clean)

                                m_pass = PASSED_PATTERN.search(line_clean)
                                if m_pass:
                                    fft_name = m_pass.group(1)
                                    thread_idx_str = m_pass.group(2)
                                    step_passed_for_all = False
                                    if thread_idx_str is not None:
                                        t_idx = int(thread_idx_str)
                                        thread_completed[t_idx] = thread_completed.get(t_idx, 0) + 1
                                        new_iter = min(thread_completed.values())
                                        if new_iter > raw_single_count:
                                            raw_single_count = new_iter
                                            step_passed_for_all = True
                                    else:
                                        raw_single_count += 1
                                        step_passed_for_all = (raw_single_count % num_threads) == 0

                                    if step_passed_for_all:
                                        verified_ffts.append(fft_name)
                                        size_k = parse_fft_size_k(fft_name)
                                        is_single_fft = fft_cfg.min_fft == fft_cfg.max_fft

                                        if is_single_fft:
                                            completed_tests += 1
                                        else:
                                            if current_set_sizes and size_k < max(current_set_sizes):
                                                completed_tests += 1
                                                current_set_sizes.clear()

                                            current_set_sizes.append(size_k)

                                            if size_k >= fft_cfg.max_fft:
                                                completed_tests += 1
                                                current_set_sizes.clear()

                                        if listener:
                                            listener.on_test_verified(fft_name, completed_tests)

                                for err_pat in ERROR_PATTERNS:
                                    if err_pat.search(line_clean):
                                        active_errors.append(line_clean)

                                if "Torture Test completed" in line_clean:
                                    summary_line = line_clean

                    # 3. Read results.txt if available
                    if results_file is None and os.path.exists(results_path):
                        try:
                            results_file = open(results_path, "r", encoding="utf-8")
                        except OSError:
                            pass

                    if results_file is not None:
                        while True:
                            res_line = results_file.readline()
                            if not res_line:
                                break
                            res_clean = strip_worker_prefix(res_line.strip())
                            for err_pat in ERROR_PATTERNS:
                                if err_pat.search(res_clean):
                                    if res_clean not in active_errors:
                                        active_errors.append(res_clean)

                    # 4. Check kernel hardware errors / MCEs
                    new_active_mce, new_idle_mce = kernel_mon.poll()
                    if new_active_mce:
                        for ev in new_active_mce:
                            active_errors.append(f"MCE on ACTIVE CPU {ev.cpu}: {ev.message}")
                            if listener:
                                listener.on_hardware_error(ev)
                    if new_idle_mce:
                        for ev in new_idle_mce:
                            idle_mce_errors.append(ev)
                            if listener:
                                listener.on_hardware_error(ev)

                    # 5. Check cycle / clock stretching
                    stretch_alerts = stretch_mon.poll()
                    if stretch_alerts and listener:
                        for sample in stretch_alerts:
                            listener.on_stretching_detected(sample)

                    # 6. Abort on active error
                    if active_errors and not stopping:
                        stopping = True
                        terminate_process(proc, graceful=False)
                        break

                    # 7. Check completion criteria
                    if not stopping:
                        if req.target_iterations is not None:
                            if completed_tests >= req.target_iterations:
                                stopping = True
                                terminate_process(proc, graceful=True)
                        elif req.duration_seconds is not None and elapsed >= req.duration_seconds:
                            if req.graceful:
                                if grace_start is None:
                                    grace_start = time.time()
                                    in_flight_target = completed_tests + 1
                                    if listener:
                                        listener.on_grace_period_started(max_grace_duration)
                                if (
                                    (in_flight_target is not None and completed_tests >= in_flight_target)
                                    or (time.time() - grace_start > max_grace_duration)
                                ):
                                    stopping = True
                                    terminate_process(proc, graceful=True)
                            else:
                                stopping = True
                                terminate_process(proc, graceful=True)

                    time.sleep(0.05)

            finally:
                terminate_process(proc, graceful=False)
                if results_file is not None:
                    try:
                        results_file.close()
                    except OSError:
                        pass
                if not active_errors and not idle_mce_errors:
                    shutil.rmtree(work_dir, ignore_errors=True)

        total_elapsed = time.time() - start_time

        avg_target = None
        avg_effective = None
        if stretch_mon.all_samples:
            avg_target = sum(s.target_mhz for s in stretch_mon.all_samples) / len(stretch_mon.all_samples)
            avg_effective = sum(s.effective_mhz for s in stretch_mon.all_samples) / len(stretch_mon.all_samples)

        stretching_detected = stretch_mon.stretch_alerts_count > 0

        if was_interrupted:
            status = "INTERRUPTED"
            passed = False
            err_msg = "Test interrupted by user (SIGINT)"
        elif active_errors:
            status = "ACTIVE_ERROR"
            passed = False
            err_msg = active_errors[0]
        elif idle_mce_errors:
            status = "IDLE_MCE_ERROR"
            passed = False
            err_msg = f"Idle core crash: {idle_mce_errors[0].message}"
        elif not verified_ffts and not stopping:
            status = "UNVERIFIED"
            passed = False
            err_msg = "Process ended before completing any verified self-tests"
        else:
            status = "PASS"
            passed = True
            err_msg = None

        return RunResult(
            passed=passed,
            status=status,
            tested_cpus=req.cpus,
            completed_tests=completed_tests,
            elapsed_seconds=total_elapsed,
            error_message=err_msg,
            active_errors=active_errors,
            idle_mce_errors=idle_mce_errors,
            verified_ffts=verified_ffts,
            summary_line=summary_line,
            stretching_detected=stretching_detected,
            max_stretch_mhz=stretch_mon.max_stretch_mhz,
            avg_stretch_mhz=stretch_mon.avg_stretch_mhz,
            avg_target_mhz=avg_target,
            avg_effective_mhz=avg_effective,
            stretch_samples_count=stretch_mon.stretch_alerts_count,
        )


