# EEGle

EEGle is an EEG-first, neurophysiology-general Python framework for specifying, compiling, running, recording, replaying, and validating synchronized experimental model systems.

EEG and BCI projects already have strong tools for signal processing, streaming, machine learning, task presentation, benchmarking, and data organization. The difficult part is connecting those tools into a complete experiment that can answer:

- What exactly ran?
- Which samples, events, preprocessing state, and model state were available to each prediction?
- Why were inputs or outputs accepted, rejected, delayed, or skipped?
- Did primary and candidate models receive comparable inputs?
- Did adaptation change the model?
- Was an action requested, authorized, delivered, or rejected?
- Can replay reproduce the live system or locate the first divergence?

EEGle addresses this by compiling portable scientific intent and site-local deployment bindings into an immutable, typed execution plan. The same plan-owned execution model supports simulation, recording, causal model execution, shadow comparison, adaptation, authorized action, evidence capture, replay, and validation.

> **Project status:** EEGle is a pre-alpha, simulation-first candidate under active development. The core compiler, runtime, evidence, replay, authoring, model-package, reference-project, and simulation workflows are implemented. Real EEG acceptance, comprehensive validation, and public-alpha qualification remain in progress.

## Key features

- **Researcher-facing authoring:** exact templates, typed Python builders, bounded compositional `ExperimentDesign`, and optional restricted YAML.
- **Portable experiments:** scientific protocol and suite definitions remain separate from devices, paths, storage, model locations, secrets, and permissions.
- **Typed compilation:** validate plugins, ports, units, channels, rates, windows, clocks, phases, artifacts, models, outcomes, adaptation, and action permissions before construction.
- **Immutable execution:** run only a verified `ExecutionPlan` and `ExecutionLock`, not mutable authoring files.
- **Causal evidence:** preserve what information was available to each prediction and action.
- **Model-system comparison:** primary, shadow, candidate, and observer roles with equivalent-input and coverage evidence.
- **Model packaging:** path-free manifests, content-addressed artifacts, initial state, synthetic conformance vectors, and guarded replacement replay.
- **Preflight and rehearsal:** verify an exact deployment and exercise bounded failure scenarios before live operation.
- **Replay and inspection:** replay captured inputs through the same engine, locate divergence, and inspect privacy-aware session summaries.
- **Observe-only by default:** a policy request cannot become a device command without explicit deployment-owned authorization.

## How EEGle works

```text
Templates / Python / restricted YAML
                ↓
ExperimentDraft or ExperimentDesign
                ↓
ProtocolSpec + SuiteSpec + deployment requirements
                ↓
reviewed DeploymentSpec
                ↓
Compiler
                ↓
ExecutionPlan + ExecutionLock
                ↓
ExecutionEngine
                ↓
EvidenceBundle
                ↓
inspect / replay / compare / structured validation results / export
```

### Core concepts

| Concept | Purpose |
|---|---|
| `ExperimentDesign` | Named, researcher-facing description of signals, events, processing, windows, models, phases, outcomes, and actions |
| `ProtocolSpec` | Scientific claims, execution mode, metrics, and acceptance criteria |
| `SuiteSpec` | Portable streams, components, phases, models, recording, and validation intent |
| `DeploymentSpec` | Local streams, devices, storage, plugins, artifacts, placement, clocks, secrets, and permissions |
| `ExecutionPlan` | Fully resolved and immutable runtime authority |
| `ExecutionLock` | Exact hashes for specifications, graph, plugins, models, and artifacts |
| `EvidenceBundle` | Durable record of admitted inputs, outputs, state, timing, outcomes, actions, integrity, and replay limits |

Custom computation remains an installed plugin. Authoring does not embed arbitrary Python or bypass compilation.

## First simulation

A base installation can create, compile, explain, rehearse, execute, inspect, replay, and export a deterministic recording-only project without hardware:

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

The generated project keeps mutable authoring, canonical specifications, reviewed deployments, content-addressed plans and locks, explanations, and sessions separate. `run` resolves and verifies the compiled plan and lock; it does not execute mutable authoring source.

Create one of the additive compositional preset revisions explicitly:

```bash
eegle new observer --id observer --preset eegle.preset.event_locked_observation --preset-version 2.0.0
```

Six complete generated examples live under [`reference_projects/`](reference_projects/README.md). Model examples use the independently installable plugin in [`examples/plugins/eegle-example-models/`](examples/plugins/eegle-example-models/README.md).

## Python authoring

Use `ExperimentDesign` for a custom bounded topology:

```python
from eegle.authoring import ContractUpdate, ExperimentDesign, ProcessingStep

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
        steps=(
            ProcessingStep(
                "identity",
                "eegle.processing.identity",
                ContractUpdate(),
            ),
        ),
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

`ExperimentDesign` is non-executable. It lowers into ordinary canonical specifications. Compilation remains authoritative, and the runtime accepts only a verified execution plan.

## Ecosystem and anticipated integrations

EEGle is designed to coordinate established scientific packages rather than replace them.

| Package or standard | Expected use with EEGle | Current status |
|---|---|---|
| **MNE-Python** | Loading, preprocessing algorithms, epochs, visualization, statistics, source analysis | Dependency-lazy `DenseSampleBatch` to `RawArray` export bridge shipped; broader workflows pending |
| **Lab Streaming Layer** | Live EEG, markers, metadata, clock synchronization, and outlets | First-party optional adapter; simulated validation complete, real EEG acceptance pending |
| **MNE-LSL** | MNE-oriented live acquisition and processing | Complementary; direct reference integration pending |
| **pyRiemann** | Covariance, tangent-space, Riemannian classifiers, transfer learning | Intended external model adapter and reference project |
| **scikit-learn** | Classical estimators, pipelines, calibration, and metrics | Independently built adapter proof implemented; general packaged adapters remain ecosystem work |
| **PyTorch / Braindecode** | Deep EEG models, training, pretrained and foundation models | Intended external adapters; not part of the base runtime |
| **MOABB** | Public-dataset benchmarking and standardized offline evaluation | Complementary upstream benchmark layer |
| **BIDS / MNE-BIDS** | Dataset organization, metadata, exchange, and archival | Intended import/export and evidence-reference integration |
| **PsychoPy** | Stimulus presentation and behavioral marker generation | External task environment; reference integration pending |
| **MLflow / Weights & Biases** | Model-training runs, aggregate metrics, and checkpoint tracking | Complementary; EEGle focuses on live execution and causal evidence |

EEGle model packages do not install frameworks, train models, download checkpoints, or serialize executable source. The executable adapter is an independently installed plugin that returns a framework-neutral `ModelResult` through the normal runtime.

See the [plugin workflow](docs/PLUGIN_DEVELOPMENT.md), [MNE export bridge](docs/MNE_INTEGRATION.md), and [LSL support boundary](docs/LSL_INTEGRATION.md) for the current integration contracts and support claims.

## Installation

### Anticipated PyPI installation

The intended public installation is:

```bash
python -m pip install eegle
```

Optional capabilities are expected to remain explicit:

```bash
# LSL live-stream integration
python -m pip install "eegle[live]"

# Restricted YAML authoring
python -m pip install "eegle[yaml]"

# Offline analysis dependencies such as MNE, pandas, and plotting
python -m pip install "eegle[analysis]"
```

### Contributor source installation

Until a public PyPI release is published, editable installation is a
contributor workflow and exposes the checkout. Release acceptance uses built
wheel and sdist artifacts in a clean environment.

```bash
git clone https://github.com/Nervoload/eegle.git
cd eegle

python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Install current optional extras from source as needed:

```bash
python -m pip install -e ".[live]"
python -m pip install -e ".[yaml]"
python -m pip install -e ".[analysis]"
```

## Device support

EEGle is device-agnostic at the portable suite level. Hardware is bound through a reviewed `DeploymentSpec` and executable plugins.

Current intended live path:

- Generic EEG, marker, and metadata streams through Lab Streaming Layer
- Exact stream selectors rather than best-effort matching
- Explicit channels, units, rates, clocks, reconnect behavior, and packet-loss evidence
- Observe-only operation when action authorization is absent

Current support claim:

- **Simulation:** validated through automated tests
- **LSL integration:** simulated-network validation complete
- **Real EEG hardware:** acceptance pending; no broad live-hardware claim yet
- **Vendor-specific amplifiers:** use LSL where available; direct vendor SDK support requires independent plugins and retained acceptance evidence
- **Robots, stimulation devices, and physical actuators:** plugin-based bounded high-level requests only; device safety controllers, watchdogs, emergency stops, and certification remain external

## Python and platform support

- **Python:** 3.11 or newer
- **Current automated matrix:** Linux on Python 3.11-3.13; macOS and Windows on Python 3.12
- **Base dependencies:** NumPy, SciPy, jsonschema, and packaging
- **Optional dependencies:** pylsl, MNE, pandas, matplotlib, PsychoPy, specparam, ruamel.yaml, and external model frameworks as installed by their adapter packages

Support claims remain evidence-based. An importable dependency or representable configuration does not by itself imply validated hardware, framework, model, or modality support.

## Scope and safety

EEGle is not a replacement for:

- MNE, pyRiemann, Braindecode, MOABB, or model-training frameworks
- a low-level robot, prosthetic, or stimulation controller
- institutional safety review, clinical validation, regulatory approval, or device certification
- BIDS or another archival raw-data standard
- a general arbitrary workflow engine

EEGle should normally emit bounded, high-level action intent. Independent deployment authorization and the device controller remain responsible for physical safety.

## Development status and next milestones

The immediate milestones are:

1. Complete a retained real EEG observe-only LSL acceptance run.
2. Close Phase 7 against its gate-to-evidence matrix and stabilize the provisional authoring/operations surface.
3. Add broader pyRiemann/scikit-learn, BIDS, and PyTorch/Braindecode integration paths.
4. Expand scientific validation, performance budgets, reports, and fault qualification.

The product rule is:

> Authoring may be convenient, but execution remains explicit, locked, and evidence-producing.

## License

EEGle is released under the MIT License.
