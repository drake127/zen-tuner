"""
Presentation view models shared by the live dashboard and the final summary table.
Turns cores, statistics and SMU metrics into formatted, tone-annotated table cells without any terminal I/O.
"""

from dataclasses import dataclass
from enum import Enum

from lib.models import STRETCH_NOISE_MHZ, STRETCH_THRESHOLD_MHZ, CoreSmuMetrics, CoreStats, PhysicalCore
from lib.ui import BOLD, CYAN, DIM, GREEN, MAGENTA, RED, RESET, WHITE, YELLOW

# C0 residency above which a live clock drop of the active core is meaningful.
LIVE_DROP_C0_PCT_MIN = 90.0


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


LIVE_COLUMNS_WIDE = (
    Column("Core", 4), Column("Status", 8, "<"), Column("Pass", 4), Column("CO", 4), Column("Volt", 6),
    Column("Power", 6), Column("Temp", 5), Column("Clock", 5), Column("Target", 6), Column("Drop", 5),
    Column("C0%", 4), Column("C1%", 4), Column("C6%", 4),
)
LIVE_COLUMNS_NARROW = (
    Column("Core", 4), Column("Status", 8, "<"), Column("Pass", 4), Column("Volt", 6), Column("Power", 6),
    Column("Temp", 5), Column("Clock", 5), Column("C0%", 4), Column("C1%", 4), Column("C6%", 4),
)
BASIC_COLUMNS = (Column("Core", 4), Column("Status", 8, "<"), Column("Pass", 4), Column("Fail", 4))
SUMMARY_COLUMNS = (
    Column("Core", 4), Column("CCD", 6, "^"), Column("CO", 5), Column("Volt", 7), Column("Power", 7),
    Column("Temp", 5), Column("Eff MHz", 8), Column("Tgt MHz", 8), Column("Drop", 6), Column("Pass", 4),
    Column("Fail", 4), Column("Status", 14, "<"),
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


def ccd_label(core: PhysicalCore) -> str:
    return f"CCD {core.ccd_id if core.ccd_id is not None else 0}"


def rank_tone(core: PhysicalCore | None) -> Tone | None:
    """Gold star for the best CPPC-ranked core of a CCD, silver for the second best."""
    if core is None:
        return None
    return {1: Tone.WARN, 2: Tone.SILVER}.get(core.pref_rank)


def core_label(core_idx: int, ranked: bool) -> str:
    if not ranked:
        return f"{core_idx:>4d}"
    return f"* {core_idx:>2d}" if core_idx < 100 else f"*{core_idx:>3d}"


def core_status(stats: CoreStats, is_active: bool) -> tuple[str, Tone]:
    if is_active:
        return "ACTIVE", Tone.ACTIVE
    if stats.failures > 0:
        return f"FAIL({stats.failures})", Tone.FAIL
    if stats.passes > 0:
        return "PASS", Tone.PASS
    return "IDLE", Tone.DEFAULT


def live_stretch_mhz(metrics: CoreSmuMetrics) -> float:
    if metrics.c0_pct < LIVE_DROP_C0_PCT_MIN:
        return 0.0
    drop = metrics.frequency_mhz - metrics.effective_mhz
    return drop if drop > STRETCH_NOISE_MHZ else 0.0


def live_core_row(
    metrics: CoreSmuMetrics,
    core: PhysicalCore | None,
    stats: CoreStats | None,
    is_active: bool,
) -> CoreRow:
    """Row of the live telemetry table for one PM table slot."""
    if not metrics.is_enabled or metrics.core_idx is None or stats is None:
        return CoreRow(cells={"Core": "-", "Status": "DISABLED"}, status_tone=Tone.DISABLED, is_disabled=True)

    ranked = rank_tone(core)
    status, tone = core_status(stats, is_active)
    summary = stats.telemetry
    if is_active:
        target, drop = f"{metrics.frequency_mhz:.0f}", fmt_drop(live_stretch_mhz(metrics))
    elif summary is not None:
        target, drop = f"{summary.target_mhz:.0f}", fmt_drop(summary.stretch_mhz)
    else:
        target, drop = f"{metrics.frequency_mhz:.0f}", "--"

    cells = {
        "Core": core_label(metrics.core_idx, ranked is not None),
        "Status": status,
        "Pass": str(stats.passes),
        "CO": fmt_co(metrics.co_offset),
        "Volt": f"{metrics.voltage_v:5.3f}V",
        "Power": f"{metrics.power_w:5.2f}W",
        "Temp": f"{metrics.temp_c:3.0f}°C",
        "Clock": f"{metrics.effective_mhz:.0f}",
        "Target": target,
        "Drop": drop,
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


def render_summary(
    cores: list[PhysicalCore],
    stats: dict[int, CoreStats],
    co_offsets: dict[int, int] | None = None,
) -> list[str]:
    """Renders the end-of-session summary table as ANSI-coloured lines."""
    co_offsets = co_offsets or {}
    inner = [f" {col.fmt(col.header)} " for col in SUMMARY_COLUMNS]
    box_w = len("║".join(inner))
    sep = "═" * box_w

    def rule(left: str, mid: str, right: str) -> str:
        return left + mid.join("═" * len(cell) for cell in inner) + right

    lines = [
        _ansi(f"╔{sep}╗", Tone.ACTIVE, bold=True),
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
        stretched = st.stretching_detected
        if st.failures:
            status, status_tone = f"FAIL ({st.failures})", Tone.FAIL
        elif st.passes:
            status, status_tone = ("PASS (STRETCH)", Tone.WARN) if stretched else ("PASS", Tone.PASS)
        else:
            status, status_tone = "SKIPPED", Tone.DISABLED

        ranked = rank_tone(core)
        clock_tone = Tone.WARN if stretched else None
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

    mce_lines = [(core, ev) for core in cores for ev in stats[core.core_idx].mce_events]
    if mce_lines:
        lines.append("")
        lines.append(_ansi(f"Hardware errors reported by the kernel during the session ({len(mce_lines)}):", Tone.FAIL,
                           bold=True))
        for core, ev in mce_lines:
            cpu = f"CPU {ev.cpu}" if ev.cpu is not None else "CPU ?"
            lines.append(_ansi(f"  [while testing Core {core.core_idx}] {cpu}: {ev.message}", Tone.FAIL))
    return lines
