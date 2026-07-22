# EEGle Migration Status

**Status:** Living project and decision tracker  
**Snapshot date:** 2026-07-22  
**Branch:** `sep`  
**Architecture authority:** [EEGLE.md](EEGLE.md)  
**Execution plan:** [MIGRATION.md](MIGRATION.md)

This file records the current state of the clean-break EEGle migration. It is
optimized for future implementation work: what is settled, what was verified,
what is next, what is blocked, and which questions still require a deliberate
decision. Update it as tasks are completed or decisions change. Do not use it to
restate the full architecture or phase plan.

## 1. Current position

| Field | Current value |
|---|---|
| Active phase | Phase 3 — single execution engine (ready, not started) |
| Phase status | Phases 0–2 complete; Phase 3 scheduling decisions are next |
| Code migration started | Yes |
| Product vision | Accepted and documented |
| Migration model | Selective preservation and clean rebuild |
| Compatibility policy | No general legacy adapters or old-CLI guarantee |
| Initial reference modality | EEG |
| Current target-branch runtime | Python 3.11+; legacy workflows originated on Python 3.10 |
| Target kernel runtime | Python 3.11+ with no artificial upper bound |

Phases 0 and 1 established the authority, inventory, and accepted scientific
behavior. Phase 2 now provides the typed modality-neutral records, canonical
locks, executable plugins, evidence primitives, processing capabilities, clean
package boundaries, and Python 3.11+ base. The single engine has not started.

## 2. Verified baseline

The following describes the repository immediately before these three migration
documents were added:

- branch `sep` was active;
- the worktree was clean;
- `python3 -m unittest discover -s tests` passed 252 tests;
- `python3 -m compileall -q eegle tests examples` completed successfully;
- the current package contains roughly 32,800 lines of Python;
- the largest modules are predominantly study recipes, tasks, reports, and
  combined realtime/model implementations rather than a small execution kernel.

After the first Phase 1 implementation slice:

- `python3 -m unittest discover -s tests` passes 258 tests;
- the Phase 1 manifest describes eight non-sensitive synthetic or structural
  behavior fixtures spanning tasks P1-001 through P1-007;
- nested `ModelContract` targets now round-trip without semantic loss;
- `eegle.models.bundles` is the sole current implementation authority for model
  bundle writing, loading, hashing, and validation;
- `eegle.realtime.classification` no longer contains a duplicate bundle
  implementation.

After Phase 0 closure:

- [PHASE0_INVENTORY.md](PHASE0_INVENTORY.md) assigns a disposition to every
  current top-level package, root module, and major implementation area;
- [phase0_baseline.json](migration/phase0_baseline.json) records the exact
  commit, package sizes, public imports, commands, configs, dependencies,
  optional imports, known cycle, recipes, artifact policy, and recovery
  branches;
- the historical artifact obligation is a scoped, read-only, one-time importer
  designed after the new evidence model, not a runtime compatibility layer;
- `classify8`, `attention8`, and `dsart8` are primary reference recipes, with
  `dsart32` supplying additional deployment-scale evidence;
- eight pre-vision documents carry explicit legacy-authority notices;
- `python3 -m unittest discover -s tests` passes 265 tests and compile-all over
  `eegle`, `tests`, and `examples` succeeds.

After Phase 2 closure:

- [PHASE2_FOUNDATIONS.md](PHASE2_FOUNDATIONS.md) records the implemented
  contracts and decisions D-002, D-003, D-005, D-010, and D-013;
- dense, sparse, metadata, clock, prediction, quality, rejection, outcome,
  state, command, authorization, and receipt records are typed, versioned, and
  round-trippable;
- canonical JSON locks, immutable plan drafts, content-addressed artifacts, and
  checksummed crash-detectable evidence frames are implemented;
- an external entry-point plugin resolves, validates, constructs, and executes;
- causal stateful and retrospective future-dependent transforms have
  machine-readable incompatible capabilities;
- task environment mutation moved from `eegle.runtime` to the explicit
  `eegle.integrations.task_environment` boundary;
- the direct realtime classification/model cycle and top-level legacy facade
  imports were removed;
- a Python 3.12 minimal-base installation, built wheel, optional-import blocking
  test, 282-test suite, and compile-all check pass.

This is a behavior baseline, not an obligation to preserve every API, file,
command, or artifact layout.

## 3. Decisions locked by the architecture

These decisions should not be reopened during ordinary implementation unless
new evidence exposes a contradiction:

- The public product model is Reproduce, Record, Replay, and Validate.
- EEGle is EEG-first and neurophysiology-general.
- “Causal” means information-availability/time causality, not causal inference.
- Causal, retrospective, and oracle modes are distinct.
- `ProtocolSpec`, `SuiteSpec`, and `DeploymentSpec` compile into an immutable
  `ExecutionPlan`.
- One engine runs live, simulated, and replay sources.
- The durable result of a run is an `EvidenceBundle`.
- Runtime data and configuration are typed after compilation.
- A suite describes a domain system; it is not an arbitrary Python/DAG language.
- Scientific intent is separate from site paths, hardware bindings, permissions,
  and secrets.
- The kernel is modality-neutral; integrations declare their support level.
- Execution capture and archival raw recording are distinct.
- Model inference remains label-blind in causal execution.
- Predictions, rejections, skips, pending work, timeouts, and failures are all
  explicit.
- Primary/shadow comparison uses equivalent admitted inputs.
- Adaptation and phase changes are recorded state transitions.
- Actuator capability and site authorization/interlocks are separate.
- Structured validation is core; plotting and HTML presentation are optional.
- LSL is first-class but optional; research and task frameworks are not base
  dependencies.
- Existing recipes are evidence and inspiration, not compatibility constraints.
- Old branches and Git history—not runtime legacy layers—preserve the historical
  implementation.
- Historical recordings may later be read through a scoped, one-time importer;
  original artifacts remain unchanged and no universal reader is promised.
- `classify8`, `attention8`, and `dsart8` are the selected reference recipes;
  their present APIs, commands, paths, and package placement are not retained.
- Destructive cleanup starts only after the responsible replacement and its
  acceptance evidence exist.
- Canonical locks use explicit finite JSON, NFC Unicode, sorted keys, compact
  UTF-8, normalized signed zero, and `sha256:` digests.
- Evidence storage v1 is a manifest plus checksummed framed semantic records and
  specialized/content-addressed stores for large data.
- Plugins are executable descriptors discovered through `eegle.plugins` entry
  points; metadata-only registration is not a target plugin contract.

## 4. Current strengths to preserve as scientific contracts

Current code has already demonstrated several behaviors worth carrying into the
new design through acceptance fixtures or rewritten tests:

- classifier input metadata is label-blind;
- online execution distinguishes predicted, rejected, skipped, and pending work;
- primary and shadow roles can be scheduled with primary-first behavior;
- admitted engine inputs can be captured exactly for replay;
- replay comparisons can report no differences for supported deterministic
  paths;
- delayed-label adaptation records state hashes and can reproduce a final state;
- timebase information is preserved rather than inferred from folder names;
- DSART phase and resume ledgers show the value of explicit phase state;
- artifact manifests and model bundles use content hashes;
- observe-only execution is an established, useful operating mode.

These are target behaviors, not instructions to copy their present modules.

## 5. Known architectural debt and correctness issues

| ID | Issue | Consequence | Planned phase |
|---|---|---|---|
| K-001 | **Resolved 2026-07-22:** nested model target metadata previously converted the target dictionary to a string and reset labels. | Exact nested semantic round-trip is now covered by a regression test. | Phase 1 |
| K-002 | **Resolved for the target foundation 2026-07-22:** executable descriptors now combine schemas, ports, capabilities, versions, provenance, and factories. The old model registry remains legacy recipe code. | Independent plugin wheels can participate without editing EEGle; legacy registry removal remains Phase 6 cleanup. | Phase 2 |
| K-003 | `SessionPaths` hard-codes recipe-specific artifact names and directory structure. | New suites inherit unrelated study assumptions and artifact collisions remain likely. | Phase 4 |
| K-004 | Task-specific epoch, label, condition, and inhibition semantics are embedded in nominally general realtime modules. | Core reuse is constrained and label leakage is harder to reason about. | Phases 2–3 |
| K-005 | **Partially resolved:** new model predictions/roles and plugin boundaries are clean, while the legacy combined realtime model/training module remains. | Target imports are independent; legacy backend and training decomposition remains Phase 6. | Phases 2 and 6 |
| K-006 | **Resolved for target foundations:** bundle I/O has one authority and executable construction belongs to the new plugin registry. | Legacy model construction remains isolated until its clients migrate. | Phases 1–2 |
| K-007 | Present online and replay behavior is distributed across workers and analysis modules rather than one engine. | Semantic drift is likely when either path changes. | Phase 3 |
| K-008 | **Resolved 2026-07-22:** task/PsychoPy environment setup moved to `eegle.integrations.task_environment`; the new `eegle.runtime` has no global environment mutation. | The target runtime namespace is clean; the legacy integration remains explicit and removable. | Phase 2 |
| K-009 | Configuration resolves relative paths against the repository project root. | Installed-package and external-project behavior is not portable. | Phase 5 |
| K-010 | **Partially resolved:** causal and retrospective transforms now declare incompatible machine-readable capabilities. | Direct validation prevents causal use; compiler-wide enforcement remains Phase 5. | Phases 2 and 5 |
| K-011 | **Resolved 2026-07-22:** pure epoch-array helpers now break the direct realtime classification/models cycle. | Retained legacy modules are acyclic at this boundary. | Phase 2 |
| K-012 | The CLI exposes study recipes and historical workflows as if they were the general library. | Public identity remains coupled to the current lab applications. | Phase 7 |
| K-013 | **Resolved for the target branch 2026-07-22:** package metadata requires Python 3.11+ without an upper bound and declares the four base dependencies. | Initial supported-matrix enforcement remains release/CI work. | Phases 2 and 7 |
| K-014 | Process supervision, task rendering, device detection, and semantic execution are interleaved. | Core failure behavior and deployment responsibilities are difficult to isolate. | Phases 2–3 |

