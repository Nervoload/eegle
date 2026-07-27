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

Phases 0 through 6 are complete and Phase 7 is ready to begin. The verified v1
foundation provides:

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

The public command-line interface is intentionally absent during the migration.
Phase 7 will add `compile`, `run`, `record`, `replay`, `compare`, `inspect`, and
`validate` commands around the new architecture. Old study commands are not
installed by the pip package. `python -m eegle` returns a dependency-free
message and a nonzero status instead of entering the historical CLI.

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

## Core package model

| Package | Responsibility |
|---|---|
| `eegle.specs` | Portable protocol/suite intent and site-local deployment |
| `eegle.compiler` | Diagnostics, typed graph, immutable plan, lock, explain/diff |
| `eegle.plugins` | Component descriptors, discovery, capabilities, factories |
| `eegle.streams` | Modality-neutral stream metadata, clocks, dense/sparse packets |
| `eegle.processing` | Causal transforms, windows, and quality components |
| `eegle.models` | Model contracts, manifests, results, roles, and dependency-light adapters |
| `eegle.runtime` | Exact construction, admission, routing, queueing, phases, and work evidence |
| `eegle.recording` | Sessions, evidence bundles, artifacts, captures, integrity |
| `eegle.replay` | Capture-backed sources, locked-plan replay, equivalence comparison |
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
- [docs/PHASE3_ENGINE.md](docs/PHASE3_ENGINE.md) preserves accepted execution
  semantics and their mapping into the sole plan-owned engine.

Generated data may contain sensitive neurophysiological or participant
information. `data/` is ignored by Git and must not be used for durable fixtures
or documentation.
