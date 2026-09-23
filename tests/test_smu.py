"""
Unit tests for AMD Ryzen SMU PM table parser and RyzenSmuMonitor.
"""

import struct
from unittest.mock import MagicMock

import pytest

from lib.smu import (
    MP1_ZEN3_GET_DLDO_PSM_MARGIN,
    PM_VER_ZEN1_GENERIC,
    PM_VER_ZEN2_GENERIC,
    PM_VER_ZEN2_MATISSE,
    PM_VER_ZEN3_GENERIC,
    PM_VER_ZEN3_VERMEER_1,
    PM_VER_ZEN3_VERMEER_2,
    RyzenSmuMonitor,
    get_smu_table_offsets,
    parse_pm_table_buffer,
)


def pm_buffer(values: dict[int, float]) -> bytes:
    buf = bytearray(4096)
    for idx, val in values.items():
        struct.pack_into("<f", buf, idx * 4, val)
    return bytes(buf)


# Package limits shared by Zen 3 layouts
PACKAGE_VALUES = {
    0: 142.0,   # PPT Limit W
    1: 78.5,    # PPT Value W
    2: 95.0,    # TDC Limit A
    3: 45.2,    # TDC Value A
    4: 90.0,    # TjMax °C
    5: 65.5,    # Package Temp °C
    8: 140.0,   # EDC Limit A
    15: 90.1,   # EDC Value A
    29: 80.0,   # Socket Power W
    45: 0.957,  # SoC V
    137: 0.85,  # VDDP V
    138: 0.90,  # VDDG IOD V
    139: 0.85,  # VDDG CCD V
}

# Core 0 metrics at the 0x380804 offsets
ZEN3_V1_BUFFER = pm_buffer(PACKAGE_VALUES | {169: 12.5, 185: 1.35, 201: 67.2, 249: 4.85, 265: 4.80, 281: 99.8})
# Core 0 metrics at the 0x380805 offsets
ZEN3_V2_BUFFER = pm_buffer(PACKAGE_VALUES | {172: 14.0, 188: 1.38, 204: 71.0, 252: 4.90, 268: 4.88, 284: 100.0})


@pytest.fixture
def smu_sysfs(tmp_path):
    def write(version: int, table: bytes) -> str:
        (tmp_path / "pm_table_version").write_bytes(struct.pack("<I", version))
        (tmp_path / "pm_table").write_bytes(table)
        return str(tmp_path)
    return write


def test_parse_zen3_v1():
    snapshot = parse_pm_table_buffer(PM_VER_ZEN3_VERMEER_1, ZEN3_V1_BUFFER, core_count=12)

    assert snapshot.pm_version == PM_VER_ZEN3_VERMEER_1
    pkg = snapshot.package
    assert pkg.ppt_limit_w == pytest.approx(142.0)
    assert pkg.ppt_w == pytest.approx(78.5)
    assert pkg.edc_a == pytest.approx(90.1)
    assert pkg.socket_power_w == pytest.approx(80.0)
    assert pkg.package_temp_c == pytest.approx(65.5)
    assert pkg.soc_voltage_v == pytest.approx(0.957)
    assert pkg.vddp_voltage_v == pytest.approx(0.85)
    assert pkg.vddg_ccd_voltage_v == pytest.approx(0.85)
    assert pkg.vddg_iod_voltage_v == pytest.approx(0.90)

    c0 = snapshot.cores[0]
    assert c0.voltage_v == pytest.approx(1.35)
    assert c0.power_w == pytest.approx(12.5)
    assert c0.temp_c == pytest.approx(67.2)
    assert c0.frequency_mhz == pytest.approx(4850.0)
    assert c0.effective_mhz == pytest.approx(4800.0)
    assert c0.c0_pct == pytest.approx(99.8)


def test_parse_zen3_v2():
    snapshot = parse_pm_table_buffer(PM_VER_ZEN3_VERMEER_2, ZEN3_V2_BUFFER, core_count=12)

    assert snapshot.pm_version == PM_VER_ZEN3_VERMEER_2
    c0 = snapshot.cores[0]
    assert c0.voltage_v == pytest.approx(1.38)
    assert c0.power_w == pytest.approx(14.0)
    assert c0.temp_c == pytest.approx(71.0)
    assert snapshot.package.soc_voltage_v == pytest.approx(0.957)


def test_parse_unsupported_version():
    assert parse_pm_table_buffer(0x999999, ZEN3_V1_BUFFER) is None


def test_parse_truncated_buffer():
    assert parse_pm_table_buffer(PM_VER_ZEN3_VERMEER_1, b"\x00" * 100) is None


def test_get_smu_table_offsets_exact():
    assert get_smu_table_offsets(PM_VER_ZEN3_VERMEER_1).core_power == 169
    assert get_smu_table_offsets(PM_VER_ZEN3_VERMEER_2).core_power == 172
    assert get_smu_table_offsets(PM_VER_ZEN2_MATISSE).core_power == 147


@pytest.mark.parametrize(
    "version, family",
    [
        (0x380899, PM_VER_ZEN3_GENERIC),
        (0x2D0999, PM_VER_ZEN3_GENERIC),
        (0x240999, PM_VER_ZEN2_GENERIC),
        (0x370002, PM_VER_ZEN2_GENERIC),
        (0x100, PM_VER_ZEN1_GENERIC),
    ],
)
def test_get_smu_table_offsets_family_fallback(version, family):
    assert get_smu_table_offsets(version) is get_smu_table_offsets(family)


def test_get_smu_table_offsets_unsupported():
    assert get_smu_table_offsets(0x999999) is None


