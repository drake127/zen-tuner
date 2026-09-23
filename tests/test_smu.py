"""
Unit tests for AMD Ryzen SMU PM table parser and RyzenSmuMonitor.
"""

import os
import struct
import tempfile
import unittest

from lib.smu import (
    PM_VER_ZEN3_VERMEER_1,
    PM_VER_ZEN3_VERMEER_2,
    RyzenSmuMonitor,
    parse_pm_table_buffer,
)


def create_mock_pm_buffer_zen3_v1() -> bytes:
    """Builds a synthetic 4096-byte PM table buffer matching version 0x380804."""
    buf = bytearray(4096)

    def set_f(idx: int, val: float) -> None:
        struct.pack_into("<f", buf, idx * 4, val)

    # Package limits
    set_f(0, 142.0)  # PPT Limit W
    set_f(1, 78.5)   # PPT Value W
    set_f(2, 95.0)   # TDC Limit A
    set_f(3, 45.2)   # TDC Value A
    set_f(4, 90.0)   # TjMax °C
    set_f(5, 65.5)   # Package Temp °C
    set_f(8, 140.0)  # EDC Limit A
    set_f(15, 90.1)  # EDC Value A
    set_f(29, 80.0)  # Socket Power W
    set_f(45, 0.957) # SoC V (actual after droop)
    set_f(137, 0.85) # VDDP V
    set_f(138, 0.90) # VDDG IOD V
    set_f(139, 0.85) # VDDG CCD V

    # Core 0 metrics (offsets for 0x380804)
    set_f(169, 12.5)  # Power W
    set_f(185, 1.35)  # Voltage V
    set_f(201, 67.2)  # Temp °C
    set_f(249, 4.85)  # Freq GHz (4850 MHz)
    set_f(265, 4.80)  # Freq Eff GHz (4800 MHz)
    set_f(281, 99.8)  # C0 %

    return bytes(buf)


def create_mock_pm_buffer_zen3_v2() -> bytes:
    """Builds a synthetic 4096-byte PM table buffer matching version 0x380805."""
    buf = bytearray(4096)

    def set_f(idx: int, val: float) -> None:
        struct.pack_into("<f", buf, idx * 4, val)

    # Package limits
    set_f(0, 142.0)
    set_f(1, 85.0)
    set_f(2, 95.0)
    set_f(3, 50.0)
    set_f(5, 70.0)
    set_f(8, 140.0)
    set_f(15, 100.0)
    set_f(29, 88.0)
    set_f(45, 0.957) # SoC V (actual after droop)
    set_f(137, 0.85)  # VDDP V
    set_f(138, 0.90)  # VDDG IOD V
    set_f(139, 0.85)  # VDDG CCD V

    # Core 0 metrics (offsets for 0x380805)
    set_f(172, 14.0)  # Power W
    set_f(188, 1.38)  # Voltage V
    set_f(204, 71.0)  # Temp °C
    set_f(252, 4.90)  # Freq GHz
    set_f(268, 4.88)  # Freq Eff GHz
    set_f(284, 100.0) # C0 %

    return bytes(buf)


