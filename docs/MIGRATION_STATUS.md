# EEGle Migration Status

**Status:** Living project and decision tracker  
**Snapshot date:** 2026-07-29
**Branch:** `split`
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
| Active phase | Phase 7 — experiment authoring, operations, packaging, and first-party integrations |
| Phase status | Active; P7-013 publication is complete, while P7-010 real EEG acceptance, P7-013A plugin tooling, and P7-014 gate closure remain pending |
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

Phase 6 began by fixing semantic ownership before adding runtime features.
[PHASE6_MODEL_SYSTEMS.md](PHASE6_MODEL_SYSTEMS.md) separates executable plugin
metadata, scientific model contracts and manifests, suite intent, deployment
materialization, compiled bindings, runtime-created predictions, component
state, outcome enrollment, and independent action authorization. The first
implementation slices replace the EEG/classifier-shaped model contract with
canonical modality-neutral contracts and compile suite/deployment/plugin/model
intent into exact plan-owned model bindings. Runtime-created prediction
envelopes, permission-driven scheduling, bounded comparison evidence,
policy-input provenance, enrolled direct-reference outcome lifecycles, explicit
adaptation state transitions, and deployment-owned action authorization are now
implemented. Policies produce requests rather than authority; the engine alone
constructs commands after exact provider and grant checks. Missing authority is
observe-only, pending decisions are causally scheduled and cancellable, and
replay refuses non-simulation action services before construction.
The P6-008 matrix now proves regular dense, explicitly timed irregular dense,
sparse-event plus dense, and multi-rate systems through the same compile,
execution, evidence-integrity, and replay path. Estimator and tensor-callable
fixtures use the normal external model boundary; no framework-specific runtime
or base imports were added.

Phase 6 closure completes runtime model admission and removes the remaining
transitional model path. Artifact-bearing factories receive only independently
digest-verified materializations; initial state is restored from a typed
artifact and checked by exact state hash. Processing compatibility is compiled
from canonical parameter attestations, every missing required model output gets
a terminal rejection, authorization providers receive immutable request
parameters rather than a digest alone, and rollback/checkpoint snapshots are
deep canonical copies. Every model now requires a manifest binding, model roles
are permission-driven only, and v1 prediction plus suite-wide primary/shadow
fallbacks are gone. The wheel excludes `eegle.ml`, old bundle/registry/model
helpers, framework integrations, and every legacy application package.

Phase 7 supplies the product boundary between researcher intent and the
canonical specifications. Templates, typed Python, restricted YAML, named
composition, presets, and project commands lower through a non-executable
draft/provenance service into `ProtocolSpec`, `SuiteSpec`, and explicit
deployment requirements before a reviewed `DeploymentSpec`; the existing
compiler and runtime remain the only semantic authorities.
[PHASE7_AUTHORING_OPERATIONS.md](PHASE7_AUTHORING_OPERATIONS.md) records the
accepted task order, rehearsal/live plan distinction, YAML dependency decision
point, user journeys, Phase 8 boundary, and closure evidence.

P7-001 now locks the first product boundary. Existing target package `__all__`
exports are stable-alpha; the new package-level `eegle.authoring` and
`eegle.operations` exports are provisional through P7-014; implementation
submodules and migration-only sources remain internal. Versioned draft,
non-hashing provenance-sidecar, and operation-error envelopes are packaged in
the base without lowering or execution behavior. The shared exit meanings are
fixed, while restricted YAML is assigned to a later `eegle[yaml]` extra using a
pinned `ruamel.yaml` series and remains absent from base imports. The boundary,
machine-readable inventory, and exact constraints are recorded in
[PHASE7_PUBLIC_BOUNDARIES.md](PHASE7_PUBLIC_BOUNDARIES.md).

P7-001 verification adds six dedicated boundary tests. Together with the
updated source-boundary and Phase 2 foundation suites, 27 focused tests pass;
compile-all and diff checks pass. The complete Python 3.14.4 run executes 280
tests: 274 pass, five environment-dependent tests skip, and the clean-wheel
test cannot start because the temporary base environment lacks the pinned
`setuptools`/`wheel` build backend, and installing those tools was unavailable
in the current sandbox. This is recorded as an environmental verification gap,
not a relaxed packaging gate; the unchanged clean-wheel test remains required
before P7-013 closure.

P7-002 implements the first bounded lowering profile. A typed
`ExperimentDraft` may preserve incomplete intent and unresolved choices, but
only complete recording intent lowers. The service deterministically produces
ordinary protocol/suite objects, source/sink routes, a single recording phase,
and typed source/clock/storage deployment requirements. Unsupported model,
action, adaptation, arbitrary graph, and site-binding values fail rather than
creating premature authorities. A separate typed sidecar binds draft and
canonical digests, distinguishes explicit values from authoring defaults, and
maps canonical schema/compiler paths back to the closest authoring source.
Runtime construction now explicitly accepts `ExecutionPlan` only.

P7-003 extends that same draft service with eight bounded, exact-version
templates: continuous recording, EEG plus events, continuous observation,
event-locked observation, primary/shadow comparison, calibration followed by
locked validation, delayed-outcome adaptation, and simulated closed-loop
action. Definitions declare finite parameter schemas, generated schema
versions, capabilities, scientific defaults, portable deployment needs,
machine fixtures, expected expansion digests, and explicit-reexpansion policy.
Draft selections bind the exact template manifest; expansion locks separately
bind explicit/effective parameters, the draft, and all generated outputs.
There is no implicit latest lookup, same-version content replacement fails, and
templates construct neither plugins nor runtime values. Every synthetic suite
passes the unchanged compiler with reviewed deployment fixtures.

P7-004 and P7-005 now expose two deliberately equivalent authoring clients.
The immutable `ExperimentBuilder` supplies exact-template constructors plus
typed signal/window/event/model/action edits, explicit defaults and portable
requirements, canonical export, non-overwriting project writing, and a compile
facade that delegates unchanged canonical values to `compile_suite()`. It has no
run facade, and runtime construction still accepts an `ExecutionPlan` only.

The optional `eegle[yaml]` adapter accepts one bounded
`eegle.template_authoring.v1` YAML 1.2 document and lowers it through the same
builder/template/draft path. It lazily imports `ruamel.yaml`, uses the pure safe
loader, rejects aliases, anchors, merges, tags, directives, duplicate keys,
implicit dates, non-string keys, non-finite values, multiple documents, and
excessive resources, and preserves JSON-Pointer line/column locations. Shared
recording and comparison fixtures prove that Python and YAML yield identical
canonical specs, requirements, and expansion identities.

P7-006 adds read-only joined explanation, difference, and guided-diagnostic
services. Every built-in reference template produces scientific-intent,
dataflow, causality, model-comparison, action-influence, and
defaults/provenance views. A matching locked plan contributes only the existing
safe compiler projection; mismatched spec hashes fail, and compiler-derived
fields remain source-free rather than becoming authoring origins.

Experiment differences label scientific, operational, presentational, and
replay-affecting impacts using canonical hashes and the existing plan diff.
Operation diagnostics add backward-compatible likely-cause, repair-description,
and finite proposal fields. Guided unit, rate, channel, clock, plugin, model,
and authorization failures preserve compiler identity, map to the closest
authoring source, and emit review-only proposals with `applied: false`.

P7-007 establishes the public operations path without adding another semantic
authority. `eegle.project.v1` indexes separately stored authoring source,
generated canonical values, a reviewed simulation deployment,
content-addressed plan/lock/explanation artifacts, and sessions. Only immutable
compiled artifacts are enforced as integrity authorities; editing mutable
authoring cannot change an already compiled run. The plan-only service verifies
the lock before session creation and delegates to the existing engine, evidence
persistence, integrity reader, and bundle replay.

