# EEGle Open-Source Library Review

This document is a working review of EEGle's viability as a pip-installable
open-source Python science library for reproducible realtime EEG/BCI
experiments and model-learning systems. It is meant to seed later public docs,
API guides, tutorials, and repo-splitting decisions.

## Executive Verdict

EEGle is viable as an alpha-stage open-source Python library if its public
identity stays narrow:

> EEGle is reproducible realtime EEG/BCI experiment infrastructure: session
> provenance, LSL/task markers, causal epoching and replay, model contracts,
> model bundles, calibration, primary/shadow inference, and conservative
> closed-loop decision policies.

That thesis is stronger than positioning EEGle as a general EEG analysis
framework, a PsychoPy app collection, a stimulation controller, or an EEG model
zoo. The project already has unusually strong building blocks for scientific
reproducibility:

- BciPy-inspired session folders in `eegle/session.py`.
- Structured telemetry in `eegle/telemetry.py`.
- Event-locked epoch export with raw-file and marker hashes in
  `eegle/realtime/epoching.py`.
- Label-blind classifier metadata and normalized prediction rows in
  `eegle/realtime/classification.py`.
- Trainable classical and compact neural baselines in `eegle/realtime/models.py`.
- Content-addressed model bundles in `eegle/models/bundles.py`.
- Typed public model contracts in `eegle/models/contracts.py`.
- Dynamic model declarations in `eegle/models/registry.py`.
- Calibration state and explicit adapter protocol in `eegle/models/calibration.py`.
- Deterministic classifier replay and full-inference scoring in
  `eegle/analysis/classification.py`.
- Conservative decision policies in `eegle/realtime/policy.py`.

The main shipping risk is not that EEGle lacks useful features. It is the
opposite: the package still carries library code, operator workflows, realtime
workers, task implementations, dashboards, model training, and analysis in one
namespace. That is acceptable for a `0.1.x` research library, but the public
docs and package boundaries must make clear what is stable API and what is a
recipe, plugin candidate, or lab workflow.

## 1. Core Packages, Methods, And Functions

### `eegle`

`eegle/__init__.py` now exposes a small public API:

- `Session`, `SessionPaths`
- `EpochingConfig`, `MarkerEvent`, `ExtractedEpoch`
- `ModelSpec`, `ModelContract`, `ModelBundle`, `ModelPrediction`
- `CalibrationState`, `CalibrationAdapter`
- `ReplayResult`
- `TaskAction`

This is the right top-level shape. It gives users stable nouns without forcing
them to discover the historical layout of `eegle.ml`, `eegle.session`, and
`eegle.realtime`.

### `eegle.core`

`eegle/core/__init__.py` collects the general-purpose experiment primitives:

- `create_session`, `paths_for_existing_session`, `SessionPaths`
- `Session`
- `load_config`, `write_config`, `merged_config`, `task_config`
- `Telemetry`
- `file_sha256`

The actual session layout is still implemented in `eegle/session.py`.
`create_session()` writes:

- `parameters.json`
- `manifest.json`
- `raw/eeg.csv` and `raw/eeg_metadata.json`
- `events/events.jsonl`, `behavior.csv`, `stimulus_manifest.json`
- `calibration/`
- `realtime/windows.jsonl`, `decisions.jsonl`, `markers.jsonl`,
  `model_predictions.jsonl`, `feedback.jsonl`, `alpha_power.jsonl`
- `realtime/epochs/epochs.npz`, `epochs.jsonl`, `manifest.json`
- `realtime/models/`
- `reports/`
- `logs/telemetry.jsonl`, `debug.jsonl`, and worker status files

This is one of EEGle's best library assets. Session creation should remain a
stable public feature.

Readiness: high for alpha. The main hardening need is versioned JSON schemas
for the session manifest, event rows, telemetry rows, epoch manifest, and
summary files.

### `eegle.streams`

`eegle/streams` is a public facade over acquisition and marker helpers:

- `LslStream`
- `resolve_streams`
- `LslMarkerOutlet`
- `NullMarkerOutlet`
- `MarkerEvent`
- `LslEegRecorder`
- `run_simulated_eeg`

This is the correct public namespace, but it is still mostly a wrapper around
older modules such as `eegle.lsl` and `eegle.devices.lsl_eeg`. For a public
library, it should grow into a small stream abstraction layer:

