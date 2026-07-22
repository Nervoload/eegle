# EEGle Phase 0 Inventory and Cleanup Boundaries

**Status:** Phase 0 disposition authority  
**Snapshot:** 2026-07-22, branch `sep`, commit `99a0b5f`  
**Architecture authority:** [EEGLE.md](EEGLE.md)  
**Migration authority:** [MIGRATION.md](MIGRATION.md)  
**Machine-readable baseline:** [phase0_baseline.json](migration/phase0_baseline.json)

This document decides how the pre-vision repository informs the clean EEGle
architecture. It is not a promise to preserve old APIs, commands, recipes, file
layouts, or runtime behavior. A disposition preserves only the responsibility
or evidence named here.

## 1. Decisions made in Phase 0

1. A **scoped, one-time historical importer** will be designed after the new
   evidence/session model exists, currently targeted for Phase 4. It is not a
   general compatibility layer and it will never rewrite original recordings.
2. `classify8`, `attention8`, and `dsart8` are the primary reference recipes.
   They contribute named scientific invariants and may later be rewritten as
   external examples. Their current package shapes and CLIs are not retained.
   `dsart32` is additional deployment-scale evidence for the DSART contract.
3. All architecture, roadmap, public-API, recipe, and operator documents that
   predate `EEGLE.md` describe the legacy implementation. They remain useful as
   behavioral and operational evidence, but are not future-design authorities.
4. Destructive cleanup is deferred until the relevant Phase 2 or Phase 3
   replacement is implemented and verified. Later artifact/config cleanup must
   wait for the phase that owns its replacement.
5. Git commit `99a0b5fef107fde5646cbf07281be1e6933dc008` and the branches recorded
   in the baseline identify the recoverable pre-migration implementation. No
   duplicate legacy package will be carried in the new runtime.

## 2. Disposition vocabulary

| Disposition | Meaning |
|---|---|
| `retain` | Responsibility and implementation boundary mostly fit; revise deliberately. |
| `extract` | Preserve an algorithm, schema idea, or testable invariant, not the wrapper. |
| `rewrite` | Responsibility belongs in EEGle, but the current boundary or semantics do not. |
| `externalize` | Useful integration, scientific method, UI, task, or deployment outside the base package. |
| `delete` | Remove after its named evidence is captured and its replacement gate passes. |

Compound dispositions are intentional. For example, `extract → rewrite` means
the current code supplies evidence or an algorithm, while the target code is a
new implementation behind a new contract.

## 3. Top-level package inventory

| Current package | Disposition | Preserve | Target boundary | Removal condition |
|---|---|---|---|---|
| `eegle.analysis` | extract, externalize, delete | Replay comparison, admission accounting, validation metrics | `eegle.replay`, `eegle.validation`, optional reports/recipe analyses | General validators exist; selected metrics have parity tests |
| `eegle.calibration` | externalize, delete | Posterior-alpha scientific method only as a plugin/example | Optional scientific-method package; general calibration lives in `eegle.models` | Plugin/example decision made and general calibration contract passes |
| `eegle.core` | extract, rewrite, delete facade | Hashing, provenance, structured config/session concepts | Direct target packages: `specs`, `compile`, `recording`, `runtime` | Target modules own each concept without facade imports |
| `eegle.devices` | externalize | LSL acquisition/marker behavior and simulated-source tests | First-party optional LSL integration | New source protocol and LSL adapter acceptance tests pass |
| `eegle.hardware` | extract, externalize | Capability declarations and preflight evidence | Core capabilities plus vendor/site integration packages | Capability protocol exists and supported profiles move outside kernel |
| `eegle.io` | extract, rewrite | Append-oriented structured events and truth-preserving timestamps | `eegle.recording` evidence writer/readers | Evidence records support sparse events and crash detection |
| `eegle.ml` | extract, externalize, delete facade | Target semantics, metrics, adapter behavior, model-family evidence | `eegle.models`, optional sklearn/Torch/Braindecode integrations | Target model contracts and external adapter tests pass |
| `eegle.models` | retain concepts, rewrite | Model contracts, bundles, calibration, roles, prediction evidence | New `eegle.models` public boundary | New bundle/contract schemas and one executable registry exist |
| `eegle.pipelines` | extract, externalize, delete | Named reference-recipe invariants and operator lessons | External examples/recipes | Each selected behavior is represented by target acceptance evidence |
| `eegle.protocols` | extract, rewrite | Scientific protocol identity, targets, acceptance intent | `eegle.specs.ProtocolSpec` | Compiler accepts typed protocol specs |
| `eegle.realtime` | extract, split, rewrite, delete | Buffers, causal processing, windows, accounting, model execution, policies | `processing`, `models`, `runtime`, `actions`, `recording` | One engine owns live and replay semantics |
| `eegle.streams` | externalize, delete facade | Source/sink protocol lessons | `eegle.streams` typed records plus optional integrations | New streams contain no imports from legacy device/workers |
| `eegle.tasks` | externalize, delete | Reference tasks and phase/state evidence | External recipe applications | Reference examples no longer import legacy kernel paths |
| `eegle.workers` | extract, rewrite, externalize, delete | Shutdown, health, append, scheduling, recorder behavior | Engine/runtime plus optional deployment supervisor/observers | Engine and explicit process-boundary contract pass failure tests |

