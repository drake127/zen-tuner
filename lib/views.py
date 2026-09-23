"""
Presentation view models shared by the live dashboard and the final summary table.
Turns cores, statistics and SMU metrics into formatted, tone-annotated table cells without any terminal I/O.
"""

from dataclasses import dataclass
from enum import Enum

from lib.models import CoreSmuMetrics, CoreStats, MceEvent, PhysicalCore, STRETCH_THRESHOLD_MHZ
from lib.ui import BOLD, CYAN, DIM, GREEN, MAGENTA, RED, RESET, WHITE, YELLOW


class Tone(Enum):
    """Semantic colour of a log line or table cell; each presenter maps it to its own palette."""
    DEFAULT = "default"
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    ACTIVE = "active"
    HEADER = "header"
    DISABLED = "disabled"
    SILVER = "silver"


ANSI_TONES: dict[Tone, str] = {
    Tone.DEFAULT: "",
    Tone.PASS: GREEN,
    Tone.FAIL: RED,
    Tone.WARN: YELLOW,
    Tone.ACTIVE: CYAN,
    Tone.HEADER: MAGENTA,
    Tone.DISABLED: DIM,
    Tone.SILVER: WHITE,
}


@dataclass(frozen=True)
class Column:
    header: str
    width: int
    align: str = ">"

    def fmt(self, value: str) -> str:
        return f"{value:{self.align}{self.width}}"[: self.width]


@dataclass(frozen=True)
class CoreRow:
    """One formatted core row; cells are keyed by column header."""
    cells: dict[str, str]
    status_tone: Tone
    rank_tone: Tone | None = None
    is_active: bool = False
    is_disabled: bool = False


# Result columns (Status, Pass, Drop) come first; the remaining live columns show current SMU readings.
LIVE_COLUMNS_WIDE = (
    Column("Core", 4), Column("Status", 8, "<"), Column("Pass", 4), Column("Drop", 5), Column("CO", 4),
    Column("Volt", 6), Column("Power", 6), Column("Temp", 5), Column("Clock", 5), Column("Target", 6),
    Column("C0%", 4), Column("C1%", 4), Column("C6%", 4),
)
LIVE_COLUMNS_NARROW = (
    Column("Core", 4), Column("Status", 8, "<"), Column("Pass", 4), Column("Drop", 5), Column("Volt", 6),
    Column("Temp", 5), Column("Clock", 5), Column("C0%", 4), Column("C1%", 4), Column("C6%", 4),
)
BASIC_COLUMNS = (Column("Core", 4), Column("Status", 8, "<"), Column("Pass", 4), Column("Fail", 4))
SUMMARY_COLUMNS = (
    Column("Core", 4), Column("CCD", 6, "^"), Column("CO", 5), Column("Volt", 7), Column("Power", 7),
    Column("Temp", 5), Column("Eff MHz", 8), Column("Tgt MHz", 8), Column("Drop", 6), Column("Pass", 4),
    Column("Fail", 4), Column("Status", 8, "<"),
)


def join_cells(columns: tuple[Column, ...], cells: dict[str, str]) -> str:
    return " ".join(col.fmt(cells.get(col.header, "--")) for col in columns)


def header_line(columns: tuple[Column, ...]) -> str:
    return " ".join(col.fmt(col.header) for col in columns)


def column_offset(columns: tuple[Column, ...], header: str) -> int:
    """Character offset of a column within a line produced by join_cells."""
    offset = 0
    for col in columns:
        if col.header == header:
            return offset
        offset += col.width + 1
    raise KeyError(header)


def fmt_drop(stretch_mhz: float | None) -> str:
    if stretch_mhz is None:
        return "--"
    return f"-{stretch_mhz:.0f}M" if stretch_mhz >= STRETCH_THRESHOLD_MHZ else "0M"


def fmt_co(co_offset: int | None) -> str:
    return f"{co_offset:+d}" if co_offset is not None else "--"


def ccd_index(core: PhysicalCore) -> int:
    return core.ccd_id if core.ccd_id is not None else 0


def ccd_label(core: PhysicalCore) -> str:
    return f"CCD {ccd_index(core)}"


def rank_tone(core: PhysicalCore | None) -> Tone | None:
    """Gold star for the best CPPC-ranked core of a CCD, silver for the second best."""
    if core is None:
        return None
    return {1: Tone.WARN, 2: Tone.SILVER}.get(core.pref_rank)


