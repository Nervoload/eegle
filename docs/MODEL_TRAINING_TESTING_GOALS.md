# Model Training, Testing, and Goals

This document orients agents and contributors around the GO/NO-GO EEG
condition-classifier path. The current goal is participant-specific,
observe-only decoding of the displayed GO versus NO-GO condition from
post-stimulus EEG epochs. This is not yet a claim that the system can infer
inhibition state, intent, or clinical status.

## Current Workflow

`classify8` is the operator-facing workflow:

```bash
classify8 collect --participant sub-001 --trials 240
classify8 train --session-dir <calibration-session> --check-ready
classify8 train --session-dir <calibration-session>
classify8 train --session-dir <run-a> --session-dir <run-b> --target attention_lapse_binary
classify8 online --participant sub-001 --model-dir <calibration-session>/models/classifier \
  --primary erp_roi_logreg --shadow pyriemann_erp_cov --shadow torch_eegnet --trials 160
classify8 evaluate --session-dir <online-session>
```

The generic `eegle` commands expose the same lower-level steps:

```bash
eegle extract-epochs --session-dir <session>
eegle train-model --kind erp_roi_logreg --epochs-npz <session>/realtime/epochs/epochs.npz --output <bundle-dir>
eegle train-model --kind cnn_eegnet --target attention_lapse_score --session-dir <session> --output <bundle-dir>
eegle model-list
eegle replay-classifier --session-dir <online-session>
eegle evaluate-model --session-dir <online-session>
```

## Data Flow

Calibration collection runs the Go/No-go task and captures EEG plus LSL marker
timing into a session directory. Epoch export reads canonical timing records and
raw EEG, then writes model-ready arrays to:

```text
<session>/realtime/epochs/
  epochs.npz
  epochs.jsonl
  manifest.json
```

Training loads one or more `epochs.npz` files, rejects duplicate or incompatible
epoch sources, preserves per-epoch source-session indices for multi-session
joins, removes ineligible practice or unlabeled epochs, applies the quality
gate, checks that both target classes are present, baseline-corrects epochs,
validates the model and channel contract, calibrates a decision threshold, and
writes frozen model bundles under:

```text
<session>/models/classifier/<model-kind>/
  manifest.json
  metrics.json
  model.joblib or model.pt
```

`classify8 train --check-ready` reports the package readiness for the requested
model kinds without fitting models. Regular training reports missing optional
model dependencies per model kind, so one unavailable stack does not hide the
readiness of the others.

Online testing snapshots the requested primary and shadow bundles into the
online session, runs marker-locked epochs through the shared realtime model
contracts, writes predictions to `realtime/model_predictions.jsonl`, and joins
ground truth only later for dashboard summaries and scoring.

Training targets are:

- `condition`: the existing displayed GO versus NO-GO label.
- `attention_lapse_binary`: behavior-derived lapse label from slow GO reaction
  times, omissions, commission errors, and trailing lapse score.
- `attention_lapse_score`: the same behavior signal used as an explicit score
  source, then thresholded for the current binary classifier families. This is
  not regression learning yet; bundle metadata marks it as thresholded binary
  classification.

## Supported Models

- `erp_roi_logreg`: interpretable baseline-corrected ERP ROI features with
  logistic regression. This is the default primary model.
- `pyriemann_erp_cov`: ERP covariance model using pyRiemann with tangent-space
  logistic regression.
- `torch_eegnet` / `cnn_eegnet`: trainable TorchScript EEGNet-style epoch model.
- `sklearn_flatten_lda`: flattened-epoch LDA baseline exposed by the generic
  `eegle train-model` command.
- `sklearn_xdawn_lda`: compatibility alias for `sklearn_flatten_lda`; despite
  the old name, it is not an xDAWN model.
- `torch_shallowconvnet` / `cnn_shallowconvnet`: external TorchScript CNN
  adapter, shadow-only by default.
- `foundation_bendr`, `foundation_labram`, and `sequence_external`: external
  checkpoint adapter targets for EEG foundation or sequence models. They are
  registry entries with explicit dependency and artifact contracts; EEGle does
  not download checkpoints.

