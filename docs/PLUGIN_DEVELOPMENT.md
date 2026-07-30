# Plugin development

EEGle plugins are independently installed distributions discovered through the
`eegle.plugins` entry-point group. A plugin owns executable behavior and its
descriptor contract; authoring names the plugin but never embeds code, imports,
or runtime objects.

The smallest distribution contains a normal `pyproject.toml` entry point:

```toml
[project.entry-points."eegle.plugins"]
my-components = "my_package:plugin_descriptors"
```

`plugin_descriptors()` returns immutable `PluginDescriptor` values. Each value
declares exact input/output ports, JSON configuration schema, supported modes,
determinism, state behavior, construction API, and implementation provenance.
Transforms must additionally publish a `ContractTransformSpec`; compilation
rejects a processing step whose authored output contract is not exactly the
plugin-attested result.

Model plugins return `ModelResult`. EEGle—not the plugin—constructs prediction
identity, timing, role, lineage, and evidence. Inference metadata must remain
label-blind: stimulus condition, response correctness, and training labels are
not model inputs. Artifact-backed plugins use `MODEL_CONTEXT_V1` and read only
digest-verified `ModelConstructionContext` materializations.

Use this repository’s
`examples/plugins/eegle-example-models` distribution as the executable minimal
example. It publishes mean and peak algorithms as genuinely different
stateless plugins plus a snapshot/restore-capable adaptive mean plugin. The
corresponding path-free manifests come from
`eegle.authoring.reference_model_manifests()` and can be transported with
`eegle model pack` without embedding this Python implementation.
Install it, then verify discovery and a reference project:

```bash
python -m pip install ./examples/plugins/eegle-example-models
eegle plugin inspect eegle.example_models.mean_threshold
eegle plugin check eegle.example_models.mean_threshold --construct
eegle compile reference_projects/03-model-comparison
eegle rehearse reference_projects/03-model-comparison
```

`eegle plugin inspect` and the default `eegle plugin check` read canonical
descriptors without invoking component factories. `--construct` is an explicit
factory call and performs structural checks; configurations are accepted only
with that flag. Programmatic tests can supply typed behavioral cases to the
reusable harness:

```python
from eegle.plugins import PluginExercise, check_plugin_conformance

report = check_plugin_conformance(
    descriptor,
    config={},
    construct=True,
    exercises=(
        PluginExercise(
            "valid",
            lambda component: component.predict(window, context),
            expected_abstained=False,
        ),
    ),
)
assert report.ready
```

For lifecycle components, also pass an explicit `ExecutionContext`; the
harness checks start/stop symmetry and attempts cleanup after an exercise
failure. Stateful descriptors are reconstructed, restored from a finite JSON
snapshot, and exercised again. A deterministic plugin fails conformance when
the canonical result hashes differ across that fresh-component replay.

Model fixtures should include valid, partially invalid, and all-invalid
windows. All-invalid inputs must return a finite `ModelResult` with
`abstained=True` and a stable reason such as `all_samples_invalid`; they must
not depend on empty-array reductions or emit NaN/Infinity.

Recommended acceptance for a plugin distribution:

1. build and install its wheel in a clean environment containing the EEGle
   wheel;
2. load entry points and verify the descriptor identity/version;
3. compile a portable suite and explicit deployment without repository imports;
4. execute a synthetic fixture through `ExecutionEngine`;
5. persist and replay the evidence bundle;
6. test missing dependency, contract mismatch, malformed output, and state
   restoration as structured failures.

Plugins must not install dependencies dynamically, download model artifacts
implicitly, create a second scheduler, or grant action authority.
