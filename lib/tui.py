"""
Ncurses-based TUI presentation layer for Zen Tuner.
Displays a split-screen dashboard with test progress log, live per-core telemetry,
SMU PM table metrics, and overall PBO limit gauges.

All curses calls happen in a single UI thread (input, resize, SMU refresh and rendering); other threads only
append log lines and flag the screen dirty, so no curses call is ever made concurrently or from a signal handler.
"""

import curses
import select
import sys
import threading
import time
import traceback

from lib.logbuffer import LogBuffer
from lib.models import PhysicalCore, SmuSnapshot
from lib.presenter import Presenter
from lib.smu import RyzenSmuMonitor
from lib.ui import Logger
from lib.views import (
    BASIC_COLUMNS,
    LIVE_COLUMNS_NARROW,
    LIVE_COLUMNS_WIDE,
    Column,
    CoreRow,
    Tone,
    basic_core_row,
    ccd_index,
    column_offset,
    header_line,
    join_cells,
    live_core_row,
)

MIN_WIDTH = 70
MIN_HEIGHT = 15
TICK_S = 0.1

TONE_PAIRS: dict[Tone, int] = {
    Tone.DEFAULT: 0,
    Tone.PASS: 1,
    Tone.FAIL: 2,
    Tone.WARN: 3,
    Tone.ACTIVE: 4,
    Tone.HEADER: 5,
    Tone.DISABLED: 6,
    Tone.SILVER: 7,
}
BOLD_TONES = (Tone.PASS, Tone.FAIL, Tone.WARN, Tone.ACTIVE)


