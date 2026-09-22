#!/usr/bin/env python3
"""
Diagnostic utility: Live monitor for AMD Ryzen APERF/MPERF clock stretching on specified CPUs.
"""

import argparse
import os
import sys
import time

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.monitors import CycleStretchingMonitor

BOLD = "\033[1m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RESET = "\033[0m"


def main() -> None:
    parser = argparse.ArgumentParser(description="Live monitor for AMD Ryzen APERF/MPERF clock stretching.")
    parser.add_argument("--cpus", type=str, default="0", help="Comma-separated CPU IDs to monitor (e.g. '0,12')")
    parser.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds (default: 1.0)")
    parser.add_argument("--duration", type=float, default=10.0, help="Total monitoring duration in seconds (default: 10)")
    parser.add_argument("--threshold", type=float, default=50.0, help="Stretch alert threshold in MHz (default: 50.0)")

    args = parser.parse_args()
    cpus = [int(c.strip()) for c in args.cpus.split(",") if c.strip()]

    print(f"Monitoring CPUs {cpus} for {args.duration:.1f}s (Interval: {args.interval}s, Threshold: {args.threshold} MHz)...")
    start = time.time()

    with CycleStretchingMonitor(cpus=cpus, threshold_mhz=args.threshold, sample_interval=args.interval) as mon:
        while time.time() - start < args.duration:
            samples = mon.poll()
            if samples:
                for s in samples:
                    print(
                        f"[{YELLOW}ALERT{RESET}] CPU {s.cpu}: Target {s.target_mhz:.0f} MHz vs "
                        f"Effective {s.effective_mhz:.0f} MHz (Drop: -{s.stretch_mhz:.0f} MHz / -{s.stretch_pct:.1f}%)"
                    )
            time.sleep(args.interval / 2.0)

    print("\nSummary:")
    print(f"Total samples recorded: {len(mon.all_samples)}")
    print(f"Max stretch recorded: {mon.max_stretch_mhz:.1f} MHz")
    print(f"Alerts exceeding {args.threshold} MHz: {mon.stretch_alerts_count}")


if __name__ == "__main__":
    main()

