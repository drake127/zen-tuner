"""
Unit tests for CursesPresenter state tracking, log buffering, and lifecycle.
"""

import tempfile
import unittest
from unittest.mock import MagicMock

from lib.models import MceEvent, PhysicalCore, RunResult, StretchSample
from lib.tui import CursesPresenter
from lib.ui import Logger


class TestTui(unittest.TestCase):

    def setUp(self):
        self.cores = [
            PhysicalCore(core_idx=0, hardware_core_id=0, ccd_id=0, logical_cpus=[0, 12]),
            PhysicalCore(core_idx=1, hardware_core_id=1, ccd_id=0, logical_cpus=[1, 13]),
        ]

    def test_log_buffering_and_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = Logger(f"{tmpdir}/test.log")
            presenter = CursesPresenter(all_cores=self.cores, duration_per_core=60.0, logger=logger)

            presenter.on_output_line("Test starting")
            self.assertEqual(len(presenter.log_lines), 1)
            self.assertEqual(presenter.log_lines[0][0], "Test starting")

            presenter.on_test_verified("4K", 1)
            self.assertIn("[VERIFIED]", presenter.log_lines[1][0])

            sample = StretchSample(cpu=0, target_mhz=4800, effective_mhz=4400, stretch_mhz=400, stretch_pct=8.3)
            presenter.on_stretching_detected(sample)
            self.assertIn("[STRETCH]", presenter.log_lines[2][0])

            mce = MceEvent(cpu=0, message="Hardware error", is_active_core=True)
            presenter.on_hardware_error(mce)
            self.assertIn("[ACTIVE ERROR]", presenter.log_lines[3][0])

            logger.close()
            presenter.close()

    def test_core_lifecycle_and_stats(self):
        from unittest.mock import MagicMock
        from lib.orchestrator import ZenTunerOrchestrator

        presenter = CursesPresenter(all_cores=self.cores, duration_per_core=30.0)
        orch = ZenTunerOrchestrator(runner=MagicMock(), all_cores=self.cores, presenter=presenter)

        presenter.print_cycle_start(1, 2)
        self.assertEqual(presenter.cycle_num, 1)

        presenter.print_core_start(1, self.cores[0], "2T")
        self.assertEqual(presenter.current_core, self.cores[0])

        pass_result = RunResult(
            passed=True,
            status="PASS",
            tested_cpus=[0, 12],
            completed_tests=2,
            elapsed_seconds=15.0,
            avg_target_mhz=4850.0,
            avg_effective_mhz=4850.0,
        )
        orch._accumulate_stats(self.cores[0].core_idx, pass_result)
        presenter.print_core_result(self.cores[0], pass_result)
        self.assertIsNone(presenter.current_core)
        self.assertEqual(presenter.stats[0].passes, 1)
        self.assertEqual(presenter.stats[0].verified_tests, 2)

    def test_refresh_interval_and_smu_formatting(self):
        from lib.models import CoreSmuMetrics, PackageSmuMetrics, SmuSnapshot

        presenter = CursesPresenter(all_cores=self.cores, duration_per_core=30.0, refresh_interval=2.5)
        self.assertEqual(presenter.refresh_interval, 2.5)

        snap = SmuSnapshot(
            cores={
                0: CoreSmuMetrics(
                    core_idx=0,
                    voltage_v=1.35,
                    power_w=15.2,
                    temp_c=68.0,
                    frequency_mhz=4850.0,
                    effective_mhz=4800.0,
                    c0_pct=99.5,
                    c1_pct=0.0,
                    c6_pct=0.5,
                ),
                1: CoreSmuMetrics(
                    core_idx=1,
                    voltage_v=0.95,
                    power_w=1.2,
                    temp_c=42.0,
                    frequency_mhz=3600.0,
                    effective_mhz=350.0,
                    c0_pct=1.0,
                    c1_pct=0.0,
                    c6_pct=99.0,
                ),
            },
            package=PackageSmuMetrics(
                socket_power_w=75.0,
                package_temp_c=70.0,
                ppt_w=75.0,
                ppt_limit_w=142.0,
                tdc_a=45.0,
                tdc_limit_a=95.0,
                edc_a=80.0,
                edc_limit_a=140.0,
                soc_voltage_v=0.9750,
                vddp_voltage_v=0.8471,
                vddg_ccd_voltage_v=0.8471,
                vddg_iod_voltage_v=0.8973,
            ),
            pm_version=0x380805,
        )
        presenter.last_smu_snapshot = snap
        self.assertEqual(presenter.last_smu_snapshot.cores[0].c0_pct, 99.5)
        self.assertEqual(presenter.last_smu_snapshot.cores[1].c6_pct, 99.0)
        self.assertAlmostEqual(presenter.last_smu_snapshot.package.soc_voltage_v, 0.9750, places=4)
        self.assertAlmostEqual(presenter.last_smu_snapshot.package.vddp_voltage_v, 0.8471, places=4)
        self.assertAlmostEqual(presenter.last_smu_snapshot.package.vddg_ccd_voltage_v, 0.8471, places=4)
        self.assertAlmostEqual(presenter.last_smu_snapshot.package.vddg_iod_voltage_v, 0.8973, places=4)
        presenter.close()

    def test_tui_ccd_and_disabled_slots(self):
        from lib.models import CoreSmuMetrics, PackageSmuMetrics, SmuSnapshot

        presenter = CursesPresenter(all_cores=self.cores, duration_per_core=30.0)
        slots = [
            CoreSmuMetrics(
                core_idx=0,
                slot_idx=0,
                ccd_idx=0,
                is_enabled=True,
                voltage_v=1.35,
                power_w=15.0,
                temp_c=65.0,
                frequency_mhz=4800.0,
                effective_mhz=4750.0,
                c0_pct=99.0,
            ),
            CoreSmuMetrics(
                core_idx=None,
                slot_idx=1,
                ccd_idx=0,
                is_enabled=False,
                voltage_v=0.0,
                power_w=0.0,
                temp_c=0.0,
                frequency_mhz=0.0,
                effective_mhz=0.0,
                c0_pct=0.0,
            ),
            CoreSmuMetrics(
                core_idx=1,
                slot_idx=8,
                ccd_idx=1,
                is_enabled=True,
                voltage_v=1.00,
                power_w=2.0,
                temp_c=40.0,
                frequency_mhz=3600.0,
                effective_mhz=350.0,
                c0_pct=1.0,
            ),
        ]
        snap = SmuSnapshot(
            cores={0: slots[0], 1: slots[2]},
            package=PackageSmuMetrics(0, 0, 0, 0, 0, 0, 0, 0),
            pm_version=0x380805,
            slots=slots,
        )
        presenter.last_smu_snapshot = snap
        self.assertEqual(len(presenter.last_smu_snapshot.slots), 3)
        self.assertEqual(CursesPresenter.COLOR_DISABLED, 6)
        calls = []

        def fake_safe_addstr(y, x, text, attr=0):
            calls.append((text, attr))

        presenter._safe_addstr = fake_safe_addstr
        presenter._stdscr = MagicMock()
        presenter._curses_active = True
        presenter._safe_color_pair = MagicMock(return_value=16)

        presenter._draw_cores_pane(top=4, left=75, height=30, width=85)
        disabled_calls = [c for c in calls if "DISABLED" in c[0]]
        self.assertTrue(len(disabled_calls) > 0)
        # Verify disabled call used safe_color_pair(COLOR_DISABLED) | curses.A_DIM
        import curses
        self.assertTrue(disabled_calls[0][1] & curses.A_DIM)
        presenter._safe_color_pair.assert_any_call(CursesPresenter.COLOR_DISABLED)

        presenter.close()

    def test_tui_interrupted_does_not_fail(self):
        presenter = CursesPresenter(all_cores=self.cores, duration_per_core=30.0)
        presenter.print_core_start(1, self.cores[0], "2T")
        self.assertEqual(presenter.current_core, self.cores[0])

        int_res = RunResult(
            passed=False,
            status="INTERRUPTED",
            tested_cpus=[0, 12],
            completed_tests=0,
            elapsed_seconds=3.0,
            error_message="Test interrupted by user",
        )
        presenter.print_core_result(self.cores[0], int_res)
        self.assertIsNone(presenter.current_core)
        # Failures must NOT be incremented
        self.assertEqual(presenter.stats[0].failures, 0)
        self.assertEqual(presenter.stats[0].passes, 0)
        self.assertIn("INTERRUPTED", presenter.log_lines[-1][0])
        presenter.close()

    def test_tui_smu_metrics_captured_at_result_time(self):
        """SMU voltage/power/temp/CO are captured into CoreStats at print_core_result time."""
        from lib.models import CoreSmuMetrics, PackageSmuMetrics, SmuSnapshot

        presenter = CursesPresenter(all_cores=self.cores, duration_per_core=30.0)
        snap = SmuSnapshot(
            cores={
                0: CoreSmuMetrics(
                    core_idx=0, voltage_v=1.258, power_w=20.5, temp_c=77.0,
                    frequency_mhz=4834.0, effective_mhz=4834.0, c0_pct=100.0, co_offset=-25,
                ),
            },
            package=PackageSmuMetrics(0, 0, 0, 0, 0, 0, 0, 0),
            pm_version=0x380805,
        )
        presenter.last_smu_snapshot = snap
        presenter.print_core_start(1, self.cores[0], "2T")

        from unittest.mock import MagicMock
        from lib.orchestrator import ZenTunerOrchestrator
        orch = ZenTunerOrchestrator(runner=MagicMock(), all_cores=self.cores, presenter=presenter)

        pass_result = RunResult(
            passed=True, status="PASS", tested_cpus=[0, 12],
            completed_tests=2, elapsed_seconds=15.0,
            avg_stretch_mhz=42.0, avg_target_mhz=4834.0, avg_effective_mhz=4792.0,
        )
        orch._accumulate_stats(self.cores[0].core_idx, pass_result)
        presenter.print_core_result(self.cores[0], pass_result)

        st = presenter.stats[0]
        self.assertAlmostEqual(st.avg_voltage_v, 1.258, places=3)
        self.assertAlmostEqual(st.avg_power_w, 20.5, places=1)
        self.assertAlmostEqual(st.avg_temp_c, 77.0, places=0)
        self.assertEqual(st.co_offset, -25)
        self.assertAlmostEqual(st.avg_stretch_mhz, 42.0, places=1)
        presenter.close()

    def test_tui_resize_handling(self):
        from unittest.mock import MagicMock
        presenter = CursesPresenter(all_cores=self.cores, duration_per_core=30.0)
        self.assertFalse(presenter._refresh_event.is_set())
        presenter._curses_active = True
        presenter._stdscr = MagicMock()
        presenter._handle_sigwinch()
        self.assertTrue(presenter._refresh_event.is_set())
        presenter.close()
        self.assertFalse(presenter._curses_active)

    def test_tui_cores_pane_columns_and_density(self):
        from unittest.mock import MagicMock
        from lib.models import CoreSmuMetrics, PackageSmuMetrics, SmuSnapshot

        presenter = CursesPresenter(all_cores=self.cores, duration_per_core=30.0)
        presenter.stats[0].passes = 3
        presenter.stats[0].avg_target_mhz = 4850.0

        slots = [
            CoreSmuMetrics(
                core_idx=0,
                slot_idx=0,
                ccd_idx=0,
                is_enabled=True,
                voltage_v=1.35,
                power_w=15.25,
                temp_c=65.0,
                frequency_mhz=4850.0,
                effective_mhz=4800.0,
                c0_pct=99.0,
                co_offset=-25,
            ),
        ]
        presenter.last_smu_snapshot = SmuSnapshot(
            cores={0: slots[0]},
            package=PackageSmuMetrics(0, 0, 0, 0, 0, 0, 0, 0),
            pm_version=0x380805,
            slots=slots,
        )

        mock_stdscr = MagicMock()
        mock_stdscr.getmaxyx.return_value = (40, 160)
        calls = []

        def fake_safe_addstr(y, x, text, attr=0):
            calls.append(text)

        presenter._safe_addstr = fake_safe_addstr
        presenter._stdscr = mock_stdscr
        presenter._curses_active = True

        # Draw cores pane on wide screen (width 85)
        presenter._draw_cores_pane(top=4, left=75, height=30, width=85)

        rendered_text = " ".join(calls)
        self.assertIn("Pass", rendered_text)
        self.assertIn("CO", rendered_text)
        self.assertIn("Target", rendered_text)
        self.assertIn("-25", rendered_text)
        self.assertIn("15.25W", rendered_text)
        self.assertIn("C0%", rendered_text)
        self.assertIn("C1%", rendered_text)
        self.assertIn("C6%", rendered_text)

        presenter.close()

    def test_tui_preferred_core_asterisk(self):
        import curses
        cores = [
            PhysicalCore(0, 0, 0, [0, 12], is_preferred=True, pref_rank=1),
            PhysicalCore(1, 1, 0, [1, 13], is_preferred=False, pref_rank=2),
            PhysicalCore(2, 2, 0, [2, 14], is_preferred=False, pref_rank=3),
        ]
        presenter = CursesPresenter(all_cores=cores, duration_per_core=30.0)

        # Mark core 0 as PASS (green) to verify green does NOT override gold star
        presenter.stats[0].passes = 1

        calls = []

        def fake_safe_addstr(y, x, text, attr=0):
            calls.append((text, attr))

        presenter._safe_addstr = fake_safe_addstr
        presenter._stdscr = MagicMock()
        presenter._curses_active = True
        presenter._safe_color_pair = lambda pair: pair * 10

        # Non-SMU path
        presenter._draw_cores_pane(top=0, left=0, height=20, width=50)
        rendered_texts = [c[0] for c in calls]
        rendered_str = "\n".join(rendered_texts)
        self.assertIn("*  0", rendered_str)
        self.assertIn("*  1", rendered_str)
        self.assertIn("   2", rendered_str)

        # Verify separate star calls were made with gold and silver attributes
        star_calls = [c for c in calls if c[0] == "*"]
        self.assertEqual(len(star_calls), 2)
        # Gold star for core 0
        gold_attr = presenter._safe_color_pair(presenter.COLOR_WARN) | curses.A_BOLD
        self.assertEqual(star_calls[0][1], gold_attr)
        # Silver star for core 1
        silver_attr = presenter._safe_color_pair(presenter.COLOR_SILVER) | curses.A_BOLD
        self.assertEqual(star_calls[1][1], silver_attr)

        # Summary table path
        from lib.ui import strip_ansi
        logged = []
        presenter.logger = MagicMock()
        presenter.logger.log = lambda t: logged.append(t)
        presenter.print_summary_table(cores, presenter.stats)
        raw_output = "\n".join(logged)
        # Check raw ANSI for gold (\033[33m) and silver (\033[37m)
        self.assertIn("\033[33m*\033[0m", raw_output)
        self.assertIn("\033[37m*\033[0m", raw_output)

        table_output = strip_ansi(raw_output)
        self.assertIn("*  0", table_output)
        self.assertIn("*  1", table_output)
        self.assertIn("   2", table_output)

    def test_tui_live_target_and_drop(self):
        from lib.models import CoreSmuMetrics, PackageSmuMetrics, SmuSnapshot
        cores = [PhysicalCore(0, 0, 0, [0, 12])]
        presenter = CursesPresenter(all_cores=cores, duration_per_core=30.0)
        presenter.current_core = cores[0]  # ACTIVE

        slot = CoreSmuMetrics(
            core_idx=0, slot_idx=0, ccd_idx=0, is_enabled=True,
            voltage_v=1.35, power_w=15.0, temp_c=65.0,
            frequency_mhz=4850.0, effective_mhz=4650.0,
            c0_pct=99.0, c1_pct=1.0, c6_pct=0.0,
        )
        presenter.last_smu_snapshot = SmuSnapshot(
            cores={0: slot}, package=PackageSmuMetrics(0, 0, 0, 0, 0, 0, 0, 0),
            pm_version=0x380805, slots=[slot],
        )

        calls = []
        presenter._safe_addstr = lambda y, x, text, attr=0: calls.append(text)
        presenter._curses_active = True

        # Render wide cores pane
        presenter._draw_cores_pane(top=0, left=0, height=20, width=100)
        rendered = "\n".join(calls)
        # Should display Clock 4650, Target 4850, Drop -200M
        self.assertIn("4650", rendered)
        self.assertIn("4850", rendered)
        self.assertIn("-200M", rendered)

        # Now test noise (4848 vs 4850 MHz) - must NOT show -2M, must show 0M
        slot_noise = CoreSmuMetrics(
            core_idx=0, slot_idx=0, ccd_idx=0, is_enabled=True,
            voltage_v=1.35, power_w=15.0, temp_c=65.0,
            frequency_mhz=4850.0, effective_mhz=4848.0,
            c0_pct=99.0, c1_pct=1.0, c6_pct=0.0,
        )
        presenter.last_smu_snapshot = SmuSnapshot(
            cores={0: slot_noise}, package=PackageSmuMetrics(0, 0, 0, 0, 0, 0, 0, 0),
            pm_version=0x380805, slots=[slot_noise],
        )
        calls.clear()
        presenter._draw_cores_pane(top=0, left=0, height=20, width=100)
        rendered_noise = "\n".join(calls)
        self.assertNotIn("-2M", rendered_noise)
        self.assertIn("0M", rendered_noise)

        presenter.close()


    def test_summary_table_displays_all_cores_co(self):
        from unittest.mock import MagicMock
        from lib.ui import strip_ansi

        cores = [
            PhysicalCore(0, 0, 0, [0, 12]),
            PhysicalCore(1, 1, 0, [1, 13]),
        ]
        smu_mock = MagicMock()
        smu_mock.is_available.return_value = True
        smu_mock.read_all_co_offsets.return_value = {0: -20, 1: -15}

        presenter = CursesPresenter(all_cores=cores, duration_per_core=30.0, smu_monitor=smu_mock)
        # Verify stats received CO offsets upon initialization
        self.assertEqual(presenter.stats[0].co_offset, -20)
        self.assertEqual(presenter.stats[1].co_offset, -15)

        # Core 0 ran and passed, Core 1 never ran (SKIPPED / not yet run)
        presenter.stats[0].passes = 1
        logged = []
        presenter.logger = MagicMock()
        presenter.logger.log = lambda t: logged.append(t)

        presenter.print_summary_table(cores, presenter.stats)
        table_output = strip_ansi("\n".join(logged))

        # Both cores should display their CO offsets in the table
        self.assertIn("-20", table_output)
        self.assertIn("-15", table_output)
        self.assertIn("SKIPPED", table_output)


if __name__ == "__main__":
    unittest.main()


