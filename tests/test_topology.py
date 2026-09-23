"""
Unit tests for CPU topology discovery and core selection parsing.
"""

import pytest

from lib.models import PhysicalCore
from lib.topology import TopologyError, discover_topology, parse_core_selection


@pytest.fixture
def cores() -> list[PhysicalCore]:
    layout = [(0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (5, 0), (8, 1), (9, 1)]
    return [
        PhysicalCore(core_idx=i, hardware_core_id=hw, ccd_id=ccd, logical_cpus=[i, i + 12])
        for i, (hw, ccd) in enumerate(layout)
    ]


def write_sysfs(root, cpu: int, rel_path: str, value: int) -> None:
    path = root / f"cpu{cpu}" / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n")


@pytest.mark.parametrize("selection", ["all", "*"])
def test_parse_core_selection_all(cores, selection):
    assert parse_core_selection(selection, cores) == cores


@pytest.mark.parametrize(
    "selection, expected",
    [("2", [2]), ("1-3", [1, 2, 3]), ("0, 2-4, 7", [0, 2, 3, 4, 7])],
)
def test_parse_core_selection(cores, selection, expected):
    assert [c.core_idx for c in parse_core_selection(selection, cores)] == expected


@pytest.mark.parametrize("selection", ["foo", "1-x", "-1", ""])
def test_parse_core_selection_invalid_syntax(cores, selection):
    with pytest.raises(ValueError):
        parse_core_selection(selection, cores)


def test_parse_core_selection_reversed_range(cores):
    with pytest.raises(ValueError, match="bounds"):
        parse_core_selection("4-2", cores)


@pytest.mark.parametrize("selection, unknown", [("99", r"\[99\]"), ("6-9", r"\[8, 9\]")])
def test_parse_core_selection_unknown_cores(cores, selection, unknown):
    with pytest.raises(ValueError, match=unknown):
        parse_core_selection(selection, cores)


def test_discover_topology_mocked_sysfs(tmp_path):
    # SMT siblings share core_id; CCD 1 cores sort after CCD 0 regardless of CPU numbering
    for cpu, core_id, l3 in ((0, 0, 0), (1, 8, 1), (2, 0, 0), (3, 8, 1), (4, 1, 0)):
        write_sysfs(tmp_path, cpu, "topology/core_id", core_id)
        write_sysfs(tmp_path, cpu, "cache/index3/id", l3)

    discovered = discover_topology(sysfs_root=str(tmp_path))
    assert [c.hardware_core_id for c in discovered] == [0, 1, 8]
    assert [c.core_idx for c in discovered] == [0, 1, 2]
    assert discovered[0].logical_cpus == [0, 2]
    assert discovered[2].logical_cpus == [1, 3]
    assert discovered[2].ccd_id == 1


def test_discover_topology_rejects_multisocket(tmp_path):
    for cpu, pkg in ((0, 0), (1, 1)):
        write_sysfs(tmp_path, cpu, "topology/core_id", 0)
        write_sysfs(tmp_path, cpu, "topology/physical_package_id", pkg)

    with pytest.raises(TopologyError, match="Multi-socket systems are not supported"):
        discover_topology(sysfs_root=str(tmp_path))


def test_discover_topology_cppc_preferred_core(tmp_path):
    for cpu, score in ((0, 216), (1, 211), (2, 196)):
        write_sysfs(tmp_path, cpu, "topology/core_id", cpu)
        write_sysfs(tmp_path, cpu, "acpi_cppc/highest_perf", score)

    discovered = discover_topology(sysfs_root=str(tmp_path))
    assert [c.pref_rank for c in discovered] == [1, 2, 3]
    assert [c.cppc_perf for c in discovered] == [216, 211, 196]
