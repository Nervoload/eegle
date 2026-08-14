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
environment. The compatible dependency ranges in `pyproject.toml` are the
cross-platform packaging source of truth.

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

The base package is intentionally lean. It installs importable session,
contract, model-bundle, calibration, protocol, and lightweight replay
primitives:

```bash
python -m pip install -e .
```

Install extras for the surfaces you use:

```bash
python -m pip install -e ".[runtime]"
python -m pip install -e ".[analysis]"
python -m pip install -e ".[ml]"
python -m pip install -e ".[torch]"
```

The `deep-learning` extra remains as a compatibility alias for the Torch/ONNX
stack:

```bash
python -m pip install -e ".[deep-learning]"
```

For local development and CI-style tests:

```bash
python -m pip install -e ".[dev]"
```

`pyproject.toml` uses compatible package ranges for pip distribution.
`constraints/macos-python310.txt` records the direct package versions from the
current macOS/Python 3.10 development environment. Use that constraints file
only when deliberately reproducing the captured macOS lab environment; do not
treat it as a Windows or Linux lockfile.

## Key Dependencies and Imports

| Purpose | Package | Common import |
| --- | --- | --- |
| Core arrays | NumPy | `import numpy as np` |
| Scientific signal processing, optional | SciPy | `from scipy import signal` |
| Tables and CSV output, optional | pandas | `import pandas as pd` |
| Plotting, optional | Matplotlib | `import matplotlib.pyplot as plt` |
| EEG analysis, optional | MNE | `import mne` |
| Lab Streaming Layer, optional | pylsl | `import pylsl` |
| Experiment display, optional | PsychoPy | `from psychopy import visual` |
| Spectral parameterization, optional | specparam | `import specparam` |
| Classical ML, optional | scikit-learn, joblib, pyRiemann | `import sklearn` |
| Deep learning, optional | PyTorch, ONNX Runtime | `import torch`, `import onnxruntime` |

Public import surfaces are documented in `docs/api/PUBLIC_API.md`. The old
module paths remain available during the `0.1.x` compatibility window.

## Command Guide

`eegle check-setup` checks the Python runtime, core and optional packages,
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

The `alpha8`, `inhibition8`, `classify8`, `attention8`, `dsart8`, `dsart32`, and `study1` installed scripts
have equivalent source-module forms. They run the posterior-alpha,
response-inhibition, participant-specific GO/NO-GO classification, and
attention-lapse system-test pipelines respectively:

```bash
alpha8 --help
inhibition8 --help
classify8 --help
attention8 --help
dsart8 --help
dsart32 --help
study1 --help
python -m eegle.pipelines.alpha8 --help
python -m eegle.pipelines.inhibition8 --help
python -m eegle.pipelines.classify8 --help
python -m eegle.pipelines.attention8 --help
python -m eegle.pipelines.dsart_recording --help
python -m eegle.pipelines.study1 --help
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

Run the formal Dynamic-State SART recipe with raw EEG recording and no online
model or task adaptation:

```bash
eegle run-forward \
  --config configs/forward_dynamic_sart.json \
  --task dynamic_sart \
  --task-mode psychopy \
  --participant sub-001 \
  --require-eeg
```

For a software-only contract check, add `--trials 24 --skip-eeg
--allow-missing-eeg --task-mode dry-run`. That override creates 12 support and
12 query trials rather than truncating the full recipe.

For participant acquisition, use the recording suites rather than the generic
single-session command. Each suite owns the complete visit: initial preflight,
120 seconds eyes open, 120 seconds eyes closed, 600-trial DSART session 1,
10-minute break, a fresh preflight/electrode gate, and an independent 600-trial
DSART session 2:

Both recording recipes are raw pass-through paths: no filtering, resampling,
re-referencing, artifact rejection, online capture, model process, or dashboard
runs during acquisition. Original EEG outlet timestamps and their measured LSL
clock corrections are retained alongside the corrected timestamp. Local
monotonic and LSL receipt-clock boundaries are retained separately so a device
clock with a different origin is never mistaken for missing EEG. See the
recording runbook for the exact column and render-path contract.

```bash
dsart8 --participant sub-001 --visit-id visit-20260716 --operator operator-id
dsart32 --participant sub-002 --visit-id visit-20260716 --operator operator-id
```

For a short, visible, no-EEG suite rehearsal, run ten experimental trials in
each session, two seconds for each baseline condition, and no timed break:

```bash
dsart8 --participant local-smoke --visit-id smoke-001 --task-mode psychopy \
  --trials 10 --baseline-seconds 2 --break-seconds 0 --skip-eeg \
  --window-size 1000 700 --output-root data/rehearsal

