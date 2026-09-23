"""
AMD Ryzen System Management Unit (SMU) Power Management Table monitor.
Communicates directly with the ryzen_smu kernel driver sysfs interface
to read real-time per-core voltage, power, temperature, and PBO limits.

Data sources & hardware offset references:
- ZenStates-Core (GPL-3.0) by Ivan Rusanov (irusanov)
- ryzen_smu by Leonardo Gates (leogx9r)
"""

from dataclasses import dataclass
import os
import struct
import threading
import time

from lib.models import CoreSmuMetrics, PackageSmuMetrics, SmuSnapshot

SYSFS_SMU_DIR = "/sys/kernel/ryzen_smu_drv"

# PM table physically describes up to 16 core slots (2 CCDs x 8 cores), including fused-off ones.
PM_TABLE_CORE_SLOTS = 16
PM_TABLE_READ_SIZE = 4096

# Supported PM Table Versions
# Zen 3 (Ryzen 5000 / Vermeer, Milan, Chagall)
PM_VER_ZEN3_VERMEER_1 = 0x380804
PM_VER_ZEN3_VERMEER_2 = 0x380805
PM_VER_ZEN3_GENERIC = 0x380000

# Zen 2 (Ryzen 3000 / Matisse, Castle Peak, Rome)
PM_VER_ZEN2_MATISSE = 0x240803
PM_VER_ZEN2_GENERIC = 0x240000

# Zen 1 / Zen+ (Ryzen 1000/2000 / Summit Ridge, Pinnacle Ridge)
PM_VER_ZEN1_GENERIC = 0x000100
PM_VER_ZEN1_PLUS = 0x000101

# MP1 mailbox opcode returning the Curve Optimizer (DLDO PSM margin) of a core on Zen 3
MP1_ZEN3_GET_DLDO_PSM_MARGIN = 0x48


@dataclass(frozen=True)
class SmuTableOffsets:
    """Offsets (in 4-byte float indices) for AMD SMU PM Table metrics and generation-specific SMU capabilities."""
    # Per-core offsets (index of slot 0; slots follow consecutively)
    core_power: int
    core_voltage: int
    core_temp: int
    core_freq: int
    core_freq_eff: int
    core_c0: int
    core_cc1: int
    core_cc6: int
    core_fit: int | None = None
    core_iddmax: int | None = None

    # Package / PBO limits and global telemetry offsets
    socket_power: int | None = None
    ppt_limit: int | None = None
    ppt_value: int | None = None
    tdc_limit: int | None = None
    tdc_value: int | None = None
    thm_limit: int | None = None
    thm_value: int | None = None
    edc_limit: int | None = None
    edc_value: int | None = None

    # Fabric & Uncore voltages
    soc_voltage: int | None = None
    vddp_voltage: int | None = None
    vddg_ccd_voltage: int | None = None
    vddg_iod_voltage: int | None = None

    # MP1 mailbox opcode for reading Curve Optimizer offsets; None if unknown for this generation
    co_margin_op: int | None = None


