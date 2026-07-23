# Phase 4 Sessions and Evidence Bundles

**Status:** Complete; P4-001 through P4-011 implemented and verified
**Date:** 2026-07-23
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
content-addressed snapshots. Engine persistence now requires the immutable
`ExecutionPlan`, verifies its hash against the run, embeds it with the exact
capture, and records engine status, failure, and replay-equivalence ceiling.

`EvidenceReader` verifies the manifest hash, semantic record hashes and
sequence, every embedded artifact's size/digest, framed capture integrity, and
component-state artifacts. External raw references remain content-addressed
without forcing the data into the session and report their content state
separately as `reference_only`, `verified`, `unavailable`, or `mismatch`.

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

Every open `EvidenceWriter` now persists a hashed `writer-state.json` containing
its phase, active ledger, last sequence, bundle inputs, and component snapshots.
`discover_interrupted_runs` reports ledger integrity and whether the writer can
be resumed or its pending finalization completed. Resume requires a recovery
token; only its hash is stored. If the final frame is incomplete, resume writes
the proven prefix to `semantic.recovered-N.eegle` and appends there, leaving the
interrupted source unchanged. The source is registered as an
`interrupted_evidence_log` artifact and the recovered semantic log records its
digest as lineage.

Publication is a restartable three-phase transaction:

```text
open -> finalizing -> finalized
```

Finalization intent is durable before artifact and bundle publication begins.
`complete_interrupted_finalization` can therefore repeat the idempotent publish
steps after interruption. A published bundle is never reopened for append.

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

## 6. Privacy, export, and retention

`ExportPolicy` and `RetentionPolicy` are versioned and content-hashed. The safe
portable-export default:

- includes only public and pseudonymized artifacts;
- omits the source session identifier and participant pseudonym while retaining
  the source bundle and plan hashes;
- excludes external references and never copies their URIs into excluded
  manifest entries;
- requires explicit digest-bound JSON-pointer rules for deployment redaction;
- assigns redacted content a new digest while preserving the source digest,
  bundle hash, and scientific plan hash;
- rejects secret-shaped JSON keys before publishing the export;
- writes a hashed export manifest and content-addressed objects atomically.

`read_portable_export` verifies the manifest hash and every exported object's
size and digest. Retention evaluation returns deterministic actions per
artifact sensitivity and never deletes or changes source data.

## 7. External and source-native artifacts

`register_external_file` hashes a local source-native file and stores a `file:`
reference without copying it into the session. `LocalFileArtifactVerifier`
distinguishes verified content, unavailable content, and size/digest mismatch.
Unavailable external content leaves embedded evidence structurally valid but
limits claims requiring that content; mismatch is an unrecoverable integrity
failure. A real 8 MiB NWB-like fixture proves this boundary without adding an
NWB dependency or claiming general NWB semantic validation.

## 8. Bundle-driven replay

`load_recorded_execution` verifies a bundle, requires one embedded immutable
execution plan and one framed execution capture, restores stream revisions and
packets, and reconstructs the recorded semantic reference. `BundleReplayRunner`
passes the restored plan, packets, streams, and requested replay mode to a fresh
engine factory and uses the existing equivalence comparator.

This bridge deliberately does not instantiate arbitrary Python from the bundle.
Plugin resolution and construction remain compiler/deployment responsibilities.
The replay acceptance test restores a real Phase 3 plan/capture and passes
semantic equivalence through a fresh engine.

## 9. Historical readability boundary

`Session.open` recognizes only the selected historical BciPy-style family. It:

- leaves the source tree unchanged;
- exposes known files through registered compatibility aliases;
- hashes existing files into an in-memory restricted artifact inventory;
- marks the session as legacy and read-only;
- does not guess, repair, or reinterpret unsupported formats.

The CLRE1 reader and frozen capture tests remain intact. `LegacySessionImporter`
now provides the one-time conversion boundary for this selected family. It:

- fingerprints the original file inventory and leaves it unchanged;
- validates JSON, JSONL, text/CSV, NPZ containers, and CLRE1 captures as
  appropriate;
