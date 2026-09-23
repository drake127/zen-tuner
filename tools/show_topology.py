#!/usr/bin/env python3
"""
Diagnostic utility: Inspects and displays the physical CPU topology, CCD grouping, and SMT siblings.
"""

import os
import sys

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.topology import TopologyError, discover_topology
from lib.ui import BOLD, CYAN, RESET


def main() -> None:
    try:
        cores = discover_topology()
    except TopologyError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    if not cores:
        print("Failed to discover CPU topology from /sys/devices/system/cpu!", file=sys.stderr)
        sys.exit(1)

    print(f"{BOLD}{CYAN}=== Discovered CPU Topology ({len(cores)} physical cores) ==={RESET}")
    print(f"{'Core':<6} {'HW Core ID':<12} {'CCD / L3':<12} {'SMT CPUs (Logical Threads)'}")
    print("-" * 55)
    for c in cores:
        ccd = f"CCD {c.ccd_id}" if c.ccd_id is not None else "N/A"
        print(f"{c.core_idx:<6d} {c.hardware_core_id:<12d} {ccd:<12s} {str(c.logical_cpus)}")
    print("-" * 55)


if __name__ == "__main__":
    main()