The `eegle` console entry and `python -m eegle` now expose `new`, `compile`,
`explain`, `diff`, `graph`, `preflight`, `rehearse`, `run`, `inspect`, `replay`,
and `model` pack/check operations. Python
callers use the same exported services. Machine success and failure use versioned
`eegle.operation_result.v1` and `eegle.operation_error.v1` envelopes with the
fixed exit meanings. The automatic deployment remains the bounded
continuous-recording simulation. P7-008 subsequently added independent
capability detection plus immutable review-only deployment proposals, and
P7-009 added exact capability preflight plus fault-evidenced rehearsal. P7-011
adds model package builders, integrity/conformance checks, and an ordinary
optional-framework plugin proof without adding a framework runtime.

P7-011 packages the canonical manifest, content-addressed artifact bytes,
optional canonical initial state, and contract-validated synthetic vectors in
a deterministic transport envelope. Verification rejects index/member/state or
artifact tampering before content-addressed materialization. Static checks join
only installed descriptor contracts; construction and prediction remain owned
by the Phase 6 model-context and runtime boundaries. An independently built
wheel uses real scikit-learn/joblib dependencies and returns `ModelResult`
through the normal engine. Replacement replay admits a separately compiled
contract-compatible model plan while preserving every non-target authority and
reports the expected counterfactual divergence.

Six dedicated P7-007 tests and 79 focused Phase 5/7/source-boundary tests pass;
compile-all and diff checks pass. The 320-test aggregate Python 3.14.4 run has
314 passes, five environment-dependent skips, and one setup failure because
that interpreter still cannot import `setuptools.build_meta`. The unchanged
source was therefore built independently as a clean 115-file wheel with the
bundled Python 3.12 build runtime, then all nine commands completed from that
wheel under the dependency-complete Python 3.14 environment. The wheel contains
the console entry metadata and excludes historical `eegle.cli`.

P7-002 adds eight dedicated tests. Fifty-one focused authoring, boundary,
foundation, and compiler tests pass with compile-all and diff checks. The full
288-test Python 3.14.4 run has 282 passes, five environment-dependent skips,
and the same one environment-only wheel failure because its temporary
interpreter lacks the pinned build backend. The expanded clean-wheel test passes
independently while a pre-existing local pinned backend payload is available,
so P7-002 wheel contents were verified without changing dependencies. Later
temporary-directory cleanup removed that payload; the aggregate-environment
gap remains recorded rather than hidden.

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

After the Phase 6 semantic-authority checkpoint and first P6-001 slice:

- decisions D-008 and D-022 through D-029 fix the model, artifact, role,
  outcome, state, and authorization ownership boundaries;
- modality-neutral model contracts use typed ports, schema-bound outputs and
  uncertainty, explicit preprocessing ownership, declared state behavior, and
  canonical content hashes without EEG, epoch, label, or scalar-confidence
  defaults;
- portable model manifests contain logical content-addressed artifact
  references and compatible plugin requirements, never local paths, factories,
  downloads, framework imports, or environment probing;
- seven dedicated Phase 6 authority tests, the 234-test Python 3.14 full suite,
  compile-all, source-boundary checks, and the clean-wheel packaging test pass
  with five environment-dependent skips;
- the remaining direct legacy model authorities stay extraction-only until
  compiler/runtime replacements permit their deletion and minimal-wheel
  inspection at P6-009.

After P6-002 compiler model binding:

- portable suite model uses and role profiles are separate from site-local
  artifact materializations, while model manifests enter compilation through a
  digest-keyed explicit catalog;
- `PlannedModelBinding` locks the exact manifest, contract, executable plugin
  version, compiled permission set, comparison group, artifact digests and
  URIs, and input-specific preprocessing component lineage;
- common signal contracts now prove dense, sparse, irregular, channel/feature,
  per-identity unit, event-kind, missingness, layout, and duration constraints
  through the existing graph rather than a model-only type system;
- compilation rejects incompatible implementation versions, modes, state and
  replay capabilities, input/output ports, unproven or duplicated
  preprocessing, artifact digests, non-equivalent comparison inputs, and role
  influence on policies or outcomes;
- local model URI changes are operational diffs, while manifest, contract,
  role, digest, and lineage changes are scientific diffs;
- nine P6-002 tests, all 57 focused Phase 5/6/source-boundary tests, and the
  243-test Python 3.14 full suite pass with five environment-dependent skips;
  compile-all and diff checks pass.

After P6-003 runtime model binding:

- executable plugins return schema-bound `ModelResult` values with no authority
  to assign component, plugin, model, role, artifact, state, input, timing, or
  prediction identity;
- the sole graph executor validates output, uncertainty, validity, abstention,
  and completion semantics before constructing `eegle.prediction.v2` from the
  immutable model binding;
- canonical predictions record the exact plugin/model versions, manifest,
  contract and result digests, artifacts, role, immediate model input, root
  admitted inputs, causal revisions, pre-inference state digest, and timing;
- executor-owned root-input lineage survives checkpoints, while emitted,
  rejected, deadline-late, and phase-cancelled results receive explicit durable
  dispositions;
- the dependency-light `CallableModel` and an independently installed stateful
  plugin wheel run through the same compiler and engine; fresh-runtime state
  restoration reproduces the exact next prediction state;
- four P6-003 tests, all 82 focused model/compiler/runtime/source-boundary/
  packaging tests, and the 247-test Python 3.14.4 suite pass with five
  environment-dependent skips; compile-all and diff checks pass.

After P6-004 role and comparison semantics:

- built-in primary, candidate, shadow, and observer profiles plus custom roles
  compile into typed priority, policy-influence, failure, queue, outcome, and
  adaptation permissions;
- the target runtime schedules from those compiled permissions, records
  reject-newest and shed-oldest dispositions, isolates reject-result failures,
  and no longer derives target behavior from role-name strings;
- comparison groups require compatible output contracts and co-activated
  phases, then emit complete or incomplete records tied to exact prediction,
  result, immediate-input, and admitted-input identities;
- policies accept only canonical predictions emitted by the current execution
  and permitted by the exact planned model binding; suite-wide primary/shadow
  knobs are rejected for compiled Phase 6 model uses;
- five P6-004 tests and all 66 focused Phase 5/6/source-boundary tests pass. The
  252-test Python 3.14.4 suite passes with five environment-dependent skips;
  compile-all and diff checks pass.

After P6-005 outcome lifecycles and P6-006 calibration/adaptation semantics:

- only predictions enrolled by a compiled `OutcomeExpectationSpec` enter the
  bounded matcher; stable typed references replace hidden temporal matching;
- every enrolled prediction and received outcome obtains explicit delayed,
  missing, duplicate, disputed, expired, overflowed, rejected,
  retrospective-only, cancelled, or pending-at-close evidence;
- pending matcher state, terminal history, exact predictions, and deadlines
  survive checkpoints and restore without resetting session semantics;
- canonical calibration artifacts bind support inputs and outcomes to a
  content-addressed model-state artifact without local-path authority;
- adaptation requires compiled role, phase, outcome-use, model-state, plugin,
  and independent deployment permission, and records eligibility plus
  requested/applied/rejected/no-op/failed/rollback transitions;
- failed mutations restore the exact prior component-owned state, while bundle
  replay reproduces the delayed update and the next prediction;
- five dedicated P6-005/P6-006 tests, all 91 focused Phase 2/5/6/source-boundary
  tests, and the 257-test Python 3.14.4 suite pass with five environment-
  dependent skips; compile-all, diff checks, and clean-wheel packaging pass.

