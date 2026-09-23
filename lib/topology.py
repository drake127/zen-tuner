"""
CPU topology discovery and core selection parsing.
Discovers physical cores, hardware core IDs, CCDs, and logical CPU siblings from sysfs.
"""

import dataclasses
import glob
import os

from lib.models import PhysicalCore


class TopologyError(Exception):
    """Raised when the CPU topology cannot be used by Zen Tuner."""


def _read_int(path: str) -> int | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _read_cppc_perf(cpu_id: int, sysfs_root: str) -> int | None:
    """Reads CPPC highest_perf or amd-pstate prefcore ranking from sysfs for cpu_id."""
    for rel_path in ("acpi_cppc/highest_perf", "cpufreq/amd_pstate_prefcore_ranking"):
        perf = _read_int(os.path.join(sysfs_root, f"cpu{cpu_id}", rel_path))
        if perf is not None:
            return perf
    return None


def discover_topology(sysfs_root: str = "/sys/devices/system/cpu") -> list[PhysicalCore]:
    """
    Scans sysfs to discover physical CPU cores, SMT siblings, CCD / L3 cache topology,
    and CPPC preferred core ranking.
    Returns cores sorted by CCD ID and hardware core ID. Raises TopologyError on multi-socket systems.
    """
    raw_cores: dict[int, tuple[int | None, list[int]]] = {}
    package_ids: set[int] = set()

    for cpu_path in glob.glob(os.path.join(sysfs_root, "cpu[0-9]*")):
        try:
            cpu_id = int(os.path.basename(cpu_path).removeprefix("cpu"))
        except ValueError:
            continue

        package_id = _read_int(os.path.join(cpu_path, "topology/physical_package_id"))
        if package_id is not None:
            package_ids.add(package_id)

        hw_core_id = _read_int(os.path.join(cpu_path, "topology/core_id"))
        if hw_core_id is None:
            continue

        ccd_id = _read_int(os.path.join(cpu_path, "cache/index3/id"))
        raw_cores.setdefault(hw_core_id, (ccd_id, []))[1].append(cpu_id)

    if len(package_ids) > 1:
        raise TopologyError(f"Multi-socket systems are not supported (detected sockets: {sorted(package_ids)})")

    sorted_hw_ids = sorted(raw_cores, key=lambda hid: (raw_cores[hid][0] or 0, hid))
    cores: list[PhysicalCore] = []
    for idx, hid in enumerate(sorted_hw_ids):
        ccd_id, cpus = raw_cores[hid]
        cpus.sort()
        cores.append(
            PhysicalCore(
                core_idx=idx,
                hardware_core_id=hid,
                ccd_id=ccd_id,
                logical_cpus=cpus,
                cppc_perf=_read_cppc_perf(cpus[0], sysfs_root),
            )
        )

    # Rank cores within each CCD by CPPC performance score (1 = best)
    ccd_perfs: dict[int | None, set[int]] = {}
    for c in cores:
        if c.cppc_perf is not None and c.cppc_perf > 0:
            ccd_perfs.setdefault(c.ccd_id, set()).add(c.cppc_perf)
    ranks = {ccd: {p: r + 1 for r, p in enumerate(sorted(perfs, reverse=True))} for ccd, perfs in ccd_perfs.items()}

    return [dataclasses.replace(c, pref_rank=ranks.get(c.ccd_id, {}).get(c.cppc_perf)) for c in cores]


def parse_core_selection(selection_str: str, available_cores: list[PhysicalCore]) -> list[PhysicalCore]:
    """
    Parses core selection strings like 'all', '0-5', '0,2,4' into a list of PhysicalCore objects.
    Raises ValueError on invalid formats, unknown core indices, or empty selections.
    """
    selection_str = selection_str.strip().lower()
    if selection_str in ("all", "*"):
        return list(available_cores)

    parts = [p.strip() for p in selection_str.split(",") if p.strip()]
    if not parts:
        raise ValueError(f"Empty core selection string: '{selection_str}'")

    selected_indices: set[int] = set()
    for part in parts:
        start_str, sep, end_str = part.partition("-")
        try:
            start = int(start_str)
            end = int(end_str) if sep else start
        except ValueError as exc:
            raise ValueError(f"Invalid core selection: '{part}'") from exc
        if start > end:
            raise ValueError(f"Invalid core range bounds: '{part}'")
        selected_indices.update(range(start, end + 1))

    core_map = {c.core_idx: c for c in available_cores}
    unknown = sorted(selected_indices - core_map.keys())
    if unknown:
        available_range = f"0-{len(available_cores) - 1}" if available_cores else "none"
        raise ValueError(f"Unknown core indices {unknown} in '{selection_str}'. Available: {available_range}")

    return [core_map[i] for i in sorted(selected_indices)]
