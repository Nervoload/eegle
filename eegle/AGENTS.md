# EEGle Package Guidance

`eegle/` currently contains the legacy implementation behind the installed
commands and the new foundations will be introduced here phase by phase. Read
`docs/EEGLE.md`, `docs/MIGRATION.md`, `docs/MIGRATION_STATUS.md`, and
`docs/PHASE0_INVENTORY.md` before changing package boundaries.

The package map below describes current code, not target ownership. New code
must follow the target package model; legacy code supplies algorithms and
acceptance evidence only as classified by the Phase 0 inventory.

## New Foundation Map

- `actions/`: commands, authorization decisions, and observed receipts.
- `compiler/`: canonical JSON/hashing and immutable execution-plan drafts.
- `plugins/`: executable descriptors, first-party built-in factories,
  entry-point discovery, and packet/context component protocols.
- `processing/`: bounded buffers, explicit causal/retrospective transforms,
  generic windows, and quality.
- `recording/`: generic sessions, evidence bundles, framed ledgers/capture,
  namespaced artifacts, component snapshots, integrity, and store protocols.
- `runtime/`: the new deterministic semantic engine, typed execution context,
  scheduling/watermark policies, work records, outcomes, and state transitions.
- `replay/`: captured sources, fresh same-engine reruns, equivalence ceilings,
  and localized divergence reports.
- `specs/`: JSON Schema validation; full suite specifications arrive later.
- `streams/`: revision-bound modality-neutral channels, causally available
  clock mappings, packets, and sources.
- `integrations/`: task/framework/site behavior excluded from base imports.

## Package Map

- `cli.py`: generic command parser and command handlers.
- `pipelines/`: top-level operator workflows that configure and call shared
  orchestration, including the abort-safe `dsart8`/`dsart32` recording suite.
- `experiment.py`: forward task plus recording orchestration.
- `feedback_manager.py`: managed process lifecycle for recorder, realtime
  processor, dashboard, and offline analyzer.
- `session.py`: BciPy-inspired session directory creation.
- `telemetry.py`: JSONL telemetry and console routing.
- `tasks/`: PVT and Go/No-go task implementations plus registry metadata.
- `realtime/`: online buffers, preprocessing, epoching, model contracts,
  adapters, policies, and feedback emitters.
- `workers/`: process entrypoints.
- `analysis/`: post-session reports, ERP/P300 analysis, classifier scoring, and
  replay.
- `calibration/`: posterior-alpha calibration helpers.
- `devices/` and `hardware/`: LSL and hardware discovery/setup helpers.

## Classifier Invariants

These are frozen current scientific behaviors, not a requirement to preserve
the listed module locations:

- Keep shared epoch quality and model metadata sanitation in
  `realtime/classification.py`.
- Keep training and inference adapter behavior in `realtime/models.py`.
- Keep online worker loading, primary/shadow model execution, and prediction row
  emission in `workers/realtime_processor.py`.
- Keep `classify8` CLI workflow wiring in `pipelines/classify8.py`.
- Do not feed ground-truth labels, stimulus condition, or response correctness
  into online model metadata.
- Do not let `classify8 demo` write real classifier prediction artifacts.

## Migration Rules

- Keep the base import independent of LSL, PsychoPy, MNE, sklearn, Torch,
  plotting, vendor SDKs, and recipe applications.
- New runtime records are typed and versioned; unvalidated config dictionaries
  do not flow past compilation.
- Live, simulated, and replay execution must converge on one semantic engine.
- Preserve label blindness, explicit accounting, availability times, state
  transitions, and content integrity as contracts.
- Remove rather than facade a superseded authority after its phase gate passes.
- Follow the cleanup preconditions in `docs/PHASE0_INVENTORY.md`; do not perform
  destructive cleanup early.

## Realtime and Worker Rules

- `FeedbackManager` owns worker startup, readiness, shutdown, and status files.
  Prefer extending that lifecycle instead of starting unmanaged processes from
  tasks.
- Realtime model decisions should become action records. Task code should accept
  or reject actions only at deterministic boundaries.
- Dashboards are observers of session artifacts. They should remain safe to
  disable and should not be required for task completion.
- Preserve replayability when changing realtime behavior. If online output
  changes, update replay and evaluation code in `analysis/` as needed.

## Verification Pointers

```bash
python3 -m unittest tests.test_realtime_synchrony
python3 -m unittest tests.test_closed_loop_model_system
python3 -m unittest tests.test_classification
python3 -m compileall -q eegle
```
