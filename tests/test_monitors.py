"""
Unit tests for SMU telemetry sampling and kernel hardware error parsing.
"""

import os
from unittest.mock import MagicMock

import pytest

from lib.models import CoreSmuMetrics, PackageSmuMetrics, SmuSnapshot
from lib.monitors import CoreTelemetryMonitor, KernelErrorMonitor

# Kernel log records as read from /dev/kmsg. Message formats follow arch/x86/kernel/cpu/mce/core.c (print_mce,
# mce_notify_irq) and drivers/edac/mce_amd.c (amd_decode_mce); they are not captured on real hardware.
MCE_KMSG_RECORDS = [
    "3,1201,5123456789,-;mce: [Hardware Error]: Machine check events logged",
    "0,1202,5123456790,-;mce: [Hardware Error]: CPU 5: Machine Check: 0 Bank 1: bc00080001010135",
    "0,1203,5123456791,-;mce: [Hardware Error]: PROCESSOR 2:a20f10 TIME 1695470000 SOCKET 0 APIC a microcode a201016",
    "3,1204,5123456792,-;[Hardware Error]: Corrected error, no action required.",
    "3,1205,5123456793,-;[Hardware Error]: CPU:12 (19:21:0) MC5_STATUS[Over|CE|MiscV|-|-|-|SyndV|-|-|-]: 0xdc2040000000011b",
    "3,1206,5123456794,-;[Hardware Error]: IPID: 0x000500b000000000, Syndrome: 0x000000005a020001",
    "3,1207,5123456795,-;[Hardware Error]: Execution Unit Ext. Error Code: 0",
]
BENIGN_KMSG_RECORDS = [
    "6,1301,5123456800,-;eth0: Link is Up - 1000Mbps/Full",
    "4,1302,5123456801,-;mce: CPU5: Core temperature above threshold, cpu clock throttled (total events = 1)",
    "6,1303,5123456802,-;mce: CPU0: Thermal monitoring enabled (TM1)",
    " SUBSYSTEM=cpu",
]


def smu_with_core(**overrides) -> MagicMock:
    values = dict(
        core_idx=0, slot_idx=0, ccd_idx=0, is_enabled=True, voltage_v=1.35, power_w=15.0, temp_c=65.0,
        frequency_mhz=4850.0, effective_mhz=4650.0, c0_pct=99.0, c1_pct=1.0, c6_pct=0.0,
    )
    metrics = CoreSmuMetrics(**(values | overrides))
    smu = MagicMock()
    smu.read_snapshot.return_value = SmuSnapshot(
        cores={0: metrics}, package=PackageSmuMetrics(0, 0, 0, 0, 0, 0, 0, 0), pm_version=0x380805, slots=[metrics]
    )
    return smu


def poll_now(mon: CoreTelemetryMonitor):
    mon._last_sample_time = 0.0
    return mon.poll()


def test_alert_on_stretch_above_threshold():
    mon = CoreTelemetryMonitor(smu_with_core(), core_idx=0)
    alert = poll_now(mon)
    assert (alert.target_mhz, alert.effective_mhz, alert.stretch_mhz) == (4850.0, 4650.0, 200.0)
    assert mon.summary.stretch_mhz == 200.0
    assert mon.summary.voltage_v == 1.35


def test_small_drop_is_sampled_without_alert():
    mon = CoreTelemetryMonitor(smu_with_core(effective_mhz=4831.0), core_idx=0)
    assert poll_now(mon) is None
    assert [s.stretch_mhz for s in mon.samples] == [19.0]


def test_default_threshold_is_50():
    assert CoreTelemetryMonitor(smu_with_core(), core_idx=0).threshold_mhz == 50.0


@pytest.mark.parametrize("overrides", [{"c0_pct": 94.9}, {"frequency_mhz": 2400.0}])
def test_light_load_is_not_sampled(overrides):
    mon = CoreTelemetryMonitor(smu_with_core(**overrides), core_idx=0)
    assert poll_now(mon) is None
    assert mon.samples == []
    assert mon.summary is None


def test_sampling_interval():
    mon = CoreTelemetryMonitor(smu_with_core(), core_idx=0, sample_interval=60.0)
    assert mon.poll() is None
    assert mon.samples == []


def test_unknown_core_is_ignored():
    mon = CoreTelemetryMonitor(smu_with_core(), core_idx=3)
    assert poll_now(mon) is None
    assert mon.samples == []


@pytest.fixture
def kernel_mon():
    with KernelErrorMonitor(tested_cpus=[0, 12], kmsg_path="/nonexistent/kmsg") as mon:
        yield mon


def test_parse_mce_records(kernel_mon):
    events = [kernel_mon.parse_line(line) for line in MCE_KMSG_RECORDS]
    assert all(events)
    assert [e.cpu for e in events] == [None, 5, None, None, 12, None, None]
    assert [e.on_tested_cpu for e in events] == [None, False, None, None, True, None, None]
    assert events[1].message == "mce: [Hardware Error]: CPU 5: Machine Check: 0 Bank 1: bc00080001010135"


@pytest.mark.parametrize("line", BENIGN_KMSG_RECORDS)
def test_ignore_benign_records(kernel_mon, line):
    assert kernel_mon.parse_line(line) is None


def test_unavailable_device(kernel_mon):
    assert kernel_mon.kmsg_fd is None
    assert kernel_mon.poll() == []


def test_poll_reads_records_from_device(tmp_path):
    fifo = tmp_path / "kmsg"
    os.mkfifo(fifo)
    with KernelErrorMonitor(tested_cpus=[5], kmsg_path=str(fifo)) as mon:
        assert mon.poll() == []
        fifo.write_text("\n".join(BENIGN_KMSG_RECORDS[:1] + MCE_KMSG_RECORDS[:2]) + "\n")
        events = mon.poll()
    assert len(events) == 2
    assert events[1].on_tested_cpu
