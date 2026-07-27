from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Mapping
import unittest

from eegle._domain import (
    ComponentKind,
    Determinism,
    EquivalenceLevel,
    ExecutionMode,
)
from eegle.compiler import compile_suite
from eegle.models import (
    CallableModel,
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
from eegle.runtime import ExecutionEngine, GraphInput
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec
from eegle.streams import TimePoint
from tests.fixtures.build_phase6_model_wheel import build_phase6_model_wheel


ROOT = Path(__file__).resolve().parents[1]
OBSERVATION_SCHEMA = "fixture.phase6_observation.v1"
EMPTY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class Observation:
    record_id: str
    value: float
    available_time: TimePoint
    # Deliberately untrusted metadata: the executor must derive admitted roots
    # from its own graph trace rather than accepting this claim.
    input_ids: tuple[str, ...] = ("forged.upstream",)
    schema: str = OBSERVATION_SCHEMA

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "record_id": self.record_id,
            "value": self.value,
            "available_time": self.available_time.to_payload(),
            "input_ids": list(self.input_ids),
        }


def _manifest() -> ModelManifest:
    return ModelManifest(
        model_id="model.callable-fixture",
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
                    uncertainty_schema={
                        "type": "object",
                        "required": ["standard_deviation"],
                        "properties": {"standard_deviation": {"type": "number"}},
                        "additionalProperties": False,
                    },
                    validity_schema={
                        "type": "object",
                        "required": ["valid"],
                        "properties": {"valid": {"type": "boolean"}},
                        "additionalProperties": False,
                    },
                    abstention_supported=True,
                ),
            ),
            state=ModelStateContract(),
        ),
        artifacts=(),
        implementations=(
            ModelImplementationRequirement("fixture.model.callable", "~=1.0"),
        ),
    )


def _protocol() -> ProtocolSpec:
    return ProtocolSpec.from_payload(
        {
            "schema": "eegle.protocol_spec.v1",
            "protocol_id": "protocol.phase6-runtime",
            "execution_mode": "causal",
            "claims": [
                {
                    "claim_id": "claim.runtime-owned-prediction",
                    "statement": "EEGle constructs canonical prediction evidence.",
                }
            ],
            "metrics": [],
            "acceptance": [],
            "annotations": {},
        }
    )


