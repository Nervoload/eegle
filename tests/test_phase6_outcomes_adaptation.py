from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
from typing import Any, Mapping
import unittest

import numpy as np

from eegle._domain import ComponentKind, Determinism, EquivalenceLevel, ExecutionMode
from eegle.compiler import canonical_hash, compile_suite
from eegle.compiler.diagnostics import CompilationError
from eegle.models import (
    CalibrationArtifact,
    ModelContract,
    ModelImplementationRequirement,
    ModelInputContract,
    ModelManifest,
    ModelOutputContract,
    ModelResult,
    ModelStateBehavior,
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
from eegle.recording import (
    ArtifactReference,
    EvidenceReader,
    Sensitivity,
    Session,
    persist_engine_run,
)
from eegle.replay import BundleReplayRunner
from eegle.runtime import (
    AdaptationResult,
    EngineStatus,
    ExecutionEngine,
    Outcome,
    OutcomeDisposition,
    OutcomeReference,
    OutcomeReferenceKind,
    OutcomeUse,
    StateTransition,
    TransitionStatus,
)
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec
from eegle.streams import DenseSampleBatch, StreamSpec, TimePoint
from tests.test_phase5_plan_execution import _dense_packet, _payload


DENSE = "eegle.dense_sample_batch.v1"
OUTCOME = "eegle.outcome.v2"
EMPTY_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
}


class AdaptiveMeanModel:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.bias = float(config.get("initial_bias", 0.0))
        self.adaptation_mode = str(config.get("adaptation_mode", "applied"))

    def predict(self, packet: DenseSampleBatch, context: Any) -> ModelResult:
        return ModelResult({"score": float(np.mean(packet.values)) + self.bias})

    def adapt(
        self,
        prediction: Prediction,
        outcome: Outcome,
        context: Any,
    ) -> AdaptationResult:
        if self.adaptation_mode == "no_op":
            return AdaptationResult(TransitionStatus.NO_OP, reason="configured_no_op")
        if self.adaptation_mode == "rejected":
            return AdaptationResult(
                TransitionStatus.REJECTED, reason="configured_rejection"
            )
        self.bias += float(dict(outcome.value).get("delta", 1.0))
        if self.adaptation_mode == "fail_after_mutation":
            raise RuntimeError("deliberate adaptive failure")
        return AdaptationResult(
            TransitionStatus.APPLIED,
            metadata={"bias": self.bias},
        )

    def snapshot_state(self) -> Mapping[str, Any]:
        return {
            "schema": "fixture.phase6_adaptive_mean_state.v1",
            "bias": self.bias,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        if state.get("schema") != "fixture.phase6_adaptive_mean_state.v1":
            raise ValueError("unsupported adaptive mean state")
        self.bias = float(state["bias"])


class PredictionOutcomeResolver:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.mode = str(config.get("mode", "one"))
        self.delay = float(config.get("delay_seconds", 0.0))
        self.uses = frozenset(
            OutcomeUse(str(value))
            for value in config.get("permitted_uses", ("metrics", "adaptation"))
        )
        self.retrospective_only = bool(config.get("retrospective_only", False))

    def update(self, prediction: Prediction, context: Any) -> tuple[Outcome, ...]:
        if self.mode == "none":
            return ()
        reference = OutcomeReference(
            OutcomeReferenceKind.PREDICTION,
            prediction.prediction_id,
        )
        available = TimePoint(
            context.current_time.seconds + self.delay,
            context.current_time.clock_id,
        )

        def outcome(value: Mapping[str, Any]) -> Outcome:
            return Outcome(
                outcome_id=context.next_id("outcome"),
                subject_id="subject.fixture",
                source_id="source.behavior",
                value=value,
                event_time=context.current_time,
                available_time=available,
                permitted_uses=self.uses,
                references=(reference,),
                retrospective_only=self.retrospective_only,
            )

        first = outcome({"correct": True, "delta": 1.0})
        if self.mode == "duplicate":
            return first, outcome({"correct": True, "delta": 1.0})
        if self.mode == "dispute":
            return first, outcome({"correct": False, "delta": -1.0})
        return (first,)


def _capabilities(*, stateful: bool = False) -> PluginCapabilities:
    return PluginCapabilities(
        supported_modes=frozenset({ExecutionMode.CAUSAL}),
        determinism=Determinism.DETERMINISTIC,
        equivalence=EquivalenceLevel.BITWISE,
        state_behavior=(
            StateBehavior.SNAPSHOT_RESTORE if stateful else StateBehavior.STATELESS
        ),
    )


def _registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register_builtins()
    registry.register(
        PluginDescriptor(
            plugin_id="fixture.model.adaptive_mean",
            version="1.0.0",
            kind=ComponentKind.MODEL,
            config_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "initial_bias": {"type": "number"},
                    "adaptation_mode": {
                        "enum": [
                            "applied",
                            "no_op",
                            "rejected",
                            "fail_after_mutation",
                        ]
                    },
                },
                "additionalProperties": False,
            },
            input_ports=(PortSpec("samples", DENSE, required=False),),
            output_ports=(PortSpec("prediction", PREDICTION_RECORD_SCHEMA),),
            capabilities=_capabilities(stateful=True),
            factory=AdaptiveMeanModel,
            implementation="tests.phase6_outcomes:AdaptiveMeanModel",
            distribution="phase6-outcome-fixture",
        )
    )
    registry.register(
        PluginDescriptor(
            plugin_id="fixture.outcome.prediction",
            version="1.0.0",
            kind=ComponentKind.OUTCOME,
            config_schema={
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "mode": {"enum": ["one", "none", "duplicate", "dispute"]},
                    "delay_seconds": {"type": "number", "minimum": 0},
                    "permitted_uses": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {
                            "enum": ["metrics", "calibration", "adaptation", "policy"]
                        },
                    },
                    "retrospective_only": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            input_ports=(
                PortSpec("prediction", PREDICTION_RECORD_SCHEMA, required=False),
            ),
            output_ports=(PortSpec("outcome", OUTCOME, multiple=True),),
            capabilities=_capabilities(),
            factory=PredictionOutcomeResolver,
            implementation="tests.phase6_outcomes:PredictionOutcomeResolver",
            distribution="phase6-outcome-fixture",
        )
    )
    return registry


