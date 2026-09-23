"""
Unit tests for presenter message formatting, console output and the scrollable log buffer.
"""

from conftest import CapturingPresenter, make_result, make_sample
from lib.logbuffer import LogBuffer
from lib.models import MceEvent, RunStatus
from lib.presenter import ConsolePresenter, Presenter, SessionInfo
from lib.ui import Logger
from lib.views import Tone


def test_runner_event_messages(two_cores):
    presenter = CapturingPresenter(two_cores)
    presenter.on_output_line("Test starting")
    presenter.on_test_verified("4K", 1)
    presenter.on_hardware_error(MceEvent(cpu=13, message="mce: [Hardware Error]: CPU 13: Machine Check",
                                         core_idx=1, tested_core_idx=1))
    presenter.on_hardware_error(MceEvent(cpu=None, message="[Hardware Error]: Corrected error"))

    texts = presenter.texts()
    assert presenter.messages[0] == ("Test starting", Tone.DEFAULT)
    assert "[VERIFIED] Self-test 4K passed! (Iterations: 1)" in texts[1]
    assert "HARDWARE ERROR CPU 13 (Core 1) while testing Core 1: mce:" in texts[2]
    assert "HARDWARE ERROR CPU ?: [Hardware Error]" in texts[3]
    assert [tone for _, tone in presenter.messages[1:]] == [Tone.PASS, Tone.FAIL, Tone.FAIL]


def test_telemetry_samples_track_current_run(two_cores):
    presenter = CapturingPresenter(two_cores)
    presenter.on_core_start(1, two_cores[0], "2T")
    assert presenter.current_stretch_mhz is None

    presenter.on_telemetry_sample(make_sample(4850.0, 4831.0))
    presenter.on_telemetry_sample(make_sample(4850.0, 4450.0))
    presenter.on_telemetry_sample(make_sample(4850.0, 4750.0))
    assert presenter.current_stretch_mhz == 100.0
    # Only samples at or above the stretching threshold are logged
    stretch_lines = [text for text in presenter.texts() if "[STRETCH]" in text]
    assert len(stretch_lines) == 2
    assert "Tgt 4850 vs Eff 4450 (-400 MHz / -8.2%)" in stretch_lines[0]

    presenter.on_core_start(1, two_cores[1], "2T")
    assert presenter.current_stretch_mhz is None


def test_core_lifecycle(two_cores):
    presenter = CapturingPresenter(two_cores)
    presenter.on_session_start(SessionInfo(total_cycles=2))
    presenter.on_cycle_start(1, "prime95")
    presenter.on_core_start(1, two_cores[0], "2T (CPUs 0+12)")
    assert presenter.current_core is two_cores[0]
    presenter.on_core_result(two_cores[0], make_result(RunStatus.PASS, completed_iterations=2, elapsed_seconds=15.0))
    assert presenter.current_core is None

    presenter.on_core_start(1, two_cores[1], "2T (CPUs 1+13)")
    presenter.on_core_result(two_cores[1], make_result(RunStatus.ERROR, error_message="FATAL ERROR: Rounding"))
    presenter.on_core_result(two_cores[1], make_result(RunStatus.INTERRUPTED))

    texts = presenter.texts()
    assert texts[0] == "▶ Starting Cycle 1 of 2 [prime95]"
    assert "Testing Core 0 (CCD 0) - 2T (CPUs 0+12)" in texts[1]
    assert texts[2].endswith("Core 0 PASS (2 iterations, 15.0s)")
    assert texts[4].endswith("Core 1 FAIL (ERROR): FATAL ERROR: Rounding")
    assert texts[5].endswith("Core 1 INTERRUPTED (Cancelled by user)")


def test_endless_cycles_are_not_numbered(two_cores):
    presenter = CapturingPresenter(two_cores)
    presenter.on_session_start(SessionInfo(total_cycles=0))
    presenter.on_cycle_start(3, "y-cruncher")
    assert presenter.texts() == ["▶ Starting Cycle 3 [y-cruncher]"]


def test_session_end_prints_summary_and_logs(tmp_path, two_cores, capsys):
    log_path = tmp_path / "session.log"
    with Logger(str(log_path)) as logger:
        presenter = Presenter(two_cores, logger=logger)
        presenter.on_notice("All selected cores have failed.")
        presenter.on_session_end(two_cores, presenter.stats, [MceEvent(cpu=1, message="mce: boom", core_idx=1)])

    out = capsys.readouterr().out
    assert "CYCLE SUMMARY RESULTS" in out
    assert "CPU 1 (Core 1): mce: boom" in out
    assert "\033[" not in out  # captured stdout is not a terminal
    log = log_path.read_text()
    assert "[!] All selected cores have failed." in log
    assert "CYCLE SUMMARY RESULTS" in log


def test_console_presenter_prints_lines(two_cores, capsys):
    presenter = ConsolePresenter(two_cores)
    presenter.on_output_line("[2026-09-23T15:01:42] Worker starting")
    assert capsys.readouterr().out == "[2026-09-23T15:01:42] Worker starting\n"


def texts_of(log: LogBuffer, visible: int) -> list[str]:
    return [text for text, _ in log.view(visible)[0]]


def test_log_buffer_follow_and_scroll():
    log = LogBuffer(maxlen=100)
    for i in range(10):
        log.append(f"line {i}", Tone.DEFAULT)

    assert (texts_of(log, 3), log.view(3)[1]) == (["line 7", "line 8", "line 9"], 0)

    log.scroll(2, 3)
    assert texts_of(log, 3) == ["line 5", "line 6", "line 7"]
    # A scrolled-up view stays anchored while new lines arrive
    log.append("line 10")
    assert texts_of(log, 3) == ["line 5", "line 6", "line 7"]

    log.scroll(100, 3)
    assert (texts_of(log, 3), log.view(3)[1]) == (["line 0", "line 1", "line 2"], 8)
    log.scroll_to_oldest(3)
    assert log.view(3)[1] == 8
    log.follow()
    assert log.view(3)[1] == 0
    log.scroll(-5, 3)
    assert log.view(3)[1] == 0


def test_log_buffer_strips_ansi_and_is_bounded():
    log = LogBuffer(maxlen=2)
    for text in ("\033[31ma\033[0m", "b", "c"):
        log.append(text)
    assert texts_of(log, 5) == ["b", "c"]
    log.append("\033[1mbold\033[0m", Tone.PASS)
    assert log.view(1)[0] == [("bold", Tone.PASS)]
