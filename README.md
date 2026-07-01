# EEGle

EEGle is a Python toolkit for reproducible realtime EEG experiments. It combines
PsychoPy tasks, Lab Streaming Layer (LSL) acquisition, Enobio/NIC2 and Neuracle
setup checks, structured session output, realtime processing, and post-session
analysis.

The source CLI is `eegle.cli:main`. Installing this project creates an
`eegle` console script in that Python environment; `eegle` is not a global or
operating-system-provided command.

## Quick Start: Complete a Two-Trial Dry Run

EEGle currently requires Python 3.10. Start from the repository root:

macOS or Linux:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[runtime]"
```

Windows PowerShell (Windows x64):

```powershell
py -3.10 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[runtime]"
```

If PowerShell blocks the activation script, allow it only for the current
PowerShell window, then activate the environment again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
```

Activation is convenient but not required. This is the activation-free Windows
equivalent, which is also useful for an operator runbook or a locked-down
machine:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[runtime]"
.\.venv\Scripts\eegle.exe check-setup --allow-missing-eeg
```

After activating the environment, verify that the console script was actually
created before using the shorter `eegle ...` examples below:

macOS or Linux:

```bash
command -v eegle
eegle --help
```

Windows PowerShell:

```powershell
Get-Command eegle
eegle --help
```

If that verification fails, the project has not been installed into the active
environment (or that environment is not activated). From the repository root,
the source-level equivalent is always explicit:

```bash
python -m eegle.cli --help
python -m eegle.cli check-setup --allow-missing-eeg
```

This module form still needs the project dependencies installed in the Python
environment, but it does not depend on the `eegle` console script being on
`PATH`.

The supplied constraints snapshot was captured on macOS. Use
`constraints/macos-python310.txt` only when recreating that exact macOS
environment; the exact pins in `pyproject.toml` are the cross-platform source of
truth.

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

Windows PowerShell (after activation) uses the same installed `eegle` command.
The one-line form avoids fragile PowerShell line-continuation characters:

```powershell
eegle run-task --config configs\default_experiment.json --task pvt --mode dry-run --trials 2 --participant demo
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
snapshot environment. Do not treat the macOS constraints file as a Windows or
Linux lockfile.

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
visible LSL streams, configured EEG stream match, realtime readiness, display
readiness, training dependencies, and whether EEG samples can be read.
It replaces the less descriptive `doctor` command; `eegle doctor` remains as a
compatibility alias.

Commands such as `run-forward` are CLI subcommands, not standalone shell
executables. `eegle run-forward ...` works only after the package installation
has created `eegle` in the active environment. The checkout-based equivalent is
`python -m eegle.cli run-forward ...`, which calls the same `main()`
function directly. Names such as `forward-go-nogo-8` are Makefile targets for
POSIX-like development shells, not the Windows-native operator path.

| Installed command | Source-module equivalent | Purpose |
| --- | --- | --- |
| `eegle check-setup` | `python -m eegle.cli check-setup` | Validate software, LSL discovery, and configured hardware |
| `eegle list-tasks` | `python -m eegle.cli list-tasks` | Show available experiment tasks |
| `eegle model-list` | `python -m eegle.cli model-list` | Show registered classical, CNN, and EEG foundation model kinds |
| `eegle run-task` | `python -m eegle.cli run-task` | Run only a task, usually for development or display testing |
| `eegle run-forward` | `python -m eegle.cli run-forward` | Run setup checks, task, optional EEG recording, and analysis |
| `eegle simulate-eeg` | `python -m eegle.cli simulate-eeg` | Start a development-only simulated EEG LSL outlet |
| `eegle analyze` | `python -m eegle.cli analyze` | Generate a post-session summary |
| `eegle report-html` | `python -m eegle.cli report-html` | Generate an interactive HTML session report |
| `eegle replay-realtime` | `python -m eegle.cli replay-realtime` | Replay captured realtime inputs and validate features |
| `eegle extract-epochs` | `python -m eegle.cli extract-epochs` | Extract marker-locked EEG epochs |
| `eegle train-model` | `python -m eegle.cli train-model` | Train a registered epoch classifier |
| `eegle evaluate-model` | `python -m eegle.cli evaluate-model` | Score classifier predictions against the stimulus manifest |
| `eegle replay-classifier` | `python -m eegle.cli replay-classifier` | Replay classifier predictions from captured EEG and markers |

