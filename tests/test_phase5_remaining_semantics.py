from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from eegle._domain import WorkStatus
from eegle.compiler import compile_suite
from eegle.plugins import PluginRegistry
from eegle.recording import (
    ArtifactReference,
    EvidenceReader,
    Session,
    persist_engine_run,
)
from eegle.replay import BundleReplayRunner
from eegle.runtime import (
    ConfirmSingleOperatorTransition,
    EngineStatus,
    ExecutionEngine,
    read_checkpoint,
    write_checkpoint,
)
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec
from eegle.streams import DenseSampleBatch, SparseEvent, SparseEventBatch, TimePoint
from tests.test_phase5_plan_execution import (
    _dense_packet,
    _payload,
    _recording_suite,
    _with_packets,
)


FIXTURES = Path(__file__).parent / "fixtures" / "migration"


def _load(directory: str, name: str) -> dict:
    return json.loads((FIXTURES / directory / name).read_text(encoding="utf-8"))


def _compile_reference(directory: str, deployment: dict):
    registry = PluginRegistry()
    registry.register_builtins()
    result = compile_suite(
        ProtocolSpec.from_payload(_load(directory, "protocol.json")),
        SuiteSpec.from_payload(_load(directory, "suite.json")),
        DeploymentSpec.from_payload(deployment),
        registry,
    )
    return result, registry


