"""
Domain models and data transfer objects for Zen Tuner.
Pure data classes with zero dependencies on terminal formatting or logging.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PhysicalCore:
    """Represents a physical CPU core with its hardware topology attributes."""
    core_idx: int  # 0-indexed sequential core number (Core 0 to Core N-1)
    hardware_core_id: int  # hardware core_id from sysfs (e.g. 0-5, 8-13 on Zen 3)
    ccd_id: int | None  # CCD / L3 cache group index
    logical_cpus: list[int]  # SMT thread siblings (e.g. [0, 12])
    cppc_perf: int | None = None  # ACPI CPPC highest_perf score
    is_preferred: bool = False  # True if this core is the best core in its CCD


@dataclass
class CoreStats:
    """Aggregated test execution statistics for a physical core across cycle runs."""
    passes: int = 0
    failures: int = 0
    verified_tests: int = 0
    total_duration: float = 0.0
    active_errors: list[str] = field(default_factory=list)
    idle_mce_errors: list[str] = field(default_factory=list)
    avg_target_mhz: float | None = None
    avg_effective_mhz: float | None = None
    max_stretch_mhz: float = 0.0
    avg_stretch_mhz: float = 0.0
    stretching_detected: bool = False
    co_offset: int | None = None
    # SMU snapshot metrics captured at end of each test (for results table)
    avg_voltage_v: float | None = None
    avg_power_w: float | None = None
    avg_temp_c: float | None = None


@dataclass(frozen=True)
class MceEvent:
    """Hardware Machine Check Exception or Hardware Error captured from kernel logs."""
    cpu: int | None
    message: str
    is_active_core: bool


@dataclass(frozen=True)
class StretchSample:
    """Instantaneous telemetry sample of clock stretching measured via APERF/MPERF MSRs."""
    cpu: int
    target_mhz: float
    effective_mhz: float
    stretch_mhz: float
    stretch_pct: float


@dataclass
class RunResult:
    """Outcome and telemetry metrics for a single core stress test execution."""
    passed: bool
    status: str  # PASS, ACTIVE_ERROR, IDLE_MCE_ERROR, PROCESS_CRASH, TIMEOUT, UNVERIFIED
    tested_cpus: list[int]
    completed_tests: int
    elapsed_seconds: float
    error_message: str | None = None
    active_errors: list[str] = field(default_factory=list)
    idle_mce_errors: list[MceEvent] = field(default_factory=list)
    verified_ffts: list[str] = field(default_factory=list)
    summary_line: str | None = None
    stretching_detected: bool = False
    max_stretch_mhz: float = 0.0
    avg_stretch_mhz: float = 0.0
    avg_target_mhz: float | None = None
    avg_effective_mhz: float | None = None
    stretch_samples_count: int = 0


@dataclass
class TestRequest:
    """Generic request specifying stress test execution parameters on target CPUs."""
    __test__ = False
    cpus: list[int]
    duration_seconds: float | None = None
    target_iterations: int | None = None
    graceful: bool = True
    parameters: dict[str, Any] = field(default_factory=dict)


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


