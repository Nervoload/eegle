# EEGle Migration Status

**Status:** Living project and decision tracker  
**Snapshot date:** 2026-07-23
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
| Active phase | Phase 6 — model, outcome, adaptation, and action semantics (ready to begin) |
| Phase status | Phase 5 complete; all compiler, reference-suite, and semantic-convergence gates verified |
| Code migration started | Yes |
| Product vision | Accepted and documented |
| Migration model | Selective preservation and clean rebuild |
| Compatibility policy | No general legacy adapters or old-CLI guarantee |
| Initial reference modality | EEG |
| Current target-branch runtime | Python 3.11+; legacy workflows originated on Python 3.10 |
| Target kernel runtime | Python 3.11+ with no artificial upper bound |

Phases 0 and 1 established the authority, inventory, and accepted scientific
behavior. Phase 2 provides the typed modality-neutral records, canonical
locks, executable plugins, evidence primitives, processing capabilities, clean
package boundaries, and Python 3.11+ base. Phase 3 established engine semantic
requirements through a temporary classifier proof; that implementation is now
deleted. Phase 5's graph-bearing `ExecutionEngine` is the sole runtime
authority. Phase 4 provides the completed session, evidence, recovery,
privacy/export, external-store, historical-import, and plan-bearing bundle
replay boundary.

Phase 5 runs from separate portable and local JSON specifications through a
typed, hashed graph to exact plugin construction and a generic phase state
machine. Recording-only, preprocessing-only, calibration/model,
classifier-shaped, mixed dense/sparse, artifact/operator-gated, and retrying
graphs execute from the immutable plan and persist through the Phase 4 evidence
boundary. Graph-produced artifacts now compile as typed phase outputs, unlock
later phases only after successful publication, and persist with content hashes
and producer lineage. Compiled triggers, checkpoint restoration, timeout and
acceptance semantics, role-aware scheduling/backpressure, and outcome/action
permission checks execute in that same engine. [PHASE5_COMPILER.md](PHASE5_COMPILER.md)
records the completed boundary and the narrower Phase 6 work that follows.

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
- engine-facing built-ins consume typed packets plus execution context and emit
  typed records with causal lineage;
- a separately installed external wheel resolves from real entry-point
  metadata, validates, constructs, and passes the shared transform contract;
- stream and clock mappings have explicit revisions, clock mappings have causal
  availability, and prediction lineage carries its input-availability frontier;
- causal stateful and retrospective future-dependent transforms have
  machine-readable incompatible capabilities;
- task environment mutation moved from `eegle.runtime` to the explicit
  `eegle.integrations.task_environment` boundary;
- the direct realtime classification/model cycle and top-level legacy facade
  imports were removed;
- a Python 3.12 minimal-base installation, built wheel, optional-import blocking
  test, 285-test suite, and compile-all check pass;
- CI defines Linux Python 3.11–3.13 plus macOS and Windows Python 3.12 core
  runs, and packaging now builds under Python 3.12.

After the first Phase 3 implementation slice:

- [PHASE3_ENGINE.md](PHASE3_ENGINE.md) records decisions D-006, D-007, and
  D-012 plus the implemented runtime and replay boundaries;
- `ExecutionEngine` runs synthetic and captured sources through one deterministic
  transform/window/quality/primary-shadow/policy path;
- dense windows are materialized, bounded, availability-aware, revision-bound,
  and snapshot/restorable;
- source watermarks, total ordering, late-input rejection, bounded queues,
  deadlines, cancellation, shadow shedding, partial runs, and failure evidence
  are explicit;
- original-availability and accelerated-causal replay use fresh components but
  the identical engine class and captured source protocol;
- replay comparison is capped at the weakest component claim and localizes a
  changed shadow decision to its prediction output;
- sixteen focused Phase 3 tests pass without task, hardware, LSL, sklearn,
  Torch, MNE, or plotting dependencies;
- the full suite passes 301 tests with five environment-dependent skips,
  compile-all and `git diff --check` pass, and the built wheel contains and
  imports the new runtime/replay modules.

After Phase 3 closure:

