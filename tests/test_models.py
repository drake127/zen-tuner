"""
Unit tests for data models, telemetry aggregation and immutability guarantees.
"""

from dataclasses import FrozenInstanceError

import pytest

from lib.models import (
    CoreStats,
    MceEvent,
    PhysicalCore,
    RunResult,
    RunStatus,
    TelemetrySample,
    TelemetrySummary,
    TestRequest,
)


def sample(target: float, effective: float) -> TelemetrySample:
    return TelemetrySample(target_mhz=target, effective_mhz=effective, voltage_v=1.3, power_w=15.0, temp_c=70.0)


def test_physical_core_frozen():
    core = PhysicalCore(core_idx=0, hardware_core_id=0, ccd_id=0, logical_cpus=[0, 12])
    with pytest.raises(FrozenInstanceError):
        core.core_idx = 1


def test_mce_event_frozen():
    event = MceEvent(cpu=0, message="Hardware Error", on_tested_cpu=True)
    with pytest.raises(FrozenInstanceError):
        event.cpu = 1


@pytest.mark.parametrize("effective, stretch", [(4848.0, 0.0), (4870.0, 0.0), (4650.0, 200.0)])
def test_sample_stretch_filters_noise(effective, stretch):
    assert sample(4850.0, effective).stretch_mhz == stretch


def test_summary_median_filters_transient_spikes():
    summary = TelemetrySummary.from_samples([sample(4850.0, 4850.0)] * 58 + [sample(4850.0, 4770.0)] * 2)
    assert summary.stretch_mhz == 0.0
    assert summary.max_stretch_mhz == 80.0
    assert not summary.stretching_detected


def test_summary_detects_persistent_stretching():
    summary = TelemetrySummary.from_samples([sample(4850.0, 4730.0)] * 60)
    assert summary.stretch_mhz == 120.0
    assert summary.stretching_detected


@pytest.mark.parametrize("effective, detected", [(4801.0, False), (4800.0, True)])
def test_summary_threshold_is_50_mhz(effective, detected):
    assert TelemetrySummary.from_samples([sample(4850.0, effective)]).stretching_detected is detected


def test_summary_of_no_samples():
    assert TelemetrySummary.from_samples([]) is None


def test_core_stats_reports_worst_run():
    stats = CoreStats()
    assert stats.telemetry is None
    assert not stats.stretching_detected

    for effective in (4850.0, 4765.0, 4840.0):
        stats.runs_telemetry.append(TelemetrySummary.from_samples([sample(4850.0, effective)]))
    assert stats.telemetry.stretch_mhz == 85.0
    assert stats.stretching_detected


def test_run_result_passed_follows_status():
    res = RunResult(status=RunStatus.PASS, tested_cpus=[0, 12], completed_iterations=1, elapsed_seconds=10.5)
    assert res.passed
    res.status = RunStatus.INTERRUPTED
    assert not res.passed


def test_test_request_defaults():
    req = TestRequest(cpus=[0, 12])
    assert req.target_iterations == 1
    assert req.core_idx is None
