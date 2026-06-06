# Closed-Loop EEG Experiment Scaffold

This scaffold is the foundation for reproducible attention and working-memory EEG experiments with Enobio/NIC2, LSL, PsychoPy, and closed-loop neurofeedback components.

## Quick Start

Python 3.10 is required. Create an isolated environment and install the package from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip wheel
python -m pip install -e ".[runtime]"
closedloop-eeg list-tasks
closedloop-eeg doctor --allow-missing-eeg
```

The base install includes analysis, LSL, and dry-run dependencies. The `runtime` extra adds PsychoPy and specparam. Install `.[ml]` for classical model training or `.[deep-learning]` for Torch/ONNX inference.

No local interpreter path, operating system, or CPU architecture is required by default. PsychoPy display behavior, LSL networking, and EEG hardware drivers still vary by platform, so validate the real acquisition setup before collecting data.

Useful development commands:

```bash
make doctor
make list-tasks
make init-session
make dry-run
make forward-dry-run
make forward-pvt
make forward-pvt-8
make forward-go-nogo-dry-run
make forward-go-nogo-8
make forward-go-nogo-alpha-dry-run
./alpha8 full
./inhibition8 full
make alpha8-dry-run
make report-html SESSION=/path/to/session
```

The Makefile and `./alpha8` / `./inhibition8` wrappers are optional POSIX-shell conveniences. The installed `closedloop-eeg`, `alpha8`, and `inhibition8` console commands are the cross-platform entry points.

The current scaffold separates the system into four layers:

1. Preflight: platform and Python environment checks, optional package checks, LSL stream scan, Enobio/NIC2 stream match.
2. Experiment setup: task registry, JSON experiment configuration, BciPy-style session folders, behavioral event logging, trigger files.
3. Realtime setup: managed worker processes, ring buffer, causal preprocessing hooks, simple band-power model skeleton, feedback decision emitters.
4. Post-analysis: behavioral summaries, process summaries, report directory, hooks for MNE/BIDS/scalp/power/classification reporting.

PsychoPy should become the primary task display engine. BciPy is treated as an optional source of conventions and compatible ideas: session layout, trigger discipline, acquisition structure, offset correction, model training, replay, and MNE/BIDS conversion.

LabRecorder/XDF is not launched by the scaffold yet. The `labrecorder_xdf` process backend is currently a reproducibility hook that reports `unsupported` until a compatible LabRecorder install and dependencies are confirmed.

## Repository And Data Hygiene

Generated experiment data and runtime caches are intentionally excluded from Git through `.gitignore`. New sessions use:

```text
data/participants/<participant-id>/sessions/<date>/<experiment-id>/<task>/run-<timestamp>/
```

The shipped configs use `example-participant`; pass `--participant <id>` for a real run. Session manifests contain participant IDs, system metadata, and local paths, so inspect any selected example dataset before publishing it. Keep raw EEG and full session output outside the source repository or in a dedicated data release.

## Alpha8 Full Pipeline

`alpha8 full` runs the complete posterior-alpha Enobio8 Go/No-go suite sequentially:

```text
doctor/preflight -> posterior alpha calibration -> 100-stimulus Go/No-go -> post-analysis -> HTML summary
```

From the repository root:

```bash
./alpha8 full
```

When the package is installed into the runtime environment, the console entry point is:

```bash
alpha8 full
```

Defaults:

- config: `configs/forward_go_nogo_enobio8.json`
- calibration suite: `posterior_alpha`
- task: Go/No-go
- task mode: `psychopy`
- main experiment stimuli: `100`
- EEG: required Enobio/NIC2 LSL stream
- output: regular session folder plus `reports/alpha8_full_summary.json`, `reports/summary.json`, and `reports/experiment_summary.html`

Development smoke without EEG:

```bash
./alpha8 full --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2
```

or:

```bash
make alpha8-dry-run
```

## Inhibition8 Observe-Only Pipeline

`inhibition8 full` preserves the existing `alpha8` workflow and adds a separate
Go/No-go instrumentation pipeline using:

```text
Fz, Cz, Pz, C3, C4, P3, P4, Oz
```

It runs calibration, an observe-only task, causal replay validation,
publication-quality analysis, exploratory feature-behavior analysis, and the
HTML summary. It does not train a decoder, adapt the task, or emit feedback.

```bash
./inhibition8 full
./inhibition8 full --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2
closedloop-eeg replay-realtime --session-dir /path/to/session
```

The realtime worker emits staged facts to `realtime/event_features.jsonl` and
captures its exact EEG chunks and marker order in `realtime/engine_input.bin`.
Replay feeds that capture through the same causal filters, reference gate,
scheduler, and feature registry. A material online-versus-replay difference
marks the session analytically invalid and prevents exploratory behavioral
modeling.

The feature windows are:

```text
readiness alpha  -1.0 to -0.2 s
ERP baseline     -0.2 to  0.0 s
N2               0.2 to  0.35 s
P3               0.3 to  0.6 s
alpha ERD        0.2 to  0.8 s
```

Realtime feature reports are written under `reports/realtime_features/`.

## Forward PVT Experiment

The first real experiment target is the forward loop only:

```text
preflight -> FeedbackManager -> PVT -> managed LSL EEG recorder -> behavior/raw data -> minimal report
```

Use this when NIC2 is streaming Enobio EEG through LSL:

```bash
make doctor-enobio8
make forward-pvt-8
```

For development without a subject or EEG device:

```bash
make forward-dry-run
```

The PsychoPy PVT now starts with an instruction screen. The participant presses `SPACE` only after they understand the task. During each trial they should keep their eyes on the center, wait for the central white target and `PRESS` cue, and press `SPACE` as quickly as possible. Early presses are flagged as premature but do not stop the task.

The photodiode timing patch is disabled by default so the experiment display is not visually confusing. Re-enable `hardware.display.photodiode_patch` only for latency validation with a real photodiode.

The forward run creates:

```text
data/participants/<participant-id>/sessions/<date>/<experiment-id>/pvt/run-<timestamp>/
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
  calibration/
    metadata.json
    events.jsonl
    eeg.csv
    psd.csv
    specparam.json
    alpha_calibration.json
    alpha_calibration.svg
  logs/
    telemetry.jsonl
    debug.jsonl
    preflight.json
    feedback_manager.json
    processes/
      recorder.status.json
      realtime_processor.status.json
      offline_analyzer.status.json
  realtime/
    windows.jsonl
    decisions.jsonl
    markers.jsonl
    feedback.jsonl
    alpha_power.jsonl
    epochs/
      epochs.npz
      epochs.jsonl
      manifest.json
  reports/
    summary.json
    alpha/
      alpha_summary.json
      trial_alpha.csv
