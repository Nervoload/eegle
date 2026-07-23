# Phase 3 Single Execution Engine

**Status:** Complete
**Completed:** 2026-07-23
**Architecture authority:** [EEGLE.md](EEGLE.md)  
**Migration authority:** [MIGRATION.md](MIGRATION.md)  
**Task tracker:** [MIGRATION_STATUS.md](MIGRATION_STATUS.md)

## 1. Purpose

Phase 3 establishes the one semantic engine used by live-like simulation and
replay. The completed reference implementation is dependency-light and
scientifically generic. It does not preserve `classify8`, PsychoPy, Go/No-Go
semantics, LSL workers, legacy session paths, or old replay code.

The implemented reference path is:

```text
finite typed source
→ watermark admission and total ordering
→ causal transform
→ bounded materialized dense window
→ finite/validity quality decision
→ primary model, then optional shadow model
→ observe-only policy
→ bounded delayed-outcome matching and eligibility evidence
→ causally safe virtual-time and state-triggered work
→ simulated action command and observed receipt
→ append-only typed evidence
→ captured source replay through a fresh instance of the same engine
→ safe-boundary checkpoint and fresh-engine restoration
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
command/receipt trace comparison. The dependency-light `SimulatedActuator`
test now proves this boundary without making a hardware-safety claim.

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

`WorkRecord` accounts for completed, predicted, rejected, skipped, pending,
timed-out, failed, and cancelled work with component, stage, role, inputs,
start, completion, deadline, reason, and details. Evidence also records
run/component lifecycle, admitted inputs, derived packets, windows, quality
decisions, predictions, outcomes, triggers, state transitions, action commands
and receipts, component state, partial failure, and completion.

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

### Delayed outcomes

Outcomes remain separate from predictions and enter the semantic queue only at
`available_time`; `event_time` never authorizes causal consumption. Packet,
outcome, and trigger ties are locked as packet first, then outcome, then
trigger, with identity-based ordering inside each class.

Every prediction enters a bounded pending registry with an explicit expiry.
Overflow deterministically expires the oldest pending prediction or rejects the
newest, according to `OutcomeRoutingPolicy`. Outcome identity and explicit
prediction IDs drive matching. Evidence distinguishes matched, partially
matched, unmatched, duplicate, malformed/rejected, expired, overflowed, and
still-pending lifecycles. `OutcomeUse` eligibility is evaluated independently
for metrics, calibration, adaptation, and policy. Adaptation eligibility is
recorded with `adaptation_applied: false`; Phase 3 remains observe-only and
never passes outcomes, correctness, conditions, or labels into model input.

### Scheduled and state-triggered work

`ScheduledTrigger` is one-shot virtual-time work targeted at a typed handler.
It carries stable identity, payload, scheduled time, optional deadline, and
parent identity for rescheduling. A time trigger is not dispatched until active
source watermarks make its scheduled time causally safe. Cancellation, missed
deadlines, handler failure, completion, and rescheduling all produce terminal
work and evidence.

`StateTriggerRule` matches typed `StateTransition` records by source, kind, and
status, then schedules the same generic trigger contract. Rules contain no task,
recipe, trial, or domain vocabulary.

### Engine checkpoint and fresh-instance restoration

`EngineCheckpoint` is an integrity-checked safe-boundary snapshot between
semantic items. It captures:

- the execution and plan hashes plus binding/version/placement identities;
- deterministic ID counters, partial-run status, semantic-item count, virtual
  time, last dispatched frontier, scheduler settings, and clock-mapping
  revisions;
- source positions, stream identities/revisions, watermarks, exhaustion, and
  source-content identity;
- ordered packet, outcome, and trigger queues;
- captured inputs, predictions, pending-prediction expiries, outcome and trigger
  disposition sets, work, cancellation, and fired state-trigger rules;
- hash-verified restorable component snapshots;
- the exact immutable evidence prefix, its next sequence, and its canonical
  digest.

Restoration is accepted only into a fresh `ExecutionEngine` with fresh component
and source instances. Plan, execution, partial-run status, scheduler,
clock-mapping, component/version/placement, state-capability, outcome-policy,
state-trigger, source-content/revision, component-state-hash, evidence-frontier,
prefix-digest, and checkpoint-integrity mismatches are rejected. A stateful
component that cannot both snapshot and restore causes checkpoint creation or
restoration to fail explicitly.

The resumed result contains the verified prefix exactly once; a resumable sink
is seeded with that prefix before new appends. Lifecycle records for checkpoint
creation and process restart are intentionally additional evidence. At the
declared `semantic` equivalence level, uninterrupted and checkpointed/restored
runs produce the same packet/window/model/outcome/trigger/action/state/work
trace and preserve work, prediction, transition, command, and receipt IDs.

## 4. Replay architecture

`ReplaySource` exposes captured packets through the same finite source contract
as simulation. Original-availability and accelerated-causal modes both preserve
original virtual availability and ordering; pacing against wall time is an
outer adapter concern. `ReplayRunner` requires a factory that creates fresh
components and a fresh `ExecutionEngine`, preventing state leakage from the
reference run.

The comparator filters to semantic evidence, including outcome lifecycles,
triggers, transitions, commands, and receipts, applies the declared equivalence
ceiling, and reports the first differing path for every divergent record. The
counterfactual test changes only the shadow threshold and localizes the result
to the shadow prediction label while primary results remain unchanged.

## 5. Package surface introduced

```text
eegle.runtime.context       deterministic IDs and concrete execution context
eegle.runtime.checkpoints   integrity-checked engine checkpoint record
eegle.runtime.outcomes      outcomes, uses, and bounded routing policy
eegle.runtime.scheduling    watermarks, queues, triggers, and placement policies
eegle.runtime.engine        bindings, lifecycle, scheduling, routing, result
eegle.runtime.state         WorkRecord terminal/pending accounting
eegle.processing.windows    DenseWindow and snapshot-capable builder
eegle.models.builtins       dependency-light mean-threshold reference model
eegle.actions.policies      observe-only policy
eegle.actions.simulated     dependency-light replay actuator substitution
eegle.recording.sinks       append-only and verified-prefix in-memory sink
eegle.streams.synthetic     finite snapshot/restorable packet source
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
- explicit cancellation;
- availability-time-only delayed outcomes with label-blind predictions;
- deterministic matching, duplicate/rejection, independent-use, bounded
  overflow, expiry, pending, and adaptation-eligibility evidence;