The `alpha8`, `inhibition8`, and `classify8` installed scripts have equivalent
source-module forms. They run the posterior-alpha, response-inhibition, and
participant-specific GO/NO-GO classification pipelines respectively:

```bash
alpha8 --help
inhibition8 --help
classify8 --help
python -m eegle.pipelines.alpha8 --help
python -m eegle.pipelines.inhibition8 --help
python -m eegle.pipelines.classify8 --help
```

### Windows PowerShell Command Forms

After activating `.venv`, the installed `eegle`, `alpha8`, `inhibition8`, and
`classify8` commands work directly in PowerShell. Do not use repository-root
`./alpha8`-style shell wrappers or Makefile targets on Windows; they require a
POSIX-like shell. Use these equivalents instead:

| POSIX-oriented form | Windows PowerShell form |
| --- | --- |
| `source .venv/bin/activate` | `.venv\Scripts\Activate.ps1` |
| `python3.10 -m eegle.cli ...` | `py -3.10 -m eegle.cli ...` |
| `./alpha8 full` | `alpha8 full` |
| `./inhibition8 full` | `inhibition8 full` |
| `./classify8 collect ...` | `classify8 collect ...` |
| `make forward-dry-run` | `eegle run-forward --config configs\default_experiment.json --task pvt --task-mode dry-run --skip-eeg --allow-missing-eeg` |

If activation is unavailable, prefix the console executable with
`.\.venv\Scripts\`, for example
`.\.venv\Scripts\classify8.exe --help`. Use `py -3.10 -m ...` when invoking a
package module directly; it guarantees that the requested Python 3.10 runtime
is used. The activation-free generic CLI form is
`.\.venv\Scripts\python.exe -m eegle.cli check-setup --allow-missing-eeg`.

## Documentation Guide

- `docs/ARCHITECTURE.md` describes the runtime data path and component
  boundaries.
- `docs/MODEL_TRAINING_TESTING_GOALS.md` describes classifier training,
  testing, evaluation, and current model goals.
- `AGENTS.md` and scoped `AGENTS.md` files under implementation directories
  orient AI agents and future contributors before code changes.

Run a 100-trial Go/No-go experiment without the alpha calibration pipeline:

```bash
eegle run-forward \
  --config configs/forward_go_nogo_enobio8.json \
  --task go_nogo \
  --task-mode psychopy \
  --trials 100 \
  --participant sub-001 \
  --require-eeg