def core_label(core_idx: int, ranked: bool) -> str:
    if not ranked:
        return f"{core_idx:>4d}"
    return f"* {core_idx:>2d}" if core_idx < 100 else f"*{core_idx:>3d}"


def core_status(stats: CoreStats, is_active: bool = False, final: bool = False) -> tuple[str, Tone]:
    """Status label of a core; final=True labels cores that never ran as SKIPPED instead of IDLE."""
    if is_active:
        return "ACTIVE", Tone.ACTIVE
    if stats.failures > 0:
        return f"FAIL({stats.failures})", Tone.FAIL
    if stats.passes > 0:
        return ("STRETCH", Tone.WARN) if stats.stretching_detected else ("PASS", Tone.PASS)
    return ("SKIPPED", Tone.DISABLED) if final else ("IDLE", Tone.DEFAULT)


def describe_mce(event: MceEvent) -> str:
    """Location of a hardware error, e.g. 'CPU 13 (Core 1) while testing Core 1'."""
    where = f"CPU {event.cpu}" if event.cpu is not None else "CPU ?"
    if event.core_idx is not None:
        where += f" (Core {event.core_idx})"
    if event.tested_core_idx is not None:
        where += f" while testing Core {event.tested_core_idx}"
    return where


def live_core_row(
    metrics: CoreSmuMetrics,
    core: PhysicalCore | None,
    stats: CoreStats | None,
    is_active: bool,
    run_stretch_mhz: float | None = None,
) -> CoreRow:
    """
    Row of the live telemetry table for one PM table slot. All columns show current SMU readings except the
    results: Drop is the median clock drop of the current run (active core) or of the worst finished run.
    """
    if not metrics.is_enabled or metrics.core_idx is None or stats is None:
        return CoreRow(cells={"Core": "-", "Status": "DISABLED"}, status_tone=Tone.DISABLED, is_disabled=True)

    ranked = rank_tone(core)
    status, tone = core_status(stats, is_active)
    finished = stats.telemetry
    drop = run_stretch_mhz if is_active else (finished.stretch_mhz if finished else None)

    cells = {
        "Core": core_label(metrics.core_idx, ranked is not None),
        "Status": status,
        "Pass": str(stats.passes),
        "Drop": fmt_drop(drop),
        "CO": fmt_co(metrics.co_offset),
        "Volt": f"{metrics.voltage_v:5.3f}V",
        "Power": f"{metrics.power_w:5.2f}W",
        "Temp": f"{metrics.temp_c:3.0f}°C",
        "Clock": f"{metrics.effective_mhz:.0f}",
        "Target": f"{metrics.frequency_mhz:.0f}",
        "C0%": f"{metrics.c0_pct:3.0f}%",
        "C1%": f"{metrics.c1_pct:3.0f}%",
        "C6%": f"{metrics.c6_pct:3.0f}%",
    }
    return CoreRow(cells=cells, status_tone=tone, rank_tone=ranked, is_active=is_active)


def basic_core_row(core: PhysicalCore, stats: CoreStats, is_active: bool) -> CoreRow:
    """Row of the core table when no SMU telemetry is available."""
    ranked = rank_tone(core)
    status, tone = core_status(stats, is_active)
    cells = {
        "Core": core_label(core.core_idx, ranked is not None),
        "Status": status,
        "Pass": str(stats.passes),
        "Fail": str(stats.failures),
    }
    return CoreRow(cells=cells, status_tone=tone, rank_tone=ranked, is_active=is_active)


def _ansi(text: str, tone: Tone | None, bold: bool = False) -> str:
    color = ANSI_TONES.get(tone, "") if tone is not None else ""
    prefix = (BOLD if bold else "") + color
    return f"{prefix}{text}{RESET}" if prefix else text