After P6-007 deployment-owned action authorization:

- policy output is an unprivileged `ActionRequest`; suite graph components and
  plugin results cannot create or route an `AuthorizedCommand`;
- deployment binds exact authorization-provider plugins and grants containing
  actuator, capability, parameter, request-lifetime, delivery-delay,
  decision-delay, and provider-failure bounds;
- the engine-owned broker alone creates canonical authorization requests,
  decisions, commands, cancellations, and dispositions, while actuators accept
  only the authorized envelope and return receipts tied to its decision;
- missing grants are observe-only, provider failures apply the locked
  observe-only or denied fallback, and pending decisions resolve, expire, or
  cancel through semantic scheduling without blocking the coordinator;
- runtime checks policy-owned prediction and state references against exact
  graph inputs and engine-owned state before admitting a request;
- the built-in provider and actuator are simulation-only, physical capabilities
  cannot use that provider, and replay rejects every non-simulation action
  service before invoking its factory;
- eight dedicated P6-007 tests and the 265-test Python 3.14.4 suite pass with
  five environment-dependent skips; compile-all, diff checks, clean-wheel
  packaging, existing simulated bundle replay, and adversarial physical replay
  refusal pass.

After P6-008 representational and framework-boundary generality:

- regular multichannel EEG-like data, irregular long-window fNIRS-like data,
  sparse spikes plus dense LFP, and mixed 250 Hz/1 kHz/event-rate auxiliary
  streams compile and execute through the sole graph engine;
- every fixture produces runtime-owned predictions, a verified plan-bearing
  evidence bundle, exact execution capture, and equivalent same-plan replay;
- input contracts prove content kind, regular/irregular/event rate, channel and
  unit identity, event kinds, and duration; a deliberately false irregular-to-
  regular declaration fails before construction;
- estimator `predict_proba` and tensor-callable adapters cross the same
  `ModelResult` boundary without adding framework imports, component kinds,
  tensor types, loaders, trainers, or registries to EEGle;
- the P6-003 independently installed plugin wheel remains the packaging proof;
  dependency-backed sklearn/Torch distributions and artifact loading remain
  Phase 7 integration work;
- three dedicated P6-008 tests, 62 focused Phase 2/6/source-boundary tests, and
  the 268-test Python 3.14.4 suite pass with five environment-dependent skips;
  compile-all, diff checks, clean-wheel packaging, bundle integrity, and replay
  equivalence pass.

After P6-009/P6-010 closure:

- `model_context_v1` is the only artifact-bearing model factory contract;
  deployment resolves artifacts, the runtime verifies bytes independently,
  factories receive an immutable construction context, and typed initial state
  is restored and hash-verified before admission;
- fixed and config-projected processing parameters compile into immutable
  attestations, including manifest-bound artifact preparation and route-wide
  forbidden-operation proof;
- required output omission produces a terminal rejection for every missing
  port, while provider evaluation receives immutable parameters verified
  against the persistent request digest;
- checkpoints and failed adaptations freeze independent canonical state; an
  adversarial component returning its live nested mapping still restores the
  exact prior state;
- all model components require a canonical manifest binding; the v1 prediction,
  classifier built-in, suite-wide role knobs, string-role executor fallbacks,
  metadata-only target registry, and local bundle authority are removed from
  the v1 packages;
- package discovery excludes `eegle.ml` and framework extras, while a clean
  wheel-content test rejects every superseded model module and legacy package;
- 122 focused foundation/compiler/runtime/evidence/packaging tests and the
  274-test Python 3.14.4 full suite pass with five environment-dependent skips.
  Compile-all and diff checks pass.

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
| K-002 | **Resolved for the v1 package:** executable descriptors combine schemas, ports, capabilities, versions, provenance, factories, and an explicit construction API. The metadata-only target registry is deleted and source-only model-family evidence is excluded from distribution. | Independent plugin wheels participate without editing EEGle; remaining application evidence is governed by C-003, not runtime compatibility. | Phases 2 and 6 |
| K-003 | **Resolved for the target storage model 2026-07-22:** generic sessions and namespaced artifact identities no longer derive from recipe paths. `SessionPaths` remains only an alias-registry view for selected legacy clients. | New suites can use arbitrary artifact namespaces; migration of old recipe clients remains Phase 4 cleanup. | Phase 4 |
| K-004 | **Resolved for the v1 kernel:** task-specific epoch, label, condition, and inhibition semantics are absent from model contracts, execution, outcome matching, and actions. | Remaining scientific/application evidence is outside the target package boundary and must be extracted or deleted under C-003. | Phases 2–6 |
| K-005 | **Resolved 2026-07-27:** `eegle.models` owns framework-neutral contracts, manifests, results, predictions, calibration, and state artifacts without delegating to `eegle.ml`; package discovery excludes `eegle.ml`. | Framework/model-family implementations remain external plugins or source-only extraction evidence, never base authorities. | Phases 2 and 6 |
| K-006 | **Resolved 2026-07-27:** portable `ModelManifest` plus deployment artifact resolution replace local runtime bundles, and executable construction belongs only to `PluginRegistry`. | Legacy local-bundle evidence is quarantined outside the distributed target packages until C-003 deletes its remaining application clients. | Phases 1, 2, and 6 |
| K-007 | **Resolved 2026-07-23:** one graph-bearing engine owns live-like, recorded, and replay execution, including watermarks, bounded queues, deadlines, phase retries, bundle replay, compiled triggers, role-aware scheduling, and persisted mid-phase restoration. | No parallel classifier engine or compatibility assembly remains available to drift. | Phases 3 and 5 |
| K-008 | **Resolved 2026-07-22:** task/PsychoPy environment setup moved to `eegle.integrations.task_environment`; the new `eegle.runtime` has no global environment mutation. | The target runtime namespace is clean; the legacy integration remains explicit and removable. | Phase 2 |
| K-009 | **Partially resolved for target specifications:** portable protocol/suite objects contain no filesystem authority; deployment storage requires an explicit URI scheme and compiler artifacts use caller-supplied paths. Legacy recipe configuration still resolves project-root paths. | New compiled suites are independent of repository layout; legacy removal remains Phase 7 cleanup. | Phases 5 and 7 |
| K-010 | **Resolved for the target compiler 2026-07-23:** causal and retrospective transforms declare incompatible capabilities and exact descriptor resolution enforces the protocol execution mode before construction. | Future-dependent components cannot compile into a causal target plan. | Phases 2 and 5 |
| K-011 | **Resolved 2026-07-22:** pure epoch-array helpers now break the direct realtime classification/models cycle. | Retained legacy modules are acyclic at this boundary. | Phase 2 |
| K-012 | **Resolved for the command boundary:** recipe console scripts and historical `eegle.cli` are gone; P7-007 installs the artifact-oriented `eegle`/module CLI, and legacy root orchestration modules remain rejected by the wheel-content test. | Remaining task/worker/hardware application evidence stays governed by its distinct C-003 release conditions. | Phase 7 |
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
| D-020 | Scheduling and checkpoint policy | Compile scheduled/state triggers, permission-defined model priorities/queue/failure dispositions, and general backpressure into the immutable plan. Execute them in the same semantic queue as packets and component work. Checkpoint only at a queue-empty source boundary; persist plan/execution identity, canonical independent runtime/component state, admitted artifacts, clocks, identifiers, trigger state, original phase start, and evidence-prefix digest for integrity-checked fresh-runtime restoration. |
| D-021 | Phase 5 permission boundary | Components declare outcome uses, required adaptation use, and actuator action capabilities. The compiler requires exact independent deployment grants before construction, and runtime routing rechecks the locked use/capability. This is an execution permission boundary, not the Phase 6 hardware-safety/interlock provider. |

