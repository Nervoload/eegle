from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
import unittest

from eegle._domain import (
    ComponentKind,
    Determinism,
    EquivalenceLevel,
    ExecutionMode,
    Lineage,
)
from eegle.compiler import canonical_hash, compile_suite
from eegle.compiler.diagnostics import CompilationError
from eegle.models import (
    ModelContract,
    ModelImplementationRequirement,
    ModelInputContract,
    ModelManifest,
    ModelOutputContract,
    ModelResult,
    ModelStateContract,
)
from eegle.models.predictions import PREDICTION_RECORD_SCHEMA, Prediction
from eegle.plugins import (
    PluginCapabilities,
    PluginDescriptor,
    PluginRegistry,
    PortSpec,
    StateBehavior,
)
from eegle.runtime import ExecutionEngine, GraphInput, ModelComparison
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec
from eegle.streams import TimePoint


OBSERVATION_SCHEMA = "fixture.phase6_role_observation.v1"
EMPTY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
}
MODEL_CONFIG_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
        "offset": {"type": "number"},
        "fail": {"type": "boolean"},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class Observation:
    record_id: str
    value: float
    available_time: TimePoint
    schema: str = OBSERVATION_SCHEMA

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "record_id": self.record_id,
            "value": self.value,
            "available_time": self.available_time.to_payload(),
        }


class RoleModel:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.offset = float(config.get("offset", 0.0))
        self.fail = bool(config.get("fail", False))

    def predict(self, item: Observation, context: Any) -> ModelResult:
        if self.fail:
            raise RuntimeError("deliberate role-local model failure")
        return ModelResult({"score": item.value + self.offset})


class ObservePolicy:
    def decide(self, prediction: Prediction, state: dict[str, Any], context: Any) -> None:
        state["last_prediction_id"] = prediction.prediction_id
        return None


def _capabilities() -> PluginCapabilities:
    return PluginCapabilities(
        supported_modes=frozenset({ExecutionMode.CAUSAL}),
        determinism=Determinism.DETERMINISTIC,
        equivalence=EquivalenceLevel.BITWISE,
        state_behavior=StateBehavior.STATELESS,
    )


def _registry(*, policy: bool = False) -> PluginRegistry:
    registry = PluginRegistry()
    registry.register(
        PluginDescriptor(
            plugin_id="fixture.model.roles",
            version="1.0.0",
            kind=ComponentKind.MODEL,
            config_schema=MODEL_CONFIG_SCHEMA,
            input_ports=(
                PortSpec("observation", OBSERVATION_SCHEMA, required=False),
            ),
            output_ports=(PortSpec("prediction", PREDICTION_RECORD_SCHEMA),),
            capabilities=_capabilities(),
            factory=RoleModel,
            implementation="tests.phase6_roles:RoleModel",
            distribution="phase6-role-fixture",
        )
    )
    if policy:
        registry.register(
            PluginDescriptor(
                plugin_id="fixture.policy.observe",
                version="1.0.0",
                kind=ComponentKind.POLICY,
                config_schema=EMPTY_SCHEMA,
                input_ports=(
                    PortSpec("prediction", PREDICTION_RECORD_SCHEMA, required=False),
                ),
                output_ports=(),
                capabilities=_capabilities(),
                factory=lambda config: ObservePolicy(),
                implementation="tests.phase6_roles:ObservePolicy",
                distribution="phase6-role-fixture",
            )
        )
    return registry


def _manifest() -> ModelManifest:
    return ModelManifest(
        model_id="model.role-fixture",
        model_version="1.0.0",
        contract=ModelContract(
            inputs=(ModelInputContract("observation", OBSERVATION_SCHEMA),),
            outputs=(
                ModelOutputContract(
                    "prediction",
                    PREDICTION_RECORD_SCHEMA,
                    value_schema={
                        "type": "object",
                        "required": ["score"],
                        "properties": {"score": {"type": "number"}},
                        "additionalProperties": False,
                    },
                ),
            ),
            state=ModelStateContract(),
        ),
        artifacts=(),
        implementations=(
            ModelImplementationRequirement("fixture.model.roles", "~=1.0"),
        ),
    )


