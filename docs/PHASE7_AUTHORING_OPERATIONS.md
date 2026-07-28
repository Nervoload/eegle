# Phase 7 Experiment Authoring and Operations

**Status:** Active implementation design
**Date:** 2026-07-28
**Architecture authority:** [EEGLE.md](EEGLE.md)
**Phase gate:** [MIGRATION.md](MIGRATION.md#12-phase-7--experiment-authoring-operations-packaging-and-integrations)

Phase 7 turns the completed compiler, engine, model-system, evidence, and replay
foundations into a usable research product. It adds convenience above the
canonical specifications and operations around the locked runtime. It does not
introduce a second semantic graph, compiler, model runtime, or execution path.

## 1. Review decision

The revised handoff aligns better with EEGle's accepted goals than the earlier
CLI-and-packaging-only plan. The missing product boundary is the path from a
researcher's scientific intent to `ProtocolSpec`, `SuiteSpec`, and
`DeploymentSpec`. A CLI alone would otherwise expose the internal compiler
vocabulary without making common experiments simple.

The following handoff proposals are accepted into Phase 7:

- progressive authoring through templates, typed Python, and restricted YAML;
- explicit defaults, source mapping, and actionable compiler explanations;
- project scaffolding, discovery, deployment generation, preflight, and
  rehearsal;
- a first-party optional LSL path and optional model-framework integrations;
- public session, evidence, replay, and comparison workflows;
- complete reference projects, clean-wheel verification, current
  documentation, and gated legacy removal.

Four corrections are required to preserve the architecture:

1. Authoring provenance is a sidecar keyed by canonical JSON paths. It must not
   make semantically identical Python and YAML inputs hash differently.
2. Rehearsal and live operation share portable scientific intent and the same
   compiler, engine, and evidence semantics. Different deployment bindings
   normally produce different locked plans and must retain those identities.
3. Restricted YAML is not executable and cannot silently add a parser to the
   base dependency set. Its parser and distribution boundary are an explicit
   Phase 7 decision.
4. Phase 7 exposes definition, preflight, integrity, replay, comparison, and
   already-structured validation. Scientific evaluation depth, benchmarks,
   fault qualification, reporting breadth, support claims, and public-alpha
   hardening remain Phase 8.

## 2. Authority boundary

The Phase 7 flow is:

```text
Templates / typed Python / restricted YAML / project CLI
                         ↓
        ExperimentDraft or named ExperimentDesign
                         +
          authoring provenance/source-map sidecar
                         ↓
       ProtocolSpec + SuiteSpec + Deployment requirements
                         ↓
       explicit discovery and deployment binding proposal
                         ↓
       ProtocolSpec + SuiteSpec + DeploymentSpec
                         ↓
                existing Compiler
                         ↓
             ExecutionPlan + lock
                         ↓
              existing ExecutionEngine
                         ↓
                  EvidenceBundle
```

| Authority | Owns | Must not own |
|---|---|---|
| `ExperimentDraft` | Incomplete researcher intent in the bounded EEGle domain vocabulary | Executable components, graph routes as a user programming language, runtime behavior, authorization |
| `ExperimentDesign` | Complete immutable named composition of signals, processing, windows, models, phases, policies, and other bounded scientific concepts | Runtime objects, arbitrary code/graph nodes, deployment bindings, authorization, compiler semantics |
| Authoring provenance sidecar | Source location, template/default origin, confirmation state, and mapping to generated canonical paths | Scientific values, canonical hashes, compiler defaults, runtime evidence |
| Template | Versioned parameters, bounded scientific defaults, requirements, and deterministic expansion | Runtime construction, plugin installation, silent upgrade of a locked project |
| Python/YAML/CLI surface | Creation and editing of a draft through one lowering service | Independent role, outcome, action, or execution semantics |
| Canonical specifications | Portable scientific intent and explicit site binding | Authoring syntax or UI state |
| Compiler and plan | Validation, exact resolution, material defaults, typed graph, and immutable execution authority | Reading drafts or repairing scientific intent silently |
| Execution engine | Execution of a validated locked plan | Interpreting templates, YAML, project files, or unresolved deployment discovery |

`ExperimentDraft` and its provenance sidecar are versioned authoring artifacts,
not additions to the five durable runtime artifacts in `EEGLE.md`. The runtime
never consumes them. Advanced users may continue to author canonical
specifications directly.

## 3. Authoring and provenance rules

- The authoring vocabulary is bounded to studies, signals, events, processing,
  windows, models, phases, outcomes, actions, recording, acceptance criteria,
  and deployment requirements.
- Custom computation remains an installed plugin. No authoring source embeds
  arbitrary Python, imports a dotted path, constructs an object, or defines a
  general DAG language.
- Lowering is deterministic. Equivalent authoring values produce byte-identical
  canonical specifications regardless of authoring surface.
- The provenance sidecar may differ between surfaces. It maps generated
  canonical JSON paths to source locations and one of: user explicit, template
  default, authoring default, detection proposal, or migration-generated.
- Plugin defaults and compiler-derived values remain compiler/plan provenance.
  Explanation may join the two provenance layers, but it must not misattribute
  one to the other.
- Scientifically material inferred values require explicit confirmation before
  a final lock. Automatic repairs are proposals that state whether they change
  scientific semantics.
- Discovery output is evidence and a proposal. Ambiguous resources fail clearly,
  and device presence never implies an authorization grant.
- Template upgrades never rewrite a locked project silently. A re-expansion is
  an explicit operation with a material plan/specification diff.

## 4. Required user journeys

Phase 7 acceptance is organized around complete journeys rather than isolated
commands.

| Journey | Required outcome |
|---|---|
| First simulation | Create a project, compile, explain, rehearse, run, inspect, and replay without hardware or optional dependencies. |
| Recording-only EEG | Bind simulated or live EEG plus events, record, and inspect evidence without a model, policy, or actuator. |
| Event-locked observer | Run causal preprocessing, quality, event windows, and a model with runtime-owned prediction evidence. |
| Model comparison | Run primary, shadow, and candidate roles on comparable admitted inputs and inspect complete or incomplete comparison evidence. |
| Adaptive model | Deliver a delayed outcome, record eligibility and state transition, checkpoint, and reproduce the declared state under replay. |
| Closed-loop simulation | Produce a policy request, simulation-only authorization, command, and receipt; missing authority remains observe-only. |
| Live LSL acceptance | Discover EEG and marker streams, create a deployment proposal, preflight, run observe-only, and persist evidence without changing the portable suite. |

## 5. Work plan

### P7-000 — Recharter Phase 7

**Status:** done

- Expand Phase 7 from CLI/packaging to authoring, operations, packaging, and
  integrations.
- Lock the core/authoring/operations/integrations layering and the Phase 8
  boundary.
- Record the reviewed task order and amended exit gate.

Acceptance: this document, the revised migration plan, decision D-033, and the
active Phase 7 status agree.

### P7-001 — Public and authoring service boundary

**Status:** done

- Resolve D-014: identify stable-alpha, provisional, and internal modules.
- Select the public service boundary shared by CLI and Python without making a
  facade a second runtime.
- Resolve the restricted-YAML parser and distribution decision before changing
  dependencies.
- Define versioned payload schemas and error contracts for the draft and
  provenance sidecar.

Acceptance: a dependency and import-boundary test, public-surface inventory,
and decision records exist before a broad CLI is implemented.

Implemented boundary: [PHASE7_PUBLIC_BOUNDARIES.md](PHASE7_PUBLIC_BOUNDARIES.md)
resolves D-014/D-034. Package-level exports of established target packages are
stable-alpha; `eegle.authoring` and `eegle.operations` are provisional public
packages through P7-014; nested implementation modules and retained legacy
sources are internal. P7-001 adds only immutable source vocabulary, the draft
and provenance envelope schemas, and the shared operation diagnostic/exit
contract. No draft lowering, YAML parser, CLI entry point, or execution facade
is present.

### P7-002 — Experiment draft and authoring provenance

**Status:** done

- Implement incomplete but bounded `ExperimentDraft` intent.
- Implement deterministic lowering to canonical protocol/suite values plus
  explicit deployment requirements.
- Emit a non-hashing provenance/source-map sidecar.
- Map canonical-validation and compiler diagnostics back to authoring sources.

Acceptance: two equivalent drafts lower identically; explicit and defaulted
values remain distinguishable; unresolved choices cannot compile; the runtime
cannot consume a draft.

Implemented boundary: `ExperimentDraft` now preserves incomplete intent and
explicit unresolved choices as a versioned, non-executable value. The first
deterministic lowering profile is deliberately recording-only: it accepts
bounded study, dense/sparse signal, optional single-phase, recording, and
acceptance intent; derives canonical streams, source/sink components, routes,
and a phase; and emits `eegle.deployment_requirements.v1` source, clock, and
evidence-storage needs. It rejects model, action, adaptation, arbitrary graph,
and site-binding input instead of anticipating later tasks.

The typed `eegle.authoring_provenance.v1` sidecar binds the draft digest and
canonical target hashes while remaining outside both specifications. Explicit
and authoring-default origins remain distinct, source lookup uses the closest
JSON Pointer, and operations can join canonical schema/compiler paths back to
those locations without changing compiler codes. A resolved simulation
deployment proves that the generated values enter the existing compiler
unchanged; runtime construction now rejects every non-`ExecutionPlan` value
before inspection. P7-003 extends this same service with exact template
selection; it does not add a second lowering or execution authority.

### P7-003 — Versioned templates

**Status:** done

Initial templates cover continuous recording, EEG plus events, continuous
observer, event-locked model, primary/shadow comparison, calibration then locked
validation, delayed-outcome adaptation, and simulated closed-loop action.

Each template declares a stable ID/version, parameter schema, generated schema
versions, required capabilities, scientific defaults, deployment requirements,
synthetic fixture, expansion digest, and explicit migration policy.

Acceptance: expansion is deterministic, generated specs compile normally,
locked projects never change through template upgrade, and templates remain
non-executable clients of the authoring service. Independent template-package
discovery is not a gate; the existing external executable-plugin proof remains
the required extension boundary.

Implemented boundary: `eegle.authoring` now exposes an immutable exact-version
catalog with no implicit-latest lookup. A draft locks the template ID, version,
manifest digest, and explicit finite-JSON parameters. Each built-in definition
declares its parameter schema and targets, generated schema versions, required
capabilities, scientific defaults, deployment-requirement kinds, synthetic
fixture and expected expansion digest, and `explicit_reexpansion` migration
policy.

The eight bounded profiles generate ordinary `ProtocolSpec` and `SuiteSpec`
values through `lower_experiment_draft()`, with model artifact and action
authorization needs remaining portable deployment requirements. A versioned
expansion lock separately binds the exact template manifest/draft and the
explicit/effective parameter plus generated-output digests. Its expansion
identity covers the effective authoring inputs and canonical outputs without a
self-referential manifest hash. Template provenance identifies explicit
parameters versus exact template defaults at canonical leaf paths.

The machine fixture inventory expands all eight profiles twice, checks their
declared digests and requirement kinds, and sends every generated protocol/suite
through the unchanged compiler with a reviewed synthetic deployment. A new
template revision must be added alongside the old one; same-version content
replacement is rejected, and exact old-version expansion remains unchanged.

### P7-004 — Typed high-level Python API

**Status:** done

- Provide typed, autocomplete-friendly persistent builders with no required
  subclassing.
- Expose draft, canonical export, project writing, defaults, requirements, and
  compile facades through the shared authoring/compiler services.
- Preserve direct access to lower-level canonical types.

Acceptance: recording-only intent fits in roughly 20 meaningful lines and
event-locked comparison in roughly 40; both match canonical golden fixtures;
the facade cannot bypass compiler failure or run an unlocked plan.

Implemented boundary: `ExperimentBuilder` is an immutable, persistent client
of the exact-version template catalog and the single draft lowerer. Eight typed
constructors mirror the built-in profiles; focused `signal`, `window`,
`events`, `model`, and `action` methods return new builders without hiding the
effective defaults, portable requirements, draft, or source map. `build()`
returns an `AuthoredExperiment` whose protocol and suite are ordinary canonical
objects. Its `compile()` method delegates those unchanged objects to the public
compiler, so compiler rejection remains authoritative, and it exposes no run
facade.

Canonical export and project writing are explicit and deterministic. A project
contains separate authoring source, template definition, expansion lock,
canonical protocol/suite, deployment requirements, provenance, and project
manifest files; existing targets are not overwritten unless the caller opts
in. The concise continuous-recording and primary/shadow comparison examples are
locked to shared golden hashes.

### P7-005 — Restricted YAML authoring

**Status:** done

Accept a deliberately small JSON-compatible YAML 1.2 subset: no executable
tags, object constructors, aliases, merge keys, duplicate keys, implicit dates,
or untyped units. Canonical JSON export is always available.

Acceptance: equivalent YAML and Python sources yield identical canonical
specification hashes; errors retain line/column locations; unsafe constructs
fail; the selected parser does not create undeclared base-import leakage.

Implemented boundary: the optional `eegle[yaml]` extra installs
`ruamel.yaml>=0.18.10,<0.19`; the dependency is imported lazily only inside
`eegle.authoring.yaml`. The adapter uses the pure safe YAML 1.2 path, inspects
scanner/composer tokens before construction, accepts exactly one finite
JSON-compatible document, and enforces bounded bytes, depth, node count, and
scalar characters. It rejects aliases, anchors, merge keys, tags, directives,
duplicate keys, implicit dates, non-string keys, non-finite values, and
multi-document input before the normal authoring schemas and builder semantics
run.

The YAML document is the concise `eegle.template_authoring.v1` payload. Node
marks become JSON-Pointer source locations, so parameter/schema failures retain
the YAML locator and exact line/column. YAML and typed Python fixtures share the
same canonical source representation and produce byte-identical protocol,
suite, requirement, and expansion identities. Import-boundary tests preserve a
parser-free base install.

### P7-006 — Explanation and guided diagnostics

**Status:** done

Add structured explanation views for scientific intent, dataflow, causality,
model comparison, action influence, defaults/provenance, and
scientific/operational/presentational/replay-affecting differences.

Diagnostics add a concise title, scientific impact, likely cause, possible
repairs, source location, documentation reference, and whether a repair changes
scientific semantics. Repairs are never applied without explicit acceptance.

Acceptance: every reference journey produces a readable contract and common
unit, rate, channel, clock, plugin, model, and authorization failures map to the
authoring source with actionable structured data.

Implemented boundary: `eegle.experiment_explanation.v1` joins one authored
experiment to an optional matching locked-plan projection without changing
either authority. Six required views cover scientific intent, dataflow,
causality/time, model comparison, action influence, and defaults/provenance.
Every built-in reference template produces all six views before deployment; a
matching plan adds the existing `explain_plan()` projection. Plan hashes must
match the authored protocol/suite, and compiler-derived fields are explicitly
source-free rather than relabeled as authoring defaults.

`eegle.experiment_diff.v1` compares authored and optional paired plan values and
labels scientific, operational, presentational, and replay-affecting impacts.
Equivalent Python/YAML origins can therefore differ presentationally without a
scientific or replay claim, while canonical or plan changes retain their
stronger impacts through the existing `diff_plans()` authority.

Guided authoring and compiler failures use the existing
`eegle.operation_error.v1` envelope. Its additive `likely_cause`, repair
description, and finite proposal fields preserve original codes, severity,
messages, and paths. Unit, rate, channel, clock, plugin, model, and
authorization families receive source-aware titles, impacts, documentation,
and proposals with `applied: false`; no service accepts or applies a repair.

### P7-007 — Project scaffolding and public CLI

**Status:** done

Implement the command families listed in the Phase 7 migration plan in thin
slices over shared Python services. Start with `new`, `compile`, `explain`,
`rehearse`, `run`, `inspect`, and `replay`; add discovery, preflight,
comparison, validation, export, model, and plugin operations with their owning
tasks.

A project keeps authoring sources separate from generated canonical specs,
detection reports, deployment bindings, immutable locks, and sessions. `run`
accepts a verified lock, not a mutable project draft. Commands provide stable
exit codes and machine-readable output.

Acceptance: the clean base wheel completes the first-simulation journey; CLI
and Python use the same services; no study-specific command or repository-root
assumption is present.

Implemented boundary: `eegle.project.v1` is a mutable, non-executable index
over explicitly separated authoring source, generated canonical values,
deployment binding, explanation, immutable compiled artifacts, and session
URIs. Mutable source and deployment digests are informative; content-addressed
plans, locks, and their joined explanations are immutable and verified on
open. Recompilation writes a new content-addressed path rather than replacing a
different lock.

The provisional `eegle.operations` package now supplies the same typed
`create_project()`, `compile_project()`, `explain_project()`,
`rehearse_project()`, `run_project()`, `run_locked_plan()`,
`inspect_session()`, and `replay_session()` services used by the CLI. The
bounded base scaffold generates only the reviewed continuous-recording packet
simulation; P7-008 adds general discovery/deployment proposals without
replacing it, while broader fault rehearsal remains P7-009.
`run_locked_plan()` reads and verifies only an execution plan and lock before
session creation, while `run_project()` merely resolves those immutable URIs.
A changed mutable authoring source therefore cannot affect an already compiled
run.

The installed `eegle`/`python -m eegle` adapter exposes `new`, `detect`,
`compile`, `explain`, `diff`, `graph`, `rehearse`, `run`, `inspect`, and
`replay`, returns
the stable exit meanings, and emits `eegle.operation_result.v1` or
`eegle.operation_error.v1` with `--json`. The first-simulation path uses the
normal compiler, runtime, evidence persistence, integrity reader, and
bundle-driven replay; it imports no legacy recipe and assumes no repository
root.

### P7-008 — Discovery and deployment generation

**Status:** done

Discover installed plugins and models plus selected stream, channel, rate,
unit, clock, storage, placement, and authorization-provider capabilities.
Generate a reviewable `DeploymentSpec` proposal with provenance. Ambiguity
requires explicit selection; secrets are references; authorization is never
inferred; deployment generation cannot alter `SuiteSpec`.

Acceptance: the same portable protocol/suite compiles against simulation and
live-capability deployments; detection is inspectable and independently
runnable; ambiguous matches fail precisely.

Implemented in `eegle.operations` as two deliberately separate services.
`detect_capabilities()` inventories canonical plugin descriptors from the base
registry and installed `eegle.plugins` entry points, reads installed
`eegle.models` manifest entry points plus explicitly named model manifests, and
combines typed integration observations for sources,
streams/channels/rates/units, storage, clock mappings, placements, endpoints,
secret needs, and authorization-provider capabilities. The resulting
`eegle.detection_report.v1` is deterministically ordered, records detection
provenance, binds exact descriptor/manifest identities, includes an explicit
UTC observation time, verifies its canonical hash on read, and can be produced
by `eegle detect` without a project or an optional transport import.

`propose_deployment()` matches those observations against the existing typed
`DeploymentRequirements` and the compiler's own `SignalContract`
compatibility predicate. Zero candidates is an unavailable result. More than
one candidate is a rejected ambiguous result naming every exact candidate;
only an explicit `DeploymentSelection` resolves it. The output is an
`eegle.deployment_proposal.v1` sidecar containing the ordinary
`DeploymentSpec`, source observations, exact decisions, selection provenance,
and the unchanged suite/requirements/report hashes. It is not compiler input.

Project proposals are content-addressed immutable artifacts under
`detections/` and `deployments/proposals/`. They do not overwrite
`deployments/simulation.json`; `eegle compile --deployment
deployment_proposal` is the explicit act that selects one for compilation.
Proposal compilation rechecks the content-addressed
detection/proposal/spec join and then delegates the unchanged canonical
protocol, suite, and selected `DeploymentSpec` to the normal compiler. Model
manifests remain portable and
path-free; site materializations still require explicit artifact bindings.

Detector configuration rejects credential-shaped literal fields. Components
receive only `SecretReference` identities selected outside the observation.
Authorization-provider observations are availability evidence only: proposal
generation adds no provider or permission. An action requirement can be
resolved only by separately supplied, exact `AuthorizationProviderBindingSpec`
and `PermissionGrant` records that match a detected capability. Extraneous
authorization on a suite with no action requirement is rejected.

Seven dedicated tests cover canonical/hash-verified report round trips,
independent command detection, the same portable suite compiling against
simulation and a detected site-capability deployment, distinct plan identity,
exact ambiguity and incompatibility failures, secret-reference-only binding,
provider presence without inferred authority, immutable project artifacts,
and explicit proposal compilation. Ninety focused foundation/compiler/Phase 7
and source-boundary tests pass. The 327-test aggregate has 321 passes, five
environment-dependent skips, and only the known Python 3.14 build-backend
setup failure. A clean 127-file wheel built independently, includes the new
discovery service and console entry, excludes historical `eegle.cli`, and runs
standalone/project `detect`, `new`, and `compile` outside the repository.

### P7-009 — Preflight and rehearsal

**Status:** done

Preflight verifies locked plugins/artifacts, streams, channels, units, measured
rates, clocks, storage, endpoints, model compatibility, authorization-provider
availability, safe-state reporting, and required operator gates according to
declared capabilities.

Rehearsal compiles the unchanged portable intent against a simulation
deployment and executes that lock through the normal engine and evidence path.
Initial fault scenarios cover missing markers, late packets, clock drift, low
quality, model latency, queue overflow, authorization denial, and simulated
actuator rejection. Comprehensive fault qualification remains Phase 8.

Acceptance: every closed-loop reference project rehearses without hardware;
fault outcomes are evidence; absence of a provider remains observe-only;
rehearsal cannot authorize a physical action or impersonate the live plan
identity.

Implemented boundary: `eegle.preflight_report.v1` is a canonical, hash-verified
decision over the exact plan, lock, compiled deployment, installed descriptor
hashes, and optional detection report. Its capability-scoped checks cover
locked artifacts/plugins, source presence, exact channels and units, measured
nominal-rate tolerance, clocks and uncertainty, storage, proxy endpoints,
secret providers, model artifacts, authorization-provider availability,
operator gates, and physical-actuator safe-state reporting. Non-applicable
capabilities remain explicit; absence of action authority is a passing
observe-only fact and never a permission.

Project preflight persists content-addressed evidence and the public
`preflight` CLI fails with stable `REJECTED` semantics when any required check
fails. Project rehearsal resolves the simulation deployment from the plan hash,
rejects any non-simulator resource or non-simulation provider/actuator, runs
preflight, and then uses the normal locked engine and evidence writer. All eight
initial fault-disposition probes are semantic records inside that bundle and
are also
bound by `eegle.rehearsal_report.v1` to portable spec hashes, the distinct
simulation plan/lock, preflight, session, and bundle. The report contract cannot
claim physical authorization or reuse its simulation hash as a live identity.

`rehearse_locked_plan()` runs independently packaged reference plugins through
the same boundary. Automated acceptance executes the simulated closed-loop
template, including its model, policy, simulation authorization, actuator, and
receipts, with authorization-denial and actuator-rejection containment evidence.
Three dedicated P7-009 tests plus the existing project journey pass.

### P7-010 — First-party LSL integration

**Status:** in progress — implementation and simulated acceptance complete; real EEG acceptance pending

Provide optional dense-source, sparse-marker, metadata, outlet, discovery,
exact-selector, clock/synchronization, reconnect, and packet-loss behavior with
simulated LSL fixtures.

Acceptance: simulated LSL passes in automation; one documented real EEG
observe-only run passes; the portable suite is unchanged across simulation and
live deployments; base imports succeed with LSL blocked; support level is
reported truthfully.

Implemented boundary: the `live` extra selects `pylsl>=1.18,<2`, while
`eegle.integrations.lsl` and its six `eegle.plugins` entry-point descriptors
remain safe to import without `pylsl`. Dense, sparse-marker, and metadata
sources emit the general packet records; corresponding outlets consume those
records. Discovery captures exact LSL identities, XML channel labels/units,
rate/format, proposal-ready resource contracts, and an online clock capability.
Selectors require UID, source ID, or an exact name/type pair and reject zero or
multiple matches. Inlets use explicit clock-sync flags, optional
dejitter/monotonization, exact-selector bounded reconnect, and regular-stream
sequence gaps plus typed packet-loss estimates.

Four automated tests cover simulated discovery and ambiguity, dense/sparse/
metadata conversion, clock correction, reconnect, packet loss, outlets,
dependency-blocked imports, descriptor registration, and the same portable
suite compiling against simulation and LSL deployments with distinct plan
identity. The live proposal also passes exact preflight, while a changed stream
selector fails closed. Support remains truthfully `simulated_validated`. The
required real EEG observe-only gate cannot be performed in the current
environment because
`pylsl` and an EEG outlet are unavailable; [LSL_INTEGRATION.md](LSL_INTEGRATION.md)
records the exact non-participant acceptance procedure and leaves the support
claim open rather than fabricating it.

The combined repository verification passes all 334 tests under Python 3.12,
with ten explicitly dependency- or environment-conditioned skips. The clean
wheel test independently exercises dependency-absent LSL detection plus the
complete base project journey.

### P7-011 — Model packaging and framework integrations

**Status:** done

Provide manifest/artifact builders, hashing, conformance checks, initial-state
packaging, and synthetic test vectors. Base supports the existing plain callable
boundary. At least one real dependency-backed framework adapter is shipped or
tested as an independent optional plugin; additional sklearn, Torch, or ONNX
adapters are prioritized by maintainable acceptance evidence.

Model packaging never serializes arbitrary import paths or Python source,
installs an environment, downloads an artifact implicitly, trains a model, or
constructs predictions outside the runtime.

Acceptance: tampering fails before construction; the independent adapter
returns `ModelResult` through the normal plugin boundary; a replacement model
can run during replay without creating a framework-specific engine.

Implemented boundary: `eegle.models` now exposes path-free manifest and local
artifact builders plus deterministic `eegle.model_package.v1` archives. Package
indexes, artifact bytes, optional canonical initial state, and contract-bound
synthetic vectors are hash verified before a content-addressed materialization
can be returned. Static conformance checks compare an installed model
descriptor's identity/version, ports, execution modes, state behavior, and
construction API with the manifest. `eegle model pack` and `eegle model check`
are thin clients of those owning services.

The dependency-backed acceptance adapter is an independently built wheel that
declares real scikit-learn/joblib dependencies, loads only a runtime-verified
estimator artifact, and returns `ModelResult` through the existing plugin and
runtime prediction boundary. The base package imports neither dependency and
has no framework engine or training/download path. A separately compiled,
contract-compatible model plan can replay the original captured inputs through
`BundleReplayRunner.run_with_model_replacements()` while all non-target
scientific and operational authorities remain exact. The result is explicit
counterfactual divergence, not an equivalence claim. See
[MODEL_PACKAGING.md](MODEL_PACKAGING.md).

Four dedicated P7-011 tests cover deterministic packaging and initial state,
tamper rejection and safe materialization, CLI/static conformance, the real
independent scikit-learn wheel, and guarded model replacement replay. The
26-test focused model/public/import run and clean-wheel journey pass. The full
Python 3.12 repository suite passes all 339 tests with five explicitly
environment-conditioned skips; compile-all, Ruff, and diff checks pass.

### P7-012 — Session, evidence, replay, and comparison experience

**Status:** done

Expose concise public services and CLI projections for session summary, phase
timeline, source health, admitted/rejected work, model coverage/comparison,
latency, adaptation, action authorization/receipts, integrity, replay ceiling,
replacement replay, and divergence localization.

Acceptance: users need no internal runtime imports; displays project structured
evidence; the first divergence can be located; safe-default exports exclude raw
participant data and protected deployment details.

Implemented boundary: `eegle.operations.inspect_session()` now returns the
additive `eegle.session_inspection.v1` projection with a session summary,
unfinished-writer state, phase timeline, source continuity, admitted/rejected
work, model-result coverage and planned comparisons, latency distributions,
adaptation eligibility/transitions, action authorization/receipts, integrity,
and replay ceilings. Projection sections fail independently. A missing,
partial, recoverable, or corrupt bundle becomes a structured `partial` or
`unavailable` result; inspection never resumes, truncates, finalizes, repairs,
deletes, or signals anything. Raw packet/prediction values, action parameters,
provider evidence, participant pseudonyms, and deployment content are not
included in the report.

`replay_session()` and `compare_session_models()` are public wrappers over the
existing bundle replay and P7-011 guarded replacement runner. They expose the
evaluated ceiling, result status, bounded divergences, and the exact first
divergence while leaving the recorded session unchanged. Expected replacement
divergence is a counterfactual result, not an integrity exception. Their CLI
projections, plus `inspect` and `export`, return structured results with exit 0
by default; `--strict` is an explicit automation opt-in and still performs no
process control.

`export_session()` delegates to the Phase 4 atomic portable-export authority
with the stricter `portable.session-safe-default` policy. Only `public`
artifacts are eligible; semantic evidence logs, execution captures/plans, raw
recordings, component state, interrupted ledgers, deployment bindings,
participant/session identity, and external URIs are excluded. Existing
destinations are never overwritten. Failure publishes no replacement and does
not mutate or remove source evidence.

Three dedicated P7-012 tests plus the P7-011 replacement-replay acceptance
cover every projection family, raw/protected-value omission, byte-for-byte
unfinished-writer preservation, graceful default/strict CLI behavior, actual
counterfactual comparison, public-only export, and occupied-destination
refusal. The 69-test Phase 7/public/source-boundary run passes with five
environment-conditioned skips. The full Python 3.12 suite passes all 342 tests
with those same five skips; compile-all, focused Ruff, and diff checks pass.

### P7-012A — Bounded compositional experiment authoring

**Status:** in progress — named composition, shared lowering, Python/YAML,
distinct-model, structured-action, and explanation slices implemented;
compositional template revisions and CLI project integration remain P7-013
work

The post-P7-012 review correctly identified a template cliff: the eight exact
profiles make supported journeys concise, but structural changes still forced
researchers into canonical component and route declarations. P7-012A fills
that missing middle layer without replacing the existing draft, exact template
revisions, canonical specifications, compiler, engine, evidence, or replay
authorities.

The provisional `ExperimentDesign` vocabulary now supplies immutable named
declarations for:

- study intent and acceptance;
- dense signals with stable channel order and per-channel units;
- sparse event streams;
- ordered installed-plugin processing chains;
- exact time-based continuous and event windows;
- quality gates;
- independently packaged models and explicit comparison groups;
- outcomes, adaptation, and calibration artifacts;
- installed policies and structured action capabilities;
- phases and recording.

Relationships use names such as `signal.eeg`, `processing.clean`,
`window.prestimulus`, and `model.primary`, never canonical list indexes. The
single `lower_experiment_design()` service validates names, detects missing or
cyclic references, and derives ordinary components, routes, phases, model uses,
outcome/adaptation records, action requirements, canonical specs, and a
source-bound provenance sidecar. `ExperimentDesign.to_draft()` enters the same
dispatch through `lower_experiment_draft()`; Python and restricted-YAML values
therefore converge on identical canonical hashes while keeping source origins
separate.

The model helper is contract-oriented: every model declares its own plugin,
manifest, role, input bindings, and configuration. Acceptance compiles two
different manifest digests on the same admitted event window. The new bounded
`eegle.actions.structured_action` policy maps named nested prediction values
into finite structured request parameters. A three-dimensional simulated
velocity design compiles while an absent deployment provider/grant remains
observe-only; the policy cannot grant authority.

`explain_composed_experiment()` reuses the six public scientific, dataflow,
causality, comparison, action, and provenance views. Composed authoring can be
written as separate normalized design, protocol, suite, requirements, and
provenance files without overwriting existing targets.

Remaining scope is deliberately bounded:

- preserve every exact template `1.0.0` expansion unchanged, then add new
  compositional preset revisions rather than silently replacing them;
- make CLI/project scaffolding accept a normalized design source;
- add clean-install compositional reference projects and richer text rendering
  in P7-013.

Acceptance implemented in `tests.test_phase7_compositional_authoring` covers
named channel/window lowering, shared design/draft lowering, order-independent
canonical output, early missing-reference failure, distinct-model compilation,
structured vector parameters, observe-only authorization, composed
explanations, and optional restricted-YAML equivalence.
Eleven dedicated tests and the complete 353-test Python 3.12 repository suite pass
with six explicitly environment-conditioned skips; compile-all, focused Ruff,
and diff checks pass.

### P7-013 — Reference projects, packaging, documentation, and cleanup

**Status:** todo

Publish clean-install projects for recording, event-locked observation, model
comparison, adaptation, simulated closed-loop action, and LSL observe-only.
The recording, observation, comparison, and structured-action projects use the
new compositional vocabulary rather than canonical graph declarations.
Each contains authoring source, canonical outputs, simulation deployment,
commands, expected explanation/evidence, replay, and relevant comparison.

Verify built wheels, optional imports blocked, Python 3.11+ and supported OS
matrix, plugin entry points, metadata, and citation data. Replace public docs
with tested quickstart, authoring, canonical-spec, deployment, compiler,
LSL, model, plugin, evidence/replay, and support-level guides.

Remove Batch 7A and C-003 application shells only after their named adapter,
preflight, process-health, and reference-project evidence has a target owner and
replacement test. Phase 8 analysis evidence remains protected.

Acceptance: projects import public APIs only and do not import `tests`; the
wheel contains only current architecture; documentation matches executable
commands; source-boundary, optional-import, wheel-content, and cleanup checks
pass.

### P7-014 — Phase closure

**Status:** todo

Map every migration exit gate and required user journey to durable acceptance
evidence. Do not close the phase merely because individual commands exist.

## 6. Implementation order

1. **Boundary:** P7-001, P7-002, and the source-mapping part of P7-006.
2. **Simulation product:** P7-003 through P7-007 and the base part of P7-012.
3. **Live EEG:** P7-008 through P7-010.
4. **Model research:** P7-011 and the replacement/comparison part of P7-012.
5. **General authoring:** P7-012A after the operational review, preserving all
   earlier exact revisions.
6. **Proof and cleanup:** P7-013 and P7-014.

The first implementation slice must establish the authoring-to-canonical
boundary before expanding the command surface.

## 7. Deliberate deferrals

Phase 7 does not own a graphical studio, cloud experiment management,
collaborative accounts, remote fleet orchestration, a model zoo, a training
framework, automatic scientific-protocol generation, arbitrary workflow
scripting, generalized hardware safety, a broad dashboard/reporting system,
automated statistical conclusions, or broad modality support claims.

Phase 8 retains comprehensive layered scientific validation, performance
budgets, fault qualification, report/plot depth, support matrices, public-alpha
hardening, and stability/deprecation publication.

## 8. Closure evidence

Phase 7 closes only when:

1. a clean base installation completes create, compile, explain, rehearse, run,
   persist, inspect, and replay for the simulated project;
2. canonical specs and hashes are independent of authoring surface while
   source/default provenance remains visible;
3. a non-model recording workflow remains simple;
4. the same portable suite binds separately to simulation and an observe-only
   LSL deployment and passes documented acceptance;
5. a model researcher can package, compare, replace, replay, and localize a
   candidate through public services;
6. a separately installed plugin participates without EEGle source changes;
7. simulated action produces requests, bounded authorization, commands, and
   receipts while absent authority remains observe-only;
8. public commands and APIs are tested from a clean wheel and contain no recipe
   authority; and
9. protected Phase 7 legacy shells are removed only after their replacement
   evidence exists, while Phase 8 evidence remains intact.

The product rule is: authoring may be convenient, but execution remains
explicit, locked, and evidence-producing.
