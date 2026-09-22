"""
AMD Ryzen System Management Unit (SMU) Power Management Table monitor.
Communicates directly with the ryzen_smu kernel driver sysfs interface
to read real-time per-core voltage, power, temperature, and PBO limits.

Data sources & hardware offset references:
- ZenStates-Core (GPL-3.0) by Ivan Rusanov (irusanov)
- ryzen_smu by Leonardo Gates (leogx9r)
"""

import os
import struct
import time
from typing import Dict, List, Optional, Tuple

from lib.models import CoreSmuMetrics, PackageSmuMetrics, SmuSnapshot

SYSFS_SMU_DIR = "/sys/kernel/ryzen_smu_drv"
PM_TABLE_PATH = os.path.join(SYSFS_SMU_DIR, "pm_table")
PM_VERSION_PATH = os.path.join(SYSFS_SMU_DIR, "pm_table_version")
DRV_VERSION_PATH = os.path.join(SYSFS_SMU_DIR, "version")
MP1_CMD_PATH = os.path.join(SYSFS_SMU_DIR, "mp1_smu_cmd")
SMU_ARGS_PATH = os.path.join(SYSFS_SMU_DIR, "smu_args")

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


class SmuTableOffsets:
    """Offsets (in 4-byte float indices) for AMD SMU PM Table metrics."""

    def __init__(
        self,
        core_power: int,
        core_voltage: int,
        core_temp: int,
        core_freq: int,
        core_freq_eff: int,
        core_c0: int,
        core_cc1: int,
        core_cc6: int,
        core_fit: Optional[int] = None,
        core_iddmax: Optional[int] = None,
        soc_voltage: Optional[int] = 45,
        vddp_voltage: Optional[int] = 137,
        vddg_ccd_voltage: Optional[int] = 139,
        vddg_iod_voltage: Optional[int] = 138,
        socket_power: int = 29,
        ppt_limit: int = 0,
        ppt_value: int = 1,
        tdc_limit: int = 2,
        tdc_value: int = 3,
        thm_limit: int = 4,
        thm_value: int = 5,
        edc_limit: int = 8,
        edc_value: int = 9,
    ):
        self.ppt_limit = ppt_limit
        self.ppt_value = ppt_value
        self.tdc_limit = tdc_limit
        self.tdc_value = tdc_value
        self.thm_limit = thm_limit
        self.thm_value = thm_value
        self.edc_limit = edc_limit
        self.edc_value = edc_value
        self.socket_power = socket_power
        self.soc_voltage = soc_voltage
        self.vddp_voltage = vddp_voltage
        self.vddg_ccd_voltage = vddg_ccd_voltage
        self.vddg_iod_voltage = vddg_iod_voltage
        self.core_power = core_power
        self.core_voltage = core_voltage
        self.core_temp = core_temp
        self.core_fit = core_fit
        self.core_iddmax = core_iddmax
        self.core_freq = core_freq
        self.core_freq_eff = core_freq_eff
        self.core_c0 = core_c0
        self.core_cc1 = core_cc1
        self.core_cc6 = core_cc6


# Mappings derived from community hardware research (ZenStates-Core by irusanov, ryzen_smu by leogx9r)
OFFSETS_MAP: Dict[int, SmuTableOffsets] = {
    # Zen 3 (Vermeer AGESA v1)
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
        soc_voltage=45,
        vddp_voltage=137,
        vddg_ccd_voltage=139,
        vddg_iod_voltage=138,
    ),
    # Zen 3 (Vermeer AGESA v2 - latest)
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
        soc_voltage=45,
        vddp_voltage=137,
        vddg_ccd_voltage=139,
        vddg_iod_voltage=138,
    ),
    # Zen 3 Generic fallback
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
        soc_voltage=45,
        vddp_voltage=137,
        vddg_ccd_voltage=139,
        vddg_iod_voltage=138,
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
        soc_voltage=45,
        vddp_voltage=125,
        vddg_ccd_voltage=None,
        vddg_iod_voltage=126,
    ),
    # Zen 2 Generic fallback
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
        soc_voltage=45,
        vddp_voltage=125,
        vddg_ccd_voltage=None,
        vddg_iod_voltage=126,
    ),
    # Zen 1 / Zen+ Generic fallback
    PM_VER_ZEN1_GENERIC: SmuTableOffsets(
        core_power=115,
        core_voltage=131,
        core_temp=147,
        core_freq=163,
        core_freq_eff=179,
        core_c0=195,
        core_cc1=211,
        core_cc6=227,
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
        soc_voltage=24,
        vddp_voltage=15,
        vddg_ccd_voltage=None,
        vddg_iod_voltage=None,
    ),
}


