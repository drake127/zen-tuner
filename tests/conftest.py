"""
Shared pytest fixtures, factories and test doubles.
"""

import pytest

from lib.models import (
    CoreSmuMetrics,
    CoreStats,
    MceEvent,
    PackageSmuMetrics,
    PhysicalCore,
    RunResult,
    RunStatus,
    SmuSnapshot,
    TelemetrySample,
    TelemetrySummary,
)
from lib.presenter import Presenter
from lib.views import Tone


class RecordingListener:
    """TestEventListener recording every event for assertions."""

    def __init__(self):
        self.lines: list[str] = []
        self.verified: list[tuple[str, int, int]] = []
        self.samples: list[TelemetrySample] = []

    def on_output_line(self, line: str) -> None:
        self.lines.append(line)

    def on_test_verified(self, step_name: str, completed_iterations: int, target_iterations: int) -> None:
        self.verified.append((step_name, completed_iterations, target_iterations))

    def on_telemetry_sample(self, sample: TelemetrySample) -> None:
        self.samples.append(sample)


def make_metrics(**overrides) -> CoreSmuMetrics:
    """PM table metrics of a fully loaded core 0 stretching by 200 MHz."""
    values = dict(core_idx=0, slot_idx=0, ccd_idx=0, is_enabled=True, voltage_v=1.35, power_w=15.25, temp_c=65.0,
                  frequency_mhz=4850.0, effective_mhz=4650.0, c0_pct=99.0, c1_pct=1.0, c6_pct=0.0, co_offset=-25)
    return CoreSmuMetrics(**(values | overrides))


def make_snapshot(slots: list[CoreSmuMetrics]) -> SmuSnapshot:
    package = PackageSmuMetrics(
        socket_power_w=75.0, package_temp_c=70.0, ppt_w=75.0, ppt_limit_w=142.0, tdc_a=45.0, tdc_limit_a=95.0,
        edc_a=80.0, edc_limit_a=140.0, soc_voltage_v=0.9750, vddp_voltage_v=0.8471, vddg_ccd_voltage_v=0.8471,
        vddg_iod_voltage_v=0.8973,
    )
    return SmuSnapshot(cores={s.core_idx: s for s in slots if s.is_enabled}, package=package, pm_version=0x380805,
                       slots=slots)


def make_sample(target: float = 4850.0, effective: float = 4850.0) -> TelemetrySample:
    return TelemetrySample(target_mhz=target, effective_mhz=effective, voltage_v=1.258, power_w=20.5, temp_c=77.0)


def make_summary(target: float = 4850.0, effective: float = 4850.0) -> TelemetrySummary:
    return TelemetrySummary.from_samples([make_sample(target, effective)])


def make_result(status: RunStatus = RunStatus.PASS, **overrides) -> RunResult:
    values = dict(completed_iterations=1 if status == RunStatus.PASS else 0, elapsed_seconds=5.0)
    return RunResult(status=status, **(values | overrides))


class CapturingPresenter(Presenter):
    """Presenter recording emitted messages and the session summary data instead of printing them."""

    def __init__(self, cores: list[PhysicalCore], **kwargs):
        super().__init__(cores, **kwargs)
        self.messages: list[tuple[str, Tone]] = []
        self.session_end: tuple[list[PhysicalCore], dict[int, CoreStats], list[MceEvent]] | None = None

    def _emit(self, text: str, tone: Tone = Tone.DEFAULT) -> None:
        super()._emit(text, tone)
        self.messages.append((text, tone))

    def on_session_end(self, cores, stats, mce_events) -> None:
        self.session_end = (cores, stats, mce_events)

    def texts(self) -> list[str]:
        return [text for text, _ in self.messages]


@pytest.fixture
def listener() -> RecordingListener:
    return RecordingListener()


@pytest.fixture
def two_cores() -> list[PhysicalCore]:
    return [
        PhysicalCore(core_idx=0, hardware_core_id=0, ccd_id=0, logical_cpus=[0, 12]),
        PhysicalCore(core_idx=1, hardware_core_id=1, ccd_id=0, logical_cpus=[1, 13]),
    ]
