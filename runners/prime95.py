"""
Prime95 / mprime execution engine for single-core stability testing.
Configures mprime torture parameters and verifies self-test outputs without any presentation logic.

Prime95 (mprime) is proprietary freeware by Mersenne Research, Inc. / George Woltman
governed by the GIMPS EULA (see contrib/prime95/license.txt). It is invoked as an external process.
"""

import argparse
from dataclasses import dataclass
import os
import re
from typing import TextIO

from lib.cpu import detect_cpu_instruction_sets, get_default_instruction_set
from lib.events import TestEventListener
from lib.models import TestRequest
from runners.base import OutputParser, StressRunner, parse_duration

# Torture test FFT bounds in K. The upper bound assumes Prime95 30.x's largest FFT length of 32M (not verified
# against the bundled build); Prime95 itself only tests the FFT lengths it implements within the range.
PRIME95_MIN_FFT_K = 4
PRIME95_MAX_FFT_K = 32768

INSTRUCTION_MODES = ("sse", "avx", "avx2", "avx512")


@dataclass(frozen=True)
class FFTConfig:
    """Torture test FFT profile configuration."""
    min_fft: int
    max_fft: int
    mem_mb: int
    desc: str


FFT_PRESETS: dict[str, FFTConfig] = {
    "smallest": FFTConfig(4, 21, 0, "Smallest FFTs (4K-21K in-place, L1/L2, max boost)"),
    "small": FFTConfig(36, 248, 0, "Small FFTs (36K-248K in-place, L1/L2/L3, thermal stress)"),
    "large": FFTConfig(426, 8192, 2048, "Large FFTs (426K-8192K, Memory Controller & RAM)"),
    "blend": FFTConfig(4, 8192, 4096, "Blend (4K-8192K, CPU + RAM cycling)"),
}

# Instruction-set specific deviations from FFT_PRESETS, carried over from the original preset matrix.
FFT_PRESET_OVERRIDES: dict[tuple[str, str], FFTConfig] = {
    ("small", "sse"): FFTConfig(40, 248, 0, "Small FFTs (40K-248K in-place, L1/L2/L3, thermal stress)"),
}

RANGE_PATTERN = re.compile(r"^(\d+)[kK]?\s*-\s*(\d+)[kK]?$")

# FFT lengths are printed in K when divisible by 1024 ("5K", "36K"), otherwise as element counts ("4608").
PASSED_PATTERN = re.compile(
    r"Self-test\s+(\d+[kKmM]?)(?:\s*\(thread\s+(\d+)\s+of\s+(\d+)\))?\s+passed!",
    re.IGNORECASE,
)
SUMMARY_PATTERN = re.compile(r"Torture Test completed.*?(\d+)\s+errors?", re.IGNORECASE)
WORKER_PREFIX_PATTERN = re.compile(r"^\[\s*(?:Worker(?:\s*#\d+)?|Main thread)\s*,?\s*", re.IGNORECASE)

ERROR_PATTERNS = [
    re.compile(r"FATAL ERROR.*", re.IGNORECASE),
    re.compile(r"Hardware failure detected.*", re.IGNORECASE),
    re.compile(r"TORTURE TEST FAILED.*", re.IGNORECASE),
    re.compile(r"Rounding was.*expected less than 0\.4", re.IGNORECASE),
    re.compile(r"SUM\(INPUTS\) != SUM\(OUTPUTS\)", re.IGNORECASE),
    re.compile(r"Illegal Sumout", re.IGNORECASE),
]

MODE_CPU_FLAGS: dict[str, dict[str, int]] = {
    "sse": {"CpuSupportsAVX": 0, "CpuSupportsAVX2": 0, "CpuSupportsAVX512F": 0, "CpuSupportsFMA3": 0,
            "CpuSupportsFMA4": 0},
    "avx": {"CpuSupportsAVX": 1, "CpuSupportsAVX2": 0, "CpuSupportsAVX512F": 0, "CpuSupportsFMA3": 0,
            "CpuSupportsFMA4": 0},
    "avx2": {"CpuSupportsAVX": 1, "CpuSupportsAVX2": 1, "CpuSupportsAVX512F": 0, "CpuSupportsFMA3": 1,
             "CpuSupportsFMA4": 0},
    "avx512": {"CpuSupportsAVX": 1, "CpuSupportsAVX2": 1, "CpuSupportsAVX512F": 1, "CpuSupportsFMA3": 1,
               "CpuSupportsFMA4": 0},
}


