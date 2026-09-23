#!/usr/bin/env python3
"""
Diagnostic utility: Inspects AMD Ryzen SMU PM table telemetry, PBO limits, and per-core parameters.
"""

import argparse
import os
import sys
import time

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.smu import RyzenSmuMonitor
from lib.ui import BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW


def print_snapshot(snap) -> None:
    pkg = snap.package
    print(f"\n{BOLD}{CYAN}=== AMD Ryzen SMU PM Table (Version: 0x{snap.pm_version:06X}) ==={RESET}")
    print(f"Socket Power: {pkg.socket_power_w:.2f} W | Package Temp: {pkg.package_temp_c:.1f} °C")

    ppt_pct = (pkg.ppt_w / pkg.ppt_limit_w * 100.0) if pkg.ppt_limit_w > 0 else 0.0
    tdc_pct = (pkg.tdc_a / pkg.tdc_limit_a * 100.0) if pkg.tdc_limit_a > 0 else 0.0
    edc_pct = (pkg.edc_a / pkg.edc_limit_a * 100.0) if pkg.edc_limit_a > 0 else 0.0

    print(
        f"PPT: {pkg.ppt_w:6.2f} W / {pkg.ppt_limit_w:6.2f} W ({ppt_pct:5.1f}%) | "
        f"TDC: {pkg.tdc_a:5.1f} A / {pkg.tdc_limit_a:5.1f} A ({tdc_pct:5.1f}%) | "
        f"EDC: {pkg.edc_a:5.1f} A / {pkg.edc_limit_a:5.1f} A ({edc_pct:5.1f}%)"
    )
    hdr = (
        f"{'Core':<5} {'Voltage':>9} {'Power':>8} {'Temp':>8} {'CO':>5} "
        f"{'Target':>10} {'Eff MHz':>10} {'C0 %':>8} {'C1 %':>8} {'C6 %':>8}"
    )
    divider = "-" * len(hdr)
    print(divider)
    print(hdr)
    print(divider)

    current_ccd = None
    items = snap.slots if snap.slots else sorted(
        snap.cores.values(),
        key=lambda c: (c.ccd_idx, c.slot_idx if c.slot_idx is not None else (c.core_idx or 0)),
    )

    for c in items:
        if c.ccd_idx != current_ccd:
            current_ccd = c.ccd_idx
            ccd_hdr = f"--- CCD {current_ccd} "
            print(f"{CYAN}{BOLD}{ccd_hdr}{'-' * (len(hdr) - len(ccd_hdr))}{RESET}")

        if not c.is_enabled or c.core_idx is None:
            dis_txt = (
                f"{'-':<5s} {'--':>9s} {'--':>8s} {'--':>8s} {'--':>5s} "
                f"{'--':>10s} {'--':>10s} {'--':>8s} {'--':>8s} {'--':>8s}"
            )
            print(f"{DIM}{dis_txt}{RESET}")
        else:
            c0_col = GREEN if c.c0_pct > 50.0 else (YELLOW if c.c0_pct > 5.0 else RESET)
            c1_col = YELLOW if c.c1_pct > 50.0 else RESET
            c6_col = DIM if c.c6_pct > 50.0 else RESET
            co_str = f"{c.co_offset:+d}" if c.co_offset is not None else "--"
            v_str = f"{c.voltage_v:6.4f} V"
            p_str = f"{c.power_w:5.2f} W"
            t_str = f"{c.temp_c:4.1f} °C"
            f_str = f"{c.frequency_mhz:5.0f} MHz"
            eff_str = f"{c.effective_mhz:5.0f} MHz"
            c0_str = f"{c.c0_pct:5.1f} %"
            c1_str = f"{c.c1_pct:5.1f} %"
            c6_str = f"{c.c6_pct:5.1f} %"
            row_txt = (
                f"{c.core_idx:<5d} {v_str:>9s} {p_str:>8s} {t_str:>8s} {co_str:>5s} "
                f"{f_str:>10s} {eff_str:>10s} "
                f"{c0_col}{c0_str:>8s}{RESET} {c1_col}{c1_str:>8s}{RESET} {c6_col}{c6_str:>8s}{RESET}"
            )
            print(row_txt)
    print(divider)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect AMD Ryzen SMU PM table telemetry.")
    parser.add_argument("--loop", action="store_true", help="Continuously poll and display telemetry")
    parser.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds (default: 1.0)")
    parser.add_argument("--cores", type=int, default=None, help="Number of enabled physical cores (default: auto-detect)")
    args = parser.parse_args()

    with RyzenSmuMonitor(core_count=args.cores) as mon:
        if not os.path.isfile(mon.pm_path):
            print(f"{BOLD}{RED}[ERROR] ryzen_smu kernel driver interface not found!{RESET}")
            print(f"Path '{mon.pm_path}' is not accessible.")
            print(f"{YELLOW}Hint: Ensure kernel module is loaded: sudo modprobe ryzen_smu{RESET}")
            sys.exit(1)

        version = mon.get_pm_version()
        if version is None:
            print(f"{RED}[ERROR] Failed to read pm_table_version from sysfs!{RESET}")
            sys.exit(1)
        if not mon.is_available():
            print(f"{RED}[ERROR] Unsupported PM table version 0x{version:06X}!{RESET}")
            sys.exit(1)

        try:
            while True:
                snap = mon.read_snapshot()
                if not snap:
                    print(f"{RED}[ERROR] Failed to parse PM table buffer (version: 0x{version:06X})!{RESET}")
                    sys.exit(1)

                if args.loop:
                    os.system("clear")
                print_snapshot(snap)

                if not args.loop:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Stopped.{RESET}")


if __name__ == "__main__":
    main()

