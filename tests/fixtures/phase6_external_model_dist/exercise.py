import sys

sys.path.insert(0, __TARGET__)

from eegle._domain import EquivalenceLevel, ExecutionMode
from eegle.compiler import compile_suite
from eegle.compiler.lock import canonical_hash
from eegle.models import (
    ModelContract,
    ModelImplementationRequirement,
    ModelInputContract,
    ModelManifest,
    ModelOutputContract,
    ModelStateBehavior,
    ModelStateContract,
)
from eegle.plugins import PluginRegistry
from eegle.runtime import ExecutionEngine, GraphInput
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec
from eegle.streams import TimePoint
from phase6_external_model import OBSERVATION_SCHEMA, Observation

registry = PluginRegistry()
loaded = registry.load_entry_points()
assert "fixture.external.stateful_model" in loaded, loaded

manifest = ModelManifest(
    model_id="model.external-accumulator",
    model_version="1.0.0",
    contract=ModelContract(
        inputs=(ModelInputContract("observation", OBSERVATION_SCHEMA),),
        outputs=(
            ModelOutputContract(
                "prediction",
                "eegle.prediction.v2",
                value_schema={
                    "type": "object",
                    "required": ["total", "count"],
                    "properties": {
                        "total": {"type": "number"},
                        "count": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
                validity_schema={
                    "type": "object",
                    "required": ["valid"],
                    "properties": {"valid": {"type": "boolean"}},
                    "additionalProperties": False,
                },
            ),
        ),
        state=ModelStateContract(
            behavior=ModelStateBehavior.SNAPSHOT_RESTORE,
            state_schema_id="fixture.phase6_accumulator_state.v1",
            state_affects_predictions=True,
            replay_equivalence=EquivalenceLevel.BITWISE,
        ),
        supported_modes=frozenset({ExecutionMode.CAUSAL}),
    ),
    artifacts=(),
    implementations=(
        ModelImplementationRequirement("fixture.external.stateful_model", "~=1.0"),
    ),
)
protocol = ProtocolSpec.from_payload(
    {
        "schema": "eegle.protocol_spec.v1",
        "protocol_id": "protocol.external-stateful",
        "execution_mode": "causal",
        "claims": [
            {
                "claim_id": "claim.external-state",
                "statement": "External state is snapshot-restorable.",
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
        "suite_id": "suite.external-stateful",
        "protocol_id": "protocol.external-stateful",
        "streams": [],
        "components": [
            {
                "component_id": "model.external",
                "kind": "model",
                "plugin_id": "fixture.external.stateful_model",
                "version_spec": "~=1.0",
                "role": "primary",
                "config": {"initial_total": 2.0},
            }
        ],
        "routes": [],
        "phases": [
            {
                "phase_id": "phase.run",
                "components": ["model.external"],
                "transitions": [],
            }
        ],
        "initial_phase": "phase.run",
        "artifacts": [],
        "model_roles": [],
        "model_uses": [
            {
                "component_id": "model.external",
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
        "deployment_id": "deployment.external-stateful",
        "suite_id": "suite.external-stateful",
        "component_bindings": [],
        "resources": [],
        "stream_bindings": [],
        "storage": [],
        "permissions": [],
        "secrets": [],
        "clock_mappings": [],
        "model_artifacts": [],
    }
)
compiled = compile_suite(
    protocol,
    suite,
    deployment,
    registry,
    model_manifests={manifest.manifest_digest: manifest},
)
inputs = {
    "phase.run": (
        GraphInput(
            "model.external",
            "observation",
            Observation("observation.one", 3.0, TimePoint(1.0, "boundary.clock")),
        ),
        GraphInput(
            "model.external",
            "observation",
            Observation("observation.two", 4.0, TimePoint(2.0, "boundary.clock")),
        ),
    )
}
engine = ExecutionEngine.from_plan(compiled.plan, registry)
run = engine.run(inputs_by_phase=inputs)
predictions = run.phase_results[0].emissions_from("model.external", "prediction")
assert [dict(value.value) for value in predictions] == [
    {"total": 5.0, "count": 1},
    {"total": 9.0, "count": 2},
]
assert predictions[0].state_digest == canonical_hash(
    {"schema": "fixture.phase6_accumulator_state.v1", "total": 2.0, "count": 0}
)
assert predictions[1].state_digest == canonical_hash(
    {"schema": "fixture.phase6_accumulator_state.v1", "total": 5.0, "count": 1}
)
snapshot = engine.runtime.snapshot_state()
fresh = ExecutionEngine.from_plan(compiled.plan, registry)
fresh.runtime.restore_state(snapshot)
assert fresh.runtime.snapshot_state().snapshot_hash == snapshot.snapshot_hash
restored = fresh.run(
    inputs_by_phase={
        "phase.run": (
            GraphInput(
                "model.external",
                "observation",
                Observation("observation.three", 1.0, TimePoint(3.0, "boundary.clock")),
            ),
        )
    }
)
third = restored.phase_results[0].emissions_from("model.external", "prediction")[0]
assert dict(third.value) == {"total": 10.0, "count": 3}
assert third.state_digest == canonical_hash(
    {"schema": "fixture.phase6_accumulator_state.v1", "total": 9.0, "count": 2}
)
assert registry.resolve("fixture.external.stateful_model").distribution == (
    "eegle-phase6-external-model-fixture"
)
print("phase6-external-stateful-ok")
