#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

"""
Zen Tuner: Advanced per-core stability tester and AMD Curve Optimizer tuner.
Main CLI entry point coordinating topology discovery, stress engines, SMU, and presentation.
"""

import argparse
import datetime
import os
import sys

from lib.cpu import detect_cpu_instruction_sets, get_default_instruction_set
from lib.orchestrator import ZenTunerOrchestrator
from lib.smu import RyzenSmuMonitor
from lib.topology import discover_topology, parse_core_selection
from lib.tui import CursesPresenter
from lib.ui import Logger
from runners import FFTConfig, Prime95Runner, build_fft_config, get_runner, parse_time


def main() -> None:
    """Main CLI entry point for the Zen Tuner multi-core orchestrator."""
    parser = argparse.ArgumentParser(
        description="Zen Tuner - Per-core CPU stability tester and Curve Optimizer tuner."
    )
    parser.add_argument("--cores", type=str, default="all", help="Cores to test (e.g. 'all', '0-11', '0,2,4', default: 'all')")

    timing_group = parser.add_mutually_exclusive_group()
    timing_group.add_argument(
        "--time",
        type=str,
        default=None,
        help="Duration per core (e.g. '360s', '5m', '10m', default: '360s')",
    )
    timing_group.add_argument(
        "--tests",
        type=int,
        default=None,
        help="Target completed verified self-tests per core (mutually exclusive with --time)",
    )

    parser.add_argument("--cycles", type=int, default=0, help="Number of cycles (0 for infinite, default: 0)")
    parser.add_argument(
        "--hyperthreading",
        type=str,
        default="on",
        choices=["off", "on", "cycle", "rr"],
        help="Hyperthreading mode: 'off' (1T), 'on' (2T SMT), 'cycle' (alternate T0->T1->T0+T1) (default: 'on')",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        default=False,
        help="Stop testing immediately on first error (default: continue on error)",
    )
    parser.add_argument("--log-dir", type=str, default=None, help="Directory for session log files (default: ./logs)")
    parser.add_argument("--runner", type=str, default="prime95", help="Stress test engine (default: 'prime95')")

    Prime95Runner.add_cli_arguments(parser)

    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass

    args = parser.parse_args()

    all_cores = discover_topology()
    if not all_cores:
        print("[ERROR] Failed to discover CPU topology from /sys/devices/system/cpu!", file=sys.stderr)
        sys.exit(1)

    try:
        selected_cores = parse_core_selection(args.cores, all_cores)
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    if args.tests is None and args.time is None:
        duration: float | None = parse_time("360s")
        target_tests: int | None = None
    elif args.tests is not None:
        duration = None
        target_tests = args.tests
    else:
        duration = parse_time(args.time)
        target_tests = None

    # Logging setup
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = args.log_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    log_file = os.path.join(log_dir, f"zen_tuner_{ts}.log")

    runner = get_runner(args.runner, mprime_path=getattr(args, "mprime", None))

    # Engine specific parameters (e.g. FFT configuration for Prime95)
    supported_modes = detect_cpu_instruction_sets()
    default_mode = get_default_instruction_set(supported_modes)
    req_mode = getattr(args, "mode", None)
    if req_mode:
        mode_val = req_mode.lower()
        if mode_val not in supported_modes:
            print(
                f"[WARNING] Requested instruction mode '{mode_val}' is not supported by CPU "
                f"(supported: {sorted(supported_modes)}).",
                file=sys.stderr,
            )
    else:
        mode_val = default_mode

    fft_cfg = build_fft_config(args.fft, args.min_fft, args.max_fft, args.memory, mode=mode_val)
    profile_desc = f"{fft_cfg.desc} [{mode_val.upper()}]"
    runner_params = {
        "fft_preset": args.fft,
        "fft_config": fft_cfg,
        "custom_fft": fft_cfg,
        "test_time_min": getattr(args, "test_time", 1),
        "mode": mode_val,
    }

    with Logger(log_file) as logger, RyzenSmuMonitor() as smu_monitor:
        presenter = CursesPresenter(
            all_cores=all_cores,
            duration_per_core=duration,
            logger=logger,
            smu_monitor=smu_monitor,
        )
        with presenter:
            presenter.print_banner(
                cores=selected_cores,
                duration=duration,
                runner_name=runner.name,
                hyperthreading_mode=args.hyperthreading,
                profile_info=profile_desc,
                tests_target=target_tests,
                cycles=args.cycles,
                graceful=True,
            )

            orchestrator = ZenTunerOrchestrator(runner=runner, all_cores=all_cores, presenter=presenter)
            stats = orchestrator.run(
                selected_cores=selected_cores,
                duration_per_core=duration,
                target_tests_per_core=target_tests,
                hyperthreading_mode=args.hyperthreading,
                cycles=args.cycles,
                continue_on_error=not args.stop_on_error,
                graceful=True,
                runner_parameters=runner_params,
            )

    total_fails = sum(s.failures for s in stats.values())
    sys.exit(0 if total_fails == 0 else 1)


if __name__ == "__main__":
    main()