def get_smu_table_offsets(version: int) -> Optional[SmuTableOffsets]:
    """
    Resolves SmuTableOffsets for a given PM table version.
    Performs exact lookup first, then matches architecture family prefixes (Zen 1, Zen 2, Zen 3).
    """
    if version in OFFSETS_MAP:
        return OFFSETS_MAP[version]

    # Family prefix matching
    prefix_24 = version & 0xFF0000
    if prefix_24 in (0x380000, 0x2D0000):
        return OFFSETS_MAP[PM_VER_ZEN3_GENERIC]
    if prefix_24 in (0x240000, 0x370000):
        return OFFSETS_MAP[PM_VER_ZEN2_GENERIC]
    if prefix_24 == 0x000100 or version in (0x100, 0x101):
        return OFFSETS_MAP[PM_VER_ZEN1_GENERIC]

    return None


def read_float(buffer: bytes, index: int) -> float:
    """Unpacks a 32-bit little-endian float from the buffer at the given float index."""
    offset = index * 4
    if offset + 4 > len(buffer):
        return 0.0
    return struct.unpack_from("<f", buffer, offset)[0]


def parse_pm_table_buffer(
    version: int,
    raw_bytes: bytes,
    target_core_count: Optional[int] = None,
    max_cores: Optional[int] = None,
    co_offsets: Optional[Dict[int, int]] = None,
) -> Optional[SmuSnapshot]:
    """
    Parses raw PM table bytes into a structured SmuSnapshot.
    Auto-detects active physical core slots, filtering out disabled/fused-off silicon cores.
    """
    effective_core_count = target_core_count if target_core_count is not None else max_cores
    offsets = get_smu_table_offsets(version)
    if not offsets:
        return None

    # Buffer must hold all 16 core slots
    min_required_len = (offsets.core_cc6 + 16) * 4
    if len(raw_bytes) < min_required_len:
        return None

    # Detect enabled physical core slots (cores with active FIT or IDDMAX limit)
    enabled_slots: List[int] = []
    if offsets.core_fit is not None and offsets.core_iddmax is not None:
        for slot_idx in range(16):
            fit = read_float(raw_bytes, offsets.core_fit + slot_idx)
            iddmax = read_float(raw_bytes, offsets.core_iddmax + slot_idx)
            if fit > 0.0 or iddmax > 0.0:
                enabled_slots.append(slot_idx)

    # Fallback to direct range if no slots passed check (e.g. synthetic test buffer)
    if not enabled_slots:
        enabled_slots = list(range(effective_core_count or 16))

    if effective_core_count is not None and len(enabled_slots) > effective_core_count:
        enabled_slots = enabled_slots[:effective_core_count]

    soc_v = read_float(raw_bytes, offsets.soc_voltage) if offsets.soc_voltage else 0.0
    vddp_v = read_float(raw_bytes, offsets.vddp_voltage) if offsets.vddp_voltage else 0.0
    vddg_ccd_v = read_float(raw_bytes, offsets.vddg_ccd_voltage) if offsets.vddg_ccd_voltage else 0.0
    vddg_iod_v = read_float(raw_bytes, offsets.vddg_iod_voltage) if offsets.vddg_iod_voltage else 0.0

    package = PackageSmuMetrics(
        socket_power_w=read_float(raw_bytes, offsets.socket_power),
        package_temp_c=read_float(raw_bytes, offsets.thm_value),
        ppt_w=read_float(raw_bytes, offsets.ppt_value),
        ppt_limit_w=read_float(raw_bytes, offsets.ppt_limit),
        tdc_a=read_float(raw_bytes, offsets.tdc_value),
        tdc_limit_a=read_float(raw_bytes, offsets.tdc_limit),
        edc_a=read_float(raw_bytes, offsets.edc_value),
        edc_limit_a=read_float(raw_bytes, offsets.edc_limit),
        soc_voltage_v=soc_v if 0.1 <= soc_v <= 2.5 else None,
        vddp_voltage_v=vddp_v if 0.1 <= vddp_v <= 2.5 else None,
        vddg_ccd_voltage_v=vddg_ccd_v if 0.1 <= vddg_ccd_v <= 2.5 else None,
        vddg_iod_voltage_v=vddg_iod_v if 0.1 <= vddg_iod_v <= 2.5 else None,
    )

    has_second_ccd = any(s >= 8 for s in enabled_slots) or (
        effective_core_count is not None and effective_core_count > 8
    )
    total_slots = 16 if has_second_ccd else 8

    cores: Dict[int, CoreSmuMetrics] = {}
    slots: List[CoreSmuMetrics] = []

    for slot_idx in range(total_slots):
        ccd_idx = 0 if slot_idx < 8 else 1
        if slot_idx in enabled_slots:
            linux_core_idx = enabled_slots.index(slot_idx)
            co = (
                co_offsets.get(slot_idx) if slot_idx in co_offsets else co_offsets.get(linux_core_idx)
            ) if co_offsets else None
            m = CoreSmuMetrics(
                core_idx=linux_core_idx,
                slot_idx=slot_idx,
                ccd_idx=ccd_idx,
                is_enabled=True,
                voltage_v=read_float(raw_bytes, offsets.core_voltage + slot_idx),
                power_w=read_float(raw_bytes, offsets.core_power + slot_idx),
                temp_c=read_float(raw_bytes, offsets.core_temp + slot_idx),
                frequency_mhz=read_float(raw_bytes, offsets.core_freq + slot_idx) * 1000.0,
                effective_mhz=read_float(raw_bytes, offsets.core_freq_eff + slot_idx) * 1000.0,
                c0_pct=read_float(raw_bytes, offsets.core_c0 + slot_idx),
                c1_pct=read_float(raw_bytes, offsets.core_cc1 + slot_idx),
                c6_pct=read_float(raw_bytes, offsets.core_cc6 + slot_idx),
                co_offset=co,
            )
            cores[linux_core_idx] = m
            slots.append(m)
        else:
            m = CoreSmuMetrics(
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
                c1_pct=0.0,
                c6_pct=0.0,
            )
            slots.append(m)

    return SmuSnapshot(cores=cores, package=package, pm_version=version, slots=slots)


