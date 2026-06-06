# EEGle

EEGle is a Python toolkit for reproducible realtime EEG experiments. It combines
PsychoPy tasks, Lab Streaming Layer (LSL) acquisition, Enobio/NIC2 setup checks,
structured session output, realtime processing, and post-session analysis.

The primary installed command and package name are both `eegle`.

## Quick Start: Complete a Two-Trial Dry Run

EEGle currently requires Python 3.10. Start from the repository root:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[runtime]" -c constraints/macos-python310.txt
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`. The supplied
constraints snapshot was captured on macOS; the exact pins in `pyproject.toml`
are the cross-platform source of truth.

Check the software setup without requiring an EEG device:

```bash
eegle check-setup --allow-missing-eeg
eegle list-tasks
```

Then complete a two-trial software-only experiment:

```bash
eegle run-task \
  --config configs/default_experiment.json \
  --task pvt \
  --mode dry-run \
  --trials 2 \
  --participant demo
```

A successful run prints its session path and writes output under:

```text
data/participants/demo/sessions/<date>/<experiment-id>/pvt/run-<timestamp>/
```

`data/` is ignored by Git because sessions may contain large or sensitive EEG
recordings.

## Installation Options

The base package includes LSL acquisition and analysis dependencies:

```bash
python -m pip install -e .
```

Install the extra needed for PsychoPy experiments and spectral parameterization:

```bash
python -m pip install -e ".[runtime]"
```

Optional model stacks are separate:

```bash
python -m pip install -e ".[ml]"
python -m pip install -e ".[deep-learning]"
```

The direct dependencies are pinned in `pyproject.toml`.
`constraints/macos-python310.txt` records the direct package versions from the
current macOS/Python 3.10 development environment. The optional ML and deep
learning pins are reproducibility targets but were not installed in that
snapshot environment.

## Key Dependencies and Imports

| Purpose | Package | Common import |
| --- | --- | --- |
| Numerical arrays | NumPy `2.2.6` | `import numpy as np` |
| Scientific signal processing | SciPy `1.14.1` | `from scipy import signal` |
| Tables and CSV output | pandas `2.3.3` | `import pandas as pd` |
| Plotting | Matplotlib `3.10.9` | `import matplotlib.pyplot as plt` |
| EEG analysis | MNE `1.12.1` | `import mne` |
| Lab Streaming Layer | pylsl `1.18.2` | `import pylsl` |
| Experiment display | PsychoPy `2026.1.3` | `from psychopy import visual` |
| Spectral parameterization | specparam `2.0.0rc6` | `import specparam` |
| Classical ML, optional | scikit-learn, joblib, pyRiemann | `import sklearn` |
| Deep learning, optional | PyTorch, ONNX Runtime | `import torch`, `import onnxruntime` |

## Command Guide

`eegle check-setup` checks the Python runtime, required and optional packages,
visible LSL streams, configured Enobio/NIC2 stream match, and whether samples
can be read. It replaces the less descriptive `doctor` command; `eegle doctor`
remains as a compatibility alias.

| Command | Purpose |
| --- | --- |
| `eegle check-setup` | Validate software, LSL discovery, and configured hardware |
| `eegle list-tasks` | Show available experiment tasks |
| `eegle run-task` | Run only a task, usually for development or display testing |
| `eegle run-forward` | Run setup checks, task, optional EEG recording, and analysis |
| `eegle simulate-eeg` | Start a development-only simulated EEG LSL outlet |
| `eegle analyze` | Generate a post-session summary |
| `eegle report-html` | Generate an interactive HTML session report |
| `eegle replay-realtime` | Replay captured realtime inputs and validate features |
| `eegle extract-epochs` | Extract marker-locked EEG epochs |
| `eegle train-model` | Train a supported classical epoch classifier |

The standalone `alpha8` command runs the posterior-alpha Enobio8 Go/No-go
pipeline. `inhibition8` runs the separate observe-only response-inhibition
pipeline. Both provide `full` and `reanalyze` subcommands:

```bash
alpha8 --help
inhibition8 --help
```

## Configuration Guide

Select a JSON config that matches the task, device, channel montage, and desired
analysis:

| Config | Use |
| --- | --- |
| `configs/default_experiment.json` | Software development and dry runs |
| `configs/forward_pvt_enobio.json` | PVT with an 8, 22, or 32-channel Enobio stream |
| `configs/forward_pvt_enobio8.json` | PVT with an 8-channel, 500 Hz Enobio stream |
| `configs/forward_go_nogo_enobio8.json` | Go/No-go with the posterior-alpha 8-channel montage |
| `configs/forward_go_nogo_inhibition8.json` | Observe-only Go/No-go with the inhibition montage |

Hardware expectations live under `hardware.eeg`. Before collecting data, check
the configured channel count, sample rate, LSL stream type/name patterns, and
montage. Task and calibration durations also live in the selected config.

## Development Runs

Run the full forward orchestration without an EEG device:

```bash
eegle run-forward \
  --config configs/default_experiment.json \
  --task pvt \
  --task-mode dry-run \
  --trials 2 \
  --participant demo \
  --skip-eeg \
  --allow-missing-eeg
