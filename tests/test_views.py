"""
Unit tests for presentation view models: table rows, statuses, drop formatting and the session summary.
"""

import pytest

from conftest import make_metrics, make_result, make_summary
from lib.models import CoreStats, FailedRun, MceEvent, PhysicalCore, RunStatus
from lib.ui import strip_ansi
from lib.views import (
    LIVE_COLUMNS_NARROW,
    LIVE_COLUMNS_WIDE,
    Tone,
    basic_core_row,
    column_offset,
    core_status,
    describe_mce,
    fmt_drop,
    header_line,
    join_cells,
    live_core_row,
    render_summary,
)


@pytest.mark.parametrize("stretch, text", [(None, "--"), (0.0, "0M"), (49.9, "0M"), (50.0, "-50M"), (200.0, "-200M")])
def test_fmt_drop(stretch, text):
    assert fmt_drop(stretch) == text


@pytest.mark.parametrize(
    "stats, active, final, expected",
    [
        (CoreStats(), True, False, ("ACTIVE", Tone.ACTIVE)),
        (CoreStats(), False, False, ("IDLE", Tone.DEFAULT)),
        (CoreStats(), False, True, ("SKIPPED", Tone.DISABLED)),
        (CoreStats(passes=1), False, False, ("PASS", Tone.PASS)),
        (CoreStats(passes=1, runs_telemetry=[make_summary(4850.0, 4700.0)]), False, True, ("STRETCH", Tone.WARN)),
        (CoreStats(failed_runs=[FailedRun(1, "prime95", make_result(RunStatus.ERROR))]), False, True,
         ("FAIL(1)", Tone.FAIL)),
    ],
    ids=["active", "idle", "skipped", "pass", "stretch", "fail"],
)
def test_core_status(stats, active, final, expected):
    assert core_status(stats, active, final) == expected


@pytest.mark.parametrize(
    "event, text",
    [
        (MceEvent(cpu=13, message="", core_idx=1, tested_core_idx=0), "CPU 13 (Core 1) while testing Core 0"),
        (MceEvent(cpu=None, message="", tested_core_idx=2), "CPU ? while testing Core 2"),
        (MceEvent(cpu=None, message=""), "CPU ?"),
    ],
)
def test_describe_mce(event, text):
    assert describe_mce(event) == text


@pytest.mark.parametrize("columns", [LIVE_COLUMNS_WIDE, LIVE_COLUMNS_NARROW], ids=["wide", "narrow"])
def test_header_and_row_share_column_layout(columns):
    line = join_cells(columns, live_core_row(make_metrics(), None, CoreStats(), is_active=False).cells)
    header = header_line(columns)
    assert len(line) == len(header)
    status_x = column_offset(columns, "Status")
    assert header[status_x:].startswith("Status")
    assert line[status_x:].startswith("IDLE")


def test_narrow_layout_fits_medium_terminals():
    # 95-119 column terminals get a 61 column pane with 59 usable columns
    assert len(header_line(LIVE_COLUMNS_NARROW)) <= 59
    assert len(header_line(LIVE_COLUMNS_WIDE)) <= 78


def test_live_row_of_active_core_shows_current_run_drop():
    row = live_core_row(make_metrics(), None, CoreStats(), is_active=True, run_stretch_mhz=120.0)
    assert row.is_active
    assert (row.cells["Status"], row.status_tone) == ("ACTIVE", Tone.ACTIVE)
    assert (row.cells["Clock"], row.cells["Target"], row.cells["Drop"]) == ("4650", "4850", "-120M")
    assert (row.cells["CO"], row.cells["Power"]) == ("-25", "15.25W")
    assert live_core_row(make_metrics(), None, CoreStats(), is_active=True).cells["Drop"] == "--"


def test_live_row_of_tested_core_shows_live_values_and_run_result():
    stats = CoreStats(passes=3, runs_telemetry=[make_summary(4834.0, 4750.0)])
    row = live_core_row(make_metrics(frequency_mhz=3600.0, effective_mhz=350.0, c0_pct=1.0), None, stats,
                        is_active=False)
    assert (row.cells["Status"], row.status_tone) == ("STRETCH", Tone.WARN)
    assert (row.cells["Pass"], row.cells["Drop"]) == ("3", "-84M")
    # All other columns are live readings
    assert (row.cells["Clock"], row.cells["Target"], row.cells["C0%"]) == ("350", "3600", "  1%")


def test_live_row_of_untested_and_disabled_slots():
    assert live_core_row(make_metrics(), None, CoreStats(), is_active=False).cells["Drop"] == "--"
    disabled = live_core_row(make_metrics(core_idx=None, is_enabled=False), None, None, is_active=False)
    assert disabled.is_disabled
    assert disabled.cells["Status"] == "DISABLED"