dsart32 --participant local-smoke-32 --visit-id smoke-32-001 --task-mode psychopy \
  --trials 10 --baseline-seconds 2 --break-seconds 0 --skip-eeg \
  --window-size 1000 700 --output-root data/rehearsal
```

For both suites, `--skip-eeg` bypasses LSL stream discovery, EEG sample/channel
validation, electrode checks, and marker loopback. The preflight records these
as skipped and continues to validate PsychoPy, storage, and visit identity.

On a Windows desktop where Controlled Folder Access or enterprise policy blocks
the repository's `Documents\...\data` directory, put the complete visit on an
approved user-writable root:

```powershell
$EegleData = Join-Path $env:LOCALAPPDATA "EEGle\data"
$env:EEGLE_SESSION_ROOT = $EegleData
$env:EEGLE_RUNTIME_CACHE_ROOT = "$env:LOCALAPPDATA\EEGle\runtime"
dsart8 --participant local-smoke --visit-id smoke-001 --task-mode psychopy --trials 10 --baseline-seconds 2 --break-seconds 0 --skip-eeg --window-size 1000 700 --session-root $EegleData
```

`dsart8` and `dsart32` use the same resolution order as `attention8`: an
explicit `--session-root`, then `EEGLE_SESSION_ROOT`, then the config's
`runtime.session_root`. The older `--output-root` spelling remains an alias.
The resolved root owns both `recording_suites\...` parent manifests and every
`participants\...` baseline/DSART child session. It is persisted into the
visit manifest and child `parameters.json` files before any PsychoPy runtime
code redirects cache-related environment variables. Relative runtime caches
such as `.runtime` are also resolved beneath that approved root, so PsychoPy,
Matplotlib, and LSL do not write back into the repository in `Documents`.
`EEGLE_RUNTIME_CACHE_ROOT` provides a direct override for constrained-language
shells and setup checks without requiring a generated JSON config.

This does not copy experiment data or runtime caches back into the checkout. If
`$EegleData` points at `LOCALAPPDATA`, both remain there unless an operator
performs a separate reviewed copy after the visit.

Shortened `--trials` runs skip participant qualification practice by default,
so this presents exactly ten experimental trials per session. Add
`--include-practice` only when specifically rehearsing the practice and
qualification flow. Full 600-trial participant runs include session-1 practice
automatically.

Do not add `--confirm-electrodes` during normal interactive acquisition. The
suite prints the signal/contact report and requires the operator to type `YES`
after both the initial and post-break electrode checks. The flag exists only
for a deliberately noninteractive, externally documented contact check.

The confirmed DSART32 montage uses Fp1 on device channel 17 and FC5 on device
channel 18. This mapping is authoritative in both the recording recipe and the
shared Enobio profile; no extra channel-map confirmation flag is required.

If a completed phase is followed by an abort or process failure, rerun the same
identity with `--resume`. Completed child sessions are retained; an incomplete
task attempt gets a new run directory and is linked as a partial recording:

```bash
dsart8 --participant sub-001 --visit-id visit-20260716 --operator operator-id --resume
```

See `docs/DSART_RECORDING.md` for the tomorrow-of-acquisition checklist,
software-only rehearsal, artifact layout, and stop/restart rules.

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
| `configs/forward_pvt_neuracle64.json` | PVT with the 65-value, 1000 Hz Neuracle W64 LSL transport |
| `configs/forward_go_nogo_enobio8.json` | Go/No-go with the posterior-alpha 8-channel montage |
| `configs/forward_go_nogo_inhibition8.json` | Observe-only Go/No-go with the inhibition montage |
| `configs/forward_go_nogo_classifier8.json` | Capture and observe-only GO/NO-GO EEG condition classification |
| `configs/forward_dynamic_sart.json` | Formal digit SART with raw recording, support/query phases, strict prestimulus epoch settings, and no active model |
| `configs/record_dsart8.json` | Two-session 600-trial DSART recording suite for the Enobio 8 dry montage |
| `configs/record_dsart32.json` | Same two-session DSART suite for the confirmed Enobio 32 wet montage |
| `configs/study1_neuracle64.json` | Visit-aware Study 1 Dynamic SART candidate for the 65-value Neuracle W64 LSL transport; live channel and cue-delivery gates remain explicit |

Hardware expectations live under `hardware.eeg`. Before collecting data, check
the configured channel count, sample rate, LSL stream type/name patterns, and
montage. Task and calibration durations also live in the selected config.

## Dynamic-State SART Data Contract

`dynamic_sart` is an independent task, not a renamed Go/No-go preset. It shows
digits 1 through 9, records a response to frequent go digits, and uses one
fixed configurable digit as the no-go stimulus. The default pilot plan is six
160-trial blocks: two support blocks followed by four query blocks. Practice is
criterion based. Digits are shown for 250 ms, followed by 1350 ms of fixation,
for a fixed 1600 ms SOI with zero intentional jitter. The complete
behavior-independent plan, seeds, conditions, fixed timing, phase assignments,
and marker labels are written before experimental execution.

The final support block ends with exactly one
`dynamic_sart_support_complete` event. At that boundary EEGle freezes a
support-only behavioral reference; query trials cannot change that reference.
The task then continues with the same timing and stimulus probabilities. This
event does not mean a model was trained or loaded. Support and query trials are
intentionally visually identical and are not announced to the participant:
`support` is the reference-estimation segment, while `query` is the later
segment evaluated relative to that frozen reference. Keeping the display and
response rule unchanged avoids making phase identity an experimental cue.

Every block begins with four guaranteed go trials before a no-go can appear;
the trailing-go reservation is configurable and is zero in the supplied
recipes. The two within-session breaks in each 600-trial recording enforce a
30-second minimum. SPACE is ignored before that minimum, is accepted from 30
through 60 seconds, and the task resumes automatically at 60 seconds. These are
separate from the recording suite's 10-minute inter-session break.

Immediately before experimental trial 1, after practice passes or is skipped,
the task displays `5`, `4`, `3`, `2`, `1`, and `GO!` for one second each.
Every countdown display has a flip-synchronized marker. Escape/Q remains active,
and countdown keypresses are recorded without being assigned to a trial.

Task-specific artifacts are written under `events/`:

```text
dynamic_sart_trials.csv
dynamic_sart_trials.jsonl
dynamic_sart_key_events.jsonl
dynamic_sart_blocks.csv
dynamic_sart_results.json
dynamic_sart_support_reference.json
dynamic_sart_labels.csv
dynamic_sart_label_contract.json
dynamic_sart_probes.jsonl
stimulus_manifest.json
```

Post-session analysis also writes the immutable-derived per-trial timing ledger
to `reports/dynamic_sart_timing.csv`.

Raw trial and key-event files are incrementally flushed. Derived labels keep
current behavior, past-only causal features, and future outcomes separate.
They include dense valid-go reaction-time targets plus distinct commission,
omission, premature, too-fast, multiple-response, and wrong-key facts; they do
not claim to observe a true latent attention state. Practice is excluded from
the support reference, labels, and default epoch extraction.

Timing schema v2 distinguishes logical schedules from observations.
`scheduled_response_window_close_*` and
`scheduled_next_trial_onset_monotonic` are plans; the unprefixed response-close
fields are sampled when the response loop actually exits. Actual inter-onset
duration is reconstructed during analysis only from the following trial's
flip-captured stimulus onset. The final presented trial therefore has no actual
next onset. Reports exclude planned breaks and thought probes from scheduler
drift summaries.

For the 200-trial support recipe, q80 is the primary exploratory
support-relative threshold. q95 remains secondary and explicitly
low-confidence: the 67-no-go plan leaves about 178 possible support go trials,
so the 200-valid-go preference rule cannot be reached. Every future label now
records its maturity trial, maturity phase, and whether it was available at
`support_complete`; selecting support anchors alone is not a leakage-safe
calibration filter.

The task is observe-only. An incoming `observe_only` action can be audited, but
actions that would alter sequence, probability, timing, salience, feedback, or
stimulation are rejected. The provided recipe disables online inference,
feedback effects, task adaptation, stimulation, and optional thought probes.
Model training, participant-specific heads, and live calibration after support
completion remain separate future work.

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
settings, Neuracle acquisition setup, Windows user/cache directories, and the
installed LabRecorder executable path. Runtime cache setup writes Matplotlib, PsychoPy,
and LSL configuration under `.runtime`; on Windows it also redirects
`USERPROFILE`, `APPDATA`, and `LOCALAPPDATA` for the process so optional GUI
libraries do not need to write into the real user profile during a run.
Preflight reports the current OS as `os_support`, installed command visibility
as `commands`, PsychoPy readiness as `display_ready`, realtime worker readiness
as `realtime_ready`, model-training dependency readiness as `training_ready`,
and the configured EEG device family as `eeg_device` when matching LSL streams
are visible.

GitHub Actions runs import, compile, and unit-test checks on Linux, macOS, and
Windows. Hardware validation remains local because NIC2, device drivers, LSL
network visibility, and PsychoPy display behavior are machine-specific.

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

The managed `labrecorder_xdf` backend launches an installed LabRecorder through
its loopback remote-control socket and writes `raw/recording.xdf`; Study 1 uses
this backend with the existing `raw/eeg.csv` recorder retained as a safety
mirror. A mirror failure is warning-only for this backend; the XDF remains
authoritative and is checked after each phase for sample retention, timestamps,
non-finite/flat/clipped signals, stream structure, and marker parity. Other
shipped acquisition presets remain on `lsl_csv` unless explicitly configured
otherwise.

## Example Neuracle W64 LSL Setup Check

For the guarded Windows x64 Neuracle W64 test sequence—including the supplied
PowerShell scripts for a visible dry run, Collect/LSL preflight, short XDF task,
and one-minute-per-condition short Study 1 visit—see
[`docs/NEURACLE64_WINDOWS_TEST.md`](docs/NEURACLE64_WINDOWS_TEST.md).

This first-pass Neuracle preset runs the same preflight, `lsl_csv` recorder,
PVT task, and session-output path as the Enobio presets. EEGle does not install
or launch the Neuracle acquisition software.

The shipped preset expects the acquisition computer to publish:

```text
type: EEG
channel count: 65 (64 physical inputs + reserved trigger/status value)
sample rate: 1000 Hz
name or source id containing: neuracle
```

The observed Collect outlet labels these values generically as `ch_001` through
`ch_065`; the confirmed Study 1 config maps them positionally. Do not reduce the
count to 64 or discard value 65 during acquisition. Raw recording preserves all
65 values while derived EEG analysis excludes ECG, EOG, and `TRIGGER_STATUS`.

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
   python -m pip install -e ".[runtime,analysis]"
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
logistic regression and pyRiemann, and the `torch` extra for EEGNet
(`deep-learning` remains a compatibility alias).
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

## Attention-Lapse System Test

`attention8` is the observe-only, SART-style attention-lapse recipe layered on
the existing Go/No-go/classify8 runtime. It uses the pre-stimulus EEG window
`[-2.0, 0.0]` seconds relative to stimulus onset and predicts same-trial lapse
risk before the response. No task adaptation, stimulation, or online encoder
updates are enabled.

```bash
attention8 collect --participant sub-001 --trials 240
attention8 train --session-dir <calibration-session> --check-ready
attention8 train --session-dir <calibration-session> --support-trials 50
attention8 compare --session-dir <calibration-session>
attention8 compare --session-dir <calibration-session> --online-session-dir <online-session> \
  --method log-reg --method riemann --method lora --method film
