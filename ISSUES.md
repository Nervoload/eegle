# EEGle Issues

**Status:** Working issue register

**Snapshot:** 2026-08-04 at commit `8105b2f`

**Scope:** Current source tree, shipped v1 package, tests, examples, packaging,
and product alignment

This document consolidates the issues identified in the structural review and
the subsequent code audit. It corrects measurements and conclusions that were
overstated in the original review and adds issues exposed by comparing the
shipped package with the intended lab workflow.

This is an issue register, not an architecture authority or migration plan.
Product decisions remain unresolved until they are deliberately accepted and
recorded in the appropriate authority document. Passing tests establish that
the current implementation behaves consistently; they do not establish that
EEGle should own the behavior being tested.

## Status and priority vocabulary

- **Confirmed:** directly demonstrated by the current code, package, or tests.
- **Decision required:** a product or ownership choice must be made before a
  correct implementation can be selected.
- **Qualification gap:** code exists, but the relevant real-world claim has not
  been demonstrated.
- **P0:** blocks the intended bench-instrument MVP or risks experimental data.
- **P1:** major architecture, maintenance, or public-surface problem.
- **P2:** cleanup, organization, or release-engineering debt.

## Verified baseline

The following measurements use physical Python lines in the current checkout:

| Measure | Verified value | Notes |
| --- | ---: | --- |
| Total `eegle/` Python | 80,271 | Shipped and excluded source together |
| Python shipped in the wheel | 54,420 | 135 modules, including 298 root-package lines omitted by the earlier 54,122 count |
| Legacy Python excluded from the wheel | 25,851 | Quarantined at the import/package boundary |
| Tests | 22,640 lines | 431 test methods |
| Phase-named tests | 294 methods | 68.2% of all test methods |
| Declared public exports | 625 | Sum of shipped `__all__` surfaces, including the root and nested LSL integration |
| Top-level CLI commands | 16 | Model and plugin subcommands add another level |
| Base built-in plugin descriptors | 19 | LSL descriptors are optional entry-point plugins, not base built-ins |
| Markdown under `docs/` | 9,957 lines | Includes `docs/api/PUBLIC_API.md` |
| Shipped exception classes | 14 | AST count |
| Shipped `raise` statements | 1,712 | AST count; not itself a quality defect |

The tests are pruned from both wheel and source distribution by `MANIFEST.in`.
They increase repository maintenance cost but do not increase the installed
package size.

## P0 — Product and MVP blockers

### ISSUE-001 — The normative product is not the intended bench instrument

**State:** Confirmed; product decision required

**Affected:** `docs/EEGLE.md`, `README.md`, package architecture

The normative vision defines a neurophysiology-general compiler and evidence
platform serving multi-lab deployments, systems engineers, reviewers, and
regulated teams. It includes broad modality representation, physical-action
authorization, exact execution evidence, and consortium-style portability.

The intended lab product is narrower: load an experiment script, validate the
connected setup, supervise recording and realtime processes, run the task
reliably, organize the resulting data, and support replay, labelling, and model
work.

The current code faithfully implements much of the written vision. The primary
failure is therefore product-scope drift, not unstructured implementation.

**Impact:** Future work will continue to optimize the wrong user journey until
the served user, owned responsibilities, and non-goals are narrowed.

**Resolution condition:** Approve a concise product definition based on
executable lab journeys before approving further architecture expansion.

### ISSUE-002 — No shipped command loads and runs a researcher script

**State:** Confirmed

**Affected:** `eegle/operations/cli.py`, `eegle/operations/projects.py`

`eegle run` accepts a compiled EEGle project and verified plan/lock pair. It
does not accept an arbitrary Python script, PsychoPy script, `.psyexp` file, or
task adapter. `ExperimentBuilder` is a builder for EEGle specifications; it is
not a script-execution interface.

**Impact:** The first step of the intended workflow is absent. Researchers must
translate an experiment into EEGle's declarative system before they can run it.

**Resolution condition:** Demonstrate an installed-package journey equivalent
to `eegle run experiment.py --config lab.yaml`, with an explicit task adapter
or subprocess contract and durable session outcome.

### ISSUE-003 — Behavioral task execution was excluded without a replacement

**State:** Confirmed

