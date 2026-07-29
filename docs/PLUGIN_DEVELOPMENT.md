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
eegle detect
eegle compile reference_projects/03-model-comparison
eegle rehearse reference_projects/03-model-comparison
```

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