def test_co_opcode_only_on_zen3():
    assert get_smu_table_offsets(PM_VER_ZEN3_VERMEER_2).co_margin_op == MP1_ZEN3_GET_DLDO_PSM_MARGIN
    assert get_smu_table_offsets(PM_VER_ZEN2_MATISSE).co_margin_op is None
    assert get_smu_table_offsets(PM_VER_ZEN1_GENERIC).co_margin_op is None


def test_monitor_without_driver(tmp_path):
    with RyzenSmuMonitor(sysfs_dir=str(tmp_path / "missing")) as mon:
        assert not mon.is_available()
        assert mon.get_pm_version() is None
        assert mon.read_snapshot() is None
        assert mon.co_offsets_by_core() == {}


def test_monitor_reads_sysfs(smu_sysfs):
    with RyzenSmuMonitor(sysfs_dir=smu_sysfs(PM_VER_ZEN3_VERMEER_1, ZEN3_V1_BUFFER), core_count=12) as mon:
        assert mon.is_available()
        assert mon.get_pm_version() == PM_VER_ZEN3_VERMEER_1
        assert mon.read_snapshot().cores[0].voltage_v == pytest.approx(1.35)


def test_monitor_unsupported_version_is_unavailable(smu_sysfs):
    with RyzenSmuMonitor(sysfs_dir=smu_sysfs(0x999999, ZEN3_V1_BUFFER)) as mon:
        assert not mon.is_available()
        assert mon.read_snapshot() is None


def test_monitor_prefix_fallback(smu_sysfs):
    with RyzenSmuMonitor(sysfs_dir=smu_sysfs(0x380899, ZEN3_V2_BUFFER), core_count=12) as mon:
        snap = mon.read_snapshot()
        assert snap.pm_version == 0x380899
        assert snap.cores[0].voltage_v == pytest.approx(1.38)


def test_monitor_caches_snapshot_between_readers(smu_sysfs):
    sysfs_dir = smu_sysfs(PM_VER_ZEN3_VERMEER_1, ZEN3_V1_BUFFER)
    with RyzenSmuMonitor(sysfs_dir=sysfs_dir, core_count=12, min_interval=60.0) as mon:
        assert mon.read_snapshot() is mon.read_snapshot()
    with RyzenSmuMonitor(sysfs_dir=sysfs_dir, core_count=12, min_interval=0.0) as mon:
        assert mon.read_snapshot() is not mon.read_snapshot()


def test_fused_off_core_slot_mapping():
    # Slots 0, 1 and 3 are enabled (non-zero FIT limit at 217+), slot 2 is fused off; voltages at 185+
    buf = pm_buffer({217: 1.0, 218: 1.0, 220: 1.0, 185: 1.10, 186: 1.15, 188: 1.32})
    snapshot = parse_pm_table_buffer(PM_VER_ZEN3_VERMEER_1, buf, core_count=3, co_offsets={0: -10, 2: -99, 3: -30})

    assert len(snapshot.cores) == 3
    assert snapshot.cores[0].voltage_v == pytest.approx(1.10)
    assert snapshot.cores[1].voltage_v == pytest.approx(1.15)
    # Linux core 2 maps to physical slot 3, skipping fused-off slot 2
    assert snapshot.cores[2].voltage_v == pytest.approx(1.32)
    # CO offsets are keyed by physical slot: core 2 gets slot 3's value, core 1 has none
    assert [snapshot.cores[i].co_offset for i in range(3)] == [-10, None, -30]

    assert len(snapshot.slots) == 8
    assert not snapshot.slots[2].is_enabled
    assert snapshot.slots[2].core_idx is None
    assert snapshot.slots[3].core_idx == 2


def test_co_offset_mailbox_query(smu_sysfs):
    mon = RyzenSmuMonitor(sysfs_dir=smu_sysfs(PM_VER_ZEN3_VERMEER_1, ZEN3_V1_BUFFER))
    # Mailbox response for -25 is encoded as (1 << 32) - 25
    mon.send_smu_command = MagicMock(return_value=((1 << 32) - 25, 0, 0, 0, 0, 0))
    assert mon.read_co_offset(0) == -25
    mon.send_smu_command.assert_called_once_with(MP1_ZEN3_GET_DLDO_PSM_MARGIN, 0)

    # Slot 8 (CCD 1): ((8 & 8) << 5 | (8 & 7)) << 20 = 0x10000000
    mon.send_smu_command.reset_mock()
    mon.read_co_offset(8)
    mon.send_smu_command.assert_called_once_with(MP1_ZEN3_GET_DLDO_PSM_MARGIN, 0x10000000)


def test_co_offset_not_queried_on_zen2(smu_sysfs):
    mon = RyzenSmuMonitor(sysfs_dir=smu_sysfs(PM_VER_ZEN2_MATISSE, ZEN3_V1_BUFFER))
    mon.send_smu_command = MagicMock()
    assert mon.read_co_offset(0) is None
    mon.send_smu_command.assert_not_called()


def test_co_offsets_by_core_uses_slot_mapping(smu_sysfs):
    mon = RyzenSmuMonitor(sysfs_dir=smu_sysfs(PM_VER_ZEN3_VERMEER_1, ZEN3_V1_BUFFER), core_count=2)
    mon.read_co_offset = MagicMock(side_effect=lambda slot: {0: -20, 1: -15}.get(slot))
    assert mon.co_offsets_by_core() == {0: -20, 1: -15}
    # Offsets are read once for all 16 slots and cached
    mon.read_snapshot()
    assert mon.read_co_offset.call_count == 16