attention8 online --participant sub-001 --model-dir <calibration-session>/models/attention8 \
  --primary causal_bandpower_logreg --shadow foundation_head_logreg --shadow foundation_prototype
attention8 online --participant sub-001 --model-dir <calibration-session>/models/attention8 \
  --primary causal_bandpower_logreg --shadow foundation_prototype --enable-adaptation --adapt-shadows
attention8 evaluate --session-dir <online-session>
attention8 readiness --participant sub-001 --participant sub-002
attention8 pilot-suite --participant sub-001 --model-dir <existing-or-calibrated-attention8-model-dir> --write-configs
attention8 protocol
```

The default training target is `attention_lapse_binary` with primary label
`slow_go_rt`: correct GO reaction times at or above the session 80th percentile.
Reports also carry secondary omission, commission, and composite-lapse labels.
The support/query split excludes practice trials; support uses the first `K`
eligible main trials and query metrics use later trials only. The default online
support size is 50, and the report grid is declared as `0, 20, 50, 100`.

The first bundled baselines are `causal_bandpower_logreg`,
`riemann_tangent_logreg`, `torch_eegnet`, `foundation_head_logreg`, and
`foundation_prototype`. Foundation models are shadow-first. EEGle does not clone
research repos or load raw public-dataset checkpoints during live sessions.
Use `eegle-model` or another workbench to produce a validated runtime zip, then
import it into EEGle as a hashed model bundle:

```bash
eegle model-import --source <eegle-model-runtime.zip> --output <bundle-dir>
```

The imported zip must include runtime metadata, the exact input contract,
license/provenance, and runtime assets. EEGle stores the zip as a
content-addressed artifact and validates bundle hashes before live inference.

`attention8 compare` is the offline method-comparison step. It trains and
scores comparison bundles from captured epochs, writes
`reports/classification/attention8_offline_comparison/summary.json` and
`summary.csv`, and optionally includes deltas against the model used in an
online session. The shorthand methods map to concrete registry-backed models:
`log-reg` -> `causal_bandpower_logreg`, `riemann` ->
`riemann_tangent_logreg`, `lora` -> `foundation_head_logreg`, and `film` ->
`foundation_prototype`. The LoRA and FiLM names are comparison profiles only in
this repository today; they do not run parameter-efficient fine-tuning or FiLM
conditioning layers unless a future external adapter supplies those contracts.

For a two-subject real-EEG classification test, keep each subject isolated:
collect one calibration session, train subject-specific bundles, run
`attention8 compare`, then run one online session and `attention8 evaluate` per
participant. Do not pool the two subjects' sessions unless that pooling is an
explicit study-design choice. `attention8 readiness --participant sub-001
--participant sub-002` prints the subject-by-subject command checklist and the
current training dependency readiness.

`attention8 pilot-suite` prints a Windows-friendly dry-electrode suite for
three practical checks: a light online model-system smoke test, a calibration
and post-calibration online task, and a 100-trial deliberate-inattention
challenge. With `--write-configs`, it writes phase configs under
`data/attention8_pilot_suite/<participant>/configs/`. These configs keep
posterior-alpha/resting/closed-eyes baseline paths disabled. Challenge cue
trials are shown in the task and recorded into task events and the stimulus
manifest for later analysis, but they are not included in prediction-time model
metadata.

On locked-down Windows desktops, do not try to bypass access control. Choose a
data root that the current user is allowed to write and pass it through the
suite:

```powershell
$EegleData = Join-Path $env:LOCALAPPDATA "EEGle\data"
$env:EEGLE_SESSION_ROOT = $EegleData
attention8 pilot-suite --participant sub-0001 --session-root $EegleData --write-configs
attention8 online --config <phase-config> --participant sub-0001-postcal --model-dir <model-dir> --session-root $EegleData
```

`EEGLE_SESSION_ROOT` is honored by all new session creation, so setting it once
keeps generated phase configs, calibration runs, online runs, and child worker
paths on the approved data root. `attention8 collect` and `attention8 online`
also accept `--session-root` and probe the selected root before launching worker
subprocesses. If Windows blocks writes, the command fails before the realtime
processor starts and prints the path to replace.

Train from the calibration session, not a post-calibration online session. If
`realtime/epochs/epochs.npz` is missing but raw EEG and task events are present,
`attention8 train` attempts epoch export before fitting models. Sparse realtime
marker logs no longer mask fuller task manifests during offline export, and
epoch rejection reasons are summarized in `realtime/epochs/manifest.json`.
For sessions collected before this fix, rerun epoch export with the session
parameters explicitly:

```powershell
eegle extract-epochs --config "$Cal\parameters.json" --session-dir $Cal --source stimulus_manifest
attention8 train --session-dir $Cal --support-trials 50
```

Online adaptation is explicit opt-in with `attention8 online
--enable-adaptation`. The realtime worker still writes each prediction before
behavior is available, then consumes the task's delayed
`go_nogo_trial_complete` event to compute the online label and update eligible
adapters. Trial condition, reaction time, correctness, omission, and commission
fields are never passed through prediction-time model metadata. Adaptation rows
are written to `realtime/adaptation_updates.jsonl`, and state snapshots are
written under `realtime/adaptation_state/`.

In v1, `foundation_prototype` updates only frozen-embedding class prototypes.
`causal_bandpower_logreg` keeps the fitted logistic model fixed and updates
delayed-label threshold/calibration state. `foundation_head_logreg`,
`riemann_tangent_logreg`, and `torch_eegnet` remain frozen online unless future
adapters explicitly declare safe delayed-label update support. This still does
not enable task adaptation or stimulation.

## Current Scope

EEGle currently supports PVT, Go/No-go, and Dynamic-State SART execution,
Enobio/NIC2 and Neuracle LSL checks, CSV recording, marker and telemetry logs,
posterior-alpha calibration, realtime/replay scaffolding, ERP analysis, and HTML
reports. N-back, Sternberg, and anti-VEA remain registered future-task scaffolds.