# Mappings derived from community hardware research (ZenStates-Core by irusanov, ryzen_smu by leogx9r)
# Each architecture/version explicitly defines its package limits and core metric indices.
OFFSETS_MAP: dict[int, SmuTableOffsets] = {
    # Zen 3 (Vermeer AGESA v1 - 0x380804)
    PM_VER_ZEN3_VERMEER_1: SmuTableOffsets(
        core_power=169,
        core_voltage=185,
        core_temp=201,
        core_fit=217,
        core_iddmax=233,
        core_freq=249,
        core_freq_eff=265,
        core_c0=281,
        core_cc1=297,
        core_cc6=313,
        socket_power=29,
        ppt_limit=0,
        ppt_value=1,
        tdc_limit=2,
        tdc_value=3,
        thm_limit=4,
        thm_value=5,
        edc_limit=8,
        edc_value=15,
        soc_voltage=45,
        vddp_voltage=137,
        vddg_ccd_voltage=139,
        vddg_iod_voltage=138,
        co_margin_op=MP1_ZEN3_GET_DLDO_PSM_MARGIN,
    ),
    # Zen 3 (Vermeer AGESA v2 - 0x380805, latest)
    PM_VER_ZEN3_VERMEER_2: SmuTableOffsets(
        core_power=172,
        core_voltage=188,
        core_temp=204,
        core_fit=220,
        core_iddmax=236,
        core_freq=252,
        core_freq_eff=268,
        core_c0=284,
        core_cc1=300,
        core_cc6=316,
        socket_power=29,
        ppt_limit=0,
        ppt_value=1,
        tdc_limit=2,
        tdc_value=3,
        thm_limit=4,
        thm_value=5,
        edc_limit=8,
        edc_value=15,
        soc_voltage=45,
        vddp_voltage=137,
        vddg_ccd_voltage=139,
        vddg_iod_voltage=138,
        co_margin_op=MP1_ZEN3_GET_DLDO_PSM_MARGIN,
    ),
    # Zen 3 Generic fallback (Ryzen 5000 / Milan / Chagall family)
    PM_VER_ZEN3_GENERIC: SmuTableOffsets(
        core_power=172,
        core_voltage=188,
        core_temp=204,
        core_fit=220,
        core_iddmax=236,
        core_freq=252,
        core_freq_eff=268,
        core_c0=284,
        core_cc1=300,
        core_cc6=316,
        socket_power=29,
        ppt_limit=0,
        ppt_value=1,
        tdc_limit=2,
        tdc_value=3,
        thm_limit=4,
        thm_value=5,
        edc_limit=8,
        edc_value=15,
        soc_voltage=45,
        vddp_voltage=137,
        vddg_ccd_voltage=139,
        vddg_iod_voltage=138,
        co_margin_op=MP1_ZEN3_GET_DLDO_PSM_MARGIN,
    ),
    # Zen 2 (Matisse / Castle Peak 0x240803)
    PM_VER_ZEN2_MATISSE: SmuTableOffsets(
        core_power=147,
        core_voltage=163,
        core_temp=179,
        core_fit=195,
        core_iddmax=211,
        core_freq=227,
        core_freq_eff=243,
        core_c0=259,
        core_cc1=275,
        core_cc6=291,
        socket_power=29,
        ppt_limit=0,
        ppt_value=1,
        tdc_limit=2,
        tdc_value=3,
        thm_limit=4,
        thm_value=5,
        edc_limit=8,
        edc_value=15,
        soc_voltage=45,
        vddp_voltage=125,
        vddg_ccd_voltage=None,
        vddg_iod_voltage=126,
    ),
    # Zen 2 Generic fallback (Ryzen 3000 / Rome family)
    PM_VER_ZEN2_GENERIC: SmuTableOffsets(
        core_power=147,
        core_voltage=163,
        core_temp=179,
        core_fit=195,
        core_iddmax=211,
        core_freq=227,
        core_freq_eff=243,
        core_c0=259,
        core_cc1=275,
        core_cc6=291,
        socket_power=29,
        ppt_limit=0,
        ppt_value=1,
        tdc_limit=2,
        tdc_value=3,
        thm_limit=4,
        thm_value=5,
        edc_limit=8,
        edc_value=15,
        soc_voltage=45,
        vddp_voltage=125,
        vddg_ccd_voltage=None,
        vddg_iod_voltage=126,
    ),
    # Zen 1 / Zen+ Generic fallback (Summit Ridge / Pinnacle Ridge)
    PM_VER_ZEN1_GENERIC: SmuTableOffsets(
        core_power=115,
        core_voltage=131,
        core_temp=147,
        core_freq=163,
        core_freq_eff=179,
        core_c0=195,
        core_cc1=211,
        core_cc6=227,
        socket_power=29,
        ppt_limit=0,
        ppt_value=1,
        tdc_limit=2,
        tdc_value=3,
        thm_limit=4,
        thm_value=5,
        edc_limit=None,
        edc_value=None,
        soc_voltage=26,
        vddp_voltage=17,
        vddg_ccd_voltage=None,
        vddg_iod_voltage=None,
    ),
    PM_VER_ZEN1_PLUS: SmuTableOffsets(
        core_power=115,
        core_voltage=131,
        core_temp=147,
        core_freq=163,
        core_freq_eff=179,
        core_c0=195,
        core_cc1=211,
        core_cc6=227,
        socket_power=29,
        ppt_limit=0,
        ppt_value=1,
        tdc_limit=2,
        tdc_value=3,
        thm_limit=4,
        thm_value=5,
        edc_limit=None,
        edc_value=None,
        soc_voltage=24,
        vddp_voltage=15,
        vddg_ccd_voltage=None,
        vddg_iod_voltage=None,
    ),
}


