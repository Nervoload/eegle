# EEGle

EEGle is an EEG-first, neurophysiology-general Python framework for reproducing,
recording, replaying, and validating synchronized model systems. It compiles
portable scientific intent plus site-local deployment bindings into one
immutable typed execution plan, then uses the same engine for simulated,
recorded, and replay execution.

This branch is the clean v1.0 rebuild. The first destructive cleanup removed
the superseded compatibility facades and the alpha, attention, classification,
DSART, and inhibition study recipes. Selected task, PsychoPy, LSL/hardware, model,
adaptation, and analysis code remains temporarily as Phase 6–8 extraction
evidence. It is not a public compatibility surface.

## Current status

Phases 0 through 6 are complete and Phase 7 is active. The Phase 7 product
boundary and work plan are recorded in
[`docs/PHASE7_AUTHORING_OPERATIONS.md`](docs/PHASE7_AUTHORING_OPERATIONS.md).
P7-001 has locked the public/provisional surface, authoring payload envelopes,
shared diagnostic/exit contract, and optional YAML distribution boundary;
P7-002 now adds typed incomplete drafts, deterministic recording-only lowering,
portable deployment requirements, non-hashing provenance, and source-mapped
canonical diagnostics. P7-003 adds eight exact-version, deterministic templates
for recording, observation, comparison, calibration/validation, delayed
adaptation, and simulated action. P7-004 and P7-005 add persistent typed Python
authoring and a restricted optional YAML 1.2 adapter over that same lowering
path. P7-006 adds complete authoring/locked-plan explanations, four-impact
diffs, and source-aware non-mutating diagnostic guidance. P7-007 adds separated
project scaffolding, shared Python operations, and the first public
artifact-oriented CLI journey. P7-008 adds canonical installed-capability
detection, typed site observations, and immutable review-only deployment
proposals. P7-009 adds capability-based preflight plus fault-evidenced,
fail-closed rehearsal. P7-010 now has a dependency-lazy first-party LSL
source/outlet/discovery implementation with simulated acceptance; its real EEG
observe-only acceptance remains explicitly pending. P7-011 adds deterministic,
hash-verified model packages, initial state and synthetic vectors, CLI
pack/check operations, a real independently installed scikit-learn adapter
proof, and contract-guarded replacement replay through the same engine. The
P7-012 session experience adds privacy-aware projections for phases, sources,
work, models, latency, adaptation, actions, integrity, and replay readiness;
public replacement comparison and non-overwriting safe export; and structured
partial/unavailable results that never recover, truncate, finalize, delete, or
signal another process. The verified v1 foundation provides:

- versioned `ProtocolSpec`, `SuiteSpec`, and `DeploymentSpec` contracts;
- a typed compiler with structured diagnostics and semantic passes;
- one graph-bearing `eegle.execution_plan.v1` plus a separate lock manifest;
- exact descriptor-locked plugin construction;
- a modality-neutral graph and phase execution engine;
- deterministic availability scheduling, watermarks, bounded queues, and
  semantic component deadlines;
- compiled scheduled/state triggers, semantic phase timeouts, protocol
  acceptance decisions, and integrity-checked mid-phase restoration;
- permission-driven model scheduling, role-local queue/failure dispositions,
  and nonfatal backpressure policy;
- compiler-checked outcome, adaptation, and action-capability permissions;
- generic sessions, evidence bundles, artifact stores, and execution capture;
- bundle-driven replay through the same locked graph with graded equivalence.
- deterministic model packages with pre-materialization integrity checks and
  optional-framework adapters kept in independent plugin distributions.

Phase 6 has fixed model-system authority, introduced modality-neutral model
contracts and path-free manifests, and joined suite model intent plus local
artifact materialization into immutable `PlannedModelBinding` values. Bound
model plugins now return contract-validated `ModelResult` values; the sole
runtime creates canonical predictions with plan-owned identity, exact graph
input lineage, model-state digests, timing, and terminal result dispositions.
Compiled primary, candidate, shadow, observer, and custom-role permissions now
govern priority, queueing, failure isolation, and policy influence. Exact
complete/incomplete comparison evidence is produced for locked comparison
groups. Enrolled direct-reference outcome expectations now provide bounded,
checkpointable delayed-label lifecycles with explicit terminal dispositions.
Calibration artifacts are content-addressed, and permissioned online adaptation
is recorded as eligibility plus requested/applied/rejected/no-op/failed/rollback
state transitions that restore and replay exactly. Policies now emit action
requests into a deployment-owned authorization broker. Exact provider and grant
locks enforce capability, parameter, timing, expiry, and failure bounds before
the runtime can construct an actuator-ready command; missing authorization is
observe-only. Pending decisions, cancellations, simulated receipts, and every
terminal disposition are durable evidence. Replay accepts simulation-only
action services and fails before constructing a physical or operator-facing
service. Four representational suites now run dense EEG-like, irregular
fNIRS-like, sparse spike plus dense LFP, and multi-rate auxiliary/behavior data
through the same compiler, runtime, evidence, and replay path. Minimal
estimator-shaped and tensor-callable fixtures confirm that optional framework
adapters need only be ordinary external model plugins returning `ModelResult`;
EEGle does not need a framework-specific runtime. These are compatibility
proofs, not claims of validated modality or sklearn/Torch support. Phase 6
closure additionally verifies artifact-backed factory construction and initial
state, parameterized preprocessing lineage, required-output accounting,
provider-visible action parameters, and alias-safe rollback. Every model now
requires a manifest binding; no classifier-shaped built-in, v1 prediction,
suite-wide role fallback, or `eegle.ml` package ships in the v1 wheel.