def _summary_table(cores: list[PhysicalCore], stats: dict[int, CoreStats], co_offsets: dict[int, int]) -> list[str]:
    inner = [f" {col.fmt(col.header)} " for col in SUMMARY_COLUMNS]
    box_w = len("║".join(inner))

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join("═" * len(cell) for cell in inner) + right

    lines = [
        _ansi(f"╔{'═' * box_w}╗", Tone.ACTIVE, bold=True),
        _ansi(f"║{'CYCLE SUMMARY RESULTS':^{box_w}}║", Tone.ACTIVE, bold=True),
        _ansi(rule("╠", "╦", "╣"), Tone.ACTIVE, bold=True),
        _ansi("║" + "║".join(inner) + "║", Tone.ACTIVE, bold=True),
        _ansi(rule("╠", "╬", "╣"), Tone.ACTIVE, bold=True),
    ]

    current_ccd = None
    for core in cores:
        st = stats[core.core_idx]
        ccd = ccd_label(core)
        if ccd != current_ccd:
            current_ccd = ccd
            title = f" ── {ccd} " + "─" * max(0, box_w - len(ccd) - 5)
            lines.append("║" + _ansi(title[:box_w], Tone.HEADER, bold=True) + "║")

        summary = st.telemetry
        status, status_tone = core_status(st, final=True)
        ranked = rank_tone(core)
        clock_tone = Tone.WARN if st.stretching_detected else None
        cells: dict[str, tuple[str, Tone | None]] = {
            "Core": (core_label(core.core_idx, ranked is not None), None),
            "CCD": (ccd, None),
            "CO": (fmt_co(co_offsets.get(core.core_idx)), None),
            "Volt": (f"{summary.voltage_v:.4f}V" if summary else "--", None),
            "Power": (f"{summary.power_w:.2f}W" if summary else "--", None),
            "Temp": (f"{summary.temp_c:.0f}°C" if summary else "--", None),
            "Eff MHz": (f"{summary.effective_mhz:.0f}" if summary else "--", clock_tone),
            "Tgt MHz": (f"{summary.target_mhz:.0f}" if summary else "--", clock_tone),
            "Drop": (fmt_drop(summary.stretch_mhz if summary else None), clock_tone),
            "Pass": (str(st.passes), None),
            "Fail": (str(st.failures), None),
            "Status": (status, status_tone),
        }
        rendered = []
        for col in SUMMARY_COLUMNS:
            text, tone = cells[col.header]
            cell = col.fmt(text)
            if col.header == "Core" and ranked is not None:
                cell = _ansi(cell[0], ranked, bold=True) + cell[1:]
            elif tone is not None:
                cell = _ansi(cell, tone, bold=col.header == "Status")
            rendered.append(f" {cell} ")
        lines.append("║" + "║".join(rendered) + "║")

    lines.append(_ansi(rule("╚", "╩", "╝"), Tone.ACTIVE, bold=True))
    totals = (
        f"Passes: {sum(s.passes for s in stats.values())} | Fails: {sum(s.failures for s in stats.values())} | "
        f"Iterations: {sum(s.verified_iterations for s in stats.values())} | "
        f"Duration: {sum(s.total_duration for s in stats.values()) / 60.0:.1f}m"
    )
    lines.append(_ansi(f"{totals:^{box_w + 2}}", None, bold=True))
    return lines


def _failures_section(cores: list[PhysicalCore], stats: dict[int, CoreStats]) -> list[str]:
    failed = [(core, run) for core in cores for run in stats[core.core_idx].failed_runs]
    if not failed:
        return []
    lines = ["", _ansi(f"Failures ({len(failed)}):", Tone.FAIL, bold=True)]
    for core, run in failed:
        result = run.result
        lines.append(_ansi(
            f"  Core {core.core_idx} - cycle {run.cycle_num}, {run.runner_name}: {result.status}: {result.error_message}",
            Tone.FAIL, bold=True,
        ))
        if result.work_dir:
            lines.append(f"    Work directory: {result.work_dir}")
        lines.append(f"    Engine output ({len(result.output)} lines):")
        lines.extend(f"    | {line}" for line in result.output)
    return lines


def _mce_section(mce_events: list[MceEvent]) -> list[str]:
    if not mce_events:
        return []
    lines = ["", _ansi(f"Hardware errors reported by the kernel during the session ({len(mce_events)}):", Tone.FAIL,
                       bold=True)]
    lines.extend(_ansi(f"  {describe_mce(ev)}: {ev.message}", Tone.FAIL) for ev in mce_events)
    return lines


def render_summary(
    cores: list[PhysicalCore],
    stats: dict[int, CoreStats],
    co_offsets: dict[int, int] | None = None,
    mce_events: list[MceEvent] | None = None,
) -> list[str]:
    """Renders the end-of-session summary as ANSI-coloured lines: result table, failures and hardware errors."""
    return (
        _summary_table(cores, stats, co_offsets or {})
        + _failures_section(cores, stats)
        + _mce_section(mce_events or [])
    )