- stream discovery
- stream capability snapshots
- marker outlets
- recorder interfaces
- simulated sources
- the managed LabRecorder/XDF recorder and future BIDS exporters

Readiness: medium. Useful now, but not yet as cleanly decoupled as `core` or
`models`.

### `eegle.realtime`

The reusable realtime surface includes:

- `EpochingConfig`
- `MarkerEvent`
- `ExtractedEpoch`
- `RealtimeEpocher`
- `ModelAdapter`
- `ModelPrediction`
- `TaskAction`
- `make_decision_policy`

Important modules:

- `eegle/realtime/epoching.py` handles marker matching, Go/No-go marker
  parsing, online epoch readiness, offline epoch extraction, epoch NPZ export,
  JSONL metadata export, and raw-file hash validation.
- `eegle/realtime/policy.py` converts predictions into bounded `TaskAction`
  records and includes `ConservativeDecisionPolicy`, `ObserveOnlyPolicy`, and
  `AttentionLapseStimulationPolicy`.
- `eegle/realtime/models.py` defines `ModelPrediction`, `ModelAdapter`,
  trainable model adapters, inference adapters, training utilities, threshold
  calibration, metrics, and bundle-writing integration.
- `eegle/realtime/classification.py` defines label-blind prediction rows,
  quality gates, classifier epoch preparation, ERP ROI features, and legacy
  bundle helpers.

Readiness: high conceptually, medium structurally. The module is powerful, but
still mixes reusable contracts, classifier-specific helpers, training code, and
compatibility functions. Over time, model bundle helpers should live only under
`eegle.models.bundles`, model training APIs should be exposed through
`eegle.models`, and `eegle.realtime` should keep the online loop, buffers,
epoching, inference orchestration, policies, and emitters.

### `eegle.models`

The public model namespace is one of the strongest pieces of the refactor:

- `ModelSpec` from `eegle/ml/registry_types.py`
- `register_model_spec`, `unregister_model_spec`
- `get_model_spec`, `resolve_model_kind`, `list_model_specs`, `list_model_kinds`
- `load_entry_point_model_specs`
- `ModelContract`, `PreprocessingContract`, `TargetContract`
- `ModelBundle`
- `write_model_bundle`, `load_model_bundle`, `load_model_bundle_object`,
  `snapshot_model_bundle`
- `CalibrationState`, `CalibrationAdapter`,
  `read_calibration_state`, `write_calibration_state`

`eegle/models/contracts.py` is especially important. `ModelContract` v2
declares:

- input kind
- channel names and required channels
- missing-channel policy
- sample rate and tolerance
- input units
- epoch window and prediction horizon
- preprocessing assumptions
- tensor layout
- target semantics
- causality
- latency budget
- adaptation permissions

`eegle/models/registry.py` supports dynamic registration through Python entry
points in the `eegle.models` group. This is exactly the mechanism needed to
keep BENDR, LaBraM, BIOT, and Braindecode integrations outside EEGle core.

Readiness: high for public alpha, with two caveats:

- The model bundle schema is strong but should be published as a versioned JSON
  schema.
- Some legacy bundle functions still exist in `eegle/realtime/classification.py`
  for compatibility. Keep wrappers during `0.1.x`, then make
  `eegle.models.bundles` the single implementation.

### `eegle.protocols`

`eegle/protocols/spec.py` adds:

- `ScientificProtocol`
- `ProtocolTarget`
- `write_protocol`
- `load_protocol`

This is early but strategically correct. A scientific protocol file should
declare the task, endpoint, prediction window, prediction horizon, target
definitions, splits, baselines, and metrics before a run. This will help EEGle
become a reproducible experiment library rather than just a runner.

Readiness: early. Keep it in the public alpha API, but document it as evolving.

### `eegle.pipelines`

The current recipe workflows are:

- `alpha8`
- `inhibition8`
- `classify8`

`classify8` is the most important recipe today. It implements:

- `collect`: collect Go/No-go calibration data and export epochs.
- `train`: train frozen participant model bundles.
- `online`: run observe-only primary plus shadow classifiers.
- `demo`: marker-driven classroom dashboard demo using
  `realtime/demo_predictions.jsonl`.
- `evaluate`: replay, score, and generate reports.

