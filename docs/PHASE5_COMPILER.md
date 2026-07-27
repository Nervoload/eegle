# Phase 5 Specifications and Compiler

**Status:** Phase 5 complete; compiler, plan-owned runtime, and reference suites verified
**Architecture authority:** [EEGLE.md](EEGLE.md)  
**Migration authority:** [MIGRATION.md](MIGRATION.md)

## 1. Implemented boundary

The first Phase 5 slice establishes a real compilation boundary:

```text
ProtocolSpec + SuiteSpec + DeploymentSpec
                  │
                  ▼
        schema and semantic checks
                  │
                  ▼
       exact plugin/capability resolution
                  │
                  ▼
             typed port graph
                  │
                  ▼
 ExecutionPlan v1 + ExecutionLock + diagnostics
                  │
                  ▼
 exact locked-plugin construction + generic typed graph
                  │
                  ▼
       plan-owned phase state machine
```

Compilation is read-only with respect to hardware and storage. It resolves
plugin descriptors but does not call factories, open devices, resolve secret
values, or create runtime components. Invalid configurations therefore fail
before deployment side effects.

## 2. Specification authorities

### `ProtocolSpec`

The protocol is the portable scientific claim boundary. Version 1 records:

- protocol identity;
- causal, retrospective, or oracle execution mode;
- human-readable claims;
- typed metric identities and parameters;
- acceptance criteria with explicit comparison operators;
- non-executable annotations.

Metrics and criteria are cross-referenced when the typed object is built. A
criterion cannot refer to a metric absent from the protocol.

### `SuiteSpec`

The suite is portable system intent. Version 1 records:

- logical streams and their signal contracts;
- component kinds, symbolic or exact plugin requests, roles, and configuration;
- typed input/output contracts;
- component-port routes;
- bounded phases, transitions, retries, resume policy, and operator gates;
- phase timeouts and protocol acceptance-criterion references;
- deterministic scheduling, primary/shadow disposition, and bounded
  backpressure policy;
- scheduled and state-transition triggers;
- outcome-use, adaptation-permission, and action-capability requirements;
- external and graph-produced artifact declarations, including producer
  phase/component/port and optional expected content digest;
- clock, recording, and validation policy;
- a single explicit initial phase.

A `SignalContract` can constrain type, unit, channel count, sample rate, and
window length. These constraints are modality-neutral and can describe dense
EEG-like signals, slow physiological streams, sparse event streams, or model
records without making EEG a kernel type.

### `DeploymentSpec`

The deployment contains site-local bindings:

- the concrete plugin/version for components left symbolic by the suite;
- local plugin configuration and in-process/proxy placement;
- resources and their declared signal contracts/capabilities;
- logical-stream-to-resource selectors;
- storage URIs with explicit schemes;
- permission grants and authorization references;
- secret references, never secret values;
- explicit clock mappings and uncertainty.

Relative storage paths and credentials embedded in URIs are rejected. Common
secret-shaped literal fields are rejected recursively. Components refer to
declared secret identities, leaving value resolution to a later deployment
boundary.

## 3. Composition decision D-004

Version 1 uses one explicit `SuiteSpec` base plus an ordered tuple of typed
`SuiteOverlay` values. An overlay may only:

- override configuration keys on an existing component;
- override recording-policy keys;
- override validation-policy keys.

It cannot add or remove components, alter plugin identity, rewrite routes,
change phases, or inherit from another overlay. Unknown component references
and duplicate overlay identities fail. This is intentionally narrower than
JSON Merge Patch: topology changes require a new, inspectable suite version.

## 4. Compiler checks implemented

The compiler currently performs these checks and accumulates stable structured
diagnostics:

- protocol-to-suite and suite-to-deployment identity references;
- exact highest compatible plugin-version resolution;
- plugin kind, configuration schema, execution mode, capability, and resource
  requirements;
- deployment component, resource, stream, and secret references;
- stream resource type, unit, channel-count, and sample-rate compatibility;
- explicit execution clocks, required mappings, mapping strategy compatibility,
  and optional uncertainty bounds;
- declared plugin ports and suite port contracts;
- route endpoint existence, type/contract compatibility, input multiplicity,
  required inputs, and acyclic data flow;
- phase component/transition references, active required-input dependencies,
  duplicate or ambiguous automatic transitions, retry/resume compatibility,
  reachability, and existence of a reachable terminal phase;
- required-artifact declaration, producer endpoint/type, producer-phase
  activation, and dominance checks so a phase cannot require an artifact that
  is unavailable on one of its incoming paths;
- model role presence and exactly one routed primary only for an active phase
  whose policy actually consumes model output; recording, preprocessing,
  calibration, and model-only phases do not acquire classifier requirements;
- trigger target capability and state-transition source checks;
- declared outcome-use routing, required use, and independent adaptation
  permission checks;
