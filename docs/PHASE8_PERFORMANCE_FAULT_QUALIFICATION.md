# Phase 8 performance and fault qualification

**Status:** first cross-boundary performance profile and core fault campaign
implemented; broader modality-scale and process-placement qualification remain
in progress

This qualification slice turns performance and fault behavior into shipped,
machine-readable claims. The authoritative profile is
`eegle/validation/qualification_profile.json`; its typed loader verifies the
schema, ordering, content hash, workload, unit, threshold direction, evidence,
and limitations for every claim.

The budgets are regression floors for deterministic local qualification. They
are not hard real-time, hardware, clinical, or reference-host claims.

## Published budgets

| Boundary | Workload | Budget |
|---|---|---|
| Compact compiled engine | 5,000 dense packets, 2 channels, 4 samples, zero sink retention | at least 250 packets/s |
| Compact compiled engine | same workload | at most 4 ms mean dispatch time per packet |
| Compact compiled engine | same workload under `tracemalloc` | at most 2.5 MiB peak traced memory |
| Runtime queue accounting | 10,000 pending events, 100,000 component/kind queries | at least 200,000 queries/s |
| Durable incremental capture | 250 dense packets with filesystem synchronization | at least 40 packets/s |

The test reads these values from the installed profile rather than duplicating
threshold constants. Each budget names the precise test evidence and its
measurement limitations.

## Profile-led refactors

The initial engine profile retained admitted packets, emissions, work records,
and semantic evidence until `run()` returned even when application persistence
was incremental. At 100, 1,000, and 3,000 packets, peak traced memory was about
0.21, 1.72, and 5.06 MiB. The retained result graph was therefore linear in
session length.

The correction is intentionally bounded:

- `PhaseRecordBuffer` counts every phase record while retaining details only
  when requested, required for phase acceptance/operator decisions, or needed
  to publish artifacts;
- application runs use an incremental admitted-packet capture sink and an
  incremental semantic-evidence sink;
- in-memory recording sinks accept an explicit retention limit and continue to
  count all inputs when retention is zero;
- public engine defaults still retain full results for compatibility and
  interactive inspection.

After the boundary change, the same 100, 1,000, and 3,000 packet profiles used
about 0.06, 0.18, and 0.43 MiB; a 10,000 packet run used about 1.24 MiB. The
remaining growth includes plan/run accounting and is covered by the published
5,000-packet budget. Acceptance-heavy phases still retain the phase details
their current metric evaluators consume; streaming acceptance accumulators are
future work if profiling demonstrates that requirement.

The scheduler profile also found that `EventQueue.count_component()` scanned
the complete heap. Ten thousand repeated queries fell from about 48,000
queries/s at a 1,000-event depth to 5,000 queries/s at 10,000 events. Maintained
component and component/kind counters now keep this operation constant-time;
the same local profiles exceeded six million queries/s. Oldest-component
eviction deliberately remains linear and is disclosed in the profile.

## Fault campaign

| Injected fault | Required outcome |
|---|---|
| Truncated final ledger frame | recoverable verified prefix; no synthesized frame |
| Checksum-corrupt ledger frame | unrecoverable `checksum_mismatch` |
| Deleted bundle artifact | unrecoverable `artifact_missing` |
| Silent live LSL source | bounded `phase_timeout` with terminal evidence |
| LSL reconnect and timestamp gap | reconnect, packet-loss, and clock observations |
| Clock-correction span above compiled uncertainty | source-health warning with measured span |
| Full reject-newest queue | deterministic `queue_full` rejection with stable accounting |

Clock drift is evaluated per component and source-to-target clock mapping, so
independent clock offsets cannot be mistaken for within-source drift. A silent
LSL inlet may still yield a valid clock-correction observation; the phase
timeout is authoritative for silence, while `insufficient_evidence` is reserved
for a genuinely absent required observation.

## Verification

```bash
python3 -m unittest tests.test_phase8_performance_faults
python3 -m unittest tests.test_phase8_validation tests.test_phase7_lsl_integration
python3 -m compileall -q eegle tests
```

Remaining P8-003 work is environmental breadth: repeated multi-process death
and restart campaigns, high-channel and sparse-event scale profiles, process
proxy/transport qualification, and retained reference-host measurements. Real
EEG acceptance remains a separate external Phase 7 gate.
