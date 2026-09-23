"""
Hardware telemetry and kernel error monitors.
Samples per-core SMU telemetry under load and watches the kernel kmsg ring buffer for hardware errors.
Implements context manager protocols with zero terminal output side effects.
"""

import errno
import os
import re
import time

from lib.models import STRETCH_THRESHOLD_MHZ, MceEvent, TelemetrySample, TelemetrySummary
from lib.smu import RyzenSmuMonitor

KMSG_PATH = "/dev/kmsg"

# Lines emitted by the x86 MCE core ("mce: [Hardware Error]: CPU 3: Machine Check: ...") and the EDC decoders
# ("[Hardware Error]: CPU:3 (19:21:0) MC1_STATUS[...]"). Thermal throttling notices ("mce: CPU3: Core temperature
# above threshold") are intentionally not matched.
MCE_LINE_MARKERS = ("[hardware error]", "machine check")
MCE_CPU_PATTERN = re.compile(r"\bCPU[:\s]\s*(\d+)\b")

# Telemetry is sampled only when the core is fully loaded at a boost clock; lighter load makes
# target vs. effective clock differences meaningless.
LOAD_C0_PCT_MIN = 95.0
LOAD_FREQ_MHZ_MIN = 2500.0


class CoreTelemetryMonitor:
    """Samples SMU telemetry of one physical core while it is under full load and flags clock stretching."""

    def __init__(
        self,
        smu_monitor: RyzenSmuMonitor,
        core_idx: int,
        threshold_mhz: float = STRETCH_THRESHOLD_MHZ,
        sample_interval: float = 1.0,
    ):
        self.smu_monitor = smu_monitor
        self.core_idx = core_idx
        self.threshold_mhz = threshold_mhz
        self.sample_interval = sample_interval
        self.samples: list[TelemetrySample] = []
        self._last_sample_time = time.monotonic()

    @property
    def summary(self) -> TelemetrySummary | None:
        return TelemetrySummary.from_samples(self.samples)

    def poll(self) -> TelemetrySample | None:
        """Takes a sample when the interval elapsed. Returns it only if its clock drop reaches the threshold."""
        now = time.monotonic()
        if now - self._last_sample_time < self.sample_interval:
            return None
        self._last_sample_time = now

        snapshot = self.smu_monitor.read_snapshot()
        metrics = snapshot.cores.get(self.core_idx) if snapshot else None
        if metrics is None or metrics.c0_pct < LOAD_C0_PCT_MIN or metrics.frequency_mhz < LOAD_FREQ_MHZ_MIN:
            return None

        sample = TelemetrySample(
            target_mhz=metrics.frequency_mhz,
            effective_mhz=metrics.effective_mhz,
            voltage_v=metrics.voltage_v,
            power_w=metrics.power_w,
            temp_c=metrics.temp_c,
        )
        self.samples.append(sample)
        return sample if sample.stretch_mhz >= self.threshold_mhz else None


class KernelErrorMonitor:
    """Monitors /dev/kmsg for Hardware Errors and Machine Check Exceptions."""

    def __init__(self, tested_cpus: list[int], kmsg_path: str | None = None):
        self.tested_cpus: set[int] = set(tested_cpus)
        self.kmsg_fd: int | None = None
        self._open(kmsg_path or KMSG_PATH)

    def __enter__(self) -> "KernelErrorMonitor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _open(self, path: str) -> None:
        try:
            self.kmsg_fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            self.kmsg_fd = None
            return
        try:
            os.lseek(self.kmsg_fd, 0, os.SEEK_END)
        except OSError:
            pass

    def poll(self) -> list[MceEvent]:
        """Reads all pending kernel log records and returns new hardware error events."""
        events: list[MceEvent] = []
        if self.kmsg_fd is None:
            return events

        while True:
            try:
                raw = os.read(self.kmsg_fd, 8192)
            except BlockingIOError:
                break
            except OSError as err:
                if err.errno == errno.EPIPE:
                    # Records were overwritten in the ring buffer before we read them; the next read resumes.
                    continue
                break
            if not raw:
                break
            for line in raw.decode("utf-8", errors="replace").splitlines():
                ev = self.parse_line(line)
                if ev:
                    events.append(ev)
        return events

    def parse_line(self, line: str) -> MceEvent | None:
        # /dev/kmsg records are "<prio>,<seq>,<ts>,<flags>;<message>"; continuation lines start with a space.
        if line.startswith(" "):
            return None
        _, sep, message = line.partition(";")
        message = (message if sep else line).strip()
        lower = message.lower()
        if not any(marker in lower for marker in MCE_LINE_MARKERS):
            return None

        m = MCE_CPU_PATTERN.search(message)
        cpu = int(m.group(1)) if m else None
        return MceEvent(cpu=cpu, message=message, on_tested_cpu=(cpu in self.tested_cpus) if cpu is not None else None)

    def close(self) -> None:
        if self.kmsg_fd is not None:
            try:
                os.close(self.kmsg_fd)
            except OSError:
                pass
            self.kmsg_fd = None
