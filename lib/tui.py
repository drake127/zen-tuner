"""
Ncurses-based TUI presentation layer for Zen Tuner.
Displays a split-screen dashboard with test progress log, live per-core telemetry,
SMU PM table metrics, and overall PBO limit gauges.
"""

from collections import deque
import curses
import os
import signal
import sys
import threading
import time

from lib.models import CoreStats, MceEvent, PhysicalCore, RunResult, SmuSnapshot, StretchSample
from lib.smu import RyzenSmuMonitor
from lib.ui import Logger, get_iso_timestamp, strip_ansi
from runners.base import TestEventListener


class CursesPresenter(TestEventListener):
    """Full-screen ncurses dashboard for real-time stress testing supervision."""

    COLOR_DEFAULT = 0
    COLOR_PASS = 1
    COLOR_FAIL = 2
    COLOR_WARN = 3
    COLOR_ACTIVE = 4
    COLOR_HEADER = 5
    COLOR_DISABLED = 6
    COLOR_SILVER = 7

    def __init__(
        self,
        all_cores: list[PhysicalCore],
        duration_per_core: float | None = None,
        logger: Logger | None = None,
        smu_monitor: RyzenSmuMonitor | None = None,
        stats: dict[int, CoreStats] | None = None,
        refresh_interval: float = 1.0,
    ):
        self.all_cores = all_cores
        self.duration_per_core = duration_per_core
        self.logger = logger
        self.smu_monitor = smu_monitor
        self.refresh_interval = max(0.1, refresh_interval)
        self.stats: dict[int, CoreStats] = stats if stats is not None else {c.core_idx: CoreStats() for c in all_cores}

        self.log_lines: deque[tuple[str, int]] = deque(maxlen=2000)
        self.cycle_num = 1
        self.total_cycles = 1
        self.current_core: PhysicalCore | None = None
        self.ht_label = ""
        self.ht_mode = "on"
        self.core_start_time = 0.0
        self.last_smu_snapshot: SmuSnapshot | None = None

        self._lock = threading.Lock()
        self._running = False
        self._curses_active = False
        self._stdscr = None
        self._ticker_thread: threading.Thread | None = None
        self._refresh_event = threading.Event()
        self._old_sigwinch = None

    def __enter__(self) -> "CursesPresenter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def start(self) -> None:
        """Initializes curses screen, hooks window resize signal, and starts ticker."""
        if not sys.stdout.isatty():
            return

        try:
            self._stdscr = curses.initscr()
            curses.noecho()
            curses.cbreak()
            try:
                curses.curs_set(0)
            except curses.error:
                pass

            if curses.has_colors():
                curses.start_color()
                try:
                    curses.use_default_colors()
                    bg = -1
                except curses.error:
                    bg = curses.COLOR_BLACK

                curses.init_pair(self.COLOR_PASS, curses.COLOR_GREEN, bg)
                curses.init_pair(self.COLOR_FAIL, curses.COLOR_RED, bg)
                curses.init_pair(self.COLOR_WARN, curses.COLOR_YELLOW, bg)
                curses.init_pair(self.COLOR_ACTIVE, curses.COLOR_CYAN, bg)
                curses.init_pair(self.COLOR_HEADER, curses.COLOR_MAGENTA, bg)
                gray_fg = 8 if curses.COLORS > 8 else curses.COLOR_WHITE
                try:
                    curses.init_pair(self.COLOR_DISABLED, gray_fg, bg)
                except curses.error:
                    try:
                        curses.init_pair(self.COLOR_DISABLED, curses.COLOR_WHITE, bg)
                    except curses.error:
                        pass
                try:
                    curses.init_pair(self.COLOR_SILVER, curses.COLOR_WHITE, bg)
                except curses.error:
                    pass

            self._stdscr.keypad(True)
            self._stdscr.nodelay(True)
            self._curses_active = True
            self._running = True

            if hasattr(signal, "SIGWINCH"):
                try:
                    self._old_sigwinch = signal.signal(signal.SIGWINCH, self._handle_sigwinch)
                except (ValueError, OSError):
                    pass

            self._ticker_thread = threading.Thread(target=self._ticker_loop, daemon=True)
            self._ticker_thread.start()
        except Exception:
            self.close()

    def close(self) -> None:
        """Restores standard terminal state safely and resets signal handlers."""
        self._running = False
        self._refresh_event.set()
        if self._ticker_thread and self._ticker_thread.is_alive():
            self._ticker_thread.join(timeout=0.5)

        if hasattr(signal, "SIGWINCH") and self._old_sigwinch is not None:
            try:
                signal.signal(signal.SIGWINCH, self._old_sigwinch)
            except (ValueError, OSError):
                pass
            self._old_sigwinch = None

        if self._curses_active and self._stdscr:
            try:
                self._stdscr.keypad(False)
                curses.nocbreak()
                curses.echo()
                try:
                    curses.curs_set(1)
                except curses.error:
                    pass
                curses.endwin()
            except Exception:
                pass
            self._curses_active = False

    def _safe_color_pair(self, pair_num: int) -> int:
        try:
            return curses.color_pair(pair_num)
        except Exception:
            return 0

    def _handle_sigwinch(self, signum=None, frame=None) -> None:
        """Immediately handles terminal resize signal and repaints the screen without delay."""
        if not self._curses_active:
            return
        if self._stdscr:
            try:
                ts = os.get_terminal_size()
                curses.resizeterm(ts.lines, ts.columns)
            except Exception:
                try:
                    curses.update_lines_cols()
                except Exception:
                    pass
        self._refresh_event.set()
        if self._stdscr and self._lock.acquire(blocking=False):
            try:
                self._render_locked(clear=True)
            finally:
                self._lock.release()

    def _ticker_loop(self) -> None:
        """Background thread updating SMU telemetry and rendering."""
        while self._running:
            if self.smu_monitor and self.smu_monitor.is_available():
                self.last_smu_snapshot = self.smu_monitor.read_snapshot(target_core_count=len(self.all_cores))
            self.render()
            self._refresh_event.wait(timeout=self.refresh_interval)
            self._refresh_event.clear()

    def _write(self, text: str, to_stderr: bool = False) -> None:
        self._add_log(text, self.COLOR_WARN)

    def _add_log(self, text: str, color_pair: int = COLOR_DEFAULT) -> None:
        clean = strip_ansi(text)
        if self.logger:
            self.logger.log(clean, to_console=False)
        with self._lock:
            self.log_lines.append((clean, color_pair))
        if self._curses_active:
            self.render()

    def on_output_line(self, line: str) -> None:
        self._add_log(line, self.COLOR_DEFAULT)

    def on_test_verified(self, iteration_name: str, completed_count: int) -> None:
        ts = get_iso_timestamp()
        v_msg = f"[{ts}] [VERIFIED] Self-test {iteration_name} passed! (Total: {completed_count})"
        self._add_log(v_msg, self.COLOR_PASS)

    def on_stretching_detected(self, sample: StretchSample) -> None:
        ts = get_iso_timestamp()
        drop = f"-{sample.stretch_mhz:.0f} MHz / -{sample.stretch_pct:.1f}%"
        msg = (
            f"[{ts}] [STRETCH] CPU {sample.cpu}: Tgt {sample.target_mhz:.0f} "
            f"vs Eff {sample.effective_mhz:.0f} ({drop})"
        )
        self._add_log(msg, self.COLOR_WARN)

    def on_hardware_error(self, event: MceEvent) -> None:
        ts = get_iso_timestamp()
        label = "ACTIVE ERROR" if event.is_active_core else "IDLE MCE CRASH"
        msg = f"[{ts}] [{label}] CPU {event.cpu}: {event.message}"
        self._add_log(msg, self.COLOR_FAIL)

    def on_grace_period_started(self, max_duration_s: float) -> None:
        ts = get_iso_timestamp()
        self._add_log(f"[{ts}] [GRACE] Target reached. Waiting for current test verification...", self.COLOR_WARN)

    def print_banner(self, **kwargs) -> None:
        self.ht_mode = kwargs.get("hyperthreading_mode", "on")
        self.start()

    def print_cycle_start(self, cycle_num: int, total_cycles: int) -> None:
        self.cycle_num = cycle_num
        self.total_cycles = total_cycles
        tot_str = f" of {total_cycles}" if total_cycles > 0 else ""
        self._add_log(f"▶ Starting Cycle {cycle_num}{tot_str}", self.COLOR_HEADER)

    def print_core_start(self, cycle_num: int, core: PhysicalCore, ht_label: str) -> None:
        self.current_core = core
        self.ht_label = ht_label
        self.core_start_time = time.time()
        ccd_desc = f"CCD {core.ccd_id}" if core.ccd_id is not None else "CCD 0"
        ts = get_iso_timestamp()
        hdr = f"[{ts}] [Cycle {cycle_num}] Testing Core {core.core_idx} ({ccd_desc}) - {ht_label}"
        self._add_log(f"┌── {hdr}", self.COLOR_ACTIVE)

    def print_core_skip(self, cycle_num: int, core: PhysicalCore, reason: str = "already failed") -> None:
        ts = get_iso_timestamp()
        ccd_desc = f"CCD {core.ccd_id}" if core.ccd_id is not None else "CCD 0"
        msg = f"├── [{ts}] [Cycle {cycle_num}] Skipping Core {core.core_idx} ({ccd_desc}) - {reason}"
        self._add_log(msg, self.COLOR_WARN)

    def print_core_result(self, core: PhysicalCore, result: RunResult, idle_core_name: str | None = None) -> None:
        ts = get_iso_timestamp()
        if result.status == "INTERRUPTED":
            msg = f"└── [{ts}] Core {core.core_idx} INTERRUPTED (Cancelled by user)"
            self._add_log(msg, self.COLOR_WARN)
            self.current_core = None
            return

        st = self.stats[core.core_idx]
        # Capture SMU snapshot at end of test (core is still warm, metrics are representative)
        if self.last_smu_snapshot:
            sm = self.last_smu_snapshot.cores.get(core.core_idx)
            if sm and sm.is_enabled:
                st.avg_voltage_v = sm.voltage_v
                st.avg_power_w = sm.power_w
                st.avg_temp_c = sm.temp_c
                if sm.co_offset is not None:
                    st.co_offset = sm.co_offset

        if result.passed:
            pass_info = f"({result.completed_tests} tests, {result.elapsed_seconds:.1f}s)"
            msg = f"└── [{ts}] Core {core.core_idx} PASS {pass_info}"
            self._add_log(msg, self.COLOR_PASS)
        else:
            msg = f"└── [{ts}] Core {core.core_idx} FAIL ({result.status})"
            self._add_log(msg, self.COLOR_FAIL)

        self.current_core = None

    def print_summary_table(self, all_cores: list[PhysicalCore], stats: dict[int, CoreStats]) -> None:
        self.close()

        # ANSI colour codes (inline)
        BOLD = "\033[1m"
        DIM = "\033[2m"
        RED = "\033[31m"
        GREEN = "\033[32m"
        YELLOW = "\033[33m"
        MAGENTA = "\033[35m"
        CYAN = "\033[36m"
        WHITE = "\033[37m"
        RESET = "\033[0m"

        def _out(text: str) -> None:
            if self.logger:
                self.logger.log(text)
            else:
                print(text)

        hdr = (
            f"║ {'Core':>4} ║ {'CCD':^6} ║ {'CO':>5} ║ {'Volt':>7} ║ {'Power':>7} ║ {'Temp':>5} ║"
            f" {'Eff MHz':>8} ║ {'Tgt MHz':>8} ║ {'Drop':>6} ║ {'Pass':>4} ║ {'Fail':>4} ║ {'Status':<14} ║"
        )
        box_w = len(hdr) - 2
        _s = "══════╦════════╦═══════╦═════════╦═════════╦═══════╦══════════╦══════════╦════════╦══════╦══════╦════════════════"
        sep_top = "╠" + _s + "╣"
        sep_hdr = "╠" + _s.replace("╦", "╬") + "╣"
        sep_bot = "╚" + _s.replace("═╦", "═╩").replace("╦", "╩") + "╝"

        _out(f"\n{BOLD}{CYAN}╔{'═' * box_w}╗{RESET}")
        _out(f"{BOLD}{CYAN}║{'CYCLE SUMMARY RESULTS':^{box_w}}║{RESET}")
        _out(f"{BOLD}{CYAN}{sep_top}{RESET}")
        _out(f"{BOLD}{CYAN}{hdr}{RESET}")
        _out(f"{BOLD}{CYAN}{sep_hdr}{RESET}")

        total_passes = sum(s.passes for s in stats.values())
        total_fails = sum(s.failures for s in stats.values())
        total_tests = sum(s.verified_tests for s in stats.values())
        total_time = sum(s.total_duration for s in stats.values())

        current_ccd = None
        for core in all_cores:
            st = stats[core.core_idx]
            ccd_str = f"CCD {core.ccd_id}" if core.ccd_id is not None else "CCD 0"

            if ccd_str != current_ccd:
                current_ccd = ccd_str
                ccd_hdr = f" {MAGENTA}{BOLD}── {ccd_str} {'─' * (box_w - len(ccd_str) - 5)}{RESET}"
                _out(f"║{ccd_hdr}║")

            co_str = f"{st.co_offset:+d}" if st.co_offset is not None else "--"
            volt_str = f"{st.avg_voltage_v:.4f}V" if st.avg_voltage_v is not None else "--"
            power_str = f"{st.avg_power_w:.2f}W" if st.avg_power_w is not None else "--"
            temp_str = f"{st.avg_temp_c:.0f}°C" if st.avg_temp_c is not None else "--"
            drop_str = f"-{st.avg_stretch_mhz:.0f}M" if st.avg_stretch_mhz > 0 else "0M"

            if st.avg_effective_mhz and st.avg_target_mhz:
                eff_str = f"{st.avg_effective_mhz:.0f}"
                tgt_str = f"{st.avg_target_mhz:.0f}"
                sc = YELLOW if st.stretching_detected else ""
                sr = RESET if st.stretching_detected else ""
                eff_col = f"{sc}{eff_str:>8s}{sr}"
                tgt_col = f"{sc}{tgt_str:>8s}{sr}"
                drop_col = f"{sc}{drop_str:>6s}{sr}"
            else:
                eff_col = f"{'--':>8s}"
                tgt_col = f"{'--':>8s}"
                drop_col = f"{'--':>6s}"

            if st.failures == 0 and st.passes > 0:
                raw_status = "PASS (STRETCH)" if st.stretching_detected else "PASS"
                status_col = f"{YELLOW if st.stretching_detected else GREEN}{BOLD}{raw_status:<14s}{RESET}"
            elif st.failures > 0:
                status_col = f"{RED}{BOLD}{f'FAIL ({st.failures})':14s}{RESET}"
            else:
                status_col = f"{DIM}{'SKIPPED':<14s}{RESET}"

            if core.pref_rank == 1 or (core.pref_rank is None and core.is_preferred):
                star = f"{BOLD}{YELLOW}*{RESET}"
                core_lbl = (
                    f"{star} {core.core_idx:>2d}"
                    if core.core_idx < 100
                    else f"{star}{core.core_idx:>3d}"
                )
            elif core.pref_rank == 2:
                star = f"{BOLD}{WHITE}*{RESET}"
                core_lbl = (
                    f"{star} {core.core_idx:>2d}"
                    if core.core_idx < 100
                    else f"{star}{core.core_idx:>3d}"
                )
            else:
                core_lbl = f"{core.core_idx:>4d}"

            _out(
                f"║ {core_lbl} ║ {ccd_str:^6s} ║ {co_str:>5s} ║ {volt_str:>7s} ║ {power_str:>7s} ║"
                f" {temp_str:>5s} ║ {eff_col} ║ {tgt_col} ║ {drop_col} ║ {st.passes:>4d} ║ {st.failures:>4d} ║"
                f" {status_col} ║"
            )

        _out(f"{BOLD}{CYAN}{sep_bot}{RESET}")
        summary_txt = (
            f"Passes: {total_passes} | Fails: {total_fails} | Tests: {total_tests} | "
            f"Duration: {total_time / 60.0:.1f}m"
        )
        _out(f"{BOLD}{summary_txt:^{box_w + 2}}{RESET}\n")

    def render(self, clear: bool = False) -> None:
        """Renders the top summary, left progress log, right core telemetry, and footer."""
        if not self._curses_active or not self._stdscr:
            return

        with self._lock:
            self._render_locked(clear=clear)

    def _render_locked(self, clear: bool = False) -> None:
        try:
            if hasattr(curses, "is_term_resized") and curses.is_term_resized(curses.LINES, curses.COLS):
                try:
                    curses.update_lines_cols()
                    curses.resizeterm(curses.LINES, curses.COLS)
                    clear = True
                except Exception:
                    pass

            max_y, max_x = self._stdscr.getmaxyx()
            if max_y < 15 or max_x < 70:
                self._stdscr.erase()
                self._safe_addstr(0, 0, "Terminal window too small! Minimum size: 70x15.", curses.A_BOLD)
                self._stdscr.refresh()
                return

            if clear:
                self._stdscr.clear()
            else:
                self._stdscr.erase()

            mid_top = self._draw_header(max_x)
            self._draw_footer(max_y, max_x)

            mid_height = max_y - mid_top - 2

            # Layout split calculation:
            # Telemetry pane on the right has fixed width; log pane on the left expands dynamically
            if max_x >= 120:
                cores_width = 80
            elif max_x >= 95:
                cores_width = 61
            else:
                cores_width = max(35, max_x - 30)

            split_x = max(25, max_x - cores_width)

            self._draw_log_pane(mid_top, 0, mid_height, split_x)
            self._draw_cores_pane(mid_top, split_x, mid_height, max_x - split_x)

            self._stdscr.refresh()
        except curses.error:
            pass

    def _safe_addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        try:
            max_y, max_x = self._stdscr.getmaxyx()
            if y < 0 or y >= max_y or x < 0 or x >= max_x:
                return
            avail = max_x - x
            if y >= max_y - 1:
                avail = max_x - x - 1
            if avail > 0:
                self._stdscr.addstr(y, x, text[:avail], attr)
        except curses.error:
            pass

    def _draw_header(self, max_x: int) -> None:
        # Title bar
        title = "Zen Tuner"
        self._safe_addstr(0, (max_x - len(title)) // 2, title, curses.A_BOLD | curses.A_REVERSE)

        # Execution stats bar
        curr_idx = self.current_core.core_idx if self.current_core else "-"
        elapsed = time.time() - self.core_start_time if self.current_core and self.core_start_time > 0 else 0.0
        tot_passes = sum(s.passes for s in self.stats.values())
        tot_fails = sum(s.failures for s in self.stats.values())

        if self.ht_mode in ("cycle", "rr"):
            phase_idx = ((self.cycle_num - 1) % 3) + 1
            phase_desc = {1: "T0 (1T)", 2: "T1 (1T)", 3: "Both (2T)"}[phase_idx]
            if max_x >= 100:
                cycle_info = f"Cycle {self.cycle_num}/{self.total_cycles or '∞'} [Phase {phase_idx}/3: {phase_desc}]"
            else:
                cycle_info = f"Cycle {self.cycle_num}/{self.total_cycles or '∞'} [P{phase_idx}/3: {phase_desc}]"
        else:
            cycle_info = f"Cycle {self.cycle_num}/{self.total_cycles or '∞'}"

        test_info = f"Core {curr_idx} ({self.ht_label or 'Idle'})"
        if self.duration_per_core is not None:
            time_info = f"Time: {elapsed:.0f}s / {self.duration_per_core:.0f}s"
        else:
            time_info = f"Time: {elapsed:.0f}s"
        pass_info = f"Passes: {tot_passes} | Fails: {tot_fails}"

        hdr_line = f"{cycle_info}  │  Testing: {test_info}  │  {time_info}  │  {pass_info}"
        self._safe_addstr(1, max(0, (max_x - len(hdr_line)) // 2), hdr_line, curses.A_BOLD)

        # SMU / Package metrics & Voltages bar
        if self.last_smu_snapshot:
            pkg = self.last_smu_snapshot.package
            ppt_pct = (pkg.ppt_w / pkg.ppt_limit_w * 100.0) if pkg.ppt_limit_w > 0 else 0.0
            tdc_pct = (pkg.tdc_a / pkg.tdc_limit_a * 100.0) if pkg.tdc_limit_a > 0 else 0.0
            edc_pct = (pkg.edc_a / pkg.edc_limit_a * 100.0) if pkg.edc_limit_a > 0 else 0.0
            smu_line = (
                f"SMU Pkg: {pkg.socket_power_w:.2f}W ({pkg.package_temp_c:.1f}°C) │ "
                f"PPT: {pkg.ppt_w:.2f}/{pkg.ppt_limit_w:.0f}W ({ppt_pct:.0f}%) │ "
                f"TDC: {pkg.tdc_a:.0f}/{pkg.tdc_limit_a:.0f}A ({tdc_pct:.0f}%) │ "
                f"EDC: {pkg.edc_a:.0f}/{pkg.edc_limit_a:.0f}A ({edc_pct:.0f}%)"
            )
            self._safe_addstr(2, max(0, (max_x - len(smu_line)) // 2), smu_line, self._safe_color_pair(self.COLOR_ACTIVE))

            soc_v = f"{pkg.soc_voltage_v:.4f}V" if pkg.soc_voltage_v else "--"
            vddp_v = f"{pkg.vddp_voltage_v:.4f}V" if pkg.vddp_voltage_v else "--"
            ccd_v = f"{pkg.vddg_ccd_voltage_v:.4f}V" if pkg.vddg_ccd_voltage_v else "--"
            iod_v = f"{pkg.vddg_iod_voltage_v:.4f}V" if pkg.vddg_iod_voltage_v else "--"
            volt_line = f"Voltages: SoC: {soc_v} │ VDDP: {vddp_v} │ VDDG CCD: {ccd_v} │ VDDG IOD: {iod_v}"
            self._safe_addstr(3, max(0, (max_x - len(volt_line)) // 2), volt_line, self._safe_color_pair(self.COLOR_HEADER))

            self._safe_addstr(4, 0, "═" * (max_x - 1), curses.A_DIM)
            return 5
        else:
            hint_line = "SMU: ryzen_smu module not active (standard MSR telemetry enabled)"
            self._safe_addstr(2, max(0, (max_x - len(hint_line)) // 2), hint_line, curses.A_DIM)
            self._safe_addstr(3, 0, "═" * (max_x - 1), curses.A_DIM)
            return 4

    def _draw_log_pane(self, top: int, left: int, height: int, width: int) -> None:
        title = "─ Progress & Validation Log "
        self._safe_addstr(top, left + 1, title + "─" * max(0, width - len(title) - 2), curses.A_DIM)

        lines_to_show = list(self.log_lines)[-(height - 2) :] if height > 2 else []
        for row_idx, (line_text, col_code) in enumerate(lines_to_show):
            attr = self._safe_color_pair(col_code)
            if col_code in (self.COLOR_PASS, self.COLOR_FAIL, self.COLOR_WARN, self.COLOR_ACTIVE):
                attr |= curses.A_BOLD
            self._safe_addstr(top + 1 + row_idx, left + 1, line_text[: width - 3], attr)

        # Vertical divider lines: left border and center divider
        for r in range(height):
            self._safe_addstr(top + r, left, "│", curses.A_DIM)
            self._safe_addstr(top + r, left + width - 1, "│", curses.A_DIM)

    def _draw_cores_pane(self, top: int, left: int, height: int, width: int) -> None:
        has_smu = self.last_smu_snapshot is not None
        title = "─ Core Telemetry " if has_smu else "─ Physical Cores "
        self._safe_addstr(top, left, title + "─" * max(0, width - len(title) - 1), curses.A_DIM)

        # Column header
        wide = width >= 79
        if has_smu:
            if wide:
                hdr = (
                    f"{'Core':>4} {'Status':<8} {'Pass':>4} {'CO':>4} {'Volt':>6} {'Power':>6} "
                    f"{'Temp':>5} {'Clock':>5} {'Target':>6} {'Drop':>5} {'C0%':>4} {'C1%':>4} {'C6%':>4}"
                )
            else:
                hdr = (
                    f"{'Core':>4} {'Status':<8} {'Pass':>4} {'Volt':>6} {'Power':>6} "
                    f"{'Temp':>5} {'Clock':>5} {'C0%':>4} {'C1%':>4} {'C6%':>4}"
                )
        else:
            if width >= 45:
                hdr = f"{'Core':>4} {'Status':<8} {'Pass':>4} {'Fail':>4} {'Target':>6} {'Drop':>6}"
            else:
                hdr = f"{'Core':>4} {'Status':<8} {'Pass':>4} {'Fail':>4}"
        self._safe_addstr(top + 1, left + 1, hdr[: width - 2], curses.A_BOLD)

        row_offset = 0
        current_ccd = None
        core_map = {c.core_idx: c for c in self.all_cores}

        if has_smu and self.last_smu_snapshot and self.last_smu_snapshot.slots:
            for sm in self.last_smu_snapshot.slots:
                if row_offset >= height - 2:
                    break

                if sm.ccd_idx != current_ccd:
                    current_ccd = sm.ccd_idx
                    ccd_title = f"── CCD {current_ccd} "
                    div_txt = ccd_title + "─" * max(0, width - len(ccd_title) - 2)
                    self._safe_addstr(
                        top + 2 + row_offset,
                        left + 1,
                        div_txt[: width - 2],
                        self._safe_color_pair(self.COLOR_HEADER) | curses.A_BOLD,
                    )
                    row_offset += 1
                    if row_offset >= height - 2:
                        break

                if not sm.is_enabled or sm.core_idx is None:
                    # Inactive/disabled silicon core
                    if wide:
                        row_txt = (
                            f"{'-':>4s} {'DISABLED':<8s} {'--':>4s} {'--':>4s} {'--':>6s} {'--':>6s} "
                            f"{'--':>5s} {'--':>5s} {'--':>6s} {'--':>5s} {'--':>4s} {'--':>4s} {'--':>4s}"
                        )
                    else:
                        row_txt = (
                            f"{'-':>4s} {'DISABLED':<8s} {'--':>4s} {'--':>6s} {'--':>6s} "
                            f"{'--':>5s} {'--':>5s} {'--':>4s} {'--':>4s} {'--':>4s}"
                        )
                    self._safe_addstr(
                        top + 2 + row_offset,
                        left + 1,
                        row_txt[: width - 2],
                        self._safe_color_pair(self.COLOR_DISABLED) | curses.A_DIM,
                    )
                    row_offset += 1
                    continue

                c_idx = sm.core_idx
                core_obj = core_map.get(c_idx)
                star_color = None
                if core_obj:
                    if core_obj.pref_rank == 1 or (core_obj.pref_rank is None and core_obj.is_preferred):
                        star_color = self.COLOR_WARN
                    elif core_obj.pref_rank == 2:
                        star_color = self.COLOR_SILVER

                if star_color is not None:
                    core_str = f"* {c_idx:>2d}" if c_idx < 100 else f"*{c_idx:>3d}"
                else:
                    core_str = f"{c_idx:>4d}"

                st = self.stats.get(c_idx, CoreStats())
                is_active = self.current_core is not None and self.current_core.core_idx == c_idx

                if is_active:
                    status_str = "ACTIVE"
                    row_color = self.COLOR_ACTIVE
                elif st.failures > 0:
                    status_str = f"FAIL({st.failures})"
                    row_color = self.COLOR_FAIL
                elif st.passes > 0:
                    status_str = "PASS"
                    row_color = self.COLOR_PASS
                else:
                    status_str = "IDLE"
                    row_color = self.COLOR_DEFAULT

                co_str = f"{sm.co_offset:+d}" if sm.co_offset is not None else "--"
                drop_str = f"-{st.avg_stretch_mhz:.0f}M" if st.avg_stretch_mhz > 0 else "0M"
                tgt_mhz = st.avg_target_mhz or sm.frequency_mhz

                if wide:
                    row_txt = (
                        f"{core_str} {status_str:<8s} {st.passes:>4d} {co_str:>4s} {sm.voltage_v:5.3f}V "
                        f"{sm.power_w:5.2f}W {sm.temp_c:3.0f}°C {sm.effective_mhz:5.0f} {tgt_mhz:6.0f} "
                        f"{drop_str:>5s} {sm.c0_pct:3.0f}% {sm.c1_pct:3.0f}% {sm.c6_pct:3.0f}%"
                    )
                else:
                    row_txt = (
                        f"{core_str} {status_str:<8s} {st.passes:>4d} {sm.voltage_v:5.3f}V "
                        f"{sm.power_w:5.2f}W {sm.temp_c:3.0f}°C {sm.effective_mhz:5.0f} "
                        f"{sm.c0_pct:3.0f}% {sm.c1_pct:3.0f}% {sm.c6_pct:3.0f}%"
                    )

                attr = self._safe_color_pair(row_color)
                if is_active:
                    attr |= curses.A_BOLD | curses.A_REVERSE
                self._safe_addstr(top + 2 + row_offset, left + 1, row_txt[: width - 2], attr)
                if star_color is not None:
                    star_attr = self._safe_color_pair(star_color) | curses.A_BOLD
                    if is_active:
                        star_attr |= curses.A_REVERSE
                    self._safe_addstr(top + 2 + row_offset, left + 1, "*", star_attr)
                row_offset += 1
        else:
            for core in self.all_cores:
                if row_offset >= height - 2:
                    break

                ccd_id = core.ccd_id if core.ccd_id is not None else 0
                if ccd_id != current_ccd:
                    current_ccd = ccd_id
                    ccd_title = f"── CCD {current_ccd} "
                    div_txt = ccd_title + "─" * max(0, width - len(ccd_title) - 2)
                    self._safe_addstr(
                        top + 2 + row_offset,
                        left + 1,
                        div_txt[: width - 2],
                        self._safe_color_pair(self.COLOR_HEADER) | curses.A_BOLD,
                    )
                    row_offset += 1
                    if row_offset >= height - 2:
                        break

                c_idx = core.core_idx
                star_color = None
                if core.pref_rank == 1 or (core.pref_rank is None and core.is_preferred):
                    star_color = self.COLOR_WARN
                elif core.pref_rank == 2:
                    star_color = self.COLOR_SILVER

                if star_color is not None:
                    core_str = f"* {c_idx:>2d}" if c_idx < 100 else f"*{c_idx:>3d}"
                else:
                    core_str = f"{c_idx:>4d}"

                st = self.stats.get(c_idx, CoreStats())
                is_active = self.current_core is not None and self.current_core.core_idx == c_idx

                if is_active:
                    status_str = "ACTIVE"
                    row_color = self.COLOR_ACTIVE
                elif st.failures > 0:
                    status_str = f"FAIL({st.failures})"
                    row_color = self.COLOR_FAIL
                elif st.passes > 0:
                    status_str = "PASS"
                    row_color = self.COLOR_PASS
                else:
                    status_str = "IDLE"
                    row_color = self.COLOR_DEFAULT

                if width >= 45:
                    drop = f"-{st.max_stretch_mhz:.0f}M" if st.max_stretch_mhz > 0 else "0M"
                    tgt = f"{st.avg_target_mhz:.0f}" if st.avg_target_mhz else "--"
                    row_txt = f"{core_str} {status_str:<8s} {st.passes:>4d} {st.failures:>4d} {tgt:>6s} {drop:>6s}"
                else:
                    row_txt = f"{core_str} {status_str:<8s} {st.passes:>4d} {st.failures:>4d}"

                attr = self._safe_color_pair(row_color)
                if is_active:
                    attr |= curses.A_BOLD | curses.A_REVERSE
                self._safe_addstr(top + 2 + row_offset, left + 1, row_txt[: width - 2], attr)
                if star_color is not None:
                    star_attr = self._safe_color_pair(star_color) | curses.A_BOLD
                    if is_active:
                        star_attr |= curses.A_REVERSE
                    self._safe_addstr(top + 2 + row_offset, left + 1, "*", star_attr)
                row_offset += 1

        # Vertical divider line on the right
        for r in range(height):
            self._safe_addstr(top + r, left + width - 1, "│", curses.A_DIM)

    def _draw_footer(self, max_y: int, max_x: int) -> None:
        self._safe_addstr(max_y - 2, 0, "═" * (max_x - 1), curses.A_DIM)
        keys_hint = " [Ctrl+C] Stop & Show Summary   Session Log: " + (self.logger.log_path if self.logger else "N/A")
        self._safe_addstr(max_y - 1, 0, keys_hint[: max_x - 1], curses.A_DIM)