These workflows should remain installable console scripts, but they should be
documented as recipes over the core API. The reusable primitives belong in
`core`, `streams`, `realtime`, `models`, `protocols`, and `analysis`.

Readiness: useful for labs, medium for public API. The workflow is coherent,
but too specific to be the core package identity.

### `eegle.tasks`

`eegle/tasks/registry.py` lists:

- `pvt`
- `go_nogo`
- planned `n_back`
- planned `sternberg`
- planned `anti_vea`

PVT and Go/No-go are scaffolded and closed-loop-ready. These should be shipped
as examples or recipes, not as the essence of EEGle. The task API should be a
way to plug task apps into the EEGle event/session/realtime contract.

Readiness: medium. Good for demonstration and lab use, but public docs should
not imply that EEGle is only a PVT/Go-No-go package.

### `eegle.analysis`

Analysis modules include session summaries, HTML reports, ERP/P300 analysis,
alpha validation, classifier evaluation, and replay. The core public value is:

- `evaluate_classifier_session()`
- `replay_classifier_session()`
- `ReplayResult`
- session report generation
- ERP/alpha reporting as optional analysis extras

Readiness: medium-high. The replay and classifier evaluation story is strong.
MNE/Matplotlib-heavy analysis should remain optional.

### `eegle.hardware`, `eegle.preflight`, And `eegle.runtime`

These modules support real operator deployments:

- package and console-command checks
- LSL discovery
- Enobio/NIC2 and Neuracle profile checks
- display readiness
- realtime worker readiness
- training dependency checks
- macOS and Windows runtime cache handling

This is valuable, but hardware-specific support should be documented as an
operator layer. EEGle core should own the abstractions and capability checks,
while device-specific details can move toward optional support modules or
plugins once there are more device families.

Readiness: good for current lab workflows, medium for public library
generalization.

## 2. Structure Of An Experiment Design Using EEGle

An EEGle experiment has five layers.

### 1. Protocol

For reproducibility, the experiment should start with a protocol declaration.
The emerging API is `eegle.protocols.ScientificProtocol`, which captures:

- protocol name
- task
- primary endpoint
- prediction window
- prediction horizon
- targets
- splits
- baselines
- metrics

This is the right top-level scientific object, but it still needs examples and
schema docs.

### 2. Config

Configs live in `configs/*.json`. They define:

- participant/session defaults
- task parameters
- output behavior
- hardware expectations
- marker stream settings
- process orchestration
- realtime processor settings
- model settings
- feedback/policy settings
- analysis settings

`eegle.config.load_config()` loads a JSON config. `ForwardExperimentRunner`
then writes the effective config to the session as `parameters.json`.

For public usage, the docs should show both:

- config-driven experiments through `eegle run-forward`
- import-driven experiments through `create_session()`, stream helpers,
  epoching, model training, replay, and reports

### 3. Session

`create_session()` creates a run directory under:

```text
data/participants/<participant-id>/sessions/<date>/<experiment-id>/<task>/run-<timestamp>/
```

The session folder is the durable unit of reproducibility. It contains the
effective parameters, manifest, raw EEG, events, markers, calibration outputs,
realtime predictions, worker logs, reports, and summaries.

This layout should be treated as a versioned public contract.

### 4. Runtime Data Path

The live data path is:

```text
task stimulus
  -> task event log and LSL marker
  -> EEG LSL stream
  -> recorder and realtime processor
  -> ring buffer and causal preprocessing
  -> marker-locked epoching
  -> model adapter prediction
  -> decision policy
  -> feedback/action emitter
  -> session artifacts
  -> offline replay and reports
```

`docs/ARCHITECTURE.md` already explains this architecture well. For public docs,
the same idea should become a shorter "How EEGle thinks about experiments"
tutorial.

### 5. Derived Datasets And Models

Offline or live epoch extraction writes:

```text
realtime/epochs/
  epochs.npz
  epochs.jsonl
  manifest.json
```

Training writes content-addressed bundles:

```text
models/classifier/<model-kind>/
  manifest.json
  metrics.json
  model.joblib or model.pt
```

Online testing snapshots model bundles into the session, writes normalized
prediction rows, and later joins truth only in analysis.

This is the strongest reproducibility loop in EEGle:

```text
session -> epochs -> model bundle -> online predictions -> replay -> metrics
```

## 3. Building A Fully Closed-Loop BCI Testing Suite With EEGle

