"""
Unit tests for data models, telemetry aggregation and immutability guarantees.
"""

from dataclasses import FrozenInstanceError

import pytest

from conftest import make_metrics, make_result, make_sample, make_summary
from lib.models import CoreStats, FailedRun, MceEvent, PhysicalCore, RunStatus, TelemetrySample, TelemetrySummary, TestRequest


def test_physical_core_frozen():
    core = PhysicalCore(core_idx=0, hardware_core_id=0, ccd_id=0, logical_cpus=[0, 12])
    with pytest.raises(FrozenInstanceError):
        core.core_idx = 1


def test_mce_event_frozen():
    event = MceEvent(cpu=0, message="Hardware Error")
    with pytest.raises(FrozenInstanceError):
        event.cpu = 1


@pytest.mark.parametrize("effective, stretch", [(4848.0, 0.0), (4870.0, 0.0), (4650.0, 200.0)])
def test_sample_stretch_filters_noise(effective, stretch):
    assert make_sample(4850.0, effective).stretch_mhz == stretch


def test_sample_from_loaded_metrics():
    sample = TelemetrySample.from_metrics(make_metrics())
    assert (sample.target_mhz, sample.effective_mhz, sample.voltage_v, sample.stretch_mhz) == (4850.0, 4650.0, 1.35, 200.0)


@pytest.mark.parametrize(
    "overrides", [{"c0_pct": 94.9}, {"frequency_mhz": 2400.0}, {"is_enabled": False}],
    ids=["light-load", "low-clock", "disabled"],
)
def test_no_sample_without_full_load(overrides):
    assert TelemetrySample.from_metrics(make_metrics(**overrides)) is None


def test_summary_median_filters_transient_spikes():
    summary = TelemetrySummary.from_samples([make_sample(4850.0, 4850.0)] * 58 + [make_sample(4850.0, 4770.0)] * 2)
    assert summary.stretch_mhz == 0.0
    assert summary.max_stretch_mhz == 80.0
    assert not summary.stretching_detected


def test_summary_detects_persistent_stretching():
    summary = TelemetrySummary.from_samples([make_sample(4850.0, 4730.0)] * 60)
    assert summary.stretch_mhz == 120.0
    assert summary.stretching_detected


@pytest.mark.parametrize("effective, detected", [(4801.0, False), (4800.0, True)])
def test_summary_threshold_is_50_mhz(effective, detected):
    assert make_summary(4850.0, effective).stretching_detected is detected


def test_summary_of_no_samples():
    assert TelemetrySummary.from_samples([]) is None


def test_core_stats_reports_worst_run():
    stats = CoreStats()
    assert stats.telemetry is None
    assert not stats.stretching_detected

    stats.runs_telemetry.extend(make_summary(4850.0, effective) for effective in (4850.0, 4765.0, 4840.0))
    assert stats.telemetry.stretch_mhz == 85.0
    assert stats.stretching_detected


def test_core_stats_failures_count_failed_runs():
    stats = CoreStats()
    stats.failed_runs.append(FailedRun(cycle_num=1, runner_name="prime95", result=make_result(RunStatus.ERROR)))
    assert stats.failures == 1


def test_run_result_passed_follows_status():
    res = make_result(RunStatus.PASS)
    assert res.passed
    res.status = RunStatus.INTERRUPTED
    assert not res.passed


def test_test_request_defaults():
    req = TestRequest(cpus=[0, 12])
    assert req.target_iterations == 1
    assert req.core_idx is None