### Resolved in Phase 6

| ID | Decision | Resolution |
|---|---|---|
| D-008 | Authorization provider | Authorization is a deployment-owned service outside suite graph composition. Operator confirmation is one deliberately configured provider mechanism, never an implicit fallback. |
| D-022 | Model authority | `PluginDescriptor` alone owns executable construction. `ModelManifest` owns portable scientific artifact identity and `ModelContract` owns scientific requirements; neither can construct code. |
| D-023 | Artifact materialization | Model manifests use content-addressed logical artifact references. Deployment resolves materializations and the compiled plan locks content identity instead of machine-local paths. |
| D-024 | Canonical model output | A plugin returns a schema-bound `ModelResult`. The runtime validates it and constructs `Prediction` with plan-owned implementation, artifact, contract, role, admitted-input, state, and timing evidence. |
| D-025 | Role semantics | Primary, shadow, candidate, and observer are built-in profiles compiled into general permission sets. Runtime semantics consume permissions, not role-name branches, and roles cannot grant action authorization. |
| D-026 | Outcome matching | Only predictions enrolled in an explicit outcome expectation enter the bounded lifecycle. Kernel matching uses direct stable references; heuristics are plugins that emit resolved references. |
| D-027 | State ownership | Mutable state belongs to the component. The engine owns eligibility, transition ordering, evidence, checkpointing, restoration, and replay comparison. Calibration artifacts and online transitions remain distinct. |
| D-028 | Action authorization | Policies emit action requests. Only a deployment-owned provider can supply an authorization decision from which the runtime can construct an actuator-ready command. Suites cannot self-authorize. |
| D-029 | Missing-provider policy | No provider means observe-only. A simulation provider may authorize simulation only; missing, expired, unavailable, or failed authorization never implies permission. |
| D-030 | Model runtime admission | Artifact-bearing model plugins use `model_context_v1`. Deployment materializes locked references; EEGle independently verifies content, supplies a read-only context, restores typed initial state, and verifies the resulting hash before execution. |
| D-031 | Preprocessing proof | Processing operations declare canonical fixed/config-projected parameter attestations. The compiler locks parameter evidence and checks runtime, model-internal, artifact-prepared, duplicated, and forbidden ownership across exact lineage. |
| D-032 | Runtime authority values | Plugins return unprivileged results and providers evaluate immutable action parameters verified against durable digests. Required output omission is terminal evidence, and all checkpoint/rollback state is an independent canonical copy rather than a component-owned alias. |

### Resolved in Phase 7

| ID | Decision | Resolution |
|---|---|---|
| D-014 | Public stability boundary | Package-level `__all__` exports in `eegle`, `actions`, `compiler`, `models`, `plugins`, `processing`, `recording`, `replay`, `runtime`, `specs`, and `streams` are stable-alpha. Package-level `eegle.authoring`, `eegle.operations`, `eegle.integrations`, and optional `eegle.integrations.lsl` exports are provisional public through P7-014. Nested implementation modules, underscore names, retained legacy sources, CLI rendering, and tests are internal unless a later inventory explicitly promotes them. |
| D-033 | Authoring authority | Templates, typed Python, restricted YAML, and project commands are non-executable clients of one versioned draft/lowering service. They emit canonical specifications plus a non-hashing provenance/source-map sidecar. Canonical specs remain the sole compiler inputs, `ExecutionPlan` remains the sole runtime input, and discovery/repair produces explicit proposals rather than mutation. |
| D-034 | Restricted YAML distribution | Restricted YAML is an optional `eegle[yaml]` authoring extra using `ruamel.yaml>=0.18.10,<0.19`, lazily imported only inside the P7-005 adapter. The originally proposed `>=0.19.1,<0.20` range was amended before release because no such version was available. Base JSON/templates/Python authoring remains parser-free. The adapter uses the pure safe YAML 1.2 path and rejects multiple documents, aliases, anchors, merges, tags/directives, duplicate keys, implicit dates, excessive resources, and non-explicit JSON before normal draft validation. |
| D-035 | Observer failure and export safety | Session inspection, replay, replacement comparison, and export are observers or derived executions, not lifecycle controllers. Evidence defects return structured partial/unavailable results by default; strict CLI exits are explicit opt-in. These services never signal another process or recover, truncate, finalize, overwrite, or delete source evidence. The P7 session-export default admits only public artifacts and explicitly excludes evidence/capture/plan/state/raw/deployment content and identity. |
| D-036 | Bounded compositional authoring | The missing middle layer is an immutable named `ExperimentDesign`, not a second graph or runtime. It describes channel-aware signals, installed processing, windows, quality, independent models/comparisons, outcomes/adaptation/calibration, policies/actions, phases, recording, and acceptance; one deterministic lowerer derives existing canonical specs and deployment requirements. Exact template `1.0.0` revisions remain unchanged, while future preset revisions must use this vocabulary. |

### Remaining open decisions