def get_smu_table_offsets(version: int) -> SmuTableOffsets | None:
    """
    Resolves SmuTableOffsets for a given PM table version.
    Performs exact lookup first, then matches architecture family prefixes (Zen 1, Zen 2, Zen 3).
    """
    if version in OFFSETS_MAP:
        return OFFSETS_MAP[version]

    prefix_24 = version & 0xFF0000
    if prefix_24 in (0x380000, 0x2D0000):
        return OFFSETS_MAP[PM_VER_ZEN3_GENERIC]
    if prefix_24 in (0x240000, 0x370000):
        return OFFSETS_MAP[PM_VER_ZEN2_GENERIC]
    if version in (PM_VER_ZEN1_GENERIC, PM_VER_ZEN1_PLUS):
        return OFFSETS_MAP[PM_VER_ZEN1_GENERIC]

    return None


def read_float(buffer: bytes, index: int | None) -> float | None:
    """Unpacks a 32-bit little-endian float at the given float index, or None if the index is None or out of range."""
    if index is None:
        return None
    offset = index * 4
    if offset + 4 > len(buffer):
        return None
    return struct.unpack_from("<f", buffer, offset)[0]


def _voltage_or_none(value: float | None) -> float | None:
    return value if value is not None and 0.1 <= value <= 2.5 else None


