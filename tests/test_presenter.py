"""
Unit tests for presenter message formatting, console output and the scrollable log buffer.
"""

from lib.logbuffer import LogBuffer
from lib.models import MceEvent, RunResult, RunStatus, TelemetrySample
from lib.presenter import ConsolePresenter, Presenter
from lib.ui import Logger
from lib.views import Tone


class CapturingPresenter(Presenter):

    def __init__(self, cores, **kwargs):
        super().__init__(cores, **kwargs)
        self.messages: list[tuple[str, Tone]] = []

    def _emit(self, text, tone=Tone.DEFAULT):
        super()._emit(text, tone)
        self.messages.append((text, tone))


def result_of(status: RunStatus, **kwargs) -> RunResult:
    return RunResult(status=status, tested_cpus=[0, 12], completed_iterations=2, elapsed_seconds=15.0, **kwargs)


def test_runner_event_messages(two_cores):
    presenter = CapturingPresenter(two_cores)
    presenter.on_output_line("Test starting")
    presenter.on_test_verified("4K", 1)
    presenter.on_stretching_detected(
        TelemetrySample(target_mhz=4800.0, effective_mhz=4400.0, voltage_v=1.3, power_w=15.0, temp_c=70.0)
    )
    presenter.on_hardware_error(MceEvent(cpu=13, message="mce: [Hardware Error]: CPU 13: Machine Check",
                                         on_tested_cpu=True))
    presenter.on_hardware_error(MceEvent(cpu=None, message="[Hardware Error]: Corrected error", on_tested_cpu=None))

    texts = [text for text, _ in presenter.messages]
    assert presenter.messages[0] == ("Test starting", Tone.DEFAULT)
    assert "[VERIFIED] Self-test 4K passed! (Iterations: 1)" in texts[1]
    assert "[STRETCH] Tgt 4800 vs Eff 4400 (-400 MHz / -8.3%)" in texts[2]
    assert "HARDWARE ERROR CPU 13 (Core 1) [TESTED CORE]:" in texts[3]
    assert "HARDWARE ERROR CPU ?:" in texts[4]
    assert [tone for _, tone in presenter.messages[1:]] == [Tone.PASS, Tone.WARN, Tone.FAIL, Tone.FAIL]


def test_core_lifecycle(two_cores):
    presenter = CapturingPresenter(two_cores)
    presenter.on_cycle_start(1, 2, "prime95")
    presenter.on_core_start(1, two_cores[0], "2T (CPUs 0+12)")
    assert presenter.current_core is two_cores[0]
    presenter.on_core_result(two_cores[0], result_of(RunStatus.PASS))
    assert presenter.current_core is None

    presenter.on_core_start(1, two_cores[1], "2T (CPUs 1+13)")
    presenter.on_core_result(two_cores[1], result_of(RunStatus.ERROR, error_message="FATAL ERROR: Rounding"))
    presenter.on_core_result(two_cores[1], result_of(RunStatus.INTERRUPTED))

    texts = [text for text, _ in presenter.messages]
    assert texts[0] == "▶ Starting Cycle 1 of 2 [prime95]"
    assert "Testing Core 0 (CCD 0) - 2T (CPUs 0+12)" in texts[1]
    assert texts[2].endswith("Core 0 PASS (2 iterations, 15.0s)")
    assert texts[4].endswith("Core 1 FAIL (ERROR): FATAL ERROR: Rounding")
    assert texts[5].endswith("Core 1 INTERRUPTED (Cancelled by user)")


def test_session_end_prints_summary_and_logs(tmp_path, two_cores, capsys):
    log_path = tmp_path / "session.log"
    with Logger(str(log_path)) as logger:
        presenter = Presenter(two_cores, logger=logger)
        presenter.on_notice("All selected cores have failed.")
        presenter.on_session_end(two_cores, presenter.stats)

    out = capsys.readouterr().out
    assert "CYCLE SUMMARY RESULTS" in out
    assert "\033[" not in out  # captured stdout is not a terminal
    log = log_path.read_text()
    assert "[!] All selected cores have failed." in log
    assert "CYCLE SUMMARY RESULTS" in log


def test_console_presenter_prints_lines(two_cores, capsys):
    presenter = ConsolePresenter(two_cores)
    presenter.on_output_line("[2026-09-23T15:01:42] Worker starting")
    assert capsys.readouterr().out == "[2026-09-23T15:01:42] Worker starting\n"


def test_log_buffer_follow_and_scroll():
    log = LogBuffer(maxlen=100)
    for i in range(10):
        log.append(f"line {i}", Tone.DEFAULT)

    lines, offset = log.view(3)
    assert ([text for text, _ in lines], offset) == (["line 7", "line 8", "line 9"], 0)

    log.scroll(2, 3)
    assert [text for text, _ in log.view(3)[0]] == ["line 5", "line 6", "line 7"]
    # A scrolled-up view stays anchored while new lines arrive
    log.append("line 10")
    assert [text for text, _ in log.view(3)[0]] == ["line 5", "line 6", "line 7"]

    log.scroll(100, 3)
    assert log.view(3) == ([("line 0", Tone.DEFAULT), ("line 1", Tone.DEFAULT), ("line 2", Tone.DEFAULT)], 8)
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
    assert [text for text, _ in log.view(5)[0]] == ["b", "c"]
    log.append("\033[1mbold\033[0m", Tone.PASS)
    assert log[-1] == ("bold", Tone.PASS)