No open architecture decision is currently blocking P7-014. New decisions must
be added here before implementation relies on them.

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
| P6-000 | done | Lock semantic authorities, identity distinctions, role permissions, direct outcome references, state ownership, and independent authorization defaults. | Decisions D-008 and D-022 through D-029; `docs/PHASE6_MODEL_SYSTEMS.md` and the normative architecture are aligned. |
| P6-001 | done | Replace legacy-shaped model contracts and bundles with modality-neutral scientific contracts and canonical path-free manifests. | Typed ports, schema-bound result/uncertainty, preprocessing ownership, state semantics, logical artifacts, implementation constraints, canonical hashes, tamper rejection, multimodal and procedural-model fixtures pass. Deployment bindings compile in P6-002; verified runtime admission closes in P6-009. |
| P6-002 | done | Compile definitive model bindings from suite model intent, deployment artifact resolution, executable plugins, roles, state, and preprocessing lineage. | `ModelUseSpec`, permission-bearing roles, local artifact bindings, exact `PlannedModelBinding`/lock round trips, common signal constraints, preprocessing proof, comparison input checks, precise diagnostics, and scientific/operational plan diffs pass. |
| P6-003 | done | Implement the runtime model boundary. | Callable and independently installed external stateful plugins return schema-bound `ModelResult`; the sole runtime creates canonical predictions from plan/input/state authority, records terminal result dispositions, and restores exact state in a fresh runtime. |
| P6-004 | done | Replace target role strings and special cases with compiled role permissions and comparisons. | Typed priorities, reject-newest/shed-oldest queues, role-local failure, candidate/observer/custom profiles, compatible co-activated comparison groups, complete/incomplete comparison evidence, and canonical policy-input isolation pass. Every model is bound; `ComponentSpec` has no duplicate role field, and no suite-wide role knob or executor string fallback remains. |
| P6-005 | done | Implement enrolled direct-reference outcome lifecycles and bounded dispositions. | Only explicitly enrolled predictions enter bounded direct-reference matching; delayed, missing, duplicate, disputed, expired, overflowed, rejected-use, retrospective-only, cancelled, pending-at-close, checkpoint, and replay evidence pass. |
| P6-006 | done | Implement calibration artifacts and permissioned adaptation transitions. | Canonical calibration artifacts and compiler-gated eligibility plus requested/applied/rejected/no-op/failed/rollback transitions reproduce exact model-owned state after checkpoint and bundle replay. |
| P6-007 | done | Implement the deployment-owned authorization broker. | Policies emit requests; exact deployment providers and grants bound parameters/timing/expiry; the engine alone creates authorized envelopes; pending, denied, expired, failed, cancelled, simulated receipt, and terminal disposition evidence pass; missing authority is observe-only; suites cannot self-authorize; replay fails closed before constructing non-simulation services. |
| P6-008 | done | Add representational generality and prove the optional framework boundary without creating framework-specific core APIs. | Dense EEG-like, irregular slow-signal, sparse-event plus dense-LFP, and multi-rate suites compile/run/persist/verify/replay through one engine. Estimator and tensor-callable fixtures use ordinary external model contracts; P6-003 supplies the installed-wheel proof, while actual sklearn/Torch distributions remain Phase 7 integration work. No support overclaims. |
| P6-009 | done | Delete replaced model/outcome/action authorities and inspect the minimal wheel. | `eegle.ml`, local bundle/model-family registries, classifier built-ins, legacy predictions, the obsolete registry-based foundation-model example, and framework extras are absent from the v1 product; source-only Phase 7/8 evidence is structurally outside target imports and tracked by C-003. The wheel rejects every removed target module. |
| P6-010 | done | Close Phase 6 against every normative gate and the independent closure review. | One-engine compile/run/record/replay evidence, installed external plugin proof, causal-label rejection, exact artifact/state admission, parameterized preprocessing, output completeness, provider parameter evaluation, adversarial rollback restoration, safe action defaults, and modality-neutral fixtures pass. |
| P7-000 | done | Recharter Phase 7 around experiment authoring, operationalization, and complete researcher journeys. | `PHASE7_AUTHORING_OPERATIONS.md`, amended Phase 7 objective/gates, D-033, and the Phase 8 scope boundary agree. |
| P7-001 | done | Lock the public and authoring service boundary, resolve D-014/D-034, and inventory the intended CLI/Python surface before implementation. | `PHASE7_PUBLIC_BOUNDARIES.md`, machine-readable public inventory, packaged draft/provenance schemas, shared operation error/exit contract, clean import/package tests, and no CLI or YAML dependency. |
| P7-002 | done | Implement deterministic `ExperimentDraft` lowering and a non-hashing authoring provenance/source-map sidecar. | Recording intent lowers identically across input ordering; unresolved/unsupported intent fails; typed source/clock/storage needs remain outside deployment bindings; compiler/schema diagnostics map to sources; runtime accepts plans only. |
| P7-003 | done | Add versioned deterministic templates for the required recording, model, adaptation, and simulated-action journeys. | Eight exact-version fixtures expand deterministically with declared hashes and typed locks, compile normally, preserve old locked revisions across explicit upgrades, and contain no executable authority. |
| P7-004 | done | Add the typed high-level Python authoring API over the shared lowering/compiler services. | Persistent typed recording/comparison workflows match shared canonical golden fixtures; expose defaults, requirements, export, and non-overwriting project writing; delegate compilation unchanged; and provide no execution bypass. |
| P7-005 | done | Add the restricted YAML authoring surface after D-034. | A bounded safe JSON-compatible subset retains line/column diagnostics and canonical JSON export, matches Python hashes, and keeps the lazily imported parser in the declared optional extra. |
| P7-006 | done | Add joined authoring/compiler explanations and guided diagnostics. | All eight reference templates expose six complete views; matching plans join without provenance leakage; four-impact diffs and common source-aware failures yield explicit, non-mutating proposals. |
| P7-007 | done | Add project scaffolding and the public artifact-oriented CLI as thin services. | Separated artifact lanes, content-addressed plan/lock outputs, shared Python services, plan-only execution, base evidence/replay journey, versioned machine output, stable exits, and no recipe/root assumption. |
| P7-008 | done | Add discovery and reviewable deployment generation. | Canonical hash-verified detection reports, installed plugin/model plus explicit-manifest discovery, typed site observations, exact compatibility matching, candidate-bearing ambiguity failures, secret references, immutable project proposals, explicit proposal compilation, unchanged portable suite, and no inferred authorization. |
| P7-009 | done | Add capability-based preflight and simulation-deployment rehearsal. | Exact plan/lock/deployment capability checks are hash-verified; the project and independently packaged closed-loop reference paths use the normal engine/bundle writer; eight initial fault-disposition outcomes are evidence; physical action and live-plan impersonation fail closed. |
| P7-010 | in progress | Implement the optional first-party LSL source/event/outlet and discovery path. | Dense/sparse/metadata sources and outlets, exact detection/selectors, clock sync, reconnect, loss evidence, blocked-import isolation, and simulated portability acceptance pass. Truthful support remains `simulated_validated` until the documented real EEG observe-only run is performed. |
| P7-011 | done | Add model-manifest/artifact packaging and at least one dependency-backed external framework plugin. | Deterministic package/index/artifact/state hashes, synthetic vectors, pre-materialization tamper rejection, descriptor conformance, CLI pack/check, a real independently installed scikit-learn adapter returning `ModelResult`, and guarded same-engine replacement replay pass without framework-specific core runtime. |
| P7-012 | done | Add public session, inspection, replay replacement, comparison, and safe export services. | Privacy-aware projections cover session/phase/source/work/model/latency/adaptation/action/integrity/replay state; unfinished and defective evidence degrades structurally without mutation; replacement replay locates the first bounded divergence; safe export is public-only, non-overwriting, and source-preserving; CLI strict exits are opt-in. |
| P7-012A | done | Add bounded named composition above canonical specifications without changing compiler/runtime authority. | Immutable ontology, deterministic named-reference lowering, channel identities, installed processing, configurable windows, quality, distinct model manifests/comparisons, outcomes/adaptation/calibration, structured policies/actions, phases/recording, Python/restricted YAML, composed project export, and six-view explanation are implemented. |
| P7-012B | done | Correct recording scope, processing contracts, field provenance, phase dependency goals, and compositional acceptance before publication. | Phase-scoped sinks execute/persist correctly; plugin-attested complete contract transformations compile; exact leaf origins/default confirmations no longer overstate derived fields; dependency closure is deterministic; simulated action evidence/replay and independent model algorithms pass; authoring responsibilities are split behind stable package exports. |
| P7-013 | done | Publish six clean-install reference projects, finish packaging/docs, and remove only replaced Phase 7 legacy shells. | Exact 2.0 compositional presets and normalized-design CLI/projects produce six materialized examples; independent mean/peak packages and a stateful adaptive plugin are verified, including explicitly deployment-granted applied adaptation and equivalent replay without inferred permission; minimal MNE export/docs, canonical metadata/citation links, declared Python/OS matrix, isolated base/plugin wheel journeys, and `eegle.cli` deletion pass without releasing other protected C-003/Phase 8 evidence. |
| P7-013A | todo | Generalize the plugin-developer workflow before public-surface closure. | Safe descriptor `inspect`/`check` commands and a reusable harness cover independent entry-point discovery, explicit construction, lifecycle, state restore, cleanup/failure, evidence, and replay without connecting to hardware during inspection. |
| P7-014 | todo | Close Phase 7 against every user journey and normative exit gate. | Gate-to-test/evidence matrix with clean base, optional live, model research, external plugin, non-model, and simulated-action proofs. |
| C-001 | done | Audit the source tree against the Phase 6–8 roadmap and establish a protected extraction boundary. | Target packages have no imports from legacy applications; model/outcome/action, integration/hardware, and validation/analysis evidence is explicitly protected in the Phase 0 inventory. |
| C-002 | done | Remove the first isolated legacy batch and its recipe-shaped tests. | Deleted compatibility facades, every obsolete recipe family, launchers, the legacy Makefile, and recipe-only tests; mixed scientific tests now exercise contracts without pipeline or worker imports. |
| C-003 | in_progress | Extract the remaining Phase 6–8 evidence and delete its application shells immediately after replacement tests exist. | Root orchestration, tasks/workers/configs, dashboards, and legacy analysis shells remain staged; no compatibility layer is authorized. |

