# Model Packaging and Optional Framework Adapters

**Status:** P7-011 implemented
**Date:** 2026-07-28
**Architecture authority:** [EEGLE.md](EEGLE.md)

EEGle model packages transport a canonical `ModelManifest`, its exact artifact
bytes, optional initial state, and portable synthetic test vectors. They do not
transport executable Python, create an environment, install a dependency,
download a model, train a model, or construct a prediction.

The executable adapter remains an installed `eegle.plugins` entry point. The
compiler binds that adapter and a deployment materialization into an immutable
plan. The existing runtime verifies every declared artifact before calling the
adapter, validates its `ModelResult`, and creates the canonical `Prediction`.

## Package contract

The media type is `application/vnd.eegle.model-package+zip`. A v1 package is a
deterministic, uncompressed ZIP with this logical shape:

```text
package.json
artifacts/<sha256-hex>
```

`package.json` uses `eegle.model_package.v1` and contains the canonical,
path-free manifest; logical artifact-ID-to-member mappings; synthetic vectors;
and its own canonical hash. Artifact members are named only by digest. The
reader rejects duplicate, extra, missing, unsafe, or symlink members, index
tampering, artifact size/digest mismatch, and an initial-state envelope whose
model, contract, version, or state-schema identity differs from the manifest.
No content is materialized until all bytes pass those checks.

Materialization writes into a directory named by the package digest and returns
ordinary `ModelArtifactBindingSpec` values for a site-local `DeploymentSpec`.
Existing materializations are reverified rather than overwritten.

## Python workflow

```python
from eegle.models import (
    ModelArtifactSource,
    ModelImplementationRequirement,
    build_model_package,
    check_model_package,
    materialize_model_package,
)
from eegle.plugins import PluginRegistry

source = ModelArtifactSource.from_file(
    "artifact.estimator",
    "model_parameters",
    "application/vnd.example.estimator+binary",
    "estimator.bin",
)

packed = build_model_package(
    "candidate.eegle-model",
    model_id="model.candidate",
    model_version="1.0.0",
    contract=contract,
    implementations=(
        ModelImplementationRequirement("lab.candidate_adapter", "~=1.0"),
    ),
    artifacts=(source,),
    test_vectors=(synthetic_vector,),
)

registry = PluginRegistry()
registry.register_builtins()
registry.load_entry_points()
report = check_model_package(
    packed.package_path,
    registry=registry,
    require_implementation=True,
)
assert report.ready

materialized = materialize_model_package(
    packed.package_path,
    "model-cache",
)
deployment_bindings = materialized.deployment_bindings()
```

`build_model_package()` can also create the canonical `ModelStateArtifact` for
a stateful contract from an explicit finite-JSON initial-state value. The
higher-level builder hashes local sources into a path-free manifest and imports
no model framework.

Synthetic vectors use `eegle.synthetic_model_test_vector.v1`; a CLI vector file
uses an `eegle.synthetic_model_test_vectors.v1` envelope with a `vectors`
array. Inputs are finite JSON keyed by exact contract input ports. Expected
outputs are `ModelResult` payloads keyed by exact output ports and are checked
against the output value, uncertainty, validity, and abstention schemas. These
are portable conformance probes, not inference records; only the runtime may
create a canonical prediction.

## CLI workflow

Given a canonical manifest whose artifacts use logical `artifact://` URIs:

```bash
eegle model pack manifest.json candidate.eegle-model \
  --artifact artifact.estimator=estimator.bin \
  --vectors vectors.json

eegle model check candidate.eegle-model --require-compatible-plugin
```

`model pack` refuses to overwrite its output and requires one exact local
binding for every manifest artifact. Its result uses `eegle.packed_model.v1`.
`model check` verifies the complete
package, validates the vectors, and compares installed descriptor ports,
execution modes, state behavior, version, kind, and construction API with the
manifest. `--no-entry-points` provides a hermetic base-only check.

## Framework adapter and trust boundary

The automated acceptance fixture builds and independently installs a real
scikit-learn adapter wheel. That wheel—not the EEGle base distribution—declares
`scikit-learn`, `joblib`, and NumPy dependencies. It loads an already-created
estimator only from the digest-verified `ModelConstructionContext`, returns a
normal `ModelResult`, and runs through the existing compiler and
`ExecutionEngine`. EEGle has no sklearn engine, estimator registry, training
API, or implicit model download.

Joblib and pickle-family artifacts can execute code while loading. Digest
verification proves identity and tamper resistance, not safety. Admit such a
package only when its bytes and producer are trusted. Prefer a non-executable
framework format when the adapter supports one.

## Replacement replay

`BundleReplayRunner.run_with_model_replacements()` accepts a separately
compiled replacement plan and exact target model component IDs. It requires the
same portable protocol, component set, typed graph, non-target components,
phases, clocks, recording, outcomes, adaptation, authorization, actions,
scheduling, model roles, comparison groups, and preprocessing authority. The
selected model manifest, compatible plugin, configuration, artifact bindings,
and placement may differ while its scientific input/output/state contract must
remain exact.

Captured source packets then enter a fresh normal `ExecutionEngine` built from
the replacement plan. The result is explicitly counterfactual and compared to
the recorded reference as divergence; it never claims the replacement plan is
an equivalent replay of the original. P7-012 owns the higher-level public
`compare_session_models()` and `eegle compare` projections for this low-level
accepted capability. They expose bounded first-divergence evidence, return an
expected counterfactual difference as `diverged`, and leave the recorded
session unchanged.

```bash
eegle compare SESSION candidate-plan.json --replace-model model.primary
```

Add `--bundle-id` when the session contains multiple published runs. With no
selection, the latest registered bundle is used and the choice is recorded as
a warning. `--strict` rejects an unavailable/incomplete comparison but does not
reject the expected `diverged` counterfactual outcome.
