from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from eegle._domain import ComponentKind, Determinism, EquivalenceLevel, ExecutionMode
from eegle.compiler import (
    CompilationError,
    ExecutionLock,
    ExecutionPlan,
    diff_plans,
    explain_plan,
    read_lock,
    read_plan,
    write_lock,
    write_plan,
)
from eegle.compiler.compiler import _compile_graph
from eegle.plugins import (
    PluginCapabilities,
    PluginDescriptor,
    PluginRegistry,
    PortSpec,
    StateBehavior,
)
from eegle.specs import (
    ComponentSpec,
    DeploymentSpec,
    ProtocolSpec,
    RouteSpec,
    SuiteOverlay,
    SuiteSpec,
    compose_suite,
)
from tests.fixtures.phase5_model_components import (
    compile_phase5_suite as compile_suite,
)
from tests.fixtures.phase5_model_components import (
    register_phase5_plugins,
)

FIXTURE = Path(__file__).parent / "fixtures" / "migration" / "phase5_simulated"
DENSE = "eegle.dense_sample_batch.v1"


def _payload(name: str) -> dict:
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def _specs() -> tuple[ProtocolSpec, SuiteSpec, DeploymentSpec]:
    return (
        ProtocolSpec.from_payload(_payload("protocol.json")),
        SuiteSpec.from_payload(_payload("suite.json")),
        DeploymentSpec.from_payload(_payload("deployment.json")),
    )


def _registry() -> PluginRegistry:
    value = PluginRegistry()
    value.register_builtins()
    register_phase5_plugins(value)
    return value


class Phase5SpecificationTests(unittest.TestCase):
    def test_reference_specs_round_trip_with_stable_independent_hashes(self) -> None:
        protocol, suite, deployment = _specs()

        self.assertEqual(ProtocolSpec.from_payload(protocol.to_payload()), protocol)
        self.assertEqual(SuiteSpec.from_payload(suite.to_payload()), suite)
        self.assertEqual(DeploymentSpec.from_payload(deployment.to_payload()), deployment)
        self.assertEqual(protocol.spec_hash, ProtocolSpec.from_payload(protocol.to_payload()).spec_hash)
        self.assertEqual(suite.spec_hash, SuiteSpec.from_payload(suite.to_payload()).spec_hash)
        self.assertEqual(
            deployment.spec_hash,
            DeploymentSpec.from_payload(deployment.to_payload()).spec_hash,
        )

    def test_bounded_ordered_overlays_change_only_existing_config_and_policy(self) -> None:
        _, suite, _ = _specs()
        overlay = SuiteOverlay.from_payload(
            {
                "schema": "eegle.suite_overlay.v1",
                "overlay_id": "overlay.threshold",
                "component_config": {"model.primary": {"threshold": 0.75}},
                "recording": {"raw_recording": "omit"},
                "validation": {"minimum_coverage": 0.99},
            }
        )

        composed = compose_suite(suite, (overlay,))

        self.assertEqual(composed.routes, suite.routes)
        self.assertEqual(composed.phases, suite.phases)
        model = next(value for value in composed.components if value.component_id == "model.primary")
        self.assertEqual(model.config["threshold"], 0.75)
        self.assertEqual(composed.recording["raw_recording"], "omit")
        self.assertEqual(compose_suite(suite, (overlay,)).spec_hash, composed.spec_hash)
        unknown = SuiteOverlay(overlay_id="overlay.invalid", component_config={"missing": {}})
        with self.assertRaisesRegex(ValueError, "unknown component missing"):
            compose_suite(suite, (unknown,))

    def test_deployment_rejects_secret_literals_and_implicit_storage_paths(self) -> None:
        secret = _payload("deployment.json")
        secret["component_bindings"][0]["config"]["password"] = "do-not-store"
        with self.assertRaisesRegex(ValueError, "literal secret fields are forbidden"):
            DeploymentSpec.from_payload(secret)

        for key in ("apiKey", "clientSecret"):
            camel_case = _payload("deployment.json")
            camel_case["component_bindings"][0]["config"][key] = "do-not-store"
            with self.subTest(key=key), self.assertRaisesRegex(
                ValueError,
                "literal secret fields are forbidden",
            ):
                DeploymentSpec.from_payload(camel_case)

        implicit = _payload("deployment.json")
        implicit["storage"][0]["uri"] = "relative/session"
        with self.assertRaisesRegex(ValueError, "include a URI scheme"):
            DeploymentSpec.from_payload(implicit)

        windows_path = _payload("deployment.json")
        windows_path["storage"][0]["uri"] = "C:\\eegle\\session"
        with self.assertRaisesRegex(ValueError, "Windows drive path"):
            DeploymentSpec.from_payload(windows_path)

    def test_versioned_specs_reject_missing_schema_instead_of_upgrading(self) -> None:
        for contract, payload in (
            (ProtocolSpec, _payload("protocol.json")),
            (SuiteSpec, _payload("suite.json")),
            (DeploymentSpec, _payload("deployment.json")),
        ):
            del payload["schema"]
            with self.subTest(contract=contract.__name__), self.assertRaises(ValueError):
                contract.from_payload(payload)


