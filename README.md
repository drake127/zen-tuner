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
│   ├── prime95.py                # Prime95 (mprime) implementation & FFT profiles
│   └── y_cruncher.py             # y-cruncher implementation & algorithm profiles
├── tools/                        # Standalone diagnostic utilities
│   ├── check_smu.py              # AMD Ryzen SMU telemetry & PBO limits inspector
│   ├── show_topology.py          # CPU topology and CCD inspector
│   └── check_stretching.py       # Live clock stretching telemetry inspector
├── tests/                        # Automated unit test suite
│   ├── test_cpu.py
│   ├── test_models.py
│   ├── test_monitors.py
│   ├── test_orchestrator.py
│   ├── test_runners.py
│   ├── test_smu.py
│   ├── test_topology.py
│   └── test_tui.py
├── contrib/
│   ├── prime95/                  # Bundled Prime95 binaries (mprime)
│   └── y-cruncher/               # Bundled y-cruncher binaries & license
└── pytest.ini                    # Pytest test configuration
```

---

## Quick Start

### Prerequisites
- Linux kernel 5.x or newer
- Root or `sudo` privileges (required for real-time `SCHED_RR`, `/dev/kmsg`, and `/dev/cpu/*/msr`)
- Python 3.12+
- Bundled stress runners:
  - **Prime95 (mprime)** is bundled under `contrib/prime95/`.
  - **y-cruncher** is bundled under `contrib/y-cruncher/`.
- *(Optional)* `ryzen_smu` kernel module for real-time SMU telemetry (`sudo modprobe ryzen_smu`)

### Running Zen Tuner
```bash
# Default run: 1 full iteration per core using Prime95 smallest FFTs
sudo ./zen_tuner.py

# Run y-cruncher (breadpit preset: BBP -> SFTv4 -> VT3, 60s per algo) for 1 iteration per core
sudo ./zen_tuner.py --runner y-cruncher

# Alternate engines every cycle (Cycle 1: Prime95, Cycle 2: y-cruncher)
sudo ./zen_tuner.py --runner cycle --cycles 4

# Target specific cores (e.g. CCD 0: cores 0 through 5) for 2 iterations
sudo ./zen_tuner.py --cores 0-5 --test-iterations 2

# 3-phase SMT cycling across cycles: Thread 0 -> Thread 1 -> Both (T0+T1)
sudo ./zen_tuner.py --cores all --hyperthreading cycle --prime-fft 36-248
```

---

## Stress Test Engines & Iteration Concept

Zen Tuner prioritizes clean test completion over arbitrary time cutoffs. Testing duration is governed by test iterations rather than cutting computations off mid-flight:

- **Prime95**: 1 iteration corresponds to completing the configured FFT step range (e.g. 4K through 21K for `smallest`, or 36K through 248K for custom ranges). The duration of each FFT step is controlled via `--prime-test-time` (default: `1m`).
- **y-cruncher**: 1 iteration corresponds to completing the full set of selected stress algorithms (e.g. `breadpit`: `BBP`, `SFTv4`, `VT3`). The duration of each algorithm step is controlled via `--yc-test-time` (default: `60s`).
- **`--test-iterations N`** (default: `1`): Specifies how many complete iteration passes each core must pass before advancing to the next core.
- **`--time TIME`** (default: `None` / unlimited): Optional maximum duration limit (timeout) per core (e.g. `10m`, `360s`). When set, tests will cleanly finish or terminate if this upper bound is reached.

---

## Command-Line Arguments Reference

### General Options

| Parameter | Type / Choices | Default | Description |
| :--- | :--- | :--- | :--- |
| `--cores` | `str` | `all` | Physical cores to test. Accepts comma-separated lists (`0,2,4`), ranges (`0-5`), CCD prefixes, or `all`. |
| `--test-iterations` | `int` | `1` | Number of complete test iterations per core. |
| `--time` | `str` | `None` | Optional maximum duration limit per core (e.g. `360s`, `5m`, `10m`). Default is unlimited (governed by `--test-iterations`). |
| `--cycles` | `int` | `0` | Number of full cycles across selected cores (`0` for infinite continuous testing). |
| `--hyperthreading` | `off`, `on`, `cycle`, `rr` | `on` | SMT execution mode: `off` (1 thread), `on` (both threads), `cycle` (alternates T0 -> T1 -> Both across cycles). |
| `--stop-on-error` | `flag` | `False` | Abort the entire test session immediately upon encountering the first calculation error or MCE. |
| `--log-dir` | `str` | `./logs` | Directory where session log files are saved. |
| `--runner` | `prime95`, `y-cruncher`, `cycle` | `prime95` | Stress test engine: `prime95`, `y-cruncher`, or `cycle` (alternates engines each cycle). |

### Prime95 (`--runner prime95`) Options

| Parameter | Type / Choices | Default | Description |
| :--- | :--- | :--- | :--- |
| `--prime-fft` | `str` | `smallest` | Prime95 FFT preset (`smallest`, `small`, `large`, `blend`) or custom range (e.g. `36-248`, `4-21`). |
| `--prime-min-fft` | `int` | auto | Custom minimum FFT size in K (overrides preset min). |
| `--prime-max-fft` | `int` | auto | Custom maximum FFT size in K (overrides preset max). |
| `--prime-memory` | `int` | `0` | Memory allocation in MB for Prime95 (`0` = in-place FFTs, keeping data in CPU cache). |
| `--prime-test-time` | `str` | `1m` | Duration per Prime95 FFT step (e.g. `30s`, `1m`, `2m`). |
| `--prime-mode` | `sse`, `avx`, `avx2`, `avx512` | auto | Prime95 instruction set mode. Defaults to highest supported by the processor. |

### y-cruncher (`--runner y-cruncher`) Options

| Parameter | Type / Choices | Default | Description |
| :--- | :--- | :--- | :--- |
| `--yc-algorithms` | `str` | `breadpit` | y-cruncher algorithm preset (`breadpit`, `fast`, `all`) or comma-separated list (e.g. `BBP,SFTv4,VT3`). |
| `--yc-memory` | `int` | `0` | Memory allocation in MB for y-cruncher (`0` = auto in-cache allocation). |
| `--yc-test-time` | `str` | `60s` | Duration per y-cruncher test step (e.g. `30s`, `60s`, `2m`). |

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
- **y-cruncher**: Bundled in `contrib/y-cruncher/` as a standalone external stress test binary. y-cruncher is copyrighted by Alexander J. Yee and distributed under the [y-cruncher End User License Agreement](file:///home/drake127/Projects/gentoo/zen-tuner/contrib/y-cruncher/License.txt). Zen Tuner invokes `y-cruncher` exclusively across process boundaries via standard OS pipes (`subprocess`), constituting mere aggregation under Section 5 of GPL-3.0.
