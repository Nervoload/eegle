from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

import numpy as np

from eegle.actions import (
    ActionCancellation,
    ActionDisposition,
    ActionRequest,
    AuthorizationEvaluation,
    AuthorizationRequest,
    AuthorizationResult,
    AuthorizationStatus,
)
from eegle._domain import (
    ComponentKind,
    Determinism,
    EquivalenceLevel,
    ExecutionMode,
)
from eegle.compiler import CompilationError, ExecutionPlan, compile_suite
from eegle.compiler.lock import canonical_hash
from eegle.plugins import (
    PluginCapabilities,
    PluginDescriptor,
    PluginRegistry,
    PortSpec,
    StateBehavior,
)
from eegle.replay import ReplayActionSafetyError, ReplayRunner
from eegle.runtime import EngineStatus, ExecutionEngine
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec
from eegle.streams import DenseSampleBatch, SparseEvent, SparseEventBatch, TimePoint
from tests.fixtures.phase5_model_components import (
    compile_phase5_suite as compile_suite,
    register_phase5_plugins,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "migration"
    / "phase5_event_window_actions"
)


def _payload(name: str) -> dict:
    with (FIXTURE / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _registry(*descriptors: PluginDescriptor) -> PluginRegistry:
    registry = PluginRegistry()
    registry.register_builtins()
    register_phase5_plugins(registry)
    for descriptor in descriptors:
        registry.register(descriptor)
    return registry


def _inject_packets(deployment: dict) -> None:
    neural = DenseSampleBatch(
        batch_id="batch.neural",
        stream_id="stream.neural",
        stream_revision=1,
        sequence_start=0,
        channel_ids=("channel.c3", "channel.c4"),
        values=np.ones((8, 2), dtype=np.float64),
        received_time=TimePoint(0.079, "boundary.clock"),
        available_time=TimePoint(0.08, "boundary.clock"),
        first_sample_time=TimePoint(0.0, "device.clock"),
        sample_period_seconds=0.01,
    )
    auxiliary = DenseSampleBatch(
        batch_id="batch.aux",
        stream_id="stream.aux",
        stream_revision=1,
        sequence_start=0,
        channel_ids=("channel.resp",),
        values=np.asarray([[0.1], [0.2]], dtype=np.float64),
        received_time=TimePoint(0.049, "boundary.clock"),
        available_time=TimePoint(0.05, "boundary.clock"),
        first_sample_time=TimePoint(0.0, "aux.clock"),
        sample_period_seconds=0.1,
    )
    markers = SparseEventBatch(
        batch_id="batch.markers",
        stream_id="stream.markers",
        stream_revision=1,
        sequence_start=0,
        events=(
            SparseEvent(
                event_id="event.target",
                kind="stimulus.target",
                event_time=TimePoint(0.02, "device.clock"),
                received_time=TimePoint(0.029, "boundary.clock"),
                available_time=TimePoint(0.03, "boundary.clock"),
                value={"trial": 1},
            ),
        ),
    )
    packets = {
        "source.neural": neural,
        "source.aux": auxiliary,
        "source.markers": markers,
    }
    for binding in deployment["component_bindings"]:
        binding["config"]["packets"] = [
            packets[binding["component_id"]].to_payload()
        ]


def _compile(
    *,
    suite: dict | None = None,
    deployment: dict | None = None,
    registry: PluginRegistry | None = None,
):
    selected_suite = deepcopy(suite or _payload("suite.json"))
    selected_deployment = deepcopy(deployment or _payload("deployment.json"))
    return compile_suite(
        ProtocolSpec.from_payload(_payload("protocol.json")),
        SuiteSpec.from_payload(selected_suite),
        DeploymentSpec.from_payload(selected_deployment),
        registry or _registry(),
    )


def _run(
    *,
    suite: dict | None = None,
    deployment: dict | None = None,
    registry: PluginRegistry | None = None,
):
    selected_suite = deepcopy(suite or _payload("suite.json"))
    selected_deployment = deepcopy(deployment or _payload("deployment.json"))
    _inject_packets(selected_deployment)
    selected_registry = registry or _registry()
    compiled = _compile(
        suite=selected_suite,
        deployment=selected_deployment,
        registry=selected_registry,
    )
    return compiled, ExecutionEngine.from_plan(compiled.plan, selected_registry).run()


def _records(run, record_type: str) -> list[dict]:
    return [
        dict(value.payload)
        for value in run.evidence
        if value.record_type == record_type
    ]


class FailingProvider:
    def authorize(self, request, context):
        raise RuntimeError("interlock service unavailable")

    def resolve(self, request, pending, context):
        raise RuntimeError("interlock service unavailable")


class ParameterAwareProvider:
    def authorize(self, evaluation, context):
        intensity = float(evaluation.parameters["intensity"])
        return AuthorizationResult(
            AuthorizationStatus.AUTHORIZED
            if intensity <= 0.5
            else AuthorizationStatus.DENIED,
            reason="provider_parameter_bound",
            evidence={"evaluated_intensity": intensity},
        )

    def resolve(self, evaluation, pending, context):
        raise AssertionError("parameter-aware provider never returns pending")

def _failing_provider_descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="test.authorization.failing",
        version="1.0.0",
        kind=ComponentKind.AUTHORIZATION,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
        },
        input_ports=(),
        output_ports=(),
        capabilities=PluginCapabilities(
            supported_modes=frozenset(ExecutionMode),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.BITWISE,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=lambda config: FailingProvider(),
        implementation="tests.test_phase6_action_authorization:FailingProvider",
        distribution="tests",
    )