## 4. Repository-root module inventory

| Current module | Disposition and target |
|---|---|
| `eegle.__init__`, `eegle.__main__` | Rewrite the public alpha surface in Phase 7; no compatibility promise for present re-exports. |
| `eegle.cli` | Replace with Reproduce/Record/Replay/Validate commands in Phase 7. |
| `eegle.components`, `eegle.factory` | Extract construction requirements; rewrite as executable plugin discovery and factories. |
| `eegle.config` | Extract validation/path lessons; replace with compiled typed specs. Never retain repository-root path resolution. |
| `eegle.eeg_csv` | Externalize as a legacy/CSV format adapter; do not make CSV the evidence store. |
| `eegle.experiment` | Extract lifecycle ordering and failure evidence; rewrite into the single engine and phase state machine. |
| `eegle.feedback_manager` | Split semantic runtime from deployment/process supervision; externalize site supervision. |
| `eegle.lsl` | Move into the optional LSL integration. |
| `eegle.preflight` | Extract generic capability checks; externalize OS, driver, and laboratory checks. |
| `eegle.psychopy_input` | Externalize with the PsychoPy task integration. |
| `eegle.recording_health` | Extract modality-neutral integrity/coverage checks into validation; externalize device-specific thresholds. |
| `eegle.runtime` | Delete after task-environment behavior is externalized; process-global `HOME`/PsychoPy mutation cannot enter the kernel. |
| `eegle.session` | Replace fixed `SessionPaths` with adaptive evidence/session bundles in Phase 4. |
| `eegle.telemetry` | Extract structured append/event semantics into evidence and observability records. |

## 5. Major-module extraction map

### Runtime, processing, models, and actions

