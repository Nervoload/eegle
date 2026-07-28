import sys

sys.path.insert(0, __TARGET__)  # noqa: F821 - replaced by the wheel test

from pathlib import Path
import tempfile

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression

from eegle._domain import EquivalenceLevel, ExecutionMode
from eegle.compiler import compile_suite
from eegle.models import (
    ModelArtifactSource,
    ModelContract,
    ModelImplementationRequirement,
    ModelInputContract,
    ModelOutputContract,
    ModelResult,
    ModelStateContract,
    SyntheticModelTestVector,
    build_model_package,
    check_model_package,
    materialize_model_package,
)
from eegle.plugins import PluginRegistry
from eegle.runtime import EngineStatus, ExecutionEngine, GraphInput
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec
from eegle.streams import TimePoint
from phase7_sklearn_model import FEATURE_VECTOR_SCHEMA, FeatureVector


with tempfile.TemporaryDirectory() as temporary_directory:
    root = Path(temporary_directory)
    estimator_path = root / "estimator.joblib"
    estimator = LogisticRegression()
    estimator.classes_ = np.asarray([0, 1])
    estimator.coef_ = np.asarray([[2.0, -1.0]], dtype=np.float64)
    estimator.intercept_ = np.asarray([0.25], dtype=np.float64)
    estimator.n_features_in_ = 2
    estimator.n_iter_ = np.asarray([1], dtype=np.int32)
    joblib.dump(estimator, estimator_path)

    contract = ModelContract(
        inputs=(ModelInputContract("features", FEATURE_VECTOR_SCHEMA),),
        outputs=(
            ModelOutputContract(
                "prediction",
                "eegle.prediction.v2",
                value_schema={
                    "type": "object",
                    "required": ["label", "probabilities"],
                    "properties": {
                        "label": {"type": "integer"},
                        "probabilities": {
                            "type": "array",
                            "items": {"type": "number"},
                        },
                    },
                    "additionalProperties": False,
                },
            ),
        ),
        state=ModelStateContract(replay_equivalence=EquivalenceLevel.NUMERIC),
        supported_modes=frozenset({ExecutionMode.CAUSAL}),
    )
    source = ModelArtifactSource.from_file(
        "artifact.estimator",
        "model_parameters",
        "application/vnd.scikit-learn.estimator+joblib",
        estimator_path,
    )
    probe = np.asarray([[0.5, -0.25]], dtype=np.float64)
    probabilities = [float(value) for value in estimator.predict_proba(probe)[0]]
    vector = SyntheticModelTestVector(
        "vector.positive",
        inputs={"features": {"values": [0.5, -0.25]}},
        expected_outputs={
            "prediction": ModelResult(
                {"label": int(estimator.predict(probe)[0]), "probabilities": probabilities}
            )
        },
    )
    package = build_model_package(
        root / "estimator.eegle-model",
        model_id="model.phase7-sklearn",
        model_version="1.0.0",
        contract=contract,
        implementations=(
            ModelImplementationRequirement(
                "fixture.phase7.sklearn_estimator", "~=1.0"
            ),
        ),
        artifacts=(source,),
        test_vectors=(vector,),
        annotations={"framework": "scikit-learn", "training_owned_by": "external"},
    )

    registry = PluginRegistry()
    loaded = registry.load_entry_points()
    assert "fixture.phase7.sklearn_estimator" in loaded, loaded
    check = check_model_package(
        package.package_path,
        registry=registry,
        require_implementation=True,
    )
    assert check.ready, check.issues
    assert check.compatible_plugins == ("fixture.phase7.sklearn_estimator",)
    materialized = materialize_model_package(
        package.package_path, root / "materialized"
    )
    manifest = materialized.index.manifest

    protocol = ProtocolSpec.from_payload(
        {
            "schema": "eegle.protocol_spec.v1",
            "protocol_id": "protocol.phase7-sklearn",
            "execution_mode": "causal",
            "claims": [
                {
                    "claim_id": "claim.dependency-backed-adapter",
                    "statement": "A packaged estimator runs through the model runtime.",
                }
            ],
            "metrics": [],
            "acceptance": [],
            "annotations": {},
        }
    )
    suite = SuiteSpec.from_payload(
        {
            "schema": "eegle.suite_spec.v1",
            "suite_id": "suite.phase7-sklearn",
            "protocol_id": "protocol.phase7-sklearn",
            "streams": [],
            "components": [
                {
                    "component_id": "model.sklearn",
                    "kind": "model",
                    "plugin_id": "fixture.phase7.sklearn_estimator",
                    "version_spec": "~=1.0",
                    "config": {"artifact_id": "artifact.estimator"},
                }
            ],
            "routes": [],
            "phases": [
                {
                    "phase_id": "phase.run",
                    "components": ["model.sklearn"],
                    "transitions": [],
                }
            ],
            "initial_phase": "phase.run",
            "artifacts": [],
            "model_roles": [],
            "model_uses": [
                {
                    "component_id": "model.sklearn",
                    "manifest_digest": manifest.manifest_digest,
                    "role_id": "primary",
                    "comparison_group": None,
                }
            ],
            "scheduling": {},
            "scheduled_triggers": [],
            "state_triggers": [],
            "clock_policy": {"execution_clock_id": "boundary.clock"},
            "recording": {},
            "validation": {},
        }
    )
    deployment = DeploymentSpec.from_payload(
        {
            "schema": "eegle.deployment_spec.v1",
            "deployment_id": "deployment.phase7-sklearn",
            "suite_id": "suite.phase7-sklearn",
            "component_bindings": [],
            "resources": [],
            "stream_bindings": [],
            "storage": [],
            "permissions": [],
            "secrets": [],
            "clock_mappings": [],
            "model_artifacts": [
                value.to_payload() for value in materialized.deployment_bindings()
            ],
        }
    )
    compiled = compile_suite(
        protocol,
        suite,
        deployment,
        registry,
        model_manifests={manifest.manifest_digest: manifest},
    )
    run = ExecutionEngine.from_plan(compiled.plan, registry).run(
        inputs_by_phase={
            "phase.run": (
                GraphInput(
                    "model.sklearn",
                    "features",
                    FeatureVector(
                        "features.one",
                        (0.5, -0.25),
                        TimePoint(1.0, "boundary.clock"),
                    ),
                ),
            )
        }
    )
    assert run.status == EngineStatus.COMPLETE, run.failure
    prediction = run.phase_results[0].emissions_from(
        "model.sklearn", "prediction"
    )[0]
    assert prediction.value["label"] == vector.expected_outputs["prediction"].value["label"]
    assert dict(prediction.artifact_digests) == {
        "artifact.estimator": manifest.artifacts[0].digest
    }
    assert prediction.plugin_id == "fixture.phase7.sklearn_estimator"
    assert registry.resolve("fixture.phase7.sklearn_estimator").distribution == (
        "eegle-phase7-sklearn-fixture"
    )

print("phase7-sklearn-adapter-ok")
