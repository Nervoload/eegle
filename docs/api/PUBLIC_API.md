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
`eegle.integrations.lsl`, and `eegle.operations` are provisional through the
Phase 7 closure review.

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
run  inspect  replay  compare  export  model  plugin
```

The comprehensive `eegle validate` aggregator belongs to Phase 8 and is not a
current command. Existing compiler, preflight, integrity, replay, comparison,
and acceptance results remain available through their owning services.