- exact deployment grants for every declared actuator action capability,
  including operator-confirmation requirements where a grant demands them;
- phase timeout and acceptance-criterion references.

Diagnostics contain `code`, JSON-style `path`, `message`, `severity`, and
structured `details`. For example, an invalid window length is reported at the
specific component configuration path, while a missing clock mapping points to
the deployment mapping collection.

Future-dependent plugins are rejected by descriptor mode resolution in a
causal protocol. The built-in retrospective SOS filter therefore cannot compile
causally but compiles under a retrospective protocol.

## 5. Plan and lock artifacts

The sole `eegle.execution_plan.v1` representation contains typed:

- the complete compiled graph, including port contracts, routes, and stable
  component order;
- compiled model-use roles, logical stream identities, and required capabilities;
- phase records and transitions;
- scheduling policy, scheduled triggers, and state-trigger rules;
- deployment placements, resource identities, endpoint identities, and secret
  references.
- artifact identities, roles, media types, exact producer endpoints, and
  optional expected digests.

There is no unpublished partial-plan compatibility representation. Phase 4
plan-bearing evidence uses this same complete schema.

`ExecutionLock` independently binds:

- the plan hash;
- protocol, suite, and deployment hashes;
- exact plugin versions, descriptor hashes, distribution, and implementation;
- every compiled component hash;
- Protocol/Suite/Deployment JSON Schema hashes;
- the typed graph hash;
- declared content-addressed artifact hashes.

Plan and lock files have explicit atomic readers/writers. Both include their
own content hashes and reject tampering on read. `explain_plan()` returns a safe
structural projection with configuration hashes, and `diff_plans()` labels each
change as scientifically or operationally material.

## 6. Compiler-to-runtime and phase boundary

The compiled graph is no longer an informational side product. It is embedded
in the immutable plan, covered by the plan and lock hashes, and is the routing
authority used at runtime.

`construct_plan_runtime()` resolves every planned component to the exact plugin
identifier, version, descriptor hash, distribution, implementation, placement,
and configuration locked by the plan. Construction rejects plugin drift,
missing proxy factories, unresolved in-process secrets, graph/descriptor port
drift, and graph-hash drift before a source is read.

`PlanGraphExecutor` executes an active phase subgraph. It:

- admits dense, sparse, or metadata packets by availability time;
- routes values through the typed compiled ports rather than through a manually
  assembled `EngineComponents` object;
- accepts small specialized component protocols and the explicit multi-port
  `GraphNode.process()` escape hatch;
- checks runtime schema identity at every input and output port;
- prevents outputs from becoming available before their inputs;
- queues future-available component outputs until their declared semantic time,
  even when another source input arrives first;
- emits graph, work, failure, and lifecycle evidence in one deterministic
  sequence;
- executes compiled scheduled and state-transition triggers through the same
  deterministic queue as packets and component work;
- prioritizes primary model work over shadow work at the same semantic time,
  with explicit shadow queue limits and failure disposition;
- supports plan-locked fail-run or nonfatal reject-newest backpressure;
- permits source/sink-only, preprocessing-only, model-only, calibration, and
  full model-policy graphs.

The coordinator no longer contains every policy implementation. Its internal
boundaries are now:

```text
runtime/admission.py   source reads, monotonic availability, watermarks
runtime/queueing.py    bounded deterministic heap and queued values
runtime/routing.py     typed dispatch, ports, value identity and availability
runtime/work.py        deadlines and terminal work construction
runtime/graph.py       semantic coordination across those policies
runtime/phases.py      phase state machine, gates, retries and artifacts
```

`ExecutionEngine` owns the compiled phase machine. It enforces required
artifacts and operator entry gates, executes only the phase's active components,
follows unambiguous complete/failed transitions, delegates explicit operator
branches to an `OperatorController`, bounds transitions, restores phase-entry
component snapshots for allowed retries, and closes all sources at termination.
Failed attempts remain in evidence; their audit history is not rolled back.

Phase timeouts are evaluated on the locked semantic clock. Protocol acceptance
criteria are evaluated after a complete phase and can select an explicit
`acceptance_failed` transition. A safe, queue-empty mid-phase boundary can emit
an integrity-checked `EngineCheckpoint`; a fresh runtime restores exact source,
component, admitted-artifact, clock-mapping, identifier, trigger, and
evidence-sequence state.
Checkpoint restoration preserves the phase's original start time and phase-entry
authorization instead of silently beginning a new phase.

Artifacts are graph values, not mutable orchestration side channels. The
`ArtifactProducer` protocol emits a versioned `ArtifactPublication` containing
an `ArtifactReference`, production time, exact component/version, input IDs,
metadata, and optionally carries a small JSON payload in-process. Serialized
semantic evidence contains only its reference, digest, and provenance; content
is stored under the artifact's sensitivity policy. The phase runner accepts only
typed initial references, validates role/media type/digest against the locked
declaration, registers successful phase publications, and exposes them to
later phase-entry gates. Publications from failed or partial attempts never
become available. `persist_engine_run()` materializes small JSON publications
into the content-addressed session store and preserves producer lineage; large
or source-native results remain independently verifiable references.

