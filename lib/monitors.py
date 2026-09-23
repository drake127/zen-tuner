"""
Hardware telemetry and kernel error monitors.
Reads CPU performance counter MSRs (APERF/MPERF/TSC) and monitors kernel kmsg ring buffer.
Implements context manager protocols with zero terminal output side effects.
"""

import errno
import os
import re
import select
import struct
import time

from lib.models import MceEvent, StretchSample

# MSR registers for x86 performance counters
MSR_IA32_TSC = 0x10
MSR_IA32_MPERF = 0xE7
MSR_IA32_APERF = 0xE8

# Clock stretching detection threshold in MHz.
# A drop below the target frequency exceeding this value is flagged as stretching.
STRETCH_THRESHOLD_MHZ: float = 100.0

MCE_CPU_PATTERNS = [
    re.compile(r"\[Hardware Error\]:\s+CPU\s+(\d+):", re.IGNORECASE),
    re.compile(r"mce:\s+\[Hardware Error\]:.*CPU\s+(\d+):", re.IGNORECASE),
    re.compile(r"Machine check events logged on CPU\s+(\d+)", re.IGNORECASE),
    re.compile(r"APIC ID:\s*([0-9a-fA-Fx]+)", re.IGNORECASE),
]


def calculate_clock_metrics(
    delta_aperf: int,
    delta_mperf: int,
    delta_tsc: int,
    delta_t: float,
    target_mhz: float,
) -> tuple[float, float, float, float, float] | None:
    """
    Computes (tsc_mhz, busy_pct, effective_mhz, stretch_mhz, stretch_pct) using turbostat formulas.
    Returns None if delta_mperf or delta_tsc or delta_t are zero/invalid.
    """
    if delta_mperf <= 0 or delta_tsc <= 0 or delta_t <= 0:
        return None

    tsc_mhz = delta_tsc / (delta_t * 1e6)
    busy_pct = (delta_mperf / delta_tsc) * 100.0
    bzy_mhz = tsc_mhz * (delta_aperf / delta_mperf)
    effective_mhz = bzy_mhz
    stretch_mhz = target_mhz - effective_mhz
    stretch_pct = (stretch_mhz / target_mhz) * 100.0 if target_mhz > 0 else 0.0

    return tsc_mhz, busy_pct, effective_mhz, stretch_mhz, stretch_pct