**Affected:** `eegle/tasks/`, `eegle/experiment.py`, `eegle/feedback_manager.py`,
`eegle/workers/`, packaging configuration

The legacy tree contains PsychoPy tasks and experiment/worker orchestration,
but those modules are excluded from the wheel. The v1 package deliberately
does not ship complete task applications and has no general task bridge that
replaces their operator-facing behavior.

**Impact:** EEGle can execute component graphs but cannot perform the ordinary
lab task workflow the package was originally intended to simplify.

**Resolution condition:** Select and validate a general task boundary before
deleting the legacy task evidence. Prefer delegation to PsychoPy or a generic
script/process adapter over moving task applications into core.

### ISSUE-004 — Device validation is plan-oriented, not complete readiness validation

**State:** Confirmed qualification gap

**Affected:** `eegle/operations/discovery.py`,
`eegle/operations/preflight.py`, `eegle/integrations/lsl/`

Detection and preflight provide useful typed LSL discovery, exact selectors,
capability matching, lock verification, storage declarations, clock policy,
and operator checks. They do not yet establish complete device readiness,
including sustained sample delivery, signal plausibility, recorder health,
driver/application state, disk growth, or task-environment readiness.

Real EEG observe-only acceptance is still explicitly pending.

**Impact:** A passing preflight may be interpreted more strongly than its
evidence supports.

**Resolution condition:** Define a device/readiness acceptance contract and
exercise it against the lab's actual amplifier, marker path, recorder, task
environment, and storage destination.

### ISSUE-005 — End-to-end latency, jitter, and synchronization are not qualified

**State:** Confirmed qualification gap

**Affected:** `eegle/validation/qualification_profile.json`,
`tests/test_phase8_performance_faults.py`, `eegle/integrations/lsl/`

The published engine budget measures at least 250 small packets per second and
at most 4 ms mean dispatch time in an in-process deterministic simulation. It
excludes physical transport and most durable filesystem cost. The shipped
runtime does not publish an end-to-end jitter metric, sustained process-level
latency distribution, or reference-host synchronization qualification.

The LSL source records raw timestamps, an initial clock correction, reconnects,
and inferred gaps. Validation can warn about the span of supplied clock
corrections, but this is not continuous oscillator, transport, or end-to-end
timing qualification.

**Impact:** The package cannot support claims about optimal or acceptable lab
latency, jitter, and synchronization from the current automated evidence.

**Resolution condition:** Measure source-to-admission, source-to-model,
marker-to-window, and source-to-disk timing under real process placement and
representative devices for a sustained session.

### ISSUE-006 — The shipped runtime is not a process supervisor

**State:** Confirmed

**Affected:** `eegle/runtime/plan_runtime.py`,
`eegle/operations/projects.py`, excluded worker stack

The runtime models placements, including proxy placements, but non-in-process
placements require a `proxy_factory`. The normal CLI execution path supplies
no process proxy factory. The legacy `FeedbackManager` owns the actual recorder,
realtime, dashboard, and analyzer process lifecycle, but it is excluded.

**Impact:** EEGle does not currently ensure that the realtime and recording
processes are running, ready, monitored, and shut down correctly.

**Resolution condition:** Implement or delegate a small supervisor with
readiness, heartbeat, bounded restart, shutdown, and failure-domain semantics.

### ISSUE-007 — Runtime failure semantics do not match lab fault domains

**State:** Confirmed; design decision required

**Affected:** `eegle/runtime/graph.py`, `eegle/recording/bundles.py`,
`eegle/operations/projects.py`

The runtime has some explicit nonfatal behavior: model-result rejection,
reject-newest and shed-oldest model queues, late-input rejection, LSL reconnect
attempts, and packet-loss observations. It does not have a general health policy
that distinguishes task, source, recorder, realtime model, feedback, dashboard,
and analysis failures.

Admitted packet capture and semantic evidence writes are synchronous. Capture
or evidence sink exceptions propagate into failed execution. The outer runner
preserves a failed or partial bundle, but that is failure preservation rather
than fault tolerance.

**Impact:** Noncritical observer/model failures may terminate a run, while a
naive future "continue on all source/sink errors" policy could allow a task to
continue after authoritative recording has stopped.