class Phase5RemainingSemanticTests(unittest.TestCase):
    def test_delayed_outcome_scheduled_and_state_triggers_share_the_graph_queue(self) -> None:
        deployment = _load("phase5_delayed_adaptation", "deployment.json")
        packet = SparseEventBatch(
            batch_id="batch.outcome",
            stream_id="stream.outcomes",
            stream_revision=1,
            sequence_start=0,
            events=(
                SparseEvent(
                    event_id="event.outcome",
                    kind="trial.outcome",
                    event_time=TimePoint(0.2, "device.clock"),
                    received_time=TimePoint(0.499, "boundary.clock"),
                    available_time=TimePoint(0.5, "boundary.clock"),
                    value={
                        "subject_id": "subject.fixture",
                        "prediction_ids": ["prediction.primary"],
                        "value": {"correct": True},
                    },
                ),
            ),
        )
        deployment["component_bindings"][0]["config"]["packets"] = [
            packet.to_payload()
        ]
        compiled, registry = _compile_reference(
            "phase5_delayed_adaptation", deployment
        )
        engine = ExecutionEngine.from_plan(compiled.plan, registry)

        run = engine.run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        result = run.phase_results[0]
        self.assertEqual(len(result.emissions_from("outcome.delayed", "outcomes")), 1)
        self.assertEqual(len(result.emissions_from("adapter.counter", "transition")), 1)
        trigger_sink = engine.runtime.node("sink.triggers").component
        self.assertEqual(len(trigger_sink.records), 2)
        self.assertEqual(
            tuple(
                value.record_type
                for value in run.evidence
                if value.record_type in {"trigger_result", "state_trigger_scheduled"}
            ),
            ("trigger_result", "state_trigger_scheduled", "trigger_result"),
        )

    def test_event_windows_multirate_primary_shadow_and_authorized_action(self) -> None:
        deployment = _load("phase5_event_window_actions", "deployment.json")
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
        compiled, registry = _compile_reference(
            "phase5_event_window_actions", deployment
        )

        engine = ExecutionEngine.from_plan(compiled.plan, registry)
        run = engine.run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        result = run.phase_results[0]
        self.assertEqual(len(result.emissions_from("window.event", "windows")), 1)
        self.assertEqual(len(result.emissions_from("model.primary", "prediction")), 1)
        self.assertEqual(len(result.emissions_from("model.shadow", "prediction")), 1)
        receipts = result.emissions_from("actuator.simulated", "receipt")
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].status.value, "delivered")
        model_work = [
            value.component_id
            for value in result.work
            if value.component_id in {"model.primary", "model.shadow"}
        ]
        self.assertEqual(model_work, ["model.primary", "model.shadow"])
        self.assertTrue(run.phase_attempts[0].acceptance[0].passed)

        streams = tuple(
            engine.runtime.node(component_id).component.stream_spec
            for component_id in ("source.neural", "source.aux", "source.markers")
        )
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(
                Path(directory) / "session",
                session_id="session.phase5.event-window-actions",
            )
            bundle = persist_engine_run(
                session,
                run,
                plan=compiled.plan,
                streams=streams,
            )
            replay = BundleReplayRunner(registry).run(
                EvidenceReader.open(session, bundle.bundle_id)
            )
        self.assertEqual(replay.result.status, EngineStatus.COMPLETE)
        self.assertTrue(replay.equivalence.equivalent, replay.equivalence.divergences)
        replay_receipts = replay.result.phase_results[0].emissions_from(
            "actuator.simulated", "receipt"
        )
        self.assertEqual(
            tuple(value.to_payload() for value in replay_receipts),
            tuple(value.to_payload() for value in receipts),
        )

        shed_suite = _load("phase5_event_window_actions", "suite.json")
        shed_suite["scheduling"]["shadow_queue_limit"] = 0
        shed = compile_suite(
            ProtocolSpec.from_payload(
                _load("phase5_event_window_actions", "protocol.json")
            ),
            SuiteSpec.from_payload(shed_suite),
            DeploymentSpec.from_payload(deployment),
            registry,
        )
        shed_run = ExecutionEngine.from_plan(shed.plan, registry).run()
        self.assertEqual(shed_run.status, EngineStatus.COMPLETE)
        self.assertEqual(
            shed_run.phase_results[0].emissions_from("model.shadow", "prediction"),
            (),
        )
        self.assertTrue(
            any(
                value.component_id == "model.shadow"
                and value.status == WorkStatus.SKIPPED
                and value.reason_code == "shadow_queue_limit"
                for value in shed_run.work
            )
        )

    def test_checkpoint_roundtrip_restores_a_fresh_runtime_mid_phase(self) -> None:
        suite = _recording_suite()
        suite["phases"][0]["resume_policy"] = "checkpoint"
        suite["phases"][0]["operator_confirmation"] = True
        suite["phases"][0]["required_artifacts"] = ["artifact.calibration"]
        suite["artifacts"] = [
            {
                "artifact_id": "artifact.calibration",
                "role": "calibration_result",
                "media_type": "application/json",
                "expected_digest": "sha256:" + "0" * 64,
            }
        ]
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.before-checkpoint", 0.1),
            _dense_packet("batch.after-checkpoint", 0.2, sequence_start=4),
        )
        registry = PluginRegistry()
        registry.register_builtins()
        compiled = compile_suite(
            ProtocolSpec.from_payload(_payload("protocol.json")),
            SuiteSpec.from_payload(suite),
            DeploymentSpec.from_payload(deployment),
            registry,
        )
        calibration = ArtifactReference(
            artifact_id="artifact.calibration",
            role="calibration_result",
            uri="memory://checkpoint/calibration",
            digest="sha256:" + "0" * 64,
            media_type="application/json",
            size_bytes=0,
        )
        partial = ExecutionEngine.from_plan(compiled.plan, registry).run(
            artifacts={calibration.artifact_id: calibration},
            operator=ConfirmSingleOperatorTransition(),
            checkpoint_after_inputs_by_phase={"phase.record": 1},
        )
        self.assertEqual(partial.status, EngineStatus.CHECKPOINTED)
        self.assertIsNotNone(partial.checkpoint)

        with tempfile.TemporaryDirectory() as directory:
            path = write_checkpoint(
                Path(directory) / "checkpoint.json", partial.checkpoint  # type: ignore[arg-type]
            )
            checkpoint = read_checkpoint(path)
            resumed = ExecutionEngine.from_plan(compiled.plan, registry).run(
                checkpoint=checkpoint
            )
            tampered = json.loads(path.read_text(encoding="utf-8"))
            tampered["state"]["phase_id"] = "phase.tampered"
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                read_checkpoint(path)

        self.assertEqual(resumed.status, EngineStatus.COMPLETE)
        self.assertIn(calibration.artifact_id, resumed.artifact_ids)
        self.assertEqual(
            tuple(value.batch_id for value in partial.captured_packets),
            ("batch.before-checkpoint",),
        )
        self.assertEqual(
            tuple(value.batch_id for value in resumed.captured_packets),
            ("batch.after-checkpoint",),
        )
        self.assertGreater(resumed.evidence[0].sequence, partial.evidence[-1].sequence)

    def test_timeout_acceptance_and_nonfatal_backpressure_are_terminally_explicit(self) -> None:
        timeout_suite = _recording_suite()
        timeout_suite["phases"][0]["timeout_seconds"] = 0.05
        deployment = _with_packets(
            _payload("deployment.json"), _dense_packet("batch.timeout", 0.1)
        )
        registry = PluginRegistry()
        registry.register_builtins()
        timed = compile_suite(
            ProtocolSpec.from_payload(_payload("protocol.json")),
            SuiteSpec.from_payload(timeout_suite),
            DeploymentSpec.from_payload(deployment),
            registry,
        )
        timeout_run = ExecutionEngine.from_plan(timed.plan, registry).run()
        self.assertEqual(timeout_run.status, EngineStatus.TIMED_OUT)

        acceptance_suite = _recording_suite()
        acceptance_suite["phases"][0]["acceptance_criteria"] = [
            "criterion.minimum_inputs"
        ]
        acceptance_protocol = _payload("protocol.json")
        acceptance_protocol["metrics"] = [
            {"metric_id": "metric.inputs", "measure": "input_count", "parameters": {}}
        ]
        acceptance_protocol["acceptance"] = [
            {
                "criterion_id": "criterion.minimum_inputs",
                "metric_id": "metric.inputs",
                "operator": "gte",
                "value": 2,
            }
        ]
        accepted = compile_suite(
            ProtocolSpec.from_payload(acceptance_protocol),
            SuiteSpec.from_payload(acceptance_suite),
            DeploymentSpec.from_payload(deployment),
            registry,
        )
        acceptance_run = ExecutionEngine.from_plan(accepted.plan, registry).run()
        self.assertEqual(acceptance_run.status, EngineStatus.FAILED)
        self.assertFalse(acceptance_run.phase_attempts[0].acceptance[0].passed)

        pressure_suite = _recording_suite()
        pressure_suite["components"].append(
            {
                "component_id": "sink.second",
                "kind": "sink",
                "plugin_id": "eegle.recording.dense_sink",
                "version_spec": "~=0.1.0",
                "config": {},
            }
        )
        pressure_suite["routes"].append(
            {
                "route_id": "route.source_second",
                "source": {"component": "source.neural", "port": "samples"},
                "target": {"component": "sink.second", "port": "records"},
            }
        )
        pressure_suite["phases"][0]["components"].append("sink.second")
        pressure_suite["validation"]["max_pending_events"] = 1
        pressure_suite["scheduling"] = {
            "backpressure": "reject_newest",
            "primary_first": True,
            "shadow_queue_limit": None,
            "shadow_failure": "continue",
        }
        pressured = compile_suite(
            ProtocolSpec.from_payload(_payload("protocol.json")),
            SuiteSpec.from_payload(pressure_suite),
            DeploymentSpec.from_payload(deployment),
            registry,
        )
        pressure_run = ExecutionEngine.from_plan(pressured.plan, registry).run()
        self.assertEqual(pressure_run.status, EngineStatus.COMPLETE)
        skipped = [value for value in pressure_run.work if value.status == WorkStatus.SKIPPED]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].reason_code, "queue_full")

    def test_compiler_rejects_unpermissioned_adaptation_and_defaults_actions_to_observe_only(self) -> None:
        delayed = _load("phase5_delayed_adaptation", "deployment.json")
        delayed["permissions"] = []
        with self.assertRaisesRegex(Exception, "adaptation permission"):
            _compile_reference("phase5_delayed_adaptation", delayed)

        actions = _load("phase5_event_window_actions", "deployment.json")
        actions["permissions"] = []
        compiled, _ = _compile_reference("phase5_event_window_actions", actions)
        self.assertEqual(compiled.plan.action_grants, ())

        invalid_suite = _load("phase5_event_window_actions", "suite.json")
        invalid_suite["components"][4]["action_capabilities"] = [
            "simulated.feedback"
        ]
        invalid_registry = PluginRegistry()
        invalid_registry.register_builtins()
        with self.assertRaisesRegex(Exception, "only actuator components"):
            compile_suite(
                ProtocolSpec.from_payload(
                    _load("phase5_event_window_actions", "protocol.json")
                ),
                SuiteSpec.from_payload(invalid_suite),
                DeploymentSpec.from_payload(
                    _load("phase5_event_window_actions", "deployment.json")
                ),
                invalid_registry,
            )


if __name__ == "__main__":
    unittest.main()