def _protocol() -> ProtocolSpec:
    return ProtocolSpec.from_payload(
        {
            "schema": "eegle.protocol_spec.v1",
            "protocol_id": "protocol.phase6-roles",
            "execution_mode": "causal",
            "claims": [
                {
                    "claim_id": "claim.role-isolation",
                    "statement": "Compiled role permissions govern model execution.",
                }
            ],
            "metrics": [],
            "acceptance": [],
            "annotations": {},
        }
    )


def _deployment() -> DeploymentSpec:
    return DeploymentSpec.from_payload(
        {
            "schema": "eegle.deployment_spec.v1",
            "deployment_id": "deployment.phase6-roles",
            "suite_id": "suite.phase6-roles",
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


def _suite_payload(
    manifest: ModelManifest,
    models: tuple[Mapping[str, Any], ...],
    *,
    custom_roles: tuple[Mapping[str, Any], ...] = (),
    policy_source: str | None = None,
) -> dict[str, Any]:
    components = [
        {
            "component_id": str(model["component_id"]),
            "kind": "model",
            "plugin_id": "fixture.model.roles",
            "version_spec": "~=1.0",
            "config": dict(model.get("config") or {}),
        }
        for model in models
    ]
    routes: list[dict[str, Any]] = []
    if policy_source is not None:
        components.append(
            {
                "component_id": "policy.observe",
                "kind": "policy",
                "plugin_id": "fixture.policy.observe",
                "version_spec": "~=1.0",
                "config": {},
            }
        )
        routes.append(
            {
                "route_id": "route.model-policy",
                "source": {"component": policy_source, "port": "prediction"},
                "target": {"component": "policy.observe", "port": "prediction"},
            }
        )
    return {
        "schema": "eegle.suite_spec.v1",
        "suite_id": "suite.phase6-roles",
        "protocol_id": "protocol.phase6-roles",
        "streams": [],
        "components": components,
        "routes": routes,
        "phases": [
            {
                "phase_id": "phase.run",
                "components": [value["component_id"] for value in components],
                "transitions": [],
            }
        ],
        "initial_phase": "phase.run",
        "artifacts": [],
        "model_roles": list(custom_roles),
        "model_uses": [
            {
                "component_id": str(model["component_id"]),
                "manifest_digest": manifest.manifest_digest,
                "role_id": str(model["role_id"]),
                "comparison_group": model.get("comparison_group"),
            }
            for model in models
        ],
        "scheduling": {},
        "scheduled_triggers": [],
        "state_triggers": [],
        "clock_policy": {"execution_clock_id": "boundary.clock"},
        "recording": {},
        "validation": {},
    }


def _compile(
    models: tuple[Mapping[str, Any], ...],
    *,
    custom_roles: tuple[Mapping[str, Any], ...] = (),
    policy_source: str | None = None,
):
    manifest = _manifest()
    registry = _registry(policy=policy_source is not None)
    suite = SuiteSpec.from_payload(
        _suite_payload(
            manifest,
            models,
            custom_roles=custom_roles,
            policy_source=policy_source,
        )
    )
    compiled = compile_suite(
        _protocol(),
        suite,
        _deployment(),
        registry,
        model_manifests={manifest.manifest_digest: manifest},
    )
    return compiled, registry


def _input(component_id: str, record_id: str, value: float) -> GraphInput:
    return GraphInput(
        component_id,
        "observation",
        Observation(record_id, value, TimePoint(0.0, "boundary.clock")),
    )


class Phase6RoleSemanticTests(unittest.TestCase):
    def test_compiled_permissions_drive_all_builtin_role_priorities(self) -> None:
        models = (
            {"component_id": "model.observer", "role_id": "observer"},
            {
                "component_id": "model.shadow",
                "role_id": "shadow",
                "comparison_group": "comparison.reference",
            },
            {
                "component_id": "model.candidate",
                "role_id": "candidate",
                "comparison_group": "comparison.reference",
            },
            {
                "component_id": "model.primary",
                "role_id": "primary",
                "comparison_group": "comparison.reference",
            },
        )
        compiled, registry = _compile(models)
        run = ExecutionEngine.from_plan(compiled.plan, registry).run(
            inputs_by_phase={
                "phase.run": tuple(
                    _input(str(model["component_id"]), "observation.shared", 3.0)
                    for model in models
                )
            }
        )

        prediction_order = [
            value.component_id
            for value in run.phase_results[0].emissions
            if isinstance(value.value, Prediction)
        ]
        self.assertEqual(
            prediction_order,
            [
                "model.primary",
                "model.candidate",
                "model.shadow",
                "model.observer",
            ],
        )
        self.assertEqual(
            {value.component_id: value.role.scheduling_priority for value in compiled.plan.model_bindings},
            {
                "model.observer": 200,
                "model.shadow": 100,
                "model.candidate": 50,
                "model.primary": 0,
            },
        )
        comparison = ModelComparison.from_payload(
            next(
                value.payload
                for value in run.evidence
                if value.record_type == "model_comparison"
            )
        )
        self.assertEqual(comparison.status.value, "complete")
        self.assertTrue(comparison.outputs_equal)

    def test_role_queue_dispositions_are_terminal_and_comparisons_are_accounted(self) -> None:
        models = (
            {
                "component_id": "model.primary",
                "role_id": "primary",
                "comparison_group": "comparison.queue",
            },
            {
                "component_id": "model.shadow",
                "role_id": "shadow",
                "comparison_group": "comparison.queue",
            },
        )
        compiled, registry = _compile(models)
        run = ExecutionEngine.from_plan(compiled.plan, registry).run(
            inputs_by_phase={
                "phase.run": (
                    _input("model.primary", "observation.one", 1.0),
                    _input("model.shadow", "observation.one", 1.0),
                    _input("model.primary", "observation.two", 2.0),
                    _input("model.shadow", "observation.two", 2.0),
                )
            }
        )

        skips = [
            value.payload
            for value in run.evidence
            if value.record_type == "work" and value.payload["status"] == "skipped"
        ]
        self.assertEqual(
            [(value["component_id"], value["reason_code"]) for value in skips],
            [("model.shadow", "model_role_queue_limit")],
        )
        comparisons = [
            ModelComparison.from_payload(value.payload)
            for value in run.evidence
            if value.record_type == "model_comparison"
        ]
        self.assertEqual(
            [value.status.value for value in comparisons],
            ["complete", "incomplete"],
        )
        self.assertEqual(comparisons[1].missing_members, ("model.shadow",))
        self.assertEqual(comparisons[1].reason_code, "phase_complete")

        observer, observer_registry = _compile(
            ({"component_id": "model.observer", "role_id": "observer"},)
        )
        observer_run = ExecutionEngine.from_plan(observer.plan, observer_registry).run(
            inputs_by_phase={
                "phase.run": (
                    _input("model.observer", "observation.old", 1.0),
                    _input("model.observer", "observation.new", 2.0),
                )
            }
        )
        predictions = observer_run.phase_results[0].emissions_from(
            "model.observer", "prediction"
        )
        self.assertEqual([value.input_ids for value in predictions], [("observation.new",)])
        shed = [
            value.payload
            for value in observer_run.evidence
            if value.record_type == "work"
            and value.payload.get("reason_code") == "model_role_shed_oldest"
        ]
        self.assertEqual(
            [tuple(value["input_ids"]) for value in shed],
            [("observation.old",)],
        )

    def test_role_local_failure_cannot_take_down_the_primary(self) -> None:
        models = (
            {"component_id": "model.primary", "role_id": "primary"},
            {
                "component_id": "model.observer",
                "role_id": "observer",
                "config": {"fail": True},
            },
        )
        compiled, registry = _compile(models)
        run = ExecutionEngine.from_plan(compiled.plan, registry).run(
            inputs_by_phase={
                "phase.run": (
                    _input("model.primary", "observation.primary", 1.0),
                    _input("model.observer", "observation.observer", 1.0),
                )
            }
        )
        self.assertEqual(run.status.value, "complete")
        self.assertEqual(
            len(run.phase_results[0].emissions_from("model.primary", "prediction")),
            1,
        )
        disposition = next(
            value.payload
            for value in run.evidence
            if value.record_type == "model_role_failure_disposition"
        )
        self.assertEqual(disposition["role_id"], "observer")
        self.assertEqual(disposition["disposition"], "reject_result")

        failed, failed_registry = _compile(
            (
                {
                    "component_id": "model.primary",
                    "role_id": "primary",
                    "config": {"fail": True},
                },
            )
        )
        primary_failure = ExecutionEngine.from_plan(
            failed.plan, failed_registry
        ).run(
            inputs_by_phase={
                "phase.run": (
                    _input("model.primary", "observation.failure", 1.0),
                )
            }
        )
        self.assertEqual(primary_failure.status.value, "failed")

    def test_custom_role_permissions_and_policy_provenance_are_enforced(self) -> None:
        role = {
            "role_id": "role.policy-model",
            "profile": "custom",
            "permissions": {
                "scheduling_priority": 7,
                "requires_equivalent_inputs": False,
                "may_feed_policy": True,
                "may_receive_outcomes": False,
                "may_adapt": False,
                "failure_disposition": "fail_run",
                "queue_disposition": "fail_run",
                "queue_limit": None,
            },
        }
        models = (
            {"component_id": "model.policy", "role_id": "role.policy-model"},
        )
        compiled, registry = _compile(
            models,
            custom_roles=(role,),
            policy_source="model.policy",
        )
        binding = compiled.plan.model_bindings[0]
        now = TimePoint(0.0, "boundary.clock")
        forged = Prediction(
            prediction_id="prediction.forged",
            component_id=binding.component_id,
            plugin_id=binding.plugin_id,
            plugin_version=binding.plugin_version,
            model_id=binding.manifest.model_id,
            model_version=binding.manifest.model_version,
            manifest_digest=binding.manifest_digest,
            contract_digest=binding.contract_digest,
            result_digest=canonical_hash({"forged": True}),
            role_id=binding.role.role_id,
            role_profile=binding.role.profile,
            output_port="prediction",
            value={"score": 1.0},
            produced_time=now,
            available_time=now,
            input_ids=("observation.forged",),
            admitted_input_ids=("observation.forged",),
            lineage=Lineage(
                component_id=binding.component_id,
                component_version=binding.plugin_version,
                input_ids=("observation.forged",),
                latest_input_available_time=now,
            ),
            artifact_digests={},
        )
        run = ExecutionEngine.from_plan(compiled.plan, registry).run(
            inputs_by_phase={
                "phase.run": (
                    GraphInput("policy.observe", "prediction", forged),
                )
            }
        )
        self.assertEqual(run.status.value, "failed")
        self.assertIn(
            "policy inputs must be canonical predictions emitted by this execution",
            run.phase_results[0].failure or "",
        )

    def test_compiler_rejects_legacy_role_knobs_and_split_comparison_phases(self) -> None:
        manifest = _manifest()
        payload = _suite_payload(
            manifest,
            (
                {
                    "component_id": "model.primary",
                    "role_id": "primary",
                    "comparison_group": "comparison.phase",
                },
                {
                    "component_id": "model.shadow",
                    "role_id": "shadow",
                    "comparison_group": "comparison.phase",
                },
            ),
        )
        payload["scheduling"]["shadow_queue_limit"] = 4
        payload["phases"][0]["components"] = ["model.primary"]
        with self.assertRaises(CompilationError) as raised:
            compile_suite(
                _protocol(),
                SuiteSpec.from_payload(payload),
                _deployment(),
                _registry(),
                model_manifests={manifest.manifest_digest: manifest},
            )
        codes = {value.code for value in raised.exception.diagnostics}
        self.assertIn("model.legacy_role_scheduling", codes)
        self.assertIn("model.comparison_phase", codes)


if __name__ == "__main__":
    unittest.main()
