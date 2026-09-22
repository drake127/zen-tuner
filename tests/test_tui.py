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
        cores = [
            PhysicalCore(core_idx=0, hardware_core_id=0, ccd_id=0, logical_cpus=[0, 12], is_preferred=True),
            PhysicalCore(core_idx=1, hardware_core_id=1, ccd_id=0, logical_cpus=[1, 13], is_preferred=False),
        ]
        presenter = CursesPresenter(all_cores=cores, duration_per_core=30.0)

        calls = []

        def fake_safe_addstr(y, x, text, attr=0):
            calls.append(text)

        presenter._safe_addstr = fake_safe_addstr
        presenter._stdscr = MagicMock()
        presenter._curses_active = True

        # Non-SMU path
        presenter._draw_cores_pane(top=0, left=0, height=20, width=50)
        rendered_text = "\n".join(calls)
        self.assertIn("*  0", rendered_text)
        self.assertIn("   1", rendered_text)

        # Summary table path
        from lib.ui import strip_ansi
        logged = []
        presenter.logger = MagicMock()
        presenter.logger.log = lambda t: logged.append(t)
        presenter.print_summary_table(cores, presenter.stats)
        table_output = strip_ansi("\n".join(logged))
        self.assertIn("*  0", table_output)
        self.assertIn("   1", table_output)

        presenter.close()


if __name__ == "__main__":
    unittest.main()