The public base CLI now provides `new`, `detect`, `compile`, `explain`, `diff`,
`graph`, `preflight`, `rehearse`, `run`, `inspect`, `replay`, `compare`,
`export`, and `model` pack/check operations. It is available as `eegle` and
`python -m eegle`; add `--json` before the command for the versioned machine
result/error envelope. Inspection, replay, comparison, and export degrade to a
structured result with exit 0 by default so an observer cannot end a recording,
long-running analysis, or training process. Automation may explicitly request
the older fail-on-attention behavior with `--strict`. Validation and plugin
command families arrive with their owning tasks. Old study commands are not
installed by the pip package.

Migration-only root orchestration modules remain in the checkout as extraction
evidence but are excluded from built wheels. The wheel contains only
`eegle.__main__`, the root package metadata/validation modules, and the target
subpackages selected in `pyproject.toml`.

## Installation

EEGle requires Python 3.11 or newer.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

The base dependency set is NumPy, SciPy, jsonschema, and packaging. LSL, MNE,
PsychoPy, scikit-learn, Torch, plotting, and vendor integrations remain optional
and must not be imported by the base foundations.

Install restricted YAML authoring explicitly when needed:

```bash
python -m pip install -e ".[yaml]"
```

Install the first-party LSL adapter explicitly when needed:

```bash
python -m pip install -e ".[live]"
```

## First simulation

The base install can create, compile, explain, rehearse, execute, inspect, and
replay a deterministic recording-only simulation without hardware or optional
dependencies:

```bash
eegle new first-simulation --id first-simulation
eegle detect --no-entry-points
eegle compile first-simulation
eegle explain first-simulation
eegle graph first-simulation
eegle preflight first-simulation
eegle rehearse first-simulation --session-id session.first.rehearsal
eegle run first-simulation --session-id session.first.run
eegle inspect first-simulation
eegle replay first-simulation
eegle export first-simulation first-simulation-export
```

The scaffold keeps `authoring/`, generated canonical values, `deployments/`,
content-addressed `builds/` and `locks/`, explanations, and `sessions/`
separate. `run` resolves and verifies the compiled plan/lock pair and does not
execute the mutable authoring source. The automatically generated simulation
deployment is deliberately limited to the continuous-recording first journey.
`eegle detect` runs independently and can index installed plugins and model
manifest entry points, explicitly named model manifests, and typed output from
optional integration detectors.
With a project and `--propose`, it writes a content-addressed detection report,
an ordinary proposed `DeploymentSpec`, and a provenance sidecar without
replacing the simulation deployment. Compile that proposal only after review:

```bash
eegle detect first-simulation --observations site-capabilities.json --propose
eegle compile first-simulation --deployment deployment_proposal
```

Ambiguous compatible resources require `--select-source`, `--select-storage`,
or `--select-clock`. Detector configuration cannot contain credential-shaped
literals; Python callers provide `SecretReference` bindings explicitly.
Detected authorization providers are evidence only and never create a grant.
Preflight verifies the exact plan/lock/deployment and the capabilities each
lock declares. Rehearsal accepts only simulation resources and simulation-only
action services; its eight initial fault dispositions are persisted in the
normal evidence bundle and bound by a separate rehearsal report.

`eegle inspect` reports session status, unfinished writers, phase timeline,
source continuity, admitted/rejected work, model coverage/comparisons, latency,
adaptation, action authorization/receipts, integrity, and replay ceiling. It
does not expose participant pseudonyms, raw values, action parameters, provider
evidence, or deployment content. `eegle export` uses a public-only policy by
default, excludes evidence logs, execution captures/plans, raw recordings,
component state, and deployment bindings, refuses an existing destination, and
never changes the source session. `eegle replay` and `eegle compare` report the
first bounded divergence without treating an expected counterfactual model
difference as a crash.