| Current module(s) | Valuable content | Target action |
|---|---|---|
| `realtime/buffer.py` | Ordered bounded sample buffering | Retain algorithm; rewrite around typed batches and declared clocks. |
| `realtime/preprocessing.py` | Filtering/resampling behavior | Split causal stateful transforms from retrospective transforms; declare capabilities. |
| `realtime/epoching.py` | Window timing, readiness, rejection evidence | Rewrite as modality-neutral `WindowBuilder`; move task conditions outside core. |
| `realtime/classification.py` | Prediction records, quality and label-blind boundary | Rewrite in model/runtime contracts; preserve Phase 1 invariants. |
| `realtime/models.py` | Protocols, adapters, datasets, training and inference | Split completely. Core protocols move to `models`; framework adapters become optional; training datasets/recipes externalize. |
| `realtime/performance.py` | Primary-first scheduling and backpressure lessons | Rewrite as engine scheduling/accounting policy. |
| `realtime/event_features.py` | Staged work and capture semantics | Extract generic scheduling; externalize inhibition-specific features. |
| `realtime/online_adaptation.py` | Delayed outcomes and state transitions | Rewrite using `OutcomeResolver` and explicit adaptation records. |
| `realtime/online_labels.py` | Delayed-label behavior | Preserve as attention8 evidence; recipe semantics externalize. |
| `realtime/policy.py` | Observe-only and decision policy behavior | Rewrite in `actions`; externalize task-specific policies. |
| `realtime/task_feedback.py`, `emitters.py` | Action request/receipt boundary lessons | Rewrite generic action records; move LSL/task clients to integrations. |
| `realtime/controller.py` | Existing orchestration | Delete after the single engine replaces it. |
| `realtime/feedback.py`, `demo_classifier.py` | Demo/recipe behavior | Externalize or delete after examples exist. |
| `models/contracts.py`, `bundles.py`, `calibration.py` | Typed semantic contracts, content hashes, calibration state | Retain concepts and rewrite versioned modality-neutral schemas. |
| `models/registry.py` | Registration metadata | Replace with an executable, discoverable plugin registry. |
| `models/adapters/*` | Framework adapter shape | Externalize framework-specific code; remove facade delegation. |
| `models/metrics.py`, `models/targets.py`, `ml/*` | Metrics and target semantics | Select and rewrite general semantics; externalize training/model-family implementations. |

### Execution, recording, and validation

| Current module(s) | Valuable content | Target action |
|---|---|---|
| `workers/realtime_processor.py` | Live ordering, failure, scheduling, capture | Use as implementation evidence for `ExecutionEngine`; do not relocate the worker. |
| `workers/common.py` | Atomic/append writes, status and shutdown utilities | Extract only general crash-safe behaviors. |
| `workers/recorder.py` | LSL recording behavior | Externalize as an LSL deployment runner. |
| `workers/dashboard.py`, `offline_analyzer.py` | Observer isolation and report behavior | Externalize as optional applications. |
| `session.py`, `core/session.py` | Session identity and current artifact map | Replace with `EvidenceBundle`; use only for the historical importer. |
| `io/events.py`, `telemetry.py` | Append-oriented events and explicit timing | Rewrite as versioned evidence records. |
| `analysis/classification.py`, `reanalysis.py` | Replay comparison, divergence and accounting | Rewrite in `replay` and `validation`. |
| Other `analysis/*` | Scientific metrics and reports | Externalize task/modality analysis; retain only general validators selected in Phase 2. |

### Tasks, protocols, hardware, and recipes

| Current area | Target action |
|---|---|
| `protocols/spec.py` | Expand and rewrite as `ProtocolSpec`; separate intent from deployment. |
| `protocols/attention.py` | Externalize with the attention8 reference recipe. |
| `tasks/dynamic_sart*` | Externalize; extract truthful phase/resume/state-ledger invariants. |
| `tasks/go_nogo.py`, `tasks/pvt.py` | Externalize as examples or delete after selected behavior is covered. |
| `hardware/capabilities.py`, `eeg_device.py` | Extract generic capabilities; remove EEG-only assumptions from core. |
| `hardware/enobio.py`, `neuracle.py`, `profiles.py` | Move to vendor/site integrations. |
| `hardware/os_support.py`, `system.py` | Externalize deployment diagnostics. |
| `devices/*`, `streams/lsl.py`, `lsl.py` | Consolidate outside base as one supported LSL integration. |
| `calibration/posterior_alpha.py` | Scientific plugin/example, not base calibration infrastructure. |

## 6. Public surface baseline

The current top-level re-exports, seven console scripts, and nineteen generic
CLI command names are recorded in the machine baseline. None is selected as a
future compatibility obligation.

The present public API mixes session paths, recipe epoching, model types,
calibration, replay, and task actions. Phase 7 will expose a smaller surface
organized around:

```text
SuiteSpec / DeploymentSpec / ProtocolSpec
              ↓ compile
         ExecutionPlan
              ↓ run
         EvidenceBundle
              ↓ replay / validate
       ValidationResult
```

The `README.md` remains the operator guide for the still-running legacy code
until that code is replaced. It is not an architecture authority.

## 7. Configurations and recipe disposition

