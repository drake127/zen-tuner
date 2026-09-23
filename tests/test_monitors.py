"""
Unit tests for hardware telemetry calculations and kernel error parsing.
"""

import unittest
from lib.monitors import KernelErrorMonitor, CycleStretchingMonitor, calculate_clock_metrics


class TestMonitors(unittest.TestCase):

    def test_calculate_clock_metrics_normal(self):
        # 1 second sample, 3700 MHz TSC rate, 100% C0 busy, 4800 MHz executed clock
        delta_t = 1.0
        delta_tsc = 3_700_000_000
        delta_mperf = 3_700_000_000  # 100% busy
        delta_aperf = 4_800_000_000  # 4800 MHz
        target_mhz = 4800.0

        res = calculate_clock_metrics(delta_aperf, delta_mperf, delta_tsc, delta_t, target_mhz)
        self.assertIsNotNone(res)
        tsc_mhz, busy_pct, effective_mhz, stretch_mhz, stretch_pct = res

        self.assertAlmostEqual(tsc_mhz, 3700.0, places=1)
        self.assertAlmostEqual(busy_pct, 100.0, places=1)
        self.assertAlmostEqual(effective_mhz, 4800.0, places=1)
        self.assertAlmostEqual(stretch_mhz, 0.0, places=1)
        self.assertAlmostEqual(stretch_pct, 0.0, places=1)

    def test_calculate_clock_metrics_stretched(self):
        # Target is 4850 MHz, but actual executed cycles are only 4400 MHz (clock stretching)
        delta_t = 1.0
        delta_tsc = 3_700_000_000
        delta_mperf = 3_700_000_000
        delta_aperf = 4_400_000_000  # 4400 MHz
        target_mhz = 4850.0

        res = calculate_clock_metrics(delta_aperf, delta_mperf, delta_tsc, delta_t, target_mhz)
        self.assertIsNotNone(res)
        _, _, effective_mhz, stretch_mhz, stretch_pct = res

        self.assertAlmostEqual(effective_mhz, 4400.0, places=1)
        self.assertAlmostEqual(stretch_mhz, 450.0, places=1)
        self.assertGreater(stretch_pct, 9.0)

    def test_calculate_clock_metrics_division_by_zero_safety(self):
        self.assertIsNone(calculate_clock_metrics(0, 0, 1000, 1.0, 4000.0))
        self.assertIsNone(calculate_clock_metrics(1000, 1000, 0, 1.0, 4000.0))
        self.assertIsNone(calculate_clock_metrics(1000, 1000, 1000, 0.0, 4000.0))

    def test_stretching_monitor_avg_stretch_property(self):
        """avg_stretch_mhz returns mean across all qualifying samples."""
        mon = CycleStretchingMonitor(cpus=[], threshold_mhz=100.0)
        # Inject samples directly (bypassing MSR polling)
        mon._stretch_sum = 300.0
        mon._stretch_count = 3
        self.assertAlmostEqual(mon.avg_stretch_mhz, 100.0, places=1)

        mon2 = CycleStretchingMonitor(cpus=[], threshold_mhz=100.0)
        self.assertAlmostEqual(mon2.avg_stretch_mhz, 0.0, places=1)

    def test_stretching_monitor_busy_threshold_is_95_pct(self):
        """Verify that the busy_pct threshold constant in poll is 95% (not 80%)."""
        import inspect
        src = inspect.getsource(CycleStretchingMonitor.poll)
        self.assertIn("95.0", src, "busy_pct threshold must be 95%")
        self.assertNotIn("80.0", src, "old 80% threshold must be removed")

    def test_kernel_error_monitor_parse_active_mce(self):
        mon = KernelErrorMonitor(tested_cpus=[0, 12])
        line = "[12345.678] [Hardware Error]: CPU 0: Machine Check Exception: Bank 5: ..."
        ev = mon.parse_line(line)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.cpu, 0)
        self.assertTrue(ev.is_active_core)

    def test_kernel_error_monitor_parse_idle_mce(self):
        mon = KernelErrorMonitor(tested_cpus=[0, 12])
        line = "[12345.678] mce: [Hardware Error]: CPU 4: Machine Check Exception ..."
        ev = mon.parse_line(line)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.cpu, 4)
        self.assertFalse(ev.is_active_core)

    def test_kernel_error_monitor_ignore_benign_logs(self):
        mon = KernelErrorMonitor(tested_cpus=[0, 12])
        line = "[12345.678] eth0: Link is Up - 1000Mbps/Full"
        self.assertIsNone(mon.parse_line(line))


    def test_stretching_monitor_with_smu(self):
        from unittest.mock import MagicMock
        from lib.models import CoreSmuMetrics, PackageSmuMetrics, SmuSnapshot

        mock_smu = MagicMock()
        mock_smu.is_available.return_value = True
        slot = CoreSmuMetrics(
            core_idx=0, slot_idx=0, ccd_idx=0, is_enabled=True,
            voltage_v=1.35, power_w=15.0, temp_c=65.0,
            frequency_mhz=4850.0, effective_mhz=4650.0,
            c0_pct=99.0, c1_pct=1.0, c6_pct=0.0,
        )
        mock_smu.read_snapshot.return_value = SmuSnapshot(
            cores={0: slot}, package=PackageSmuMetrics(0, 0, 0, 0, 0, 0, 0, 0),
            pm_version=0x380805, slots=[slot],
        )

        mon = CycleStretchingMonitor(cpus=[0, 12], threshold_mhz=100.0, smu_monitor=mock_smu, core_idx=0)
        # Force dt >= sample_interval
        mon.last_sample_time = 0.0
        alerts = mon.poll()

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].target_mhz, 4850.0)
        self.assertEqual(alerts[0].effective_mhz, 4650.0)
        self.assertAlmostEqual(alerts[0].stretch_mhz, 200.0, places=1)
        self.assertAlmostEqual(mon.avg_stretch_mhz, 200.0, places=1)

    def test_stretching_monitor_noise_filtering(self):
        from unittest.mock import MagicMock
        from lib.models import CoreSmuMetrics, PackageSmuMetrics, SmuSnapshot

        mock_smu = MagicMock()
        mock_smu.is_available.return_value = True
        # 2 MHz difference is noise and must be filtered out
        slot = CoreSmuMetrics(
            core_idx=0, slot_idx=0, ccd_idx=0, is_enabled=True,
            voltage_v=1.35, power_w=15.0, temp_c=65.0,
            frequency_mhz=4850.0, effective_mhz=4848.0,
            c0_pct=99.0, c1_pct=1.0, c6_pct=0.0,
        )
        mock_smu.read_snapshot.return_value = SmuSnapshot(
            cores={0: slot}, package=PackageSmuMetrics(0, 0, 0, 0, 0, 0, 0, 0),
            pm_version=0x380805, slots=[slot],
        )

        mon = CycleStretchingMonitor(cpus=[0, 12], threshold_mhz=100.0, smu_monitor=mock_smu, core_idx=0)
        mon.last_sample_time = 0.0
        alerts = mon.poll()

        self.assertEqual(len(alerts), 0)
        self.assertEqual(len(mon.all_samples), 1)
        self.assertEqual(mon.all_samples[0].stretch_mhz, 0.0)
        self.assertEqual(mon.avg_stretch_mhz, 0.0)

    def test_get_target_freq_mhz_amd_pstate(self):
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            cpu_dir = os.path.join(tmpdir, "devices", "system", "cpu", "cpu0", "cpufreq")
            os.makedirs(cpu_dir)
            with open(os.path.join(cpu_dir, "scaling_driver"), "w") as f:
                f.write("amd-pstate-epp\n")
            with open(os.path.join(cpu_dir, "scaling_cur_freq"), "w") as f:
                f.write("3600000\n")
            with open(os.path.join(cpu_dir, "amd_pstate_max_freq"), "w") as f:
                f.write("4950000\n")

            mon = CycleStretchingMonitor(cpus=[0])
            tgt = mon._get_target_freq_mhz(0, sysfs_root=tmpdir)
            self.assertEqual(tgt, 4950.0)

    def test_stretching_monitor_default_threshold_is_50(self):
        mon = CycleStretchingMonitor(cpus=[0])
        self.assertEqual(mon.threshold_mhz, 50.0)

    def test_stretching_monitor_median_filters_transient_spikes(self):
        from lib.models import StretchSample
        mon = CycleStretchingMonitor(cpus=[0], threshold_mhz=50.0)
        # 59 normal samples (effective == target, 0 stretch)
        for _ in range(59):
            mon.all_samples.append(
                StretchSample(cpu=0, target_mhz=4850.0, effective_mhz=4849.0, stretch_mhz=0.0, stretch_pct=0.0)
            )
        # 1 transient drop of 19 MHz
        mon.all_samples.append(
            StretchSample(cpu=0, target_mhz=4850.0, effective_mhz=4831.0, stretch_mhz=19.0, stretch_pct=0.4)
        )
        self.assertEqual(mon.median_stretch_mhz, 0.0)
        self.assertFalse(mon.stretching_detected)
        self.assertAlmostEqual(mon.avg_stretch_mhz, 19.0 / 60.0, places=2)

    def test_stretching_monitor_median_detects_persistent_stretching(self):
        from lib.models import StretchSample
        mon = CycleStretchingMonitor(cpus=[0], threshold_mhz=50.0)
        for _ in range(60):
            mon.all_samples.append(
                StretchSample(cpu=0, target_mhz=4850.0, effective_mhz=4730.0, stretch_mhz=120.0, stretch_pct=2.5)
            )
        self.assertEqual(mon.median_stretch_mhz, 120.0)
        self.assertTrue(mon.stretching_detected)

    def test_stretching_monitor_transient_spike_above_50_does_not_flag_stretching(self):
        from lib.models import StretchSample
        mon = CycleStretchingMonitor(cpus=[0], threshold_mhz=50.0)
        for _ in range(58):
            mon.all_samples.append(
                StretchSample(cpu=0, target_mhz=4850.0, effective_mhz=4850.0, stretch_mhz=0.0, stretch_pct=0.0)
            )
        # 2 samples of 80 MHz drop (e.g. FFT size switch)
        for _ in range(2):
            mon.all_samples.append(
                StretchSample(cpu=0, target_mhz=4850.0, effective_mhz=4770.0, stretch_mhz=80.0, stretch_pct=1.6)
            )
        self.assertEqual(mon.median_stretch_mhz, 0.0)
        self.assertFalse(mon.stretching_detected)


if __name__ == "__main__":
    unittest.main()