def _manifest() -> ModelManifest:
    return ModelManifest(
        model_id="model.adaptive-mean",
        model_version="1.0.0",
        contract=ModelContract(
            inputs=(ModelInputContract("samples", DENSE),),
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
            state=ModelStateContract(
                behavior=ModelStateBehavior.SNAPSHOT_RESTORE,
                state_schema_id="fixture.phase6_adaptive_mean_state.v1",
                adaptation_supported=True,
                state_affects_predictions=True,
                replay_equivalence=EquivalenceLevel.BITWISE,
            ),
        ),
        artifacts=(),
        implementations=(
            ModelImplementationRequirement("fixture.model.adaptive_mean", "~=1.0"),
        ),
    )


def _protocol() -> ProtocolSpec:
    return ProtocolSpec.from_payload(
        {
            "schema": "eegle.protocol_spec.v1",
            "protocol_id": "protocol.phase6-outcomes",
            "execution_mode": "causal",
            "claims": [
                {
                    "claim_id": "claim.outcome-state",
                    "statement": "Direct outcomes produce replayable state transitions.",
                }
            ],
            "metrics": [],
            "acceptance": [],
            "annotations": {},
        }
    )


def _suite(
    manifest: ModelManifest,
    *,
    mode: str = "one",
    delay_seconds: float = 0.0,
    ttl_seconds: float = 300.0,
    pending_limit: int = 128,
    overflow: str = "expire_oldest",
    adaptation: bool = False,
    adaptation_mode: str = "applied",
    timeout_seconds: float | None = None,
    outcome_permitted_uses: tuple[str, ...] = ("metrics", "adaptation"),
    retrospective_only: bool = False,
) -> SuiteSpec:
    source_component = deepcopy(_payload("suite.json")["components"][0])
    source_component["config"] = {}
    phase: dict[str, Any] = {
        "phase_id": "phase.run",
        "components": ["source.neural", "model.adaptive", "outcome.behavior"],
        "transitions": [],
        "resume_policy": "checkpoint",
    }
    if timeout_seconds is not None:
        phase["timeout_seconds"] = timeout_seconds
    return SuiteSpec.from_payload(
        {
            "schema": "eegle.suite_spec.v1",
            "suite_id": "suite.phase6-outcomes",
            "protocol_id": "protocol.phase6-outcomes",
            "streams": [_payload("suite.json")["streams"][0]],
            "components": [
                source_component,
                {
                    "component_id": "model.adaptive",
                    "kind": "model",
                    "plugin_id": "fixture.model.adaptive_mean",
                    "version_spec": "~=1.0",
                    "config": {"adaptation_mode": adaptation_mode},
                },
                {
                    "component_id": "outcome.behavior",
                    "kind": "outcome",
                    "plugin_id": "fixture.outcome.prediction",
                    "version_spec": "~=1.0",
                    "config": {
                        "mode": mode,
                        "delay_seconds": delay_seconds,
                        "permitted_uses": list(outcome_permitted_uses),
                        "retrospective_only": retrospective_only,
                    },
                    "outcome_uses": list(outcome_permitted_uses),
                },
            ],
            "routes": [
                {
                    "route_id": "route.source-model",
                    "source": {"component": "source.neural", "port": "samples"},
                    "target": {"component": "model.adaptive", "port": "samples"},
                },
                {
                    "route_id": "route.model-outcome",
                    "source": {"component": "model.adaptive", "port": "prediction"},
                    "target": {"component": "outcome.behavior", "port": "prediction"},
                },
            ],
            "phases": [phase],
            "initial_phase": "phase.run",
            "artifacts": [],
            "model_roles": [],
            "model_uses": [
                {
                    "component_id": "model.adaptive",
                    "manifest_digest": manifest.manifest_digest,
                    "role_id": "primary",
                    "comparison_group": None,
                }
            ],
            "outcome_expectations": [
                {
                    "expectation_id": "expectation.behavior",
                    "model_component_id": "model.adaptive",
                    "outcome_component_ids": ["outcome.behavior"],
                    "permitted_uses": ["metrics", "adaptation"],
                    "max_pending_predictions": pending_limit,
                    "prediction_ttl_seconds": ttl_seconds,
                    "overflow_disposition": overflow,
                }
            ],
            "adaptations": (
                [
                    {
                        "adaptation_id": "adaptation.behavior",
                        "expectation_id": "expectation.behavior",
                        "model_component_id": "model.adaptive",
                        "enabled_phases": ["phase.run"],
                    }
                ]
                if adaptation
                else []
            ),
            "scheduling": {},
            "scheduled_triggers": [],
            "state_triggers": [],
            "clock_policy": {"execution_clock_id": "boundary.clock"},
            "recording": {"execution_capture": True, "semantic_evidence": True},
            "validation": {"require_replay_equivalence": True},
        }
    )


