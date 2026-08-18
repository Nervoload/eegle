# Phase 7 Public and Authoring Boundaries

**Status:** P7-014 local surface audit complete; real EEG acceptance and current-candidate remote closure evidence remain pending
**Date:** 2026-07-29
**Architecture authority:** [EEGLE.md](EEGLE.md)
**Phase design:** [PHASE7_AUTHORING_OPERATIONS.md](PHASE7_AUTHORING_OPERATIONS.md)
**Machine-readable inventory:**
[phase7_public_surface.json](migration/phase7_public_surface.json)

This document resolves D-014 and D-034 and fixes the boundary that P7-002
through P7-008 build on. It identifies what is public, what is explicitly
provisional, how Python and the CLI share services, which versioned
payloads cross the authoring boundary, and how process exit status relates to
structured diagnostics.

## 1. D-014 — Public stability boundary

EEGle uses three visibility classes during the alpha migration.

### Stable-alpha public

The names exported in package-level `__all__` from these packages are the
current low-level public surface:

```text
eegle
eegle.actions
eegle.compiler
eegle.models
eegle.plugins
eegle.processing
eegle.recording
eegle.replay
eegle.runtime
eegle.specs
eegle.streams
```

“Stable-alpha” does not mean a 1.0 compatibility promise. It means:

- package-level exports are deliberate rather than incidental;
- a breaking change requires an explicit migration decision, test change, and
  status entry rather than silent removal;
- versioned payloads retain their schema identity or receive a new version;
- examples and public documentation import package-level names rather than
  implementation submodules where a package export exists.

The top-level `eegle` package remains deliberately minimal. P7-007 adds no
user-journey function there; the console/module entry point delegates to the
provisional `eegle.operations` services recorded in the public inventory and
clean-wheel test.

### Provisional public

The package-level exports of these Phase 7 packages are public for testing and
early use but provisional until P7-014:

```text
eegle.authoring
eegle.integrations
eegle.integrations.lsl
eegle.operations
```

`eegle.authoring` owns non-executable authoring envelopes, provenance
vocabulary, draft/template/composition services, exact compositional presets,
and the optional restricted-YAML adapter. `eegle.integrations` owns
dependency-lazy optional bridges; its LSL subpackage remains simulated-validated
until the retained real acceptance gate passes. `eegle.operations` owns
the application-service results, joined explanation/diff projections,
diagnostics, and process-exit meanings shared by Python and the CLI.
Provisional Python names may be refined during Phase 7,
but the v1 payload schema IDs and assigned exit-code meanings cannot be reused
for incompatible semantics.

The provisional `eegle.operations.RunControl` is an additive local supervisor
boundary for a single locked run. It accepts reasoned `complete()` and
`cancel()` requests before or after internal engine attachment; the first
terminal request wins. `run_project()`, `run_locked_plan()`, and
`rehearse_project()` accept it without exporting the engine or changing the
CLI. `ProjectRun` and session inspection additively project the terminal reason.

### Internal or source-only

The following are not public contracts:

- underscore-prefixed root modules and names;
- package implementation submodules not explicitly listed as a public module;
- the CLI parser, rendering, and terminal-formatting implementation;
- migration-only application, task, worker, hardware, analysis, and legacy
  integration sources excluded from the v1 wheel;
- test fixtures and anything imported from `tests`.

Python permits direct submodule imports, but importability alone does not make a
submodule public. The package-level `__all__` inventory is the authority.

## 2. Shared Python and CLI service boundary

The CLI is an adapter over the same typed services used by Python:

```text
CLI argument parsing / terminal rendering
                    ↓
          eegle.operations services
                    ↓
 authoring / compiler / runtime / recording / replay owners
```

The operations layer may coordinate owning packages, but it must not:

- reinterpret draft, role, outcome, action, or plan semantics;
- construct runtime components directly;
- compile through a private graph path;
- run unresolved authoring values or an unlocked plan;
- turn a diagnostic repair proposal into silent mutation;
- turn process exit status into the only machine-readable result.

