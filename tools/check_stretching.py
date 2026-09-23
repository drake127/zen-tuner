#!/usr/bin/env python3
"""
Diagnostic utility: Live monitor of SMU-reported clock stretching of a physical core (requires ryzen_smu).
"""

import argparse
import os
import sys
import time

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.models import STRETCH_THRESHOLD_MHZ
from lib.monitors import CoreTelemetryMonitor
from lib.smu import RyzenSmuMonitor
from lib.topology import discover_topology
from lib.ui import RESET, YELLOW
from lib.views import fmt_drop


def main() -> None:
    parser = argparse.ArgumentParser(description="Live monitor of SMU-reported clock stretching of a physical core.")
    parser.add_argument("--core", type=int, default=0, help="Physical core index to monitor (default: 0)")
    parser.add_argument("--interval", type=float, default=1.0, help="Sampling interval in seconds (default: 1.0)")
    parser.add_argument("--duration", type=float, default=10.0, help="Total monitoring duration in seconds (default: 10)")
    parser.add_argument(
        "--threshold",
        type=float,
        default=STRETCH_THRESHOLD_MHZ,
        help=f"Stretch alert threshold in MHz (default: {STRETCH_THRESHOLD_MHZ})",
    )
    args = parser.parse_args()

    with RyzenSmuMonitor(core_count=len(discover_topology()) or None) as smu:
        if not smu.is_available():
            print("[ERROR] ryzen_smu telemetry is not available; clock stretching cannot be measured.", file=sys.stderr)
            sys.exit(1)

        print(f"Monitoring Core {args.core} for {args.duration:.1f}s (interval {args.interval}s, "
              f"threshold {args.threshold} MHz). Samples are taken only under full load (C0 >= 95%).")
        mon = CoreTelemetryMonitor(smu, args.core, threshold_mhz=args.threshold, sample_interval=args.interval)
        start = time.monotonic()
        while time.monotonic() - start < args.duration:
            alert = mon.poll()
            if alert:
                print(
                    f"[{YELLOW}ALERT{RESET}] Core {args.core}: Target {alert.target_mhz:.0f} MHz vs "
                    f"Effective {alert.effective_mhz:.0f} MHz (Drop: -{alert.stretch_mhz:.0f} MHz)"
                )
            time.sleep(args.interval / 4.0)

    summary = mon.summary
    print("\nSummary:")
    print(f"Samples under load: {len(mon.samples)}")
    if summary:
        print(f"Median drop: {fmt_drop(summary.stretch_mhz)} | Max drop: {summary.max_stretch_mhz:.0f} MHz | "
              f"Stretching detected: {summary.stretching_detected}")


if __name__ == "__main__":
    main()