```

`configs/forward_pvt_enobio.json` is the generic Enobio PVT config; it requires an Enobio/NIC2 LSL EEG stream before the session proceeds.

For the 8-channel Enobio cap, use `configs/forward_pvt_enobio8.json`.

## Telemetry And Logging

Each session writes structured telemetry beside the existing task timing files:

```text
logs/telemetry.jsonl  # lifecycle, task-domain, realtime, EEG health, and process events
logs/debug.jsonl      # debug/trace spans when enabled
logs/processes/       # worker stdout/stderr plus status JSON
```

Console telemetry is sparse by default: session init, preflight, LSL/device verification, worker status, task start/end, analysis, and completion. Existing timing records remain canonical for analysis: `events/events.jsonl`, `events/behavior.csv`, `triggers.txt`, stimulus manifests, raw EEG CSV, and realtime epoch/decision files.

Useful runtime overrides:

```bash
python -m reproduce.cli run-forward \
  --config configs/forward_go_nogo_enobio8.json \
  --task go_nogo \
  --task-mode dry-run \
  --skip-eeg \
  --log-level realtime

python -m reproduce.cli run-forward \
  --config configs/forward_go_nogo_enobio8.json \
  --task go_nogo \
  --task-mode dry-run \
  --skip-eeg \
  --log-level debug \
  --trace
```

Use `--quiet` to suppress console telemetry while still writing session logs. Config defaults live under the top-level `telemetry` block: `console_level`, `file_level`, `trace_enabled`, and `heartbeat_seconds`.

## Go/No-go Experiment

The Go/No-go task is registered as `go_nogo`. It presents a 70/30 Go/No-go split by default. Go stimuli are combinations of circle, square, triangle, star, or hexagon with red, blue, green, yellow, or magenta. The no-go stimulus is configured before the run and shown explicitly in the instructions.

Development run:

```bash
make forward-go-nogo-dry-run
```

EEG-connected run:

```bash
make doctor-enobio8
make forward-go-nogo-8
```

Go/No-go writes two task-specific outputs in addition to the shared event logs:

```text
events/go_nogo_results.json
events/go_nogo_trials.csv
events/stimulus_manifest.json
```

The CSV columns are `stimulus_number`, `is_no_go`, `reaction_time`, `correct_press`, `rolling_average_accuracy`, `button_press_count`, and `timestamp_since_start`.

## Go/No-go Posterior Alpha Calibration

The `posterior_alpha` calibration suite runs before Go/No-go when selected:

```text
preflight -> recorder -> posterior alpha calibration -> alpha analysis -> realtime alpha Go/No-go -> reports
```

Development run without EEG:

```bash
make forward-go-nogo-alpha-dry-run
```

EEG-connected run:

```bash
python -m reproduce.cli run-forward \
  --config configs/forward_go_nogo_enobio8.json \
  --task go_nogo \
  --task-mode psychopy \
  --require-eeg \
  --calibration-suite posterior_alpha
