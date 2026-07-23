# EEGle Phase 2 Foundation Contracts

**Status:** Implemented, closure-hardened, and locally verified
**Date:** 2026-07-22  
**Architecture authority:** [EEGLE.md](EEGLE.md)  
**Migration authority:** [MIGRATION.md](MIGRATION.md)

Phase 2 establishes the modality-neutral types and boundaries required before
the single execution engine is built. These are alpha foundation contracts, not
the Phase 5 suite compiler or Phase 7 stable public API.

## 1. Decisions resolved

### D-002 — evidence storage v1

EEGle v1 uses a small evidence manifest plus specialized stores:

- normalized control and semantic evidence is appended as canonical JSON in a
  length-prefixed binary frame;
- every frame has a SHA-256 checksum and a 64 MiB safety limit;
- an incomplete final frame is detectable and earlier complete frames remain
  readable for explicit recovery;
- the manifest content-addresses record logs and all embedded or external
  artifacts;
- large dense samples and modality-native archival data use `SampleStore` or an
  external content-addressed reference rather than one universal container;
- manifest status distinguishes open, complete, partial, and failed bundles.

The framing format is an execution-evidence primitive, not a substitute for
NWB, BIDS, XDF, Zarr, or a source-native archival store. Phase 4 will add bundle
lifecycle, recovery policy, privacy classification, and the historical
importer.

### D-003 — canonical serialization and hashing v1

Canonical values are explicit JSON data serialized as UTF-8 with:

- Unicode strings and object keys normalized to NFC;
- keys sorted lexicographically;
- compact separators and no insignificant whitespace;
- finite JSON numbers only;
- negative zero normalized to positive zero;
- tuples represented as JSON arrays;
- SHA-256 digests represented as `sha256:<lowercase hex>`.

Paths, arrays, bytes, sets, datetimes, and arbitrary objects are rejected unless
their owning schema explicitly converts them to JSON. This prevents local path,
dtype, timezone, and implicit-object behavior from entering a portable lock.
Plan, evidence-record, evidence-manifest, transform-state, and artifact hashes
all use this implementation.

### D-005 — plugin distribution

EEGle uses a hybrid distribution model:

- the base wheel contains only small, broadly useful built-ins;
- first-party integrations may be extras or companion distributions according
  to dependency and release needs;
- third parties publish independent wheels using the `eegle.plugins` Python
  entry-point group;
- every discovered object is a complete executable `PluginDescriptor`, not
  registry metadata alone;
- resolution locks an exact version, implementation, distribution, descriptor
  hash, ports, capabilities, state behavior, determinism, and equivalence.

An independent fixture distribution outside `eegle/` is assembled as a real
`py3-none-any` wheel, installed into an isolated target, discovered through its
installed entry-point metadata, schema-validated, constructed, and exercised
through the same packet contract as built-ins.

### D-010 — Python and base dependencies

The target branch now declares:

```text
Python >=3.11
NumPy >=2.0,<3
SciPy >=1.14,<2
jsonschema >=4.23,<5
packaging >=24,<27
```

There is no artificial Python upper bound. Repository CI covers Python 3.11,
3.12, and 3.13 on Linux, plus Python 3.12 on macOS and Windows. Phase 2 was
locally installed and tested from a built Python 3.12 environment with only
base dependencies; optional research, transport, task, UI, and vendor packages
were deliberately unavailable. Remote matrix execution remains release
evidence rather than a claim made by this local verification record.

### D-013 — maintained processing scope

The initial base processing set is intentionally small:

- a bounded sequence-aware buffer with eviction accounting;
- identity transformation;
- stateful causal second-order-section filtering;
- explicit retrospective zero-phase second-order-section filtering;
- continuous and event window specifications;
- a generic finite/validity-mask quality gate.

Specialized referencing, artifact correction, resampling, spectral analysis,
feature extraction, modality methods, and task semantics are not implied by the
base. They can be selected later when they strengthen the four competencies.

## 2. Data-plane contracts

### Streams and channels

`ChannelSpec` records stable identity, channel kind, unit, sensor/anatomical
references, and explicit geometry metadata. `StreamSpec` records an immutable
positive revision, modality, content kind, rate model, clock, ordered channels,
portable numeric dtype for dense streams, sample rate where regular, coordinate
frame, geometry reference/revision, and missing-data policy.

The core does not assume EEG, microvolts, scalp positions, regular sampling, or
low channel counts.

### Dense samples

`DenseSampleBatch` uses a strict `samples × channels` array. Channel order is
explicit and unique. A batch binds to an exact positive stream revision and has
a stable identity, contiguous sequence range, source timing, boundary
receive/availability timing, optional lineage, and one of two timing models:

- regular: first sample time plus a positive sample period;
- irregular: one nondecreasing clock-bearing time per sample.

Arrays are copied and made read-only. Non-finite values require a same-shape
validity mask and must be marked invalid. Invalid values serialize as JSON
`null`; validity, rather than the numeric placeholder, carries their meaning.

### Sparse and metadata events

`SparseEventBatch` represents spikes, markers, responses, detections, and
annotations without densification. `MetadataEvent` represents time-varying
stream state such as geometry, montage, impedance, calibration, or device
changes.

### Clocks

Every `TimePoint` carries a clock identity. `ClockMapping` has a positive
revision, an explicit target-clock availability time, optional supersession,
and uses the mapping:

```text
target_seconds = offset_seconds + scale * source_seconds
```

It includes uncertainty, a source-clock validity interval, measurement time,
and provenance. Mapping a point requires an explicit `as_of` time and fails if
the mapping was not yet available, if the clocks are wrong, or if the source
point falls outside the validity interval.