Only the next two phases should normally be expanded into fine-grained tasks.
Later phase detail belongs in `MIGRATION.md` until it becomes actionable.

## 8. Risk and blocker register

| ID | State | Risk or blocker | Mitigation |
|---|---|---|---|
| R-001 | monitoring | “Cleanup” could delete the only executable example of a subtle scientific behavior. | The inventory binds every cleanup batch to replacement evidence; Phase 1 fixtures and Git history provide separate acceptance and recovery paths. |
| R-002 | open | Freezing behavior could be misread as preserving old APIs and recipes. | Freeze only named scientific invariants and evidence; allow clean public interfaces. |
| R-003 | open | A new specification system could grow into an opaque workflow language. | Keep the domain vocabulary bounded; compile to typed ports; custom logic remains Python plugins. |
| R-004 | monitoring | Modality neutrality could produce abstract types that are inefficient for real EEG or high-density data. | P6-008 proves representation and semantics only. Benchmark dense EEG, sparse traffic, and high-channel-count throughput before claiming validated modality support. |
| R-005 | monitoring | One engine could become one monolithic process. | Process placement remains explicit proxy metadata and the coordinator owns semantics. Cross-process transport/health must preserve the proven boundary without duplicating the engine. |
| R-006 | monitoring | Replay “equivalence” may be overstated for nondeterministic frameworks or hardware. | The comparator caps claims at the weakest declaration; simulated command/receipt substitution now passes, while real hardware remains an observe-only or trace claim. |
| R-007 | monitoring | Action support may be mistaken for hardware safety. | The independent broker defaults to observe-only and replay rejects non-simulation services, while documentation keeps device, interlock, and clinical safety outside EEGle's claims. |
| R-008 | monitoring | Evidence capture could duplicate very large raw streams. | Bundle v1 separates capture from archival stores; a real external source-native fixture proves no copy and local digest verification. Broader sparse/high-channel throughput evidence remains before modality support claims. |
| R-009 | monitoring | Maintaining old and new architectures too long could recreate compatibility debt. | Cleanup checkpoint 1 removed isolated recipes/facades and added a source-boundary test. C-003 tracks the smaller protected extraction remainder; no compatibility adapter is authorized. |
| R-010 | monitoring | Optional dependency imports could leak back into the base package. | A subprocess test now blocks fourteen optional families while importing every foundation package; retain it as a release gate. |
| R-011 | monitoring | GitHub Actions run 29967855396 passes Linux 3.11–3.13 and macOS 3.12; Windows 3.12 reaches unit tests and fails two legacy path/session-name assertions. The local Phase 3 closure tree has not yet run remotely. | Preserve the exact run/job evidence, fix legacy portability separately, and do not claim a green Windows suite or weaken target engine contracts. |
| R-012 | resolved | An in-progress Phase 3 outcome/checkpoint edit temporarily called a missing `_finalize_pending_predictions()` method and blocked integrated verification. | The method and full outcome lifecycle now pass the 27-test Phase 3 module and integrated 330-test full suite. |
| R-013 | resolved | Two engine authorities could have diverged. The temporary Phase 3 implementation, `EngineComponents`, aliases, and API-shaped tests are deleted; replay now reconstructs the locked graph. | Keep Phase 6 extensions inside `PlanGraphExecutor`/`ExecutionEngine`; never restore a parallel classifier engine. |
| R-014 | monitoring | `runtime/graph.py` remains a large semantic coordinator and could become difficult to change safely as integrations grow. | Keep `PlanGraphExecutor` as the sole ordering authority. Extract cohesive, stateless policies or state-owning coordinators behind typed records only when a change supplies focused acceptance tests; do not add another loop, scheduler, or classifier-shaped facade merely to reduce line count. |
| R-015 | monitoring | A convenient draft, template, YAML, explanation, project, or discovery layer could become a second specification or graph authority. | P7-002 exposes one lowerer, rejects arbitrary routes/site bindings, sends only canonical specs to the compiler, and enforces `ExecutionPlan` at runtime construction. P7-003 templates and the P7-004/P7-005 builder/YAML clients dispatch only bounded internal profiles through that service. P7-006 explanation/diff is read-only. P7-007 makes the project a non-executable index, delegates compile unchanged, and verifies a plan/lock pair in a service that reads no mutable authoring before session creation. P7-008 produces a sidecar proposal plus an ordinary `DeploymentSpec`; only explicit selection sends that spec with unchanged portable inputs to the compiler. |
| R-016 | monitoring | Provenance embedded in canonical values could make equivalent authoring surfaces produce different scientific hashes or misattribute compiler defaults. | P7-002 proves source-map changes leave canonical hashes unchanged and maps existing diagnostics without rewriting them. P7-004/P7-005 golden fixtures prove equivalent Python/YAML sources yield identical canonical and expansion hashes. P7-006 joins compiler fields only as source-free `compiler_derived` projections and classifies Python/YAML provenance-only changes as presentational. |
| R-017 | open | Rehearsal could be misrepresented as execution of the hardware-bound plan or silently authorize a physical action. | Compile an explicit simulation deployment into its own lock, compare unchanged portable intent and material differences, and accept simulation-only authorization providers. |
| R-018 | open | Phase 7 product work could absorb Phase 8 validation depth and delay a coherent usable workflow. | Gate Phase 7 on definition/preflight/integrity/replay/comparison exposure and complete journeys; retain scientific evaluation, benchmarks, fault qualification, reporting depth, and alpha hardening in Phase 8. |
| R-019 | monitoring | General authoring could become an arbitrary workflow language or silently alter exact template revisions. | `ExperimentDesign` accepts only bounded typed declarations and installed plugin IDs, derives rather than accepts canonical routes, and sends only canonical specs to the existing compiler. Exact template `1.0.0` fixtures remain unchanged; compositional presets require new revisions and P7-013 acceptance. |

