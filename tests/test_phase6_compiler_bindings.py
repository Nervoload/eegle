from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import unittest

from eegle._domain import ComponentKind, Determinism, EquivalenceLevel, ExecutionMode
from eegle.compiler import (
    ExecutionLock,
    ExecutionPlan,
    compile_suite,
    diff_plans,
    explain_plan,
)
from eegle.compiler.diagnostics import CompilationError
from eegle.models import (
    ModelContract,
    ModelImplementationRequirement,
    ModelInputContract,
    ModelManifest,
    ModelOutputContract,
    ModelStateBehavior,
    ModelStateContract,
    PreprocessingOwnership,
    PreprocessingRequirement,
)
from eegle.plugins import (
    ContractTransformSpec,
    ConstructionAPI,
    PluginCapabilities,
    PluginDescriptor,
    PluginRegistry,
    PortSpec,
    ProcessingOperationBinding,
    StateBehavior,
)
from eegle.recording import ArtifactReference, Sensitivity
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec


_DENSE_WINDOW = "eegle.dense_window.v1"
_MODEL_RESULT = "eegle.prediction.v2"
_EMPTY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
}


def _capabilities(
    *,
    state: StateBehavior = StateBehavior.STATELESS,
    operations: tuple[str, ...] = (),
) -> PluginCapabilities:
    return PluginCapabilities(
        supported_modes=frozenset({ExecutionMode.CAUSAL}),
        determinism=Determinism.DETERMINISTIC,
        equivalence=EquivalenceLevel.NUMERIC,
        state_behavior=state,
        processing_operations=operations,
    )


def _descriptor(
    plugin_id: str,
    kind: ComponentKind,
    inputs: tuple[PortSpec, ...],
    outputs: tuple[PortSpec, ...],
    *,
    state: StateBehavior = StateBehavior.STATELESS,
    operations: tuple[str, ...] = (),
) -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id=plugin_id,
        version="1.2.0",
        kind=kind,
        config_schema=_EMPTY_SCHEMA,
        input_ports=inputs,
        output_ports=outputs,
        capabilities=_capabilities(state=state, operations=operations),
        factory=lambda config: object(),
        implementation=f"tests.phase6:{plugin_id.rsplit('.', 1)[-1]}",
        distribution="phase6-fixture",
        construction_api=(
            ConstructionAPI.MODEL_CONTEXT_V1
            if kind == ComponentKind.MODEL
            else ConstructionAPI.CONFIG_V1
        ),
        contract_transform=(
            ContractTransformSpec(inputs[0].name, outputs[0].name)
            if kind == ComponentKind.TRANSFORM and inputs and outputs
            else None
        ),
    )


def _registry(
    *,
    transform_operations: tuple[str, ...] = ("detrend",),
    model_state: StateBehavior = StateBehavior.SNAPSHOT_RESTORE,
    model_output_type: str = _MODEL_RESULT,
) -> PluginRegistry:
    registry = PluginRegistry()
    registry.register(
        _descriptor(
            "fixture.source.slow",
            ComponentKind.SOURCE,
            (),
            (PortSpec("windows", _DENSE_WINDOW, multiple=True),),
        )
    )
    registry.register(
        _descriptor(
            "fixture.transform.detrend",
            ComponentKind.TRANSFORM,
            (PortSpec("input", _DENSE_WINDOW),),
            (PortSpec("output", _DENSE_WINDOW),),
            operations=transform_operations,
        )
    )
    registry.register(
        _descriptor(
            "fixture.model.slow",
            ComponentKind.MODEL,
            (PortSpec("observations", _DENSE_WINDOW),),
            (PortSpec("estimate", model_output_type),),
            state=model_state,
            operations=("normalization",),
        )
    )
    registry.register(
        _descriptor(
            "fixture.policy",
            ComponentKind.POLICY,
            (PortSpec("prediction", _MODEL_RESULT),),
            (),
        )
    )
    return registry