class RyzenSmuMonitor:
    """Interacts with the ryzen_smu kernel driver to retrieve Zen 3 telemetry."""

    def __init__(self, sysfs_dir: str = SYSFS_SMU_DIR):
        self.sysfs_dir = sysfs_dir
        self.pm_path = os.path.join(sysfs_dir, "pm_table")
        self.version_path = os.path.join(sysfs_dir, "pm_table_version")
        self.mp1_path = os.path.join(sysfs_dir, "mp1_smu_cmd")
        self.smu_args_path = os.path.join(sysfs_dir, "smu_args")
        self._fd: Optional[int] = None
        self._cached_version: Optional[int] = None
        self._cached_co_offsets: Optional[Dict[int, int]] = None

    def is_available(self) -> bool:
        """Returns True if the ryzen_smu kernel driver is loaded and the PM table is readable."""
        return os.path.isfile(self.pm_path) and os.path.isfile(self.version_path)

    def get_pm_version(self) -> Optional[int]:
        """Reads and caches the 32-bit PM table version from sysfs."""
        if self._cached_version is not None:
            return self._cached_version
        if not os.path.isfile(self.version_path):
            return None
        try:
            with open(self.version_path, "rb") as f:
                raw = f.read(4)
                if len(raw) == 4:
                    self._cached_version = struct.unpack("<I", raw)[0]
                    return self._cached_version
        except (OSError, PermissionError):
            pass
        return None

    def send_smu_command(
        self,
        op: int,
        arg1: int = 0,
        arg2: int = 0,
        arg3: int = 0,
        arg4: int = 0,
        arg5: int = 0,
        arg6: int = 0,
        timeout_s: float = 0.2,
    ) -> Optional[Tuple[int, ...]]:
        """Sends a mailbox command to the SMU via sysfs and returns response args, or None on failure."""
        if not (os.path.isfile(self.mp1_path) and os.path.isfile(self.smu_args_path)):
            return None
        try:
            t_start = time.time()
            with open(self.mp1_path, "rb") as f_cmd:
                raw = f_cmd.read(4)
                val = struct.unpack("<I", raw)[0] if len(raw) == 4 else 0
                while val == 0 and (time.time() - t_start) < timeout_s:
                    time.sleep(0.005)
                    f_cmd.seek(0)
                    raw = f_cmd.read(4)
                    val = struct.unpack("<I", raw)[0] if len(raw) == 4 else 0

            args_payload = struct.pack("<6I", arg1, arg2, arg3, arg4, arg5, arg6)
            with open(self.smu_args_path, "wb") as f_args:
                f_args.write(args_payload)

            with open(self.mp1_path, "wb") as f_cmd:
                f_cmd.write(struct.pack("<I", op))

            t_start = time.time()
            with open(self.mp1_path, "rb") as f_cmd:
                raw = f_cmd.read(4)
                val = struct.unpack("<I", raw)[0] if len(raw) == 4 else 0
                while val == 0 and (time.time() - t_start) < timeout_s:
                    time.sleep(0.005)
                    f_cmd.seek(0)
                    raw = f_cmd.read(4)
                    val = struct.unpack("<I", raw)[0] if len(raw) == 4 else 0

            if val != 1:
                return None

            with open(self.smu_args_path, "rb") as f_args:
                resp_raw = f_args.read(24)
                if len(resp_raw) == 24:
                    return struct.unpack("<6I", resp_raw)
        except (OSError, PermissionError):
            pass
        return None

    def read_co_offset(self, core_id: int) -> Optional[int]:
        """
        Reads the Curve Optimizer (DLDO PSM Margin) offset for a core on Zen 3 (Vermeer).
        Opcode 0x48 (GetDldoPsmMargin).
        """
        arg1 = ((core_id & 8) << 5 | (core_id & 7)) << 20
        resp = self.send_smu_command(0x48, arg1=arg1)
        if not resp:
            return None
        val = resp[0]
        if val > 0x7FFFFFFF:
            val = val - 0x100000000
        if -100 <= val <= 100:
            return val
        return None

    def read_all_co_offsets(self, slot_count: int = 16) -> Dict[int, int]:
        """Reads and caches Curve Optimizer offsets keyed by physical silicon slot index (0..15)."""
        if self._cached_co_offsets is not None:
            return self._cached_co_offsets

        offsets: Dict[int, int] = {}
        if not (os.path.isfile(self.mp1_path) and os.path.isfile(self.smu_args_path)):
            self._cached_co_offsets = offsets
            return offsets

        for s in range(slot_count):
            val = self.read_co_offset(s)
            if val is not None:
                offsets[s] = val

        self._cached_co_offsets = offsets
        return offsets

    def read_snapshot(
        self,
        target_core_count: Optional[int] = None,
        max_cores: Optional[int] = None,
    ) -> Optional[SmuSnapshot]:
        """Reads the current PM table dump and returns parsed SmuSnapshot, or None if unavailable."""
        version = self.get_pm_version()
        if version is None or get_smu_table_offsets(version) is None:
            return None

        try:
            if self._fd is None:
                self._fd = os.open(self.pm_path, os.O_RDONLY)
            os.lseek(self._fd, 0, os.SEEK_SET)
            raw = os.read(self._fd, 4096)
            if not raw:
                return None
            co_offsets = self.read_all_co_offsets(16)
            return parse_pm_table_buffer(
                version, raw, target_core_count=target_core_count, max_cores=max_cores, co_offsets=co_offsets
            )
        except (OSError, PermissionError):
            return None

    def close(self) -> None:
        """Closes open sysfs file descriptors."""
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