All eleven files in `configs/` are **legacy deployment and recipe evidence**.
They will not be accepted unchanged as target `SuiteSpec` files. Phase 5 may
extract source selection, channel/rate requirements, model roles, phase intent,
recording policy, and acceptance thresholds from them.

| Recipe family | Disposition | Selected evidence |
|---|---|---|
| `classify8` | Primary reference inspiration; later external example candidate | Label-blind inputs, primary/shadow equivalence, explicit work accounting, admitted-input capture/replay, observe-only policy |
| `attention8` | Primary reference inspiration; later external example candidate | Delayed outcomes, adaptation transitions/state reconstruction, model/protocol comparisons |
| `dsart8` | Primary reference inspiration; later external example candidate | Phase/suite/resume ledgers, schedule-versus-observation truth, abort/resume handling, append-oriented acquisition |
| `dsart32` | DSART deployment-scale evidence | The same contract under more channels and real hardware constraints |
| `alpha8` | Historical behavior evidence; externalize/delete | Causal spectral processing and calibration ideas only if selected by Phase 2 |
| `inhibition8` | Historical behavior evidence; externalize/delete | Staged feature scheduling only; task/feature semantics are not core |
| Forward PVT/Go-No-Go presets | Historical deployment evidence | Preflight, source capability, recorder and task lifecycle lessons |

## 8. Artifact compatibility decision

### One-time importer, not legacy runtime

Phase 4 will consider a read-only importer from explicitly supported historical
schema families into the new evidence model. The importer must:

- detect a supported family and version rather than guessing;
- verify available content hashes and record missing integrity information;
- preserve source, receive, observation, reconstructed, and derived times as
  distinct fields;
- retain the original artifact tree unchanged;
- report unsupported or ambiguous records without silently repairing them;
- identify which output fields were imported, derived, omitted, or invalid;
- emit a new evidence bundle with provenance pointing to the originals.

Candidate durable inputs are raw sample/event captures, timebase metadata,
scientific manifests, DSART ledgers, exact engine input captures, model bundle
artifacts, predictions, outcomes, and adaptation transitions. HTML, plots,
status files, debug logs, and derived recipe summaries are regenerable or
discardable unless a later scientific review identifies a specific obligation.

Current model-bundle schemas may receive a dedicated importer, but the new
runtime will not keep old bundle readers in its main execution path.

## 9. Dependency and import baseline

The current base is Python `>=3.10,<3.11` with NumPy as its only declared hard
dependency. Runtime, analysis, ML, and deep-learning extras collectively expose
LSL, PsychoPy, SciPy, MNE, pandas, plotting, sklearn, PyRiemann, Torch, ONNX,
SPECParam, and joblib. Optional imports also include pyglet.

The target decision remains:

- base Python 3.11+ with no artificial upper bound;
- NumPy, SciPy, a JSON Schema validator, and the small packaging/version utility
  selected in Phase 2 as hard dependencies;
- LSL, MNE, sklearn, Torch/Braindecode, MOABB, plotting, formats, task renderers,
  and vendor SDKs as import-optional integrations;
- no task framework, driver, model framework, or report UI imported by the base
  package.

One confirmed direct cycle exists today:

```text
eegle.realtime.classification ↔ eegle.realtime.models
```

Phase 2 removes the cycle by making predictions/contracts independent of any
adapter implementation. A fresh base-wheel import test will prevent optional
dependencies from leaking into the kernel.

## 10. Process and artifact boundaries observed today

Current process entrypoints include the task/experiment process, recorder,
realtime processor, dashboard, offline analyzer, and recipe-specific phase
workers. Process supervision and semantic execution are currently interleaved.
The target keeps one semantic engine while allowing declared in-process,
subprocess, and external adapters.

`SessionPaths` currently fixes recipe-specific names for raw EEG, events,
calibration, models, predictions, windows, actions/feedback, phase records,
reports, logs, and completion/status summaries. Phase 4 replaces this map with
an evidence manifest and typed record/store declarations. Fixed names remain
only in the historical importer.

## 11. Ordered cleanup batches

