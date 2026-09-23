"""
Unit tests for CursesPresenter rendering and input handling against a fake curses screen.
"""

import curses

import pytest

from lib.models import CoreSmuMetrics, PackageSmuMetrics, PhysicalCore, SmuSnapshot
from lib.presenter import SessionInfo
from lib.tui import CursesPresenter
from lib.ui import Logger
from lib.views import Tone


class FakeScreen:
    """Records addstr calls into a character grid; getch returns scripted keys."""

    def __init__(self, height: int = 40, width: int = 160, keys: list[int] | None = None):
        self.height, self.width = height, width
        self.keys = list(keys or [])
        self.calls: list[tuple[int, int, str, int]] = []
        self.grid = [[" "] * width for _ in range(height)]

    def getmaxyx(self):
        return self.height, self.width

    def addstr(self, y, x, text, attr=0):
        self.calls.append((y, x, text, attr))
        for i, ch in enumerate(text):
            self.grid[y][x + i] = ch

    def getch(self):
        return self.keys.pop(0) if self.keys else -1

    def erase(self):
        self.grid = [[" "] * self.width for _ in range(self.height)]

    clear = erase

    def refresh(self):
        pass

    def text(self) -> str:
        return "\n".join("".join(row) for row in self.grid)


def slot(core_idx, slot_idx, ccd_idx=0, enabled=True, **overrides) -> CoreSmuMetrics:
    values = dict(core_idx=core_idx, slot_idx=slot_idx, ccd_idx=ccd_idx, is_enabled=enabled, voltage_v=1.35,
                  power_w=15.25, temp_c=65.0, frequency_mhz=4850.0, effective_mhz=4650.0, c0_pct=99.0,
                  c1_pct=1.0, c6_pct=0.0, co_offset=-25)
    return CoreSmuMetrics(**(values | overrides))


def snapshot(slots: list[CoreSmuMetrics]) -> SmuSnapshot:
    package = PackageSmuMetrics(
        socket_power_w=75.0, package_temp_c=70.0, ppt_w=75.0, ppt_limit_w=142.0, tdc_a=45.0, tdc_limit_a=95.0,
        edc_a=80.0, edc_limit_a=140.0, soc_voltage_v=0.9750, vddp_voltage_v=0.8471, vddg_ccd_voltage_v=0.8471,
        vddg_iod_voltage_v=0.8973,
    )
    return SmuSnapshot(cores={s.core_idx: s for s in slots if s.is_enabled}, package=package, pm_version=0x380805,
                       slots=slots)


@pytest.fixture
def cores() -> list[PhysicalCore]:
    return [
        PhysicalCore(0, 0, 0, [0, 12], pref_rank=1),
        PhysicalCore(1, 1, 0, [1, 13], pref_rank=2),
        PhysicalCore(2, 8, 1, [2, 14]),
    ]


@pytest.fixture
def presenter(cores):
    p = CursesPresenter(cores, session=SessionInfo(profile="Smallest FFTs", hyperthreading_mode="cycle",
                                                   target_iterations=2, total_cycles=3))
    p._stdscr = FakeScreen()
    yield p
    p._stdscr = None


def test_render_with_smu(presenter, cores):
    presenter.last_smu_snapshot = snapshot([slot(0, 0), slot(None, 1, enabled=False), slot(1, 2), slot(2, 8, ccd_idx=1)])
    presenter.stats[1].passes = 3
    presenter.on_cycle_start(2, 3, "prime95")
    presenter.on_core_start(2, cores[0], "Phase 2/3: 1T (CPU 12)")
    presenter._render()
    screen = presenter._stdscr.text()

    assert "Zen Tuner  [Smallest FFTs]" in screen
    assert "Cycle 2/3 [Phase 2/3: T1 (1T)]" in screen
    assert "Testing: Core 0 (Phase 2/3: 1T (CPU 12))" in screen
    assert "PPT: 75.00/142W (53%)" in screen
    assert "VDDG IOD: 0.8973V" in screen
    for text in ("Core Status   Pass   CO", "C6%", "── CCD 0", "── CCD 1", "DISABLED", "ACTIVE", "-25", "15.25W",
                 "-200M", "Starting Cycle 2 of 3 [prime95]"):
        assert text in screen


def test_render_without_smu(presenter):
    presenter.stats[0].failures = 1
    presenter._render()
    screen = presenter._stdscr.text()
    assert "ryzen_smu telemetry not available" in screen
    assert "Physical Cores" in screen
    assert "Core Status   Pass Fail" in screen
    assert "FAIL(1)" in screen


def test_status_and_star_overlays(presenter, monkeypatch):
    monkeypatch.setattr(presenter, "_attr", lambda tone, bold=False: {Tone.PASS: 100, Tone.WARN: 200}.get(tone, 0))
    presenter.stats[0].passes = 1
    presenter._draw_cores_pane(top=0, left=0, height=20, width=50)
    calls = presenter._stdscr.calls
    # Status is drawn in its own tone exactly at the Status column; the gold star keeps the star tone
    assert (3, 1 + 5, "PASS    ", 100) in calls
    assert (3, 1, "*", 200) in calls


def test_terminal_too_small(presenter):
    presenter._stdscr = FakeScreen(height=10, width=60)
    presenter._render()
    assert "Terminal window too small" in presenter._stdscr.text()


def test_emit_logs_and_marks_dirty(tmp_path, cores):
    with Logger(str(tmp_path / "session.log")) as logger:
        presenter = CursesPresenter(cores, logger=logger)
        presenter._dirty = False
        presenter.on_output_line("\033[32mWorker starting\033[0m")
    assert presenter._dirty
    assert presenter.log[-1] == ("Worker starting", Tone.DEFAULT)
    assert (tmp_path / "session.log").read_text() == "Worker starting\n"


def test_scroll_keys(presenter):
    for i in range(50):
        presenter.log.append(f"line {i}")
    presenter._log_visible = 10
    presenter._stdscr = FakeScreen(keys=[curses.KEY_UP, curses.KEY_PPAGE])
    assert presenter._handle_input() is False
    assert presenter.log.view(10)[1] == 13

    presenter._stdscr = FakeScreen(keys=[curses.KEY_END])
    presenter._handle_input()
    assert presenter.log.view(10)[1] == 0

    presenter._stdscr = FakeScreen(keys=[curses.KEY_HOME])
    presenter._handle_input()
    assert presenter.log.view(10)[1] == 40


def test_log_pane_scroll_hint(presenter):
    for i in range(50):
        presenter.log.append(f"line {i}")
    presenter.log.scroll(5, 10)
    presenter._draw_log_pane(top=0, left=0, height=12, width=60)
    screen = presenter._stdscr.text()
    assert "↑ 5 lines | End=follow" in screen
    assert "line 44" in screen and "line 45" not in screen


def test_resize_key_requests_clear(presenter, monkeypatch):
    monkeypatch.setattr(curses, "update_lines_cols", lambda: None)
    presenter._stdscr = FakeScreen(keys=[curses.KEY_RESIZE])
    assert presenter._handle_input() is True


def test_close_without_start_is_noop(cores):
    presenter = CursesPresenter(cores)
    presenter.close()
    presenter.close()