## 6. Decisions and decision points

### Resolved in Phase 0

| ID | Decision | Resolution |
|---|---|---|
| D-001 | Historical artifact readability | Build a scoped, read-only, one-time importer after the new evidence model exists, currently Phase 4. Preserve originals, detect supported schema families, and do not add a general runtime compatibility layer. |
| D-009 | Reference recipes | Use `classify8`, `attention8`, and `dsart8` as primary behavior inspiration and possible future external examples. Use `dsart32` as DSART deployment-scale evidence. Other recipes are historical evidence unless a later phase selects a specific invariant. |

### Resolved in Phase 2

| ID | Decision | Resolution |
|---|---|---|
| D-002 | Evidence storage v1 | Use a content-addressed bundle manifest, checksummed length-prefixed canonical-JSON semantic logs, and specialized `SampleStore`/external artifacts for dense or source-native data. |
| D-003 | Canonical serialization and hashing | Use canonical JSON v1 rules documented in `PHASE2_FOUNDATIONS.md` and SHA-256 `sha256:` digests. Reject implicit paths, arrays, bytes, non-finite values, and arbitrary objects. |
| D-005 | Plugin distribution | Hybrid: small base built-ins plus optional extras/companion wheels. Independent packages use the `eegle.plugins` entry-point group and supply complete executable descriptors. |
| D-010 | Dependency floors | Python `>=3.11`; NumPy `>=2,<3`; SciPy `>=1.14,<2`; jsonschema `>=4.23,<5`; packaging `>=24,<27`. No Python upper bound; initial intended CI is 3.11–3.13. |
| D-013 | Built-in processing scope | Maintain bounded buffers, identity, causal SOS, retrospective zero-phase SOS, generic window specs, and finite/validity quality in base. Specialized methods remain integrations/plugins. |

### Remaining open decisions

These decisions are intentionally not locked yet. Resolve them at or before the
listed decision point and record the result here and in the relevant normative
document.

| ID | Decision | Options or questions | Decision point |
|---|---|---|---|
| D-004 | Suite composition | Prefer one explicit base plus constrained overlays/JSON Merge Patch, or a small typed composition model. Avoid an inheritance language. | Phase 5 |
| D-006 | Runtime scheduling model | Event loop, synchronous deterministic loop with workers, or hybrid; define watermarks and multi-stream ordering before engine implementation. | Phase 3 |
| D-007 | Process boundary | Decide which components may be in-process, subprocess, or external and how state, health, and backpressure cross that boundary. | Phase 3 |
| D-008 | Authorization provider | Define a minimal local authorization/interlock protocol and which actions require operator confirmation. | Phase 6 |
| D-011 | Privacy classification | Define evidence field sensitivity, deployment redaction, participant identity policy, and export defaults. | Phase 4 |
| D-012 | Replay tolerances | Define default numeric comparison, nondeterminism reporting, state equivalence, and hardware-action substitution. | Phase 3 |
| D-014 | Public stability boundary | Decide which modules are public at alpha and how experimental interfaces are marked. | Phase 7 |

## 7. Immediate action board

Status values: `todo`, `in_progress`, `blocked`, `done`, or `dropped`.