A full closed-loop BCI suite should be built as a layered workflow, not as one
giant script.

### Step 1. Declare The Scientific Question

Create a `ScientificProtocol` that declares:

- the task, such as SART, Go/No-go, or a custom task
- the target, such as condition, attention lapse, P300 presence, or workload
- the prediction window and horizon
- train/test splits
- baselines
- required metrics
- latency and false-positive criteria

This protocol should be saved into the session or project manifest.

### Step 2. Configure The Experiment

Start from a JSON config like `configs/forward_go_nogo_classifier8.json` or a
new public example config. Configure:

- LSL EEG stream matching
- marker stream name/type/source ID
- session root
- task parameters
- realtime epoching window
- preprocessing
- model kind
- shadow models
- decision policy
- feedback mode
- dashboard/report settings

For public use, configs should be paired with Python examples so users can see
the importable API behind the CLI.

### Step 3. Collect Calibration Data

Use `classify8 collect` or the lower-level runner:

```bash
eegle run-forward --config configs/forward_go_nogo_classifier8.json --task go_nogo
eegle extract-epochs --session-dir <session>
```

This captures raw EEG, task events, markers, and model-ready epochs. For a new
task, the essential contract is: write stimulus timing and behavior to the
session, emit LSL markers, and make those markers epochable.

### Step 4. Train Baselines And Bundles

Use:

```bash
eegle train-model --kind erp_roi_logreg --session-dir <session> --output <bundle-dir>
```

or:

```bash
classify8 train --session-dir <calibration-session>
```

The current trainable baselines are useful:

- `erp_roi_logreg`
- `pyriemann_erp_cov`
- `sklearn_flatten_lda`
- `torch_eegnet`

Foundation models should initially be shadow-only external adapters registered
through plugin packages.

### Step 5. Run Online With Primary And Shadow Models

Use `classify8 online` today:

```bash
classify8 online \
  --participant sub-001 \
  --model-dir <calibration-session>/models/classifier \
  --primary erp_roi_logreg \
  --shadow pyriemann_erp_cov \
  --shadow torch_eegnet
```

The realtime worker loads a primary model and optional shadow models, applies
quality gates, sanitizes metadata before inference, writes predictions, and
emits policy actions. The observe-only default is correct for research safety.

### Step 6. Close The Loop Conservatively

Closed-loop actions should be mediated through `TaskAction`, not direct task
mutation. The policy layer already supports:

- `observe_only`
- bounded task adaptation
- stimulation candidate gating

`AttentionLapseStimulationPolicy` requires explicit `allow_stimulation`,
`research_safety_ack`, valid quality checks, refractory periods, non-practice
trials, and probability thresholds.

For a public closed-loop suite, this layer should be documented as:

```text
model prediction -> policy gate -> explicit TaskAction -> task boundary
```

No realtime model should silently mutate task state or model weights.

### Step 7. Replay And Evaluate

After the online run:

```bash
eegle replay-classifier --session-dir <online-session>
eegle evaluate-model --session-dir <online-session>
```

or:

```bash
classify8 evaluate --session-dir <online-session>
```

Replay reconstructs marker-locked epochs from captured EEG and markers, reloads
session-snapshot bundles, and compares replay predictions to online rows.
Evaluation joins predictions to the canonical stimulus manifest and reports
threshold-sensitive metrics at the calibrated operating point, while preserving
default `0.5` metrics for comparison.

### Step 8. Generalize Beyond Go/No-go

To build a suite, EEGle needs a documented task adapter contract:

- task emits markers
- task writes a stimulus manifest
- task consumes `TaskAction` only at deterministic boundaries
- task logs accepted/rejected/applied actions
- task never passes labels into inference metadata

That is the missing bridge from "classify8 works" to "others can build BCI
testing systems easily."

## 4. Is EEGle Clear For Users?

EEGle is much clearer than a typical research repo, but not yet clear enough
for a broad public audience.

What is clear:

- The README explains installation, console scripts, Windows forms, optional
  extras, command guide, and session paths.
- `docs/ARCHITECTURE.md` explains the runtime data path.
- `docs/MODEL_TRAINING_TESTING_GOALS.md` explains the classifier workflow and
  research limits.
- `docs/api/PUBLIC_API.md` identifies the intended public namespaces.
- Tests document important scientific invariants.

What is still unclear for public adoption:

- The first-page value proposition should be shorter and more explicit.
- The docs should show an import-first quickstart, not just CLI usage.
- The difference between "stable public API", "recipe", "operator workflow",
  and "experimental model integration" needs to be visible.
- `classify8` is valuable, but it can accidentally look like the whole product.
- Model bundle, epoch dataset, telemetry, prediction row, and protocol schemas
  need reference docs.
- Examples exist under `examples/`, but they need to become tutorial pages.
- The worker launch path still assumes `PROJECT_ROOT` in
  `eegle/feedback_manager.py` and `eegle/runtime.py`, which is practical in a
  checkout but should be hardened for installed-package usage.

Public clarity score: good for an alpha research library, not yet polished for
unassisted external adoption.

## 5. Core Use Case

The core use case is:

> Build reproducible realtime EEG/BCI experiments where a task, markers, EEG,
> causal preprocessing, model inference, closed-loop policy actions, replay,
> and evaluation are all captured as auditable session artifacts.

The flagship workflow should be:

```text
collect calibration data
  -> extract causal epochs/windows
  -> train classical and compact neural baselines
  -> package frozen model bundles
  -> run primary plus shadow models online
  -> emit conservative actions
  -> replay exact inputs
  -> evaluate latency, false positives, accuracy, and coverage
  -> generate reports
```

This is not another MNE, Braindecode, PsychoPy, or stimulation package. EEGle
should integrate with those systems while owning the reproducibility layer
around realtime experiments and model evaluation.

## 6. Broadening And Generalizing Features

EEGle can broaden its value without losing focus by generalizing around
scientific primitives.

High-value generalizations:

- Versioned session, epoch, prediction, bundle, calibration, telemetry, and
  protocol schemas.
- A task adapter contract independent of PVT and Go/No-go.
- A stream adapter contract independent of LSL, while keeping LSL first-class.
- Dataset adapters for BIDS EEG and XDF.
- Model adapter plugins through `eegle.models` entry points.
- A generic `ExperimentRunner` or `RealtimeRunner` public API that can be used
  without the CLI.
- A standard replay API for classifier replay and future alpha/event-feature
  replay.
- A benchmarking/reporting layer that compares primary and shadow models on the
  same captured epochs.
- A calibration API that supports explicit state export/load and replayable
  updates.
- More target schemas: condition, attention lapse, workload, ERP/P300,
  SSVEP/frequency response, motor imagery, and continuous scores.

Generalizations to avoid inside core:

- Full model zoo ownership.
- Heavy foundation-model training code.
- Task-specific dashboards as public APIs.
- Device-driver sprawl.
- Clinical or stimulation claims.
- Hidden online adaptation.

## 7. Boundary: Core EEGle Versus Other Repos

### Keep In EEGle Core

Keep these as core library features:

- session layout and provenance
- config loading and effective-parameter snapshots
- artifact hashing
- telemetry/event schemas
- stream discovery and recorder interfaces
- simulated streams
- marker events
- ring buffers and causal preprocessing
- epoch/window extraction
- model prediction contract
- model registry and specs
- model contract v2
- model bundles
- calibration state and adapter protocol
- deterministic replay
- standard metrics and threshold calibration
- conservative decision policies
- public CLI wrappers
- examples and tutorials that run without hardware

### Keep As Recipes In This Repo

These can remain in EEGle, but should be labeled recipes:

- `alpha8`
- `inhibition8`
- `classify8`
- PVT task
- Go/No-go task
- SART or other task recipes when added
- dry-run demos
- classroom dashboard demo

They are useful proofs that the core works, but they should not drive the core
API design.

### Move To Plugin Or Adjacent Repos

Good candidates for plugin packages or adjacent repos:

- `eegle-foundation-bendr`
- `eegle-foundation-labram`
- `eegle-foundation-biot`
- `eegle-braindecode`
- heavy checkpoint downloaders
- foundation-model fine-tuning pipelines
- hardware-specific stimulation drivers
- lab-specific dashboards
- venue-specific task apps
- real-time generated stimulus/image pipelines
- device-family packages beyond generic profiles, once support grows

The boundary rule:

> EEGle core owns reproducible realtime experiment infrastructure and model
> evaluation contracts. Plugins own heavy models, specialist hardware, and
> lab-specific applications.

## 8. EEGle Overengineering