def parse_pm_table_buffer(
    version: int,
    raw_bytes: bytes,
    core_count: int | None = None,
    co_offsets: dict[int, int] | None = None,
) -> SmuSnapshot | None:
    """
    Parses raw PM table bytes into a structured SmuSnapshot.
    Enabled physical core slots are detected by a non-zero FIT or IDDMAX limit; fused-off slots are skipped when
    numbering cores, matching the Linux core order. co_offsets are keyed by physical slot index.
    """
    offsets = get_smu_table_offsets(version)
    if not offsets:
        return None

    if len(raw_bytes) < (offsets.core_cc6 + PM_TABLE_CORE_SLOTS) * 4:
        return None

    def f(index: int | None) -> float:
        return read_float(raw_bytes, index) or 0.0

    enabled_slots: list[int] = []
    if offsets.core_fit is not None and offsets.core_iddmax is not None:
        enabled_slots = [
            s for s in range(PM_TABLE_CORE_SLOTS) if f(offsets.core_fit + s) > 0.0 or f(offsets.core_iddmax + s) > 0.0
        ]

    # Tables without FIT/IDDMAX limits (Zen 1) or synthetic buffers: assume slots are populated in order
    if not enabled_slots:
        enabled_slots = list(range(core_count or PM_TABLE_CORE_SLOTS))

    if core_count is not None:
        enabled_slots = enabled_slots[:core_count]

    package = PackageSmuMetrics(
        socket_power_w=f(offsets.socket_power),
        package_temp_c=f(offsets.thm_value),
        ppt_w=f(offsets.ppt_value),
        ppt_limit_w=f(offsets.ppt_limit),
        tdc_a=f(offsets.tdc_value),
        tdc_limit_a=f(offsets.tdc_limit),
        edc_a=f(offsets.edc_value),
        edc_limit_a=f(offsets.edc_limit),
        soc_voltage_v=_voltage_or_none(read_float(raw_bytes, offsets.soc_voltage)),
        vddp_voltage_v=_voltage_or_none(read_float(raw_bytes, offsets.vddp_voltage)),
        vddg_ccd_voltage_v=_voltage_or_none(read_float(raw_bytes, offsets.vddg_ccd_voltage)),
        vddg_iod_voltage_v=_voltage_or_none(read_float(raw_bytes, offsets.vddg_iod_voltage)),
    )

    has_second_ccd = any(s >= 8 for s in enabled_slots) or (core_count is not None and core_count > 8)
    total_slots = PM_TABLE_CORE_SLOTS if has_second_ccd else PM_TABLE_CORE_SLOTS // 2

    cores: dict[int, CoreSmuMetrics] = {}
    slots: list[CoreSmuMetrics] = []
    core_idx_by_slot = {slot: idx for idx, slot in enumerate(enabled_slots)}

    for slot_idx in range(total_slots):
        ccd_idx = slot_idx // 8
        core_idx = core_idx_by_slot.get(slot_idx)
        if core_idx is None:
            slots.append(
                CoreSmuMetrics(
                    core_idx=None,
                    slot_idx=slot_idx,
                    ccd_idx=ccd_idx,
                    is_enabled=False,
                    voltage_v=0.0,
                    power_w=0.0,
                    temp_c=0.0,
                    frequency_mhz=0.0,
                    effective_mhz=0.0,
                    c0_pct=0.0,
                )
            )
            continue

        m = CoreSmuMetrics(
            core_idx=core_idx,
            slot_idx=slot_idx,
            ccd_idx=ccd_idx,
            is_enabled=True,
            voltage_v=f(offsets.core_voltage + slot_idx),
            power_w=f(offsets.core_power + slot_idx),
            temp_c=f(offsets.core_temp + slot_idx),
            frequency_mhz=f(offsets.core_freq + slot_idx) * 1000.0,
            effective_mhz=f(offsets.core_freq_eff + slot_idx) * 1000.0,
            c0_pct=f(offsets.core_c0 + slot_idx),
            c1_pct=f(offsets.core_cc1 + slot_idx),
            c6_pct=f(offsets.core_cc6 + slot_idx),
            co_offset=co_offsets.get(slot_idx) if co_offsets else None,
        )
        cores[core_idx] = m
        slots.append(m)

    return SmuSnapshot(cores=cores, package=package, pm_version=version, slots=slots)