- outcomes are consumed only at `available_time`, match explicit prediction
  identities, remain bounded by deterministic expiry/overflow, and independently
  evidence metrics/calibration/adaptation/policy eligibility without label
  leakage;
- packet, outcome, and trigger work share one locked semantic order; time
  triggers wait for causally safe source frontiers and state transitions schedule
  the same generic trigger contract;
- safe-boundary checkpoints capture engine/source/component/evidence state,
  partial status, and scheduler/clock-mapping identity, then restore only into
  fresh, matching instances from one verified prefix;
- uninterrupted and restored execution compare equivalent at the declared
  semantic level with stable semantic IDs;
- simulated command/receipt traces compare in replay and after restoration;
- 27 focused Phase 3 tests and the 330-test Python 3.12.13 full suite pass with
  five skips; compile-all, diff check, wheel build/content, and installed-wheel
  runtime/replay/action/recording imports pass.

After Phase 5 closure:

- portable protocol/suite intent and local deployment compile into the sole
  graph-bearing `eegle.execution_plan.v1` plus an independent lock manifest;
- exact descriptor-locked plugins construct and execute arbitrary active phase
  subgraphs without classifier-shaped assembly;
- scheduled/state triggers, semantic timeout and acceptance decisions,
  primary-first/shadow disposition, and fail-run/reject-newest backpressure are
  plan-owned runtime semantics;
- integrity-checked checkpoints restore a fresh runtime at a safe mid-phase
  boundary without resetting phase time, identity, admitted artifacts, trigger,
  clock, or evidence state;
- compiler and runtime boundaries require declared outcome/adaptation uses and
  exact action-capability deployment permissions;
- four durable suite families cover simulated observation, calibration and
  mixed streams, delayed adaptation, causal event windows, broader multi-rate
  admission, and authorized simulated action with bundle replay;
- all 41 focused Phase 5 tests and the 344-test Python 3.14.4 suite pass with
  five environment-dependent skips; compile-all passes.

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
| K-003 | **Resolved for the target storage model 2026-07-22:** generic sessions and namespaced artifact identities no longer derive from recipe paths. `SessionPaths` remains only an alias-registry view for selected legacy clients. | New suites can use arbitrary artifact namespaces; migration of old recipe clients remains Phase 4 cleanup. | Phase 4 |
| K-004 | Task-specific epoch, label, condition, and inhibition semantics are embedded in nominally general realtime modules. | Core reuse is constrained and label leakage is harder to reason about. | Phases 2–3 |
| K-005 | **Partially resolved:** new model predictions/roles and plugin boundaries are clean, while `eegle.models` still delegates selected contracts, calibration, registry, and target helpers to `eegle.ml`. | The realtime adapter facades are gone, but durable model semantics must move into `eegle.models` and framework/model-family registries must externalize before `eegle.ml` leaves the wheel. | Phases 2 and 6 |
| K-006 | **Resolved for target foundations:** bundle I/O has one authority and executable construction belongs to the new plugin registry. | Legacy model construction remains isolated until its clients migrate. | Phases 1–2 |
| K-007 | **Resolved 2026-07-23:** one graph-bearing engine owns live-like, recorded, and replay execution, including watermarks, bounded queues, deadlines, phase retries, bundle replay, compiled triggers, role-aware scheduling, and persisted mid-phase restoration. | No parallel classifier engine or compatibility assembly remains available to drift. | Phases 3 and 5 |
| K-008 | **Resolved 2026-07-22:** task/PsychoPy environment setup moved to `eegle.integrations.task_environment`; the new `eegle.runtime` has no global environment mutation. | The target runtime namespace is clean; the legacy integration remains explicit and removable. | Phase 2 |
| K-009 | **Partially resolved for target specifications:** portable protocol/suite objects contain no filesystem authority; deployment storage requires an explicit URI scheme and compiler artifacts use caller-supplied paths. Legacy recipe configuration still resolves project-root paths. | New compiled suites are independent of repository layout; legacy removal remains Phase 7 cleanup. | Phases 5 and 7 |
| K-010 | **Resolved for the target compiler 2026-07-23:** causal and retrospective transforms declare incompatible capabilities and exact descriptor resolution enforces the protocol execution mode before construction. | Future-dependent components cannot compile into a causal target plan. | Phases 2 and 5 |
| K-011 | **Resolved 2026-07-22:** pure epoch-array helpers now break the direct realtime classification/models cycle. | Retained legacy modules are acyclic at this boundary. | Phase 2 |
| K-012 | **Partially resolved:** recipe console scripts are gone, `python -m eegle` is an inert dependency-free notice, and legacy root orchestration modules are rejected by a real wheel-content test. | The historical CLI remains only as source-checkout migration evidence; Phase 7 still must implement the new artifact-oriented CLI and then delete that evidence. | Phases 5 and 7 |
| K-013 | **Resolved for the target branch 2026-07-22:** package metadata requires Python 3.11+ without an upper bound and declares the four base dependencies. | Initial supported-matrix enforcement remains release/CI work. | Phases 2 and 7 |
| K-014 | **Partially resolved for the target runtime:** semantic execution now carries explicit in-process/subprocess/external proxy placement independent of task or device code. | Cross-process health transport and legacy supervisor removal remain before the separation is complete. | Phases 2–3 |

