from __future__ import annotations

import unittest

import eegle.runtime as runtime_surface
from eegle._domain import (
    ComponentKind,
    Determinism,
    EquivalenceLevel,
    ExecutionMode,
)
from eegle.plugins import (
    PluginCapabilities,
    PluginDescriptor,
    PortSpec,
    StateBehavior,
)
from eegle.processing.quality import QualityDecision, QualityStatus
from eegle.runtime import EngineStatus, ExecutionEngine
from tests.test_phase5_plan_execution import (
    _compile,
    _dense_packet,
    _payload,
    _recording_suite,
    _registry,
    _with_packets,
)


DENSE_WINDOW = "eegle.dense_window.v1"
QUALITY_DECISION = "eegle.quality_decision.v1"


class RejectAllQuality:
    def process(self, input_port, value, context):
        if input_port != "window":
            raise ValueError(f"unexpected quality input port: {input_port}")
        return {
            "decision": QualityDecision(
                decision_id=context.next_id("quality"),
                item_id=value.window_id,
                gate_id="quality.reject_all",
                status=QualityStatus.REJECTED,
                decided_time=context.current_time,
                reasons=("fixture_rejection",),
            ),
            "accepted_window": None,
        }


def _rejecting_quality_descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="fixture.quality.reject_all",
        version="1.0.0",
        kind=ComponentKind.QUALITY,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        input_ports=(PortSpec("window", DENSE_WINDOW),),
        output_ports=(
            PortSpec("decision", QUALITY_DECISION),
            PortSpec("accepted_window", DENSE_WINDOW),
        ),
        capabilities=PluginCapabilities(
            supported_modes=frozenset({ExecutionMode.CAUSAL}),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.SEMANTIC,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=lambda config: RejectAllQuality(),
        implementation="tests.test_phase5_execution_semantics:RejectAllQuality",
        distribution="tests",
    )


class Phase5ImplementedSemanticAcceptanceTests(unittest.TestCase):
    def test_quality_rejection_prevents_model_and_policy_work(self) -> None:
        suite = _payload("suite.json")
        suite["components"].insert(
            3,
            {
                "component_id": "quality.reject",
                "kind": "quality",
                "plugin_id": "fixture.quality.reject_all",
                "version_spec": "==1.0.0",
                "config": {},
                "output_contracts": {
                    "accepted_window": {
                        "type_id": DENSE_WINDOW,
                        "unit": "uV",
                        "channel_count": 2,
                        "nominal_rate_hz": 100.0,
                        "window_samples": 4,
                    }
                },
            },
        )
        suite["routes"] = [
            value
            for value in suite["routes"]
            if value["route_id"] != "route.window_model"
        ]
        suite["routes"].extend(
            [
                {
                    "route_id": "route.window_quality",
                    "source": {
                        "component": "window.continuous",
                        "port": "windows",
                    },
                    "target": {"component": "quality.reject", "port": "window"},
                },
                {
                    "route_id": "route.quality_model",
                    "source": {
                        "component": "quality.reject",
                        "port": "accepted_window",
                    },
                    "target": {"component": "model.primary", "port": "window"},
                },
            ]
        )
        suite["phases"][0]["components"].insert(3, "quality.reject")
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.quality.reject", 0.1),
        )
        registry = _registry()
        registry.register(_rejecting_quality_descriptor())
        compiled, registry = _compile(suite, deployment, registry)
        run = ExecutionEngine.from_plan(compiled.plan, registry).run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        result = run.phase_results[0]
        decisions = result.emissions_from("quality.reject", "decision")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].status, QualityStatus.REJECTED)
        self.assertEqual(result.emissions_from("model.primary", "prediction"), ())
        self.assertFalse(
            any(
                value.component_id in {"model.primary", "policy.observe"}
                for value in run.work
            )
        )

    def test_pre_run_cancellation_is_terminal_and_does_not_execute_components(self) -> None:
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.cancelled", 0.1),
        )
        compiled, registry = _compile(_recording_suite(), deployment)
        engine = ExecutionEngine.from_plan(compiled.plan, registry)
        engine.cancel()

        run = engine.run()

        self.assertEqual(run.status, EngineStatus.CANCELLED)
        self.assertEqual(run.work, ())
        self.assertEqual(run.phase_results[0].emissions, ())

    def test_integrated_semantics_are_reexported_as_runtime_api(self) -> None:
        for name in (
            "EngineCheckpoint",
            "OutcomeRoutingPolicy",
            "ScheduledTrigger",
            "StateTriggerRule",
            "TriggerResult",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(runtime_surface, name))
        self.assertFalse(hasattr(runtime_surface, "SchedulingPolicy"))


if __name__ == "__main__":
    unittest.main()
