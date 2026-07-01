# Architecture

The scaffold follows this data path:

```text
Preflight
  -> optional calibration suite
  -> PsychoPy task
  -> LSL marker stream
  -> Enobio/NIC2 EEG LSL stream
  -> recorder/session data
  -> realtime buffer
  -> preprocessing/model/alpha measurement
  -> feedback/adaptation
  -> post-analysis reports
```

## Layer 0: Preflight

`eegle.preflight` checks the active or explicitly configured Python
interpreter, shared-codebase OS support, optional platform expectations, package
availability, LSL discovery, configured EEG device identification, and
Enobio/NIC2 stream matching. It also emits capability checks for installed
console commands, display readiness, realtime worker readiness, and
model-training dependency readiness. OS-specific differences should stay in this
preflight/support layer or in narrowly guarded runtime workarounds; the operator
entry points remain the installed console commands on every supported OS.

## Layer 1: Experiment Setup

`configs/default_experiment.json` defines the participant, task, output formats, hardware expectations, task parameters, optional calibration suites, realtime settings, and analysis settings.

Experiment components are selected in the config:

```json
{
  "components": {
    "preflight": "default",
    "task": "pvt",
    "eeg_recorder": "lsl_csv",
    "realtime_processor": "disabled",
    "feedback": "disabled",
    "analysis": "minimal"
  }
}
```

The current implemented forward stack is:

```text
default preflight -> FeedbackManager -> task -> managed recorder/realtime workers -> minimal analysis
```

When `--calibration-suite posterior_alpha` is selected for Go/No-go, the runner starts the recorder, runs the posterior alpha calibration suite, writes the individualized or fallback alpha band into `parameters.json`, then starts realtime alpha measurement before the main task.

`alpha8 full` is the top-level Enobio8 posterior-alpha orchestration command. It runs a required Enobio8 setup/preflight check, then invokes the existing forward runner with `posterior_alpha`, 100 main Go/No-go stimuli, realtime alpha measurement, post-analysis, and `reports/experiment_summary.html` generation in strict sequence. The dry validation form is `alpha8 full --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2`.

`eegle.session` creates a BciPy-inspired session layout:

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
    stimulus_manifest.json
  calibration/
    metadata.json
    events.jsonl
    eeg.csv
    psd.csv
    fooof.json
    alpha_calibration.json
    alpha_calibration.svg
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
    experiment_summary.html
    alpha/
      alpha_summary.json
      trial_alpha.csv
      offline_alpha_timeseries.csv
  logs/
    telemetry.jsonl
    debug.jsonl
    preflight.json
    feedback_manager.json
    processes/
      recorder.status.json
      realtime_processor.status.json
      offline_analyzer.status.json
