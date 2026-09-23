"""
CPU topology discovery and core selection parsing.
Discovers physical cores, hardware core IDs, CCDs, and logical CPU siblings from sysfs.
"""

import glob
import os
from typing import Any

from lib.models import PhysicalCore


def _read_cppc_perf(cpu_id: int, sysfs_root: str) -> int | None:
    """Reads CPPC highest_perf or amd-pstate prefcore ranking from sysfs for cpu_id."""
    paths = (
        os.path.join(sysfs_root, f"cpu{cpu_id}", "acpi_cppc", "highest_perf"),
        os.path.join(sysfs_root, f"cpu{cpu_id}", "cpufreq", "amd_pstate_prefcore_ranking"),
    )
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return int(f.read().strip())
            except (OSError, ValueError):
                pass
    return None


def discover_topology(sysfs_root: str = "/sys/devices/system/cpu") -> list[PhysicalCore]:
    """
    Scans sysfs to discover physical CPU cores, SMT siblings, CCD / L3 cache topology,
    and CPPC preferred core ranking.
    Returns cores sorted by CCD ID and hardware core ID.
    """
    raw_cores: dict[int, dict[str, Any]] = {}
    package_ids: set[int] = set()
    pattern = os.path.join(sysfs_root, "cpu[0-9]*")

    for cpu_path in sorted(glob.glob(pattern)):
        base = os.path.basename(cpu_path)
        try:
            cpu_id = int(base.replace("cpu", ""))
        except ValueError:
            continue

        pkg_file = os.path.join(cpu_path, "topology/physical_package_id")
        if os.path.exists(pkg_file):
            try:
                with open(pkg_file, "r", encoding="utf-8") as f:
                    package_ids.add(int(f.read().strip()))
            except (OSError, ValueError):
                pass

        core_id_file = os.path.join(cpu_path, "topology/core_id")
        if not os.path.exists(core_id_file):
            continue

        try:
            with open(core_id_file, "r", encoding="utf-8") as f:
                hw_core_id = int(f.read().strip())
        except (OSError, ValueError):
            continue

        l3_file = os.path.join(cpu_path, "cache/index3/id")
        ccd_id: int | None = None
        if os.path.exists(l3_file):
            try:
                with open(l3_file, "r", encoding="utf-8") as f:
                    ccd_id = int(f.read().strip())
            except (OSError, ValueError):
                pass

        if hw_core_id not in raw_cores:
            raw_cores[hw_core_id] = {"cpus": [], "ccd": ccd_id}
        raw_cores[hw_core_id]["cpus"].append(cpu_id)

    assert len(package_ids) <= 1, f"Multi-socket systems are not supported (detected sockets: {package_ids})"

    # Sort cores by CCD ID then by hardware core ID
    sorted_hw_ids = sorted(raw_cores.keys(), key=lambda hid: (raw_cores[hid]["ccd"] or 0, hid))
    cores: list[PhysicalCore] = []
    for idx, hid in enumerate(sorted_hw_ids):
        cpus = sorted(raw_cores[hid]["cpus"])
        perf = _read_cppc_perf(cpus[0], sysfs_root) if cpus else None
        cores.append(
            PhysicalCore(
                core_idx=idx,
                hardware_core_id=hid,
                ccd_id=raw_cores[hid]["ccd"],
                logical_cpus=cpus,
                cppc_perf=perf,
            )
        )

    # Determine preferred ranking per CCD based on CPPC performance ranking
    ccd_cores: dict[int | None, list[PhysicalCore]] = {}
    for c in cores:
        ccd_cores.setdefault(c.ccd_id, []).append(c)

    core_ranks: dict[int, int] = {}
    for ccd_id, group in ccd_cores.items():
        unique_perfs = sorted(
            {c.cppc_perf for c in group if c.cppc_perf is not None and c.cppc_perf > 0},
            reverse=True,
        )
        perf_to_rank = {p: r + 1 for r, p in enumerate(unique_perfs)}
        for c in group:
            if c.cppc_perf in perf_to_rank:
                core_ranks[c.core_idx] = perf_to_rank[c.cppc_perf]

    if core_ranks:
        cores = [
            PhysicalCore(
                core_idx=c.core_idx,
                hardware_core_id=c.hardware_core_id,
                ccd_id=c.ccd_id,
                logical_cpus=c.logical_cpus,
                cppc_perf=c.cppc_perf,
                pref_rank=core_ranks.get(c.core_idx),
                is_preferred=(core_ranks.get(c.core_idx) == 1),
            )
            for c in cores
        ]

    return cores


def parse_core_selection(selection_str: str, available_cores: list[PhysicalCore]) -> list[PhysicalCore]:
    """
    Parses core selection strings like 'all', '0-5', '0,2,4' into a list of PhysicalCore objects.
    Raises ValueError on invalid formats or empty selections.
    """
    selection_str = selection_str.strip().lower()
    if selection_str in ("all", "*"):
        return list(available_cores)

    selected_indices: set[int] = set()
    parts = [p.strip() for p in selection_str.split(",") if p.strip()]
    if not parts:
        raise ValueError(f"Empty core selection string: '{selection_str}'")

    for part in parts:
        if "-" in part:
            split_parts = part.split("-", 1)
            try:
                start, end = int(split_parts[0]), int(split_parts[1])
                if start > end:
                    raise ValueError(f"Invalid range bounds: '{part}'")
                selected_indices.update(range(start, end + 1))
            except ValueError as exc:
                raise ValueError(f"Invalid core range: '{part}'") from exc
        else:
            try:
                selected_indices.add(int(part))
            except ValueError as exc:
                raise ValueError(f"Invalid core index: '{part}'") from exc

    core_map = {c.core_idx: c for c in available_cores}
    selected_cores = [core_map[i] for i in sorted(selected_indices) if i in core_map]

    if not selected_cores:
        available_range = f"0-{len(available_cores) - 1}" if available_cores else "none"
        raise ValueError(f"No valid cores matching '{selection_str}'. Available: {available_range}")

    return selected_cores