def _parameter_aware_provider_descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="test.authorization.parameter_aware",
        version="1.0.0",
        kind=ComponentKind.AUTHORIZATION,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
        },
        input_ports=(),
        output_ports=(),
        capabilities=PluginCapabilities(
            supported_modes=frozenset(ExecutionMode),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.BITWISE,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=lambda config: ParameterAwareProvider(),
        implementation="tests.test_phase6_action_authorization:ParameterAwareProvider",
        distribution="tests",
    )


class ForgedCommandPolicy:
    def decide(self, prediction, state, context):
        return None


def _forged_policy_descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="test.policy.forged_command",
        version="1.0.0",
        kind=ComponentKind.POLICY,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
        },
        input_ports=(PortSpec("prediction", "eegle.prediction.v2"),),
        output_ports=(PortSpec("request", "eegle.authorized_command.v1"),),
        capabilities=PluginCapabilities(
            supported_modes=frozenset(ExecutionMode),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.BITWISE,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=lambda config: ForgedCommandPolicy(),
        implementation="tests.test_phase6_action_authorization:ForgedCommandPolicy",
        distribution="tests",
    )


class ForgedActionRequestPolicy:
    def __init__(self, forgery: str) -> None:
        self.forgery = forgery

    def decide(self, prediction, state, context):
        return ActionRequest(
            request_id=context.next_id("action_request"),
            capability="simulated.feedback",
            requested_by=context.component_id,
            parameters={"intensity": 0.4},
            requested_time=context.current_time,
            available_time=context.current_time,
            prediction_id=(
                "prediction.forged"
                if self.forgery == "prediction"
                else prediction.prediction_id
            ),
            policy_state_hash=(
                canonical_hash({"forged": True})
                if self.forgery == "state"
                else canonical_hash(state)
            ),
        )


def _forged_request_policy_descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="test.policy.forged_request",
        version="1.0.0",
        kind=ComponentKind.POLICY,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "forgery": {"type": "string", "enum": ["prediction", "state"]}
            },
            "required": ["forgery"],
            "additionalProperties": False,
        },
        input_ports=(PortSpec("prediction", "eegle.prediction.v2"),),
        output_ports=(PortSpec("request", "eegle.action_request.v1"),),
        capabilities=PluginCapabilities(
            supported_modes=frozenset(ExecutionMode),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.BITWISE,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=lambda config: ForgedActionRequestPolicy(str(config["forgery"])),
        implementation=(
            "tests.test_phase6_action_authorization:ForgedActionRequestPolicy"
        ),
        distribution="tests",
    )