**Resolution condition:** Approve and test a role-specific fault matrix. At a
minimum:

- dashboard, report, and nonessential observer failure should not stop capture;
- shadow/candidate model failure should normally disable that model and continue;
- feedback failure should normally preserve recording and mark the session;
- temporary source interruption may recover within declared limits and must
  record the gap;
- primary recorder loss, corrupt storage, or unknown write state should stop
  the task unless verified redundant recording remains healthy;
- task failure should stop cleanly and preserve a partial session.

### ISSUE-008 — EEGle owns a custom raw execution-capture format without an accepted raw authority

**State:** Confirmed; ownership decision required

**Affected:** `eegle/recording/framing.py`, `eegle/recording/stores.py`,
`eegle/recording/bundles.py`

The v1 package synchronously writes admitted packets into a checksummed custom
`.eegle` framed store. This is useful for exact engine replay, but it overlaps
responsibilities already handled by LSL/LabRecorder/XDF and established
neurophysiology storage and organization tools such as BIDS/MNE-BIDS.

The architecture says EEGle does not own a universal raw format, yet the
installed workflow makes EEGle's execution capture central while source-native
or archival raw recording remains an external reference.

**Impact:** Storage code and evidence semantics are substantial, while the
simple authoritative raw-data workflow needed by the lab is unresolved.

**Resolution condition:** Decide whether authoritative raw data are recorded by
LabRecorder/XDF, a vendor recorder, or another adapter. If exact admitted-input
capture remains valuable, define it as a bounded evidence sidecar rather than
an implicit replacement for archival raw data.

### ISSUE-009 — Training and general labelling are absent from the shipped package

**State:** Confirmed

**Affected:** `eegle/models/`, CLI, excluded `eegle/realtime/` and
`eegle/analysis/`

The shipped model package can describe, pack, verify, admit, replay, and compare
already-trained models. It has no user-facing training command or general
labelling workflow. The relevant training, epoch, and label code remains in the
excluded legacy tree.

**Impact:** The model packaging system is more developed than the workflow that
produces and labels models for lab use.

**Resolution condition:** Define small training and labelling extension points
that delegate to user code and established ML/neurophysiology libraries such as
MNE, sklearn, or Braindecode. Do not assume the legacy training implementation
should be copied into core.

### ISSUE-010 — Replay is strong only inside EEGle's current artifact universe

**State:** Confirmed; ownership decision required

**Affected:** `eegle/replay/`, `eegle/recording/`, `eegle/integrations/mne.py`

Fresh-engine replay, equivalence comparison, and replacement-model replay are
substantial and well tested for EEGle evidence bundles. Direct replay from the
lab's likely authoritative raw formats and task/session outputs is not the
primary public path.

**Impact:** A powerful internal replay feature may not serve data collected by
the eventual MVP recorder without conversion into EEGle's artifact model.

**Resolution condition:** Demonstrate replay from the selected authoritative
raw/session format into a user-provided analysis or model hook. Retain exact
same-engine replay only to the extent that it supplies needed scientific value.

### ISSUE-011 — The mandatory compiler/runtime/evidence spine has not earned MVP centrality

**State:** Decision required

**Affected:** `eegle/specs/`, `eegle/compiler/`, `eegle/runtime/`,
`eegle/recording/`, `eegle/replay/`, `eegle/validation/`

The spine is coherent and functioning: canonical specifications compile into
hashed plans and locks, one engine runs them, evidence is persisted, and the
same engine replays them. Coherence and test coverage do not prove that this
must be the mandatory interface for a bench instrument.

For the simplest recording reference, a 70-line design expands into a
1,411-line simulation deployment dominated by literal sample data. A much
simpler supervisor-and-script architecture could satisfy the initial lab
journey while keeping a compact provenance manifest.

**Impact:** Treating the spine as automatically retained may preserve the
largest source of product complexity without comparing it to the intended user
workflow.

**Resolution condition:** Build or prototype the simplest vertical lab path,
then decide whether the spine is mandatory, optional advanced functionality,
substantially reduced, or removed. Preserve capabilities based on accepted use
cases, not package boundaries or sunk test investment.

## P1 — Architecture and scope issues

### ISSUE-012 — Two overlapping authoring systems are public simultaneously