def _manifest(
    *,
    model_id: str = "model.slow-estimator",
    required_channels: tuple[str, ...] = ("sensor.oxy", "sensor.deoxy"),
    implementation: str = "fixture.model.slow",
    supported_modes: frozenset[ExecutionMode] = frozenset({ExecutionMode.CAUSAL}),
    detrend_parameters: dict | None = None,
) -> ModelManifest:
    contract = ModelContract(
        inputs=(
            ModelInputContract(
                port_name="observations",
                type_id=_DENSE_WINDOW,
                requirements={
                    "content_kind": "dense_samples",
                    "rate_model": "irregular",
                    "channel_ids": list(required_channels),
                    "units": {value: "mol/m2" for value in required_channels},
                    "minimum_duration_seconds": 12.0,
                    "missing_data_policy": "validity_mask",
                },
                preprocessing=(
                    PreprocessingRequirement(
                        requirement_id="requirement.detrend",
                        operation="detrend",
                        ownership=PreprocessingOwnership.UPSTREAM,
                        parameters=detrend_parameters or {},
                        required_lineage=("transform.detrend",),
                    ),
                    PreprocessingRequirement(
                        requirement_id="requirement.normalization",
                        operation="normalization",
                        ownership=PreprocessingOwnership.MODEL_INTERNAL,
                    ),
                ),
            ),
        ),
        outputs=(
            ModelOutputContract(
                port_name="estimate",
                type_id=_MODEL_RESULT,
                value_schema={"type": "object", "required": ["value"]},
                uncertainty_schema={"type": "object"},
            ),
        ),
        state=ModelStateContract(
            behavior=ModelStateBehavior.SNAPSHOT_RESTORE,
            state_schema_id="fixture.slow_state.v1",
            initial_state_required=True,
            adaptation_supported=True,
            state_affects_predictions=True,
            replay_equivalence=EquivalenceLevel.NUMERIC,
        ),
        supported_modes=supported_modes,
    )
    return ModelManifest(
        model_id=model_id,
        model_version="1.2.0",
        contract=contract,
        artifacts=(
            ArtifactReference(
                artifact_id="artifact.weights",
                role="model_parameters",
                uri="artifact://artifact.weights",
                digest="sha256:" + "1" * 64,
                media_type="application/octet-stream",
                size_bytes=1024,
                sensitivity=Sensitivity.INTERNAL,
            ),
            ArtifactReference(
                artifact_id="artifact.initial-state",
                role="model_initial_state",
                uri="artifact://artifact.initial-state",
                digest="sha256:" + "2" * 64,
                media_type="application/json",
                size_bytes=128,
                sensitivity=Sensitivity.INTERNAL,
            ),
        ),
        implementations=(
            ModelImplementationRequirement(
                plugin_id=implementation,
                version_spec="~=1.2",
            ),
        ),
        initial_state_artifact_id="artifact.initial-state",
    )


def _protocol() -> ProtocolSpec:
    return ProtocolSpec.from_payload(
        {
            "schema": "eegle.protocol_spec.v1",
            "protocol_id": "protocol.phase6-model-binding",
            "execution_mode": "causal",
            "claims": [
                {
                    "claim_id": "claim.model-binding",
                    "statement": "The locked model binding governs prediction execution.",
                }
            ],
            "metrics": [],
            "acceptance": [],
            "annotations": {},
        }
    )


def _signal_contract() -> dict:
    return {
        "type_id": _DENSE_WINDOW,
        "content_kind": "dense_samples",
        "rate_model": "irregular",
        "channel_ids": ["sensor.oxy", "sensor.deoxy"],
        "units": {"sensor.oxy": "mol/m2", "sensor.deoxy": "mol/m2"},
        "missing_data_policy": "validity_mask",
        "window_duration_seconds": 12.5,
    }