def _must_not_construct(config):
    raise AssertionError("replay attempted to construct a physical action service")


def _physical_authorization_descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="test.authorization.physical",
        version="1.0.0",
        kind=ComponentKind.AUTHORIZATION,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
        },
        input_ports=(),
        output_ports=(),
        capabilities=PluginCapabilities(
            supported_modes=frozenset(ExecutionMode),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.TRACE,
            state_behavior=StateBehavior.EXTERNAL,
        ),
        factory=_must_not_construct,
        implementation="tests.test_phase6_action_authorization:_must_not_construct",
        distribution="tests",
    )


def _physical_actuator_descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="test.actions.physical",
        version="1.0.0",
        kind=ComponentKind.ACTUATOR,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
        },
        input_ports=(PortSpec("request", "eegle.action_request.v1"),),
        output_ports=(PortSpec("receipt", "eegle.action_receipt.v1"),),
        capabilities=PluginCapabilities(
            supported_modes=frozenset(ExecutionMode),
            determinism=Determinism.NONDETERMINISTIC,
            equivalence=EquivalenceLevel.TRACE,
            state_behavior=StateBehavior.EXTERNAL,
        ),
        factory=_must_not_construct,
        implementation="tests.test_phase6_action_authorization:_must_not_construct",
        distribution="tests",
    )