**State:** Confirmed

**Affected:** `eegle/authoring/`

The draft/template/profile/builder/YAML path and the newer
design/composition/preset/lowering path both produce the same canonical
`ProtocolSpec` and `SuiteSpec` families. They share some implementation paths,
so they are not completely independent, but they duplicate public vocabulary,
schemas, lowering, provenance, tests, and documentation.

**Impact:** Researchers face multiple ways to express the same experiment, and
maintainers must preserve equivalence between them.

**Resolution condition:** Select one researcher-facing authoring model. Retain
compatibility only where a real user artifact requires migration.

### ISSUE-013 — The provisional public API is far too large

**State:** Confirmed

**Affected:** package `__init__.py` files and `docs/api/PUBLIC_API.md`

The shipped surfaces declare 625 public names. Large contributors include
`operations` (107), `authoring` (94), `recording` (67), `runtime` (53), `specs`
(47), `validation` (45), and `compiler` (41).

**Impact:** Even before public alpha, internal implementation types are being
treated as compatibility commitments. This makes simplification and safe
renaming much harder.

**Resolution condition:** Define a small task-oriented public surface and make
lower-level packages explicitly provisional or internal. Do not use the current
`__all__` inventory as a reason to preserve every name.

### ISSUE-014 — The action/authorization system is broader than the bench MVP

**State:** Confirmed; product decision required

**Affected:** `eegle/actions/`, `eegle/runtime/action_broker.py`, specs,
compiler, plugins, replay, and validation

The code implements policy request, deployment authorization, interlock,
command, cancellation, receipt, and evidence semantics suitable for physical
or prosthetic control. Basic neurofeedback and task adaptation may need only a
bounded action request plus task acknowledgement.

The action concepts are deeply cross-cutting; removing them is not a low-risk
1,700-line deletion.

**Impact:** A specialized safety architecture increases core schemas, runtime
states, validation, tests, and authoring complexity for an unconfirmed MVP use
case.

**Resolution condition:** Decide which action classes the lab actually needs.
Keep a minimal task/feedback action contract in core if required; move physical
authorization and actuator semantics behind optional integrations if they are
not an accepted first-release use case.

### ISSUE-015 — Privacy, retention, export, and legacy import policy exceed the confirmed scope

**State:** Decision required

**Affected:** `eegle/recording/policies.py`,
`eegle/recording/importers.py`, `eegle/operations/sessions.py`

The package includes four sensitivity classes, digest-bound redaction,
portable-export manifests, retention decisions, and a large importer for the
historical session layout. These may be disproportionate to the first lab MVP,
but human neurophysiology data remain sensitive even in a single lab.

**Impact:** The current implementation adds considerable policy surface, while
deleting it without a data-handling decision could remove necessary safeguards
or strand existing sessions.

**Resolution condition:** Define the minimum privacy/export contract and the
legacy-data migration window. Simplify aggressively, but preserve or externalize
the migration tool until existing data no longer require it.

### ISSUE-016 — Discovery and explanation services are larger than their MVP value

**State:** Confirmed; scope decision required

**Affected:** `eegle/operations/discovery.py`,
`eegle/operations/explanations.py`, `eegle/operations/preflight.py`

Discovery is 1,681 lines and explanations are 994 lines. They support typed
deployment proposals, provenance, ambiguity diagnostics, and multi-view
explanations. Some of this is valuable; much of it exists because authoring and
compilation are broad.

**Impact:** Operator tooling is optimized for explaining generated plans rather
than quickly confirming that a real lab setup is ready.

**Resolution condition:** After the product path is selected, retain only the
discovery and explanation behavior needed by the accepted preflight and repair
journeys.

### ISSUE-017 — Placement abstractions exceed executable placement support

**State:** Confirmed

**Affected:** `eegle/specs/`, `eegle/compiler/`,
`eegle/runtime/plan_runtime.py`

Plans can describe non-in-process placements, secrets, and proxies. The normal
installed run path cannot construct those placements without an externally
supplied proxy factory.

**Impact:** Users and tests can manipulate deployment concepts that the public
runner cannot execute.

**Resolution condition:** Either implement the small set of real process
placements required by the MVP supervisor or remove/defer generalized placement
semantics from the public contract.