@pytest.mark.parametrize("rank, label, tone", [(1, "*  0", Tone.WARN), (2, "*  0", Tone.SILVER), (3, "   0", None)])
def test_preferred_core_star(rank, label, tone):
    row = basic_core_row(PhysicalCore(0, 0, 0, [0, 12], pref_rank=rank), CoreStats(passes=1), is_active=False)
    assert row.cells["Core"] == label
    assert row.rank_tone is tone
    # The star keeps its own tone; the status keeps the PASS tone
    assert row.status_tone is Tone.PASS


@pytest.fixture
def summary_cores() -> list[PhysicalCore]:
    return [
        PhysicalCore(0, 0, 0, [0, 12], pref_rank=1),
        PhysicalCore(1, 1, 0, [1, 13], pref_rank=2),
        PhysicalCore(2, 2, 0, [2, 14]),
        PhysicalCore(3, 8, 1, [3, 15]),
    ]


def summary_rows(lines: list[str]) -> dict[str, str]:
    return {line.split("║")[1].strip(): line for line in lines if line.startswith("║ ") and "║" in line[1:]}


def test_summary_table(summary_cores):
    failure = make_result(RunStatus.ERROR, error_message="FATAL ERROR")
    stats = {
        0: CoreStats(passes=1, runs_telemetry=[make_summary(4850.0, 4831.0)]),
        1: CoreStats(passes=1, runs_telemetry=[make_summary(4850.0, 4700.0)]),
        2: CoreStats(failed_runs=[FailedRun(1, "prime95", failure)]),
        3: CoreStats(),
    }
    raw = render_summary(summary_cores, stats, co_offsets={0: -20, 3: -15})
    lines = [strip_ansi(line) for line in raw]
    rows = summary_rows(lines)

    assert "-20" in rows["*  0"] and "PASS " in rows["*  0"]
    # 19 MHz median drop is below the stretching threshold
    assert "0M" in rows["*  0"] and "-19M" not in rows["*  0"]
    assert "-150M" in rows["*  1"] and "STRETCH" in rows["*  1"]
    assert "1.2580V" in rows["*  1"] and "20.50W" in rows["*  1"] and "77°C" in rows["*  1"]
    assert "FAIL(1)" in rows["2"]
    assert "-15" in rows["3"] and "SKIPPED" in rows["3"]
    assert any("── CCD 1" in line for line in lines)
    # Gold and silver stars are coloured independently of the row
    assert any("\033[1m\033[33m*\033[0m" in line for line in raw)
    assert any("\033[1m\033[37m*\033[0m" in line for line in raw)
    table = [line for line in lines if line and line[0] in "╔║╠╚"]
    assert len({len(line) for line in table}) == 1


def test_summary_lists_failures_with_engine_output(summary_cores):
    stats = {c.core_idx: CoreStats() for c in summary_cores}
    failure = make_result(RunStatus.CRASH, error_message="Process exited abnormally with code 3",
                          output=["Worker starting", "Unexpected message"], work_dir="/tmp/zen_tuner_prime95_x")
    stats[2].failed_runs.append(FailedRun(cycle_num=2, runner_name="prime95", result=failure))
    lines = [strip_ansi(line) for line in render_summary(summary_cores, stats)]

    start = lines.index("Failures (1):")
    assert lines[start + 1:start + 6] == [
        "  Core 2 - cycle 2, prime95: CRASH: Process exited abnormally with code 3",
        "    Work directory: /tmp/zen_tuner_prime95_x",
        "    Engine output (2 lines):",
        "    | Worker starting",
        "    | Unexpected message",
    ]


def test_summary_lists_hardware_errors(summary_cores):
    stats = {c.core_idx: CoreStats() for c in summary_cores}
    events = [
        MceEvent(cpu=14, message="mce: [Hardware Error]: CPU 14: Machine Check", core_idx=2, tested_core_idx=2),
        MceEvent(cpu=None, message="[Hardware Error]: Corrected error", tested_core_idx=2),
    ]
    lines = [strip_ansi(line) for line in render_summary(summary_cores, stats, mce_events=events)]
    assert "Hardware errors reported by the kernel during the session (2):" in lines
    assert "  CPU 14 (Core 2) while testing Core 2: mce: [Hardware Error]: CPU 14: Machine Check" in lines
    assert "  CPU ? while testing Core 2: [Hardware Error]: Corrected error" in lines
    assert not any(line.startswith("Failures") for line in lines)