```

Smoke-test the specialized pipelines:

```bash
alpha8 full --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2
inhibition8 full --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2
```

The Makefile and repository-root `./alpha8` and `./inhibition8` wrappers are
optional POSIX conveniences. Installed console commands are preferred because
they work outside the repository root.

## Session Output and Data Hygiene

Sessions use named participant and experiment folders:

```text
data/participants/<participant-id>/sessions/<date>/<experiment-id>/<task>/run-<timestamp>/
  parameters.json
  manifest.json
  session_summary.json
  triggers.txt
  raw/
    eeg.csv
    eeg_metadata.json
  events/
    behavior.csv
    events.jsonl
  logs/
    telemetry.jsonl
    debug.jsonl
    preflight.json
    processes/
  realtime/
  reports/
```

`events/events.jsonl`, `events/behavior.csv`, and `triggers.txt` are the
canonical task timing records. Realtime telemetry and derived reports are
written beside them, not in their place.

Generated sessions, runtime caches, models, artifacts, virtual environments,
build output, logs, and operating-system metadata are excluded by `.gitignore`.
Session manifests may still contain participant identifiers and local paths, so
inspect any data before publishing it.

## Logging

Console output is intentionally sparse. Use these opt-in controls when
debugging:

```bash
eegle run-forward ... --log-level realtime
eegle run-forward ... --log-level debug --trace
eegle run-forward ... --quiet
```

Structured history is written to `logs/telemetry.jsonl`; debug spans are written
to `logs/debug.jsonl`.

## Example run with NIC2 Enobio 8 channel on macOS

This example runs the PVT task with an Enobio 8-channel device and NIC2 on
macOS. NIC2 itself is external software and is not installed by EEGle.

1. Install Python 3.10 and EEGle:

   ```bash
   python3.10 -m venv .venv
   source .venv/bin/activate
   python -m pip install --upgrade pip
   python -m pip install -e ".[runtime]" -c constraints/macos-python310.txt
   ```

2. Connect the Enobio device, open NIC2, connect to the device, and begin
   acquisition. Enable NIC2's LSL EEG outlet. The exact NIC2 menu labels may
   differ by version.

3. Confirm that NIC2 publishes a stream with:

   ```text
   type: EEG
   channel count: 8
   sample rate: 500 Hz
   name containing: enobio, nic, neuroelectrics, or LSLOutletStreamName-EEG
   ```

4. Validate stream discovery and sample delivery:

   ```bash
   eegle check-setup \
     --config configs/forward_pvt_enobio8.json \
     --require-eeg \
     --lsl-wait 5
   ```

   Do not begin the experiment until `enobio_lsl` and `eeg_sample_probe` report
   `OK`. If discovery fails, verify that NIC2 is actively acquiring, its LSL
   outlet is enabled, and macOS/network firewall settings allow LSL traffic.

5. Run the PVT experiment:

   ```bash
   eegle run-forward \
     --config configs/forward_pvt_enobio8.json \
     --task pvt \
     --task-mode psychopy \
     --participant sub-001 \
     --require-eeg
   ```

6. Inspect the printed session path. Confirm that `raw/eeg.csv`,
   `raw/eeg_metadata.json`, `events/behavior.csv`, `events/events.jsonl`,
   `triggers.txt`, and process status files were written before treating the run
   as usable data.

For the posterior-alpha Go/No-go experiment, use
`configs/forward_go_nogo_enobio8.json`. Its expected montage is:

```text
P3, P4, PO3, PO4, Pz, O1, O2, Oz
```

For the observe-only inhibition pipeline, use
`configs/forward_go_nogo_inhibition8.json`. Its expected montage is:

```text
Fz, Cz, Pz, C3, C4, P3, P4, Oz
```

LabRecorder/XDF launch is not implemented. EEGle currently records the matched
LSL EEG stream to its own session output.

## Current Scope

EEGle currently supports PVT and Go/No-go execution, Enobio/NIC2 LSL checks,
CSV recording, marker and telemetry logs, posterior-alpha calibration,
realtime/replay scaffolding, ERP analysis, and HTML reports. N-back, Sternberg,
and anti-VEA remain registered future-task scaffolds.