Current or potential overengineering:

- Too many responsibilities in one source tree: runner, workers, tasks,
  dashboards, analysis, training, hardware checks, and model placeholders.
- Compatibility wrappers can become clutter if they are not assigned a
  deprecation window.
- Foundation-model registry placeholders may signal more support than exists.
  They should be explicit shadow-only external adapter specs until real plugins
  exist.
- Protocol objects can become abstract overhead if they are not tied to actual
  examples and validation.
- `eegle/realtime/models.py` is large and mixes adapter construction, training,
  metrics, and artifact handling.
- `eegle/realtime/classification.py` duplicates model bundle helpers now also
  present in `eegle/models/bundles.py`.
- Inline dashboard HTML/JavaScript in worker code is useful for iteration but
  not ideal as a public library surface.
- A full worker/process orchestration stack is heavy for users who only want to
  extract epochs or evaluate model bundles.

The fix is not deletion. The fix is labels, boundaries, wrappers, and docs:
core APIs stay small, recipes stay available, and optional integrations move
outward.

## 9. EEGle Underengineering

Current underengineering:

- Public import-first workflows are still thin compared with CLI workflows.
- Versioned JSON schemas are not yet published for the major artifacts.
- Worker subprocess launch still relies on repository-root assumptions through
  `PROJECT_ROOT`, `cwd=str(PROJECT_ROOT)`, and `PYTHONPATH` injection.
- Base install compatibility needs continuous CI enforcement so optional
  dependencies do not leak into core imports.
- The package is Python 3.10 only. This is acceptable for lab reproducibility
  today, but public adoption will eventually need a broader tested Python
  range.
- `eegle.streams` is a facade, not yet a complete abstraction layer.
- `eegle.protocols` is typed but not yet integrated into runs, reports, and
  validation.
- Task integration is implicit. External users need a documented task adapter
  guide.
- There is no published BIDS/XDF canonical raw-data path yet.
- Model bundle documentation needs schema examples, validation behavior, and
  migration notes.
- Privacy and data-handling docs should be stronger because `data/` may contain
  sensitive EEG and behavior.
- Plugin loading has unit coverage for registration, but the project should add
  an installed-entry-point integration test.

These gaps are solvable and do not undermine the core thesis. They define the
next release work.

## 10. What EEGle Does Best, And How To Improve It

EEGle does best at making realtime EEG experiments auditable.

The strongest chain is:

```text
task marker
  -> raw EEG/session artifact
  -> causal epoch
  -> model contract
  -> frozen bundle
  -> prediction row
  -> conservative action
  -> replay
  -> metrics/report
```

Specific strengths:

- The session layout is concrete and reproducible.
- The epoch export protects raw data with before/after hashes.
- Model input metadata is sanitized before inference.
- Prediction rows record `ground_truth_joined_during_inference=False`.
- Online prediction and demo prediction files are separate.
- Model bundles validate artifact and bundle hashes.
- Threshold-calibrated metrics preserve default-threshold comparisons.
- Primary and shadow model roles support model comparison without task changes.
- Stimulation is gated as a candidate action, not an automatic control claim.
- Cross-platform command docs and CI are now part of the packaging story.

Ways to make the core function better:

- Publish formal artifact schemas.
- Make session creation, epoch extraction, bundle loading, replay, and reports
  the center of the docs.
- Add a public `RealtimeRunner` or `ExperimentRunner` API that does not require
  users to learn worker internals.
- Split model training into `eegle.models.training` or `eegle.models.adapters`
  while keeping compatibility imports.
- Deduplicate bundle helpers into `eegle.models.bundles`.
- Promote `eegle.protocols` from a side object into session manifests and
  reports.
- Add hardware-free tutorials that complete the full calibration-to-replay loop.
- Add BIDS/XDF adapters so EEGle can sit cleanly beside MNE/Braindecode.
- Keep foundation models shadow-only until they pass contract, latency, and
  replay validation.

## 11. What EEGle Is Missing

For public pip release quality, EEGle is missing:

- A polished docs site.
- A one-page "What EEGle is and is not" overview.
- Import-first tutorials for:
  - creating a session
  - writing markers
  - extracting epochs
  - training a bundle
  - replaying a classifier session
  - registering an external model adapter
