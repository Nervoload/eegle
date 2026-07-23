# Phase 4 Sessions and Evidence Bundles

**Status:** Initial gate implemented; Phase 4 remains in progress  
**Date:** 2026-07-22  
**Architecture authority:** [EEGLE.md](EEGLE.md)  
**Migration authority:** [MIGRATION.md](MIGRATION.md)  
**Task tracker:** [MIGRATION_STATUS.md](MIGRATION_STATUS.md)

## 1. Scope of this slice

This slice replaces fixed recipe-shaped storage as a target authority. It does
not migrate every old task or worker. The current recipes can continue through
an explicit compatibility view while new execution persists through generic
session, artifact, bundle, and sample-store contracts.

The implemented ownership chain is:

```text
Session
→ ArtifactStore
→ EvidenceBundle
  ├─ semantic EvidenceLedger
  ├─ execution SampleStore references
  ├─ archival raw SampleStore/external references
  ├─ component-state snapshots
  └─ plan, lock, validation, action, receipt, and derived references
```

Execution capture and archival raw recording are deliberately independent.
They may point to the same physical content when that is scientifically true,
but neither role implies the other.

## 2. Session and artifact identity

`Session` owns a stable `session_id`, lifecycle status, optional participant
pseudonym, bundle registry, and versioned `session.json`. Its identity does not
depend on a participant/date/task directory convention.

`ArtifactStore` owns a hashed `.eegle/artifacts.json` manifest. Every finalized
artifact has:

- a namespace and artifact ID;
- a role, media type, size, and SHA-256 digest;
- an embedded relative URI or an external URI;
- a sensitivity class;
- optional component, input-digest, state, and metadata lineage.

Embedded byte/JSON registration is content-addressed. Multiple components,
models, phases, or observers may use the same local artifact ID in different
namespaces without collision.

The narrow `SessionPaths` class is now an artifact-alias view. Constructing it
for a generic session registers old names as compatibility aliases; opening a
historical session constructs the same alias registry in memory without writing
to the source tree. No target runtime module imports it.

## 3. EvidenceBundle v1

`EvidenceBundle` is the hashed, versioned output of one execution. It records:

- session and bundle identity;
- exact plan hash;
- open/complete/partial/failed status and times;
- one semantic evidence ledger;
- zero or more execution captures;
- zero or more archival raw references;
- component-state snapshots;
- other typed artifacts and bundle metadata;
- the last expected semantic sequence.

`EvidenceWriter` is an engine evidence sink. It enforces contiguous sequence
numbers, writes the semantic ledger, accepts separately finalized sample/raw
references, captures canonical component state, and atomically publishes the
bundle manifest. `persist_engine_run` turns a Phase 3 `EngineRunResult` into a
self-describing bundle and extracts the engine's state records into
content-addressed snapshots.

`EvidenceReader` verifies the manifest hash, semantic record hashes and
sequence, every embedded artifact's size/digest, framed capture integrity, and
component-state artifacts. External raw references remain content-addressed
without forcing the data into the session.

## 4. Framing, interruption, and recovery

The semantic ledger and reference packet store share the Phase 2 canonical
framing contract:

```text
format magic
repeated:
  unsigned 64-bit payload length
  canonical JSON payload
  SHA-256(payload)
```

Integrity is structured rather than represented only by exceptions:

- `valid`: every frame and declared artifact verifies;
- `recoverable`: only an incomplete final length/payload is present and the
  last complete byte boundary is known;
- `unrecoverable`: the header, checksum, canonical encoding, record hash,
  sequence, or an interior boundary cannot be proven.

A truncation issue reports artifact ID, frame offset, last complete offset, and
expected/observed bytes. `recover_framed_prefix` copies only the verified prefix
to a new path and never alters the source. The recovered file is verified again
before it is returned.

The historical CLRE1 format remains readable, but its implementation now lives
under `eegle.recording.legacy_capture`. New code must use `SampleStore`.

## 5. SampleStore

The runtime-checkable `SampleStore` protocol defines:

```text
store_id
purpose: execution_capture | archival_raw
open_stream(StreamSpec)
append(DenseSampleBatch | SparseEventBatch | MetadataEvent)
close() -> ArtifactReference
```

`FramedSampleStore` is the dependency-light exact-packet reference
implementation. It records each `StreamSpec` revision before packets bound to
that revision and preserves packet timing, availability, missingness, sequence,
and lineage. It is not a replacement for NWB, BIDS, XDF, Zarr, or source-native
stores; adapters for those formats implement the same protocol or register an
external content-addressed reference.

## 6. Historical readability boundary

`Session.open` recognizes only the selected historical BciPy-style family. It:

- leaves the source tree unchanged;
- exposes known files through registered compatibility aliases;
- hashes existing files into an in-memory restricted artifact inventory;
- marks the session as legacy and read-only;
- does not guess, repair, or reinterpret unsupported formats.

The CLRE1 reader and frozen capture tests remain intact. A future one-time
import command may emit a new portable bundle with provenance pointing to the
original tree; that importer is not a reason to keep legacy paths in the target
runtime.

## 7. Initial acceptance evidence

`tests/test_phase4_evidence_bundles.py` proves:

- namespace isolation and artifact-lineage persistence;
- generic session manifest and artifact registry round-trip;
- mutation-free readability of an existing recipe session;
- independent embedded execution capture and external raw reference;
- component-state content hashes and bundle verification;
- exact dense packet round-trip through `SampleStore`;
- precise recoverable result for a deliberately truncated capture;
- unrecoverable classification for checksum corruption;
- non-destructive verified-prefix recovery;
- direct Phase 3 engine-result bundle assembly;
- runtime protocol conformance.

The historical CLRE1 capture/replay invariants and inhibition tests also pass
after moving their implementation into `recording`.

At this checkpoint, the complete Python 3.12 suite passes 308 tests with five
environment-appropriate skips. Compile-all, diff whitespace validation, wheel
build, and installed-wheel imports of the new recording API also pass.

## 8. Remaining before Phase 4 closure

The initial gate is not the full phase exit. Remaining work includes:

1. Define interrupted open-writer discovery, resume authorization, and bundle
   finalization policy across process restarts.
2. Add retention, export, field/stream inclusion, and deployment-redaction
   policies; resolve D-011 without storing secrets.
3. Register immutable plan/lock, clock mapping, validation, action, and receipt
   artifacts automatically where the compiler/runtime produces them.
4. Add at least one source-native or large-data store integration and prove
   sparse/high-channel-count scaling before broader modality claims.
5. Build the scoped one-time historical importer with explicit
   imported/derived/omitted/invalid reporting.
6. Move selected target clients from `SessionPaths` to artifact identities and
   remove compatibility aliases as their last clients disappear.
7. Complete the remaining Phase 3 outcome and engine-checkpoint work so resumed
   component state can be validated end-to-end, not merely stored.