P7-001 added contracts only. P7-002 adds one authoring-owned lowering service
and operations-owned source remapping. P7-003 adds exact-version declarative
templates as clients of that same service. P7-004 adds the typed persistent
Python builder and P7-005 adds a restricted YAML adapter; both lower through
the same template/draft service. P7-006 adds read-only explanation, difference,
and guided-diagnostic services over authored/canonical/compiler values. None
of P7-001 through P7-006 adds a console entry point, binds site resources, or
implements execution orchestration. P7-007 installs the console/module adapter
over shared operations, creates one explicit bounded simulation deployment,
and delegates compilation, locked execution, persistence, inspection, and
replay to their existing owners.

P7-011 extends the established stable-alpha `eegle.models` and `eegle.replay`
surfaces with deterministic, hash-verified model packages and contract-guarded
replacement replay. The package is data plus canonical identity, never an
executable import-path or environment envelope. Framework code remains an
independently installed plugin, and replacement execution remains the existing
locked engine. The `model` CLI family delegates directly to those owners.

P7-012B keeps the package-level authoring import stable while separating its
implementation responsibilities. Immutable declarations live in `design`,
persistent editing in `composition`, named lowering in `lowering`, source
mapping in `design_provenance`, composed exports in `composed_projects`, and
the design JSON Schema in `schemas`. These implementation modules are not
separate public APIs.

P7-013 adds exact compositional preset revision `2.0.0` and normalized-design
project input without changing template revision `1.0.0`. Six generated
projects retain their source, canonical outputs, deployment requirements,
simulation deployment, explanation, and project index. The separate example
model distribution publishes mean, peak, and stateful adaptive algorithms
through `eegle.plugins`; model code never enters authoring or a model package.
The dependency-lazy MNE bridge is a one-way analysis export and retains EEGle
timing identities in a sidecar rather than claiming MNE as a replay authority.

P7-013A adds construction-free installed-descriptor inspection and a reusable
conformance harness at the existing plugin boundary. Component construction,
behavioral exercises, and lifecycle I/O remain explicit caller choices. The
harness verifies canonical contract outputs, cleanup on failure, declared
snapshot/restore behavior, and fresh-component replay equivalence. Model
fixtures cover valid, partially invalid, and all-invalid inputs; an all-invalid
window yields a finite explicit abstention rather than an incidental numerical
exception or non-finite score.

P7-013B makes compiled generated project values content-addressed revisions.
A successful compile publishes the complete generated directory with one
same-filesystem rename, then atomically switches `eegle-project.json`; a failed
compile leaves the previously indexed generated set unchanged. Distribution
metadata is the runtime and CLI version authority, release language remains
pre-alpha, and package automation separately proves the wheel, sdist, optional
dependencies, independent plugin, and reference journey outside the checkout.

## 3. Authoring payload contracts

The provisional `eegle.authoring` package defines versioned payload contracts
for draft, provenance, deployment requirements, exact templates, composed
designs, generated exports, and project manifests. JSON Schema 2020-12
validators guard external source/envelope boundaries; immutable typed values
validate definitions, locks, and generated exports.

### `eegle.experiment_design.v1`

P7-012A adds the missing bounded middle layer between template parameters and
direct canonical graph construction. `ExperimentDesign` contains immutable,
stable named declarations for study intent, channel-aware dense signals,
events, processing chains, continuous/event windows, quality gates,
independently packaged models, comparison groups, outcomes, adaptation,
calibration artifacts, policies, structured actions, phases, recording, and
acceptance. It contains no component list, route list, runtime factory,
deployment selector, permission grant, or executable code.

`lower_experiment_design()` validates named references and deterministically
derives the ordinary `ProtocolSpec`, `SuiteSpec`, portable deployment
requirements, and provenance sidecar. `ExperimentDesign.to_draft()` marks the
same design schema and dispatches through `lower_experiment_draft()`, so Python,
restricted YAML, and draft clients share one lowering result. Time-based
continuous windows lower to samples only when the declared input rate gives an
exact positive whole-sample count. Missing or cyclic references fail before
canonical lowering.