| ID | Status | Task | Output or acceptance evidence |
|---|---|---|---|
| P0-001 | done | Establish normative product architecture. | `docs/EEGLE.md` |
| P0-002 | done | Establish clean-break migration phases and gates. | `docs/MIGRATION.md` |
| P0-003 | done | Establish living task, issue, and decision tracker. | This file |
| P0-004 | done | Create an explicit keep/rewrite/extract/delete inventory for every current top-level package and large module. | `docs/PHASE0_INVENTORY.md` |
| P0-005 | done | Select golden scientific behaviors and the smallest non-sensitive fixtures representing them. | `tests/fixtures/migration/phase1_invariants.json` |
| P0-006 | done | Classify existing session/artifact schemas as discardable, one-time-import, or durable. | Decision D-001 and inventory section 8 |
| P0-007 | done | Select current recipes for behavior extraction, external rewrite, or deletion. | Decision D-009 and inventory section 7 |
| P0-008 | done | Define cleanup batches that preserve a runnable branch at each checkpoint. | Inventory section 11 |
| P0-009 | done | Mark old architecture and roadmap documents as historical without deleting useful present-state material. | Eight authority notices and inventory legacy-document manifest |
| P0-010 | done | Record a dependency/import baseline for the current base package and optional workflows. | `docs/migration/phase0_baseline.json` |
| P1-001 | done | Fix and test nested `ModelContract` target serialization. | Exact typed round-trip regression passes |
| P1-002 | done | Add golden label-blind inference tests independent of recipe names. | Synthetic leakage fixture and invariant test pass |
| P1-003 | done | Add golden primary/shadow accepted-input and accounting tests. | Primary-first shared-input trace and explicit accounting pass |
| P1-004 | done | Add golden capture/replay parity and divergence-localization tests. | Exact float64 capture and localized probability divergence pass |
| P1-005 | done | Add delayed-outcome/adaptation state reconstruction tests. | State transition and restored semantic hash pass |
| P1-006 | done | Freeze current timing and phase-ledger semantics worth retaining. | Manifest records clock, observation, abort, and resume fixtures |
| P1-007 | done | Resolve or isolate duplicated model bundle/registry authorities before extraction. | Bundle implementation consolidated in `eegle.models.bundles`; target construction now belongs to the Phase 2 executable plugin registry |
| P2-001 | done | Scaffold clean target package boundaries without legacy facade imports. | Blocked-optional-import architecture test and built-wheel inspection |
| P2-002 | done | Implement first typed packet, clock, channel, model, outcome, state, and action records. | Versioned round-trip, validation, missingness, timing, and immutability tests |
| P2-003 | done | Implement executable plugin metadata/factory contract. | External entry-point fixture resolves, validates, constructs, and runs |
| P2-004 | done | Draft evidence, plan-lock, and stable-hashing schemas. | Decisions D-002/D-003, tamper tests, framing/truncation tests |
| P2-005 | done | Split causal and retrospective processing capabilities. | Capability rejection plus causal numerical/state-restoration tests |
| P2-006 | done | Establish the Python 3.11+ base dependency matrix. | Minimal Python 3.12 install, wheel metadata/import, and full suite |
| P3-001 | todo | Resolve engine scheduling model, ordering/watermarks, and process boundary. | Decisions D-006 and D-007 |
| P3-002 | todo | Implement typed execution context and component lifecycle. | Deterministic lifecycle/failure tests |
| P3-003 | todo | Implement input admission, ordering, availability enforcement, and routing. | Multi-stream virtual-time tests |
| P3-004 | todo | Implement bounded work, deadlines, cancellation, and backpressure accounting. | Explicit terminal-state tests |
| P3-005 | todo | Run primary/shadow models over shared admitted inputs with observe-only policy. | Generic synthetic vertical slice |
| P3-006 | todo | Feed captured inputs through the identical engine and compare equivalence. | Live-like/replay parity and localized divergence |

Only the next two phases should normally be expanded into fine-grained tasks.
Later phase detail belongs in `MIGRATION.md` until it becomes actionable.

## 8. Risk and blocker register

| ID | State | Risk or blocker | Mitigation |
|---|---|---|---|
| R-001 | monitoring | “Cleanup” could delete the only executable example of a subtle scientific behavior. | The inventory binds every cleanup batch to replacement evidence; Phase 1 fixtures and Git history provide separate acceptance and recovery paths. |
| R-002 | open | Freezing behavior could be misread as preserving old APIs and recipes. | Freeze only named scientific invariants and evidence; allow clean public interfaces. |
| R-003 | open | A new specification system could grow into an opaque workflow language. | Keep the domain vocabulary bounded; compile to typed ports; custom logic remains Python plugins. |
| R-004 | open | Modality neutrality could produce abstract types that are inefficient for real EEG or high-density data. | Benchmark dense EEG first, then add sparse and high-channel-count fixtures before claiming support. |
| R-005 | open | One engine could become one monolithic process. | Keep semantic execution unified while allowing explicit in-process, subprocess, and external component boundaries. |
| R-006 | open | Replay “equivalence” may be overstated for nondeterministic frameworks or hardware. | Require declared equivalence levels and report the weakest applicable guarantee. |
| R-007 | open | Action support may be mistaken for hardware safety. | Keep authorization independent, default to observe-only, and document adapter/safety boundaries. |
| R-008 | open | Evidence capture could duplicate very large raw streams. | Separate execution capture from archival stores and support content-addressed external references. |
| R-009 | open | Maintaining old and new architectures too long could recreate compatibility debt. | Use short extraction windows, phase gates, and delete superseded paths rather than adding permanent adapters. |
| R-010 | monitoring | Optional dependency imports could leak back into the base package. | A subprocess test now blocks fourteen optional families while importing every foundation package; retain it as a release gate. |

