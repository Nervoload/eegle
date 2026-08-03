# EEGle Package Guidance

`eegle/` contains the v1 foundations plus selected Phase 6–8 extraction
evidence. The first destructive cleanup removed recipe and compatibility
facades that no target package consumed. Read
`docs/EEGLE.md`, `docs/MIGRATION.md`, `docs/MIGRATION_STATUS.md`, and
`docs/PHASE0_INVENTORY.md` before changing package boundaries. During the
active phase, also read `docs/PHASE7_AUTHORING_OPERATIONS.md` before adding
authoring, CLI, discovery, preflight, packaging, or integration code. Read
`docs/PHASE7_PUBLIC_BOUNDARIES.md` before changing package-level exports,
authoring schemas, operation diagnostics, exit codes, or YAML dependencies.

The package map below describes current code, not target ownership. New code
must follow the target package model; legacy code supplies algorithms and
acceptance evidence only as classified by the Phase 0 inventory.

## New Foundation Map

- `actions/`: commands, authorization decisions, and observed receipts.
- `authoring/`: provisional non-executable drafts, bounded named compositional
  designs, deterministic recording and exact-version built-in template
  lowering, portable deployment requirements, expansion locks,
  provenance/source maps, persistent typed Python builders, deterministic
  project export, and an optional restricted-YAML adapter.
- `compiler/`: canonical JSON/hashing and immutable execution-plan drafts.
- `operations/`: provisional project, compile, plan-only run, capability-based
  preflight, fail-closed fault-evidenced rehearsal,
  privacy-aware session/evidence projections, replay/replacement comparison,
  public-only non-overwriting export, Python/CLI diagnostics,
  canonical-to-source remapping, exit semantics, explanation/diff, and
  non-mutating repair proposals; observer failures return structured results
  by default and application services delegate to owning packages.
- `plugins/`: executable descriptors, first-party built-in factories,
  entry-point discovery, and packet/context component protocols.
- `processing/`: bounded buffers, explicit causal/retrospective transforms,
  generic windows, and quality.
- `recording/`: generic sessions, evidence bundles, framed ledgers/capture,
  namespaced artifacts, component snapshots, integrity, and store protocols.
- `runtime/`: the new deterministic semantic engine, typed execution context,
  locked construction, phase orchestration, admission/watermarks, bounded
  queueing, typed routing, work records, outcomes, and state transitions.
- `replay/`: captured sources, fresh same-engine reruns, equivalence ceilings,
  localized divergence reports, and guarded counterfactual model replacement
  through separately compiled plans.
- `validation/`: versioned result/observation/evidence-reference schemas, the
  typed protocol-metric registry, semantic evidence taxonomy, read-only
  aggregation over existing compiler/runtime/recording/replay authorities,
  and the hash-verified performance/fault qualification profile.
- `models/`: framework-neutral contracts, path-free manifests, results,
  canonical state, and deterministic hash-verified package transport. It does
  not own framework environments, training, downloads, or prediction identity.
- `specs/`: portable protocol/suite intent, site-local deployment, scheduling,
  trigger, phase, artifact, outcome-use, and action-capability declarations.
- `streams/`: revision-bound modality-neutral channels, causally available
  clock mappings, packets, and sources.
- `integrations/lsl/`: provisional first-party optional LSL source, marker,
  metadata, outlet, and detection plugins; descriptor imports remain pylsl-free.
- `integrations/mne.py`: dependency-lazy analysis/replay projections with
  explicit timing and lineage sidecars; it is never the recording authority.
- `integrations/support.py` and `research_support.json`: typed, hash-verified
  research capability claims; keep representable/adapter/validated/reference
  dimensions independent.
- `integrations/{legacy_sessions,task_environment}.py`: retained legacy
  integration evidence excluded from the wheel and target imports.

Application runs stream semantic evidence and admitted packet capture. Compact
execution may omit phase details only when no acceptance or operator decision
requires them; public engine defaults retain inspectable results. Performance
budgets belong in `validation/qualification_profile.json` with a concrete
workload, unit, limitation, and executable evidence link.

## Protected extraction evidence

- `__main__.py`: thin public entry point for the artifact-oriented Phase 7 CLI.
- `cli.py`: historical command parser retained only as source-checkout evidence;
  it is excluded from wheels and is not a target authority.
- `experiment.py`: forward task plus recording orchestration.
- `feedback_manager.py`: managed process lifecycle for recorder, realtime
  processor, dashboard, and offline analyzer.
- `session.py`: BciPy-inspired session directory creation.
- `telemetry.py`: JSONL telemetry and console routing.
- `tasks/`: PVT and Go/No-go task implementations plus registry metadata.
- `realtime/`: Phase 6/8 evidence for online buffers, preprocessing, epoching,
  model adapters, policies, delayed outcomes, and feedback emitters.
- `workers/`: process entrypoints.
- `analysis/`: post-session reports, ERP/P300 analysis, classifier scoring, and
  replay.
- `calibration/`: posterior-alpha calibration helpers.
- `devices/` and `hardware/`: LSL and hardware discovery/setup helpers.

The deleted `core`, `protocols`, `components`, `alpha8`, `attention8`,
`classify8`, `dsart*`, and `inhibition8` paths are recoverable from the Phase 0
baseline commit. Do not recreate their APIs.

## Classifier Invariants

These are frozen current scientific behaviors, not a requirement to preserve
the listed module locations:

- Keep shared epoch quality and model metadata sanitation in
  `realtime/classification.py`.
- Keep training and inference adapter behavior in `realtime/models.py`.
- Primary/shadow scheduling now belongs only to the plan-owned target runtime.
  Legacy worker behavior is extraction evidence, not an authority.
- Do not feed ground-truth labels, stimulus condition, or response correctness
  into online model metadata.

## Migration Rules

- Keep the base import independent of LSL, PsychoPy, MNE, sklearn, Torch,
  plotting, vendor SDKs, and recipe applications.
- Model packages cannot serialize Python source or arbitrary import paths.
  Verify all package bytes before materialization; framework adapters must load
  only plan-admitted artifacts through `ModelConstructionContext` and return
  `ModelResult` for the runtime-owned prediction boundary.
- New runtime records are typed and versioned; unvalidated config dictionaries
  do not flow past compilation.
- Live, simulated, and replay execution must converge on one semantic engine.
- Preserve label blindness, explicit accounting, availability times, state
  transitions, and content integrity as contracts.
- Remove rather than facade a superseded authority after its phase gate passes.
- Keep migration-only root orchestration modules out of built wheels; update the
  isolated Phase 5 packaging test whenever the approved root surface changes.
- Follow the cleanup checkpoints in `docs/PHASE0_INVENTORY.md`. Preserve the
  named Phase 6–8 evidence until its replacement acceptance test exists.

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
- Inspection, replay comparison, and export must not signal workers or mutate,
  recover, truncate, finalize, overwrite, or delete recording/training data.
  Degrade to a structured partial/unavailable result; strict CLI exit behavior
  is explicit opt-in for automation.

## Verification Pointers

```bash
python3 -m unittest tests.test_realtime_synchrony
python3 -m unittest tests.test_closed_loop_model_system
python3 -m unittest tests.test_classification
python3 -m compileall -q eegle
```