class Phase5CompilerTests(unittest.TestCase):
    def test_causal_compiler_rejects_label_bearing_model_routes(self) -> None:
        sparse = "eegle.sparse_event_batch.v1"
        capabilities = PluginCapabilities(
            supported_modes=frozenset({ExecutionMode.CAUSAL}),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.BITWISE,
            state_behavior=StateBehavior.STATELESS,
        )
        source = PluginDescriptor(
            plugin_id="fixture.events",
            version="1.0.0",
            kind=ComponentKind.TRANSFORM,
            config_schema={"type": "object"},
            input_ports=(),
            output_ports=(PortSpec("events", sparse),),
            capabilities=capabilities,
            factory=lambda config: object(),
            implementation="fixture:Events",
            distribution="tests",
        )
        model = PluginDescriptor(
            plugin_id="fixture.leaky_model",
            version="1.0.0",
            kind=ComponentKind.MODEL,
            config_schema={"type": "object"},
            input_ports=(PortSpec("labels", sparse),),
            output_ports=(),
            capabilities=capabilities,
            factory=lambda config: object(),
            implementation="fixture:LeakyModel",
            distribution="tests",
        )
        suite = SimpleNamespace(
            components=(
                ComponentSpec("events", ComponentKind.TRANSFORM),
                ComponentSpec("model", ComponentKind.MODEL),
            ),
            routes=(RouteSpec("route.labels", "events", "events", "model", "labels"),),
        )
        diagnostics = []

        _compile_graph(
            ExecutionMode.CAUSAL,
            suite,
            {"events": source, "model": model},
            {},
            diagnostics,
        )

        self.assertTrue(
            any(value.code == "model.label_blind_input" for value in diagnostics)
        )

    def test_reference_suite_compiles_deterministically_to_plan_graph_and_lock(self) -> None:
        protocol, suite, deployment = _specs()
        registry = _registry()

        first = compile_suite(protocol, suite, deployment, registry)
        second = compile_suite(protocol, suite, deployment, registry)

        self.assertEqual(first.plan.plan_hash, second.plan.plan_hash)
        self.assertEqual(first.lock.lock_hash, second.lock.lock_hash)
        self.assertEqual(
            first.graph.component_order,
            (
                "source.neural",
                "transform.identity",
                "window.continuous",
                "model.primary",
                "policy.observe",
            ),
        )
        self.assertEqual(first.plan.schema, "eegle.execution_plan.v1")
        self.assertEqual(first.plan.initial_phase, "phase.observe")
        self.assertEqual(first.plan.components[0].plugin_id, "eegle.sources.packet_sequence_dense")
        self.assertEqual(ExecutionPlan.from_payload(first.plan.to_payload()).plan_hash, first.plan.plan_hash)
        restored_lock = ExecutionLock.from_payload(first.lock.to_payload())
        restored_lock.verify_plan(first.plan)

    def test_same_suite_compiles_against_simulated_and_live_capability_deployments(self) -> None:
        protocol, suite, simulated = _specs()
        registry = _registry()
        registry.register(
            PluginDescriptor(
                plugin_id="fixture.live_dense_source",
                version="1.2.0",
                kind=ComponentKind.SOURCE,
                config_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                input_ports=(),
                output_ports=(PortSpec("samples", DENSE, multiple=True),),
                capabilities=PluginCapabilities(
                    supported_modes=frozenset({ExecutionMode.CAUSAL}),
                    determinism=Determinism.EXTERNAL,
                    equivalence=EquivalenceLevel.TRACE,
                    state_behavior=StateBehavior.EXTERNAL,
                    resources=("amplifier",),
                ),
                factory=lambda config: object(),
                implementation="fixture.live:Source",
                distribution="fixture-live",
            )
        )
        live_payload = simulated.to_payload()
        live_payload["deployment_id"] = "deployment.live"
        live_payload["component_bindings"][0]["plugin_id"] = "fixture.live_dense_source"
        live_payload["component_bindings"][0]["version_spec"] = "~=1.2.0"
        live_payload["component_bindings"][0]["config"] = {}
        live_payload["resources"][0]["kind"] = "amplifier"
        live_payload["resources"][0]["capabilities"] = ["amplifier"]
        live = DeploymentSpec.from_payload(live_payload)

        simulated_result = compile_suite(protocol, suite, simulated, registry)
        live_result = compile_suite(protocol, suite, live, registry)

        self.assertEqual(simulated_result.plan.spec_hashes["suite"], live_result.plan.spec_hashes["suite"])
        self.assertEqual(
            simulated_result.plan.spec_hashes["protocol"],
            live_result.plan.spec_hashes["protocol"],
        )
        self.assertNotEqual(simulated_result.plan.spec_hashes["deployment"], live_result.plan.spec_hashes["deployment"])
        self.assertEqual(live_result.plan.components[0].plugin_version, "1.2.0")

    def test_invalid_plugin_config_has_stable_component_path(self) -> None:
        protocol, _, deployment = _specs()
        suite_payload = _payload("suite.json")
        suite_payload["components"][2]["config"]["window_samples"] = 0

        with self.assertRaises(CompilationError) as raised:
            compile_suite(
                protocol,
                SuiteSpec.from_payload(suite_payload),
                deployment,
                _registry(),
            )

        diagnostic = next(
            value for value in raised.exception.diagnostics if value.code == "plugin.config"
        )
        self.assertEqual(diagnostic.path, "$.suite.components[2].config.window_samples")
        self.assertIn("minimum of 1", diagnostic.message)

        deployment_payload = _payload("deployment.json")
        del deployment_payload["component_bindings"][0]["config"]["stream_spec"]
        with self.assertRaises(CompilationError) as deployment_error:
            compile_suite(
                protocol,
                SuiteSpec.from_payload(_payload("suite.json")),
                DeploymentSpec.from_payload(deployment_payload),
                _registry(),
            )
        deployment_diagnostic = next(
            value
            for value in deployment_error.exception.diagnostics
            if value.code == "plugin.config"
        )
        self.assertEqual(
            deployment_diagnostic.path,
            "$.deployment.component_bindings[0].config",
        )

    def test_signal_unit_rate_and_channel_capabilities_fail_before_source_construction(self) -> None:
        protocol, suite, _ = _specs()
        deployment_payload = _payload("deployment.json")
        contract = deployment_payload["resources"][0]["contract"]
        contract["unit"] = "V"
        contract["channel_count"] = 1
        contract["nominal_rate_hz"] = 50.0

        with self.assertRaises(CompilationError) as raised:
            compile_suite(
                protocol,
                suite,
                DeploymentSpec.from_payload(deployment_payload),
                _registry(),
            )

        messages = [
            value.message
            for value in raised.exception.diagnostics
            if value.code == "stream.capability"
        ]
        self.assertTrue(any("unit V" in value for value in messages))
        self.assertTrue(any("channel count 1" in value for value in messages))
        self.assertTrue(any("sample rate 50.0" in value for value in messages))

    def test_port_contract_and_phase_errors_are_precise(self) -> None:
        protocol, _, deployment = _specs()
        payload = _payload("suite.json")
        payload["components"][3]["input_contracts"]["window"]["type_id"] = DENSE
        payload["phases"][0]["transitions"] = [
            {"target_phase": "phase.missing", "condition": "complete"}
        ]

        with self.assertRaises(CompilationError) as raised:
            compile_suite(protocol, SuiteSpec.from_payload(payload), deployment, _registry())

        by_code = {value.code: value for value in raised.exception.diagnostics}
        self.assertEqual(
            by_code["port.contract_type"].path,
            "$.suite.components[3].input_contracts.window.type_id",
        )
        self.assertEqual(
            by_code["phase.transition_reference"].path,
            "$.suite.phases[0].transitions[0].target_phase",
        )

    def test_runtime_limits_and_deadlines_fail_during_compilation(self) -> None:
        protocol, _, deployment = _specs()
        payload = _payload("suite.json")
        payload["validation"].update(
            {
                "max_pending_events": 0,
                "component_deadlines_seconds": {
                    "model.primary": -0.1,
                    "model.missing": 0.1,
                },
            }
        )

        with self.assertRaises(CompilationError) as raised:
            compile_suite(
                protocol,
                SuiteSpec.from_payload(payload),
                deployment,
                _registry(),
            )

        by_code = {value.code: value for value in raised.exception.diagnostics}
        self.assertEqual(
            by_code["runtime.limit"].path,
            "$.suite.validation.max_pending_events",
        )
        self.assertEqual(
            by_code["runtime.deadline_value"].path,
            "$.suite.validation.component_deadlines_seconds.model.primary",
        )
        self.assertEqual(
            by_code["runtime.deadline_component"].path,
            "$.suite.validation.component_deadlines_seconds.model.missing",
        )

    def test_runtime_limits_reject_integer_like_values(self) -> None:
        protocol, _, deployment = _specs()
        payload = _payload("suite.json")
        payload["validation"]["max_pending_events"] = 1.0

        with self.assertRaises(CompilationError) as raised:
            compile_suite(
                protocol,
                SuiteSpec.from_payload(payload),
                deployment,
                _registry(),
            )

        self.assertTrue(
            any(
                value.code == "runtime.limit"
                and value.path == "$.suite.validation.max_pending_events"
                for value in raised.exception.diagnostics
            )
        )

    def test_artifact_declarations_ports_dominance_and_lock_hashes_are_checked(self) -> None:
        protocol, _, deployment = _specs()
        undeclared = _payload("suite.json")
        undeclared["phases"][0]["required_artifacts"] = ["artifact.missing"]
        with self.assertRaises(CompilationError) as missing:
            compile_suite(
                protocol, SuiteSpec.from_payload(undeclared), deployment, _registry()
            )
        self.assertTrue(
            any(value.code == "artifact.undeclared" for value in missing.exception.diagnostics)
        )

        unlocked = _payload("suite.json")
        unlocked["phases"][0]["required_artifacts"] = ["model.external"]
        unlocked["artifacts"] = [
            {
                "artifact_id": "model.external",
                "role": "model_bundle",
                "media_type": "application/json",
            }
        ]
        with self.assertRaises(CompilationError) as unlocked_error:
            compile_suite(
                protocol, SuiteSpec.from_payload(unlocked), deployment, _registry()
            )
        self.assertTrue(
            any(
                value.code == "artifact.digest_required"
                for value in unlocked_error.exception.diagnostics
            )
        )

        payload = _payload("suite.json")
        payload["components"].append(
            {
                "component_id": "artifact.calibration",
                "kind": "artifact",
                "plugin_id": "eegle.artifacts.prediction_summary",
                "version_spec": "~=0.1.0",
                "config": {
                    "artifact_id": "calibration.model",
                    "role": "calibration_model",
                },
            }
        )
        payload["routes"].append(
            {
                "route_id": "route.model_artifact",
                "source": {"component": "model.primary", "port": "prediction"},
                "target": {"component": "artifact.calibration", "port": "prediction"},
            }
        )
        all_components = [value["component_id"] for value in payload["components"]]
        payload["phases"] = [
            {
                "phase_id": "phase.choose",
                "components": all_components,
                "transitions": [
                    {"target_phase": "phase.produce", "condition": "operator"},
                    {"target_phase": "phase.consume", "condition": "operator"},
                ],
            },
            {
                "phase_id": "phase.produce",
                "components": all_components,
                "transitions": [],
            },
            {
                "phase_id": "phase.consume",
                "components": all_components,
                "transitions": [],
                "required_artifacts": ["calibration.model"],
            },
        ]
        payload["initial_phase"] = "phase.choose"
        payload["artifacts"] = [
            {
                "artifact_id": "calibration.model",
                "role": "calibration_model",
                "media_type": "application/json",
                "producer_phase": "phase.produce",
                "producer_component": "artifact.calibration",
                "producer_port": "artifact",
            }
        ]
        with self.assertRaises(CompilationError) as dominance:
            compile_suite(
                protocol, SuiteSpec.from_payload(payload), deployment, _registry()
            )
        self.assertTrue(
            any(
                value.code == "artifact.not_guaranteed"
                for value in dominance.exception.diagnostics
            )
        )

        external = _payload("suite.json")
        digest = "sha256:" + "1" * 64
        external["artifacts"] = [
            {
                "artifact_id": "model.frozen",
                "role": "model_bundle",
                "media_type": "application/json",
                "expected_digest": digest,
            }
        ]
        compiled = compile_suite(
            protocol, SuiteSpec.from_payload(external), deployment, _registry()
        )
        self.assertEqual(compiled.lock.artifact_hashes, {"model.frozen": digest})
        restored = ExecutionLock.from_payload(compiled.lock.to_payload())
        restored.verify_plan(ExecutionPlan.from_payload(compiled.plan.to_payload()))

    def test_clock_mapping_is_explicit_and_mode_compatible(self) -> None:
        protocol, suite, _ = _specs()
        missing_payload = _payload("deployment.json")
        missing_payload["clock_mappings"] = []
        with self.assertRaises(CompilationError) as missing:
            compile_suite(
                protocol,
                suite,
                DeploymentSpec.from_payload(missing_payload),
                _registry(),
            )
        self.assertTrue(
            any(value.code == "clock.mapping_missing" for value in missing.exception.diagnostics)
        )

        posthoc_payload = _payload("deployment.json")
        posthoc_payload["clock_mappings"][0]["strategy"] = "posthoc"
        with self.assertRaises(CompilationError) as posthoc:
            compile_suite(
                protocol,
                suite,
                DeploymentSpec.from_payload(posthoc_payload),
                _registry(),
            )
        self.assertEqual(
            next(value for value in posthoc.exception.diagnostics if value.code == "clock.mapping_mode").path,
            "$.deployment.clock_mappings[0].strategy",
        )

    def test_future_dependent_filter_is_rejected_in_causal_and_accepted_retrospectively(self) -> None:
        protocol_payload = _payload("protocol.json")
        suite_payload = _payload("suite.json")
        suite_payload["components"][1]["plugin_id"] = "eegle.processing.retrospective_sos"
        suite_payload["components"][1]["config"] = {
            "sos": [[1.0, 0.0, 0.0, 1.0, 0.0, 0.0]]
        }
        suite = SuiteSpec.from_payload(suite_payload)
        deployment = DeploymentSpec.from_payload(_payload("deployment.json"))

        with self.assertRaises(CompilationError) as raised:
            compile_suite(ProtocolSpec.from_payload(protocol_payload), suite, deployment, _registry())
        self.assertTrue(any(value.code == "plugin.resolve" for value in raised.exception.diagnostics))

        protocol_payload["execution_mode"] = "retrospective"
        retrospective = compile_suite(
            ProtocolSpec.from_payload(protocol_payload), suite, deployment, _registry()
        )
        self.assertEqual(retrospective.plan.execution_mode, ExecutionMode.RETROSPECTIVE)

    def test_explain_and_diff_classify_scientific_and_operational_changes(self) -> None:
        protocol, suite, deployment = _specs()
        registry = _registry()
        baseline = compile_suite(protocol, suite, deployment, registry)
        overlay = SuiteOverlay(
            overlay_id="overlay.threshold",
            component_config={"model.primary": {"threshold": 0.4}},
        )
        changed_suite = compose_suite(suite, (overlay,))
        changed_deployment_payload = deployment.to_payload()
        changed_deployment_payload["deployment_id"] = "deployment.alternate"
        changed_deployment_payload["storage"][0]["uri"] = "memory://alternate/evidence"
        changed = compile_suite(
            protocol,
            changed_suite,
            DeploymentSpec.from_payload(changed_deployment_payload),
            registry,
        )

        explanation = explain_plan(baseline.plan)
        difference = diff_plans(baseline.plan, changed.plan)

        self.assertEqual(explanation.component_order, baseline.graph.component_order)
        self.assertFalse(difference.equivalent)
        materiality = {value.materiality.value for value in difference.changes}
        self.assertEqual(materiality, {"scientific", "operational"})
        self.assertTrue(any(value.path == "$.components.model.primary" for value in difference.changes))
        self.assertTrue(any(value.path == "$.spec_hashes.deployment" for value in difference.changes))

    def test_lock_tampering_is_detected(self) -> None:
        protocol, suite, deployment = _specs()
        compiled = compile_suite(protocol, suite, deployment, _registry())

        tampered = deepcopy(compiled.lock.to_payload())
        tampered["component_hashes"]["model.primary"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "lock hash mismatch"):
            ExecutionLock.from_payload(tampered)

    def test_plan_and_lock_files_round_trip_atomically(self) -> None:
        protocol, suite, deployment = _specs()
        compiled = compile_suite(protocol, suite, deployment, _registry())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_plan(root / "execution.plan.json", compiled.plan)
            write_lock(root / "execution.lock.json", compiled.lock)

            restored_plan = read_plan(root / "execution.plan.json")
            restored_lock = read_lock(root / "execution.lock.json")

        self.assertEqual(restored_plan.plan_hash, compiled.plan.plan_hash)
        self.assertEqual(restored_lock.lock_hash, compiled.lock.lock_hash)
        restored_lock.verify_plan(restored_plan)


if __name__ == "__main__":
    unittest.main()