class CursesPresenter(Presenter):
    """Full-screen ncurses dashboard for real-time stress testing supervision."""

    def __init__(
        self,
        all_cores: list[PhysicalCore],
        logger: Logger | None = None,
        smu_monitor: RyzenSmuMonitor | None = None,
        refresh_interval: float = 1.0,
    ):
        super().__init__(all_cores, logger=logger, smu_monitor=smu_monitor)
        self.refresh_interval = max(0.1, refresh_interval)
        self.log = LogBuffer()
        self.last_smu_snapshot: SmuSnapshot | None = None
        self._stdscr: "curses.window | None" = None
        self._ui_thread: threading.Thread | None = None
        self._running = False
        self._dirty = True
        self._log_visible = 1
        self._ui_error: str | None = None

    # Lifecycle

    def start(self) -> None:
        """Initializes curses and starts the UI thread."""
        self._stdscr = curses.initscr()
        try:
            curses.noecho()
            curses.cbreak()
            self._stdscr.keypad(True)
            self._stdscr.nodelay(True)
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            self._init_colors()
            try:
                curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
                curses.mouseinterval(0)
            except curses.error:
                pass
        except Exception:
            self._restore_terminal()
            raise

        self._running = True
        self._ui_thread = threading.Thread(target=self._ui_loop, name="zen-tuner-ui", daemon=True)
        self._ui_thread.start()

    def close(self) -> None:
        """Stops the UI thread and restores the terminal. Safe to call repeatedly."""
        self._running = False
        if self._ui_thread is not None:
            self._ui_thread.join()
            self._ui_thread = None
        self._restore_terminal()
        if self._ui_error:
            print(f"[ERROR] Dashboard stopped after an internal error:\n{self._ui_error}", file=sys.stderr)
            self._ui_error = None

    def _restore_terminal(self) -> None:
        if self._stdscr is None:
            return
        try:
            self._stdscr.keypad(False)
            curses.nocbreak()
            curses.echo()
            try:
                curses.curs_set(1)
            except curses.error:
                pass
        finally:
            curses.endwin()
            self._stdscr = None

    @staticmethod
    def _init_colors() -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        gray = 8 if curses.COLORS > 8 else curses.COLOR_WHITE
        palette = {
            Tone.PASS: curses.COLOR_GREEN,
            Tone.FAIL: curses.COLOR_RED,
            Tone.WARN: curses.COLOR_YELLOW,
            Tone.ACTIVE: curses.COLOR_CYAN,
            Tone.HEADER: curses.COLOR_MAGENTA,
            Tone.DISABLED: gray,
            Tone.SILVER: curses.COLOR_WHITE,
        }
        for tone, fg in palette.items():
            try:
                curses.init_pair(TONE_PAIRS[tone], fg, bg)
            except curses.error:
                pass

    # Presenter output

    def _emit(self, text: str, tone: Tone = Tone.DEFAULT) -> None:
        super()._emit(text, tone)
        self.log.append(text, tone)
        self._dirty = True

    # UI thread

    def _ui_loop(self) -> None:
        try:
            self._ui_iterations()
        except Exception:
            # Keep the stress run going without a dashboard; the error is reported once the terminal is restored.
            self._ui_error = traceback.format_exc()
            self._running = False

    def _ui_iterations(self) -> None:
        next_smu_read = 0.0
        while self._running:
            try:
                select.select([sys.stdin], [], [], TICK_S)
            except (OSError, ValueError):
                time.sleep(TICK_S)

            clear = self._handle_input()
            now = time.monotonic()
            if now >= next_smu_read:
                next_smu_read = now + self.refresh_interval
                if self.smu_monitor is not None and self.smu_monitor.is_available():
                    self.last_smu_snapshot = self.smu_monitor.read_snapshot()
                self._dirty = True
            if self._dirty or clear:
                self._dirty = False
                self._render(clear)

    def _handle_input(self) -> bool:
        """Processes all pending keys. Returns True when the screen must be fully cleared (resize)."""
        clear = False
        while True:
            try:
                key = self._stdscr.getch()
            except curses.error:
                break
            if key == -1:
                break
            if key == curses.KEY_RESIZE:
                curses.update_lines_cols()
                clear = True
            elif key == curses.KEY_UP:
                self.log.scroll(3, self._log_visible)
            elif key == curses.KEY_DOWN:
                self.log.scroll(-3, self._log_visible)
            elif key == curses.KEY_PPAGE:
                self.log.scroll(self._log_visible, self._log_visible)
            elif key == curses.KEY_NPAGE:
                self.log.scroll(-self._log_visible, self._log_visible)
            elif key == curses.KEY_HOME:
                self.log.scroll_to_oldest(self._log_visible)
            elif key == curses.KEY_END:
                self.log.follow()
            elif key == curses.KEY_MOUSE:
                try:
                    bstate = curses.getmouse()[4]
                except curses.error:
                    continue
                if bstate & curses.BUTTON4_PRESSED:
                    self.log.scroll(3, self._log_visible)
                elif bstate & curses.BUTTON5_PRESSED:
                    self.log.scroll(-3, self._log_visible)
            else:
                continue
            self._dirty = True
        return clear

    # Rendering

    def _attr(self, tone: Tone, bold: bool = False) -> int:
        try:
            attr = curses.color_pair(TONE_PAIRS[tone])
        except curses.error:
            attr = 0
        return attr | curses.A_BOLD if bold else attr

    def _put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        """Writes text clipped to the screen; the bottom-right cell is never written to avoid a curses error."""
        max_y, max_x = self._stdscr.getmaxyx()
        if not (0 <= y < max_y and 0 <= x < max_x):
            return
        avail = max_x - x - (1 if y == max_y - 1 else 0)
        if avail <= 0:
            return
        try:
            self._stdscr.addstr(y, x, text[:avail], attr)
        except curses.error:
            pass

    def _render(self, clear: bool = False) -> None:
        scr = self._stdscr
        if clear:
            scr.clear()
        else:
            scr.erase()

        max_y, max_x = scr.getmaxyx()
        if max_y < MIN_HEIGHT or max_x < MIN_WIDTH:
            self._put(0, 0, f"Terminal window too small! Minimum size: {MIN_WIDTH}x{MIN_HEIGHT}.", curses.A_BOLD)
            scr.refresh()
            return

        top = self._draw_header(max_x)
        self._draw_footer(max_y, max_x)
        mid_height = max_y - top - 2

        # Telemetry pane on the right has a fixed width; the log pane on the left takes the rest
        if max_x >= 120:
            cores_width = 80
        elif max_x >= 95:
            cores_width = 61
        else:
            cores_width = max(35, max_x - 30)
        split_x = max(25, max_x - cores_width)

        self._draw_log_pane(top, 0, mid_height, split_x)
        self._draw_cores_pane(top, split_x, mid_height, max_x - split_x)
        scr.refresh()

    def _centered(self, y: int, max_x: int, text: str, attr: int) -> None:
        self._put(y, max(0, (max_x - len(text)) // 2), text, attr)

    def _draw_header(self, max_x: int) -> int:
        """Draws title, progress and SMU package bars. Returns the first row below the header."""
        session = self.session
        title = f"Zen Tuner  [{session.profile}]" if session.profile else "Zen Tuner"
        if len(title) >= max_x - 4:
            title = "Zen Tuner"
        self._centered(0, max_x, title, curses.A_BOLD | curses.A_REVERSE)

        total = session.total_cycles or "∞"
        cycle_info = f"Cycle {self.cycle_num}/{total}"
        if session.hyperthreading_mode == "cycle":
            phase = (self.cycle_num - 1) % 3 + 1
            desc = {1: "T0 (1T)", 2: "T1 (1T)", 3: "Both (2T)"}[phase]
            cycle_info += f" [Phase {phase}/3: {desc}]" if max_x >= 100 else f" [P{phase}/3: {desc}]"

        core = self.current_core
        elapsed = time.monotonic() - self.core_start_time if core else 0.0
        passes = sum(s.passes for s in self.stats.values())
        fails = sum(s.failures for s in self.stats.values())
        progress = (
            f"{cycle_info}  │  Testing: Core {core.core_idx if core else '-'} ({self.ht_label if core else 'Idle'})  │  "
            f"Time: {elapsed:.0f}s (Iter target: {session.target_iterations})  │  Passes: {passes} | Fails: {fails}"
        )
        self._centered(1, max_x, progress, curses.A_BOLD)

        snap = self.last_smu_snapshot
        if snap is None:
            self._centered(2, max_x, "SMU: ryzen_smu telemetry not available (voltage/clock columns disabled)",
                           curses.A_DIM)
            self._put(3, 0, "═" * (max_x - 1), curses.A_DIM)
            return 4

        pkg = snap.package

        def pct(value: float, limit: float) -> float:
            return value / limit * 100.0 if limit > 0 else 0.0

        smu_line = (
            f"SMU Pkg: {pkg.socket_power_w:.2f}W ({pkg.package_temp_c:.1f}°C) │ "
            f"PPT: {pkg.ppt_w:.2f}/{pkg.ppt_limit_w:.0f}W ({pct(pkg.ppt_w, pkg.ppt_limit_w):.0f}%) │ "
            f"TDC: {pkg.tdc_a:.0f}/{pkg.tdc_limit_a:.0f}A ({pct(pkg.tdc_a, pkg.tdc_limit_a):.0f}%) │ "
            f"EDC: {pkg.edc_a:.0f}/{pkg.edc_limit_a:.0f}A ({pct(pkg.edc_a, pkg.edc_limit_a):.0f}%)"
        )
        self._centered(2, max_x, smu_line, self._attr(Tone.ACTIVE))

        def volt(value: float | None) -> str:
            return f"{value:.4f}V" if value else "--"

        volt_line = (
            f"Voltages: SoC: {volt(pkg.soc_voltage_v)} │ VDDP: {volt(pkg.vddp_voltage_v)} │ "
            f"VDDG CCD: {volt(pkg.vddg_ccd_voltage_v)} │ VDDG IOD: {volt(pkg.vddg_iod_voltage_v)}"
        )
        self._centered(3, max_x, volt_line, self._attr(Tone.HEADER))
        self._put(4, 0, "═" * (max_x - 1), curses.A_DIM)
        return 5

    def _draw_log_pane(self, top: int, left: int, height: int, width: int) -> None:
        visible = max(1, height - 2)
        self._log_visible = visible
        lines, offset = self.log.view(visible)

        title = "─ Progress & Validation Log "
        if offset:
            hint = f" ↑ {offset} lines | End=follow ─"
            title += "─" * max(0, width - len(title) - len(hint) - 2) + hint
        else:
            title += "─" * max(0, width - len(title) - 2)
        self._put(top, left + 1, title[: width - 2], curses.A_BOLD if offset else curses.A_DIM)

        for row, (text, tone) in enumerate(lines):
            self._put(top + 1 + row, left + 1, text[: width - 3], self._attr(tone, bold=tone in BOLD_TONES))

        for r in range(height):
            self._put(top + r, left, "│", curses.A_DIM)
            self._put(top + r, left + width - 1, "│", curses.A_DIM)

    def _draw_cores_pane(self, top: int, left: int, height: int, width: int) -> None:
        snap = self.last_smu_snapshot
        title = "─ Core Telemetry " if snap else "─ Physical Cores "
        self._put(top, left, title + "─" * max(0, width - len(title) - 1), curses.A_DIM)

        if snap is not None and snap.slots:
            columns = LIVE_COLUMNS_WIDE if len(header_line(LIVE_COLUMNS_WIDE)) <= width - 2 else LIVE_COLUMNS_NARROW
            active_idx = self.current_core.core_idx if self.current_core else None
            run_stretch = self.current_stretch_mhz
            entries = [
                (sm.ccd_idx, live_core_row(sm, self.core_by_idx.get(sm.core_idx), self.stats.get(sm.core_idx),
                                           sm.core_idx is not None and sm.core_idx == active_idx, run_stretch))
                for sm in snap.slots
            ]
        else:
            columns = BASIC_COLUMNS
            active_idx = self.current_core.core_idx if self.current_core else None
            entries = [
                (ccd_index(c), basic_core_row(c, self.stats[c.core_idx], c.core_idx == active_idx))
                for c in self.all_cores
            ]

        self._put(top + 1, left + 1, header_line(columns)[: width - 2], curses.A_BOLD)

        y = top + 2
        bottom = top + height
        current_ccd = None
        for ccd, row in entries:
            if ccd != current_ccd:
                if y >= bottom:
                    break
                current_ccd = ccd
                ccd_title = f"── CCD {ccd} "
                divider = ccd_title + "─" * max(0, width - len(ccd_title) - 2)
                self._put(y, left + 1, divider[: width - 2], self._attr(Tone.HEADER, bold=True))
                y += 1
            if y >= bottom:
                break
            self._draw_core_row(y, left, width, columns, row)
            y += 1

        for r in range(height):
            self._put(top + r, left + width - 1, "│", curses.A_DIM)

    def _draw_core_row(self, y: int, left: int, width: int, columns: tuple[Column, ...], row: CoreRow) -> None:
        avail = width - 2
        text = join_cells(columns, row.cells)[:avail]
        x = left + 1
        if row.is_disabled:
            self._put(y, x, text, self._attr(Tone.DISABLED) | curses.A_DIM)
            return

        if row.is_active:
            self._put(y, x, text, self._attr(Tone.ACTIVE, bold=True) | curses.A_REVERSE)
        else:
            self._put(y, x, text, self._attr(Tone.DEFAULT))
            status_col = next(c for c in columns if c.header == "Status")
            status_x = column_offset(columns, "Status")
            status_text = status_col.fmt(row.cells["Status"])[: max(0, avail - status_x)]
            self._put(y, x + status_x, status_text, self._attr(row.status_tone, bold=row.status_tone != Tone.DEFAULT))

        if row.rank_tone is not None:
            star_attr = self._attr(row.rank_tone, bold=True) | (curses.A_REVERSE if row.is_active else 0)
            self._put(y, x, "*", star_attr)

    def _draw_footer(self, max_y: int, max_x: int) -> None:
        self._put(max_y - 2, 0, "═" * (max_x - 1), curses.A_DIM)
        log_path = self.logger.log_path if self.logger else "N/A"
        hint = " [Ctrl+C] Stop & Summary  [↑↓/PgUp/PgDn/Home] Scroll log  [End] Follow   Log: " + log_path
        self._put(max_y - 1, 0, hint[: max_x - 1], curses.A_DIM)