- Versioned JSON schemas for:
  - session manifest
  - epoch manifest
  - prediction rows
  - model bundle
  - model contract
  - calibration state
  - scientific protocol
  - telemetry rows
- A stable task adapter guide.
- A stable stream/recorder adapter guide.
- BIDS EEG and XDF import/export support.
- More explicit package-resource handling for shipped configs and examples.
- Installed-package worker launch hardening.
- Optional dependency CI jobs for `runtime`, `analysis`, `ml`, and `torch`.
- Plugin integration tests for Python entry points.
- Release checklist for PyPI/TestPyPI.
- Governance docs for compatibility, deprecation, schema migration, and
  contributor expectations around scientific claims.
- Benchmark datasets or synthetic fixtures for model comparisons.
- Public safety language around closed-loop policies and stimulation.

## Feature Readiness Matrix

| Area | Value | Readiness | Recommendation |
| --- | --- | --- | --- |
| Session layout | Very high | High | Stabilize as versioned public schema |
| Config loading | High | Medium | Add public examples and package-resource defaults |
| Telemetry | High | Medium-high | Publish schema and event glossary |
| LSL stream helpers | High | Medium | Keep optional, harden stream abstraction |
| Simulated EEG | High | Medium-high | Use heavily in tutorials and CI |
| Realtime epoching | Very high | High | Keep central; document as causal epoch API |
| Causal preprocessing | High | Medium | Add contracts and validation docs |
| Model contracts | Very high | Medium-high | Publish schema, add more validation |
| Model registry | High | Medium-high | Add entry-point integration tests |
| Model bundles | Very high | High | Deduplicate implementation and document schema |
| Classical baselines | High | Medium-high | Keep as built-in baseline adapters |
| EEGNet baseline | Medium-high | Medium | Keep optional under `torch` extra |
| Foundation placeholders | Medium | Early | Move real integrations to plugins |
| Calibration state | High | Early-medium | Tie into realtime/replay manifests |
| Attention-lapse targets | High for research | Medium | Keep, but document target assumptions |
| Decision policies | High | Medium-high | Keep conservative and action-record based |
| Stimulation candidate policy | Medium | Early | Keep gated, non-claiming, and optional |
| `classify8` | Very high as recipe | Medium-high | Keep as flagship recipe, not core identity |
| Dashboards | Medium | Medium | Treat as observers, not core APIs |
| ERP/alpha reports | Medium-high | Medium | Keep optional analysis extras |
| Preflight/hardware checks | High | Medium-high | Keep operator layer, avoid core coupling |
| Protocol files | Very high potential | Early | Integrate with sessions and reports |

## Recommended Next Refactor Stages

1. Stabilize docs and package story.
   - Make `docs/OPEN_SOURCE_LIBRARY_REVIEW.md` feed the README, API docs, and
     tutorials.
   - Add "stable API versus recipe" labeling.

2. Deduplicate public implementations.
   - Make `eegle.models.bundles` the single bundle implementation.
   - Keep `eegle.realtime.classification` imports as compatibility wrappers.

3. Promote import-first examples.
   - Add examples that use `eegle.core`, `eegle.streams`, `eegle.realtime`,
     `eegle.models`, and `eegle.protocols` directly.

4. Publish schemas.
   - Start with model bundle, model contract, epoch manifest, prediction row,
     and protocol.

5. Harden installed-package runtime.
   - Remove assumptions that workers run from the source checkout.
   - Resolve configs through package resources or explicit paths.

6. Split heavy integrations outward.
   - Keep built-in classical baselines and compact EEGNet.
   - Move BENDR, LaBraM, BIOT, Braindecode, and large checkpoint logic to
     optional plugin packages.

7. Build a tutorial BCI loop.
   - Simulated stream
   - Dry-run task
   - Epoch extraction
   - Model training
   - Online observe-only run
   - Replay
   - Report

## Final Positioning

EEGle should be released as:

> A Python library for reproducible realtime EEG/BCI experiments and model
> evaluation, with first-class session provenance, causal epoching, model
> bundles, calibration, replay, and conservative closed-loop policies.

That is a clear and valuable niche. The project should not shrink into a toy
example, but it should make its center of gravity unmistakable. Keep the lab
workflows. Keep `classify8`. Keep the dashboards and alpha/inhibition recipes.
But package the stable scientific primitives so external researchers can build
their own BCI systems without inheriting every local experiment assumption.