No migration blocker is currently recorded. Phases 2 through 6 provide the
typed foundations, one execution engine, evidence/replay boundary, compiler,
and complete model-system semantics. Phase 7 is active. P7-001 fixes the public
authoring/service and dependency boundaries; P7-002 adds the bounded recording
lowerer, deployment requirements, typed non-hashing provenance, and diagnostic
source join. P7-003 adds eight exact-version non-executable templates with
fixture-locked expansion identities. P7-004 and P7-005 add equivalent typed
Python and restricted-YAML clients, deterministic export/project artifacts, and
cross-surface golden identities over that same service. P7-006 supplies
read-only joined views, four-impact diffs, and source-aware repair proposals
without adding an authority. P7-007 supplies separated project artifacts,
shared base simulation services, immutable plan/lock execution, evidence
inspection/replay, and the first public CLI slice. P7-008 adds canonical
capability reports, exact matching, immutable proposal artifacts, and explicit
proposal compilation without changing suite authority. P7-009 adds exact
capability preflight and fault-evidenced, simulation-only rehearsal. P7-010's
dependency-lazy LSL implementation passes simulated acceptance, while its real
EEG observe-only support gate remains explicitly open. P7-011 adds deterministic
model transport, a dependency-backed adapter proof, and counterfactual replay
through separately compiled plans without adding a framework authority. P7-012
adds privacy-aware evidence projections, nonfatal replay/replacement comparison,
public-only non-overwriting export, and explicit guarantees that observer
failure cannot mutate evidence or signal recording, processing, or training
processes. P7-012B closes the reviewed compositional correctness gaps and splits
the authoring implementation by responsibility. P7-013 adds exact
2.0 presets, normalized-design CLI/project input, six reference projects,
independent example model plugins, and a minimal dependency-lazy MNE bridge.
Exact template 1.0 expansions remain unchanged. Clean-wheel and publication
checks pass; the real EEG LSL observe-only gate remains open for P7-014.

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
| 2026-07-24 | Legacy cleanup checkpoint 1 | Target-to-legacy imports were audited and structurally blocked. `core`, `protocols`, `components.py`, every alpha/attention/classification/DSART/inhibition recipe, their launchers and recipe-only tests, and the legacy Makefile were removed. Classification, adaptation, analysis, and migration tests retain scientific evidence without importing recipes or the realtime worker. The focused 28-test cleanup/packaging suite passes with three optional-dependency skips; the complete 227-test Python 3.14 suite passes with five skips; compile-all and diff checks pass. Phase 6–8 evidence remains protected and the smaller application remainder is staged under C-003. |
| 2026-07-23 | Phase 5 closed | P5-006 through P5-008 add compiled scheduled/state triggers, semantic timeout and acceptance decisions, primary-first/shadow scheduling, nonfatal reject-newest backpressure, exact outcome/adaptation/action permission checks, and tamper-detecting fresh-runtime mid-phase restoration. Durable delayed-adaptation and event-window/multi-rate/simulated-action suites compile and execute; the latter persists and bundle-replays with equivalent receipts. All 41 focused tests and the 344-test full suite pass with five environment-dependent skips; compile-all passes. Phase 6 is ready. |
| 2026-07-26 | Phase 6 semantic authorities locked | P6-000 resolves model/plugin/artifact/binding/prediction ownership, permission-defined roles, enrolled direct-reference outcomes, component state ownership, and deployment-only authorization with observe-only default. `PHASE6_MODEL_SYSTEMS.md`, `EEGLE.md`, and the migration plan agree. |
| 2026-07-26 | P6-001 model authority completed | Modality-neutral canonical model contracts and path-free model manifests replace the target authority while legacy contract/bundle helpers leave the package-level model surface. Seven dedicated tests and the 234-test Python 3.14 suite pass with five skips; compile-all, source boundaries, diff checks, and clean-wheel packaging pass. Compiler bindings are P6-002 and legacy deletion remains P6-009. |
| 2026-07-26 | P6-002 compiler model binding completed | Suite model uses and roles plus deployment materializations compile into exact immutable model bindings with manifest/plugin/state/input/preprocessing/artifact/comparison/permission checks. Nine dedicated tests, 57 focused tests, and the 243-test Python 3.14 suite pass with five skips; compile-all, source boundaries, diff checks, and clean-wheel packaging pass. Runtime-owned model results and predictions are P6-003. |
| 2026-07-26 | P6-003 runtime model boundary completed | Contract-bound plugin results now become plan-owned canonical predictions with executor-derived admitted inputs, exact pre-inference state and terminal emitted/rejected/late/cancelled dispositions. A plain callable and independently installed stateful wheel prove one-engine execution and fresh-runtime restoration. Four dedicated tests, 82 focused tests, and the 247-test Python 3.14.4 suite pass with five skips; P6-004 replaces the remaining role-name runtime branches. |
| 2026-07-27 | P6-004 role and comparison semantics completed | Permission-bearing roles now drive target scheduling, queue and failure dispositions; comparison groups emit exact complete/incomplete evidence; policies reject predictions not emitted by the current locked execution. Five dedicated tests, 66 focused tests, and the 252-test Python 3.14.4 suite pass with five skips; compile-all and diff checks pass. P6-005 implements enrolled direct-reference outcome lifecycles. |
| 2026-07-27 | P6-005/P6-006 outcome and adaptation semantics completed | Compiled expectations enroll only declared predictions into a bounded, checkpointable direct-reference lifecycle with explicit dispositions. Canonical calibration artifacts and independently permissioned adaptation emit eligibility and requested/applied/rejected/no-op/failed/rollback state evidence; failed mutation restores prior state and bundle replay reproduces delayed updates. Five dedicated tests, 91 focused tests, and the 257-test Python 3.14.4 suite pass with five skips; compile-all, diff checks, and clean-wheel packaging pass. P6-007 is the deployment-owned authorization broker. |
| 2026-07-27 | P6-007 deployment-owned action authorization completed | Policies emit unprivileged requests; exact deployment providers and grants bound capabilities, parameters, timing, expiry, and failure behavior; the engine alone creates actuator-ready commands. Observe-only defaults, pending resolution, expiry, cancellation, simulated receipts, lineage-forgery rejection, and fail-closed physical replay pass. Eight dedicated tests and the 265-test Python 3.14.4 suite pass with five skips; compile-all, diff checks, and clean-wheel packaging pass. P6-008 is next. |
| 2026-07-27 | P6-008 representational generality completed | Four modality-neutral suites prove regular/irregular dense, sparse plus dense, and multi-rate streams through compile, run, verified evidence, and equivalent replay. Estimator and tensor-callable fixtures prove the external framework boundary without adding a framework runtime; dependency-backed packages remain Phase 7. Three dedicated tests, 62 focused tests, and the 268-test Python 3.14.4 suite pass with five skips; compile-all, diff checks, and clean-wheel packaging pass. P6-009 is next. |
| 2026-07-27 | Phase 6 closed | The five independent closure blockers are resolved: digest-verified artifact/state admission, parameterized preprocessing attestations, terminal required-output accounting, immutable provider parameter evaluation, and alias-safe state rollback. Every model now requires a manifest binding; v1 predictions, classifier built-ins, suite-wide role knobs, target metadata registries/local bundles, `eegle.ml` discovery, and framework extras are removed from the v1 package. Retained Phase 5 graphs use canonical Phase 6 model semantics. All 122 focused closure tests and the 274-test Python 3.14.4 suite pass with five skips; compile-all, diff checks, source boundaries, and clean-wheel inspection pass. Phase 7 is ready. |
| 2026-07-27 | Phase 7 rechartered and started | The accepted handoff expands Phase 7 to experiment authoring and operations while retaining canonical specs, the compiler, and the locked engine as sole authorities. P7-000 adds the reviewed design, complete journeys, provenance sidecar, rehearsal/live lock distinction, D-033/D-034, detailed tasks, risks, and amended exit gate. P7-001 is next; no Phase 7 implementation is claimed yet. |
| 2026-07-27 | P7-001 public and authoring boundary completed | D-014/D-034 fix stable-alpha, provisional, internal, and YAML-extra boundaries. `eegle.authoring` packages versioned non-executable draft/provenance contracts; `eegle.operations` packages shared structured diagnostic and exit semantics. A machine inventory and six dedicated tests prevent authority, dependency, and source-map drift. Twenty-seven focused tests, compile-all, and diff checks pass. The 280-test full run has 274 passes, five skips, and one environmental clean-wheel setup failure because the temporary interpreter lacks its pinned build backend; the packaging gate is unchanged. P7-002 is next. |
| 2026-07-28 | P7-002 draft lowering and provenance completed | Typed incomplete drafts now deterministically lower bounded recording intent into canonical protocol/suite values plus portable source, clock, and storage requirements. A digest-bound sidecar distinguishes explicit/default origins and maps canonical diagnostics to source; unresolved or out-of-profile intent fails, a resolved deployment compiles normally, and runtime construction rejects non-plans. Eight dedicated and 51 focused tests pass. The 288-test aggregate run has 282 passes, five skips, and one missing-build-backend failure; the expanded clean-wheel test passed independently while a pre-existing local pinned backend payload remained available. P7-003 is next. |
| 2026-07-28 | P7-003 versioned templates completed | An immutable exact-version catalog supplies eight bounded recording, observation, comparison, calibration/validation, adaptation, and simulated-action profiles. Manifest-locked draft selections, typed expansion locks, parameter/default provenance, declared synthetic digests, same-version replacement rejection, and explicit re-expansion preserve old locked output. Eight dedicated and 39 focused tests pass; all eight fixtures compile through the existing compiler. The 296-test aggregate run has 290 passes, five environment-dependent skips, and the unchanged clean-wheel build-backend failure because this temporary interpreter cannot import `setuptools.build_meta`; compile-all passes. P7-004 is next. |
| 2026-07-28 | P7-004/P7-005 Python and YAML authoring completed | One persistent typed builder and one optional restricted-YAML adapter lower through the same exact-template/draft service. Shared golden recording/comparison fixtures prove identical canonical specs, requirements, and expansion identities; canonical/project export stays non-executable; compilation cannot bypass normal rejection; and YAML retains precise locations while rejecting unsafe constructs and resource excess. Ten dedicated and 53 focused tests pass. The 306-test aggregate run has 300 passes, five environment-dependent skips, and the unchanged clean-wheel build-backend failure because the temporary Python 3.14 interpreter cannot import `setuptools.build_meta`. P7-006 is next. |
| 2026-07-28 | P7-006 explanation and guided diagnostics completed | Six complete views explain all eight reference templates and optionally join only a matching existing locked-plan projection. Four-impact experiment diffs distinguish provenance-only presentation from scientific, operational, and replay-affecting changes. Common authoring/compiler failures preserve their original identity while gaining likely cause, closest source, documentation, and inspectable `applied: false` proposals. Six dedicated and 60 focused tests pass; compile-all and diff checks pass. The 313-test aggregate run has 307 passes, five environment-dependent skips, and the unchanged clean-wheel build-backend failure because the temporary Python 3.14 interpreter cannot import `setuptools.build_meta`. P7-007 is next. |
| 2026-07-28 | P7-007 project scaffolding and public CLI completed | A path-safe project index separates mutable authoring/deployment artifacts, generated canonical values, content-addressed immutable plan/lock/explanation artifacts, and sessions. Shared Python services and the installed `eegle`/module adapter complete create, compile, explain, diff, graph, base rehearsal, locked run, integrity inspection, and equivalent bundle replay with versioned machine envelopes and fixed exits. Removing mutable authoring after compile does not affect execution; a tampered lock fails before session creation. Six dedicated and 79 focused tests pass. The 320-test aggregate has 314 passes, five skips, and only the known Python 3.14 build-backend setup failure; an independently built 115-file clean wheel completes all nine commands and excludes the historical CLI. P7-008 is next. |
| 2026-07-28 | P7-008 discovery and deployment generation completed | `eegle.detection_report.v1` deterministically records exact installed plugin descriptors, installed or explicitly supplied model manifests, and typed source/stream/channel/rate/unit/clock/storage/placement/endpoint/authorization-provider observations with provenance and hash verification. `eegle.deployment_proposal.v1` reuses compiler contract compatibility, rejects zero/ambiguous matches precisely, records automatic versus explicit decisions, admits only secret references, and never turns provider presence into a grant. Content-addressed project reports/specs/provenance remain separate from simulation; proposal compilation is explicit and rejoins the unchanged suite through the normal compiler. Seven dedicated and 90 focused tests pass. The 327-test aggregate has 321 passes, five skips, and only the known Python 3.14 build-backend setup failure. An independently built 127-file clean wheel includes discovery/console metadata, excludes historical `eegle.cli`, and runs detection plus the project path outside the repository. P7-009 is next. |
| 2026-07-28 | P7-009 capability preflight and rehearsal completed | Canonical preflight binds the exact plan, lock, deployment, optional detection report, plugin/artifact/source/channel/unit/rate/clock/storage/endpoint/model/provider/gate/safe-state checks, and persists a hash-verified report. Simulation-only rehearsal rejects physical authority, executes through the normal engine/bundle path, and records all eight initial fault-disposition outcomes in semantic evidence plus a report that cannot impersonate a live plan. The simulated closed-loop template passes with independently supplied model plugins. |
| 2026-07-28 | P7-010 automated LSL implementation completed; real gate open | The optional `live` extra and dependency-lazy LSL subpackage provide six source/outlet descriptors, exact discovery and selectors, dense/sparse/metadata conversion, explicit clock sync, bounded reconnect, and sequence-gap loss evidence. Exact live-capability preflight and simulation/live portable-suite separation pass, selector drift fails closed, and base imports remain pylsl-free. The complete 334-test repository suite passes with ten conditional skips, and the clean wheel completes dependency-absent LSL detection plus the base project journey. Support truthfully remains `simulated_validated`; `docs/LSL_INTEGRATION.md` records the unperformed real EEG observe-only acceptance, so P7-010 remains in progress. |
| 2026-07-28 | P7-011 model packaging and framework integration completed | Deterministic manifest/artifact/state packaging, synthetic vectors, pre-materialization integrity rejection, content-addressed safe extraction, descriptor conformance, and CLI pack/check are public model services. A separately built wheel declares real scikit-learn/joblib dependencies, loads only a runtime-verified estimator, and returns `ModelResult`; separately compiled compatible model plans replay the original capture through the same engine and expose counterfactual divergence. Four dedicated tests, a 26-test focused run, the clean-wheel journey, and all 339 repository tests pass with five conditional skips; compile-all, Ruff, and diff checks pass. P7-012 is next. |
| 2026-07-28 | P7-012 session, evidence, comparison, and safe export experience completed | Public privacy-aware projections cover session/phase/source/work/model/latency/adaptation/action/integrity/replay evidence. Replay and replacement failure degrade structurally, strict exits are opt-in, unfinished writers are observed without recovery, and public-only export neither overwrites nor mutates source data. Three dedicated tests plus actual replacement replay, a 69-test Phase 7 run, and all 342 repository tests pass with five conditional skips; compile-all, focused Ruff, and diff checks pass. P7-013 is next. |
| 2026-07-28 | P7-012A bounded compositional authoring core implemented | `ExperimentDesign` adds immutable named signals/channels, processing, configurable windows, quality, distinct model/comparison, outcome/adaptation/calibration, policy/action, phase, recording, and acceptance declarations. One lowerer emits existing specs, requirements, and non-hashing provenance; draft and restricted-YAML clients converge on it. Two different manifests compile on one admitted input, structured 3D requests remain unprivileged, missing authority compiles observe-only, and six-view explanation works. Eleven dedicated tests and all 353 repository tests pass with six conditional skips; compile-all, focused Ruff, and diff checks pass. P7-013 retains new preset revisions and clean-install project/CLI publication. |
| 2026-07-29 | P7-012B correctness hardening and P7-013 publication completed | Recording sinks are phase-correct, complete processing contracts are plugin-attested, canonical provenance is field-exact, phase goals expand dependencies, and simulated action evidence/replay plus independent model algorithms have regression coverage. The former concentrated authoring module is split behind stable package exports and its schema boundary. Exact 2.0 presets, normalized-design projects/CLI, six reference projects, separately packaged mean/peak/stateful-adaptive plugins, minimal MNE export, metadata/citation correction, and guarded `eegle.cli` removal are complete. Clean source-independent wheel/plugin journeys run model comparison and deployment-granted adaptation; the latter records eligible/requested/applied transitions and equivalent replay. All 369 Python 3.12 tests pass with seven conditional skips, plus compile-all, focused Ruff, and diff checks. Only real LSL acceptance remains external to P7-014 closure. |

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