## 6. Decisions and decision points

### Resolved in Phase 0

| ID | Decision | Resolution |
|---|---|---|
| D-001 | Historical artifact readability | The initial Phase 4 reader now detects the selected BciPy-style family without mutation and exposes only registered compatibility aliases. A scoped one-time bundle importer still must report imported/derived/omitted/invalid artifacts, preserve originals, and must not become a legacy execution runtime. |
| D-009 | Reference recipes | Use `classify8`, `attention8`, and `dsart8` as primary behavior inspiration and possible future external examples. Use `dsart32` as DSART deployment-scale evidence. Other recipes are historical evidence unless a later phase selects a specific invariant. |

### Resolved in Phase 2

| ID | Decision | Resolution |
|---|---|---|
| D-002 | Evidence storage v1 | Use a content-addressed bundle manifest, checksummed length-prefixed canonical-JSON semantic logs, and specialized `SampleStore`/external artifacts for dense or source-native data. |
| D-003 | Canonical serialization and hashing | Use canonical JSON v1 rules documented in `PHASE2_FOUNDATIONS.md` and SHA-256 `sha256:` digests. Reject implicit paths, arrays, bytes, non-finite values, and arbitrary objects. |
| D-005 | Plugin distribution | Hybrid: small base built-ins plus optional extras/companion wheels. Independent packages use the `eegle.plugins` entry-point group and supply complete executable descriptors. |
| D-010 | Dependency floors | Python `>=3.11`; NumPy `>=2,<3`; SciPy `>=1.14,<2`; jsonschema `>=4.23,<5`; packaging `>=24,<27`. No Python upper bound; initial intended CI is 3.11–3.13. |
| D-013 | Built-in processing scope | Maintain bounded buffers, identity, causal SOS, retrospective zero-phase SOS, generic window specs, and finite/validity quality in base. Specialized methods remain integrations/plugins. |

### Resolved in Phase 3

| ID | Decision | Resolution |
|---|---|---|
| D-006 | Runtime scheduling model | Use a deterministic synchronous semantic coordinator over virtual time. Sources publish monotonic availability watermarks; dispatch order is availability, source, stream revision, sequence, then packet identity. Typed workers/proxies may perform I/O or compute outside the loop. |
| D-007 | Process boundary | In-process is the reference. Subprocess and external components participate through typed proxies; placement is evidence-bearing deployment metadata, never a second semantic engine. Admission, deadlines, cancellation, health, backpressure, and result disposition return to the coordinator. |
| D-012 | Replay tolerances | Cap comparison at the weakest relevant equivalence declaration. Defaults are canonical bytes for bitwise and recursive `rtol=1e-7`, `atol=1e-9` for numeric; semantic and trace projections are explicit. Compare snapshot state when declared and substitute hardware with observe-only/simulated action traces. |

### Resolved in Phase 4

