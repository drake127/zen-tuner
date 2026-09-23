"""
Unit tests for CPU instruction set detection and default hierarchy resolution.
"""

import pytest

from lib.cpu import detect_cpu_instruction_sets, get_default_instruction_set


@pytest.mark.parametrize(
    "flags, expected, default",
    [
        ("fpu vme sse sse2 avx avx2", {"sse", "avx", "avx2"}, "avx2"),
        ("fpu vme sse sse2 avx avx2 avx512f", {"sse", "avx", "avx2", "avx512"}, "avx512"),
        ("fpu vme sse sse2", {"sse"}, "sse"),
    ],
)
def test_detect_from_mock_cpuinfo(tmp_path, flags, expected, default):
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(f"processor\t: 0\nflags\t\t: {flags}\n")
    supported = detect_cpu_instruction_sets(str(cpuinfo))
    assert supported == expected
    assert get_default_instruction_set(supported) == default


def test_detection_is_cached():
    assert detect_cpu_instruction_sets() is detect_cpu_instruction_sets()


@pytest.mark.parametrize(
    "supported, expected",
    [
        ({"sse"}, "sse"),
        ({"sse", "avx"}, "avx"),
        ({"sse", "avx", "avx2"}, "avx2"),
        ({"sse", "avx", "avx2", "avx512"}, "avx512"),
    ],
)
def test_default_instruction_set_hierarchy(supported, expected):
    assert get_default_instruction_set(supported) == expected
