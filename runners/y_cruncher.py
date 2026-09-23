"""
y-cruncher execution engine for single-core and multi-core stability testing.
Generates stress configuration files, supervises execution, verifies completed algorithms,
and dispatches events to listeners without presentation coupling.

y-cruncher is proprietary software by Alexander J. Yee (see contrib/y-cruncher/Read Me.txt).
It is invoked as an external process.
"""

import argparse
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

from lib.models import MceEvent, RunResult, TestRequest
from lib.monitors import CycleStretchingMonitor, KernelErrorMonitor
from lib.ui import strip_ansi
from runners.base import StressRunner, TestEventListener, parse_step_time, terminate_process

ALGORITHM_PRESETS: dict[str, list[str]] = {
    "breadpit": ["BBP", "SFTv4", "VT3"],
    "fast": ["BKT", "BBP", "SFTv4"],
    "all": ["BKT", "BBP", "SFTv4", "SNT", "SVT", "FFTv4", "N63", "VT3"],
}

PASSED_PATTERN = re.compile(r"Running\s+([A-Za-z0-9_]+):\s+Passed", re.IGNORECASE)
FAILED_PATTERN = re.compile(r"Running\s+([A-Za-z0-9_]+):\s+Failed", re.IGNORECASE)
ERROR_PATTERNS = [
    re.compile(r"Stress test failed with\s+(\d+)\s+error", re.IGNORECASE),
    re.compile(r"Exception Encountered:\s*(\w+)", re.IGNORECASE),
    re.compile(r"FATAL ERROR.*", re.IGNORECASE),
    re.compile(r"Hardware failure detected.*", re.IGNORECASE),
]