| ID | Decision | Resolution |
|---|---|---|
| D-015 | Session and bundle separation | A generic session owns lifecycle and a namespaced artifact registry. Each execution produces a versioned bundle with separate semantic ledger, execution-capture, archival-raw, component-state, and other artifact references. `SessionPaths` is only an explicit compatibility alias view. |
| D-016 | Integrity and recovery | Per-frame checksums and artifact digests yield structured valid/recoverable/unrecoverable results. Only an incomplete final frame can be recovered automatically, by copying the proven prefix without modifying the source. |
| D-011 | Privacy classification | Artifacts are public, pseudonymized, internal, or restricted. Safe portable export allows only public/pseudonymized content, omits source session IDs, participant pseudonyms, and external URIs by default, retains source bundle/plan hashes, rejects undeclared secret-shaped JSON, records digest-bound redaction lineage, and never performs retention deletion implicitly. |
| D-017 | External artifact verification | Structural evidence integrity and external-content availability are separate. External results are reference-only, verified, unavailable, or mismatch; only a mismatch makes bundle integrity fail. Local source-native files can be hashed and referenced without copying. |

### Resolved in Phase 5

| ID | Decision | Resolution |
|---|---|---|
| D-004 | Suite composition | Use one explicit `SuiteSpec` base plus ordered typed `SuiteOverlay` values. V1 overlays may override only existing component config and recording/validation policy keys; they cannot alter topology, identities, plugins, routes, or phases. Structural changes require a new explicit suite. |
| D-018 | Compiler/runtime join | Embed the complete typed graph in `ExecutionPlan`, construct exact descriptor-locked plugins into `PlanRuntime`, and let the sole `ExecutionEngine` execute phase-local subgraphs. Classifier requirements apply only to phases with routed model/policy components. The Phase 3 assembly and compatibility aliases are deleted. |
| D-019 | Artifact production and phase availability | Declare external and produced artifacts in `SuiteSpec`; lock producer phase/component/port, role, media type, and optional digest in the plan. Components emit typed `ArtifactPublication` graph values. Only publications from successful phases satisfy later entry gates. Semantic evidence records the reference, digest, and provenance but not materialized content; persistence stores bounded JSON under artifact sensitivity policy or preserves a verifiable external reference with producer lineage. |
| D-020 | Scheduling and checkpoint policy | Compile scheduled/state triggers and primary/shadow/backpressure disposition into the immutable plan. Execute them in the same semantic queue as packets and component work. Checkpoint only at a queue-empty source boundary; persist plan/execution identity, runtime/component state, admitted artifacts, clocks, identifiers, trigger state, original phase start, and evidence-prefix digest for integrity-checked fresh-runtime restoration. |
| D-021 | Phase 5 permission boundary | Components declare outcome uses, required adaptation use, and actuator action capabilities. The compiler requires exact independent deployment grants before construction, and runtime routing rechecks the locked use/capability. This is an execution permission boundary, not the Phase 6 hardware-safety/interlock provider. |

### Remaining open decisions

These decisions are intentionally not locked yet. Resolve them at or before the
listed decision point and record the result here and in the relevant normative
document.

