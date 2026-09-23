"""
Hardware telemetry and kernel error monitors.
Samples per-core SMU telemetry under load and watches the kernel kmsg ring buffer for hardware errors.
"""

from collections.abc import Callable
import errno
import os
import re
import select
import threading
import time

from lib.models import MceEvent, TelemetrySample, TelemetrySummary
from lib.smu import RyzenSmuMonitor

KMSG_PATH = "/dev/kmsg"

# Lines emitted by the x86 MCE core ("mce: [Hardware Error]: CPU 3: Machine Check: ...") and the EDC decoders
# ("[Hardware Error]: CPU:3 (19:21:0) MC1_STATUS[...]"). Thermal throttling notices ("mce: CPU3: Core temperature
# above threshold") are intentionally not matched.
MCE_LINE_MARKERS = ("[hardware error]", "machine check")
MCE_CPU_PATTERN = re.compile(r"\bCPU[:\s]\s*(\d+)\b")

KMSG_POLL_INTERVAL_S = 0.2


def kernel_clock() -> float:
    """Seconds on the clock closest to /dev/kmsg timestamps (sched_clock is not NTP-slewed, like MONOTONIC_RAW)."""
    return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)


class CoreTelemetryMonitor:
    """Samples SMU telemetry of one physical core while it is under full load."""

    def __init__(self, smu_monitor: RyzenSmuMonitor, core_idx: int, sample_interval: float = 1.0):
        self.smu_monitor = smu_monitor
        self.core_idx = core_idx
        self.sample_interval = sample_interval
        self.samples: list[TelemetrySample] = []
        self._last_sample_time = time.monotonic()

    @property
    def summary(self) -> TelemetrySummary | None:
        return TelemetrySummary.from_samples(self.samples)

    def poll(self) -> TelemetrySample | None:
        """Takes a sample when the interval elapsed and the core is fully loaded; returns the new sample."""
        now = time.monotonic()
        if now - self._last_sample_time < self.sample_interval:
            return None
        self._last_sample_time = now

        snapshot = self.smu_monitor.read_snapshot()
        metrics = snapshot.cores.get(self.core_idx) if snapshot else None
        sample = TelemetrySample.from_metrics(metrics) if metrics else None
        if sample is not None:
            self.samples.append(sample)
        return sample


def parse_kmsg_record(record: str) -> MceEvent | None:
    """
    Parses one /dev/kmsg record ("<prio>,<seq>,<usec>,<flags>;<message>") into an MceEvent if it reports a
    hardware error. The timestamp is the kernel log clock, comparable with kernel_clock().
    """
    # Continuation lines carry key=value dictionary entries and start with a space.
    if record.startswith(" "):
        return None
    header, sep, message = record.partition(";")
    if not sep:
        header, message = "", record
    message = message.strip()
    if not any(marker in message.lower() for marker in MCE_LINE_MARKERS):
        return None

    fields = header.split(",")
    timestamp_s = int(fields[2]) / 1e6 if len(fields) > 2 and fields[2].isdigit() else None
    m = MCE_CPU_PATTERN.search(message)
    return MceEvent(cpu=int(m.group(1)) if m else None, message=message, timestamp_s=timestamp_s)


class KernelErrorMonitor:
    """
    Watches /dev/kmsg for hardware errors for the whole session in a background thread.
    Only records logged after start() are reported; callback runs in the monitor thread.
    """

    def __init__(self, callback: Callable[[MceEvent], None], kmsg_path: str | None = None):
        self.callback = callback
        self.kmsg_path = kmsg_path or KMSG_PATH
        self.available = False
        self._fd: int | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def __enter__(self) -> "KernelErrorMonitor":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def start(self) -> None:
        try:
            self._fd = os.open(self.kmsg_path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            os.lseek(self._fd, 0, os.SEEK_END)
        except OSError:
            pass
        self.available = True
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="zen-tuner-kmsg", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while self._running:
            ready, _, _ = select.select([self._fd], [], [], KMSG_POLL_INTERVAL_S)
            if ready:
                self._read_pending()

    def _read_pending(self) -> None:
        while True:
            try:
                raw = os.read(self._fd, 8192)
            except BlockingIOError:
                return
            except OSError as err:
                if err.errno == errno.EPIPE:
                    # Records were overwritten in the ring buffer before we read them; the next read resumes.
                    continue
                return
            if not raw:
                # A FIFO without writers reports EOF; /dev/kmsg never does.
                time.sleep(KMSG_POLL_INTERVAL_S)
                return
            for record in raw.decode("utf-8", errors="replace").splitlines():
                event = parse_kmsg_record(record)
                if event:
                    self.callback(event)

    def close(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
