"""
Unit tests for SMU telemetry sampling and kernel hardware error monitoring.
"""

import os
import threading
from unittest.mock import MagicMock

import pytest

from conftest import make_metrics, make_snapshot
from lib.monitors import CoreTelemetryMonitor, KernelErrorMonitor, kernel_clock, parse_kmsg_record

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


def smu_with(**overrides) -> MagicMock:
    smu = MagicMock()
    smu.read_snapshot.return_value = make_snapshot([make_metrics(**overrides)])
    return smu


def poll_now(mon: CoreTelemetryMonitor):
    mon._last_sample_time = 0.0
    return mon.poll()


def test_telemetry_sample_under_load():
    mon = CoreTelemetryMonitor(smu_with(), core_idx=0)
    sample = poll_now(mon)
    assert (sample.target_mhz, sample.effective_mhz, sample.stretch_mhz) == (4850.0, 4650.0, 200.0)
    assert mon.summary.stretch_mhz == 200.0
    assert mon.summary.voltage_v == 1.35


@pytest.mark.parametrize("overrides", [{"c0_pct": 94.9}, {"frequency_mhz": 2400.0}], ids=["light-load", "low-clock"])
def test_light_load_is_not_sampled(overrides):
    mon = CoreTelemetryMonitor(smu_with(**overrides), core_idx=0)
    assert poll_now(mon) is None
    assert mon.summary is None


def test_sampling_interval():
    mon = CoreTelemetryMonitor(smu_with(), core_idx=0, sample_interval=60.0)
    assert mon.poll() is None
    assert mon.samples == []


def test_unknown_core_is_ignored():
    mon = CoreTelemetryMonitor(smu_with(), core_idx=3)
    assert poll_now(mon) is None
    assert mon.samples == []


def test_parse_mce_records():
    events = [parse_kmsg_record(line) for line in MCE_KMSG_RECORDS]
    assert all(events)
    assert [e.cpu for e in events] == [None, 5, None, None, 12, None, None]
    assert events[0].timestamp_s == pytest.approx(5123.456789)
    assert events[1].message == "mce: [Hardware Error]: CPU 5: Machine Check: 0 Bank 1: bc00080001010135"


@pytest.mark.parametrize("line", BENIGN_KMSG_RECORDS)
def test_ignore_benign_records(line):
    assert parse_kmsg_record(line) is None


def test_record_without_header():
    event = parse_kmsg_record("[Hardware Error]: CPU:3 (19:21:0) MC5_STATUS")
    assert (event.cpu, event.timestamp_s) == (3, None)


def test_kernel_clock_is_monotonic_raw():
    assert kernel_clock() <= kernel_clock()


def test_monitor_reports_records_from_device(tmp_path):
    fifo = tmp_path / "kmsg"
    os.mkfifo(fifo)
    received = []
    done = threading.Event()

    def collect(event):
        received.append(event)
        if len(received) == 2:
            done.set()

    with KernelErrorMonitor(collect, kmsg_path=str(fifo)) as mon:
        assert mon.available
        fifo.write_text("\n".join(BENIGN_KMSG_RECORDS[:1] + MCE_KMSG_RECORDS[:2]) + "\n")
        assert done.wait(5.0)
    assert [e.cpu for e in received] == [None, 5]


def test_unavailable_device():
    with KernelErrorMonitor(lambda event: None, kmsg_path="/nonexistent/kmsg") as mon:
        assert not mon.available