| ID | Decision | Options or questions | Decision point |
|---|---|---|---|
| D-008 | Authorization provider | Define a minimal local authorization/interlock protocol and which actions require operator confirmation. | Phase 6 |
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
| P2-006 | done | Establish the Python 3.11+ base dependency matrix. | Linux 3.11–3.13 plus macOS/Windows 3.12 workflow; minimal local Python 3.12 install, wheel metadata/import, and full suite |
| P2-007 | done | Align engine-facing component protocols, built-ins, factories, and contract tests. | Packet/context transforms, factory conformance checks, built-in descriptors, and reusable contract assertions pass |
| P2-008 | done | Complete causal input lineage, clock-mapping availability, and stream revision semantics. | Round-trip and causality tests reject unavailable mappings, premature predictions, and unavailable component input |
| P2-009 | done | Replace Python 3.10 automation and prove an independent plugin distribution boundary. | Cross-platform supported-version workflow plus installed external-wheel entry-point test |
| P3-001 | done | Resolve engine scheduling model, ordering/watermarks, and process boundary. | Decisions D-006 and D-007; `PHASE3_ENGINE.md` |
| P3-002 | done | Implement typed execution context and component lifecycle. | Deterministic IDs, lifecycle evidence, snapshots, single-use engine, controlled close, and failure tests |
| P3-003 | done | Implement input admission, ordering, availability enforcement, and routing. | Multi-source virtual-time ordering and late-input tests |
| P3-004 | done | Implement bounded work, deadlines, cancellation, and backpressure accounting. | Terminal/pending work, deadline, cancellation, queue, and shadow-shedding tests |
| P3-005 | done | Run primary/shadow models over shared admitted inputs with observe-only policy. | Generic synthetic vertical slice with comparable admitted inputs |
| P3-006 | done | Feed captured inputs through the identical engine and compare equivalence. | Original/accelerated replay parity and localized counterfactual shadow divergence |
| P3-007 | done | Generalize delayed outcome delivery, bounded pending predictions, expiry, and observe-only adaptation eligibility through the engine. | Availability-only outcome lifecycle, matching/duplicate/rejection/use, bounded overflow/expiry/pending, label-blindness, and replay tests pass |
| P3-008 | done | Add general scheduled/state-triggered work and engine-level checkpoint/resume across a partial run. | Causally safe trigger/deadline/cancel/failure/reschedule tests plus integrity-checked fresh-engine restored-run equivalence pass |

Phase 3 `done` entries record semantic requirements proven by the now-retired
classifier-shaped slice. Current implementation ownership is tracked by
P5-008: every retained engine-level requirement is now present in the sole v1
engine rather than recovered through compatibility code.

| P4-001 | done | Implement generic session identity/lifecycle and namespaced artifact registry. | Hashed session/artifact manifests, namespace isolation, external references, sensitivity, aliases, and lineage tests |
| P4-002 | done | Implement versioned evidence bundle writer/reader and direct engine-result persistence. | Self-describing complete bundle round-trip and Phase 3 result assembly tests |
| P4-003 | done | Move target ledgers and capture framing into `recording`. | Typed evidence ledger, framed packet store, and isolated historical CLRE1 reader |
| P4-004 | done | Define and implement the `SampleStore` protocol. | Runtime protocol check and exact dense-packet/stream-revision round-trip |
| P4-005 | done | Add checksums, truncation classification, and non-destructive prefix recovery. | Deliberately truncated capture reports artifact/frame/proven boundary/expected/observed fields and recovers only complete frames |
| P4-006 | done | Add artifact lineage and component-state snapshots. | Producing component/version/input digests persist; canonical state hash equals its artifact digest |
| P4-007 | done | Keep selected existing sessions readable without restoring path authority. | Mutation-free read-only legacy discovery and frozen CLRE1 tests |
| P4-008 | done | Define interrupted-writer restart/finalization policy and one-time historical bundle importer. | Authorized open-run discovery/resume, non-mutating prefix recovery, restartable finalization, and mutation-free imported/derived/omitted/invalid report tests |
| P4-009 | done | Resolve privacy, redaction, retention, and export policy (D-011). | Versioned/hash-verified policies and export manifest, safe defaults, digest-bound redaction, portable-object verification, secret rejection, and non-mutating retention tests pass |
| P4-010 | done | Add source-native/large-data store evidence and migrate selected target clients off compatibility aliases. | A real 8 MiB local NWB-like source remains external and reports reference-only/verified/mismatch/unavailable precisely; target-foundation scan finds no `SessionPaths` usage |
| P4-011 | done | Make engine-produced evidence plan-bearing and replayable directly from an `EvidenceBundle`. | Engine persistence requires and embeds the immutable plan; the bundle loader restores plan, streams, exact capture, evidence, status, and replay ceiling; fresh-engine equivalence test passes |
| P5-001 | done | Implement versioned portable `ProtocolSpec`, `SuiteSpec`, and site-local `DeploymentSpec`. | Typed round trips, stable independent hashes, explicit storage URIs, secret references, and literal-secret rejection pass. |
| P5-002 | done | Resolve bounded suite composition (D-004). | Ordered typed overlays change only existing component config and recording/validation policy; topology and identity mutation are impossible in v1. |
| P5-003 | done | Implement the first compiler validation core. | Exact plugin/mode/config/resource resolution plus typed port, signal, stream, clock, phase, role, and actuator-permission diagnostics pass before construction. |
| P5-004 | done | Produce one durable explainable graph-bearing plan and independent lock manifest. | `eegle.execution_plan.v1` includes phases, placements, typed graph, component/schema/graph hashes, atomic I/O, tamper rejection, explain, and material diff; no partial legacy schema remains. |
| P5-005 | done | Add the simulated continuous observation reference suite. | Separate protocol/suite/deployment JSON compiles deterministically against simulated and live-capability source deployments. |
| P5-006 | done | Complete phase/artifact/default/policy compilation semantics. | Phase-local requirements, roles, gates, bounded transitions/retries, explicit scheduling/resource defaults, semantic timeouts, protocol acceptance, typed artifact production, checkpoint resume, and outcome/adaptation/action permissions compile into and execute from the locked plan. |
| P5-007 | done | Add the remaining reference-suite families and close the compiler-to-runtime construction boundary. | Four durable families cover simulated continuous observation, calibration/mixed streams, delayed outcome/adaptation, primary/shadow event windows, broader multi-rate admission, and authorized simulated action. The action family persists and bundle-replays with equivalent receipts. |
| P5-008 | done | Complete Phase 3 semantic convergence in the sole plan-owned graph coordinator. | The sole engine owns watermark scheduling, bounded fail-run/reject-newest queues, primary-first shadow disposition, deadlines, quality/cancellation, compiled scheduled/state triggers, integrity-checked fresh-runtime checkpoint restoration, source-substitution bundle replay, and typed outcome/action routing. No retired runtime API or semantic skips remain. |
| P5-009 | done | Close the pre-Phase-7 legacy command and root-wheel boundary. | No console scripts; dependency-free inert `python -m eegle`; custom build boundary and isolated wheel test admit only four approved root modules and reject legacy orchestration. |