class Phase6ActionAuthorizationTests(unittest.TestCase):
    def test_compiler_locks_provider_and_rejects_suite_authority(self) -> None:
        compiled = _compile()
        self.assertEqual(len(compiled.plan.authorization_providers), 1)
        provider = compiled.plan.authorization_providers[0]
        self.assertEqual(provider.provider_id, "authorization.simulation")
        self.assertTrue(provider.simulation_only)
        self.assertEqual(len(compiled.plan.action_grants), 1)
        self.assertEqual(
            ExecutionPlan.from_payload(compiled.plan.to_payload()), compiled.plan
        )

        missing = _payload("deployment.json")
        missing["permissions"][0]["authorization_ref"] = "authorization.missing"
        with self.assertRaisesRegex(CompilationError, "resolved deployment authorization provider"):
            _compile(deployment=missing)

        operator = _payload("deployment.json")
        operator["permissions"][0]["operator_confirmation"] = True
        with self.assertRaisesRegex(CompilationError, "explicit provider mechanism"):
            _compile(deployment=operator)

        physical_suite = _payload("suite.json")
        physical_suite["components"][6]["config"]["capability"] = "tms.pulse"
        physical_suite["components"][7]["action_capabilities"] = ["tms.pulse"]
        physical_deployment = _payload("deployment.json")
        physical_deployment["permissions"][0]["capability"] = "tms.pulse"
        with self.assertRaisesRegex(CompilationError, "simulation-only"):
            _compile(suite=physical_suite, deployment=physical_deployment)

        forged_suite = _payload("suite.json")
        forged_suite["components"][6]["plugin_id"] = "test.policy.forged_command"
        forged_suite["components"][6]["version_spec"] = "==1.0.0"
        forged_suite["components"][6]["config"] = {}
        forged_registry = _registry(_forged_policy_descriptor())
        with self.assertRaisesRegex(CompilationError, "authorized commands"):
            _compile(suite=forged_suite, registry=forged_registry)

    def test_authorized_and_observe_only_paths_are_distinct(self) -> None:
        compiled, authorized = _run()
        self.assertEqual(authorized.status, EngineStatus.COMPLETE)
        self.assertEqual(
            [value["status"] for value in _records(authorized, "authorization_decision")],
            ["authorized"],
        )
        commands = _records(authorized, "authorized_command")
        receipts = _records(authorized, "action_receipt")
        self.assertEqual(len(commands), 1)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(
            receipts[0]["receipt"]["authorization_decision_id"],
            commands[0]["authorization_decision_id"],
        )
        self.assertIn(
            "delivered",
            [value["status"] for value in _records(authorized, "action_disposition")],
        )
        for payload in _records(authorized, "action_disposition"):
            self.assertEqual(
                ActionDisposition.from_payload(payload).to_payload(),
                payload,
            )
        self.assertTrue(compiled.plan.action_grants)

        no_provider = _payload("deployment.json")
        no_provider["permissions"] = []
        no_provider["authorization_providers"] = []
        compiled, observed = _run(deployment=no_provider)
        self.assertEqual(observed.status, EngineStatus.COMPLETE)
        self.assertEqual(compiled.plan.action_grants, ())
        self.assertEqual(_records(observed, "authorized_command"), [])
        self.assertEqual(_records(observed, "action_receipt"), [])
        self.assertEqual(
            [value["status"] for value in _records(observed, "authorization_decision")],
            ["observe_only"],
        )

    def test_locked_parameter_timing_and_ttl_bounds_deny_without_submission(self) -> None:
        cases = (
            ("parameter", {"parameters": {"intensity": 1.5}}, {}, "parameter_above_maximum"),
            (
                "delivery",
                {"intended_delivery_delay_seconds": 2.0},
                {"maximum_delivery_delay_seconds": 1.0},
                "delivery_delay_exceeds",
            ),
            (
                "ttl",
                {"expires_after_seconds": 2.0},
                {"maximum_request_ttl_seconds": 1.0},
                "ttl_exceeds",
            ),
        )
        for name, policy_update, permission_update, reason in cases:
            with self.subTest(name=name):
                suite = _payload("suite.json")
                suite["components"][6]["config"].update(policy_update)
                deployment = _payload("deployment.json")
                deployment["permissions"][0].update(permission_update)
                _, run = _run(suite=suite, deployment=deployment)
                self.assertEqual(run.status, EngineStatus.COMPLETE)
                self.assertEqual(_records(run, "authorized_command"), [])
                self.assertEqual(_records(run, "action_receipt"), [])
                decisions = _records(run, "authorization_decision")
                self.assertEqual(decisions[-1]["status"], "denied")
                self.assertIn(reason, decisions[-1]["reason"])

    def test_provider_receives_immutable_parameters_while_evidence_keeps_only_digest(self) -> None:
        suite = _payload("suite.json")
        suite["components"][6]["config"]["parameters"] = {"intensity": 0.75}
        deployment = _payload("deployment.json")
        deployment["authorization_providers"][0].update(
            {
                "plugin_id": "test.authorization.parameter_aware",
                "version_spec": "==1.0.0",
                "config": {},
            }
        )
        registry = _registry(_parameter_aware_provider_descriptor())
        _, run = _run(suite=suite, deployment=deployment, registry=registry)

        request = _records(run, "authorization_request")[0]
        decision = _records(run, "authorization_decision")[-1]
        self.assertNotIn("parameters", request)
        self.assertIn("parameters_digest", request)
        self.assertEqual(decision["status"], "denied")
        self.assertEqual(decision["evidence"]["evaluated_intensity"], 0.75)
        self.assertEqual(_records(run, "authorized_command"), [])
        with self.assertRaisesRegex(ValueError, "parameter digest mismatch"):
            AuthorizationEvaluation(
                AuthorizationRequest.from_payload(request),
                {"intensity": 0.25},
            )

    def test_pending_authorization_resolves_or_expires_causally(self) -> None:
        deployment = _payload("deployment.json")
        deployment["authorization_providers"][0]["config"] = {
            "decision": "authorized",
            "decision_delay_seconds": 0.2,
        }
        deployment["permissions"][0]["maximum_decision_delay_seconds"] = 0.5
        _, resolved = _run(deployment=deployment)
        self.assertEqual(resolved.status, EngineStatus.COMPLETE)
        self.assertEqual(
            [value["status"] for value in _records(resolved, "authorization_decision")],
            ["pending", "authorized"],
        )
        self.assertEqual(len(_records(resolved, "action_receipt")), 1)

        expiring_suite = _payload("suite.json")
        expiring_suite["components"][6]["config"]["expires_after_seconds"] = 0.1
        expiring_deployment = deepcopy(deployment)
        expiring_deployment["permissions"][0]["maximum_request_ttl_seconds"] = 0.2
        _, expired = _run(
            suite=expiring_suite,
            deployment=expiring_deployment,
        )
        self.assertEqual(expired.status, EngineStatus.COMPLETE)
        self.assertEqual(
            [value["status"] for value in _records(expired, "authorization_decision")],
            ["pending", "expired"],
        )
        self.assertEqual(_records(expired, "action_receipt"), [])

    def test_pending_authorization_is_cancelled_on_phase_timeout(self) -> None:
        suite = _payload("suite.json")
        suite["phases"][0]["timeout_seconds"] = 0.15
        deployment = _payload("deployment.json")
        deployment["authorization_providers"][0]["config"] = {
            "decision": "authorized",
            "decision_delay_seconds": 1.0,
        }
        deployment["permissions"][0]["maximum_decision_delay_seconds"] = 2.0
        _, run = _run(suite=suite, deployment=deployment)
        self.assertEqual(run.status, EngineStatus.TIMED_OUT)
        cancellations = _records(run, "action_cancellation")
        self.assertEqual(len(cancellations), 1)
        self.assertEqual(
            ActionCancellation.from_payload(cancellations[0]).to_payload(),
            cancellations[0],
        )
        self.assertEqual(
            [value["status"] for value in _records(run, "authorization_decision")],
            ["pending", "cancelled"],
        )
        self.assertEqual(_records(run, "action_receipt"), [])

    def test_provider_failure_uses_locked_observe_only_fallback(self) -> None:
        deployment = _payload("deployment.json")
        deployment["authorization_providers"][0].update(
            {
                "plugin_id": "test.authorization.failing",
                "version_spec": "==1.0.0",
                "config": {},
                "failure_disposition": "observe_only",
            }
        )
        registry = _registry(_failing_provider_descriptor())
        _, run = _run(deployment=deployment, registry=registry)
        self.assertEqual(run.status, EngineStatus.COMPLETE)
        self.assertEqual(
            [value["status"] for value in _records(run, "authorization_decision")],
            ["failed", "observe_only"],
        )
        self.assertEqual(_records(run, "authorized_command"), [])
        self.assertEqual(_records(run, "action_receipt"), [])

    def test_runtime_rejects_policy_forged_prediction_and_state_lineage(self) -> None:
        registry = _registry(_forged_request_policy_descriptor())
        for forgery, expected in (
            ("prediction", "prediction identity differs"),
            ("state", "policy state differs"),
        ):
            with self.subTest(forgery=forgery):
                suite = _payload("suite.json")
                suite["components"][6].update(
                    {
                        "plugin_id": "test.policy.forged_request",
                        "version_spec": "==1.0.0",
                        "config": {"forgery": forgery},
                    }
                )
                _, run = _run(suite=suite, registry=registry)
                self.assertEqual(run.status, EngineStatus.FAILED)
                self.assertIn(expected, str(run.failure))

    def test_replay_refuses_physical_action_services_before_construction(self) -> None:
        _, reference = _run()
        suite = _payload("suite.json")
        suite["components"][6]["config"]["capability"] = "tms.pulse"
        suite["components"][7].update(
            {
                "plugin_id": "test.actions.physical",
                "version_spec": "==1.0.0",
                "action_capabilities": ["tms.pulse"],
            }
        )
        deployment = _payload("deployment.json")
        deployment["permissions"][0]["capability"] = "tms.pulse"
        deployment["authorization_providers"][0].update(
            {
                "plugin_id": "test.authorization.physical",
                "version_spec": "==1.0.0",
                "config": {},
            }
        )
        registry = _registry(
            _physical_authorization_descriptor(),
            _physical_actuator_descriptor(),
        )
        compiled = _compile(
            suite=suite,
            deployment=deployment,
            registry=registry,
        )

        with self.assertRaisesRegex(
            ReplayActionSafetyError,
            "replay refuses non-simulation action services",
        ):
            ReplayRunner(compiled.plan, registry, ()).run(reference)


if __name__ == "__main__":
    unittest.main()