`eegle.composed_experiment.v1` and
`eegle.composed_authoring_project.v1` keep the normalized design, canonical
outputs, requirements, and provenance separate. Compilation still delegates
unchanged canonical values to `compile_suite()`. The project writer does not
bind deployment resources or execute. Existing template `1.0.0` revisions are
unchanged; future compositional preset revisions must be added alongside them.

### `eegle.experiment_draft.v1`

The draft envelope contains:

- a stable draft identity and positive revision;
- an explicit bounded top-level vocabulary for study, signals, events,
  processing, windows, models, phases, outcomes, actions, recording,
  acceptance, and deployment requirements;
- explicit unresolved requirements, choices, or detection needs.

The envelope remains valid while incomplete. The P7-002 service gives its
recording profile typed semantics and deterministic canonical generation. A
non-empty `unresolved` collection is valid draft data but lowering rejects it,
so it cannot reach compilation. Other bounded sections remain reserved for
their owning Phase 7 tasks and are rejected by this profile rather than treated
as arbitrary component or route declarations.

P7-003 adds an optional exact `template` selection containing the stable
template ID, version, manifest digest, and explicit finite-JSON parameters. The
draft does not embed a generated graph or an executable factory. Manifest
mismatch, an unknown exact revision, invalid parameters, or unresolved choices
prevent lowering.

### `eegle.authoring_provenance.v1`

The provenance sidecar binds one draft revision/digest to protocol, suite, and
optional deployment canonical targets. Each entry records:

- canonical target artifact and JSON Pointer;
- authoring origin;
- source kind and optional location;
- scientific materiality;
- confirmation state;
- optional template identity and parameter path.

The allowed origins are user explicit, template default, authoring default,
authoring derived, detection proposal, and migration generated. Derived means
the authoring lowerer deterministically calculated the exact canonical leaf; it
does not imply user confirmation. Compiler-derived and plugin defaults are
intentionally absent: they remain compiler/plan provenance.

The sidecar is never embedded in a canonical specification and never
participates in its hash. Its own draft and target digest references bind it to
the values it explains.

The draft and provenance validators first reject non-explicit JSON values
through EEGle's explicit JSON boundary, then apply their schemas. They do not
lower, compile, repair, or execute a draft.

### `eegle.deployment_requirements.v1`

Lowering emits portable requirements rather than guessing a site-local
`DeploymentSpec`. The typed requirements identify source component and stream
bindings with signal contracts and capabilities, required clock mappings,
evidence storage, exact model manifests, and action capabilities requiring
authorization. They contain no selector, endpoint, credential, permission
grant, artifact URI, or authorization decision. P7-008 will turn these values
and separate discovery evidence into a reviewable deployment proposal.

### `eegle.template_expansion_lock.v1`

Each expansion lock binds the exact template ID/version and manifest digest,
explicit and effective parameter digests, draft digest, and generated protocol,
suite, and deployment-requirement digests. The expansion digest is the stable
identity of the exact template revision, parameter values, and canonical
outputs; full lock serialization also retains the manifest and draft binding.

The built-in catalog has no implicit-latest operation and refuses changed
content at an existing ID/version. Its eight profiles cover continuous
recording, EEG plus events, continuous observation, event-locked observation,
primary/shadow comparison, calibration followed by locked validation, delayed
outcome adaptation, and simulated closed-loop action. Each definition declares
`explicit_reexpansion`: adding a new revision cannot rewrite or reinterpret a
locked old selection.

Template profiles are bounded internal lowering implementations, not plugin
factories or runtime objects. They produce ordinary canonical specs and
portable needs, which still undergo normal deployment review and compiler
validation. Custom computation remains an executable plugin; independent
template-package discovery is intentionally not a P7-003 gate.

### P7-004/P7-005 source and export payloads

`eegle.template_authoring.v1` remains the accepted exact-template YAML shape:
schema ID, draft ID, optional revision, and one exact template selection with
finite JSON parameters. P7-012A additionally admits the normalized
`eegle.experiment_design.v1` shape through a separate typed entry point. Both
use the same restricted parser and neither defines executable YAML behavior.