## 3. Semantic evidence records

Phase 2 adds versioned round-trippable records for:

- predictions and exact admitted-input lineage;
- quality decisions and reason/metric evidence;
- explicit rejections;
- delayed outcomes and permitted uses;
- state transitions with prior/resulting hashes and triggers;
- action commands;
- independent deployment authorization decisions;
- actuator receipts and reported delivery;
- artifact references, evidence records, and evidence manifests.

Predictions do not include outcome or task-label fields. Their lineage must
exactly match admitted input IDs and carries the latest input availability in
the execution clock, stream revisions, clock-mapping revisions, component
version, and component-state hash. A prediction cannot be produced before that
availability frontier. Outcomes are separate and declare whether they may be
used for metrics, calibration, adaptation, or policy.

## 4. Component and plugin contracts

The structural protocols are:

```text
Source.read
Transform.update
WindowBuilder.update
QualityGate.evaluate
Model.predict
OutcomeResolver.update
Adapter.update
Policy.decide
Actuator.submit
Sink.append
StatefulComponent.snapshot_state / restore_state
```

`ExecutionContext` supplies execution/component identity and version,
execution mode, current logical time, active clock-mapping revisions, and
deterministic record-ID allocation. Packet transforms and quality gates consume
that context uniformly; raw NumPy-only signatures are internal numerical
details rather than engine component contracts.

`PluginDescriptor` adds:

- stable ID and validated version;
- component kind;
- JSON Schema 2020-12 configuration;
- typed input and output ports;
- causal/retrospective/oracle support;
- determinism and equivalence level;
- state behavior and resource requirements;
- executable factory;
- implementation and distribution provenance.

The registry can hold multiple versions, resolve PEP 440 version constraints,
validate configuration before construction, validate execution-mode support,
reject factories missing the methods or snapshot behavior claimed by their
descriptor, register dependency-light first-party built-ins, and discover
independent distributions. Reusable behavioral contract assertions exercise
the exact packet/context call and typed return path because runtime-checkable
protocols alone cannot validate Python signatures.

## 5. Processing causality

Every transform exposes machine-readable `TransformCapabilities`.

The causal SOS filter component:

- has no lookahead and does not require future data;
- retains exact filter state between chunks;
- snapshots its coefficient hash, channel count, state, and state hash;
- rejects restoration from different coefficients, channels, or corrupted
  state;
- consumes and produces `DenseSampleBatch`, advances output availability to the
  component time, and records the exact input availability and pre-update state
  used by the result.

The retrospective SOS filter declares that it requires future samples and
supports only retrospective and oracle execution. Asking it to validate for a
causal plan fails before processing. Phase 5 will apply the same capability
check during compilation.

## 6. Package boundaries established

```text
eegle.actions       commands, authorization, receipts
eegle.compiler      canonical locks and immutable plan draft
eegle.plugins       executable descriptors, registry, protocols
eegle.processing    bounded buffers, transforms, windows, quality
eegle.recording     evidence, framing, artifacts, stores
eegle.runtime       outcomes and state records; engine reserved for Phase 3
eegle.specs         JSON Schema validation foundation
eegle.streams       channels, clocks, packets, sources
eegle.models        contracts, bundles, predictions, roles, calibration
eegle.integrations  explicitly optional/application-specific behavior
```

The old task environment no longer occupies `eegle.runtime`; it moved to
`eegle.integrations.task_environment`. The new runtime namespace does not mutate
`HOME`, configure PsychoPy, resolve repository-root paths, or import task
frameworks.

The top-level `eegle` namespace is intentionally reduced to version and
`ExecutionMode` during alpha migration. The legacy `streams` and `models`
package facades no longer import LSL or the combined realtime model module.

## 7. Verification contract

Phase 2 acceptance verifies:

- all dense, sparse, metadata, clock, prediction, quality, rejection, outcome,
  transition, command, authorization, and receipt records round-trip;
- canonical hashes are invariant to object order, Unicode normalization, and
  signed zero, while non-finite or implicit values fail;
- plan and evidence tampering is detected;
- a truncated final evidence frame preserves explicitly recoverable complete
  frames;
- chunked causal filtering matches one-pass causal filtering and restores exact
  state;
- retrospective filtering cannot validate for causal execution;
- a real external plugin wheel installs, resolves through entry-point metadata,
  validates, constructs, and passes the shared transform contract;
- foundation imports succeed while LSL, MNE, MNE-LSL, PsychoPy, sklearn, Torch,
  Braindecode, MOABB, plotting, pandas, PyRiemann, ONNX, pyglet, and SPECParam
  imports are blocked;
- foundation sources contain no selected recipe names or `PROJECT_ROOT`;
- the direct legacy classifier/model import cycle is absent;
- the complete 285-test legacy and migration suite remains green in the minimal
  Python 3.12 base environment, with five environment-only skips;
- repository CI defines supported base runs for Linux 3.11–3.13 and macOS and
  Windows 3.12, while the package build workflow uses Python 3.12.

## 8. Deliberate deferrals

Phase 2 does not implement:

- the execution engine, scheduling, watermarks, or backpressure policy;
- full `ProtocolSpec`, `SuiteSpec`, or `DeploymentSpec` compilation;
- evidence-bundle lifecycle, privacy policy, historical import, or archival
  format implementations;
- model outcome matching or adaptive execution;
- action authorization providers or hardware drivers;
- stable top-level API or replacement CLI.

Those responsibilities remain in Phases 3–7. No legacy recipe defines these
foundation contracts.