class CycleStretchingMonitor:
    """Monitors APERF/MPERF MSRs to detect AMD Ryzen cycle and clock stretching."""

    def __init__(
        self,
        cpus: list[int],
        threshold_mhz: float = STRETCH_THRESHOLD_MHZ,
        sample_interval: float = 1.0,
        smu_monitor: object | None = None,
        core_idx: int | None = None,
    ):
        self.cpus: list[int] = list(cpus)
        self.threshold_mhz: float = threshold_mhz
        self.sample_interval: float = sample_interval
        self.smu_monitor = smu_monitor
        self.core_idx = core_idx
        self.msr_fds: dict[int, int] = {}
        self.last_sample_time: float = 0.0
        self.prev_state: dict[int, dict[str, float]] = {}
        self.all_samples: list[StretchSample] = []
        self.max_stretch_mhz: float = 0.0
        self.stretch_alerts_count: int = 0
        self._stretch_sum: float = 0.0
        self._stretch_count: int = 0
        self._init_msrs()

    @property
    def avg_stretch_mhz(self) -> float:
        """Average clock stretch in MHz across all qualifying samples (busy >= 95%)."""
        return self._stretch_sum / self._stretch_count if self._stretch_count > 0 else 0.0

    def __enter__(self) -> "CycleStretchingMonitor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _init_msrs(self) -> None:
        for cpu in self.cpus:
            path = f"/dev/cpu/{cpu}/msr"
            try:
                fd = os.open(path, os.O_RDONLY)
                self.msr_fds[cpu] = fd
            except (OSError, PermissionError):
                pass
        self.last_sample_time = time.perf_counter()
        self._record_baseline()

    def _read_msr(self, cpu: int, reg: int) -> int | None:
        fd = self.msr_fds.get(cpu)
        if fd is None:
            return None
        try:
            os.lseek(fd, reg, os.SEEK_SET)
            raw = os.read(fd, 8)
            if len(raw) == 8:
                return struct.unpack("<Q", raw)[0]
        except OSError:
            pass
        return None

    def _get_target_freq_mhz(self, cpu: int, sysfs_root: str = "/sys") -> float | None:
        driver_path = f"{sysfs_root}/devices/system/cpu/cpu{cpu}/cpufreq/scaling_driver"
        is_amd_pstate = False
        try:
            if os.path.exists(driver_path):
                with open(driver_path, "r", encoding="utf-8") as f:
                    is_amd_pstate = "amd-pstate" in f.read().strip()
        except OSError:
            pass

        if is_amd_pstate:
            candidates = ("amd_pstate_max_freq", "scaling_max_freq", "cpuinfo_max_freq")
        else:
            candidates = ("scaling_cur_freq", "cpuinfo_cur_freq", "scaling_max_freq")

        for fname in candidates:
            path = f"{sysfs_root}/devices/system/cpu/cpu{cpu}/cpufreq/{fname}"
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        val = float(f.read().strip()) / 1000.0
                        if val > 0:
                            return val
                except (OSError, ValueError):
                    pass
        return None

    def _record_baseline(self) -> None:
        now = time.perf_counter()
        for cpu in self.cpus:
            aperf = self._read_msr(cpu, MSR_IA32_APERF)
            mperf = self._read_msr(cpu, MSR_IA32_MPERF)
            tsc = self._read_msr(cpu, MSR_IA32_TSC)
            if aperf is not None and mperf is not None and tsc is not None:
                self.prev_state[cpu] = {"time": now, "aperf": aperf, "mperf": mperf, "tsc": tsc}

    def poll(self) -> list[StretchSample]:
        """Polls hardware telemetry and returns new samples where clock stretching was detected."""
        now = time.perf_counter()
        dt = now - self.last_sample_time
        if dt < self.sample_interval:
            return []

        self.last_sample_time = now
        new_alerts: list[StretchSample] = []

        if self.smu_monitor and hasattr(self.smu_monitor, "is_available") and self.smu_monitor.is_available():
            if self.core_idx is not None and hasattr(self.smu_monitor, "read_snapshot"):
                try:
                    snap = self.smu_monitor.read_snapshot()
                except Exception:
                    snap = None

                if snap and self.core_idx in snap.cores:
                    sm = snap.cores[self.core_idx]
                    if sm.is_enabled and sm.c0_pct >= 95.0 and sm.frequency_mhz >= 2500.0:
                        target_mhz = sm.frequency_mhz
                        effective_mhz = sm.effective_mhz
                        raw_stretch = target_mhz - effective_mhz
                        stretch_mhz = raw_stretch if raw_stretch > 5.0 else 0.0
                        stretch_pct = (stretch_mhz / target_mhz) * 100.0 if target_mhz > 0 else 0.0

                        sample = StretchSample(
                            cpu=self.cpus[0] if self.cpus else self.core_idx,
                            target_mhz=target_mhz,
                            effective_mhz=effective_mhz,
                            stretch_mhz=stretch_mhz,
                            stretch_pct=stretch_pct,
                        )
                        self.all_samples.append(sample)
                        if stretch_mhz > 0:
                            self._stretch_sum += stretch_mhz
                            self._stretch_count += 1
                        if stretch_mhz > self.max_stretch_mhz:
                            self.max_stretch_mhz = stretch_mhz
                        if stretch_mhz >= self.threshold_mhz:
                            self.stretch_alerts_count += 1
                            new_alerts.append(sample)
                    return new_alerts

        for cpu in self.cpus:
            if cpu not in self.prev_state:
                continue

            aperf = self._read_msr(cpu, MSR_IA32_APERF)
            mperf = self._read_msr(cpu, MSR_IA32_MPERF)
            tsc = self._read_msr(cpu, MSR_IA32_TSC)
            target_mhz = self._get_target_freq_mhz(cpu)

            if aperf is None or mperf is None or tsc is None or target_mhz is None:
                continue

            prev = self.prev_state[cpu]
            delta_t = now - prev["time"]
            if delta_t <= 0:
                continue

            delta_aperf = (aperf - prev["aperf"]) & 0xFFFFFFFFFFFFFFFF
            delta_mperf = (mperf - prev["mperf"]) & 0xFFFFFFFFFFFFFFFF
            delta_tsc = (tsc - prev["tsc"]) & 0xFFFFFFFFFFFFFFFF

            self.prev_state[cpu] = {"time": now, "aperf": aperf, "mperf": mperf, "tsc": tsc}

            metrics = calculate_clock_metrics(delta_aperf, delta_mperf, delta_tsc, delta_t, target_mhz)
            if metrics is None:
                continue

            _, busy_pct, effective_mhz, raw_stretch, raw_pct = metrics

            # Check stretching under active torture load (>= 95% busy, >= 2500 MHz target)
            if busy_pct >= 95.0 and target_mhz >= 2500.0:
                stretch_mhz = raw_stretch if raw_stretch > 5.0 else 0.0
                stretch_pct = (stretch_mhz / target_mhz) * 100.0 if target_mhz > 0 else 0.0
                sample = StretchSample(
                    cpu=cpu,
                    target_mhz=target_mhz,
                    effective_mhz=effective_mhz,
                    stretch_mhz=stretch_mhz,
                    stretch_pct=stretch_pct,
                )
                self.all_samples.append(sample)
                if stretch_mhz > 0:
                    self._stretch_sum += stretch_mhz
                    self._stretch_count += 1
                if stretch_mhz > self.max_stretch_mhz:
                    self.max_stretch_mhz = stretch_mhz

                if stretch_mhz >= self.threshold_mhz:
                    self.stretch_alerts_count += 1
                    new_alerts.append(sample)

        return new_alerts

    def close(self) -> None:
        for fd in self.msr_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self.msr_fds.clear()