The compiler now writes explicit graph-event, idle-cycle, phase-transition, and
clock-mapping revision defaults into the plan. Derived processing lineage uses
those locked mapping revisions.

This is the only target compiler/runtime join. The Phase 3 classifier-shaped
engine and `EngineComponents` assembly have been deleted. Phase 3 now records
semantic requirements only; no compatibility alias or second runtime remains.

Generic plan runs already satisfy the Phase 4 persistence contract. They can be
written as plan-bearing evidence bundles with exact admitted packet capture.
Generic bundle-to-plan replay verifies the embedded plan and capture, substitutes
only locked source components with `ReplaySource`, reconstructs every other
component from the exact plugin registry, and compares the resulting evidence.

## 7. Reference evidence

The first reference fixture is
`tests/fixtures/migration/phase5_simulated`. It contains separate human-readable
protocol, suite, and deployment JSON files for causal continuous observation:

```text
deployment-bound packet source
→ identity transform
→ continuous window
→ primary mean-threshold model
→ observe-only policy
```

The source is symbolic in the suite and bound to the dependency-light packet
sequence source only by the simulated deployment. A test binds that same suite
to an independently declared live-capability source descriptor and amplifier
resource without changing the protocol or suite hash.

`tests/fixtures/migration/phase5_calibration_multistream` is a second durable
protocol/suite/deployment family. It combines regular dense neural samples and
sparse task markers, produces a calibration-result artifact from a candidate
model, and gates a later evaluation phase on that artifact. It demonstrates that
calibration needs neither a primary model nor a policy and that phase artifacts
are compiled, executed, and persisted through the same generic boundaries.

`tests/fixtures/migration/phase5_delayed_adaptation` carries a sparse outcome
through an outcome resolver into a permission-gated adaptive component. A
phase-start scheduled trigger and a state-transition trigger execute in the
same queue, and the phase permits integrity-checked checkpoint restoration.

`tests/fixtures/migration/phase5_event_window_actions` joins a 100 Hz dense
neural stream with sparse markers while independently admitting a 10 Hz
auxiliary stream. One causal event window is shared by primary and shadow
models; only the primary route can create an authorized simulated action. The
result persists to a self-describing bundle and replays with equivalent action
receipts.

Forty-one focused Phase 5 tests prove:

- direct execution of the locked classifier graph without `EngineComponents`,
  including interleaved source arrival and declared model latency;
- recording-only and preprocessing-only routes without any model or policy;
- calibration/model execution without a primary role or policy;
- deterministic availability ordering across dense and sparse sources;
- generic phase transitions, required-artifact blocks, and operator gates;
- phase-entry source restoration and retry evidence;
- compiler rejection of forbidden retry semantics;
- runtime rejection of plugin provenance drift; and
- persistence of a generic plan run as an integrity-valid, plan-bearing
  evidence bundle;
- typed graph artifact production, phase-output registration, and durable
  materialization with producer lineage; and
- execution of the durable calibration/mixed-stream reference fixture;
- incomplete-source watermark gating and plan-locked bounded queues;
- component deadline timeout evidence with late-output suppression; and
- bundle-driven reconstruction and equivalence without an engine factory;
- compiled scheduled and state triggers with deterministic evidence ordering;
- checkpoint persistence, tamper rejection, and fresh-runtime mid-phase restore;
- semantic phase timeout and protocol acceptance outcomes;
- primary-first scheduling, bounded shadow shedding, and nonfatal
  reject-newest backpressure;
- outcome/adaptation and action-capability permission rejection before
  construction; and
- the delayed-adaptation and event-window/multi-rate/action fixture families.

On 2026-07-23, all 41 focused tests pass. The complete local suite passes 344
tests with five environment-dependent skips under Python 3.14.4; compile-all
also passes. The real isolated-wheel test rejects every known legacy root module
and verifies the dependency-free `python -m eegle` response.

## 8. Phase 5 closure and Phase 6 boundary

Every Phase 5 exit gate in [MIGRATION.md](MIGRATION.md) is demonstrated. Phase
5 is complete.

The following are deliberately not claimed by this closure:

- a clinical or hardware-safety authorization provider;
- vendor actuator drivers or physical-delivery verification;
- the complete generic outcome matching, duplicate, dispute, expiry, and
  adaptation-state model;
- framework-specific model adapters and generality claims for non-EEG
  modalities; or
- the public CLI, which remains Phase 7 work.

The Phase 5 fixtures establish the compiler and execution contracts those Phase
6 capabilities must use. They do not replace Phase 6's independent
authorization, model, outcome, adaptation, and modality-generality gates.