```

Run the complete posterior-alpha calibration plus 100-trial Go/No-go pipeline:

```bash
alpha8 full --participant sub-001
```

Windows PowerShell forms (after activation):

```powershell
eegle run-forward --config configs\forward_go_nogo_enobio8.json --task go_nogo --task-mode psychopy --trials 100 --participant sub-001 --require-eeg
alpha8 full --participant sub-001
```

## Configuration Guide

Select a JSON config that matches the task, device, channel montage, and desired
analysis:

| Config | Use |
| --- | --- |
| `configs/default_experiment.json` | Software development and dry runs |
| `configs/forward_pvt_enobio.json` | PVT with an 8, 22, or 32-channel Enobio stream |
| `configs/forward_pvt_enobio8.json` | PVT with an 8-channel, 500 Hz Enobio stream |
| `configs/forward_pvt_neuracle64.json` | PVT with a 64-channel, 1000 Hz Neuracle LSL stream |
| `configs/forward_go_nogo_enobio8.json` | Go/No-go with the posterior-alpha 8-channel montage |
| `configs/forward_go_nogo_inhibition8.json` | Observe-only Go/No-go with the inhibition montage |
| `configs/forward_go_nogo_classifier8.json` | Capture and observe-only GO/NO-GO EEG condition classification |

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

The Windows PowerShell software-only verification is:

```powershell
eegle check-setup --allow-missing-eeg
eegle run-forward --config configs\default_experiment.json --task pvt --task-mode dry-run --trials 2 --participant demo --skip-eeg --allow-missing-eeg
alpha8 full --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2
inhibition8 full --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2
```

The Makefile and repository-root `./alpha8`, `./inhibition8`, and `./classify8`
wrappers are optional POSIX conveniences. Installed console commands are
preferred because they work outside the repository root and on Windows.

## Cross-Platform Support Model

EEGle is intended to run from one shared Python codebase on macOS, Windows, and
Linux. The places that need OS-specific handling are setup and operator
environment details: virtual-environment activation syntax, optional POSIX
wrappers, PsychoPy display validation, external NIC2 installation, LSL/firewall
settings, Neuracle acquisition setup, and future LabRecorder launching.
Preflight reports the current OS as `os_support`, installed command visibility
as `commands`, PsychoPy readiness as `display_ready`, realtime worker readiness
as `realtime_ready`, model-training dependency readiness as `training_ready`,
and the configured EEG device family as `eeg_device` when matching LSL streams
are visible.

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

   Do not begin the experiment until `eeg_device` and `eeg_sample_probe` report
   `OK`; current Enobio configs also emit the legacy `enobio_lsl` detail check.
   If discovery fails, verify that NIC2 is actively acquiring, its LSL outlet is
   enabled, and the local firewall allows LSL traffic.

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

## Example Neuracle 64-Channel LSL Setup Check

This first-pass Neuracle preset runs the same preflight, `lsl_csv` recorder,
PVT task, and session-output path as the Enobio presets. EEGle does not install
or launch the Neuracle acquisition software.

The shipped preset expects the acquisition computer to publish:

```text
type: EEG
channel count: 64
sample rate: 1000 Hz
name or source id containing: neuracle
```

If the lab computer is configured for a different Neuracle channel count or
sample rate, use `configs/forward_pvt_neuracle64.json` as the starting preset
and adjust only the `hardware.eeg` expectations before collecting data.

Validate stream discovery and sample delivery:

```bash
eegle check-setup \
  --config configs/forward_pvt_neuracle64.json \
  --require-eeg \
  --lsl-wait 5
```

Continue only when `eeg_device`, `neuracle_lsl`, and `eeg_sample_probe` report
`OK`. If discovery fails, verify that Neuracle acquisition is actively
streaming, its LSL outlet is enabled, and the local firewall allows LSL traffic.

Run the PVT experiment:

```bash
eegle run-forward \
  --config configs/forward_pvt_neuracle64.json \
  --task pvt \
  --task-mode psychopy \
  --participant sub-001 \
  --require-eeg
```

## Windows PowerShell Setup and NIC2 Enobio 8 Run

Use 64-bit CPython 3.10 and PowerShell for the supported Windows x64 operator
path. The Python package and command layer are shared with macOS and Linux;
NIC2, the Enobio driver connection, Windows Firewall permissions, and
PsychoPy's display setup remain machine-specific checks. Windows on ARM is not
validated; confirm compatible LSL and device-driver binaries before using it.

1. Create and install the runtime environment:

   ```powershell
   py -3.10 -m venv .venv
   .venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   python -m pip install -e ".[runtime]"
   ```

   Do not use `constraints/macos-python310.txt` on Windows. It is a captured
   macOS environment, not a cross-platform lockfile.

2. Connect the Enobio device, open NIC2, start acquisition, and enable NIC2's
   LSL EEG outlet. Allow NIC2 and Python through Windows Firewall if LSL stream
   discovery is blocked.

3. Confirm that NIC2 publishes an EEG stream with eight channels at 500 Hz and
   a name containing `enobio`, `nic`, `neuroelectrics`, or
   `LSLOutletStreamName-EEG`.

4. Validate the actual stream before collecting a session:

   ```powershell
   eegle check-setup --config configs\forward_pvt_enobio8.json --require-eeg --lsl-wait 5
   ```

   Continue only when `eeg_device` and `eeg_sample_probe` report `OK`. If they
   do not, resolve NIC2 acquisition, its LSL outlet, and firewall visibility
   before retrying.

5. Run the PVT experiment:

   ```powershell
   eegle run-forward --config configs\forward_pvt_enobio8.json --task pvt --task-mode psychopy --participant sub-001 --require-eeg
   ```

6. Verify the printed session path contains `raw\eeg.csv`,
   `raw\eeg_metadata.json`, `events\behavior.csv`, `events\events.jsonl`,
   `triggers.txt`, and worker status files before treating the run as usable
   data.

## GO/NO-GO EEG Classification

`classify8` implements a participant-specific calibration, frozen-model, and
online-test workflow. It decodes the displayed GO versus NO-GO condition from
the post-stimulus EEG epoch; it is not an inhibition-decoding claim.

```bash
classify8 collect --participant sub-001 --trials 240
classify8 train --session-dir <calibration-session> --check-ready
classify8 train --session-dir <calibration-session>
classify8 train --session-dir <run-a> --session-dir <run-b> --target attention_lapse_binary
classify8 online --participant sub-001 --model-dir <calibration-session>/models/classifier \
  --primary erp_roi_logreg --shadow pyriemann_erp_cov --shadow torch_eegnet --trials 160
