"""
Unit tests for CPU instruction set detection and default hierarchy resolution.
"""

import tempfile
import unittest

from lib.cpu import detect_cpu_instruction_sets, get_default_instruction_set


class TestCpu(unittest.TestCase):

    def test_detect_from_mock_cpuinfo(self):
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as f:
            f.write("processor\t: 0\nflags\t\t: fpu vme sse sse2 avx avx2\n")
            f.flush()
            supported = detect_cpu_instruction_sets(f.name)
            self.assertEqual(supported, {"sse", "avx", "avx2"})
            self.assertEqual(get_default_instruction_set(supported), "avx2")

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as f:
            f.write("processor\t: 0\nflags\t\t: fpu vme sse sse2 avx avx2 avx512f\n")
            f.flush()
            supported = detect_cpu_instruction_sets(f.name)
            self.assertEqual(supported, {"sse", "avx", "avx2", "avx512"})
            self.assertEqual(get_default_instruction_set(supported), "avx512")

        with tempfile.NamedTemporaryFile("w+", encoding="utf-8") as f:
            f.write("processor\t: 0\nflags\t\t: fpu vme sse sse2\n")
            f.flush()
            supported = detect_cpu_instruction_sets(f.name)
            self.assertEqual(supported, {"sse"})
            self.assertEqual(get_default_instruction_set(supported), "sse")

    def test_default_instruction_set_hierarchy(self):
        self.assertEqual(get_default_instruction_set({"sse"}), "sse")
        self.assertEqual(get_default_instruction_set({"sse", "avx"}), "avx")
        self.assertEqual(get_default_instruction_set({"sse", "avx", "avx2"}), "avx2")
        self.assertEqual(get_default_instruction_set({"sse", "avx", "avx2", "avx512"}), "avx512")


if __name__ == "__main__":
    unittest.main()