class YCruncherRunner(StressRunner):
    """y-cruncher stress test runner implementation."""

    def __init__(self, binary_path: str | None = None, base_work_dir: str | None = None):
        self.y_cruncher_bin = self._resolve_binary(binary_path)
        self.base_work_dir = base_work_dir or os.path.join(tempfile.gettempdir(), f"zen_tuner_ycruncher_runs_{os.getuid()}")
        os.makedirs(self.base_work_dir, exist_ok=True)

    @property
    def name(self) -> str:
        return "y-cruncher"

    def is_available(self) -> bool:
        return (
            self.y_cruncher_bin is not None
            and os.path.exists(self.y_cruncher_bin)
            and os.access(self.y_cruncher_bin, os.X_OK)
        )

    @staticmethod
    def _resolve_binary(binary_path: str | None = None) -> str | None:
        raw_binary: str | None = None
        if binary_path:
            raw_binary = os.path.abspath(binary_path)
        else:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for rel_path in ("contrib/y-cruncher/y-cruncher", "y-cruncher/y-cruncher"):
                candidate = os.path.join(base_dir, rel_path)
                if os.path.exists(candidate) and os.access(candidate, os.X_OK):
                    raw_binary = candidate
                    break
            if not raw_binary:
                raw_binary = shutil.which("y-cruncher")

        if not raw_binary or not os.path.exists(raw_binary) or not os.access(raw_binary, os.X_OK):
            return raw_binary

        # If pointing to the y-cruncher launcher, resolve the tuned CPU binary in Binaries/
        binaries_dir = os.path.join(os.path.dirname(raw_binary), "Binaries")
        if os.path.isdir(binaries_dir):
            try:
                res = subprocess.run(
                    [raw_binary, "pause:-2", "-h"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                for line in res.stdout.splitlines():
                    candidate_bin = line.strip()
                    if (
                        os.path.isabs(candidate_bin)
                        and os.path.exists(candidate_bin)
                        and os.access(candidate_bin, os.X_OK)
                    ):
                        return candidate_bin
            except Exception:
                pass

        return raw_binary

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Registers y-cruncher specific CLI arguments."""
        group = parser.add_argument_group("y-cruncher Specific Options")
        group.add_argument(
            "--yc-algorithms",
            type=str,
            default="breadpit",
            help="y-cruncher test algorithms ('breadpit', 'all', 'fast', or list like 'BBP,SFTv4,VT3') (default: 'breadpit')",
        )
        group.add_argument(
            "--yc-memory",
            type=int,
            default=0,
            help="Memory allocation in MB for y-cruncher (0 = auto in-cache allocation) (default: 0)",
        )
        group.add_argument(
            "--yc-test-time",
            type=str,
            default="60s",
            help="Duration per y-cruncher test step (e.g. '60s', '30s', '2m') (default: '60s')",
        )

    @classmethod
    def parse_parameters(cls, args: argparse.Namespace) -> tuple[dict[str, Any], str]:
        """Parses y-cruncher CLI arguments into runner parameters and profile description."""
        algo_arg = getattr(args, "yc_algorithms", "breadpit")
        algo_key = algo_arg.strip().lower()
        if algo_key in ALGORITHM_PRESETS:
            algorithms = list(ALGORITHM_PRESETS[algo_key])
        else:
            algorithms = [a.strip() for a in algo_arg.split(",") if a.strip()]
            if not algorithms:
                algorithms = list(ALGORITHM_PRESETS["breadpit"])

        test_time_val = getattr(args, "yc_test_time", "60s")
        seconds_per_test = max(1, int(round(parse_step_time(test_time_val, default_seconds=60.0))))
        memory_mb = getattr(args, "yc_memory", None)
        if memory_mb is not None and memory_mb <= 0:
            memory_mb = None

        profile_desc = f"y-cruncher [{','.join(algorithms)}]"
        runner_params = {
            "algorithms": algorithms,
            "seconds_per_test": seconds_per_test,
            "memory_mb": memory_mb,
        }
        return runner_params, profile_desc

    def write_config(
        self,
        work_dir: str,
        cpus: list[int],
        algorithms: list[str],
        seconds_per_test: int,
        seconds_total: int = 0,
        memory_mb: int | None = None,
    ) -> str:
        """Generates stressTest.cfg configuration file for y-cruncher."""
        cores_str = " ".join(str(c) for c in cpus)
        if memory_mb is not None and memory_mb > 0:
            mem_bytes = int(memory_mb) * 1024 * 1024
        else:
            # Default to in-cache allocation matching CoreCycler standard
            mem_bytes = 26567600 if len(cpus) > 1 else 13418572

        test_lines = "\n".join(f'            "{algo}"' for algo in algorithms)
        cfg_content = (
            "{\n"
            '    Action : "StressTest"\n'
            "    StressTest : {\n"
            '        AllocateLocally : "true"\n'
            f"        LogicalCores : [{cores_str}]\n"
            f"        TotalMemory : {mem_bytes}\n"
            f"        SecondsPerTest : {seconds_per_test}\n"
            f"        SecondsTotal : {seconds_total}\n"
            '        StopOnError : "true"\n'
            "        Tests : [\n"
            f"{test_lines}\n"
            "        ]\n"
            "    }\n"
            "}\n"
        )
        cfg_path = os.path.join(work_dir, "stressTest.cfg")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(cfg_content)
        return cfg_path

    def run_test(
        self,
        request: TestRequest,
        listener: TestEventListener | None = None,
    ) -> RunResult:
        """Executes a y-cruncher stress run on requested CPUs according to request parameters."""
        if not self.is_available():
            raise FileNotFoundError(f"y-cruncher binary not found: {self.y_cruncher_bin}")

        algorithms = request.parameters.get("algorithms") or ALGORITHM_PRESETS["breadpit"]
        seconds_per_test = request.parameters.get("seconds_per_test", 60)
        memory_mb = request.parameters.get("memory_mb")
        iterations = request.target_iterations
        target_tests = (iterations * len(algorithms)) if iterations is not None else None
        duration = request.duration_seconds

        run_id = f"yc_{int(time.time() * 1000)}_{os.getpid()}"
        work_dir = os.path.join(self.base_work_dir, run_id)
        os.makedirs(work_dir, exist_ok=True)

        if duration is not None and duration > 0:
            seconds_total = int(round(duration))
        elif target_tests is not None:
            seconds_total = target_tests * seconds_per_test
        else:
            seconds_total = 0
        cfg_path = self.write_config(
            work_dir=work_dir,
            cpus=request.cpus,
            algorithms=algorithms,
            seconds_per_test=seconds_per_test,
            seconds_total=seconds_total,
            memory_mb=memory_mb,
        )

        is_root = os.geteuid() == 0
        if is_root:
            try:
                os.sched_setscheduler(0, os.SCHED_RR, os.sched_param(50))
            except OSError:
                pass

        cmd = [
            self.y_cruncher_bin,
            "pause:-2",
            "skip-warnings",
            "colors:0",
            "status:none",
            "config",
            cfg_path,
        ]
        active_errors: list[str] = []
        idle_mce_errors: list[MceEvent] = []
        verified_tests: list[str] = []
        last_verified_algo: str | None = None
        completed_tests = 0
        summary_line: str | None = None
        stopping = False
        was_interrupted = False
        start_time = time.time()

        use_pty = True
        read_fd = None
        master_fd = None
        try:
            m_fd, slave_fd = pty.openpty()
            proc = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
            os.close(slave_fd)
            master_fd = m_fd
            if proc.stdout is not None:
                os.close(master_fd)
                master_fd = None
                use_pty = False
                read_fd = proc.stdout.fileno()
            else:
                read_fd = master_fd
            os.set_blocking(read_fd, False)
        except Exception:
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            use_pty = False
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            read_fd = proc.stdout.fileno() if proc.stdout else None
            if read_fd is not None:
                os.set_blocking(read_fd, False)

        try:
            os.sched_setaffinity(proc.pid, set(request.cpus))
            if is_root:
                os.sched_setscheduler(proc.pid, os.SCHED_RR, os.sched_param(40))
        except OSError:
            pass

        smu_mon = request.parameters.get("smu_monitor")
        core_idx = request.parameters.get("core_idx")

        buffer = ""

        with KernelErrorMonitor(tested_cpus=request.cpus) as kernel_mon, \
             CycleStretchingMonitor(cpus=request.cpus, smu_monitor=smu_mon, core_idx=core_idx) as stretch_mon:
            try:
                while True:
                    elapsed = time.time() - start_time

                    # 1. Check process exit
                    ret = proc.poll()
                    if ret is not None:
                        if use_pty and read_fd is not None:
                            try:
                                while True:
                                    chunk_b = os.read(read_fd, 4096)
                                    if not chunk_b:
                                        break
                                    buffer += chunk_b.decode("utf-8", errors="replace")
                            except (BlockingIOError, OSError):
                                pass
                        elif proc.stdout:
                            try:
                                rest = proc.stdout.read()
                                if rest:
                                    buffer += rest
                            except Exception:
                                pass

                        # Drain and check remaining buffer lines
                        if buffer:
                            for raw_line in re.split(r"[\r\n]+", buffer):
                                line = raw_line.strip()
                                if not line:
                                    continue
                                line_clean = strip_ansi(line)
                                if listener:
                                    listener.on_output_line(line_clean)
                                if FAILED_PATTERN.search(line_clean):
                                    active_errors.append(f"Algorithm failure: {line_clean}")
                                for ep in ERROR_PATTERNS:
                                    if ep.search(line_clean):
                                        active_errors.append(line_clean)
                                m_pass = PASSED_PATTERN.search(line_clean)
                                if m_pass:
                                    algo_name = m_pass.group(1)
                                    if algo_name != last_verified_algo:
                                        last_verified_algo = algo_name
                                        verified_tests.append(algo_name)
                                        completed_tests += 1
                                        if listener:
                                            listener.on_test_verified(algo_name, completed_tests)

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

                    # 2. Read non-blockingly and process output lines
                    if read_fd is not None:
                        r, _, _ = select.select([read_fd], [], [], 0.05)
                        if r:
                            while True:
                                try:
                                    if use_pty:
                                        chunk_b = os.read(read_fd, 4096)
                                        if not chunk_b:
                                            break
                                        chunk = chunk_b.decode("utf-8", errors="replace")
                                    else:
                                        chunk = proc.stdout.read(4096)
                                        if not chunk:
                                            break
                                except (BlockingIOError, OSError):
                                    break
                                buffer += chunk

                            # Split by carriage return or newline
                            parts = re.split(r"[\r\n]+", buffer)
                            buffer = parts[-1]  # Keep incomplete last piece in buffer
                            for raw_line in parts[:-1]:
                                line = raw_line.strip()
                                if not line:
                                    continue
                                line_clean = strip_ansi(line)
                                if listener:
                                    listener.on_output_line(line_clean)

                                # Check passed pattern: Running <algo>: Passed
                                m_pass = PASSED_PATTERN.search(line_clean)
                                if m_pass:
                                    algo_name = m_pass.group(1)
                                    if algo_name != last_verified_algo:
                                        last_verified_algo = algo_name
                                        verified_tests.append(algo_name)
                                        completed_tests += 1
                                        if listener:
                                            listener.on_test_verified(algo_name, completed_tests)

                                # Check failures and errors
                                m_fail = FAILED_PATTERN.search(line_clean)
                                if m_fail:
                                    active_errors.append(f"Algorithm failure: {line_clean}")
                                    stopping = True
                                    terminate_process(proc, graceful=True)
                                    break

                                for ep in ERROR_PATTERNS:
                                    if ep.search(line_clean):
                                        active_errors.append(line_clean)
                                        stopping = True
                                        terminate_process(proc, graceful=True)
                                        break
                                if stopping:
                                    break

                    # 3. Check kernel hardware errors / MCEs
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

                    # 4. Check cycle / clock stretching
                    stretch_alerts = stretch_mon.poll()
                    if stretch_alerts and listener:
                        for sample in stretch_alerts:
                            listener.on_stretching_detected(sample)

                    # 5. Abort on active error
                    if active_errors and not stopping:
                        stopping = True
                        terminate_process(proc, graceful=False)
                        break

                    # 6. Check target tests completion
                    if not stopping and target_tests is not None and completed_tests >= target_tests:
                        stopping = True
                        terminate_process(proc, graceful=True)

                    # 7. Check duration expiration
                    if not stopping and duration is not None and elapsed >= duration:
                        stopping = True
                        terminate_process(proc, graceful=True)

                    time.sleep(0.05)

            except KeyboardInterrupt:
                was_interrupted = True
                terminate_process(proc, graceful=False)
            finally:
                terminate_process(proc, graceful=True)
                if use_pty and read_fd is not None:
                    try:
                        os.close(read_fd)
                    except OSError:
                        pass
                shutil.rmtree(work_dir, ignore_errors=True)

            elapsed_total = time.time() - start_time

            avg_target = None
            avg_effective = None
            if stretch_mon.all_samples:
                avg_target = sum(s.target_mhz for s in stretch_mon.all_samples) / len(stretch_mon.all_samples)
                avg_effective = sum(s.effective_mhz for s in stretch_mon.all_samples) / len(stretch_mon.all_samples)

            stretching_detected = stretch_mon.stretching_detected

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
            elif completed_tests == 0 and duration is not None and elapsed_total < (duration * 0.5):
                status = "TIMEOUT"
                passed = False
                err_msg = "Process ended before completing any verified algorithms"
            else:
                status = "PASS"
                passed = True
                err_msg = None

            if completed_tests > 0:
                summary_line = f"{completed_tests} algorithms completed ({', '.join(verified_tests[:6])})"

            return RunResult(
                passed=passed,
                status=status,
                tested_cpus=request.cpus,
                completed_tests=completed_tests,
                elapsed_seconds=elapsed_total,
                error_message=err_msg,
                active_errors=active_errors,
                idle_mce_errors=idle_mce_errors,
                verified_ffts=verified_tests,
                summary_line=summary_line,
                stretching_detected=stretching_detected,
                max_stretch_mhz=stretch_mon.max_stretch_mhz,
                avg_stretch_mhz=stretch_mon.avg_stretch_mhz,
                median_stretch_mhz=stretch_mon.median_stretch_mhz,
                avg_target_mhz=avg_target,
                avg_effective_mhz=avg_effective,
                stretch_samples_count=stretch_mon.stretch_alerts_count,
            )