def parse_fft_size_k(fft_name: str) -> float:
    """Parses a Prime95 FFT length ('36K', '4M', or an element count such as '4608') into K."""
    s = fft_name.strip().upper()
    if s.endswith("M"):
        return float(s[:-1]) * 1024
    if s.endswith("K"):
        return float(s[:-1])
    return int(s) / 1024


def build_fft_config(
    preset: str | None = None,
    min_fft: int | None = None,
    max_fft: int | None = None,
    memory_mb: int | None = None,
    mode: str = "avx2",
) -> FFTConfig:
    """
    Builds an FFTConfig from a preset name, a 'min-max' range string, or explicit bounds.
    Explicit bounds define a custom range on their own: a missing lower bound starts at Prime95's smallest FFT
    and a missing upper bound covers all larger FFTs. Raises ValueError on invalid or conflicting input.
    """
    if min_fft is not None or max_fft is not None:
        if preset is not None:
            raise ValueError("Use either an FFT preset/range or explicit --prime-min-fft/--prime-max-fft bounds")
        lo = min_fft if min_fft is not None else PRIME95_MIN_FFT_K
        hi = max_fft if max_fft is not None else PRIME95_MAX_FFT_K
        base = FFTConfig(lo, hi, 0, f"Custom {lo}K-{hi}K")
    else:
        preset = (preset or "smallest").strip().lower()
        match = RANGE_PATTERN.match(preset)
        if match:
            lo, hi = int(match.group(1)), int(match.group(2))
            base = FFTConfig(lo, hi, 0, f"Range {lo}K-{hi}K")
        elif preset in FFT_PRESETS:
            base = FFT_PRESET_OVERRIDES.get((preset, mode), FFT_PRESETS[preset])
        else:
            raise ValueError(f"Unknown Prime95 FFT preset '{preset}' (choose {', '.join(FFT_PRESETS)} or 'min-max')")

    if not PRIME95_MIN_FFT_K <= base.min_fft <= base.max_fft <= PRIME95_MAX_FFT_K:
        raise ValueError(
            f"Invalid FFT range {base.min_fft}K-{base.max_fft}K "
            f"(required: {PRIME95_MIN_FFT_K} <= min <= max <= {PRIME95_MAX_FFT_K})"
        )

    if memory_mb is None:
        return base
    if memory_mb < 0:
        raise ValueError(f"Prime95 memory must not be negative: {memory_mb}")
    return FFTConfig(base.min_fft, base.max_fft, memory_mb, f"{base.desc} (Mem: {memory_mb}MB)")


def strip_worker_prefix(line: str) -> str:
    r"""Removes 'Worker #\d+', 'Worker', 'Main thread' from bracketed prefix e.g. [Worker 2026-...] -> [2026-...]."""
    return WORKER_PREFIX_PATTERN.sub("[", line)


@dataclass(frozen=True)
class Prime95Params:
    fft: FFTConfig
    test_time_min: int
    mode: str | None


class Prime95OutputParser(OutputParser):
    """
    Counts verified FFT steps and completed passes over the FFT range.
    With SMT both threads report each step ("(thread N of M)"); a step counts once all threads passed it.
    A pass completes when an FFT at or above max_fft passes, or when the FFT size wraps to a smaller one.
    """

    def __init__(self, fft: FFTConfig, target_iterations: int, listener: TestEventListener, results_path: str):
        super().__init__(target_iterations, listener)
        self.fft = fft
        self.results_path = results_path
        self._results_file: TextIO | None = None
        self._thread_passes: dict[int, int] = {}
        self._steps_passed = 0
        self._largest_in_pass: float | None = None

    def clean_line(self, line: str) -> str:
        return strip_worker_prefix(super().clean_line(line))

    def _check_errors(self, line: str) -> None:
        for pattern in ERROR_PATTERNS:
            m = pattern.search(line)
            if m:
                self.add_error(m.group(0))
        summary = SUMMARY_PATTERN.search(line)
        if summary and int(summary.group(1)) > 0:
            self.add_error(summary.group(0))

    def feed(self, line: str) -> None:
        self._check_errors(line)
        m = PASSED_PATTERN.search(line)
        if not m:
            return
        fft_name, thread_idx = m.group(1), m.group(2)
        if thread_idx is not None:
            self._thread_passes[int(thread_idx)] = self._thread_passes.get(int(thread_idx), 0) + 1
            steps = min(self._thread_passes.get(t, 0) for t in range(1, int(m.group(3)) + 1))
            if steps <= self._steps_passed:
                return
            self._steps_passed = steps
        else:
            self._steps_passed += 1

        size_k = parse_fft_size_k(fft_name)
        if self.fft.min_fft == self.fft.max_fft:
            completes = True
        else:
            if self._largest_in_pass is not None and size_k < self._largest_in_pass:
                # The previous pass ended below max_fft (Prime95 has no FFT length equal to max_fft).
                self.completed_iterations += 1
                self._largest_in_pass = None
            completes = size_k >= self.fft.max_fft
            self._largest_in_pass = None if completes else max(self._largest_in_pass or 0.0, size_k)
        self.step_verified(fft_name, completes)

    def poll(self) -> None:
        if self._results_file is None:
            try:
                self._results_file = open(self.results_path, "r", encoding="utf-8", errors="replace")
            except OSError:
                return
        for line in self._results_file:
            self._check_errors(line)

    def close(self) -> None:
        if self._results_file is not None:
            self._results_file.close()
            self._results_file = None