Only the next two phases should normally be expanded into fine-grained tasks.
Later phase detail belongs in `MIGRATION.md` until it becomes actionable.

## 8. Risk and blocker register

| ID | State | Risk or blocker | Mitigation |
|---|---|---|---|
| R-001 | monitoring | “Cleanup” could delete the only executable example of a subtle scientific behavior. | The inventory binds every cleanup batch to replacement evidence; Phase 1 fixtures and Git history provide separate acceptance and recovery paths. |
| R-002 | open | Freezing behavior could be misread as preserving old APIs and recipes. | Freeze only named scientific invariants and evidence; allow clean public interfaces. |
| R-003 | open | A new specification system could grow into an opaque workflow language. | Keep the domain vocabulary bounded; compile to typed ports; custom logic remains Python plugins. |
| R-004 | open | Modality neutrality could produce abstract types that are inefficient for real EEG or high-density data. | Benchmark dense EEG first, then add sparse and high-channel-count fixtures before claiming support. |
| R-005 | monitoring | One engine could become one monolithic process. | Process placement remains explicit proxy metadata and the coordinator owns semantics. Cross-process transport/health must preserve the proven boundary without duplicating the engine. |
| R-006 | monitoring | Replay “equivalence” may be overstated for nondeterministic frameworks or hardware. | The comparator caps claims at the weakest declaration; simulated command/receipt substitution now passes, while real hardware remains an observe-only or trace claim. |
| R-007 | open | Action support may be mistaken for hardware safety. | Keep authorization independent, default to observe-only, and document adapter/safety boundaries. |
| R-008 | monitoring | Evidence capture could duplicate very large raw streams. | Bundle v1 separates capture from archival stores; a real external source-native fixture proves no copy and local digest verification. Broader sparse/high-channel throughput evidence remains before modality support claims. |
| R-009 | open | Maintaining old and new architectures too long could recreate compatibility debt. | Use short extraction windows, phase gates, and delete superseded paths rather than adding permanent adapters. |
| R-010 | monitoring | Optional dependency imports could leak back into the base package. | A subprocess test now blocks fourteen optional families while importing every foundation package; retain it as a release gate. |
| R-011 | monitoring | GitHub Actions run 29967855396 passes Linux 3.11–3.13 and macOS 3.12; Windows 3.12 reaches unit tests and fails two legacy path/session-name assertions. The local Phase 3 closure tree has not yet run remotely. | Preserve the exact run/job evidence, fix legacy portability separately, and do not claim a green Windows suite or weaken target engine contracts. |
| R-012 | resolved | An in-progress Phase 3 outcome/checkpoint edit temporarily called a missing `_finalize_pending_predictions()` method and blocked integrated verification. | The method and full outcome lifecycle now pass the 27-test Phase 3 module and integrated 330-test full suite. |
| R-013 | resolved | Two engine authorities could have diverged. The temporary Phase 3 implementation, `EngineComponents`, aliases, and API-shaped tests are deleted; replay now reconstructs the locked graph. | Keep Phase 6 extensions inside `PlanGraphExecutor`/`ExecutionEngine`; never restore a parallel classifier engine. |