def _suite(manifest: ModelManifest, *, phase_timeout: float | None = None) -> SuiteSpec:
    phase: dict[str, Any] = {
        "phase_id": "phase.run",
        "components": ["model.callable"],
        "transitions": [],
    }
    if phase_timeout is not None:
        phase["timeout_seconds"] = phase_timeout
    return SuiteSpec.from_payload(
        {
            "schema": "eegle.suite_spec.v1",
            "suite_id": "suite.phase6-runtime",
            "protocol_id": "protocol.phase6-runtime",
            "streams": [],
            "components": [
                {
                    "component_id": "model.callable",
                    "kind": "model",
                    "plugin_id": "fixture.model.callable",
                    "version_spec": "~=1.0",
                    "role": "observer",
                    "config": {},
                }
            ],
            "routes": [],
            "phases": [phase],
            "initial_phase": "phase.run",
            "artifacts": [],
            "model_roles": [],
            "model_uses": [
                {
                    "component_id": "model.callable",
                    "manifest_digest": manifest.manifest_digest,
                    "role_id": "observer",
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


def _deployment() -> DeploymentSpec:
    return DeploymentSpec.from_payload(
        {
            "schema": "eegle.deployment_spec.v1",
            "deployment_id": "deployment.phase6-runtime",
            "suite_id": "suite.phase6-runtime",
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


def _descriptor(factory: Any) -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="fixture.model.callable",
        version="1.0.0",
        kind=ComponentKind.MODEL,
        config_schema=EMPTY_SCHEMA,
        input_ports=(PortSpec("observation", OBSERVATION_SCHEMA, required=False),),
        output_ports=(PortSpec("prediction", PREDICTION_RECORD_SCHEMA),),
        capabilities=PluginCapabilities(
            supported_modes=frozenset({ExecutionMode.CAUSAL}),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.BITWISE,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=factory,
        implementation="tests.phase6_runtime:CallableModel",
        distribution="phase6-runtime-fixture",
    )


def _compiled(
    factory: Any,
    *,
    delay: float = 0.0,
    component_deadline: float | None = None,
    phase_timeout: float | None = None,
):
    manifest = _manifest()
    registry = PluginRegistry()
    registry.register(_descriptor(factory))
    suite = _suite(manifest, phase_timeout=phase_timeout)
    if component_deadline is not None:
        payload = suite.to_payload()
        payload["validation"]["component_deadlines_seconds"] = {
            "model.callable": component_deadline
        }
        suite = SuiteSpec.from_payload(payload)
    compiled = compile_suite(
        _protocol(),
        suite,
        _deployment(),
        registry,
        model_manifests={manifest.manifest_digest: manifest},
    )
    return compiled, registry


def _result(item: Observation, *, delay: float = 0.0) -> ModelResult:
    return ModelResult(
        {"score": item.value * 2.0},
        uncertainty={"standard_deviation": 0.25},
        validity={"valid": True},
        completion_delay_seconds=delay,
    )


class InvalidModel:
    def predict(self, item: Observation, context: Any) -> Mapping[str, Any]:
        # A plugin cannot bypass the runtime by returning its own graph record.
        return {"score": item.value}


class Phase6ModelRuntimeTests(unittest.TestCase):
    def test_callable_result_becomes_plan_owned_prediction(self) -> None:
        compiled, registry = _compiled(
            lambda config: CallableModel(lambda item: _result(item))
        )
        run = ExecutionEngine.from_plan(compiled.plan, registry).run(
            inputs_by_phase={
                "phase.run": (
                    GraphInput(
                        "model.callable",
                        "observation",
                        Observation(
                            "observation.one",
                            3.0,
                            TimePoint(1.0, "boundary.clock"),
                        ),
                    ),
                )
            }
        )

        prediction = run.phase_results[0].emissions_from(
            "model.callable", "prediction"
        )[0]
        binding = compiled.plan.model_bindings[0]
        self.assertIsInstance(prediction, Prediction)
        self.assertEqual(prediction.value, {"score": 6.0})
        self.assertEqual(prediction.component_id, binding.component_id)
        self.assertEqual(prediction.plugin_id, binding.plugin_id)
        self.assertEqual(prediction.manifest_digest, binding.manifest_digest)
        self.assertEqual(prediction.contract_digest, binding.contract_digest)
        self.assertEqual(prediction.role_id, binding.role.role_id)
        self.assertEqual(prediction.input_ids, ("observation.one",))
        self.assertEqual(prediction.admitted_input_ids, ("observation.one",))
        self.assertEqual(Prediction.from_payload(prediction.to_payload()), prediction)
        dispositions = [
            value.payload
            for value in run.evidence
            if value.record_type == "model_result_disposition"
        ]
        self.assertEqual([value["status"] for value in dispositions], ["emitted"])
        self.assertEqual(dispositions[0]["result_digest"], prediction.result_digest)

    def test_non_result_and_invalid_contract_values_are_rejected(self) -> None:
        compiled, registry = _compiled(lambda config: InvalidModel())
        run = ExecutionEngine.from_plan(compiled.plan, registry).run(
            inputs_by_phase={
                "phase.run": (
                    GraphInput(
                        "model.callable",
                        "observation",
                        Observation(
                            "observation.invalid",
                            1.0,
                            TimePoint(0.0, "boundary.clock"),
                        ),
                    ),
                )
            }
        )
        self.assertEqual(run.status.value, "complete")
        disposition = next(
            value.payload
            for value in run.evidence
            if value.record_type == "model_result_disposition"
        )
        self.assertEqual(disposition["status"], "rejected")
        self.assertEqual(disposition["reason_code"], "invalid_result_type")
        role_disposition = next(
            value.payload
            for value in run.evidence
            if value.record_type == "model_role_failure_disposition"
        )
        self.assertEqual(role_disposition["disposition"], "reject_result")

        compiled, registry = _compiled(
            lambda config: CallableModel(
                lambda item: ModelResult({"unexpected": item.value})
            )
        )
        run = ExecutionEngine.from_plan(compiled.plan, registry).run(
            inputs_by_phase={
                "phase.run": (
                    GraphInput(
                        "model.callable",
                        "observation",
                        Observation(
                            "observation.bad-schema",
                            1.0,
                            TimePoint(0.0, "boundary.clock"),
                        ),
                    ),
                )
            }
        )
        disposition = next(
            value.payload
            for value in run.evidence
            if value.record_type == "model_result_disposition"
        )
        self.assertEqual(disposition["reason_code"], "invalid_value")

    def test_late_and_phase_cancelled_results_have_terminal_dispositions(self) -> None:
        factory = lambda config: CallableModel(lambda item: _result(item, delay=0.2))
        compiled, registry = _compiled(factory, component_deadline=0.1)
        late = ExecutionEngine.from_plan(compiled.plan, registry).run(
            inputs_by_phase={
                "phase.run": (
                    GraphInput(
                        "model.callable",
                        "observation",
                        Observation(
                            "observation.late",
                            1.0,
                            TimePoint(0.0, "boundary.clock"),
                        ),
                    ),
                )
            }
        )
        statuses = [
            value.payload["status"]
            for value in late.evidence
            if value.record_type == "model_result_disposition"
        ]
        self.assertEqual(statuses, ["late"])
        self.assertEqual(
            late.phase_results[0].emissions_from("model.callable", "prediction"),
            (),
        )

        compiled, registry = _compiled(factory, phase_timeout=0.1)
        cancelled = ExecutionEngine.from_plan(compiled.plan, registry).run(
            inputs_by_phase={
                "phase.run": (
                    GraphInput(
                        "model.callable",
                        "observation",
                        Observation(
                            "observation.cancelled",
                            1.0,
                            TimePoint(0.0, "boundary.clock"),
                        ),
                    ),
                )
            }
        )
        disposition = next(
            value.payload
            for value in cancelled.evidence
            if value.record_type == "model_result_disposition"
        )
        self.assertEqual(cancelled.status.value, "timed_out")
        self.assertEqual(disposition["status"], "cancelled")
        self.assertEqual(disposition["reason_code"], "phase_timed_out")

    def test_installed_external_stateful_model_wheel_runs_and_restores(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "phase6_external_model_dist"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel = build_phase6_model_wheel(fixture, root / "wheel")
            target = root / "installed"
            install = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--target",
                    str(target),
                    str(wheel),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            script = (fixture / "exercise.py").read_text(encoding="utf-8").replace(
                "__TARGET__", repr(str(target))
            )
            executed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertIn("phase6-external-stateful-ok", executed.stdout)


if __name__ == "__main__":
    unittest.main()