No migration blocker is currently recorded. Unresolved design choices are not
blockers until their decision point prevents the next phase gate.

## 9. Completion log

| Date | Item | Notes |
|---|---|---|
| 2026-07-22 | Product architecture recorded | Four competencies, core contracts, boundaries, and target package model established. |
| 2026-07-22 | Clean-break strategy accepted | Existing code is an evidence source; no general legacy layer is planned. |
| 2026-07-22 | Baseline verified | 252 unit tests and compile-all passed before documentation changes. |
| 2026-07-22 | Migration tracker initialized | Known debt, decisions, risks, and immediate tasks recorded. |
| 2026-07-22 | Phase 1 behavior manifest added | Eight non-sensitive invariant records cover model contracts, label blindness, role accounting, capture/replay, adaptation, timing, ledgers, and bundle integrity. |
| 2026-07-22 | Model contract serialization corrected | Nested target name, labels, mapping, and learning problem now survive payload round-trip. |
| 2026-07-22 | Model bundle authority consolidated | Duplicate realtime bundle helpers removed; `eegle.models.bundles` is the current implementation authority. |
| 2026-07-22 | Phase 1 implementation verified | 258 full-suite tests passed at the implementation checkpoint; the gate later closed with Phase 0. |
| 2026-07-22 | Phase 0 inventory completed | Every top-level package and major area classified; public surface, configs, dependencies, processes, artifacts, cycle, and recovery baseline recorded. |
| 2026-07-22 | Historical compatibility scoped | A later one-time importer will preserve originals; no legacy runtime or universal reader is planned. |
| 2026-07-22 | Reference recipes selected | `classify8`, `attention8`, and `dsart8` selected; `dsart32` retained as deployment-scale evidence. |
| 2026-07-22 | Legacy authorities marked | Eight pre-vision documents now identify themselves as legacy current-implementation evidence. |
| 2026-07-22 | Phases 0 and 1 closed | 265 full-suite tests pass and compile-all succeeds; Phase 2 is ready but not started. |
| 2026-07-22 | Phase 2 records implemented | Modality-neutral stream, clock, semantic, state, and action record schemas round-trip with explicit timing and lineage. |
| 2026-07-22 | Plugin and processing foundations implemented | External executable entry points and causal/retrospective capability validation pass. |
| 2026-07-22 | Evidence and lock foundations implemented | Canonical plan hashes, content-addressed manifests, checksummed framing, tamper detection, and truncation recovery pass. |
| 2026-07-22 | Runtime boundary cleaned | PsychoPy environment mutation moved to an integration and the realtime classification/model cycle was removed. |
| 2026-07-22 | Phase 2 closed | Minimal Python 3.12 base installation, built wheel/import, 282 tests, and compile-all pass; Phase 3 is ready but not started. |

## 10. Instructions for future Codex work

Before an EEGle migration task:

1. Read `EEGLE.md`, then the active phase in `MIGRATION.md`, then this file.
2. Confirm the current branch and worktree before editing.
3. Map the task to a task ID or add a narrowly scoped ID here.
4. Treat current code as evidence, not as the default target API.
5. Preserve a behavior only when it supports an architectural invariant,
   accepted use case, or explicitly selected artifact obligation.
6. Prefer rewriting a tangled boundary over adding a permanent compatibility
   facade.
7. Keep optional frameworks out of base imports.
8. Update task status, decisions, risks, baseline evidence, and completion log in
   the same change that materially affects them.
9. Do not mark a phase complete until every gate in `MIGRATION.md` is satisfied.
10. When implementation evidence contradicts the vision, record the evidence and
    deliberately amend `EEGLE.md`; do not silently drift from it.

For reviews, distinguish:

- **implemented:** code exists;
- **verified:** acceptance evidence passed;
- **supported:** documented and included in release tests;
- **planned:** appears only in these design documents.

This distinction prevents target architecture from being reported as current
functionality.
