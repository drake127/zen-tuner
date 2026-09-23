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

from lib.orchestrator import ZenTunerOrchestrator
from lib.smu import RyzenSmuMonitor
from lib.topology import discover_topology, parse_core_selection
from lib.tui import CursesPresenter
from lib.ui import Logger
from runners import build_fft_config, get_registered_runner_classes, get_runner, parse_time


def main() -> None:
    """Main CLI entry point for the Zen Tuner multi-core orchestrator."""
    parser = argparse.ArgumentParser(
        description="Zen Tuner - Per-core CPU stability tester and Curve Optimizer tuner."
    )
    parser.add_argument("--cores", type=str, default="all", help="Cores to test (e.g. 'all', '0-11', '0,2,4') (default: 'all')")

    timing_group = parser.add_mutually_exclusive_group()
    timing_group.add_argument(
        "--time",
        type=str,
        default=None,
        help="Duration per core (e.g. '360s', '5m', '10m') (default: '360s')",
    )
    timing_group.add_argument(
        "--tests",
        type=int,
        default=None,
        help="Target completed verified self-tests per core (mutually exclusive with --time) (default: None)",
    )

    parser.add_argument("--cycles", type=int, default=0, help="Number of cycles (0 for infinite) (default: 0)")
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
        help="Stop testing immediately on first error (default: False)",
    )
    parser.add_argument("--log-dir", type=str, default=None, help="Directory for session log files (default: ./logs)")
    parser.add_argument(
        "--runner",
        type=str.lower,
        default="prime95",
        choices=["prime95", "y-cruncher", "ycruncher", "cycle"],
        help="Stress test engine: 'prime95', 'y-cruncher', or 'cycle' (alternates each cycle) (default: 'prime95')",
    )

    for runner_cls in get_registered_runner_classes():
        runner_cls.add_cli_arguments(parser)

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

    runner_choice = args.runner.lower()
    if runner_choice == "cycle":
        p95_runner = get_runner("prime95")
        yc_runner = get_runner("y-cruncher")
        p95_params, p95_desc = p95_runner.parse_parameters(args)
        yc_params, yc_desc = yc_runner.parse_parameters(args)

        runners = {"prime95": p95_runner, "y-cruncher": yc_runner}
        runner_params = {"prime95": p95_params, "y-cruncher": yc_params}
        runner_mode = "cycle"
        runner_banner_name = "cycle (prime95 ↔ y-cruncher)"
        profile_desc = f"{p95_desc} / {yc_desc}"
    else:
        runner = get_runner(runner_choice)
        runner_params, profile_desc = runner.parse_parameters(args)
        runners = runner
        runner_mode = "single"
        runner_banner_name = runner.name

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
                runner_name=runner_banner_name,
                hyperthreading_mode=args.hyperthreading,
                profile_info=profile_desc,
                tests_target=target_tests,
                cycles=args.cycles,
                graceful=True,
            )

            orchestrator = ZenTunerOrchestrator(
                runner=runners,
                all_cores=all_cores,
                presenter=presenter,
                runner_mode=runner_mode,
            )
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
