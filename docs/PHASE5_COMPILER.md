# Phase 5 Specifications and Compiler

**Status:** First vertical slice implemented; Phase 5 remains in progress  
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
 ExecutionPlan v2 + ExecutionLock + diagnostics
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
- phase component/transition references, duplicate transitions, reachability,
  and existence of a reachable terminal phase;
- model role presence, uniqueness, and exactly one primary role when models are
  present;
- independent deployment permission for any actuator component.

Diagnostics contain `code`, JSON-style `path`, `message`, `severity`, and
structured `details`. For example, an invalid window length is reported at the
specific component configuration path, while a missing clock mapping points to
the deployment mapping collection.

Future-dependent plugins are rejected by descriptor mode resolution in a
causal protocol. The built-in retrospective SOS filter therefore cannot compile
causally but compiles under a retrospective protocol.

## 5. Plan and lock artifacts

`ExecutionPlan` version 2 adds typed:

- component roles, logical stream identities, and required capabilities;
- phase records and transitions;
- deployment placements, resource identities, endpoint identities, and secret
  references.

The existing v1 representation remains readable and retains its original hash
projection. Phase 5 does not invalidate plan-bearing Phase 4 evidence.

`ExecutionLock` independently binds:

- the plan hash;
- protocol, suite, and deployment hashes;
- exact plugin versions, descriptor hashes, distribution, and implementation;
- every compiled component hash;
- Protocol/Suite/Deployment JSON Schema hashes;
- the typed graph hash;
- future content-addressed artifact hashes.

Plan and lock files have explicit atomic readers/writers. Both include their
own content hashes and reject tampering on read. `explain_plan()` returns a safe
structural projection with configuration hashes, and `diff_plans()` labels each
change as scientifically or operationally material.

## 6. Reference evidence

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

Thirteen focused tests cover specification round trips, bounded composition,
privacy/path rejection, deterministic compilation, simulated/live deployment
independence, plugin configuration diagnostics, signal capabilities, port and
phase errors, clock mappings, execution-mode compatibility, explain/diff,
atomic files, lock tampering, and legacy v1 plans. On 2026-07-23, the complete
suite passed 343 tests with five environment-dependent skips under Python
3.14.4 and the four base dependency families. Compile-all and diff whitespace
checks passed; a wheel built with the pinned backend contains every new spec and
compiler module, and its installed public Phase 5 imports pass outside the
source tree.

## 7. Remaining Phase 5 work

This slice does not complete Phase 5. Remaining work includes:

- typed artifact declarations, producers, phase dependencies, and artifact/model
  content hashes;
- fuller phase entry, timeout, retry, resume, and acceptance semantics compiled
  into the engine state model;
- outcome-availability, calibration, adaptation, and role-permission checks;
- richer action capability/authorization compilation before the Phase 6
  provider protocol is finalized;
- explicit defaults for scheduling and process-resource policies;
- event-window primary/shadow, calibration/evaluation, delayed-outcome,
  multi-rate dense/sparse, and authorized simulated-actuator reference suites;
- a compiler-to-runtime component construction boundary that consumes only the
  locked plan;
- CLI exposure, which deliberately remains Phase 7.

Phase 5 must not be marked complete until every exit gate in
[MIGRATION.md](MIGRATION.md) is demonstrated.