class KernelErrorMonitor:
    """Monitors /dev/kmsg for Hardware Errors and Machine Check Exceptions."""

    def __init__(self, tested_cpus: list[int]):
        self.tested_cpus: set[int] = set(tested_cpus)
        self.kmsg_fd: int | None = None
        self.active_events: list[MceEvent] = []
        self.idle_events: list[MceEvent] = []
        self._init_kmsg()

    def __enter__(self) -> "KernelErrorMonitor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _init_kmsg(self) -> None:
        try:
            self.kmsg_fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
            try:
                os.lseek(self.kmsg_fd, 0, os.SEEK_END)
            except OSError:
                pass
        except (PermissionError, OSError):
            self.kmsg_fd = None

    def poll(self) -> tuple[list[MceEvent], list[MceEvent]]:
        """Reads new kernel log lines and returns (new_active_events, new_idle_events)."""
        new_active: list[MceEvent] = []
        new_idle: list[MceEvent] = []

        if self.kmsg_fd is None:
            return new_active, new_idle

        while True:
            r, _, _ = select.select([self.kmsg_fd], [], [], 0)
            if not r:
                break
            try:
                raw = os.read(self.kmsg_fd, 4096).decode("utf-8", errors="replace")
                if not raw:
                    break
                for line in raw.splitlines():
                    ev = self.parse_line(line)
                    if ev:
                        if ev.is_active_core:
                            new_active.append(ev)
                            self.active_events.append(ev)
                        else:
                            new_idle.append(ev)
                            self.idle_events.append(ev)
            except BlockingIOError:
                break
            except OSError as err:
                if err.errno == errno.EPIPE:
                    # kmsg ring buffer rolled over, continue reading
                    continue
                break

        return new_active, new_idle

    def parse_line(self, line: str) -> MceEvent | None:
        lower = line.lower()
        if "hardware error" not in lower and "machine check" not in lower and "mce:" not in lower:
            return None

        cpu_id: int | None = None
        for pat in MCE_CPU_PATTERNS:
            m = pat.search(line)
            if m:
                val = m.group(1)
                try:
                    cpu_id = int(val, 0) if val.startswith(("0x", "0X")) else int(val)
                    break
                except ValueError:
                    pass

        is_active = (cpu_id in self.tested_cpus) if cpu_id is not None else True
        return MceEvent(cpu=cpu_id, message=line.strip(), is_active_core=is_active)

    def close(self) -> None:
        if self.kmsg_fd is not None:
            try:
                os.close(self.kmsg_fd)
            except OSError:
                pass
            self.kmsg_fd = None