Run `eegle model-list` for the current registry, aliases, trainability,
dependency requirements, realtime support, and supported targets.

## Quality and Label Safety

The shared quality and model-preparation contracts live in
`eegle/realtime/classification.py`. The shared training and inference
adapters live in `eegle/realtime/models.py`.

Important invariants:

- Model input metadata is sanitized before inference. Trial labels, stimulus
  condition, response correctness, and training labels must not be available to
  the model.
- Quality-gate rejection should be explicit and traceable through prediction
  rows, metrics, and tests.
- Model bundles store channel names, sample rate, sample count, epoch window,
  baseline, input units, model family/spec metadata, channel contract, artifact
  hashes, calibration threshold, software versions, and path-redacted
  training-source hashes.
- Multi-session attention-lapse labels join behavior to epochs by
  `source_session_index` plus trial number. Training refuses ambiguous
  trial-only joins when more than one session is involved.
- Realtime inference does strict channel, sample-rate, sample-count, and epoch
  window matching. Resampling and montage transforms must be done before bundle
  training/export until explicit realtime implementations are added.
- Online predictions are observe-only by default. Stimulation candidates require
  the `attention_lapse_stimulation` policy plus explicit `allow_stimulation`
  and `research_safety_ack` gates, valid quality checks, cooldowns, and
  non-practice trials.

## Evaluation

The evaluation path should answer separate questions:

- Did the EEG device and NIC2/LSL connection deliver the expected channel count,
  sample rate, and marker stream?
- Were epochs complete and accepted by the quality gate?
- Did the signal show excessive drift, DC offset, flatlines, non-finite values,
  or 60 Hz contamination?
- Does the selected model generalize beyond the calibration data?
- Does the calibrated operating threshold produce acceptable balanced accuracy
  and target recall?
- Do primary and shadow models agree or fail in different ways?

Scoring joins predictions to `events/stimulus_manifest.json`, which is the
canonical truth source. Evaluation outputs include classification reports under
`reports/classification/`. Training, live dashboard snapshots, and full offline
session evaluation report confusion matrices and threshold-sensitive metrics at
the calibrated operating threshold when one is present. Default `0.5` threshold
metrics are retained under `default_threshold_metrics` for reproducible
comparison with older bundles and papers.

## Testing Strategy

Use focused tests first when changing the classifier path:

```bash
python3 -m unittest tests.test_classification
python3 -m unittest tests.test_ml_infrastructure
python3 -m compileall -q eegle/realtime/classification.py eegle/realtime/models.py eegle/pipelines/classify8.py tests/test_classification.py tests/test_ml_infrastructure.py
```

Use broader suites when changing shared orchestration, config loading, or worker
contracts:

```bash
python3 -m unittest discover -s tests
```

When real recordings are involved, keep the investigation decomposed into
device, connection, signal quality, model design, and data size/label quality.
Do not collapse poor online accuracy into a single model verdict until those
channels have been checked.

## Near-Term Goals

- Build stronger multi-session validation, especially leave-one-session-out
  checks before trusting online performance.
- Add operator-facing quality summaries that distinguish contact quality,
  connectivity, drift, line noise, flatlines, and rejected epoch causes.
- Add stronger calibration-set separation, especially leave-one-session-out
  threshold selection for multi-session participant models.
- Keep primary and shadow model reporting side by side so pyRiemann, ROI, and
  deep models can be compared on the same captured epochs.
- Expand reports to explain model coverage, balanced accuracy, NO-GO recall,
  ROC AUC, permutation significance, and why any epochs were rejected.
- Preserve replayability: online sessions should be reproducible from captured
  EEG chunks, markers, frozen bundles, and config snapshots.

## Non-Goals For Now

- Do not treat the classifier as clinical evidence.
- Do not enable adaptive task changes from classifier predictions by default.
- Do not mix simulated `classify8 demo` predictions with real classifier
  artifacts.
- Do not publish or commit participant session data without explicit review.