`eegle.authored_experiment.v1` is the deterministic canonical export envelope
for that source plus the exact template definition, expansion lock, canonical
protocol/suite, deployment requirements, and authoring provenance.
`eegle.authoring_project.v1` names the separately written project files and
their digests. Neither payload is executable, contains a deployment binding, or
replaces any canonical/compiler/runtime authority.

`ExperimentBuilder` is persistent: each typed edit returns a new value. Its
eight convenience constructors select exact built-in template revisions; its
`build()` operation creates an `AuthoredExperiment` through normal template
expansion and draft lowering. Canonical export and project writing preserve the
individual artifacts. The compile convenience delegates the unchanged
protocol/suite to `compile_suite()` and offers no execution convenience.

### P7-002–P7-006 lowering, template, source-map, and explanation boundary

`lower_experiment_draft()` owns the bounded recording profile and dispatches an
exact manifest-locked built-in template selection to its corresponding bounded
internal profile. It sorts identity-bearing values before canonical
construction, produces normal `ProtocolSpec` and `SuiteSpec` objects, and
returns them with deployment requirements and a typed `AuthoringProvenance`
sidecar. Provenance does not appear in any canonical payload or hash.

The source sidecar records exact and parent JSON Pointers. Diagnostic mapping
converts existing `$`-rooted protocol/suite/compiler paths to canonical JSON
Pointers and attaches the closest authoring location while preserving the
original diagnostic code, severity, message, and path. Compiler-derived values
are still not authoring origins.

P7-006 defines two additional versioned operation payloads.
`eegle.experiment_explanation.v1` provides complete scientific-intent,
dataflow, causality, model-comparison, action-influence, and
defaults/provenance views. It may embed the existing safe
`eegle.plan_explanation.v1` projection only when the locked plan's protocol and
suite hashes match the authored canonical values. Compiler-derived projection
fields have no authoring source or origin.

`eegle.experiment_diff.v1` classifies differences with one or more scientific,
operational, presentational, and replay-affecting impacts. Canonical and locked
plan comparison delegates to canonical hashes and `diff_plans()`; provenance
comparison remains presentational and outside the specification hashes. Both
payloads are read-only projections and are never runtime inputs.

## 4. Public diagnostic and exit contract

`eegle.operations` defines `eegle.operation_error.v1`. An operation failure
contains a stable operation identity, one exit code, and one or more structured
diagnostics. Each diagnostic contains a category, title, explanation, severity,
optional canonical path and authoring source, scientific impact, optional
likely cause, repair proposals, documentation reference, and structured
details.

Repair values include a description and finite proposal payload. P7-006 emits
review-only proposals with `applied: false`; `automatic_proposal` means EEGle
can construct a candidate change for inspection, never that a repair is applied
without acceptance.

| Exit | Name | Meaning |
|---:|---|---|
| 0 | `SUCCESS` | The requested operation completed under its declared contract. |
| 2 | `USAGE_ERROR` | Command syntax or invocation is invalid; no domain operation ran. |
| 3 | `INVALID_INPUT` | Authoring, schema, or artifact input is malformed or incomplete. |
| 4 | `REJECTED` | Valid input was rejected by compilation, preflight, policy, or another declared gate. |
| 5 | `UNAVAILABLE` | A selected optional integration, resource, or capability is unavailable. |
| 6 | `EXECUTION_FAILED` | A run or operation began and failed under recorded execution semantics. |
| 7 | `INTEGRITY_FAILED` | Hash, evidence, artifact, or security verification failed. |
| 8 | `INSUFFICIENT_EVIDENCE` | The requested conclusion cannot be supported by available evidence. |
| 70 | `INTERNAL_ERROR` | EEGle encountered an unexpected implementation failure. |

Warnings do not change a successful exit automatically. A service selects the
exit code from its typed terminal status, not from terminal formatting or the
number of diagnostics. Python callers receive structured values or an
`OperationError`; the CLI renders the same payload and returns its code.