No batch is authorized solely because it appears here. Each removal requires
its precondition, focused acceptance evidence, a full test run, and a clean
import check at the checkpoint.

### Batch 0A — authority cleanup (Phase 0, now)

- Add legacy notices to pre-vision documents.
- Record package, API, CLI, config, dependency, process, artifact, and cycle
  baselines.
- Make no destructive source deletion.

Verification: Phase 0 inventory test, Phase 1 invariant tests, full unit suite,
and compile-all.

### Batch 2A — foundation facades (after Phase 2 contracts pass)

- Remove replaced facade exports in `core`, `streams`, `models/adapters`, and
  duplicate `ml`/`models` registry or target paths.
- Remove process-global task environment behavior from the kernel.
- Move LSL, PsychoPy, hardware/vendor, sklearn, and Torch imports behind tested
  integration boundaries.

Precondition: typed records, stable hashing, executable plugins, base import
tests, and processing capability declarations pass.

Phase 2 result: the precondition passes. Top-level and stream/model facade
imports were reduced, task environment mutation moved to an integration, and
the direct classifier/model cycle was removed. Remaining `core`, ML registry,
adapter, and legacy execution paths stay only while current recipe evidence
still depends on them; their responsible Phase 3 or Phase 6 cleanup gate remains
binding.

### Batch 3A — old semantic runtime (after the engine gate)

- Remove superseded realtime controller/orchestration and worker-owned semantic
  execution.
- Remove separate live/replay code paths and legacy scheduling authorities.
- Retain reference recipes only as external clients or frozen fixtures.

Precondition: one engine passes live/simulated/replay equivalence, accounting,
ordering, backpressure, state restoration, and failure tests.

### Batch 4A — fixed sessions and artifact paths

- Remove `SessionPaths` and recipe-specific artifact routing from the new
  runtime.
- Keep old path knowledge only in the one-time importer package.
- Remove legacy derived reports once their durable evidence classification is
  verified.

Precondition: `EvidenceBundle`, crash recovery, raw-store references, privacy
classification, and importer decision tests pass.

### Batch 5A — dictionary config and recipe presets

- Remove repository-root config resolution, dictionary-flow runtime code,
  hard-coded component factories, and old bundled presets.
- Publish rewritten examples only as valid compiled suites/deployments.

Precondition: compiler, lock manifest, capability/causality checks, schema
errors, and suite composition all pass.

### Batch 6A — task-specific semantics and action bridges

- Remove task labels, conditions, feature semantics, and feedback assumptions
  from general model/runtime packages.
- Move task policies, actuators, outcome resolvers, and framework adapters to
  external integrations or examples.

Precondition: general model/outcome/action contracts pass label-blindness,
authorization, receipt, and state-transition tests.

### Batch 7A — legacy commands and application surface

- Remove recipe console scripts, old generic CLI commands, old top-level
  re-exports, packaged legacy configs, dashboards, and application tasks.
- Rewrite README and public API docs against an installed clean wheel.

Precondition: the new CLI completes compile, validate, run, replay, compare, and
export smoke workflows; external examples install independently.

## 12. Phase 0 gate evidence

| Gate requirement | Evidence |
|---|---|
| Accepted architecture and migration | `EEGLE.md`, `MIGRATION.md`, and explicit user acceptance |
| Complete disposition inventory | Sections 3–5 of this document |
| Golden fixtures selected | `tests/fixtures/migration/phase1_invariants.json` |
| Artifact obligation decided | Section 8 and machine baseline policy |
| Recipe disposition decided | Section 7 |
| Historical recovery identified | Commit and recovery branches in machine baseline |
| Test/import/dependency baseline recorded | `MIGRATION_STATUS.md` and machine baseline |
| Conflicting docs cannot appear authoritative | Legacy notices on all pre-vision documents |
| Cleanup is ordered and checkpointed | Section 11 |

Phase 0 is complete: the consistency test, Phase 1 invariant tests, 265-test
full suite, and compile-all check pass with this inventory in place. This
document must be amended when later implementation evidence changes a
disposition; silent drift is not allowed.