No migration blocker is currently recorded. Phase 5 is complete over the Phase
2 foundations, converged Phase 3 engine semantics, and Phase 4 evidence
boundary. Phase 6 is ready to begin from the verified plan-owned runtime.

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
| 2026-07-22 | Phase 2 closure hardening completed | Unified typed component calls, causal lineage and revision semantics, real external-wheel discovery, and supported cross-platform automation are in place; 285 tests pass locally with five skips. |
| 2026-07-22 | Phase 3 decisions locked | Deterministic hybrid scheduling, typed process proxies, and graded replay tolerances resolve D-006, D-007, and D-012. |
| 2026-07-22 | First Phase 3 engine slice implemented | Synthetic execution, bounded windows, primary/shadow roles, observe-only policy, explicit work evidence, same-engine replay, and divergence localization pass sixteen focused tests; the 301-test full suite, compile-all, diff check, wheel build, and installed import smoke pass. Phase 3 remains open for P3-007/P3-008. |
| 2026-07-22 | First Phase 4 evidence slice implemented | Generic sessions, namespaced artifacts, EvidenceBundle v1, framed semantic/sample stores, component snapshots, external raw references, legacy read-only discovery, and precise truncation recovery pass seven focused tests. The 308-test Python 3.12 suite, compile-all, diff check, wheel build, and installed API import pass with five skips. Phase 4 remains open for P4-008 through P4-010. |
| 2026-07-22 | Phase 4 interruption and import boundary implemented | Versioned writer state, authorized resume, immutable-prefix recovery, restartable finalization, and the scoped historical importer pass thirteen focused tests. Recipe filename knowledge remains in an integration profile. The 314-test Python 3.12 suite passes with five skips; compile-all, wheel, and installed imports are verified separately. P4-008 is closed; privacy/export and source-native/client migration remain. |
| 2026-07-23 | Phase 4 privacy and external-storage gates implemented | Safe portable export/redaction/retention and a real non-copied source-native file with explicit external verification pass eighteen focused Phase 4 tests. P4-009 and P4-010 close. Plan-bearing bundle replay is implemented, but current integrated verification is blocked by R-012. |
| 2026-07-23 | Phase 4 closed | Plan-bearing bundle replay clears P4-011 and all six exit gates. Nineteen dedicated closure tests and the 320-test full suite pass with five skips; compile-all, diff check, wheel build/contents, and installed-wheel API imports pass. |
| 2026-07-23 | Phase 3 closed | P3-007/P3-008 add bounded availability-time outcomes, independent use eligibility, causally safe scheduled/state triggers, integrity-checked fresh-engine restoration, and simulated command/receipt replay. Twenty-seven focused tests and the integrated 330-test Python 3.12.13 suite pass with five skips; compile-all, diff check, wheel build/contents, and installed imports pass. K-007 is resolved for the target engine; the observed Windows matrix failures are recorded precisely rather than hidden. |
| 2026-07-23 | Phase 5 first vertical slice implemented | P5-001 through P5-005 add separate versioned specs, bounded overlays, structured compilation, typed graph/capability/clock/phase checks, v2 plans with v1 compatibility, locks, explain/diff, atomic files, and the simulated continuous reference. Thirteen focused tests and the 343-test Python 3.14.4 suite pass with five skips; compile-all, diff check, wheel contents, and installed public imports pass. Phase 5 remains open for P5-006/P5-007. |
| 2026-07-23 | Phase 5 compiler/runtime join implemented | `ExecutionPlan` now embeds its typed graph; exact locked plugins construct into `PlanRuntime`; `PlanExecutionEngine` routes arbitrary active phase subgraphs and owns transitions, gates, retries, and evidence. Nine runtime tests cover classifier, recording-only, preprocessing-only, calibration/model, dense+sparse, multi-phase, retry, drift rejection, and bundle persistence. All 22 focused Phase 5 tests and the 352-test Python 3.14.4 suite pass with five skips; compile-all, wheel contents, and installed-wheel imports pass. Phase 5 remains open for artifact production, durable reference families, and P5-008 scheduler/replay unification. |
| 2026-07-23 | Phase 5 artifact production slice implemented | `ArtifactSpec`, `PlannedArtifact`, digest locks, compiler producer/dominance checks, the `ArtifactProducer`/`ArtifactPublication` graph contract, successful-phase registration, and evidence-store materialization with lineage are implemented. A durable calibration/mixed dense+sparse reference family now compiles and executes. All 25 focused Phase 5 tests and the 355-test Python 3.14.4 suite pass with five skips. Phase 5 remains open for timeouts/checkpoints/acceptance, outcome/action permissions, the remaining reference families, and P5-008 scheduler/replay unification. |
| 2026-07-23 | Phase 3/5 engine authority consolidated | The 2,077-line classifier engine, `EngineComponents`, compatibility aliases, four stream facades, five model-to-realtime adapter/metric facades, and 27 API-shaped tests are deleted. `ExecutionEngine` now means the locked graph/phase runtime. Bundle replay reconstructs that plan without a factory; watermarks, bounded queues, and component deadlines run generically, including rejection of sources that regress behind a declared watermark. Compiler phase/artifact/role checks moved to named semantic passes, and the sole complete plan schema is `eegle.execution_plan.v1`. Study console scripts and recipe configs are no longer distribution metadata. A clean 221 KB wheel contains 112 files, excludes legacy study/task/worker/analysis packages, target-to-realtime model facades, and entry points, and passes installed target-surface imports. Thirty-two focused Phase 5 tests and the 335-test full suite pass with five skips; compile-all passes. P5-008 remains open only for triggers, persisted mid-phase restoration, and Phase 6 outcome/action suites. |
| 2026-07-23 | Phase 5 semantic and packaging hardening | The graph coordinator delegates admission, bounded queueing, typed routing, and deadline/work construction to focused modules and is reduced from about 1,100 to 827 lines. Target-shaped tests prove quality suppression, terminal pre-run cancellation, and honest runtime exports; six skipped contracts name the remaining P5-008/Phase 6 behavior. Console scripts remain absent, `python -m eegle` is an inert dependency-free notice, and an isolated real-wheel test rejects every migration-only root module. Forty-two focused Phase 5 tests run with six skips; the complete 345-test suite passes with eleven skips, and compile-all passes. The clean 196 KB wheel contains 103 files, exactly four root modules, and passes installed target-package imports. |
| 2026-07-23 | Phase 5 closed | P5-006 through P5-008 add compiled scheduled/state triggers, semantic timeout and acceptance decisions, primary-first/shadow scheduling, nonfatal reject-newest backpressure, exact outcome/adaptation/action permission checks, and tamper-detecting fresh-runtime mid-phase restoration. Durable delayed-adaptation and event-window/multi-rate/simulated-action suites compile and execute; the latter persists and bundle-replays with equivalent receipts. All 41 focused tests and the 344-test full suite pass with five environment-dependent skips; compile-all passes. Phase 6 is ready. |

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