### ISSUE-018 — EEGle recreates general graph/runtime behavior without a proven need

**State:** Confirmed overlap; ownership decision required

**Affected:** compiler, runtime, plugin, authoring, and scheduling packages

EEGle owns a typed port graph, component descriptors, scheduling, queues,
lifecycle, placements, and plugin construction. This overlaps established
realtime biosignal and workflow systems such as Timeflux. The architecture
simultaneously says EEGle does not own a general-purpose DAG language.

**Impact:** EEGle carries the maintenance cost of a general execution framework
while the original MVP could be implemented as orchestration around scripts and
established packages.

**Resolution condition:** Document the concrete scientific guarantees that
cannot be achieved through a thinner integration layer. Remove or delegate the
remainder.

### ISSUE-019 — Processing behavior risks expanding into MNE/MNE-LSL duplication

**State:** Confirmed risk

**Affected:** `eegle/processing/`, `eegle/integrations/mne.py`

The current maintained processing set is comparatively small: identity and SOS
filters, bounded buffers, windows, and finite quality. The MNE bridge correctly
delegates object creation and retains EEGle-specific timing sidecars.

**Impact:** Adding broader preprocessing, epoching, rejection, resampling, or
feature algorithms to core would recreate established scientific packages.

**Resolution condition:** Keep EEGle processing limited to orchestration and
timing/lineage behavior that external tools cannot represent. Prefer adapters
for scientific algorithms.

### ISSUE-020 — Qualification machinery is more mature than real qualification

**State:** Confirmed

**Affected:** `eegle/validation/`, qualification profile and Phase 8 tests

Schemas, evidence taxonomies, profiles, result layers, and support matrices are
extensive. Current empirical qualification is narrow: deterministic simulation,
injected LSL behavior, a tiny native local-network smoke test, and no retained
real EEG session.

**Impact:** Formal validation structures may create an impression of operational
readiness beyond the evidence they aggregate.

**Resolution condition:** Prioritize real acceptance records and operator
outcomes over additional validation taxonomy.

### ISSUE-021 — Reference deployments inline large synthetic datasets

**State:** Confirmed

**Affected:** `reference_projects/*/deployments/simulation.json`

Simulation deployments contain roughly 1,400–1,700 lines, largely literal
sample arrays. This obscures the deployment information being demonstrated and
inflates generated explanations and provenance.

**Impact:** Reference projects are difficult to review and make the authoring
system appear more ceremonious than necessary.

**Resolution condition:** Use a small deterministic seeded source plugin or
compact fixture reference when exact literal values are not the subject of the
test.

### ISSUE-022 — The excluded legacy tree remains substantial repository debt

**State:** Confirmed

**Affected:** 25,851 excluded Python lines and associated tests/docs/configs

The legacy tree is cleanly excluded from installed artifacts and is not imported
by v1. It nevertheless contains behavior needed by the intended MVP, including
task execution, process supervision, timing/health monitoring, epoching,
training, and labelling.

**Impact:** It complicates navigation, packaging, tests, examples, and design
discussions. Deleting it immediately would also erase the only implementation
evidence for several required behaviors.

**Resolution condition:** Convert required behavior into explicit acceptance
tests or adapters, intentionally reject unwanted behavior, then delete the
legacy sources and their obsolete tests.

### ISSUE-023 — Packaging encodes the legacy split in multiple places

**State:** Confirmed

**Affected:** `pyproject.toml`, `setup.py`, `MANIFEST.in`, packaging tests

Package discovery allowlists target subpackages, a custom `build_py` subclass
excludes selected root modules, and `MANIFEST.in` separately prunes/excludes
legacy content from source distributions.

These mechanisms have distinct setuptools roles, so they are not literally the
same rule repeated three times. They exist because target and legacy modules
share one source namespace.

**Impact:** Package correctness depends on synchronized exclusion logic and
expensive installed-artifact tests.

**Resolution condition:** Delete or relocate the legacy tree after its evidence
is harvested, then simplify package discovery and retain wheel/sdist smoke tests
for the resulting normal layout.

### ISSUE-024 — All four root Python examples are broken for installed users

**State:** Confirmed