- packet/outcome/trigger tie-breaking and causally safe time triggers;
- state-trigger scheduling, deadline, cancellation, failure, and rescheduling;
- integrity, plan, component, evidence-prefix, and source mismatch rejection;
- fresh-source/component/engine restoration with uninterrupted semantic
  equivalence and stable semantic IDs;
- simulated action command/receipt equivalence in replay and after restoration.

The focused module passes 27 tests. In an isolated Python 3.12.13 base
environment, the complete repository passes 330 tests with five
environment-appropriate skips. Compile-all over `eegle`, `tests`, and
`examples`, `git diff --check`, wheel construction, wheel content inspection,
and installed-wheel imports of runtime, replay, actions, and recording
boundaries pass.

## 7. Supported-version evidence and closure boundary

The first observed remote five-job matrix is GitHub Actions run
[`29967855396`](https://github.com/Nervoload/eegle/actions/runs/29967855396) at
commit `511e8f64580d8d425db2b72e89cf0982c7b8716c`. Linux Python 3.11, 3.12, and
3.13 and macOS Python 3.12 passed installation, compile, public imports, and the
full suite. Windows Python 3.12 passed installation, compile, and public imports,
then failed two legacy tests:

- `test_attention8_cli_defaults_to_attention_config_and_protocol` expected a
  POSIX slash but received the valid Windows path
  `configs\\forward_attention_lapse_go_nogo8.json`;
- `test_session_creation_does_not_reuse_same_second_directory` expected a
  `-01` suffix that the Windows run did not produce.

Those failures are recorded as legacy portability debt, not an engine semantic
failure, and Phase 3 does not weaken platform-neutral contracts to hide them.
The closure implementation above is locally verified on Python 3.12; it has not
yet been pushed through a new remote matrix, so no stronger current Windows
claim is made.

Phase 4 `EvidenceWriter` recovery remains a separate storage guarantee. It can
resume append/finalization from a verified immutable ledger prefix; it does not
restore engine virtual time, queues, source positions, pending outcomes or
triggers, component execution state, or deterministic ID allocation.
