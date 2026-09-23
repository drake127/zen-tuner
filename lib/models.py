"""
Domain models and data transfer objects for Zen Tuner.
Pure data classes with zero dependencies on terminal formatting or logging.
"""

from dataclasses import dataclass, field
from enum import StrEnum
import statistics
from typing import Any

# Clock drops at or below this value are measurement noise between the SMU clock and effective clock readings.
STRETCH_NOISE_MHZ: float = 5.0

# Median clock drop at or above this value is reported as clock stretching.
STRETCH_THRESHOLD_MHZ: float = 50.0

# A core counts as fully loaded at a boost clock only above these values; target vs. effective clock differences
# under lighter load are meaningless.
LOAD_C0_PCT_MIN: float = 95.0
LOAD_FREQ_MHZ_MIN: float = 2500.0


@dataclass(frozen=True)
class PhysicalCore:
    """Represents a physical CPU core with its hardware topology attributes."""
    core_idx: int  # 0-indexed sequential core number (Core 0 to Core N-1)
    hardware_core_id: int  # hardware core_id from sysfs (e.g. 0-5, 8-13 on Zen 3)
    ccd_id: int | None  # CCD / L3 cache group index
    logical_cpus: list[int]  # SMT thread siblings (e.g. [0, 12])
    cppc_perf: int | None = None  # ACPI CPPC highest_perf score
    pref_rank: int | None = None  # CPPC ranking within CCD (1 = gold, 2 = silver)


class RunStatus(StrEnum):
    """Outcome of a single core stress test execution."""
    PASS = "PASS"
    ERROR = "ERROR"  # stress engine reported a computation error
    CRASH = "CRASH"  # stress process terminated abnormally
    UNVERIFIED = "UNVERIFIED"  # process ended before completing the requested iterations
    INTERRUPTED = "INTERRUPTED"  # cancelled by the user


@dataclass(frozen=True)
class MceEvent:
    """Hardware Machine Check Exception or Hardware Error line captured from kernel logs."""
    cpu: int | None  # logical CPU named by the kernel line, if any
    message: str
    timestamp_s: float | None = None  # kernel log timestamp in seconds since boot (see monitors.kernel_clock)
    core_idx: int | None = None  # physical core owning cpu
    tested_core_idx: int | None = None  # core under test (or last tested) when the event was logged


@dataclass(frozen=True)
class TelemetrySample:
    """Per-core SMU telemetry sampled while the core is fully loaded."""
    target_mhz: float
    effective_mhz: float
    voltage_v: float
    power_w: float
    temp_c: float

    @property
    def stretch_mhz(self) -> float:
        drop = self.target_mhz - self.effective_mhz
        return drop if drop > STRETCH_NOISE_MHZ else 0.0

    @classmethod
    def from_metrics(cls, metrics: "CoreSmuMetrics") -> "TelemetrySample | None":
        """Sample of a core's PM table metrics, or None when the core is not fully loaded at a boost clock."""
        if not metrics.is_enabled or metrics.c0_pct < LOAD_C0_PCT_MIN or metrics.frequency_mhz < LOAD_FREQ_MHZ_MIN:
            return None
        return cls(
            target_mhz=metrics.frequency_mhz,
            effective_mhz=metrics.effective_mhz,
            voltage_v=metrics.voltage_v,
            power_w=metrics.power_w,
            temp_c=metrics.temp_c,
        )


@dataclass(frozen=True)
class TelemetrySummary:
    """Median telemetry of a single test run; medians suppress transient spikes (e.g. FFT size switches)."""
    target_mhz: float
    effective_mhz: float
    stretch_mhz: float
    max_stretch_mhz: float
    voltage_v: float
    power_w: float
    temp_c: float

    @property
    def stretching_detected(self) -> bool:
        return self.stretch_mhz >= STRETCH_THRESHOLD_MHZ

    @classmethod
    def from_samples(cls, samples: list[TelemetrySample]) -> "TelemetrySummary | None":
        if not samples:
            return None
        return cls(
            target_mhz=statistics.median(s.target_mhz for s in samples),
            effective_mhz=statistics.median(s.effective_mhz for s in samples),
            stretch_mhz=statistics.median(s.stretch_mhz for s in samples),
            max_stretch_mhz=max(s.stretch_mhz for s in samples),
            voltage_v=statistics.median(s.voltage_v for s in samples),
            power_w=statistics.median(s.power_w for s in samples),
            temp_c=statistics.median(s.temp_c for s in samples),
        )


@dataclass
class RunResult:
    """Outcome and telemetry for a single core stress test execution."""
    status: RunStatus
    completed_iterations: int
    elapsed_seconds: float
    error_message: str | None = None
    telemetry: TelemetrySummary | None = None
    output: list[str] = field(default_factory=list)  # cleaned engine output of the run
    work_dir: str | None = None  # kept engine work directory of a failed run

    @property
    def passed(self) -> bool:
        return self.status == RunStatus.PASS


@dataclass(frozen=True)
class FailedRun:
    """A failed core run together with the context it ran in."""
    cycle_num: int
    runner_name: str
    result: RunResult


@dataclass
class CoreStats:
    """Aggregated test execution statistics for a physical core across cycle runs."""
    passes: int = 0
    verified_iterations: int = 0
    total_duration: float = 0.0
    failed_runs: list[FailedRun] = field(default_factory=list)
    runs_telemetry: list[TelemetrySummary] = field(default_factory=list)

    @property
    def failures(self) -> int:
        return len(self.failed_runs)

    @property
    def telemetry(self) -> TelemetrySummary | None:
        """Telemetry of the run with the worst median clock stretch (latest run wins ties)."""
        worst: TelemetrySummary | None = None
        for summary in self.runs_telemetry:
            if worst is None or summary.stretch_mhz >= worst.stretch_mhz:
                worst = summary
        return worst

    @property
    def stretching_detected(self) -> bool:
        summary = self.telemetry
        return summary is not None and summary.stretching_detected


@dataclass
class TestRequest:
    """Generic request specifying stress test execution parameters on target CPUs."""
    __test__ = False
    cpus: list[int]
    target_iterations: int = 1
    core_idx: int | None = None
    parameters: Any = None  # runner-specific parameters object produced by StressRunner.parse_parameters


@dataclass(frozen=True)
class CoreSmuMetrics:
    """Per-core telemetry metrics extracted from the AMD SMU Power Management table."""
    core_idx: int | None
    voltage_v: float
    power_w: float
    temp_c: float
    frequency_mhz: float
    effective_mhz: float
    c0_pct: float
    c1_pct: float = 0.0
    c6_pct: float = 0.0
    slot_idx: int | None = None
    ccd_idx: int = 0
    is_enabled: bool = True
    co_offset: int | None = None


@dataclass(frozen=True)
class PackageSmuMetrics:
    """Socket-level power, PBO limits, and voltage rails from the AMD SMU PM table."""
    socket_power_w: float
    package_temp_c: float
    ppt_w: float
    ppt_limit_w: float
    tdc_a: float
    tdc_limit_a: float
    edc_a: float
    edc_limit_a: float
    soc_voltage_v: float | None = None
    vddp_voltage_v: float | None = None
    vddg_ccd_voltage_v: float | None = None
    vddg_iod_voltage_v: float | None = None


@dataclass(frozen=True)
class SmuSnapshot:
    """Complete snapshot of SMU telemetry at a given point in time."""
    cores: dict[int, CoreSmuMetrics]
    package: PackageSmuMetrics
    pm_version: int
    slots: list[CoreSmuMetrics] = field(default_factory=list)