### P7-007 project and command payloads

P7-007 adds `eegle.operation_result.v1`, `eegle.project.v1`,
`eegle.project_graph.v1`, `eegle.project_result.v1`,
`eegle.session_inspection.v1`, and
`eegle.replay_inspection.v1`. The success envelope fixes `ok: true` and exit
code zero; failures continue to use `eegle.operation_error.v1` and its stable
nonzero meanings.

The project manifest is a path-safe index, not a compiler or runtime input.
Its mutable authoring/deployment entries can change before recompilation.
Execution plans, locks, and locked explanations use content-addressed paths and
are verified as immutable. The shared plan-only run service verifies the lock
before creating a session and never reads a draft, template, YAML document, or
mutable project manifest.

### P7-012 session operation payloads

P7-012 adds `eegle.model_replacement_comparison.v1` and
`eegle.session_export.v1` while extending `eegle.session_inspection.v1` and
`eegle.replay_inspection.v1` additively. Read-only session operations return
typed `complete`, `partial`, `diverged`, or `unavailable` outcomes rather than
turning evidence defects into an unhandled process exit. CLI `--strict` is the
explicit opt-in for automation that needs a nonzero attention signal.

This is not lifecycle authority: the services cannot signal workers, resume or
finalize writers, recover or truncate ledgers, or overwrite/delete source
evidence. The P7 safe export wrapper narrows the Phase 4 portable policy to
`public` artifacts and excludes semantic logs, captures, execution plans,
component state, interrupted ledgers, raw recordings, deployment bindings,
identity, and external URIs.

### P7-008 detection and deployment proposal payloads

P7-008 adds `eegle.detection_report.v1` and
`eegle.deployment_proposal.v1` to the provisional `eegle.operations` surface.
The report is canonical, deterministically ordered, hash-verified detection
evidence. It records exact installed plugin descriptors, explicitly supplied
or installed-entry-point portable model manifests, and typed site observations
supplied by independent integration detectors. The base detector imports no
optional transport.

The proposal is a non-executable sidecar over one ordinary `DeploymentSpec`.
It binds the unchanged suite and requirements hashes to a detection-report hash
and records each automatic or explicit matching decision. The sidecar is not a
compiler input. Projects index the report, proposed spec, and proposal
provenance separately and never replace `simulation_deployment`; callers or
`compile --deployment deployment_proposal` must select the proposal explicitly.

Compatibility uses the compiler's existing signal-contract predicate.
Ambiguous matches return `REJECTED` with exact candidate identities; missing
matches return `UNAVAILABLE`. Credential-shaped detector configuration is
rejected and component bindings contain only `SecretReference` identities.
Provider observations never imply authorization. Only exact, separately
reviewed provider and permission records can satisfy an action requirement,
and extraneous authorization is rejected.

### P7-009 preflight and rehearsal payloads

`eegle.preflight_report.v1` binds one exact plan, lock, deployment, and optional
detection report to deterministically ordered checks. Checks cover lock and
descriptor integrity, artifacts, streams, channels, units, measured nominal
rate, clocks, storage, endpoints, model materialization, secret/provider
availability, operator gates, and actuator safe-state reporting only when the
locked capabilities require them. A missing authorization provider is recorded
as observe-only and never repaired into a grant.

`eegle.rehearsal_report.v1` binds portable protocol/suite hashes to a distinct
simulation plan/lock, preflight report, session, bundle, and eight initial fault
disposition probes. The outcomes are also appended as ordinary semantic records
before the normal bundle is finalized. The contract rejects a physical-action
claim and rejects reuse of the simulation plan hash as a named live-plan hash.
`rehearse_locked_plan()` lets reference fixtures supply independently installed
plugins while preserving the same compiler, engine, and evidence path used by
project rehearsal.

### P7-010 provisional LSL boundary

