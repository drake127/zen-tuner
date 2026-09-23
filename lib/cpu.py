"""
CPU hardware capabilities and instruction set detection.
Discovers CPUID feature flags from /proc/cpuinfo once per process.
"""

import functools
import os

INSTRUCTION_SET_HIERARCHY = ("avx512", "avx2", "avx", "sse")


@functools.cache
def detect_cpu_instruction_sets(cpuinfo_path: str = "/proc/cpuinfo") -> frozenset[str]:
    """
    Detects CPU instruction sets supported by the hardware (sse, avx, avx2, avx512).
    Inspects kernel-decoded CPUID feature flags from /proc/cpuinfo.
    """
    supported: set[str] = set()
    try:
        with open(cpuinfo_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith(("flags", "Features")):
                    _, _, value = line.partition(":")
                    flags = set(value.split())
                    if "sse" in flags or "sse2" in flags:
                        supported.add("sse")
                    if "avx" in flags:
                        supported.add("avx")
                    if "avx2" in flags:
                        supported.add("avx2")
                    if "avx512f" in flags:
                        supported.add("avx512")
                    break
    except OSError:
        pass

    # Baseline fallback for x86_64 if /proc/cpuinfo is unavailable
    if not supported and os.uname().machine == "x86_64":
        supported.update({"sse", "avx", "avx2"})

    return frozenset(supported)


def get_default_instruction_set(supported: frozenset[str] | set[str]) -> str:
    """Returns the highest supported instruction set: avx512 > avx2 > avx > sse."""
    for mode in INSTRUCTION_SET_HIERARCHY:
        if mode in supported:
            return mode
    return "sse"
