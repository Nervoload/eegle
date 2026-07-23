# Phase 3 Single Execution Engine

**Status:** First vertical slice implemented; Phase 3 remains in progress  
**Architecture authority:** [EEGLE.md](EEGLE.md)  
**Migration authority:** [MIGRATION.md](MIGRATION.md)  
**Task tracker:** [MIGRATION_STATUS.md](MIGRATION_STATUS.md)

## 1. Purpose

Phase 3 establishes the one semantic engine used by live-like simulation and
replay. The current slice is intentionally dependency-light and scientifically
generic. It does not preserve `classify8`, PsychoPy, Go/No-Go semantics, LSL
workers, legacy session paths, or old replay code.

The implemented reference path is:

```text
finite typed source
→ watermark admission and total ordering
→ causal transform
→ bounded materialized dense window
→ finite/validity quality decision
→ primary model, then optional shadow model
→ observe-only policy
→ append-only typed evidence
→ captured source replay through a fresh instance of the same engine
→ graded equivalence or localized divergence
```

## 2. Locked decisions

### D-006: deterministic hybrid scheduling

The semantic coordinator is synchronous and deterministic over virtual time.
Sources or component proxies may use threads, subprocesses, devices, or remote
services internally, but arrival-thread timing never becomes scientific order.

Each source publishes a monotonic availability watermark. The engine dispatches
only packets at or before the minimum active-source watermark. Ties use:

```text
availability time
→ source component id
→ stream id and revision
→ sequence
→ packet id
```

Late packets and queue overflow follow explicit reject-or-fail policies. Both
produce terminal work evidence. Idle sources can leave a run partial with
pending work rather than causing the engine to guess that no earlier input will
arrive.

### D-007: one semantic engine across process boundaries

In-process execution is the reference. `subprocess_proxy` and `external_proxy`
placements identify typed proxies, not alternate engines. Lifecycle evidence
records placement and endpoint identity. The coordinator still owns admission,
deadline, cancellation, backpressure, terminal status, and evidence.

The current slice proves the boundary metadata and identical component call
contract. Cross-process health and transport implementation remains deployment
work and must preserve this contract.

### D-012: conservative replay claims

The comparison level is never stronger than the weakest relevant component:

- `bitwise`: canonical evidence payload bytes;
- `numeric`: recursive comparison, default `rtol=1e-7`, `atol=1e-9`;
- `semantic`: normalized labels, decisions, quality, work states, state hashes,
  and action intent;
- `trace`: ordered component/role/lineage/control-flow projection;
- `non_replayable`: no equivalence claim.

When a caller requests bitwise comparison from a graph whose weakest component
declares semantic equivalence, the report explicitly downgrades to semantic.
Hardware replay is defined as observe-only or simulated substitution plus
command/receipt trace comparison; an actuator integration test is still due.

## 3. Implemented runtime contracts

### Execution context and IDs

`RuntimeExecutionContext` carries execution/component identity, component
version, execution mode, virtual current time, clock-mapping revisions, and a
deterministic execution-local ID source. Identical ordered traces therefore
produce stable semantic identities without using wall time or random UUIDs.

### Materialized dense windows

`DenseWindow` supplies the missing typed model input between packet buffering
and inference. It carries:

- immutable samples-by-channels values and optional validity mask;
- stream id and revision;
- sample sequence and window time bounds;
- output availability;
- exact contributing transformed-packet IDs;
- component/version, clock-mapping, stream-revision, and availability lineage.

`ContinuousWindowBuilder` supports overlapping fixed-sample windows, retains
fewer than one window of samples after each update, rejects discontinuous or
contract-changing input, and snapshots/restores pending samples exactly.

### Work and lifecycle evidence

`WorkRecord` accounts for predicted, rejected, skipped, pending, timed-out,
failed, and cancelled work with component, stage, role, inputs, start,
completion, deadline, reason, and details. Evidence also records run/component
lifecycle, admitted inputs, derived packets, windows, quality decisions,
predictions, component state, action requests, partial failure, and completion.

The engine writes through an abstract append sink while retaining the local
typed result needed for immediate validation. Dense packet/window evidence is a
metadata-and-content-hash reference; arrays remain in the separate execution
capture rather than being duplicated in the semantic log. Durable bundle and
sample-store assembly remains Phase 4.

### Primary and shadow execution

Exactly one primary role is required. Models are ordered primary first, then
candidate, shadow, and observer by locked priority. All receive the same
immutable `DenseWindow` and predictions must preserve its admitted input IDs.
Shadow/candidate work may be skipped at a declared queue threshold; this never
changes primary inputs. A timed-out primary cannot reach policy. The reference
policy is observe-only.

## 4. Replay architecture

`ReplaySource` exposes captured packets through the same finite source contract
as simulation. Original-availability and accelerated-causal modes both preserve
original virtual availability and ordering; pacing against wall time is an
outer adapter concern. `ReplayRunner` requires a factory that creates fresh
components and a fresh `ExecutionEngine`, preventing state leakage from the
reference run.

The comparator filters to semantic evidence, applies the declared equivalence
ceiling, and reports the first differing path for every divergent record. The
counterfactual test changes only the shadow threshold and localizes the result
to the shadow prediction label while primary results remain unchanged.

## 5. Package surface introduced

```text
eegle.runtime.context       deterministic IDs and concrete execution context
eegle.runtime.scheduling    watermarks, lateness, queue, placement policies
eegle.runtime.engine        bindings, lifecycle, scheduling, routing, result
eegle.runtime.state         WorkRecord terminal/pending accounting
eegle.processing.windows    DenseWindow and snapshot-capable builder
eegle.models.builtins       dependency-light mean-threshold reference model
eegle.actions.policies      observe-only policy
eegle.recording.sinks       append-only in-memory evidence sink
eegle.streams.synthetic     finite packet source and availability helper
eegle.replay.source         captured source and replay modes
eegle.replay.runner         fresh same-engine replay
eegle.replay.compare        graded comparison and divergence localization
```

The built-in registry now also constructs the continuous window, reference
model, and observe-only policy. These are reference components, not claims that
mean-threshold classification is a useful scientific model.

## 6. Acceptance evidence

`tests/test_phase3_execution_engine.py` currently proves:

- bounded window causality, lineage, round-trip, snapshot, and restore;
- a full dependency-light primary/shadow observe-only trace;
- shared primary/shadow admitted inputs and primary-first evidence order;
- original-availability and accelerated-causal same-engine replay;
- conservative equivalence downgrade and localized counterfactual divergence;
- primary deadline accounting;
- subprocess-proxy placement evidence;
- quality rejection before inference;
- deterministic shadow shedding and graceful shadow failure;
- optional component start/stop lifecycle and terminal transform failure;
- multi-source availability ordering;
- late-packet rejection;
- bounded-queue backpressure and pending watermarks;
- explicit cancellation.

## 7. Remaining Phase 3 closure work

The first slice satisfies the central live-like/replay and deterministic
scheduling gate, but Phase 3 is not closed yet:

1. Route delayed outcomes through the engine, bound pending predictions, record
   match/expiry/duplicate/rejection, and emit observe-only adaptation
   eligibility without label leakage.
2. Add general scheduled and state-triggered component work beyond packet-driven
   execution.
3. Resume an engine from a partial-run checkpoint, not only restore an
   individual window builder.
4. Prove action substitution/receipt comparison with a simulated actuator.
5. Observe the first remote Linux/macOS/Windows supported-version matrix and
   record platform-specific failures without weakening the semantic contract.

Until these close, the engine is the correct new foundation but not yet a
complete Phase 3 runtime release.
