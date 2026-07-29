# First-party LSL integration

**Implementation status:** simulated validation complete; real EEG observe-only
acceptance pending

The provisional `eegle.integrations.lsl` package binds Lab Streaming Layer to
the general EEGle stream and plugin contracts. It is not a vendor profile and
does not change portable scientific intent. Install it with:

```bash
python -m pip install "eegle[live]"
```

The base distribution contains dependency-lazy descriptor code, but imports
neither `pylsl` nor its native library until LSL detection, a source, or an
outlet is constructed.

## Implemented boundary

- regular or irregular numeric LSL chunks become `DenseSampleBatch` values;
- marker chunks become `SparseEventBatch` values;
- other irregular/string streams become `MetadataEvent` values;
- dense, sparse, and metadata outlet descriptors publish the corresponding
  records without introducing a second runtime;
- selectors require `uid`, `source_id`, or the exact `name`/`type` pair and
  reject zero or multiple matches;
- inlets enable liblsl clock synchronization; optional dejittering also enables
  monotonization, while the default preserves unsmoothed synchronized times;
- bounded reconnect re-resolves the same exact selector and never falls over to
  another matching-looking stream;
- regular-stream timestamp gaps advance packet sequence identity and expose a
  typed estimated-loss observation;
- discovery emits typed source, channel, unit, nominal-rate, clock, reconnect,
  and packet-loss capabilities for the normal Phase 7 deployment proposal.

LSL post-processing is explicit because liblsl documents that clock-sync maps
remote timestamps into the local clock domain and that enabling post-processing
means original timestamps are no longer recoverable. Liblsl also documents
recoverable inlet reconnection by stream UID. See the upstream
[post-processing flags](https://labstreaminglayer.readthedocs.io/projects/liblsl/ref/enums.html),
[stream inlet and time-correction contract](https://labstreaminglayer.readthedocs.io/projects/liblsl/ref/inlet.html),
and [time-synchronization guide](https://labstreaminglayer.readthedocs.io/info/time_synchronization.html).

## Discovery and preflight

Bounded CLI discovery is opt-in:

```bash
eegle detect my-project --lsl --lsl-wait 2
```

The result reports one of:

| Support level | Meaning |
|---|---|
| `unavailable` | `pylsl` or its native library could not be imported. |
| `simulated_validated` | The adapter passed automated simulated-network acceptance; no real hardware claim is made. |
| `live_observe_only_validated` | A named, retained real-run acceptance record exists. This level is not yet claimed. |

Review an exact proposal before compiling it. A detected stream does not grant
authorization, infer a vendor profile, or prove electrode quality. After
compilation, `eegle preflight PROJECT` verifies the exact lock, plugin hashes,
observed stream/channel/unit/rate facts, clock bound, storage, placement, model
artifacts, provider availability, safe-state reports, and operator gates.

## Real EEG observe-only acceptance

This gate is intentionally open as of 2026-07-29; the current environment has
no `pylsl` installation or live EEG outlet. P7-010 must remain `in_progress`
until the following non-participant run is completed and its redacted evidence
identity is recorded here:

1. Start one EEG outlet with a stable `source_id`/UID and known channel order,
   units, and nominal rate. No policy, authorization provider, or actuator may
   be present.
2. Install `eegle[live]`, run bounded `detect --lsl`, and retain its exact
   support, stream, selector, and clock observations.
3. Produce and review a deployment proposal for the recording-only portable
   suite. Resolve all ambiguity explicitly.
4. Compile the proposal, run preflight, and require matching channels, units,
   measured nominal rate, storage, and clock uncertainty.
5. Run observe-only through the normal locked engine and evidence path; stop at
   the declared operator boundary.
6. Verify the session bundle and confirm the portable suite hash equals the
   separately compiled simulation suite hash while the deployment plan hashes
   differ.
7. Record the date, OS, Python/pylsl/liblsl versions, non-sensitive stream
   identity, plan hash, bundle hash, observed packet-loss/reconnect facts, and
   limitations. Do not store participant data in Git.

Until that record exists, documentation and machine output must never claim
more than `simulated_validated`; an installation without the optional dependency
reports `unavailable`.
