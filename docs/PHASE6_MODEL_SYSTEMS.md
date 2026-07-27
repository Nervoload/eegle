# Phase 6 Model-System Semantics

**Status:** Complete implementation design
**Authority:** [EEGLE.md](EEGLE.md)  
**Phase gate:** [MIGRATION.md](MIGRATION.md#11-phase-6--general-model-outcome-adaptation-and-action-semantics)

This document fixes the implementation authorities for Phase 6. It refines the
roadmap without changing EEGle's product definition: EEGle compiles, executes,
records, replays, and validates synchronized neurophysiological model systems.

## 1. Semantic authorities

| Authority | Owns | Must not own |
|---|---|---|
| `PluginDescriptor` | Executable implementation, factory, configuration schema, typed ports, implementation capabilities, version and provenance | Scientific model artifact identity, local deployment, suite role |
| `ModelManifest` | Portable model identity, model contract, logical content-addressed artifacts, compatible plugin requirements, optional scientific provenance | Factories, local paths, copying/downloading, framework imports, runtime environment probing |
| `ModelContract` | Scientific input/output requirements, preprocessing ownership, model-state semantics, uncertainty schema, supported information modes | Placement, deadlines, credentials, local resources, executable construction |
| `SuiteSpec` | Logical use of a model, routes, phases, role and outcome/action intent | Local artifact paths, credentials, authorization mechanism |
| `DeploymentSpec` | Local artifact resolution, placement, resources, secrets references and authorization-provider binding | Scientific topology or self-granted suite authority |
| `PlannedModelBinding` | Exact compiled join of plugin, manifest, contract, role, state, artifacts and deployment constraints | Mutable runtime state or unresolved paths |
| `Prediction` | Canonical plan-owned evidence envelope around one validated model result | Self-asserted plugin identity, role, artifact digest or admitted-input lineage |
| `EvidenceBundle` | Durable evidence that the locked authorities governed execution | New scientific or authorization claims invented after execution |

The compiler must reject contradictory declarations rather than choose one
authority silently. Plugin capabilities and model requirements are intersected;
deployment may narrow but not expand them.

## 2. Model boundary

A model plugin returns a contract-bound `ModelResult`. The runtime validates the
result and constructs the canonical `Prediction` using plan-owned component,
plugin, manifest, contract, role, input-lineage, state and timing information.
External code is not trusted to self-assign those identities.

Model outputs use explicit schemas rather than a closed taxonomy of
classification, regression, embeddings or generative output types. Optional
uncertainty, abstention and validity fields are separately schema-bound. The
contract must not assume dense inputs, fixed sampling, channels, epochs,
microvolts, binary labels or one scalar confidence.

Preprocessing requirements name exactly one owner: upstream graph,
model-internal, artifact preparation, or forbidden. The compiled binding later
proves those requirements and their canonical parameters against actual route
lineage and stores the resulting attestations. Model-specific
placement, worker and timeout abstractions are not introduced; the generic
component placement and proxy boundary remains authoritative.

`ModelManifest` is canonical portable data. A model bundle may be a materialized
or distributable collection containing a manifest and artifacts, but it is not
the semantic authority and cannot combine training, package management, local
caching and runtime construction.

## 3. Roles

Primary, shadow, candidate and observer are built-in role profiles. Compilation
expands every profile or custom declaration into a permission-bearing role:

- scheduling class and priority;
- comparison group and required input equivalence;
- permission to feed a policy, receive outcomes or adapt;
- failure and queue disposition.

Roles never authorize physical actions. At most, a role may permit its
prediction to reach a policy that emits an action request. Deployment-owned
authorization remains independently required. Runtime scheduling consumes
compiled permissions rather than comparing role-name strings.

## 4. Outcomes and state

Only predictions explicitly enrolled in an `OutcomeExpectationSpec` enter the
bounded outcome lifecycle. Observer outputs and other predictions with no
scientific expectation are not forced to acquire fictional labels.

Kernel matching accepts stable direct references to predictions, events,
windows, actions, artifacts or phases. Temporal, nearest-neighbor and
task-specific heuristics are explicit plugins that emit direct references.
Enrolled predictions and received outcomes receive explicit pending, matched,
expired, overflowed, duplicate, disputed, rejected, cancelled or end-of-run
dispositions according to the locked policy.

Calibration produces an artifact used as initial state. Online adaptation is a
permissioned attempted state transition. Mutable state belongs to the component
being changed; the engine owns eligibility, ordering, evidence, checkpointing
and restoration. Framework wrappers and adaptive state updaters are distinct
concepts; the runtime's generic adaptive component should be named
`StateUpdater` rather than overloading “adapter.”

## 5. Actions and authorization

The action path is:

```text
Policy
→ ActionRequest
→ engine role/capability/phase checks
→ deployment-owned AuthorizationProvider
→ AuthorizationDecision
→ AuthorizedCommand (authorized only)
→ Actuator
→ ActionReceipt
```

An authorization provider is a deployment service, not a suite graph node. A
suite and its overlays cannot construct an `AuthorizedCommand`. Providers may be
discovered through executable plugin infrastructure, but deployment constructs
and binds them independently of scientific graph composition.

The v1 default is:

- no provider: observe-only;
- built-in simulation provider: may authorize simulation-only capabilities;
- explicit independent provider: decides only within locked capabilities,
  parameter bounds, time bounds and failure policy;
- missing, expired or failed provider: observe-only or denied exactly as locked,
  never implicitly authorized.

Operator confirmation is a deliberately configured provider mechanism, not an
automatic fallback. The provider contract may yield a pending decision without
blocking the semantic engine; interactive UI belongs to Phase 7. Replay never
contacts hardware or prompts an operator and instead replays recorded decisions
or uses an explicitly declared simulated/trace substitution.

## 6. Scope controls

Phase 6 does not add:

- top-level `outcomes` or `adaptation` packages merely to mirror concepts;
- a model-specific execution loop or proxy system;
- an exhaustive learning-problem/output enum;
- a training framework, model zoo or automatic model downloader;
- arbitrary Python import strings in JSON;
- a generic hidden temporal matcher;
- task labels or framework tensor types in kernel contracts;
- interactive authorization UI, device safety logic or modality support claims;
- Phase 8 scientific evaluation and reporting.

Outcome coordination remains under `eegle.runtime`, calibration/model state
contracts remain under `eegle.models`, and action records and broker contracts
remain under `eegle.actions` with orchestration called by the sole execution
engine. Split files only when responsibility or testing benefits, not to satisfy
a diagram.

## 7. Implementation order

1. P6-000: lock these authorities and decisions.
2. P6-001: replace legacy-shaped contracts with canonical model contracts and
   manifests.
3. P6-002: compile definitive model bindings and role permissions.
4. P6-003: introduce `ModelResult`, runtime-owned predictions, a callable model
   and an external stateful plugin fixture.
5. P6-004: remove string role branches and prove comparison permissions.
6. P6-005: implement enrolled outcome expectations and bounded dispositions.
7. P6-006: implement calibration artifacts and replayable state updates.
8. P6-007: implement the deployment-owned authorization broker.
9. P6-008: prove representational generality and optional framework plugins.
10. P6-009: delete replaced legacy authorities and inspect the minimal wheel.
11. P6-010: map every Phase 6 gate to durable acceptance evidence.

## 8. Implemented compiler boundary

P6-002 compiles portable `ModelUseSpec` and `ModelRoleSpec` declarations with a
digest-keyed model-manifest catalog and site-local `ModelArtifactBindingSpec`
values. The immutable plan carries a `PlannedModelBinding` containing the exact
plugin version, complete canonical manifest, compiled role permissions,
comparison group, content digests, local materialization URIs, and proven
preprocessing component lineage.

The compiler checks implementation/version compatibility, information mode,
state capability, replay-equivalence ceiling, input and output ports, common
dense/sparse/irregular signal constraints, required or duplicated
preprocessing, artifact digests, equivalent comparison inputs, and role-to-
policy/outcome permissions. It never reads or downloads the bound artifact;
content verification belongs to construction/runtime admission. Plan diffing
classifies manifest, contract, role, digest, and lineage changes as scientific,
while a local materialization URI change is operational.

Every model component must bind exactly one canonical manifest and compiled
role. An unbound model is rejected during compilation and construction.

## 9. Implemented runtime model boundary

P6-003 introduces a framework-neutral `ModelResult` containing only the value,
optional uncertainty and validity, abstention, and a non-negative semantic
completion delay. It cannot carry a prediction ID, component or plugin
identity, role, manifest or contract digest, artifact identity, input lineage,
state identity, or absolute timestamp. Every declared value, uncertainty,
validity, and abstention is checked against the compiled output contract.

For model components with a `PlannedModelBinding`, the sole graph executor
routes every `predict()` or multi-port `process()` return through
`model_runtime`. The runtime captures prediction-affecting state before the
call, derives immediate and root admitted inputs from its own graph trace,
merges causal clock and stream revisions, and creates `eegle.prediction.v2`
with the exact locked implementation, manifest, contract, role, comparison,
artifact, result, state, input, and timing identities. The plugin cannot
self-author any field in that envelope.

Every returned result receives a terminal `emitted`, `rejected`, `late`, or
`cancelled` disposition. Every required output port must be present and
non-empty; omission creates a separate terminal rejection for each missing
port. Component deadlines reject late results before graph
emission; phase termination records cancellation for still-pending results.
Executor-owned admitted-input lineage is included in persisted checkpoints so
restoration cannot replace graph evidence with component claims.

The base `CallableModel` wraps ordinary Python callables without permitting
import strings in suite JSON. A separately built and installed stateful model
wheel proves entry-point discovery, compilation, canonical predictions,
pre-inference state hashes, snapshot restoration in a fresh runtime, and
continued execution without modifying EEGle source.

## 10. Implemented role and comparison boundary

P6-004 makes each `PlannedModelRole` a typed runtime authority. Built-in
primary, candidate, shadow, and observer profiles compile to explicit
scheduling priority, equivalent-input requirement, policy/outcome/adaptation
permissions, queue limit and disposition, and failure disposition. Custom roles
use exactly the same compiled representation. Suite-wide `primary_first`,
`shadow_queue_limit`, and `shadow_failure` fields are not part of `SuiteSpec`;
`ComponentSpec` also has no duplicate role field. Role intent exists only in a
model use, and all model scheduling behavior comes from the compiled permission
set.

The executor uses compiled priorities without inspecting a bound model's role
name. Queue overflow produces terminal fail-run, reject-newest, or shed-oldest
work evidence as declared. Reject-result failures remain local to the model;
fail-run failures terminate the phase. The accepted built-in order is primary,
candidate, shadow, then observer for otherwise simultaneous work.

Comparison groups must contain enough members for roles that require
equivalence, expose compatible output contracts, and activate every member in
the same phase. Runtime comparison keys include group, output port, immediate
model inputs, and executor-derived admitted inputs. A complete
`ModelComparison` records every prediction and result digest plus exact output
equality. A missing, rejected, shed, late, or failed member produces an
incomplete terminal comparison naming the missing members and phase
disposition; pending comparison state survives checkpoints.

Policy routing is also rechecked at runtime. A policy accepts a canonical
prediction only when the current execution emitted that exact value, its
identity matches the immutable binding, and the compiled role permits policy
influence. This prevents external or forged prediction envelopes from using a
permitted role identity. It does not grant actuator authority; P6-007 now
enforces that independent deployment-owned boundary as described below.

## 11. Implemented outcome lifecycle

P6-005 makes outcome enrollment an explicit compiled contract. An
`OutcomeExpectationSpec` names the model, permitted outcome sources and uses,
pending bound, semantic time-to-live, and overflow disposition. Predictions
without an expectation never enter the matcher. This preserves model outputs
such as embeddings, quality estimates, and annotations that have no label
lifecycle.

The kernel matches stable typed references only. A task-specific temporal or
heuristic resolver must be an ordinary plugin that emits an `Outcome` carrying
resolved prediction, event, window, action, artifact, or phase references. The
coordinator does not infer nearest-time relationships.

Every enrolled reference produces durable dispositions for pending, matched,
unmatched, expired, overflowed, duplicate, disputed, retrospective-only,
rejected, cancelled, or pending-at-close states. Permitted uses are the
intersection of the compiled expectation and the received outcome. Pending
predictions, deadlines, ordering, and disposition history are checkpointed and
restored as engine state, and bundle replay compares the same lifecycle
evidence.

## 12. Implemented calibration and adaptation boundary

P6-006 represents calibration as a canonical `CalibrationArtifact`: algorithm
and model identity, support inputs and outcomes, produced time, provenance, and
a generic content-addressed model-state reference. It is portable data, not a
local directory, runtime factory, or framework-specific object.

Online adaptation is separate. A compiled `AdaptationSpec` binds one outcome
expectation to the state-owning model and allowed phases. Compilation requires
an outcome use that includes adaptation, a role permitted to receive outcomes
and adapt, an adaptation-capable state contract, snapshot/restore plugin
capability, active phase routes, and an exact independent deployment grant.

At runtime the engine records an eligibility decision and a requested state
transition before invoking the model-owned update. Applied, rejected, and no-op
results are validated against the before/after snapshot hashes. An exception or
invalid mutation records failure, restores the exact prior snapshot, and records
rollback when restoration changed state. Outcome coordinator and component
state snapshots share the normal checkpoint boundary, so delayed adaptation and
the next prediction reproduce through bundle replay without a second learning
loop. Snapshot capture deep-freezes canonical JSON before the update, so even a
component returning a live nested mapping cannot mutate its rollback authority.

## 13. Implemented action authorization boundary

P6-007 separates scientific policy from deployment authority. A policy may emit
an `ActionRequest`; it cannot emit an `AuthorizedCommand`. The compiler rejects
suite components that claim authorization-provider authority or output an
authorized-command schema. Deployment binds an exact authorization-provider
plugin and an independent permission grant containing the permitted capability,
actuator identities, parameter constraints, request lifetime, delivery delay,
decision delay, and provider failure disposition.

The engine-owned `ActionBroker` is the only constructor of an actuator-ready
command. It records the request, authorization request, pending and terminal
decisions, authorized envelope, submission, receipt, cancellation, and terminal
disposition. No grant or provider means observe-only. Provider failure resolves
to the locked observe-only or denied disposition. Pending authorization is
semantic scheduled work: it may resolve later, expire, or be cancelled by phase
termination without blocking the coordinator.

Provider calls receive an `AuthorizationEvaluation` containing the typed
request and an immutable copy of the actual parameters. The broker verifies
that copy against the persistent request digest before both `authorize()` and
`resolve()`, so providers never decide from an opaque digest alone.

Phase 6 does not invent a generic device-history or interlock-state vocabulary.
A concrete integration that needs broker-owned command history, actuator state,
or interlock attestations must add a narrow immutable runtime view and declare
that capability explicitly; it must not receive the executor or mutable queues.

The built-in provider and actuator are explicitly simulation-only. The compiler
prevents that provider from granting a non-simulated capability. Replay checks
the exact locked provider and actuator descriptors before constructing any
component and refuses every service not declared simulation-only. This provides
a safe default for evidence replay without claiming that EEGle supplies device
or clinical safety. A later trace-only replay mode may use explicit inert
substitutions; it must not weaken this fail-closed boundary.

## 14. Implemented representational and framework-boundary generality

P6-008 proves that modality neutrality is executable rather than vocabulary
alone. Four durable fixtures use the same `SuiteSpec` → `ExecutionPlan` →
`ExecutionEngine` → `EvidenceBundle` → replay path:

- regular multichannel EEG-like dense samples;
- slow, explicitly timestamped irregular fNIRS-like dense samples;
- sparse spike events alongside regular dense LFP;
- 250 Hz EEG-like, 1 kHz EMG-like, and event-rate behavior streams in one plan.

Each fixture compiles its content kind, rate model, channel identities, units,
event kinds, and duration requirements into the model binding, creates
runtime-owned predictions with exact admitted-input lineage, persists a valid
plan-bearing evidence bundle, and replays equivalently. A deliberately false
regular-rate requirement for the irregular fixture fails compilation. The
compiler therefore checks declared capabilities rather than trusting a modality
name or branching on `eeg`, `fnirs`, or `neuropixels`.

### What an optional framework plugin means

Sklearn and Torch do not require new EEGle component kinds or runtimes. A
framework integration is an ordinary external model plugin whose distribution:

1. owns and imports the optional framework dependency;
2. constructs or loads the framework object under deployment control;
3. converts a typed EEGle input into the framework's array or tensor shape;
4. invokes the estimator or module; and
5. returns the existing framework-neutral `ModelResult`.

P6-008 uses minimal `predict_proba`-shaped and tensor-callable test doubles to
prove those two interaction boundaries through the real compiler and engine.
It deliberately adds no sklearn/Torch imports, loaders, training APIs, model
zoo, tensor abstraction, or second plugin registry to the base package. P6-003
already proves that a separately installed model-plugin wheel can be discovered
and executed without modifying EEGle source. Actual dependency-backed companion
distributions, framework-specific artifact decoding, supported versions, and
installation extras remain Phase 7 integration work. Generic deployment
materialization and digest verification are complete in the Phase 6 runtime
admission boundary.

These fixtures establish **architectural compatibility only**. They do not make
claims about validated fNIRS, Neuropixels, sklearn, Torch, throughput, model
quality, or hardware support.

## 15. Closure boundary

P6-009/P6-010 remove the transitional authorities rather than preserve them as
compatibility APIs:

- artifact-bearing factories use the explicit `model_context_v1` construction
  API and receive only independently verified materializations;
- typed model-state artifacts are restored and exact-hash checked before the
  component is admitted;
- preprocessing operations expose fixed/config-projected parameter
  attestations, including manifest-bound artifact preparation and route-wide
  forbidden-operation proof;
- v1 predictions, the classifier-shaped model built-in, metadata-only target
  registry, local runtime bundle authority, unbound models, string-role
  executor fallbacks, and suite-wide role knobs are absent from the target;
- `eegle.ml`, optional framework extras, legacy applications, and retained
  Phase 7/8 extraction evidence are outside package discovery and target
  imports.

The Phase 6 exit gate is covered by the dedicated P6 tests, the installed
external-wheel fixture, source-boundary and clean-wheel tests, and the complete
suite recorded in `MIGRATION_STATUS.md`.
