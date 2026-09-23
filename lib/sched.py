"""
Linux scheduling helpers for the controller thread and spawned stress processes.
Scheduling policy and CPU affinity are per-thread attributes on Linux and are inherited by child processes.
"""

import contextlib
import os
from collections.abc import Iterator

CONTROLLER_RT_PRIORITY = 50
WORKER_RT_PRIORITY = 40


@contextlib.contextmanager
def thread_rt_priority(priority: int) -> Iterator[None]:
    """Runs the block with the calling thread at SCHED_RR priority (when permitted), then restores its policy."""
    old_policy = os.sched_getscheduler(0)
    old_param = os.sched_getparam(0)
    try:
        os.sched_setscheduler(0, os.SCHED_RR, os.sched_param(priority))
    except OSError:
        pass
    try:
        yield
    finally:
        try:
            os.sched_setscheduler(0, old_policy, old_param)
        except OSError:
            pass


@contextlib.contextmanager
def inherited_scheduling(cpus: list[int], rt_priority: int = WORKER_RT_PRIORITY) -> Iterator[None]:
    """
    Temporarily applies CPU affinity and SCHED_RR to the calling thread so that a process spawned inside
    the block inherits both at fork time, before it can start any threads of its own.
    The calling thread's original affinity and scheduling policy are restored on exit.
    """
    old_affinity = os.sched_getaffinity(0)
    os.sched_setaffinity(0, cpus)
    try:
        with thread_rt_priority(rt_priority):
            yield
    finally:
        os.sched_setaffinity(0, old_affinity)