class RyzenSmuMonitor:
    """
    Thread-safe reader of the ryzen_smu kernel driver interface.
    The PM table layout and Curve Optimizer offsets are resolved once; snapshots are cached for min_interval
    seconds so that concurrent readers (dashboard, telemetry sampling) share a single PM table transfer.
    """

    def __init__(self, sysfs_dir: str = SYSFS_SMU_DIR, core_count: int | None = None, min_interval: float = 0.25):
        self.core_count = core_count
        self.min_interval = min_interval
        self.pm_path = os.path.join(sysfs_dir, "pm_table")
        self.version_path = os.path.join(sysfs_dir, "pm_table_version")
        self.mp1_path = os.path.join(sysfs_dir, "mp1_smu_cmd")
        self.smu_args_path = os.path.join(sysfs_dir, "smu_args")
        self._lock = threading.Lock()
        self._fd: int | None = None
        self._version: int | None = None
        self._version_resolved = False
        self._co_offsets: dict[int, int] | None = None
        self._cached_snapshot: SmuSnapshot | None = None
        self._cached_at = 0.0

    def is_available(self) -> bool:
        """Returns True if the ryzen_smu driver is loaded and exposes a supported PM table."""
        return os.path.isfile(self.pm_path) and self.layout is not None

    def get_pm_version(self) -> int | None:
        """Reads the 32-bit PM table version from sysfs once."""
        if not self._version_resolved:
            self._version_resolved = True
            try:
                with open(self.version_path, "rb") as f:
                    raw = f.read(4)
                if len(raw) == 4:
                    self._version = struct.unpack("<I", raw)[0]
            except OSError:
                pass
        return self._version

    @property
    def layout(self) -> SmuTableOffsets | None:
        version = self.get_pm_version()
        return get_smu_table_offsets(version) if version is not None else None

    def _wait_mailbox(self, timeout_s: float) -> int:
        deadline = time.monotonic() + timeout_s
        with open(self.mp1_path, "rb") as f_cmd:
            while True:
                raw = f_cmd.read(4)
                status = struct.unpack("<I", raw)[0] if len(raw) == 4 else 0
                if status != 0 or time.monotonic() >= deadline:
                    return status
                time.sleep(0.005)
                f_cmd.seek(0)

    def _send_smu_command(self, op: int, *args: int, timeout_s: float = 0.2) -> tuple[int, ...] | None:
        """Sends an MP1 mailbox command (up to 6 arguments) and returns the response arguments. Caller holds _lock."""
        if not (os.path.isfile(self.mp1_path) and os.path.isfile(self.smu_args_path)):
            return None
        payload = list(args) + [0] * (6 - len(args))
        try:
            self._wait_mailbox(timeout_s)
            with open(self.smu_args_path, "wb") as f_args:
                f_args.write(struct.pack("<6I", *payload))
            with open(self.mp1_path, "wb") as f_cmd:
                f_cmd.write(struct.pack("<I", op))
            if self._wait_mailbox(timeout_s) != 1:
                return None
            with open(self.smu_args_path, "rb") as f_args:
                resp_raw = f_args.read(24)
            if len(resp_raw) == 24:
                return struct.unpack("<6I", resp_raw)
        except OSError:
            pass
        return None

    def _read_co_offset(self, slot_idx: int) -> int | None:
        """Reads the Curve Optimizer offset of a physical core slot, or None if unsupported. Caller holds _lock."""
        layout = self.layout
        if layout is None or layout.co_margin_op is None:
            return None
        core_mask = ((slot_idx & 8) << 5 | (slot_idx & 7)) << 20
        resp = self._send_smu_command(layout.co_margin_op, core_mask)
        if not resp:
            return None
        val = resp[0] - 0x100000000 if resp[0] > 0x7FFFFFFF else resp[0]
        return val if -100 <= val <= 100 else None

    def _read_co_offsets_locked(self) -> dict[int, int]:
        if self._co_offsets is None:
            self._co_offsets = {}
            for slot in range(PM_TABLE_CORE_SLOTS):
                val = self._read_co_offset(slot)
                if val is not None:
                    self._co_offsets[slot] = val
        return self._co_offsets

    def read_snapshot(self) -> SmuSnapshot | None:
        """Returns the current parsed PM table (cached for min_interval seconds), or None if unavailable."""
        if self.layout is None:
            return None

        with self._lock:
            now = time.monotonic()
            if self._cached_snapshot is not None and now - self._cached_at < self.min_interval:
                return self._cached_snapshot
            try:
                if self._fd is None:
                    self._fd = os.open(self.pm_path, os.O_RDONLY)
                raw = os.pread(self._fd, PM_TABLE_READ_SIZE, 0)
            except OSError:
                return None
            if not raw:
                return None
            snapshot = parse_pm_table_buffer(self.get_pm_version(), raw, core_count=self.core_count,
                                             co_offsets=self._read_co_offsets_locked())
            self._cached_snapshot = snapshot
            self._cached_at = now
            return snapshot

    def co_offsets_by_core(self) -> dict[int, int]:
        """Curve Optimizer offsets keyed by Linux core index, using the PM table's enabled slot mapping."""
        snapshot = self.read_snapshot()
        if snapshot is None:
            return {}
        return {idx: m.co_offset for idx, m in snapshot.cores.items() if m.co_offset is not None}

    def close(self) -> None:
        """Closes open sysfs file descriptors."""
        with self._lock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None

    def __enter__(self) -> "RyzenSmuMonitor":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
