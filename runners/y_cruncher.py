"""
y-cruncher execution engine for single-core and multi-core stability testing.
Generates stress configuration files and verifies completed algorithms without presentation coupling.

y-cruncher is proprietary software by Alexander J. Yee (see contrib/y-cruncher/Read Me.txt).
It is invoked as an external process.
"""

import argparse
from dataclasses import dataclass
import os
import re
import signal

from lib.events import TestEventListener
from lib.models import TestRequest
from runners.base import OutputParser, StressRunner, parse_duration

# Stress test components accepted by y-cruncher (see contrib/y-cruncher/Command Lines.txt)
VALID_ALGORITHMS = ("BKT", "BBP", "SFTv4", "SNT", "SVT", "FFTv4", "NTT63", "N63", "VSTv3", "VT3")

ALGORITHM_PRESETS: dict[str, tuple[str, ...]] = {
    "breadpit": ("BBP", "SFTv4", "VT3"),
    "fast": ("BKT", "BBP", "SFTv4"),
    "all": ("BKT", "BBP", "SFTv4", "SNT", "SVT", "FFTv4", "N63", "VT3"),
}

# In-cache allocation matching CoreCycler defaults (bytes)
DEFAULT_MEMORY_1T = 13418572
DEFAULT_MEMORY_2T = 26567600

PASSED_PATTERN = re.compile(r"Running\s+([A-Za-z0-9_]+):\s+Passed", re.IGNORECASE)
FAILED_PATTERN = re.compile(r"Running\s+([A-Za-z0-9_]+):\s+Failed", re.IGNORECASE)
# Only "Exception Encountered" was observed on the bundled build (invalid configuration). Any other failure makes
# y-cruncher stop on its own (StopOnError), which ends the run as UNVERIFIED with the full engine output reported.
ERROR_PATTERNS = [
    re.compile(r"Stress test failed with\s+(\d+)\s+error", re.IGNORECASE),
    re.compile(r"Exception Encountered:\s*(\w+)", re.IGNORECASE),
]


def parse_algorithms(value: str) -> tuple[str, ...]:
    """Resolves a preset name or comma-separated algorithm list into canonical y-cruncher test tags."""
    key = value.strip().lower()
    if key in ALGORITHM_PRESETS:
        return ALGORITHM_PRESETS[key]

    canonical = {a.lower(): a for a in VALID_ALGORITHMS}
    algorithms: list[str] = []
    for item in (a.strip() for a in value.split(",")):
        if not item:
            continue
        if item.lower() not in canonical:
            raise ValueError(
                f"Unknown y-cruncher algorithm '{item}' "
                f"(presets: {', '.join(ALGORITHM_PRESETS)}; algorithms: {', '.join(VALID_ALGORITHMS)})"
            )
        algorithms.append(canonical[item.lower()])
    if not algorithms:
        raise ValueError("No y-cruncher algorithms selected")
    return tuple(algorithms)


@dataclass(frozen=True)
class YCruncherParams:
    algorithms: tuple[str, ...]
    seconds_per_test: int
    memory_mb: int | None


class YCruncherOutputParser(OutputParser):
    """Counts passed algorithms; one iteration is a pass over all configured algorithms."""

    def __init__(self, algorithm_count: int, target_iterations: int, listener: TestEventListener):
        super().__init__(target_iterations, listener)
        self.algorithm_count = algorithm_count

    def feed(self, line: str) -> None:
        if FAILED_PATTERN.search(line):
            self.add_error(f"Algorithm failure: {line}")
        for pattern in ERROR_PATTERNS:
            if pattern.search(line):
                self.add_error(line)

        m = PASSED_PATTERN.search(line)
        if m:
            self.step_verified(m.group(1), (self.steps_verified + 1) % self.algorithm_count == 0)


class YCruncherRunner(StressRunner):
    """y-cruncher stress test runner implementation."""

    name = "y-cruncher"
    # The launcher selects the CPU-tuned binary from Binaries/ itself.
    bundled_binary = "contrib/y-cruncher/y-cruncher"
    binary_name = "y-cruncher"
    # y-cruncher ignores SIGINT while stress testing; SIGTERM ends it immediately.
    stop_signal = signal.SIGTERM
    # Output is not flushed line by line when stdout is a pipe.
    use_pty = True

    @classmethod
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None:
        """Registers y-cruncher specific CLI arguments."""
        group = parser.add_argument_group("y-cruncher Specific Options")
        group.add_argument(
            "--yc-algorithms",
            type=str,
            default="breadpit",
            help=f"y-cruncher preset ({', '.join(ALGORITHM_PRESETS)}) or list like 'BBP,SFTv4,VT3' (default: 'breadpit')",
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
            help="Duration per y-cruncher algorithm in whole seconds (e.g. '60', '30s', '2m') (default: '60s')",
        )

    @classmethod
    def parse_parameters(cls, args: argparse.Namespace) -> tuple[YCruncherParams, str]:
        """Validates y-cruncher CLI arguments into runner parameters and profile description."""
        algorithms = parse_algorithms(args.yc_algorithms)

        seconds = parse_duration(args.yc_test_time)
        if seconds != int(seconds):
            raise ValueError(f"y-cruncher test time must be a whole number of seconds: '{args.yc_test_time}'")

        if args.yc_memory < 0:
            raise ValueError(f"y-cruncher memory must not be negative: {args.yc_memory}")

        params = YCruncherParams(
            algorithms=algorithms,
            seconds_per_test=int(seconds),
            memory_mb=args.yc_memory or None,
        )
        return params, f"y-cruncher [{','.join(algorithms)}]"

    @staticmethod
    def write_config(work_dir: str, cpus: list[int], params: YCruncherParams) -> str:
        """Generates stressTest.cfg; SecondsTotal 0 runs until the supervisor stops it after the last iteration."""
        if params.memory_mb:
            mem_bytes = params.memory_mb * 1024 * 1024
        else:
            mem_bytes = DEFAULT_MEMORY_2T if len(cpus) > 1 else DEFAULT_MEMORY_1T

        test_lines = "\n".join(f'            "{algo}"' for algo in params.algorithms)
        cfg_content = (
            "{\n"
            '    Action : "StressTest"\n'
            "    StressTest : {\n"
            '        AllocateLocally : "true"\n'
            f"        LogicalCores : [{' '.join(str(c) for c in cpus)}]\n"
            f"        TotalMemory : {mem_bytes}\n"
            f"        SecondsPerTest : {params.seconds_per_test}\n"
            "        SecondsTotal : 0\n"
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

    def _prepare(self, request: TestRequest, work_dir: str) -> list[str]:
        cfg_path = self.write_config(work_dir, request.cpus, request.parameters)
        return [self.binary, "pause:-2", "skip-warnings", "colors:0", "status:none", "config", cfg_path]

    def _create_parser(self, request: TestRequest, work_dir: str, listener: TestEventListener) -> OutputParser:
        params: YCruncherParams = request.parameters
        return YCruncherOutputParser(len(params.algorithms), request.target_iterations, listener)
