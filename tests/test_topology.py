"""
Unit tests for CPU topology discovery and core selection parsing.
"""

import os
import unittest
from lib.models import PhysicalCore
from lib.topology import discover_topology, parse_core_selection


class TestTopology(unittest.TestCase):

    def setUp(self):
        self.cores = [
            PhysicalCore(core_idx=0, hardware_core_id=0, ccd_id=0, logical_cpus=[0, 12]),
            PhysicalCore(core_idx=1, hardware_core_id=1, ccd_id=0, logical_cpus=[1, 13]),
            PhysicalCore(core_idx=2, hardware_core_id=2, ccd_id=0, logical_cpus=[2, 14]),
            PhysicalCore(core_idx=3, hardware_core_id=3, ccd_id=0, logical_cpus=[3, 15]),
            PhysicalCore(core_idx=4, hardware_core_id=4, ccd_id=0, logical_cpus=[4, 16]),
            PhysicalCore(core_idx=5, hardware_core_id=5, ccd_id=0, logical_cpus=[5, 17]),
            PhysicalCore(core_idx=6, hardware_core_id=8, ccd_id=1, logical_cpus=[6, 18]),
            PhysicalCore(core_idx=7, hardware_core_id=9, ccd_id=1, logical_cpus=[7, 19]),
        ]

    def test_parse_core_selection_all(self):
        res = parse_core_selection("all", self.cores)
        self.assertEqual(len(res), 8)
        self.assertEqual(res, self.cores)

        res_star = parse_core_selection("*", self.cores)
        self.assertEqual(res_star, self.cores)

    def test_parse_core_selection_single(self):
        res = parse_core_selection("2", self.cores)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].core_idx, 2)

    def test_parse_core_selection_range(self):
        res = parse_core_selection("1-3", self.cores)
        self.assertEqual([c.core_idx for c in res], [1, 2, 3])

    def test_parse_core_selection_combined(self):
        res = parse_core_selection("0, 2-4, 7", self.cores)
        self.assertEqual([c.core_idx for c in res], [0, 2, 3, 4, 7])

    def test_parse_core_selection_invalid_syntax(self):
        with self.assertRaises(ValueError):
            parse_core_selection("foo", self.cores)

        with self.assertRaises(ValueError):
            parse_core_selection("4-2", self.cores)

    def test_parse_core_selection_out_of_bounds(self):
        with self.assertRaises(ValueError):
            parse_core_selection("99", self.cores)

    def test_discover_topology_mocked_sysfs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            for cpu in (0, 1):
                p = os.path.join(tmpdir, f"cpu{cpu}", "topology")
                os.makedirs(p)
                with open(os.path.join(p, "core_id"), "w") as f:
                    f.write(f"{cpu}\n")

            discovered = discover_topology(sysfs_root=tmpdir)
            self.assertEqual(len(discovered), 2)
            self.assertEqual(discovered[0].core_idx, 0)
            self.assertEqual(discovered[1].core_idx, 1)

    def test_discover_topology_multisocket_assert(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # cpu0 on socket 0, cpu1 on socket 1
            for cpu, pkg in ((0, 0), (1, 1)):
                p = os.path.join(tmpdir, f"cpu{cpu}", "topology")
                os.makedirs(p)
                with open(os.path.join(p, "core_id"), "w") as f:
                    f.write("0\n")
                with open(os.path.join(p, "physical_package_id"), "w") as f:
                    f.write(f"{pkg}\n")

            with self.assertRaises(AssertionError) as ctx:
                discover_topology(sysfs_root=tmpdir)
            self.assertIn("Multi-socket systems are not supported", str(ctx.exception))

    def test_discover_topology_cppc_preferred_core(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create cpu0 (score 216), cpu1 (score 211), cpu2 (score 196) in CCD 0
            for cpu, score in ((0, 216), (1, 211), (2, 196)):
                top_p = os.path.join(tmpdir, f"cpu{cpu}", "topology")
                os.makedirs(top_p)
                with open(os.path.join(top_p, "core_id"), "w") as f:
                    f.write(f"{cpu}\n")

                cppc_p = os.path.join(tmpdir, f"cpu{cpu}", "acpi_cppc")
                os.makedirs(cppc_p)
                with open(os.path.join(cppc_p, "highest_perf"), "w") as f:
                    f.write(f"{score}\n")

            discovered = discover_topology(sysfs_root=tmpdir)
            self.assertEqual(len(discovered), 3)
            # Core 0: rank 1 (gold)
            self.assertTrue(discovered[0].is_preferred)
            self.assertEqual(discovered[0].pref_rank, 1)
            self.assertEqual(discovered[0].cppc_perf, 216)
            # Core 1: rank 2 (silver)
            self.assertFalse(discovered[1].is_preferred)
            self.assertEqual(discovered[1].pref_rank, 2)
            self.assertEqual(discovered[1].cppc_perf, 211)
            # Core 2: rank 3
            self.assertFalse(discovered[2].is_preferred)
            self.assertEqual(discovered[2].pref_rank, 3)
            self.assertEqual(discovered[2].cppc_perf, 196)


if __name__ == "__main__":
    unittest.main()