**Affected:** `examples/02_simulated_lsl_recording.py` through
`examples/05_replay_classifier.py`

- Example 02 imports nonexistent `eegle.streams.simulated` rather than the
  available `synthetic` module.
- Examples 03–05 import legacy packages excluded from the wheel.
- Example 04 documents a `torch` extra that is not declared.

**Impact:** The most discoverable user code demonstrates unsupported APIs and
works differently in a checkout than after installation.

**Resolution condition:** Remove or rewrite every example against the selected
installed MVP. Add an automated installed-wheel example test.

### ISSUE-025 — Empty historical directories and bytecode residue remain

**State:** Confirmed P2 cleanup

**Affected:** `eegle/core/`, `eegle/pipelines/`, `eegle/protocols/`

These no longer contain tracked Python source but retain local `__pycache__`
residue from deleted paths.

**Impact:** Minor navigation confusion and evidence of incomplete local cleanup.

**Resolution condition:** Remove local generated residue and empty directories;
do not recreate their deleted APIs.

## P1 — Test and evidence issues

### ISSUE-026 — Tests are organized by migration phase rather than behavior

**State:** Confirmed

**Affected:** `tests/test_phase*.py`

Sixty-eight percent of test methods are in files named for phases 0–8. Many are
valuable current contract tests, but their names describe when the behavior was
implemented rather than what it verifies.

**Impact:** Maintainers cannot easily locate the complete coverage for LSL,
windowing, recording recovery, model packaging, or CLI behavior. Completed
migration scaffolding is harder to distinguish from durable acceptance tests.

**Resolution condition:** Reorganize retained tests by product behavior after
the package boundary is selected. Preserve scientific invariants and installed
artifact tests; remove phase governance tests when their gates are retired.

### ISSUE-027 — Legacy and migration-boundary tests remain a material maintenance cost

**State:** Confirmed

**Affected:** multiple test modules

At least nine test modules directly import packages excluded from the wheel,
totalling 3,734 lines. Additional inventory, source-boundary, packaging, and
migration tests inspect the legacy split. The earlier claim of exactly thirteen
legacy files and approximately 5,300 lines depends on a broader, unstated
classification and should not be treated as an exact measurement.

**Impact:** The repository validates behavior that installed users cannot call,
while also paying for multiple layers of migration governance.

**Resolution condition:** Classify each test as durable product acceptance,
scientific behavior to harvest, packaging boundary, or completed migration
record. Delete tests only after their required replacement evidence exists.

### ISSUE-028 — Synthetic tests establish architecture, not lab readiness

**State:** Confirmed qualification gap

**Affected:** reference projects, LSL tests, Phase 8 performance tests

The six reference projects run simulated deployments. Most LSL tests use an
injected pylsl-like network. The installed native smoke verifies a local outlet,
two channels, two samples, metadata, and MNE conversion. Performance tests run
small in-process packets and constructed fault evidence.

**Impact:** The suite gives strong confidence in deterministic contracts but
limited confidence in long-duration, multi-device, process, disk, task, and
recovery behavior.

**Resolution condition:** Add a retained non-participant real-lab acceptance
record and fault campaign covering representative session duration, devices,
processes, recorder, disk, task, and optional realtime model.

### ISSUE-029 — Strong tests are locking potentially unnecessary architecture

**State:** Confirmed risk

**Affected:** compiler, authoring, action, model-package, and evidence tests

Many tests are high quality and genuinely validate current shipped behavior.
That does not make the tested architecture an MVP requirement. In particular,
compiler locks, generalized action authorization, dual authoring, deployment
proposal, and exact model-replacement machinery should not survive solely
because their test suites are comprehensive.

**Impact:** Sunk implementation and test investment can bias product decisions
toward retaining unnecessary systems.

**Resolution condition:** Derive the durable acceptance suite from user journeys
and scientific invariants. Retain implementation-specific tests only for
capabilities that remain accepted product scope.

### ISSUE-030 — There is no executable six-journey MVP acceptance suite

**State:** Confirmed

**Affected:** tests, examples, CI, product definition

The repository has rich component and reference-project coverage but no single
acceptance definition proving the intended sequence:

1. load a researcher script;
2. validate required devices and processes;
3. verify recording/realtime health and timing;
4. run a task through declared fault conditions;
5. finalize and organize authoritative raw data and outcomes;
6. replay, label, and invoke model training or evaluation.

**Impact:** Architecture can pass all tests while the intended product remains
unusable.

**Resolution condition:** Define these journeys before the next major cleanup
and make them the top-level product gates.

## P1 — Documentation and decision issues

### ISSUE-031 — `docs/EEGLE.md` codifies the scope expansion

**State:** Confirmed

**Affected:** `docs/EEGLE.md`

The normative document includes OPM-MEG, Neuropixels, TMS/tES/DBS and robotic
action targets, a regulated-team persona, broad storage formats, and an
EEG-controlled prosthetic journey. It also explicitly excludes complete task
applications and PsychoPy UI from the base package.

**Impact:** The implementation is being measured against a product vision that
does not match the intended first users. Code cleanup alone will not hold while
this remains the architecture authority.

**Resolution condition:** Replace or revise the normative vision only after an
explicit product decision. Preserve the current file as historical evidence if
it is superseded.

### ISSUE-032 — Documentation mixes authority, live status, completed process records, and legacy evidence

**State:** Confirmed

**Affected:** approximately 10,000 Markdown lines under `docs/`

The repository contains normative architecture, migration sequencing, live
status, phase plans, closure evidence, public API inventories, integration
guides, pre-vision architecture, and historical library reviews. Some files
declare themselves legacy while still presenting extensive recommendations.

**Impact:** Readers and agents must understand a complex authority hierarchy
before determining what EEGle currently is. Completed phase history obscures
current product decisions.

**Resolution condition:** In the next documentation pass, inventory every
Markdown file as one of: current authority, current user guide, current
operations/reference, historical decision record, generated evidence, or
deletion candidate. Do not rewrite content before that classification is
approved.

### ISSUE-033 — Documentation promises breadth beyond validated support

**State:** Confirmed qualification gap

**Affected:** product vision, README, research support matrix

The architecture distinguishes representable, adapter available, validated,
and reference-supported levels, which is good. The breadth of modalities,
actions, models, and storage formats nevertheless dominates the vision while
the only reference-supported path is simulation-first EEG and real EEG
acceptance remains pending.

**Impact:** Prospective users may confuse a generic type system with functioning
integration or operational support.

**Resolution condition:** Lead public documentation with currently executable
and validated lab journeys. Move representable future scope to a clearly
noncommittal roadmap or remove it from the first product definition.

### ISSUE-034 — Documentation cleanup depends on unresolved product decisions

**State:** Decision required

**Affected:** all current docs

Deleting completed phase files or shortening the vision before deciding the
product boundary risks losing evidence or writing a third conflicting story.

**Impact:** A cosmetic documentation cleanup could make the repository look
simpler without resolving its architectural contradiction.

**Resolution condition:** First classify documents and identify decisions they
record. Then approve the bench MVP, ownership boundaries, and acceptance
journeys. Only afterward rewrite current authorities and archive/delete
historical material.

## P2 — Release and maintenance issues

### ISSUE-035 — Release engineering validates packaging better than user usefulness

**State:** Confirmed observation

**Affected:** `.github/workflows/package.yml`

The workflow's build, wheel, sdist, optional-integration, plugin, and reference
jobs are technically appropriate for the current fragile split package. Job
count alone is not overengineering, and the manual TestPyPI job is safely gated.

The workflow proves that the selected v1 artifact works outside the checkout;
it does not prove that v1 performs the intended lab workflow.

**Impact:** A green release workflow can be mistaken for product readiness.

**Resolution condition:** Keep installed-artifact checks while simplifying the
source boundary. Add the real MVP journeys; consolidate CI jobs only for cost or
clarity, not as a substitute for architectural simplification.

### ISSUE-036 — Pre-execution validation is much stronger than runtime recovery

**State:** Confirmed

**Affected:** schemas, compiler, runtime, supervisor boundary

The package has 14 custom exception classes and approximately 1,712 explicit
raise sites, with extensive checks for schemas, hashes, identities, ports,
versions, causality, permissions, and locks. The number of raises is not itself
a defect. The imbalance is that malformed definitions are handled much more
comprehensively than device, process, recorder, and storage degradation during
a session.

