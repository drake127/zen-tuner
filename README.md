# Zen Tuner for Linux

[![Tests](https://github.com/drake127/zen-tuner/actions/workflows/tests.yml/badge.svg)](https://github.com/drake127/zen-tuner/actions/workflows/tests.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

**Zen Tuner for Linux** is an advanced per-core stability testing and AMD Curve Optimizer (PBO) tuner designed for AMD Ryzen (Zen 3 / Zen 4 / Zen 5) and Intel modern multi-core processors. It cycles through physical cores one by one using configurable stress runners (Prime95/mprime, and future engines like stress-ng) with real-time `SCHED_RR` priority.

---

## Key Features

- **AMD Ryzen SMU PM Table Integration**: Directly interfaces with the `ryzen_smu` kernel driver (`/sys/kernel/ryzen_smu_drv/pm_table`) to read real-time per-core VDDCR voltage, power consumption (W), temperature (°C), effective clock, C-states (`C0 %`, `C6 %`), and socket PBO limits (PPT, TDC, EDC), with auto-detection of enabled physical slots on multi-CCD chips (filtering out fused-off silicon cores).
- **Split-Screen Ncurses TUI Dashboard**: Real-time terminal interface displaying cycle summary and PBO gauges at the top, live scrolling test log on the left, and per-core telemetry table on the right.
- **Hardware Telemetry & Clock Stretching Detection**: Direct MSR polling (`APERF` 0xE8, `MPERF` 0xE7, `TSC` 0x10) to detect Ryzen clock stretching in real time whenever actual executed clock falls below target clock.
- **Pluggable Stress Runner Architecture**: Abstract `StressRunner` interface decouples stress engines from orchestration, logging, and presentation.
- **Active vs. Idle Core Crash Isolation**: Monitors kernel ring buffer (`/dev/kmsg`) to distinguish active core calculation errors from transient idle core Machine Check Exceptions (MCEs).
- **Real-Time Priority (`SCHED_RR`)**: Controller process runs at `SCHED_RR` priority 50 unpinned; child worker runs at priority 40 pinned to target core.

---

## Directory Structure

```text
zen-tuner/
├── zen_tuner.py                 # Top-level executable orchestrator CLI
├── lib/                          # Internal core logic and infrastructure
│   ├── __init__.py
│   ├── models.py                 # Pure dataclasses (PhysicalCore, CoreStats, SmuSnapshot, etc.)
│   ├── smu.py                    # AMD Ryzen SMU PM Table parser & sysfs reader (Zen 3)
│   ├── topology.py               # CPU topology discovery from sysfs & core selection parser
│   ├── monitors.py               # Hardware MSR APERF/MPERF & /dev/kmsg monitors
│   ├── tui.py                    # Ncurses full-screen split dashboard (CursesPresenter)
│   ├── ui.py                     # Logger, ANSI formatting & terminal utilities
│   └── orchestrator.py           # ZenTunerOrchestrator: engine-agnostic cycling & state
├── runners/                      # Pluggable stress runner engines
│   ├── __init__.py               # Runner registry (get_runner, list_runners)
│   ├── base.py                   # StressRunner ABC, TestRequest, TestEventListener
│   └── prime95.py                # Prime95 (mprime) implementation & FFT profiles
├── tools/                        # Standalone diagnostic utilities
│   ├── check_smu.py              # AMD Ryzen SMU telemetry & PBO limits inspector
│   ├── show_topology.py          # CPU topology and CCD inspector
│   └── check_stretching.py       # Live clock stretching telemetry inspector
├── tests/                        # Automated unit test suite
│   ├── test_models.py
│   ├── test_monitors.py
│   ├── test_orchestrator.py
│   ├── test_runners.py
│   ├── test_smu.py
│   ├── test_topology.py
├── contrib/
│   └── prime95/                  # Bundled Prime95 binaries (mprime)
└── pytest.ini                    # Pytest test configuration
```

---

## Quick Start

### Prerequisites
- Linux kernel 5.x or newer
- Root or `sudo` privileges (required for real-time `SCHED_RR`, `/dev/kmsg`, and `/dev/cpu/*/msr`)
- Python 3.12+
- Stress runner: **Prime95 (mprime)** is bundled under `contrib/prime95/`. Alternatively, any system-installed `mprime` in `$PATH` or custom path via `--mprime <path>` is supported.
- *(Optional)* `ryzen_smu` kernel module for real-time SMU telemetry (`sudo modprobe ryzen_smu`)

### Running Zen Tuner
```bash
# Default interactive run with ncurses dashboard
sudo ./zen_tuner.py --time 5m --fft smallest --hyperthreading on

# Target specific cores (e.g. CCD 0: cores 0 through 5)
sudo ./zen_tuner.py --cores 0-5 --time 300s --cycles 2

# 3-phase SMT cycling across cycles: Thread 0 -> Thread 1 -> Both (T0+T1)
sudo ./zen_tuner.py --cores all --hyperthreading cycle --fft smallest
```

### Diagnostic Tools
```bash
# Inspect Ryzen SMU PM Table telemetry, voltages, and PBO limits
sudo ./tools/check_smu.py --loop

# Inspect CPU topology, CCD mapping, and physical cores
./tools/show_topology.py

# Live telemetry of clock stretching on CPU 0
sudo ./tools/check_stretching.py --cpus 0 --duration 10
```

---

## Running Automated Tests

```bash
python3 -m pytest -v
```

---

## Data Sources & Hardware Telemetry Acknowledgments

AMD Ryzen SMU Power Management (PM) table layouts, register offsets, and mailbox command specifications are derived from and inspired by reverse-engineering research from the open-source community:
- **[ZenStates-Core](https://github.com/irusanov/ZenStates-Core)** (GPL-3.0) by Ivan Rusanov (irusanov)
- **[ryzen_smu](https://github.com/leogx9r/ryzen_smu)** by Leonardo Gates (leogx9r)

---

## License

- **Zen Tuner**: Licensed under the **GNU General Public License v3.0 (GPL-3.0)**. See [LICENSE](file:///home/drake127/Projects/gentoo/zen-tuner/LICENSE) for full details.
- **Prime95 (mprime)**: Bundled in `contrib/prime95/` as a standalone external stress test binary. Prime95 is proprietary freeware copyrighted by Mersenne Research, Inc. / George Woltman and distributed under the [GIMPS End User License Agreement](file:///home/drake127/Projects/gentoo/zen-tuner/contrib/prime95/license.txt). Third-party libraries in `contrib/prime95/` (e.g. GNU MP `libgmp.so`) are licensed under LGPL-3.0 / GPL-2.0+. Zen Tuner invokes `mprime` exclusively across process boundaries via standard OS pipes (`subprocess`), constituting mere aggregation under Section 5 of GPL-3.0.