def _deployment(
    packets: tuple[DenseSampleBatch, ...],
    *,
    adaptation: bool = False,
) -> DeploymentSpec:
    payload = deepcopy(_payload("deployment.json"))
    payload["deployment_id"] = "deployment.phase6-outcomes"
    payload["suite_id"] = "suite.phase6-outcomes"
    payload["component_bindings"] = [payload["component_bindings"][0]]
    payload["component_bindings"][0]["config"]["packets"] = [
        value.to_payload() for value in packets
    ]
    payload["permissions"] = (
        [
            {
                "permission_id": "permission.model-adaptation",
                "capability": "adaptation",
                "component_ids": ["model.adaptive"],
                "authorization_ref": "authorization.simulation",
                "operator_confirmation": False,
            }
        ]
        if adaptation
        else []
    )
    return DeploymentSpec.from_payload(payload)


def _compiled(
    packets: tuple[DenseSampleBatch, ...],
    **suite_options: Any,
):
    manifest = _manifest()
    registry = _registry()
    adaptation = bool(suite_options.get("adaptation", False))
    compiled = compile_suite(
        _protocol(),
        _suite(manifest, **suite_options),
        _deployment(packets, adaptation=adaptation),
        registry,
        model_manifests={manifest.manifest_digest: manifest},
    )
    return compiled, registry


def _statuses(run: Any) -> list[str]:
    return [
        str(value.payload["status"])
        for value in run.evidence
        if value.record_type == "outcome_disposition"
    ]


