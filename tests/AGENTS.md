# Test Guidance

The test suite uses `unittest`. Prefer focused tests while iterating, then run
broader discovery when touching shared orchestration.

## Test Map

- `test_phase0_inventory.py`: migration inventory, authority notices, baseline,
  recipe decisions, and historical compatibility policy.
- `test_phase1_migration_invariants.py`: the remaining domain-neutral
  label-blindness, capture/divergence, and delayed-adaptation evidence frozen
  before the clean rebuild. Primary/shadow scheduling is owned by Phase 5 tests.
- `test_phase2_foundations.py`: typed records, canonical locks, evidence
  framing, packet/context plugin contracts, processing causality, real external
  wheel discovery, clean imports, and dependency policy.
- `test_phase4_evidence_bundles.py`: generic sessions, namespaced artifacts,
  evidence-bundle lifecycle, component snapshots, independent execution/raw
  references, precise truncation recovery, and legacy read-only discovery.
- `test_phase5_specs_compiler.py`: portable/site specifications, bounded
  composition, typed compiler diagnostics, exact locks, and explain/diff.
- `test_phase5_plan_execution.py`: the sole target-engine acceptance surface:
  exact locked construction, generic graph routing, phase orchestration,
  non-classifier topologies, mixed dense/sparse streams, watermarks, bounded
  queues, deadlines, retry restoration, artifacts, and bundle replay.
- `test_phase5_execution_semantics.py`: durable target-shaped quality,
  cancellation, watermark, deadline, and runtime-surface acceptance.
- `test_phase5_remaining_semantics.py`: compiled triggers, mid-phase checkpoint
  restoration, timeout/acceptance decisions, outcome/action permissions, and
  the final durable Phase 5 suites using canonical Phase 6 model bindings.
- `test_phase5_packaging.py`: clean-wheel root-module exclusions and the inert
  dependency-free `python -m eegle` boundary before the Phase 7 CLI exists.
- `test_phase6_model_authority.py`: modality-neutral model contracts, path-free
  model manifests, canonical hashes, and semantic tamper rejection.
- `test_phase6_compiler_bindings.py`: suite model uses, deployment artifact
  materialization, compiled role permissions, model/plugin/state/input
  compatibility, preprocessing lineage, exact locks, and plan diff materiality.
- `test_phase6_model_runtime.py`: callable and independently installed stateful
  model plugins, contract-bound results, runtime-owned canonical predictions,
  exact input/state lineage, terminal result dispositions, and restoration.
- `test_phase6_role_semantics.py`: compiled built-in/custom role priorities,
  queue and failure dispositions, complete/incomplete comparison evidence,
  policy-input provenance, and rejection of legacy target-role scheduling.
- `test_phase6_outcomes_adaptation.py`: enrolled direct-reference outcome
  matching, bounded/terminal dispositions, checkpoint restoration, canonical
  calibration artifacts, permissioned adaptation transitions, rollback, and
  bundle replay of delayed state updates.
- `test_phase6_action_authorization.py`: deployment-owned provider and grant
  locks, observe-only defaults, bounded and pending decisions, cancellations,
  canonical command/receipt lineage, provider failure, self-authorization
  rejection, and fail-closed replay of non-simulated action services.
- `test_phase6_generality.py`: compile/run/evidence/replay proof for dense
  EEG-like, irregular slow-signal, sparse spike plus dense LFP, and multi-rate
  streams; contract rejection of false modality claims; and dependency-free
  estimator/tensor adapter boundaries without framework support overclaims.
- `test_source_boundaries.py`: target packages may not import legacy
  application, recipe, worker, task, or analysis modules.
- `eegle.plugins.testing`: reusable behavioral assertions for independently
  packaged components; tests must keep these focused on public typed contracts.
- `test_classification.py`: recipe-independent Phase 6/8 evidence for
  label-blind metadata, input contracts, model bundles, epoch quality, replay,
  rejection accounting, and training eligibility.
- `test_ml_infrastructure.py`: source-only extraction evidence for the retired
  model registry, channel contracts,
  attention-lapse targets, threshold calibration, external checkpoint adapters,
  and stimulation policy gates.
- `test_closed_loop_model_system.py`: closed-loop model system and realtime
  integration behavior.
- `test_realtime_synchrony.py`: marker timing, realtime synchrony, and process
  coordination behavior.
- `test_portability.py`: config/package portability and import-level checks.

## Common Commands

Classifier path:

```bash
python3 -m unittest tests.test_classification
python3 -m unittest tests.test_ml_infrastructure
```

Realtime/worker path:

```bash
python3 -m unittest tests.test_realtime_synchrony tests.test_closed_loop_model_system
```

Full suite:

```bash
python3 -m unittest discover -s tests
```

Compile check after structural edits:

```bash
python3 -m compileall -q eegle tests
```

Migration tests preserve named scientific claims, not old import paths, command
names, recipe layouts, or worker boundaries. When a legacy test conflicts with
an accepted clean-break phase, rewrite it around the selected invariant rather
than adding a permanent compatibility facade.

If the full suite fails because of environment-only dependencies, preserve the
focused test result in your final notes and name the blocker explicitly.
