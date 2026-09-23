# Zen Tuner for Linux

[![Tests](https://github.com/drake127/zen-tuner/actions/workflows/tests.yml/badge.svg)](https://github.com/drake127/zen-tuner/actions/workflows/tests.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)

**Zen Tuner for Linux** is an advanced per-core stability testing and AMD Curve Optimizer (PBO) tuner designed for AMD Ryzen and other modern x86 multi-core processors. It cycles through physical cores one by one using configurable stress runners (Prime95/mprime, y-cruncher) with real-time `SCHED_RR` priority. SMU telemetry (voltage, power, clocks, Curve Optimizer offsets) is available on PM table layouts of Zen 1–3; stress testing itself works on any x86-64 CPU.

---

## Key Features

- **AMD Ryzen SMU PM Table Integration**: Directly interfaces with the `ryzen_smu` kernel driver (`/sys/kernel/ryzen_smu_drv/pm_table`) to read real-time per-core VDDCR voltage, power consumption (W), temperature (°C), effective clock, C-states (`C0 %`, `C6 %`), and socket PBO limits (PPT, TDC, EDC), with auto-detection of enabled physical slots on multi-CCD chips (filtering out fused-off silicon cores).
- **Split-Screen Ncurses TUI Dashboard**: Real-time terminal interface displaying cycle summary and PBO gauges at the top, live scrolling test log on the left, and per-core telemetry table (including Curve Optimizer offsets on Zen 3) on the right. Without an interactive terminal (pipe, redirect) the log is printed as plain lines instead.
- **Clock Stretching Detection**: Compares the SMU-reported core clock with the effective clock while the tested core is fully loaded (C0 ≥ 95 %). A median drop of 50 MHz or more over a run is reported as stretching. Without `ryzen_smu` the clock columns show `--`, since APERF/MPERF alone cannot tell stretching apart from a lower boost clock.
- **Pluggable Stress Runner Architecture**: Runners only describe how to configure an engine and parse its output (`StressRunner` + `OutputParser`); process supervision, stop signals, telemetry and exit classification are shared.
- **Hardware Error Logging**: Watches the kernel ring buffer (`/dev/kmsg`) for Machine Check / `[Hardware Error]` records and reports them prominently in the log and the final summary. They do not fail a core yet.
- **Real-Time Priority (`SCHED_RR`)**: Controller thread runs at `SCHED_RR` priority 50 unpinned; the stress process inherits priority 40 and the target core affinity at spawn time, before it starts any threads.

---

## Directory Structure

```text
zen-tuner/
├── zen_tuner.py                 # Top-level executable orchestrator CLI
├── lib/                          # Internal core logic and infrastructure
│   ├── __init__.py
│   ├── cpu.py                    # Instruction set detection from /proc/cpuinfo
│   ├── models.py                 # Pure dataclasses (PhysicalCore, CoreStats, RunResult, SmuSnapshot, etc.)
│   ├── smu.py                    # AMD Ryzen SMU PM Table parser & sysfs reader (Zen 1-3)
│   ├── sched.py                  # SCHED_RR / CPU affinity helpers
│   ├── topology.py               # CPU topology discovery from sysfs & core selection parser
│   ├── monitors.py               # SMU core telemetry sampling & /dev/kmsg hardware error monitor
│   ├── orchestrator.py           # ZenTunerOrchestrator: engine-agnostic cycling & statistics
│   ├── presenter.py              # Presenter base (event messages, summary) & ConsolePresenter
│   ├── views.py                  # View models shared by the dashboard and the summary table
│   ├── logbuffer.py              # Thread-safe scrollable log buffer
│   ├── tui.py                    # Ncurses full-screen split dashboard (CursesPresenter)
│   └── ui.py                     # Session log file, ANSI colours & terminal utilities
├── runners/                      # Pluggable stress runner engines
│   ├── __init__.py               # Runner registry (RUNNER_CLASSES, get_runner)
│   ├── base.py                   # StressRunner, OutputParser, process supervision, TestEventListener
│   ├── prime95.py                # Prime95 (mprime) implementation & FFT profiles
│   └── y_cruncher.py             # y-cruncher implementation & algorithm profiles
├── tools/                        # Standalone diagnostic utilities
│   ├── check_smu.py              # AMD Ryzen SMU telemetry & PBO limits inspector
│   ├── show_topology.py          # CPU topology and CCD inspector
│   └── check_stretching.py       # Live SMU clock stretching inspector
├── tests/                        # Automated pytest suite (no stress engine is run)
│   ├── conftest.py
│   ├── test_cpu.py
│   ├── test_models.py
│   ├── test_monitors.py
│   ├── test_orchestrator.py
│   ├── test_presenter.py
│   ├── test_runners.py
│   ├── test_smu.py
│   ├── test_topology.py
│   ├── test_tui.py
│   └── test_views.py
├── contrib/
│   ├── prime95/                  # Bundled Prime95 binaries (mprime)
│   └── y-cruncher/               # Bundled y-cruncher binaries & documentation
└── pytest.ini                    # Pytest test configuration
```

---

## Quick Start

### Prerequisites
- Linux kernel 5.x or newer
- Root or `sudo` privileges (required for real-time `SCHED_RR`, `/dev/kmsg`, and `ryzen_smu`)
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
- **y-cruncher**: 1 iteration corresponds to completing the full set of selected stress algorithms (e.g. `breadpit`: `BBP`, `SFTv4`, `VT3`). The duration of each algorithm step is controlled via `--yc-test-time` (default: `60s`). y-cruncher runs without a total time limit and is stopped right after the last required algorithm passes.
- **`--test-iterations N`** (default: `1`): Specifies how many complete iteration passes each core must pass before advancing to the next core. A core only passes when all iterations were verified; an engine that ends earlier is reported as `UNVERIFIED`.
- Durations accept `s`, `m` and `h` suffixes; a bare number means seconds. Prime95 steps must be whole minutes.

### Stopping a Session

Press `Ctrl+C` (or send `SIGTERM`) once to stop the running engine and print the summary; a second signal aborts immediately. Stress engines run in their own process group, so only Zen Tuner decides when and how they are stopped.

### Results

Each core run ends with one of these results:

| Result | Meaning |
| :--- | :--- |
| `PASS` | All requested iterations were verified by the engine. |
| `ERROR` | The engine reported a computation error (e.g. Prime95 rounding / hardware failure, y-cruncher failed test). |
| `CRASH` | The engine terminated abnormally (signal or non-zero exit code). |
| `UNVERIFIED` | The engine ended before verifying all requested iterations. |
| `INTERRUPTED` | Stopped by the user; not counted as a failure. |

A failed core is skipped in later cycles. The summary table marks cores with a median clock drop of 50 MHz or more as `PASS (STRETCH)` and lists all kernel hardware errors recorded during the session. The session log is written to `logs/zen_tuner_<timestamp>.log`; the engine's work directory (configuration, `results.txt`) is kept in the system temp directory for failed runs. The exit code is `0` when no core failed, `1` otherwise.

---

## Command-Line Arguments Reference

### General Options

| Parameter | Type / Choices | Default | Description |
| :--- | :--- | :--- | :--- |
| `--cores` | `str` | `all` | Physical cores to test. Accepts comma-separated lists (`0,2,4`), ranges (`0-5`), or `all`. |
| `--test-iterations` | `int` | `1` | Number of complete test iterations per core. |
| `--cycles` | `int` | `0` | Number of full cycles across selected cores (`0` for infinite continuous testing). |
| `--hyperthreading` | `off`, `on`, `cycle` | `on` | SMT execution mode: `off` (1 thread), `on` (both threads), `cycle` (alternates T0 -> T1 -> Both across cycles). |
| `--stop-on-error` | `flag` | `False` | Abort the entire test session immediately upon the first failed core (computation error, crash, unverified run). |
| `--log-dir` | `str` | `./logs` | Directory where session log files are saved. |
| `--runner` | `prime95`, `y-cruncher`, `cycle` | `prime95` | Stress test engine: `prime95`, `y-cruncher`, or `cycle` (alternates engines each cycle). |

### Prime95 (`--runner prime95`) Options

| Parameter | Type / Choices | Default | Description |
| :--- | :--- | :--- | :--- |
| `--prime-fft` | `str` | `smallest` | Prime95 FFT preset (`smallest`, `small`, `large`, `blend`) or custom range (e.g. `36-248`, `4-21`). |
| `--prime-min-fft` | `int` | `4` | Custom minimum FFT size in K; replaces `--prime-fft`. |
| `--prime-max-fft` | `int` | `32768` | Custom maximum FFT size in K; replaces `--prime-fft`. With only `--prime-min-fft`, all larger FFTs are tested. |
| `--prime-memory` | `int` | preset | Memory allocation in MB for Prime95 (`0` = in-place FFTs, keeping data in CPU cache). |
| `--prime-test-time` | `str` | `1m` | Duration per Prime95 FFT step in whole minutes (e.g. `1m`, `2m`, `180`). |
| `--prime-mode` | `sse`, `avx`, `avx2`, `avx512` | auto | Prime95 instruction set mode. Defaults to highest supported by the processor; unsupported modes are rejected. |

### y-cruncher (`--runner y-cruncher`) Options

| Parameter | Type / Choices | Default | Description |
| :--- | :--- | :--- | :--- |
| `--yc-algorithms` | `str` | `breadpit` | y-cruncher algorithm preset (`breadpit`, `fast`, `all`) or comma-separated list of `BKT`, `BBP`, `SFTv4`, `SNT`, `SVT`, `FFTv4`, `NTT63`, `N63`, `VSTv3`, `VT3`. |
| `--yc-memory` | `int` | `0` | Memory allocation in MB for y-cruncher (`0` = auto in-cache allocation). |
| `--yc-test-time` | `str` | `60s` | Duration per y-cruncher algorithm in whole seconds (e.g. `30`, `60s`, `2m`). |

### Diagnostic Tools
```bash
# Inspect Ryzen SMU PM Table telemetry, voltages, and PBO limits
sudo ./tools/check_smu.py --loop

# Inspect CPU topology, CCD mapping, and physical cores
./tools/show_topology.py

# Live SMU clock stretching telemetry of physical core 0 (requires ryzen_smu)
sudo ./tools/check_stretching.py --core 0 --duration 10
```

---

## Running Automated Tests

```bash
python3 -m pytest -v
```

The suite runs in a few seconds and never starts a stress engine: supervision tests replace the engine binary with a lightweight script that mimics its output and signal handling.

---

## Data Sources & Hardware Telemetry Acknowledgments

AMD Ryzen SMU Power Management (PM) table layouts, register offsets, and mailbox command specifications are derived from and inspired by reverse-engineering research from the open-source community:
- **[ZenStates-Core](https://github.com/irusanov/ZenStates-Core)** (GPL-3.0) by Ivan Rusanov (irusanov)
- **[ryzen_smu](https://github.com/leogx9r/ryzen_smu)** by Leonardo Gates (leogx9r)

---

## License

- **Zen Tuner**: Licensed under the **GNU General Public License v3.0 (GPL-3.0)**. See [LICENSE](LICENSE) for full details.
- **Prime95 (mprime)**: Bundled in `contrib/prime95/` as a standalone external stress test binary. Prime95 is proprietary freeware copyrighted by Mersenne Research, Inc. / George Woltman and distributed under the [GIMPS End User License Agreement](contrib/prime95/license.txt). Third-party libraries in `contrib/prime95/` (e.g. GNU MP `libgmp.so`) are licensed under LGPL-3.0 / GPL-2.0+. Zen Tuner invokes `mprime` exclusively across process boundaries via standard OS pipes (`subprocess`), constituting mere aggregation under Section 5 of GPL-3.0.
- **y-cruncher**: Bundled in `contrib/y-cruncher/` as a standalone external stress test binary. y-cruncher is copyrighted by Alexander J. Yee and distributed under the license terms in [Read Me.txt](contrib/y-cruncher/Read%20Me.txt). Zen Tuner invokes `y-cruncher` exclusively across process boundaries via a pseudo-terminal (`subprocess`), constituting mere aggregation under Section 5 of GPL-3.0.