With `eegle[live]`, run bounded LSL discovery explicitly:

```bash
eegle detect first-simulation --lsl --lsl-wait 2
```

The adapter provides dense, sparse-marker, metadata, and outlet plugins with
exact selectors, LSL clock synchronization, bounded reconnect, and sequence-gap
packet-loss evidence. Current support is `simulated_validated`, not real-hardware
validated; see [`docs/LSL_INTEGRATION.md`](docs/LSL_INTEGRATION.md).

## Compile and execute a suite

The current Python API consumes typed specifications rather than recipe
arguments or mutable runtime dictionaries:

```python
import json
from pathlib import Path

from eegle.compiler import compile_suite
from eegle.plugins import PluginRegistry
from eegle.runtime import ExecutionEngine
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec

fixture = Path("tests/fixtures/migration/phase5_delayed_adaptation")

protocol = ProtocolSpec.from_payload(
    json.loads((fixture / "protocol.json").read_text())
)
suite = SuiteSpec.from_payload(
    json.loads((fixture / "suite.json").read_text())
)
deployment = DeploymentSpec.from_payload(
    json.loads((fixture / "deployment.json").read_text())
)

registry = PluginRegistry()
registry.register_builtins()

compiled = compile_suite(protocol, suite, deployment, registry)
engine = ExecutionEngine.from_plan(compiled.plan, registry)
result = engine.run()

print(compiled.plan.plan_hash)
print(result.status.value)
```

The compiler resolves exact plugins, validates modes, capabilities, resources,
clocks, typed ports, phases, roles, triggers, outcome/adaptation uses, action
capabilities, and artifact dependencies, and locks the resulting graph before
any component is constructed.

Exact built-in template revisions expand through the same authoring lowerer into
ordinary canonical specifications plus portable deployment requirements:

```python
from eegle.authoring import expand_template

expanded = expand_template(
    "eegle.template.continuous_recording",
    "1.0.0",
    draft_id="resting-state",
)

print(expanded.lowered.protocol.spec_hash)
print(expanded.lowered.suite.spec_hash)
print(expanded.expansion_digest)
```

Templates do not compile, construct plugins, select site resources, or execute.
Model templates require an exact external model plugin ID and manifest digest as
parameters; all deployments still pass through normal review and compilation.

The persistent builder exposes the same exact templates without creating a
second specification or runtime API:

```python
from eegle.authoring import ExperimentBuilder

authored = (
    ExperimentBuilder.continuous_recording(draft_id="resting-state")
    .signal(sample_rate_hz=250.0, channel_count=8)
    .build()
)

print(authored.protocol.spec_hash)
print(authored.defaults)
print(authored.requirements.to_payload())
```

For a custom bounded topology, use named compositional authoring instead of
declaring canonical component indexes and routes:

```python
from eegle.authoring import ExperimentDesign, ProcessingStep

authored = (
    ExperimentDesign.create(
        "attention-observer",
        "Observe causal prestimulus EEG.",
    )
    .dense_signal(
        "eeg",
        modality="eeg",
        channels=("Fz", "Cz", "Pz"),
        unit="uV",
        rate_hz=500.0,
    )
    .event_stream("markers", kinds=("stimulus",))
    .processing_chain(
        "clean",
        input="signal.eeg",
        steps=(ProcessingStep("identity", "eegle.processing.identity"),),
    )
    .event_window(
        "prestimulus",
        input="processing.clean",
        event_stream="event.markers",
        event_kind="stimulus",
        start_seconds=-2.0,
        end_seconds=-0.05,
    )
    .record("signal.eeg", "event.markers")
    .build()
)

print(authored.protocol.spec_hash)
print(authored.suite.spec_hash)
print(authored.requirements.to_payload())
```

`ExperimentDesign` is non-executable. It supports named channel-aware signals,
installed-plugin processing chains, exact continuous/event windows, quality
gates, independent models/comparisons, outcomes, adaptation/calibration,
structured policies/actions, phases, recording, and acceptance. `build()`
derives ordinary canonical specs; `compile()` still delegates them unchanged to
the existing compiler. Declaring an action capability never creates a provider
or permission grant, so missing deployment authority remains observe-only.

With the optional extra installed, restricted YAML lowers through that same
builder/template path:

```python
from eegle.authoring import read_yaml_experiment

authored = read_yaml_experiment("experiment.yaml")
print(authored.canonical_json())
```

YAML input is one `eegle.template_authoring.v1` document. It supports only
finite JSON-compatible values and rejects aliases, anchors, merges, tags,
duplicate keys, implicit dates, untyped units, and excessive input resources.
The same restricted parser accepts `eegle.experiment_design.v1` through
`read_yaml_design()`. Typed Python and YAML produce identical canonical hashes;
their source locations remain separate provenance.