class Prime95Runner(StressRunner):
    """Prime95 (mprime) stress test runner implementation."""

    name = "prime95"
    bundled_binary = "contrib/prime95/mprime"
    binary_name = "mprime"

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Registers Prime95 specific CLI arguments."""
        group = parser.add_argument_group("Prime95 Specific Options")
        group.add_argument(
            "--prime-fft",
            type=str,
            default=None,
            help=f"Prime95 FFT preset ({', '.join(FFT_PRESETS)}) or range (e.g. '36-248') (default: 'smallest')",
        )
        group.add_argument(
            "--prime-min-fft",
            type=int,
            default=None,
            help=f"Custom minimum FFT size in K instead of a preset (default: {PRIME95_MIN_FFT_K})",
        )
        group.add_argument(
            "--prime-max-fft",
            type=int,
            default=None,
            help=f"Custom maximum FFT size in K instead of a preset (default: {PRIME95_MAX_FFT_K})",
        )
        group.add_argument(
            "--prime-memory",
            type=int,
            default=None,
            help="Memory allocation in MB for Prime95 (0 = in-place FFTs) (default: preset value)",
        )
        group.add_argument(
            "--prime-test-time",
            type=str,
            default="1m",
            help="Duration per Prime95 FFT step in whole minutes (e.g. '1m', '2m', '180') (default: '1m')",
        )
        group.add_argument(
            "--prime-mode",
            type=str.lower,
            choices=INSTRUCTION_MODES,
            default=None,
            help="Prime95 instruction set mode (default: highest supported by the CPU)",
        )

    @classmethod
    def parse_parameters(cls, args: argparse.Namespace) -> tuple[Prime95Params, str]:
        """Validates Prime95 CLI arguments into runner parameters and profile description."""
        supported_modes = detect_cpu_instruction_sets()
        mode = args.prime_mode or get_default_instruction_set(supported_modes)
        if mode not in supported_modes:
            raise ValueError(
                f"Instruction set mode '{mode}' is not supported by this CPU (supported: {sorted(supported_modes)})"
            )

        fft = build_fft_config(args.prime_fft, args.prime_min_fft, args.prime_max_fft, args.prime_memory, mode=mode)

        seconds = parse_duration(args.prime_test_time)
        if seconds % 60:
            raise ValueError(f"Prime95 test time must be a whole number of minutes: '{args.prime_test_time}'")

        return Prime95Params(fft=fft, test_time_min=int(seconds // 60), mode=mode), f"{fft.desc} [{mode.upper()}]"

    @staticmethod
    def write_config(work_dir: str, fft: FFTConfig, num_threads: int, test_time_min: int, mode: str | None) -> None:
        """Generates prime.txt ensuring a single torture worker, error checks, and ISO timestamps."""
        mode_block = "".join(f"{k}={v}\n" for k, v in MODE_CPU_FLAGS.get(mode or "", {}).items())
        content = (
            "StressTester=1\n"
            "UsePrimenet=0\n"
            "TortureCores=1\n"
            f"TortureHyperthreading={1 if num_threads > 1 else 0}\n"
            f"MinTortureFFT={fft.min_fft}\n"
            f"MaxTortureFFT={fft.max_fft}\n"
            f"TortureMem={fft.mem_mb}\n"
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
        with open(os.path.join(work_dir, "prime.txt"), "w", encoding="utf-8") as f:
            f.write(content)

    def _prepare(self, request: TestRequest, work_dir: str) -> list[str]:
        params: Prime95Params = request.parameters
        self.write_config(work_dir, params.fft, len(request.cpus), params.test_time_min, params.mode)
        return [self.binary, f"-w{work_dir}", "-t"]

    def _create_parser(self, request: TestRequest, work_dir: str, listener: TestEventListener) -> OutputParser:
        params: Prime95Params = request.parameters
        return Prime95OutputParser(params.fft, request.target_iterations, listener, os.path.join(work_dir, "results.txt"))