- copies only selected durable scientific artifacts into namespaced,
  content-addressed storage with source URI/digest lineage;
- records producer integrity as unavailable when the historical producer did
  not supply checksums;
- emits a new generic session and evidence bundle;
- reports every source file under explicit `imported`, `omitted`, or `invalid`
  lists, with importer-created identities/report fields under `derived`.

The recording kernel owns generic `LegacyImportRule` and importer mechanics.
Historical study-specific filenames are added by
`eegle.integrations.legacy_sessions`; the broad package-boundary test prevents
reference recipe names from leaking back into recording foundations.

Malformed or unknown source records are not repaired. Presentation artifacts,
debug logs, and replaced status summaries are omitted with explicit reasons.
This importer does not restore historical execution behavior or make legacy
paths part of the target runtime.

## 10. Acceptance evidence

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
- runtime protocol conformance;
- discovery and authorized resume of an open writer;
- non-mutating recovery from a truncated semantic ledger;
- restart and idempotence of an interrupted finalization transaction;
- mutation-free historical import with complete
  imported/derived/omitted/invalid classification;
- rejection of undeclared historical families before output creation;
- recipe-specific import rules remaining an integration profile.

`tests/test_phase4_closure.py` additionally proves:

- safe defaults omit participant identity, restricted artifacts, and external
  URIs while retaining scientific plan identity;
- digest-bound redaction changes deployment content identity without changing
  the source plan identity;
- secret-shaped JSON fails before an export is published;
- portable manifests and objects verify after round-trip;
- retention evaluation is non-mutating;
- a real large source-native file is not copied and reports all four external
  verification outcomes precisely;
- target foundation packages do not reference `SessionPaths`.

The historical CLRE1 capture/replay invariants and inhibition tests also pass
after moving their implementation into `recording`.

At this checkpoint, the complete Python 3.12 suite passes 314 tests with five
environment-appropriate skips. Compile-all, diff whitespace validation, wheel
build, and installed-wheel imports of the new recording API pass. The remote
Windows failure was later reviewed during Phase 3 closure and resolved to two
legacy portability assertions; no Phase 4 storage-contract failure was present.

At final closure, 19 dedicated Phase 4/bundle-replay tests pass. The complete
suite passes 320 tests with five skips. Compile-all and `git diff --check` pass;
the wheel builds, contains `recording.external`, `recording.policies`, and
`replay.bundle`, and imports their public APIs from an isolated installation.

## 11. Closure verdict and follow-on work

Every Phase 4 exit gate in `MIGRATION.md` is satisfied:

1. Engine persistence embeds the immutable plan and exact execution capture;
   replay starts from the bundle and uses a fresh execution engine.
2. Interrupted writes yield precise recoverable/unrecoverable status and
   restartable finalization.
3. Artifact namespaces support multiple phases, models, and observers without
   filename identity.
4. A real large raw source remains external while its reference and local
   content integrity are independently verifiable.
5. Portable export excludes or explicitly redacts deployment-sensitive content
   without changing the recorded scientific plan identity.
6. No target foundation/runtime package depends on `SessionPaths`.

Phase 4 is complete. Follow-on work is deliberately assigned to later or
already-open tasks:

1. Continue attaching clock mapping, validation, action, and receipt artifacts
   as those producers become general runtime/compiler authorities; their
   storage roles are already generic.
2. Keep sparse/high-channel throughput and true NWB/BIDS/Zarr semantic adapters
   as support-level evidence before making broader modality claims, not as a
   reason to couple those dependencies into the Phase 4 kernel.
3. Remove individual compatibility aliases only when their remaining selected
   historical clients are migrated or removed; the target runtime boundary is
   already clean.
4. Keep `EvidenceWriter` append/finalization recovery distinct from the now
   completed Phase 3 engine checkpoint: writer recovery does not reconstruct
   virtual time, queues, source positions, pending outcomes/triggers, component
   execution state, or deterministic IDs.