def _suite(manifest: ModelManifest, *, role: str = "primary", policy: bool = False) -> dict:
    components = [
        {
            "component_id": "source.signal",
            "kind": "source",
            "plugin_id": "fixture.source.slow",
            "version_spec": "~=1.2",
            "stream_id": "stream.slow",
            "config": {},
        },
        {
            "component_id": "transform.detrend",
            "kind": "transform",
            "plugin_id": "fixture.transform.detrend",
            "version_spec": "~=1.2",
            "config": {},
            "input_contracts": {"input": _signal_contract()},
            "output_contracts": {"output": _signal_contract()},
        },
        {
            "component_id": "model.slow",
            "kind": "model",
            "plugin_id": "fixture.model.slow",
            "version_spec": "~=1.2",
            "config": {},
        },
    ]
    routes = [
        {
            "route_id": "route.source-transform",
            "source": {"component": "source.signal", "port": "windows"},
            "target": {"component": "transform.detrend", "port": "input"},
        },
        {
            "route_id": "route.transform-model",
            "source": {"component": "transform.detrend", "port": "output"},
            "target": {"component": "model.slow", "port": "observations"},
        },
    ]
    if policy:
        components.append(
            {
                "component_id": "policy.output",
                "kind": "policy",
                "plugin_id": "fixture.policy",
                "version_spec": "~=1.2",
                "config": {},
            }
        )
        routes.append(
            {
                "route_id": "route.model-policy",
                "source": {"component": "model.slow", "port": "estimate"},
                "target": {"component": "policy.output", "port": "prediction"},
            }
        )
    return {
        "schema": "eegle.suite_spec.v1",
        "suite_id": "suite.phase6-model-binding",
        "protocol_id": "protocol.phase6-model-binding",
        "streams": [
            {
                "stream_id": "stream.slow",
                "modality": "fnirs-like",
                "clock_id": "boundary.clock",
                "contract": _signal_contract(),
            }
        ],
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
        "model_roles": [],
        "model_uses": [
            {
                "component_id": "model.slow",
                "manifest_digest": manifest.manifest_digest,
                "role_id": role,
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


def _deployment(manifest: ModelManifest, *, uri_suffix: str = "a") -> dict:
    return {
        "schema": "eegle.deployment_spec.v1",
        "deployment_id": "deployment.phase6-model-binding",
        "suite_id": "suite.phase6-model-binding",
        "component_bindings": [],
        "resources": [
            {
                "resource_id": "resource.slow",
                "kind": "simulator",
                "selector": {},
                "capabilities": [],
                "contract": _signal_contract(),
            }
        ],
        "stream_bindings": [
            {
                "stream_id": "stream.slow",
                "resource_id": "resource.slow",
                "selector": {},
            }
        ],
        "storage": [],
        "permissions": [],
        "secrets": [],
        "clock_mappings": [],
        "model_artifacts": [
            {
                "manifest_digest": manifest.manifest_digest,
                "artifact_id": artifact.artifact_id,
                "uri": f"file:///models/{uri_suffix}/{artifact.artifact_id}",
                "digest": artifact.digest,
            }
            for artifact in manifest.artifacts
        ],
    }


def _compile(
    manifest: ModelManifest,
    *,
    suite: dict | None = None,
    deployment: dict | None = None,
    registry: PluginRegistry | None = None,
):
    return compile_suite(
        _protocol(),
        SuiteSpec.from_payload(_suite(manifest) if suite is None else suite),
        DeploymentSpec.from_payload(
            _deployment(manifest) if deployment is None else deployment
        ),
        _registry() if registry is None else registry,
        model_manifests={manifest.manifest_digest: manifest},
    )


class Phase6CompilerBindingTests(unittest.TestCase):
    def test_compiler_locks_exact_model_manifest_role_artifacts_and_lineage(self) -> None:
        manifest = _manifest()

        compiled = _compile(manifest)

        self.assertEqual(len(compiled.plan.model_bindings), 1)
        binding = compiled.plan.model_bindings[0]
        self.assertEqual(binding.component_id, "model.slow")
        self.assertEqual(binding.manifest_digest, manifest.manifest_digest)
        self.assertEqual(binding.contract_digest, manifest.contract.contract_digest)
        self.assertEqual(binding.role.profile, "primary")
        self.assertTrue(binding.role.may_feed_policy)
        self.assertEqual(
            binding.preprocessing_lineage["observations"],
            ("source.signal", "transform.detrend"),
        )
        self.assertEqual(
            {value.artifact_id for value in binding.artifacts},
            {"artifact.weights", "artifact.initial-state"},
        )
        self.assertEqual(
            compiled.lock.model_hashes["model.slow"],
            manifest.manifest_digest,
        )
        restored_plan = ExecutionPlan.from_payload(compiled.plan.to_payload())
        restored_lock = ExecutionLock.from_payload(compiled.lock.to_payload())
        restored_lock.verify_plan(restored_plan)
        explanation = explain_plan(compiled.plan).to_payload()
        self.assertEqual(
            explanation["model_bindings"][0]["manifest_digest"],
            manifest.manifest_digest,
        )

    def test_model_input_and_preprocessing_must_be_proven_by_the_route(self) -> None:
        incompatible = _manifest(required_channels=("sensor.oxy", "sensor.missing"))
        with self.assertRaises(CompilationError) as raised:
            _compile(incompatible)
        codes = {value.code for value in raised.exception.diagnostics}
        self.assertIn("model.input_contract", codes)

        manifest = _manifest()
        duplicate = _registry(transform_operations=("detrend", "normalization"))
        with self.assertRaises(CompilationError) as raised:
            _compile(manifest, registry=duplicate)
        self.assertIn(
            "model.preprocessing_duplicate",
            {value.code for value in raised.exception.diagnostics},
        )

    def test_preprocessing_parameters_are_projected_and_locked(self) -> None:
        manifest = _manifest(
            detrend_parameters={"causal": True, "axis": "time"}
        )
        registry = _registry()
        registry.unregister("fixture.transform.detrend")
        registry.register(
            PluginDescriptor(
                plugin_id="fixture.transform.detrend",
                version="1.2.0",
                kind=ComponentKind.TRANSFORM,
                config_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {"axis": {"type": "string"}},
                    "additionalProperties": False,
                },
                input_ports=(PortSpec("input", _DENSE_WINDOW),),
                output_ports=(PortSpec("output", _DENSE_WINDOW),),
                capabilities=PluginCapabilities(
                    supported_modes=frozenset({ExecutionMode.CAUSAL}),
                    determinism=Determinism.DETERMINISTIC,
                    equivalence=EquivalenceLevel.NUMERIC,
                    state_behavior=StateBehavior.STATELESS,
                    processing_operations=(
                        ProcessingOperationBinding(
                            "detrend",
                            fixed_parameters={"causal": True},
                            config_projection={"axis": "/axis"},
                        ),
                    ),
                ),
                factory=lambda config: object(),
                implementation="tests.phase6:parameterized_detrend",
                distribution="phase6-fixture",
                contract_transform=ContractTransformSpec("input", "output"),
            )
        )
        suite = _suite(manifest)
        suite["components"][1]["config"] = {"axis": "time"}
        compiled = _compile(manifest, suite=suite, registry=registry)
        attestation = compiled.plan.model_bindings[0].preprocessing_attestations[0]
        self.assertEqual(attestation.owner_id, "transform.detrend")
        self.assertEqual(
            dict(attestation.parameters),
            {"causal": True, "axis": "time"},
        )

        mismatched = deepcopy(suite)
        mismatched["components"][1]["config"]["axis"] = "channels"
        with self.assertRaises(CompilationError) as raised:
            _compile(manifest, suite=mismatched, registry=registry)
        self.assertIn(
            "model.preprocessing_parameters",
            {value.code for value in raised.exception.diagnostics},
        )

    def test_artifact_prepared_and_forbidden_processing_are_digest_bound(self) -> None:
        manifest = _manifest()
        input_contract = manifest.contract.inputs[0]
        processing = (
            *input_contract.preprocessing,
            PreprocessingRequirement(
                requirement_id="requirement.artifact-scaling",
                operation="robust_scaling",
                ownership=PreprocessingOwnership.ARTIFACT_PREPARED,
                parameters={"quantile_low": 0.1, "quantile_high": 0.9},
            ),
            PreprocessingRequirement(
                requirement_id="requirement.no-zero-phase",
                operation="zero_phase_filter",
                ownership=PreprocessingOwnership.FORBIDDEN,
            ),
        )
        contract = replace(
            manifest.contract,
            inputs=(replace(input_contract, preprocessing=processing),),
        )
        manifest = replace(manifest, contract=contract)

        compiled = _compile(manifest)

        attestations = {
            value.requirement_id: value
            for value in compiled.plan.model_bindings[0].preprocessing_attestations
        }
        artifact = attestations["requirement.artifact-scaling"]
        self.assertEqual(artifact.owner_kind, "artifact_prepared")
        self.assertEqual(artifact.owner_id, manifest.manifest_digest)
        self.assertEqual(
            dict(artifact.parameters),
            {"quantile_low": 0.1, "quantile_high": 0.9},
        )
        forbidden = attestations["requirement.no-zero-phase"]
        self.assertEqual(forbidden.owner_id, "compiled_route")

    def test_every_model_requires_a_manifest_binding(self) -> None:
        manifest = _manifest()
        suite = _suite(manifest)
        suite["model_uses"] = []

        with self.assertRaises(CompilationError) as raised:
            _compile(manifest, suite=suite)

        self.assertIn(
            "model.binding_missing",
            {value.code for value in raised.exception.diagnostics},
        )

    def test_artifact_digest_and_implementation_are_compiler_checked(self) -> None:
        manifest = _manifest()
        deployment = _deployment(manifest)
        deployment["model_artifacts"][0]["digest"] = "sha256:" + "9" * 64
        with self.assertRaises(CompilationError) as raised:
            _compile(manifest, deployment=deployment)
        diagnostic = next(
            value for value in raised.exception.diagnostics
            if value.code == "model.artifact_digest"
        )
        self.assertEqual(
            diagnostic.path,
            "$.deployment.model_artifacts[0].digest",
        )

        incompatible = _manifest(implementation="fixture.model.other")
        with self.assertRaises(CompilationError) as raised:
            _compile(incompatible)
        self.assertIn(
            "model.implementation",
            {value.code for value in raised.exception.diagnostics},
        )

        config_only = _registry()
        descriptor = config_only.resolve("fixture.model.slow", "==1.2.0")
        config_only.unregister("fixture.model.slow")
        config_only.register(
            replace(descriptor, construction_api=ConstructionAPI.CONFIG_V1)
        )
        with self.assertRaises(CompilationError) as raised:
            _compile(manifest, registry=config_only)
        self.assertIn(
            "model.construction_api",
            {value.code for value in raised.exception.diagnostics},
        )

    def test_role_permissions_prevent_shadow_policy_influence(self) -> None:
        manifest = _manifest()
        suite = _suite(manifest, role="shadow", policy=True)
        suite["model_uses"][0]["comparison_group"] = "comparison.reference"

        with self.assertRaises(CompilationError) as raised:
            _compile(manifest, suite=suite)

        codes = {value.code for value in raised.exception.diagnostics}
        self.assertIn("model.role_policy_permission", codes)
        self.assertIn("model.comparison_group_size", codes)

    def test_comparison_roles_lock_shared_inputs_and_reject_different_inputs(self) -> None:
        manifest = _manifest()
        suite = _suite(manifest)
        shadow = deepcopy(suite["components"][2])
        shadow["component_id"] = "model.shadow"
        suite["components"].append(shadow)
        suite["routes"].append(
            {
                "route_id": "route.transform-shadow",
                "source": {"component": "transform.detrend", "port": "output"},
                "target": {"component": "model.shadow", "port": "observations"},
            }
        )
        suite["phases"][0]["components"].append("model.shadow")
        suite["model_uses"][0]["comparison_group"] = "comparison.reference"
        suite["model_uses"].append(
            {
                "component_id": "model.shadow",
                "manifest_digest": manifest.manifest_digest,
                "role_id": "shadow",
                "comparison_group": "comparison.reference",
            }
        )

        compiled = _compile(manifest, suite=suite)

        roles = {value.component_id: value.role for value in compiled.plan.model_bindings}
        self.assertTrue(roles["model.slow"].may_feed_policy)
        self.assertFalse(roles["model.shadow"].may_feed_policy)
        self.assertTrue(roles["model.shadow"].requires_equivalent_inputs)

        divergent = deepcopy(suite)
        divergent["routes"][-1]["source"] = {
            "component": "source.signal",
            "port": "windows",
        }
        with self.assertRaises(CompilationError) as raised:
            _compile(manifest, suite=divergent)
        self.assertIn(
            "model.comparison_inputs",
            {value.code for value in raised.exception.diagnostics},
        )

    def test_mode_state_output_and_duration_incompatibilities_are_precise(self) -> None:
        retrospective = _manifest(
            supported_modes=frozenset({ExecutionMode.RETROSPECTIVE})
        )
        with self.assertRaises(CompilationError) as raised:
            _compile(retrospective)
        self.assertIn(
            "model.execution_mode",
            {value.code for value in raised.exception.diagnostics},
        )

        manifest = _manifest()
        with self.assertRaises(CompilationError) as raised:
            _compile(manifest, registry=_registry(model_state=StateBehavior.STATELESS))
        self.assertIn(
            "model.state_behavior",
            {value.code for value in raised.exception.diagnostics},
        )

        with self.assertRaises(CompilationError) as raised:
            _compile(
                manifest,
                registry=_registry(model_output_type="fixture.incompatible_output.v1"),
            )
        self.assertIn(
            "model.output_type",
            {value.code for value in raised.exception.diagnostics},
        )

        suite = _suite(manifest)
        suite["components"][1]["output_contracts"]["output"][
            "window_duration_seconds"
        ] = 5.0
        with self.assertRaises(CompilationError) as raised:
            _compile(manifest, suite=suite)
        self.assertIn(
            "model.input_contract",
            {value.code for value in raised.exception.diagnostics},
        )

    def test_custom_role_permissions_compile_without_becoming_authorization(self) -> None:
        manifest = _manifest()
        suite = _suite(manifest, role="role.research-observer")
        suite["model_roles"] = [
            {
                "role_id": "role.research-observer",
                "profile": "custom",
                "permissions": {
                    "scheduling_priority": 250,
                    "requires_equivalent_inputs": False,
                    "may_feed_policy": False,
                    "may_receive_outcomes": True,
                    "may_adapt": False,
                    "failure_disposition": "reject_result",
                    "queue_disposition": "shed_oldest",
                    "queue_limit": 2,
                },
            }
        ]

        compiled = _compile(manifest, suite=suite)

        role = compiled.plan.model_bindings[0].role
        self.assertEqual(role.role_id, "role.research-observer")
        self.assertFalse(role.may_feed_policy)
        self.assertFalse(role.may_adapt)
        self.assertNotIn("authorization", str(role.to_payload()).lower())

    def test_materialization_changes_are_operational_and_model_changes_scientific(self) -> None:
        manifest = _manifest()
        before = _compile(manifest)
        after = _compile(manifest, deployment=_deployment(manifest, uri_suffix="b"))

        difference = diff_plans(before.plan, after.plan)

        operational = {
            value.path for value in difference.changes if value.materiality.value == "operational"
        }
        scientific = {
            value.path for value in difference.changes if value.materiality.value == "scientific"
        }
        self.assertIn("$.model_materializations.model.slow", operational)
        self.assertNotIn("$.model_bindings.model.slow", scientific)

    def test_plan_rejects_tampered_model_binding_digests(self) -> None:
        compiled = _compile(_manifest())
        payload = deepcopy(compiled.plan.to_payload())
        payload["model_bindings"][0]["contract_digest"] = "sha256:" + "0" * 64

        with self.assertRaisesRegex(ValueError, "contract digest mismatch"):
            ExecutionPlan.from_payload(payload)


if __name__ == "__main__":
    unittest.main()
