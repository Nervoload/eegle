# EEGle public API

This document describes the pre-alpha API shipped by the current EEGle wheel.
Package-level exports are authoritative; importable implementation submodules
and source-only migration evidence are not public merely because a checkout
contains them. The machine inventory lives in
[`docs/migration/phase7_public_surface.json`](../migration/phase7_public_surface.json).

## Stability boundary

The package-level exports of these modules are the stable-alpha low-level
surface. “Stable-alpha” requires an explicit migration decision for breaking
changes; it is not a 1.0 compatibility promise.

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

The package-level exports of `eegle.authoring`, `eegle.integrations`,
`eegle.integrations.lsl`, `eegle.operations`, and `eegle.validation` are
provisional while their remaining external and Phase 8 gates are open.

## Author, compile, and run

Researcher-facing authoring lowers into the canonical specification and
compiler boundary:

```python
from eegle.authoring import ExperimentDesign
from eegle.operations import compile_project, create_project, run_project

project = create_project(
    "first-simulation",
    project_id="first-simulation",
)
compiled = compile_project(project.root)
run = run_project(project.root, session_id="session.first.run")
```

Advanced callers may author `ProtocolSpec`, `SuiteSpec`, and `DeploymentSpec`
directly and call `eegle.compiler.compile_suite`. Runtime construction accepts
only a verified `ExecutionPlan`; mutable authoring values and project manifests
are never runtime inputs.

## Evidence and replay

`eegle.recording` owns sessions, evidence bundles, integrity, stores, recovery,
and privacy/export primitives. `eegle.replay` owns same-engine replay and
divergence results. The provisional operations projections provide concise,
read-only workflows:

```python
from eegle.operations import inspect_session, replay_session

inspection = inspect_session("first-simulation/sessions/session.first.run")
replay = replay_session("first-simulation/sessions/session.first.run")
```

Inspection, comparison, replay, and export do not signal workers or mutate,
recover, truncate, overwrite, or delete their source evidence.

Application supervisors may pass an `evidence_resume_token` to `run_project`
or `run_locked_plan` and retain it outside EEGle. A hard interruption then
leaves a durably appended writer that can be authorized through
`EvidenceWriter.resume`; EEGle never persists the recovery secret.
Without a caller-supplied secret, an interactive interruption publishes the
available evidence as a partial bundle.

## Structured validation

Structured validation belongs to Phase 8. The first vertical slice makes
correctness results explicit without claiming the later scientific and scale
qualification gates.

`eegle.validation` owns the versioned status, severity, observation, evidence-
reference, result, and report contracts. The operations adapter aggregates the
existing compiler, lock, bundle-integrity, semantic-evidence, runtime, and
replay authorities without creating a second execution or validation engine:

```python
from eegle.operations import validate_target

report = validate_target("first-simulation")
print(report.status.value, report.report_hash)
```

Missing denominators or required observations report
`insufficient_evidence`; they do not become successful zero-valued metrics.
Validation is read-only. `eegle validate TARGET --strict` returns a non-zero
exit for failure or insufficient evidence, while the default command always
returns the structured report.

## Research integrations

The provisional `eegle.integrations` surface includes dependency-lazy MNE
adapters for dense raw export, sparse marker annotations, admitted-window
epochs, and explicit-clock replay inputs. Every adapter returns a sidecar for
the EEGle clock, availability, identity, and lineage facts that its MNE object
cannot represent.

`research_integration_support_matrix()` exposes the packaged, hash-verified
`eegle.research_integration_support.v1` support record. Its capability rows
distinguish `representable`, `adapter_available`, `validated`, and
`reference_supported`; callers must not infer one dimension from another.

## Models and plugins

`eegle.models` owns framework-neutral contracts, manifests, model packages,
results, predictions, state artifacts, and package verification. Executable
behavior belongs to descriptors discovered through `eegle.plugins`:

```python
from eegle.operations import check_plugin, inspect_plugins
from eegle.plugins import PluginRegistry

inspection = inspect_plugins("eegle.processing.identity")
static_check = check_plugin("eegle.processing.identity")
```

Descriptor inspection never invokes a component factory. Construction and
behavioral conformance are explicit through `check_plugin(..., construct=True)`
or the reusable `eegle.plugins.check_plugin_conformance` harness. Model plugins
return `ModelResult`; EEGle adds prediction identity, timing, role, lineage, and
evidence only after validating that result against the locked model contract.
The matching installed commands are `eegle plugin inspect` and
`eegle plugin check`; component construction requires `--construct`.

## Command surface

The installed `eegle` command and `python -m eegle` share the operations
services. Current families are:

```text
new  detect  compile  explain  diff  graph  preflight  rehearse
run  inspect  replay  compare  validate  export  model  plugin
```

The current `validate` command is the first Phase 8 vertical slice. The
provisional `eegle.validation` package also exposes the typed, hash-verified
performance/fault qualification profile shipped with the artifact. Broader
scientific performance, calibration, high-channel/sparse scale, and
multi-process qualification remain open; the command reports absent evidence
explicitly rather than claiming those gates have passed.