**Impact:** Engineering effort is concentrated before execution while the lab's
highest-cost failures occur during execution.

**Resolution condition:** Do not weaken useful preflight validation. Shift the
next reliability work toward runtime health, fault classification, recovery,
and authoritative-data protection.

### ISSUE-037 — Model transport and admission precede the model-development workflow

**State:** Confirmed; scope decision required

**Affected:** `eegle/models/`, compiler model bindings, runtime model admission,
replacement replay, model-package tests

EEGle has deterministic model manifests, content-addressed artifact packaging,
pre-materialization integrity checks, installed adapter conformance, state
artifacts, role semantics, and counterfactual replacement replay. These are
well-implemented reproducibility capabilities. The shipped product still lacks
the simpler lab workflow for preparing labels, extracting a dataset, invoking
training, selecting a model, and attaching the result to an experiment.

**Impact:** Advanced transport and admission machinery can become the permanent
model architecture before ordinary model use establishes which contracts are
actually necessary.

**Resolution condition:** Start from one real lab train-to-run-to-replay journey.
Retain the package and admission features that journey or an accepted
reproducibility requirement needs; move the remainder to optional tooling or
defer it.

## Corrections and constraints for future cleanup

The following statements must not be treated as accepted conclusions:

1. **The tests are not pip bloat.** They are excluded from distributions. Their
   issue is maintenance focus and architectural lock-in.
2. **The legacy tree is not coupled to v1 at runtime.** It is repository debt
   and protected implementation evidence. Delete it only after required
   behaviors have replacements or explicit rejection decisions.
3. **The compiler/runtime/evidence spine is not automatically retained.** It
   must justify its mandatory role against a simpler lab supervisor.
4. **Do not default all source and sink failures to log-and-continue.** Recorder
   and corrupt-storage failures may require immediate task termination.
5. **Do not port legacy algorithms wholesale.** Harvest requirements and tests,
   then delegate to established packages or user hooks where possible.
6. **Do not delete privacy handling without a human-data decision.** Simplify it
   only after defining the minimum safe export and retention behavior.
7. **Do not remove packaging CI merely because the package is pre-alpha.** The
   current source split makes installed wheel and sdist checks valuable.
8. **Do not preserve architecture solely because 431 tests pass.** Passing tests
   prove internal consistency, not product necessity.
9. **Do not claim six-of-six MVP coverage after porting monitoring, epoching,
   training, and labels alone.** Script/task execution and authoritative
   recording would still be missing.

## Decisions required before design cleanup

The next product/design pass should answer these questions explicitly:

1. Who is the first served user: this lab, external labs, model researchers, or
   systems/device integrators?
2. Is the first public journey script-first, declarative-project-first, or are
   both truly required?
3. What program owns authoritative raw recording?
4. What minimum session manifest and evidence must EEGle own beside that raw
   recording?
5. Is exact same-engine replay a core requirement, an optional advanced mode,
   or unnecessary for the first release?
6. Does the compiler remain mandatory, become optional, or get replaced by
   direct validated configuration?
7. Which process roles must EEGle start and supervise?
8. Which failures stop the participant task, which disable a component, and
   which only produce warnings?
9. What is the task adapter contract for Python and PsychoPy experiments?
10. Are training and labelling owned workflows or extension hooks?
11. Which actions are required: observe-only output, task adaptation,
    neurofeedback, or physical-device control?
12. What privacy, export, and legacy-session migration behavior is required for
    current lab data?

## Scope of the next documentation pass

The next pass should evaluate Markdown cleanup without yet changing product
semantics:

1. List every Markdown file and its current audience.
2. Identify authority conflicts and duplicated explanations.
3. Mark files as retain/current, rewrite, merge, archive, generated evidence, or
   delete.
4. Extract decisions and acceptance evidence before removing phase records.
5. Propose a minimal current documentation set for approval.
6. Use the approved product decisions and MVP acceptance journeys to rewrite
   `docs/EEGLE.md`, the README, migration guidance, and user-facing guides.

No code or legacy evidence should be deleted merely because this register names
it as an issue. Deletion should follow an accepted product boundary and an
executable replacement or explicit rejection of the affected behavior.