`eegle.integrations.lsl` is provisional public through P7-014 and ships as a
dependency-lazy subpackage selected explicitly in the wheel. Its six plugin
descriptors cover dense, sparse-marker, and metadata sources and outlets. The
module provides typed discovery, exact selectors, synchronized timestamps,
bounded exact-selector reconnect, and regular-stream sequence-gap loss
observations. It imports `pylsl` only on detector/source/outlet construction.

The support record is deliberately three-valued: `unavailable`,
`simulated_validated`, or `live_observe_only_validated`. The last state requires
a named real acceptance identity. No such identity exists yet, so P7-010
remains in progress even though its automated implementation and simulated
acceptance are present.

The installed-wheel native-network probe additionally verifies that discovery
retrieves full `pylsl` channel descriptors rather than accepting resolver-short
fallback labels, and that dense packets traverse the local network adapter.
This strengthens simulated validation without changing the live-support claim.

## 5. D-034 — Restricted YAML distribution

Restricted YAML is selected as an optional authoring surface, not a base
dependency. P7-005 introduces:

```text
eegle[yaml]
    └── ruamel.yaml >=0.18.10,<0.19
```

The base install retains JSON authoring envelopes, canonical JSON
specifications, templates, and the typed Python builder. YAML imports remain
lazy and isolated to the YAML adapter.

The selection is based on the maintained parser's YAML 1.2 support, pure-Python
safe loader mode, and default duplicate-key rejection. Its documentation also
recommends pinning the tested API series. See the
[ruamel.yaml overview](https://yaml.dev/doc/ruamel.yaml/overview/),
[safe-loader guidance](https://yaml.dev/doc/ruamel.yaml/basicuse/),
[duplicate-key policy](https://yaml.dev/doc/ruamel.yaml/api/), and
[current package metadata](https://pypi.org/project/ruamel.yaml/).

The originally recorded `>=0.19.1,<0.20` range was amended before release
because that version did not exist in the package index. The implementation
pins the tested 0.18 API series and wraps it with EEGle restrictions:

- YAML 1.2 semantics and the pure-Python safe path;
- one document only;
- no aliases, anchors, merge keys, explicit/custom tags, or object
  constructors;
- duplicate-key rejection;
- no implicit date/datetime values;
- bounded input size, nesting, node count, and scalar size;
- conversion to explicit finite JSON followed by the normal draft schema;
- line/column source mapping and actionable diagnostics.

The adapter is absent from base imports, and importing `eegle.authoring` does
not attempt to import `ruamel.yaml`.

## 6. Boundary verification

`tests.test_phase7_public_boundaries` verifies:

- the machine inventory matches package-level `__all__` authorities;
- authoring and operations packages are included in the target wheel package
  selection and import no optional or legacy authorities;
- draft and provenance envelopes accept explicit examples and reject authority
  leakage or malformed payloads;
- the provenance sidecar remains separate from the draft and canonical values;
- operation errors round-trip through their versioned schema;
- exit-code meanings remain numerically stable; and
- the YAML parser remains absent from base dependencies and imports.

`tests.test_phase7_draft_lowering` additionally verifies deterministic
recording lowering, typed requirement and sidecar round trips, explicit versus
default origins, source-independent canonical hashes, canonical diagnostic
mapping, normal compilation after deployment binding, unresolved rejection,
and the runtime plan-only boundary.

`tests.test_phase7_templates` verifies the eight catalog definitions and their
machine-readable synthetic fixtures, deterministic declared expansion digests,
exact selection and manifest tamper rejection, parameter-to-canonical
provenance, normal compilation of every generated suite, and preservation of a
locked old expansion after an explicit new template revision is added.

`tests.test_phase7_authoring_surfaces` verifies persistent typed construction,
canonical/project export, compiler/runtime boundaries, shared Python/YAML
golden hashes, precise YAML source locations, unsafe-subset rejection, and each
resource bound. Packaging and base-import tests separately verify that the YAML
parser remains optional and isolated.

`tests.test_phase7_explanations` verifies all six views for all eight reference
templates, matching-plan enforcement, compiler-versus-authoring provenance,
observe-only action explanation, four-impact differences, guided common
failures, operation-envelope validation, and non-mutating repair proposals.