classify8 demo --participant classroom-demo --trials 40
classify8 evaluate --session-dir <online-session>
```

`--model-dir` must point to the parent directory containing each requested
model bundle, including its `manifest.json`. For example:

```bash
MODEL_DIR="$PWD/models/pilot_001/classifier_merged_480"
test -f "$MODEL_DIR/erp_roi_logreg/manifest.json"
test -f "$MODEL_DIR/pyriemann_erp_cov/manifest.json"
```

Windows PowerShell version:

```powershell
$ModelDir = Join-Path $PWD "models\pilot_001\classifier_merged_480"
Test-Path "$ModelDir\erp_roi_logreg\manifest.json"
Test-Path "$ModelDir\pyriemann_erp_cov\manifest.json"
classify8 online --participant sub-001 --model-dir $ModelDir --primary erp_roi_logreg --shadow pyriemann_erp_cov --shadow torch_eegnet --trials 160
```

The online worker runs one primary model plus optional shadow models on each
accepted marker-locked epoch. Model input metadata is label-blind, predictions
are observe-only, and canonical truth is joined from
`events/stimulus_manifest.json` only for display and scoring. Predictions are
written to `realtime/model_predictions.jsonl`, frozen bundles are copied into
the online session, and reports are written under `reports/classification/`.

Model kinds are registry-backed. `eegle model-list` shows classical models,
CNNs such as `torch_eegnet` / `cnn_eegnet`, and external EEG foundation-model
adapter targets such as `foundation_bendr`, `foundation_labram`, and
`sequence_external`. Foundation and sequence entries require user-supplied
checkpoints; EEGle records artifact hashes but does not download checkpoints.

Training targets can be `condition`, `attention_lapse_binary`, or
`attention_lapse_score`. Attention-lapse targets are derived from task behavior
first: slow correct GO reaction times, omissions, commission errors, and a
trailing lapse score. Multi-session lapse training joins behavior by
source-session index plus trial number, and refuses ambiguous trial-only joins.
`attention_lapse_score` is currently a score-derived binary target, not
regression learning. Stimulation-moment actions are disabled by default and
require the `attention_lapse_stimulation` policy plus explicit
`allow_stimulation` and `research_safety_ack` config gates.

Training summaries, live dashboard metrics, and offline classifier reports use
the calibrated operating threshold when a bundle or prediction row provides one;
the default `0.5` metrics are kept separately for comparison.

The optional dashboard binds only to `127.0.0.1`; it does not auto-open a
browser or interact with PsychoPy. Training requires the `ml` extra for ROI
logistic regression and pyRiemann, and the `deep-learning` extra for EEGNet.
See `docs/MODEL_TRAINING_TESTING_GOALS.md` for the current model contracts,
evaluation checks, and near-term model goals.

`classify8 demo` runs a classroom-friendly dashboard at
`http://127.0.0.1:8765`. It connects directly to the run-unique PsychoPy LSL
marker stream, waits 1.2 seconds, then shows the marked stimulus as a simulated
guess with a configurable error rate. It does not claim to decode EEG and does
not write to the real classifier prediction artifact:

```bash
classify8 demo --prediction-delay-seconds 1.5 --error-rate 0.1
classify8 demo --record-eeg
```

Demo guesses and illustrative ERP windows are written separately to
`realtime/demo_predictions.jsonl`.

## Current Scope

EEGle currently supports PVT and Go/No-go execution, Enobio/NIC2 and Neuracle
LSL checks, CSV recording, marker and telemetry logs, posterior-alpha calibration,
realtime/replay scaffolding, ERP analysis, and HTML reports. N-back, Sternberg,
and anti-VEA remain registered future-task scaffolds.
