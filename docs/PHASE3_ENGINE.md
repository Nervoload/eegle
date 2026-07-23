# Phase 3 Semantic Engine Requirements

**Status:** Semantic requirements accepted; provisional implementation retired

**Accepted:** 2026-07-23

**Consolidated into the plan-owned engine:** 2026-07-23

**Architecture authority:** [EEGLE.md](EEGLE.md)

**Migration authority:** [MIGRATION.md](MIGRATION.md)

**Task tracker:** [MIGRATION_STATUS.md](MIGRATION_STATUS.md)

## 1. Purpose and authority

Phase 3 established the semantic requirements for EEGle's single execution
engine. Its first classifier-shaped implementation was deliberately a temporary
proof, not a public API. Phase 5 proved that a compiler-owned typed graph can
execute classifier, recording-only, preprocessing-only, calibration, mixed
dense/sparse, and multi-phase systems. The temporary `EngineComponents`
assembly and `eegle.runtime.engine` implementation have therefore been deleted.

There is now one v1.0 authority:

```text
ProtocolSpec + SuiteSpec + DeploymentSpec
→ compiler and semantic passes
→ immutable graph-bearing ExecutionPlan v1
→ exact PlanRuntime construction
→ ExecutionEngine phase orchestration
→ PlanGraphExecutor scheduling and routing
→ EvidenceBundle
→ replay through the same locked graph
```

Phase 3 remains useful as a requirements record. It must not be used to restore
the deleted classifier API or to justify a compatibility adapter.

## 2. Accepted semantic requirements

### Deterministic causal scheduling

- Scientific order is based on declared availability time, not arrival-thread
  or wall-clock timing.
- Active sources expose monotonic availability watermarks. An event cannot
  dispatch while an incomplete source might still produce an earlier event.
- Ties are resolved deterministically using component, stream/revision,
  sequence, value identity, and queue order.
- Queues are bounded by the immutable plan and overflow produces a precise
  failed work/run result.
- Component output availability cannot precede input availability.
- Per-component semantic deadlines are plan-locked; late outputs are suppressed
  and the work item terminates as `timed_out`.
- Cancellation, partial source progress, component failures, retries, and
  shutdown are explicit result/evidence states.

### One engine across topology and placement

The engine must not require a classifier topology. A phase may be:

- acquisition or recording only;
- preprocessing or conversion only;
- continuous or event-window model execution;
- calibration without a primary model or policy;
- primary/shadow/candidate comparison;
- outcome, adaptation, policy, and action routing;
- validation or artifact production.

In-process, subprocess, and external placements are component implementations
or proxies around one coordinator. They are never alternate engines.

### Explicit work and evidence

Every bounded unit of work reaches a terminal or pending state: completed,
predicted, rejected, skipped, pending, timed out, failed, or cancelled. Evidence
preserves typed inputs, emissions, lineage, phase transitions, work status,
component state, artifact publication, decisions, and actions without embedding
large raw arrays in the semantic ledger.

### Conservative replay claims

Replay comparison is capped at the weakest relevant component declaration:

- `bitwise`: canonical evidence payload bytes;
- `numeric`: recursive values with declared tolerances;
- `semantic`: values, decisions, state, lineage, and intent;
- `trace`: ordered component and control-flow projections;
- `non_replayable`: no equivalence claim.

Hardware actions are replaced by observe-only or simulated adapters for replay.
EEGle compares command intent and receipts; it does not claim that replayed
hardware delivery occurred.

## 3. Current implementation mapping

| Requirement | Current authority |
|---|---|
| Immutable runtime authority | `eegle.compiler.plan.ExecutionPlan` |
| Exact locked construction | `eegle.runtime.plan_runtime.PlanRuntime` |
| Phase gates, transitions, retries, lifecycle | `eegle.runtime.phases.ExecutionEngine` |
| Typed routing, virtual time, watermarks, queues, deadlines | `eegle.runtime.graph.PlanGraphExecutor` |
| Work and state records | `eegle.runtime.state` |
| Component snapshots | `PlanRuntimeSnapshot` plus component protocols |
| Evidence and capture persistence | `eegle.recording` |
| Capture-backed source substitution | `eegle.replay.source` |
| Plan and bundle replay | `eegle.replay.runner` and `eegle.replay.bundle` |
| Graded equivalence | `eegle.replay.compare` |

`ExecutionEngine.from_plan()` is the only construction route exported from
`eegle.runtime`. Callers do not assemble transforms, windows, models, or
policies manually.

## 4. Consolidation evidence

The Phase 5 execution tests currently prove:

- classifier execution from a locked graph;
- recording-only and preprocessing-only graphs without a model or policy;
- calibration/model execution without a primary or policy;
- dense and sparse availability ordering;
- incomplete-source watermark gating;
- plan-locked queue failure and component deadlines;
- phase transitions, operator gates, artifact requirements, and bounded retry;
- exact component snapshot restoration at a retry boundary;
- plugin provenance drift rejection;
- graph-produced artifact registration and persistence;
- plan-bearing evidence-bundle replay without an engine factory;
- scheduled and state-transition triggers in the graph queue;
- primary-first/shadow scheduling and nonfatal backpressure disposition;
- semantic phase timeout and acceptance decisions; and
- integrity-checked fresh-runtime mid-phase checkpoint restoration.

On 2026-07-23, the complete local repository suite passes 344 tests with five
environment-dependent skips after deletion of the temporary engine and its
API-shaped test module. All 41 focused Phase 5 tests pass; no semantic skip
remains for P5-008.

The sole coordinator now delegates source admission/watermarks, bounded
queueing, typed routing, and deadline/work construction to separate runtime
modules. Trigger and restoration convergence extended that coordinator rather
than rebuilding a second monolithic engine.

## 5. Convergence closure

P5-006 through P5-008 are complete in the plan-owned graph. Phase 3's temporary
classifier proof is no longer an implementation dependency or API authority.

Phase 6 still owns the complete model, outcome matching, adaptation-state, and
independent action-authorization semantics. The Phase 5 fixtures establish
their typed availability and permission boundary without claiming hardware
safety, physical-delivery validation, or complete outcome expiry/dispute logic.