class TestRyzenSmu(unittest.TestCase):

    def test_parse_zen3_v1(self):
        raw = create_mock_pm_buffer_zen3_v1()
        snapshot = parse_pm_table_buffer(PM_VER_ZEN3_VERMEER_1, raw, max_cores=12)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.pm_version, PM_VER_ZEN3_VERMEER_1)
        self.assertAlmostEqual(snapshot.package.ppt_limit_w, 142.0, places=1)
        self.assertAlmostEqual(snapshot.package.ppt_w, 78.5, places=1)
        self.assertAlmostEqual(snapshot.package.socket_power_w, 80.0, places=1)
        self.assertAlmostEqual(snapshot.package.package_temp_c, 65.5, places=1)
        self.assertAlmostEqual(snapshot.package.soc_voltage_v, 0.957, places=3)
        self.assertAlmostEqual(snapshot.package.vddp_voltage_v, 0.85, places=2)
        self.assertAlmostEqual(snapshot.package.vddg_ccd_voltage_v, 0.85, places=2)
        self.assertAlmostEqual(snapshot.package.vddg_iod_voltage_v, 0.90, places=2)

        c0 = snapshot.cores[0]
        self.assertAlmostEqual(c0.voltage_v, 1.35, places=2)
        self.assertAlmostEqual(c0.power_w, 12.5, places=1)
        self.assertAlmostEqual(c0.temp_c, 67.2, places=1)
        self.assertAlmostEqual(c0.frequency_mhz, 4850.0, places=1)
        self.assertAlmostEqual(c0.effective_mhz, 4800.0, places=1)
        self.assertAlmostEqual(c0.c0_pct, 99.8, places=1)

    def test_parse_zen3_v2(self):
        raw = create_mock_pm_buffer_zen3_v2()
        snapshot = parse_pm_table_buffer(PM_VER_ZEN3_VERMEER_2, raw, max_cores=12)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.pm_version, PM_VER_ZEN3_VERMEER_2)
        c0 = snapshot.cores[0]
        self.assertAlmostEqual(c0.voltage_v, 1.38, places=2)
        self.assertAlmostEqual(c0.power_w, 14.0, places=1)
        self.assertAlmostEqual(c0.temp_c, 71.0, places=1)
        self.assertAlmostEqual(snapshot.package.soc_voltage_v, 0.957, places=3)
        self.assertAlmostEqual(snapshot.package.vddp_voltage_v, 0.85, places=2)
        self.assertAlmostEqual(snapshot.package.vddg_ccd_voltage_v, 0.85, places=2)
        self.assertAlmostEqual(snapshot.package.vddg_iod_voltage_v, 0.90, places=2)

    def test_parse_unsupported_version(self):
        raw = create_mock_pm_buffer_zen3_v1()
        self.assertIsNone(parse_pm_table_buffer(0x999999, raw))

    def test_parse_truncated_buffer(self):
        raw = b"\x00" * 100
        self.assertIsNone(parse_pm_table_buffer(PM_VER_ZEN3_VERMEER_1, raw))

    def test_get_smu_table_offsets_fallback(self):
        from lib.smu import (
            PM_VER_ZEN1_GENERIC,
            PM_VER_ZEN2_GENERIC,
            PM_VER_ZEN2_MATISSE,
            PM_VER_ZEN3_GENERIC,
            PM_VER_ZEN3_VERMEER_1,
            PM_VER_ZEN3_VERMEER_2,
            get_smu_table_offsets,
        )

        # Exact match
        self.assertEqual(get_smu_table_offsets(PM_VER_ZEN3_VERMEER_1).core_power, 169)
        self.assertEqual(get_smu_table_offsets(PM_VER_ZEN3_VERMEER_2).core_power, 172)
        self.assertEqual(get_smu_table_offsets(PM_VER_ZEN2_MATISSE).core_power, 147)

        # Prefix fallback for Zen 3 family
        self.assertEqual(get_smu_table_offsets(0x380899), get_smu_table_offsets(PM_VER_ZEN3_GENERIC))
        self.assertEqual(get_smu_table_offsets(0x2D0999), get_smu_table_offsets(PM_VER_ZEN3_GENERIC))

        # Prefix fallback for Zen 2 family
        self.assertEqual(get_smu_table_offsets(0x240999), get_smu_table_offsets(PM_VER_ZEN2_GENERIC))
        self.assertEqual(get_smu_table_offsets(0x370002), get_smu_table_offsets(PM_VER_ZEN2_GENERIC))

        # Zen 1 family
        self.assertEqual(get_smu_table_offsets(0x100), get_smu_table_offsets(PM_VER_ZEN1_GENERIC))

        # Unsupported
        self.assertIsNone(get_smu_table_offsets(0x999999))

    def test_monitor_unavailable_fallback(self):
        mon = RyzenSmuMonitor(sysfs_dir="/tmp/non_existent_smu_dir_123")
        self.assertFalse(mon.is_available())
        self.assertIsNone(mon.get_pm_version())
        self.assertIsNone(mon.read_snapshot())
        mon.close()

    def test_monitor_mocked_sysfs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            v_path = os.path.join(tmpdir, "pm_table_version")
            pm_path = os.path.join(tmpdir, "pm_table")

            with open(v_path, "wb") as f:
                f.write(struct.pack("<I", PM_VER_ZEN3_VERMEER_1))

            with open(pm_path, "wb") as f:
                f.write(create_mock_pm_buffer_zen3_v1())

            with RyzenSmuMonitor(sysfs_dir=tmpdir) as mon:
                self.assertTrue(mon.is_available())
                self.assertEqual(mon.get_pm_version(), PM_VER_ZEN3_VERMEER_1)
                snap = mon.read_snapshot(max_cores=12)
                self.assertIsNotNone(snap)
                self.assertAlmostEqual(snap.cores[0].voltage_v, 1.35, places=2)

    def test_monitor_prefix_fallback(self):
        # Test that unlisted Zen 3 minor version (e.g. 0x380899) falls back and reads snapshot
        with tempfile.TemporaryDirectory() as tmpdir:
            v_path = os.path.join(tmpdir, "pm_table_version")
            pm_path = os.path.join(tmpdir, "pm_table")

            with open(v_path, "wb") as f:
                f.write(struct.pack("<I", 0x380899))

            with open(pm_path, "wb") as f:
                f.write(create_mock_pm_buffer_zen3_v2())

            with RyzenSmuMonitor(sysfs_dir=tmpdir) as mon:
                self.assertTrue(mon.is_available())
                snap = mon.read_snapshot(max_cores=12)
                self.assertIsNotNone(snap)
                self.assertEqual(snap.pm_version, 0x380899)
                self.assertAlmostEqual(snap.cores[0].voltage_v, 1.38, places=2)

    def test_fused_off_core_slot_mapping(self):
        # Build buffer where slot 0, 1 are enabled, slot 2 is fused off, slot 3 is enabled
        buf = bytearray(4096)

        def set_f(idx: int, val: float) -> None:
            struct.pack_into("<f", buf, idx * 4, val)

        # Offsets for Zen 3 Vermeer v1 (0x380804): FIT offset is 217, Voltage is 185
        set_f(217 + 0, 1.0)  # slot 0 FIT enabled
        set_f(217 + 1, 1.0)  # slot 1 FIT enabled
        set_f(217 + 2, 0.0)  # slot 2 fused off!
        set_f(217 + 3, 1.0)  # slot 3 FIT enabled

        set_f(185 + 0, 1.10)  # slot 0 volt
        set_f(185 + 1, 1.15)  # slot 1 volt
        set_f(185 + 2, 0.00)  # slot 2 volt (0V)
        set_f(185 + 3, 1.32)  # slot 3 volt (1.32V)

        snapshot = parse_pm_table_buffer(PM_VER_ZEN3_VERMEER_1, bytes(buf), max_cores=3)
        self.assertIsNotNone(snapshot)
        self.assertEqual(len(snapshot.cores), 3)
        self.assertAlmostEqual(snapshot.cores[0].voltage_v, 1.10, places=2)
        self.assertAlmostEqual(snapshot.cores[1].voltage_v, 1.15, places=2)
        # Linux core 2 should map to physical slot 3 (1.32V), skipping fused-off slot 2
        self.assertAlmostEqual(snapshot.cores[2].voltage_v, 1.32, places=2)

        # Verify physical silicon slots representation
        self.assertEqual(len(snapshot.slots), 8)
        self.assertTrue(snapshot.slots[0].is_enabled)
        self.assertEqual(snapshot.slots[0].core_idx, 0)
        self.assertFalse(snapshot.slots[2].is_enabled)
        self.assertIsNone(snapshot.slots[2].core_idx)
        self.assertEqual(snapshot.slots[2].slot_idx, 2)
        self.assertEqual(snapshot.slots[2].ccd_idx, 0)
        self.assertTrue(snapshot.slots[3].is_enabled)
        self.assertEqual(snapshot.slots[3].core_idx, 2)

    def test_co_offset_mailbox_query(self):
        from unittest.mock import MagicMock
        mon = RyzenSmuMonitor(sysfs_dir="/tmp")
        # Mock SMU mailbox response for Core 0: -25 (encoded as (1<<32)-25)
        u32_val = (1 << 32) - 25
        mon.send_smu_command = MagicMock(return_value=(u32_val, 0, 0, 0, 0, 0))

        co = mon.read_co_offset(0)
        self.assertEqual(co, -25)
        mon.send_smu_command.assert_called_once_with(0x48, arg1=0)

        # Core 8 (CCD 1) bit manipulation: ((8 & 8) << 5 | (8 & 7)) << 20 = 256 << 20 = 0x10000000
        mon.send_smu_command.reset_mock()
        mon.read_co_offset(8)
        mon.send_smu_command.assert_called_once_with(0x48, arg1=0x10000000)

        # Test parse_pm_table_buffer populates co_offset
        raw = create_mock_pm_buffer_zen3_v1()
        snap = parse_pm_table_buffer(PM_VER_ZEN3_VERMEER_1, raw, max_cores=2, co_offsets={0: -25, 1: -15})
        self.assertIsNotNone(snap)
        self.assertEqual(snap.cores[0].co_offset, -25)


if __name__ == "__main__":
    unittest.main()

