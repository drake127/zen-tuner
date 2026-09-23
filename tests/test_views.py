"""
Unit tests for presentation view models: table rows, drop formatting and the summary table.
"""

import pytest

from lib.models import CoreSmuMetrics, CoreStats, MceEvent, PhysicalCore, TelemetrySample, TelemetrySummary
from lib.ui import strip_ansi
from lib.views import (
    LIVE_COLUMNS_WIDE,
    Tone,
    basic_core_row,
    column_offset,
    fmt_drop,
    header_line,
    join_cells,
    live_core_row,
    render_summary,
)


def metrics(**overrides) -> CoreSmuMetrics:
    values = dict(core_idx=0, slot_idx=0, ccd_idx=0, is_enabled=True, voltage_v=1.35, power_w=15.25, temp_c=65.0,
                  frequency_mhz=4850.0, effective_mhz=4650.0, c0_pct=99.0, c1_pct=1.0, c6_pct=0.0, co_offset=-25)
    return CoreSmuMetrics(**(values | overrides))


def summary(target: float, effective: float) -> TelemetrySummary:
    return TelemetrySummary.from_samples(
        [TelemetrySample(target_mhz=target, effective_mhz=effective, voltage_v=1.258, power_w=20.5, temp_c=77.0)]
    )


@pytest.mark.parametrize("stretch, text", [(None, "--"), (0.0, "0M"), (49.9, "0M"), (50.0, "-50M"), (200.0, "-200M")])
def test_fmt_drop(stretch, text):
    assert fmt_drop(stretch) == text


def test_header_and_row_share_column_layout():
    row = live_core_row(metrics(), None, CoreStats(), is_active=False)
    line = join_cells(LIVE_COLUMNS_WIDE, row.cells)
    header = header_line(LIVE_COLUMNS_WIDE)
    assert len(line) == len(header)
    status_x = column_offset(LIVE_COLUMNS_WIDE, "Status")
    assert header[status_x:].startswith("Status")
    assert line[status_x:].startswith("IDLE")


def test_live_row_of_active_core_shows_live_drop():
    row = live_core_row(metrics(), None, CoreStats(), is_active=True)
    assert row.is_active
    assert (row.cells["Status"], row.status_tone) == ("ACTIVE", Tone.ACTIVE)
    assert (row.cells["Clock"], row.cells["Target"], row.cells["Drop"]) == ("4650", "4850", "-200M")
    assert (row.cells["CO"], row.cells["Power"]) == ("-25", "15.25W")


@pytest.mark.parametrize("effective, c0, drop", [(4848.0, 99.0, "0M"), (4831.0, 99.0, "0M"), (4650.0, 50.0, "0M")])
def test_live_row_filters_noise_and_light_load(effective, c0, drop):
    row = live_core_row(metrics(effective_mhz=effective, c0_pct=c0), None, CoreStats(), is_active=True)
    assert row.cells["Drop"] == drop


def test_live_row_of_tested_core_shows_run_telemetry():
    stats = CoreStats(passes=3, runs_telemetry=[summary(4834.0, 4750.0)])
    row = live_core_row(metrics(), None, stats, is_active=False)
    assert (row.cells["Status"], row.status_tone) == ("PASS", Tone.PASS)
    assert (row.cells["Pass"], row.cells["Target"], row.cells["Drop"]) == ("3", "4834", "-84M")


def test_live_row_of_untested_and_disabled_slots():
    assert live_core_row(metrics(), None, CoreStats(), is_active=False).cells["Drop"] == "--"
    disabled = live_core_row(metrics(core_idx=None, is_enabled=False), None, None, is_active=False)
    assert disabled.is_disabled
    assert disabled.cells["Status"] == "DISABLED"


@pytest.mark.parametrize("rank, label, tone", [(1, "*  0", Tone.WARN), (2, "*  0", Tone.SILVER), (3, "   0", None)])
def test_preferred_core_star(rank, label, tone):
    core = PhysicalCore(0, 0, 0, [0, 12], pref_rank=rank)
    row = basic_core_row(core, CoreStats(passes=1), is_active=False)
    assert row.cells["Core"] == label
    assert row.rank_tone is tone
    # The star keeps its own tone; the status keeps the PASS tone
    assert row.status_tone is Tone.PASS


def test_basic_row_failure_status():
    row = basic_core_row(PhysicalCore(0, 0, 0, [0, 12]), CoreStats(failures=2), is_active=False)
    assert (row.cells["Status"], row.status_tone, row.cells["Fail"]) == ("FAIL(2)", Tone.FAIL, "2")


@pytest.fixture
def summary_cores() -> list[PhysicalCore]:
    return [
        PhysicalCore(0, 0, 0, [0, 12], pref_rank=1),
        PhysicalCore(1, 1, 0, [1, 13], pref_rank=2),
        PhysicalCore(2, 2, 0, [2, 14]),
        PhysicalCore(3, 8, 1, [3, 15]),
    ]


def test_summary_table(summary_cores):
    stats = {
        0: CoreStats(passes=1, runs_telemetry=[summary(4850.0, 4831.0)]),
        1: CoreStats(passes=1, runs_telemetry=[summary(4850.0, 4700.0)]),
        2: CoreStats(failures=1, errors=["FATAL ERROR"]),
        3: CoreStats(),
    }
    raw = render_summary(summary_cores, stats, co_offsets={0: -20, 3: -15})
    lines = [strip_ansi(line) for line in raw]
    rows = {line.split("║")[1].strip(): line for line in lines if line.startswith("║ ") and "║" in line[1:]}

    assert "-20" in rows["*  0"] and "PASS " in rows["*  0"]
    # 19 MHz median drop is below the stretching threshold
    assert "0M" in rows["*  0"] and "-19M" not in rows["*  0"]
    assert "-150M" in rows["*  1"] and "PASS (STRETCH)" in rows["*  1"]
    assert "1.2580V" in rows["*  1"] and "20.50W" in rows["*  1"] and "77°C" in rows["*  1"]
    assert "FAIL (1)" in rows["2"]
    assert "-15" in rows["3"] and "SKIPPED" in rows["3"]
    assert any("── CCD 1" in line for line in lines)
    # Gold and silver stars are coloured independently of the row
    assert any("\033[1m\033[33m*\033[0m" in line for line in raw)
    assert any("\033[1m\033[37m*\033[0m" in line for line in raw)
    # All table lines have the same width
    table = [line for line in lines if line[:1] in "╔║╠╚"]
    assert len({len(line) for line in table}) == 1


def test_summary_lists_hardware_errors(summary_cores):
    stats = {c.core_idx: CoreStats() for c in summary_cores}
    stats[2].mce_events.append(MceEvent(cpu=14, message="mce: [Hardware Error]: CPU 14: Machine Check", on_tested_cpu=True))
    stats[2].mce_events.append(MceEvent(cpu=None, message="[Hardware Error]: Corrected error", on_tested_cpu=None))
    lines = [strip_ansi(line) for line in render_summary(summary_cores, stats)]
    assert "Hardware errors reported by the kernel during the session (2):" in lines
    assert "  [while testing Core 2] CPU 14: mce: [Hardware Error]: CPU 14: Machine Check" in lines
    assert "  [while testing Core 2] CPU ?: [Hardware Error]: Corrected error" in lines