Authored values can be explained before deployment or joined to a matching
locked plan after compilation:

```python
from eegle.operations import ExplanationViewKind, explain_authored_experiment

explanation = explain_authored_experiment(authored)
print(explanation.view(ExplanationViewKind.SCIENTIFIC_INTENT).summary)
print(explanation.view(ExplanationViewKind.ACTION_INFLUENCE).summary)
```

The six views cover scientific intent, dataflow, causality, model comparison,
action influence, and defaults/provenance. Difference and diagnostic services
return structured payloads; repair proposals are inspectable records and are
never applied by explanation.

## Core package model

| Package | Responsibility |
|---|---|
| `eegle.authoring` | Provisional non-executable drafts, bounded named designs, exact-version templates, persistent Python/YAML authoring, deterministic lowering/export, deployment requirements, and provenance |
| `eegle.operations` | Provisional project/run services, privacy-aware session projections, replay/replacement comparison, safe export, and shared Python/CLI diagnostics and envelopes |
| `eegle.specs` | Portable protocol/suite intent and site-local deployment |
| `eegle.compiler` | Diagnostics, typed graph, immutable plan, lock, explain/diff |
| `eegle.plugins` | Component descriptors, discovery, capabilities, factories |
| `eegle.streams` | Modality-neutral stream metadata, clocks, dense/sparse packets |
| `eegle.processing` | Causal transforms, windows, and quality components |
| `eegle.models` | Model contracts, manifests, results, deterministic packages, and framework-neutral adapter contracts |
| `eegle.runtime` | Exact construction, admission, routing, queueing, phases, and work evidence |
| `eegle.recording` | Sessions, evidence bundles, artifacts, captures, integrity |
| `eegle.replay` | Capture-backed sources, locked-plan and guarded replacement replay, equivalence comparison |
| `eegle.actions` | Policies, authorization records, commands, and receipts |

## Development verification

Focused Phase 5 checks:

```bash
python -m unittest tests.test_phase5_specs_compiler
python -m unittest tests.test_phase5_plan_execution
python -m unittest tests.test_phase5_execution_semantics
python -m unittest tests.test_phase5_remaining_semantics
python -m unittest tests.test_phase5_packaging
```

Focused Phase 6 model-system checks:

```bash
python -m unittest tests.test_phase6_model_authority
python -m unittest tests.test_phase6_compiler_bindings
python -m unittest tests.test_phase6_model_runtime
python -m unittest tests.test_phase6_role_semantics
python -m unittest tests.test_phase6_outcomes_adaptation
python -m unittest tests.test_phase6_action_authorization
python -m unittest tests.test_phase6_generality
```

Focused Phase 7 authoring and public-boundary checks:

```bash
python -m unittest tests.test_phase7_explanations tests.test_phase7_authoring_surfaces tests.test_phase7_templates tests.test_phase7_draft_lowering
python -m unittest tests.test_phase7_public_boundaries tests.test_source_boundaries
python -m unittest tests.test_phase7_model_packaging
python -m unittest tests.test_phase7_session_experience
```

Complete local verification:

```bash
python -m compileall -q eegle tests
python -m unittest discover -s tests
git diff --check
```

## Architecture and migration authority

- [docs/EEGLE.md](docs/EEGLE.md) defines the v1.0 vision and architecture.
- [docs/MIGRATION.md](docs/MIGRATION.md) defines phase objectives and gates.
- [docs/MIGRATION_STATUS.md](docs/MIGRATION_STATUS.md) tracks live decisions,
  risks, completed work, and next tasks.
- [docs/PHASE5_COMPILER.md](docs/PHASE5_COMPILER.md) records the current compiler
  and runtime boundary.
- [docs/PHASE6_MODEL_SYSTEMS.md](docs/PHASE6_MODEL_SYSTEMS.md) records the completed
  model, role, outcome, adaptation, and authorization boundary.
- [docs/PHASE7_AUTHORING_OPERATIONS.md](docs/PHASE7_AUTHORING_OPERATIONS.md)
  records the active authoring, operations, integration, and product-journey
  work plan.
- [docs/PHASE7_PUBLIC_BOUNDARIES.md](docs/PHASE7_PUBLIC_BOUNDARIES.md) records
  the P7-001 public/provisional/internal surface, payload schemas, diagnostic and
  exit meanings, and optional YAML decision.
- [docs/PHASE3_ENGINE.md](docs/PHASE3_ENGINE.md) preserves accepted execution
  semantics and their mapping into the sole plan-owned engine.

Generated data may contain sensitive neurophysiological or participant
information. `data/` is ignored by Git and must not be used for durable fixtures
or documentation.