class Phase6OutcomeAdaptationTests(unittest.TestCase):
    def test_direct_outcome_duplicate_dispute_expiry_and_close_dispositions(self) -> None:
        packet = _dense_packet("batch.one", 0.0)
        duplicate, registry = _compiled((packet,), mode="duplicate")
        duplicate_run = ExecutionEngine.from_plan(duplicate.plan, registry).run()
        self.assertEqual(
            _statuses(duplicate_run),
            ["pending", "matched", "duplicate"],
        )

        dispute, registry = _compiled((packet,), mode="dispute")
        dispute_run = ExecutionEngine.from_plan(dispute.plan, registry).run()
        self.assertEqual(
            _statuses(dispute_run),
            ["pending", "matched", "disputed"],
        )

        expired, registry = _compiled(
            (packet,), mode="one", delay_seconds=1.0, ttl_seconds=0.1
        )
        expired_run = ExecutionEngine.from_plan(expired.plan, registry).run()
        self.assertEqual(
            _statuses(expired_run),
            ["pending", "expired", "unmatched"],
        )

        missing, registry = _compiled((packet,), mode="none")
        missing_run = ExecutionEngine.from_plan(missing.plan, registry).run()
        self.assertEqual(_statuses(missing_run), ["pending", "pending_at_close"])
        dispositions = [
            OutcomeDisposition.from_payload(value.payload)
            for value in missing_run.evidence
            if value.record_type == "outcome_disposition"
        ]
        self.assertFalse(dispositions[0].terminal)
        self.assertTrue(dispositions[1].terminal)

        rejected, registry = _compiled(
            (packet,), outcome_permitted_uses=("policy",)
        )
        rejected_run = ExecutionEngine.from_plan(rejected.plan, registry).run()
        self.assertEqual(
            _statuses(rejected_run),
            ["pending", "rejected", "pending_at_close"],
        )

        retrospective, registry = _compiled((packet,), retrospective_only=True)
        retrospective_run = ExecutionEngine.from_plan(
            retrospective.plan, registry
        ).run()
        self.assertEqual(
            _statuses(retrospective_run),
            ["pending", "retrospective_only"],
        )

    def test_bounded_overflow_and_timeout_cancellation_are_explicit(self) -> None:
        packets = (
            _dense_packet("batch.one", 0.0),
            _dense_packet("batch.two", 0.0, sequence_start=4),
        )
        oldest, registry = _compiled(
            packets,
            mode="none",
            pending_limit=1,
            overflow="expire_oldest",
        )
        oldest_run = ExecutionEngine.from_plan(oldest.plan, registry).run()
        self.assertEqual(
            _statuses(oldest_run),
            ["pending", "overflowed", "pending", "pending_at_close"],
        )

        newest, registry = _compiled(
            packets,
            mode="none",
            pending_limit=1,
            overflow="reject_newest",
        )
        newest_run = ExecutionEngine.from_plan(newest.plan, registry).run()
        self.assertEqual(
            _statuses(newest_run),
            ["pending", "overflowed", "pending_at_close"],
        )

        timed, registry = _compiled(
            (packets[0],),
            mode="one",
            delay_seconds=2.0,
            timeout_seconds=0.5,
        )
        timed_run = ExecutionEngine.from_plan(timed.plan, registry).run()
        self.assertEqual(timed_run.status, EngineStatus.TIMED_OUT)
        self.assertEqual(_statuses(timed_run), ["pending", "cancelled"])

    def test_adaptation_applies_noops_rejects_and_rolls_back_state(self) -> None:
        packet = _dense_packet("batch.adapt", 0.0)
        applied, registry = _compiled((packet,), adaptation=True)
        applied_engine = ExecutionEngine.from_plan(applied.plan, registry)
        applied_run = applied_engine.run()
        transitions = [
            StateTransition.from_payload(value.payload)
            for value in applied_run.evidence
            if value.record_type == "state_transition"
        ]
        self.assertEqual(
            [value.status for value in transitions],
            [TransitionStatus.REQUESTED, TransitionStatus.APPLIED],
        )
        self.assertEqual(
            applied_engine.runtime.node("model.adaptive").component.bias,
            1.0,
        )
        eligibility = next(
            value.payload
            for value in applied_run.evidence
            if value.record_type == "adaptation_eligibility"
        )
        self.assertEqual(eligibility["status"], "eligible")

        for mode, expected in (
            ("no_op", TransitionStatus.NO_OP),
            ("rejected", TransitionStatus.REJECTED),
        ):
            compiled, registry = _compiled(
                (packet,), adaptation=True, adaptation_mode=mode
            )
            engine = ExecutionEngine.from_plan(compiled.plan, registry)
            run = engine.run()
            statuses = [
                value.payload["status"]
                for value in run.evidence
                if value.record_type == "state_transition"
            ]
            self.assertEqual(statuses, ["requested", expected.value])
            self.assertEqual(engine.runtime.node("model.adaptive").component.bias, 0.0)

        failed, registry = _compiled(
            (packet,), adaptation=True, adaptation_mode="fail_after_mutation"
        )
        failed_engine = ExecutionEngine.from_plan(failed.plan, registry)
        failed_run = failed_engine.run()
        self.assertEqual(failed_run.status, EngineStatus.COMPLETE)
        self.assertEqual(
            [
                value.payload["status"]
                for value in failed_run.evidence
                if value.record_type == "state_transition"
            ],
            ["requested", "failed", "rolled_back"],
        )
        self.assertEqual(failed_engine.runtime.node("model.adaptive").component.bias, 0.0)

    def test_outcome_state_survives_checkpoint_and_adaptation_bundle_replay(self) -> None:
        packets = (
            _dense_packet("batch.before", 0.0),
            _dense_packet("batch.after", 1.0, sequence_start=4),
        )
        checkpointed, registry = _compiled(packets, mode="none")
        partial = ExecutionEngine.from_plan(checkpointed.plan, registry).run(
            checkpoint_after_inputs_by_phase={"phase.run": 1}
        )
        self.assertEqual(partial.status, EngineStatus.CHECKPOINTED)
        self.assertIsNotNone(partial.checkpoint)
        resumed = ExecutionEngine.from_plan(checkpointed.plan, registry).run(
            checkpoint=partial.checkpoint
        )
        self.assertEqual(resumed.status, EngineStatus.COMPLETE)
        self.assertEqual(
            _statuses(resumed),
            ["pending", "pending_at_close", "pending_at_close"],
        )

        replayed, registry = _compiled(packets, adaptation=True)
        engine = ExecutionEngine.from_plan(replayed.plan, registry)
        reference = engine.run()
        stream = engine.runtime.node("source.neural").component.stream_spec
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(
                Path(directory) / "session",
                session_id="session.phase6-outcome-replay",
            )
            bundle = persist_engine_run(
                session,
                reference,
                plan=replayed.plan,
                streams=(stream,),
            )
            replay = BundleReplayRunner(registry).run(
                EvidenceReader.open(session, bundle.bundle_id)
            )
        self.assertTrue(replay.equivalence.equivalent, replay.equivalence.divergences)
        self.assertEqual(
            replay.result.phase_results[0]
            .emissions_from("model.adaptive", "prediction")[1]
            .value["score"],
            reference.phase_results[0]
            .emissions_from("model.adaptive", "prediction")[1]
            .value["score"],
        )

    def test_compiler_permissions_and_calibration_artifact_are_explicit(self) -> None:
        manifest = _manifest()
        suite = _suite(manifest, adaptation=True)
        packet = _dense_packet("batch.compiler", 0.0)
        deployment = _deployment((packet,), adaptation=False)
        with self.assertRaises(CompilationError) as raised:
            compile_suite(
                _protocol(),
                suite,
                deployment,
                _registry(),
                model_manifests={manifest.manifest_digest: manifest},
            )
        self.assertIn(
            "adaptation.authorization",
            {value.code for value in raised.exception.diagnostics},
        )

        observer_payload = _suite(manifest).to_payload()
        observer_payload["model_uses"][0]["role_id"] = "observer"
        with self.assertRaises(CompilationError) as raised:
            compile_suite(
                _protocol(),
                SuiteSpec.from_payload(observer_payload),
                _deployment((packet,)),
                _registry(),
                model_manifests={manifest.manifest_digest: manifest},
            )
        self.assertIn(
            "outcome.role_permission",
            {value.code for value in raised.exception.diagnostics},
        )

        state = ArtifactReference(
            artifact_id="artifact.calibrated-state",
            role="model_initial_state",
            uri="artifact://artifact.calibrated-state",
            digest="sha256:" + "4" * 64,
            media_type="application/json",
            size_bytes=128,
            sensitivity=Sensitivity.INTERNAL,
        )
        calibration = CalibrationArtifact(
            calibration_id="calibration.session-one",
            algorithm_id="algorithm.fixture-calibration",
            model_id="model.adaptive-mean",
            state_reference=state,
            support_input_ids=("batch.support",),
            support_outcome_ids=("outcome.support",),
            produced_time=TimePoint(2.0, "boundary.clock"),
            provenance={"protocol_id": "protocol.phase6-outcomes"},
        )
        self.assertEqual(
            CalibrationArtifact.from_payload(calibration.to_payload()), calibration
        )
        tampered = calibration.to_payload()
        tampered["support_outcome_ids"] = ["outcome.tampered"]
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            CalibrationArtifact.from_payload(tampered)


if __name__ == "__main__":
    unittest.main()
