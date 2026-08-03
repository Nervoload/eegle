# Phase 8 validation foundation

**Status:** first vertical slice implemented; comprehensive Phase 8 validation
and qualification remain in progress

This slice establishes one vocabulary for what available evidence supports.
It does not replace schema validation, compilation, preflight, runtime
admission, bundle integrity, or replay. Each owning service still makes its
decision; `eegle.validation` projects those decisions into a common report.

## Result contract

The packaged contracts are:

- `eegle.validation_evidence_reference.v1`;
- `eegle.validation_observation.v1`;
- `eegle.validation_result.v1`;
- `eegle.validation_report.v1`.

Every result has a layer, severity, summary, structured observations, bounded
evidence references, and one of these statuses:

| Status | Meaning |
|---|---|
| `pass` | Available evidence satisfies the declared check. |
| `fail` | Available evidence contradicts or violates the declared check. |
| `warning` | The check completed and found a non-fatal condition needing review. |
| `not_applicable` | The validated execution did not exercise that behavior. |
| `insufficient_evidence` | The check requires evidence or a denominator that is absent. |

`insufficient_evidence` is never converted to zero, `pass`, or
`not_applicable`. Report aggregation is deterministic and content-addressed.

## Metric registry and acceptance

The compiler and runtime now share a typed registry for `input_count`,
`emission_count`, `work_count`, `artifact_count`,
`component_emission_count`, `work_status_count`, and `prediction_coverage`.
Compilation rejects unsupported measures, unknown or missing parameters,
invalid active component/port references, incompatible output types, invalid
work statuses, inactive phase references, and non-numeric criterion values.

Prediction coverage requires at least one admitted window. A run with no
windows records an `acceptance_result` with `observed: null`, evidence count
zero, `status: insufficient_evidence`, and a reason; phase acceptance fails
closed.

## Evidence and replay

The semantic evidence registry decodes current runtime payloads through their
domain types where those types exist. It validates record identity, sequence,
emission-clock monotonicity, graph value hashes/types, phase terminals,
acceptance results, model dispositions, adaptation transitions, authorization,
actions, receipts, outcomes, triggers, artifacts, and source observations.
Unknown supplemental record types are warnings; malformed registered records
are failures.

Replay comparison is derived from that current evidence taxonomy plus the
explicit legacy read-only taxonomy. `model_result_disposition` is comparable,
and all four terminal states—emitted, rejected, late, and cancelled—have
pairwise regression evidence. A replay with no comparable records is
`insufficient_evidence`, not equivalent evidence.

## Read-only service

`validate_target`, `validate_project`, and `validate_session` inspect immutable
artifacts without recovery, truncation, finalization, or authoring mutation.
The installed adapter is:

```bash
eegle validate PROJECT_OR_SESSION
eegle validate PROJECT_OR_SESSION --bundle-id BUNDLE
eegle validate PROJECT_OR_SESSION --strict
```

The report currently aggregates verified plan/lock identity, compiled graph
compatibility, bundle integrity, semantic evidence, availability timing,
prediction causality, execution terminals, model-result accounting,
adaptation, action authorization/receipts, protocol acceptance, and replay.
Live-source bundles also carry their required observation taxonomy. Validation
reports absent required clock evidence as `insufficient_evidence`, clean
observations as `pass`, and recorded reconnect or packet-loss events as
`warning` with counts and evidence references.
`--strict` uses the existing stable exit meanings for failure and insufficient
evidence.

## Runtime and integration correctness included in this slice

- Locked project runs append semantic evidence incrementally through the
  existing durable `EvidenceWriter`; application runs do not retain graph
  evidence logs in memory. Component-state snapshots are materialized as their
  records arrive. Writer state tracks every appended boundary, and an exception
  finalizes a failed bundle when possible or leaves an authorized resumable
  writer when finalization itself cannot complete. The same engine evidence
  sink can continue after `EvidenceWriter.resume()` at the verified ledger
  boundary. Supervisors that require hard-process recovery can supply and
  retain `evidence_resume_token` to `run_project()` or `run_locked_plan()`; the
  secret is never written into session or bundle metadata. An unsupervised
  `KeyboardInterrupt` publishes a partial bundle instead of leaving an
  unauthorized open writer.
- LSL preserves raw source timestamps, records the measured source-to-boundary
  correction, and emits clock, reconnect, and packet-loss observations through
  the engine. Simulated compiled execution covers silence, reconnect, gaps,
  cancellation, bounded duration, semantic validation, and bundle persistence.
- The MNE bridge accepts both regular first-sample/period timing and explicit
  per-sample timing used by LSL. It also projects markers to annotations,
  admitted windows to epochs, and MNE raw plus annotations to separate dense
  and sparse replay inputs. Sidecars preserve EEGle timing and lineage that MNE
  cannot represent directly.
- A packaged, hash-verified support matrix separates representability, adapter
  availability, validation, and reference-support claims for LSL, MNE,
  pyRiemann, and generic foundation-model integration capabilities.
- Action inspection uses the domain `request_id` from real typed action
  requests.

## Remaining Phase 8 work

This foundation is not comprehensive scientific validation. The next slices
should add:

1. stream/window/quality and synchronization-uncertainty validators with
   explicit required-observation policies;
2. calibration and predictive-performance metrics with label-blind online
   inference and separate outcome/training evidence;
3. expand the first published performance/fault profile with high-channel,
   sparse-event, repeated clock-fault, process-proxy, and multi-process restart
   qualification; see `PHASE8_PERFORMANCE_FAULT_QUALIFICATION.md`;
4. retained installed-artifact and external-reference qualification for the
   new MNE paths, plus actual pyRiemann and foundation-model adapters;
5. reports/plots derived only from structured results, followed by the release,
   TestPyPI, license, typing, and public-alpha gates.

Real EEG acceptance remains an independent Phase 7 external gate. Its absence
does not block simulated implementation work and must not be represented as
live hardware validation.
