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

from lib.orchestrator import HYPERTHREADING_MODES, ZenTunerOrchestrator
from lib.presenter import ConsolePresenter
from lib.smu import RyzenSmuMonitor
from lib.topology import TopologyError, discover_topology, parse_core_selection
from lib.tui import CursesPresenter
from lib.ui import Logger
from runners import RUNNER_CLASSES, Engine, get_runner

CYCLE_RUNNERS = ("prime95", "y-cruncher")


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {value}")
    return number


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"must not be negative: {value}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Zen Tuner - Per-core CPU stability tester and Curve Optimizer tuner.")
    parser.add_argument("--cores", type=str, default="all", help="Cores to test (e.g. 'all', '0-11', '0,2,4') (default: 'all')")
    parser.add_argument(
        "--test-iterations",
        type=positive_int,
        default=1,
        help="Number of complete test iterations per core (default: 1)",
    )
    parser.add_argument("--cycles", type=non_negative_int, default=0, help="Number of cycles (0 for infinite) (default: 0)")
    parser.add_argument(
        "--hyperthreading",
        type=str,
        default="on",
        choices=HYPERTHREADING_MODES,
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
        choices=[*RUNNER_CLASSES, "cycle"],
        help="Stress test engine: 'prime95', 'y-cruncher', or 'cycle' (alternates each cycle) (default: 'prime95')",
    )
    for runner_cls in RUNNER_CLASSES.values():
        runner_cls.add_cli_arguments(parser)
    return parser


def main() -> None:
    """Main CLI entry point for the Zen Tuner multi-core orchestrator."""
    parser = build_parser()
    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass
    args = parser.parse_args()

    try:
        all_cores = discover_topology()
    except TopologyError as e:
        parser.exit(1, f"[ERROR] {e}\n")
    if not all_cores:
        parser.exit(1, "[ERROR] Failed to discover CPU topology from /sys/devices/system/cpu!\n")

    try:
        selected_cores = parse_core_selection(args.cores, all_cores)
        runner_names = CYCLE_RUNNERS if args.runner == "cycle" else (args.runner,)
        engines = []
        for name in runner_names:
            runner = get_runner(name)
            if not runner.is_available():
                raise ValueError(f"Stress engine '{name}' binary is not available")
            parameters, profile = runner.parse_parameters(args)
            engines.append(Engine(runner, parameters, profile))
    except ValueError as e:
        parser.error(str(e))

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = args.log_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    log_file = os.path.join(log_dir, f"zen_tuner_{ts}.log")

    presenter_cls = CursesPresenter if sys.stdout.isatty() and sys.stdin.isatty() else ConsolePresenter

    with Logger(log_file) as logger, RyzenSmuMonitor(core_count=len(all_cores)) as smu_monitor:
        with presenter_cls(all_cores, logger=logger, smu_monitor=smu_monitor) as presenter:
            orchestrator = ZenTunerOrchestrator(engines, all_cores, presenter=presenter, smu_monitor=smu_monitor)
            try:
                stats = orchestrator.run(
                    selected_cores=selected_cores,
                    target_iterations=args.test_iterations,
                    hyperthreading_mode=args.hyperthreading,
                    cycles=args.cycles,
                    continue_on_error=not args.stop_on_error,
                )
            except KeyboardInterrupt:
                # Second Ctrl+C: the summary has already been printed while unwinding.
                sys.exit(130)

    sys.exit(0 if all(s.failures == 0 for s in stats.values()) else 1)


if __name__ == "__main__":
    main()