```

The posterior 8-channel profile for this pipeline is `P3, P4, PO3, PO4, Pz, O1, O2, Oz` and is registered as `enobio8_alpha_posterior`. The PsychoPy calibration GUI asks the participant to confirm instructions with `SPACE`, runs a 5-second countdown, records eyes-open fixation, records eyes-closed rest, and then runs a short Go/No-go calibration practice block.

Calibration analysis writes PSD, specparam metadata, the accepted or fallback online band, baseline power statistics, and a lightweight SVG plot under `calibration/`. If `specparam` is not installed in the active runtime, preflight reports that as a warning and calibration falls back to 8-12 Hz with `low_confidence_fallback`.

During the main task, realtime alpha estimates are written to `realtime/alpha_power.jsonl`. Post-analysis joins those estimates to Go/No-go trials, computes an offline raw-EEG alpha time series, and writes `reports/alpha/trial_alpha.csv`, `reports/alpha/offline_alpha_timeseries.csv`, and `reports/alpha/alpha_summary.json`. The first alpha pipeline is observe-only: it measures and tags alpha state but does not adapt stimuli or show participant feedback.

The offline analyzer also writes `reports/experiment_summary.html`. Regenerate it for an existing session with:

```bash
make report-html SESSION=/path/to/session
```

The HTML report includes a resizable channel-level alpha graph and an EEG replay canvas that overlays raw channel traces, live alpha estimates, offline alpha power, and event-marker highlights. Marker categories such as stimulus onset, stimulus offset, button presses, calibration, feedback, and system events can be toggled independently.

## Go/No-go ERP / P300 Analysis

After a Go/No-go session with `raw/eeg.csv`, run:

```bash
python -m reproduce.cli analyze-erp \
  --config configs/forward_go_nogo_enobio8.json \
  --session-dir <session-dir>
```

This creates:

```text
reports/erp/
  erp_summary.json
  erp_roi_waveforms.csv
  p300_by_trial.csv
  erp_waveforms.png
  go_nogo_erp_dashboard.html
```

The analyzer uses MNE to load the EEG CSV, epoch stimulus onsets from `events/stimulus_manifest.json`, baseline-correct from -200 to 0 ms, and search for P300 peaks from 250 to 600 ms. The dashboard is a horizontal strip of fixed-width stimulus-locked interval cards: each card shows the saved stimulus, a single-trial ROI waveform with the detected P3 point, and montage frames across that stimulus interval. The current CSV-only alignment estimates event samples from EEG `local_received_time`; use XDF or recorded marker streams for tighter timing validation.

## Marker-Locked Epoch Extraction

The realtime marker epocher uses `realtime.epoching` config to convert stimulus markers such as `go_nogo_stimulus_onset_*` into fixed-window EEG epochs. Online, the realtime worker stores markers, waits until enough post-stimulus raw samples are buffered, writes epoch metadata to `realtime/epochs/epochs.jsonl`, and can run a model decision from that completed epoch. Rolling-window decisions remain available by disabling `realtime.epoching.enabled`.

For offline replay, analysis, or model training:

```bash
python -m reproduce.cli extract-epochs \
  --config configs/forward_go_nogo_enobio8.json \
  --session-dir <session-dir>
```

This writes derived artifacts only under `realtime/epochs/`: `epochs.npz` stores `X` as `epochs x channels x samples`, `y` as target labels, and timing/channel arrays; `epochs.jsonl` stores per-epoch marker metadata; `manifest.json` records the raw EEG SHA-256 before and after export so any accidental mutation is detectable.

## Closed-Loop Model Decisions

The v1 closed-loop model path is `RealtimeEpocher -> ModelAdapter -> DecisionPolicy -> feedback emitter -> TaskFeedbackClient -> PsychoPy boundary`. `realtime.model.kind` selects adapters such as `erp_peak_baseline`, `sklearn_xdawn_lda`, `pyriemann_erp_cov`, `torch_eegnet`, `torch_shallowconvnet`, or `onnx_p300`. Optional ML packages are imported only when the selected adapter needs them.

Classical baselines can be trained from extracted epochs:

```bash
python -m reproduce.cli train-model \
  --config configs/forward_go_nogo_enobio8.json \
  --kind sklearn_xdawn_lda \
  --epochs-npz <session-dir>/realtime/epochs/epochs.npz \
  --output <session-dir>/realtime/models/sklearn_xdawn_lda.joblib
```

`realtime.decision_policy` converts normalized predictions into bounded task actions such as `increase_no_go_probability`, `adjust_isi`, `repeat_condition`, `show_reward`, and `set_visual_alpha`. The Go/No-go PsychoPy task polls `feedback.client` nonblockingly and applies accepted actions only at deterministic boundaries before trial generation or between trials.