```

`eegle.telemetry` is the observability side-channel for this layout. It writes structured JSONL records with `timestamp_wall`, `timestamp_monotonic`, `session_id`, `session_dir`, `component`, `event`, `level`, `message`, and `metadata`. Console routing has four levels:

- `quiet`: suppress console telemetry while preserving files.
- `default`: sparse operator-visible lifecycle and failure messages.
- `realtime`: task stimulus/response, marker, model, feedback, and EEG health events.
- `debug`: realtime plus debug diagnostics; `--trace` also writes span records to `logs/debug.jsonl`.

Telemetry augments the canonical experiment files. Stimulus timing still lives in `events/events.jsonl`, `triggers.txt`, stimulus manifests, and marker streams; EEG remains in `raw/` or future XDF/BIDS outputs.

## Layer 2: Realtime Closed Loop

`eegle.feedback_manager.FeedbackManager` owns the managed Architecture C lifecycle. It can start the recorder before calibration, reconfigure after calibration, launch enabled realtime workers before the task, stop long-running workers after the task, and run offline analysis after recording ends. The current workers are:

- `recorder`: `lsl_csv` is implemented; `labrecorder_xdf` is a configured hook only.
- `realtime_processor`: reads EEG LSL and marker LSL, maintains raw and processed ring buffers, uses causal online preprocessing, runs marker-locked epochs through a registry-backed `ModelAdapter`, converts predictions through `DecisionPolicy`, and logs/emits explicit task actions. It can also run calibrated posterior alpha measurement continuously and write `realtime/alpha_power.jsonl` while marker-locked epoching remains enabled. Rolling-window decisions remain available as a compatibility path.
- `dashboard`: optional non-critical localhost HTTP worker that reads session artifacts and displays live classifier status without touching the PsychoPy process.
- `dashboard` demo mode: an explicitly simulated classroom path that subscribes
  to the run-unique PsychoPy LSL marker stream, emits delayed imperfect guesses
  to `realtime/demo_predictions.jsonl`, and never alters real classifier
  predictions or evaluation artifacts.
- `offline_analyzer`: runs the minimal session report.

`eegle.realtime` defines the reusable online pieces: ring buffer, marker-locked epoching, causal preprocessing, model registry, model adapters, bounded decision policies, feedback emitters, and the task-side feedback client. The marker epocher waits until the configured post-stimulus window is available before emitting an epoch, so P300 model decisions naturally apply to later stimuli or blocks.

The feedback contract is action-oriented: model predictions become `TaskAction` records such as `increase_no_go_probability`, `adjust_isi`, `repeat_condition`, `show_reward`, or `set_visual_alpha`. The PsychoPy Go/No-go task consumes these through `TaskFeedbackClient` only at deterministic boundaries and writes received/accepted/rejected/applied action records into task events and the stimulus manifest.

The first posterior alpha pipeline is observe-only. Calibration uses MNE/Welch PSD plus FOOOF/specparam when installed to select an individualized alpha band. If no accepted posterior alpha peak is available, the suite writes a `low_confidence_fallback` result with an 8-12 Hz band. Realtime alpha uses causal filtering, posterior aggregation, artifact gating, Hilbert-envelope power snapshots, and optional baseline z-scoring.

The `FeedbackManager` reports worker `process.start`, `process.ready`, `process.stop`, `process.failed`, and timeout events into telemetry. Worker status JSON files remain the compact machine-readable process state; telemetry adds timestamped history for reproducibility and debugging.

## Layer 3: Post-Experiment Analysis

`eegle.analysis` summarizes task behavior, process state, realtime logs, raw EEG metadata, alpha validation, and Go/No-go ERP/P300 outputs when a Go/No-go stimulus manifest and EEG CSV are present. The ERP path uses MNE to build stimulus-locked epochs, ROI waveforms, P300 metrics, per-trial P300 CSVs, and a static HTML dashboard made of fixed-width stimulus-locked interval cards. Each card shows the saved stimulus, the single-trial ROI waveform with the detected P3 point, and montage frames across that stimulus interval.

Alpha validation joins `realtime/alpha_power.jsonl` to Go/No-go trial timing, writes `reports/alpha/trial_alpha.csv`, computes `reports/alpha/offline_alpha_timeseries.csv` from raw EEG, and summarizes whether pre-stimulus alpha predicts reaction time or accuracy in `reports/alpha/alpha_summary.json`. `reports/experiment_summary.html` embeds a decimated replay view with raw channel traces, marker toggles, live alpha estimates, and the offline alpha overlay.

The current ERP alignment path estimates event samples from task monotonic timestamps and EEG `local_received_time` in `raw/eeg.csv`. Go/No-go events now also store LSL-clock timestamps in event metadata and stimulus manifests when `pylsl.local_clock()` is available, and LSL marker outlets use that clock for pushed markers. XDF or a recorded marker stream should still become the canonical timing source for publication-quality ERP latency work.

The `extract-epochs` CLI replays the same marker-locked extraction offline and writes model-ready derived data to `realtime/epochs/epochs.npz` without modifying `raw/eeg.csv`. Its manifest hashes the raw EEG file before and after export and records marker-source hashes, epoch config, data shape, channel names, and label mapping.

The `classify8` workflow layers participant-specific condition and
attention-lapse decoding on the marker-locked epoch path. Calibration collection
can run with `realtime.inference.enabled=false` while still capturing exact EEG
chunks and markers. Training writes versioned frozen model bundles. Online
testing loads a primary model plus optional shadows, applies a shared quality
gate, removes condition/stimulus/response metadata before inference, and writes
normalized rows to `realtime/model_predictions.jsonl`. Feedback remains
observe-only unless a research-gated stimulation-candidate policy is explicitly
enabled.

`eegle.ml` owns the model registry, model-family specs, channel/input
contracts, behavior-derived attention-lapse targets, and threshold calibration.
Multi-session behavior targets preserve per-epoch source-session identity and
refuse ambiguous trial-only joins. Evaluation helpers report threshold-sensitive
metrics at the calibrated operating point while preserving default-threshold
comparisons. Supported trainable paths include
baseline-corrected ERP ROI logistic regression, pyRiemann xDAWN covariance plus
tangent-space logistic regression, and TorchScript EEGNet. External
CNN/foundation entries such as BENDR, LaBraM, and sequence decoders are
registry/checkpoint adapter targets; checkpoints are user-supplied and hashed.
The old flattened `sklearn_xdawn_lda` name remains a compatibility alias for
`sklearn_flatten_lda`; it is not an xDAWN model.
See `MODEL_TRAINING_TESTING_GOALS.md` for the training workflow, quality
contract, evaluation questions, and near-term model goals.

Classifier replay consumes the same binary capture format used by the staged
feature replay, reconstructs marker-locked epochs, reloads the session-snapshot
model bundles, and compares labels and probabilities. Final scoring joins
predictions to `events/stimulus_manifest.json`, which remains the canonical
truth source.
