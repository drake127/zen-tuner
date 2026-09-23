"""
CPU hardware capabilities and instruction set detection.
Universally discovers CPUID feature flags from /proc/cpuinfo across vendors.
"""

import os

INSTRUCTION_SET_HIERARCHY = ("avx512", "avx2", "avx", "sse")


def detect_cpu_instruction_sets(cpuinfo_path: str = "/proc/cpuinfo") -> set[str]:
    """
    Detects CPU instruction sets supported by the hardware (sse, avx, avx2, avx512).
    Inspects kernel-decoded CPUID feature flags from /proc/cpuinfo.
    """
    supported: set[str] = set()
    if os.path.exists(cpuinfo_path):
        try:
            with open(cpuinfo_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("flags") or line.startswith("Features"):
                        parts = line.split(":", 1)
                        if len(parts) == 2:
                            flags = set(parts[1].split())
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
    if not supported and os.uname().machine in ("x86_64", "AMD64"):
        supported.update({"sse", "avx", "avx2"})

    return supported


def get_default_instruction_set(supported: set[str] | None = None) -> str:
    """Returns the highest supported instruction set: avx512 > avx2 > avx > sse."""
    if supported is None:
        supported = detect_cpu_instruction_sets()
    for mode in INSTRUCTION_SET_HIERARCHY:
        if mode in supported:
            return mode
    return "sse"

