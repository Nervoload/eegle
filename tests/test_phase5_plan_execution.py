from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from eegle._domain import (
    ComponentKind,
    Determinism,
    EquivalenceLevel,
    ExecutionMode,
    WorkStatus,
)
from eegle.compiler import CompilationError, compile_suite
from eegle.plugins import (
    PluginCapabilities,
    PluginDescriptor,
    PluginRegistry,
    PortSpec,
    StateBehavior,
)
from eegle.plugins.builtins import builtin_plugin_descriptors
from eegle.recording import (
    ArtifactReference,
    EvidenceReader,
    Session,
    persist_engine_run,
)
from eegle.replay import BundleReplayRunner
from eegle.runtime import (
    ConfirmSingleOperatorTransition,
    ExecutionEngine,
    EngineStatus,
    PlanConstructionError,
)
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec
from eegle.streams import (
    ContentKind,
    DenseSampleBatch,
    RateModel,
    SparseEvent,
    SparseEventBatch,
    StreamSpec,
    TimePoint,
)
from tests.fixtures.phase5_model_components import (
    compile_phase5_suite as compile_suite,
    register_phase5_plugins,
)


FIXTURE = Path(__file__).parent / "fixtures" / "migration" / "phase5_simulated"
CALIBRATION_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "migration"
    / "phase5_calibration_multistream"
)
DENSE = "eegle.dense_sample_batch.v1"
SPARSE = "eegle.sparse_event_batch.v1"


def _external_artifact(artifact_id: str = "artifact.calibration") -> ArtifactReference:
    return ArtifactReference(
        artifact_id=artifact_id,
        role="calibration",
        uri=f"memory://fixture/{artifact_id}",
        digest="sha256:" + "0" * 64,
        media_type="application/json",
        size_bytes=0,
    )


def _payload(name: str) -> dict:
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


def _calibration_payload(name: str) -> dict:
    return json.loads((CALIBRATION_FIXTURE / name).read_text(encoding="utf-8"))


def _registry() -> PluginRegistry:
    value = PluginRegistry()
    value.register_builtins()
    register_phase5_plugins(value)
    return value


def _dense_packet(
    batch_id: str,
    available_seconds: float,
    *,
    stream_id: str = "stream.neural",
    sequence_start: int = 0,
) -> DenseSampleBatch:
    return DenseSampleBatch(
        batch_id=batch_id,
        stream_id=stream_id,
        stream_revision=1,
        sequence_start=sequence_start,
        channel_ids=("channel.c3", "channel.c4"),
        values=np.asarray(
            [[-1.0, 0.0], [0.5, 1.0], [1.5, 2.0], [2.0, 3.0]],
            dtype=np.float64,
        ),
        received_time=TimePoint(available_seconds - 0.001, "boundary.clock"),
        available_time=TimePoint(available_seconds, "boundary.clock"),
        first_sample_time=TimePoint(sequence_start * 0.01, "device.clock"),
        sample_period_seconds=0.01,
    )


def _with_packets(deployment: dict, *packets: DenseSampleBatch) -> dict:
    value = deepcopy(deployment)
    value["component_bindings"][0]["config"]["packets"] = [
        packet.to_payload() for packet in packets
    ]
    return value


def _recording_suite(*, transform: bool = False) -> dict:
    value = _payload("suite.json")
    source = deepcopy(value["components"][0])
    sink = {
        "component_id": "sink.dense",
        "kind": "sink",
        "plugin_id": "eegle.recording.dense_sink",
        "version_spec": "~=0.1.0",
        "config": {},
    }
    if transform:
        identity = deepcopy(value["components"][1])
        value["components"] = [source, identity, sink]
        value["routes"] = [
            {
                "route_id": "route.source_transform",
                "source": {"component": "source.neural", "port": "samples"},
                "target": {"component": "transform.identity", "port": "samples"},
            },
            {
                "route_id": "route.transform_sink",
                "source": {"component": "transform.identity", "port": "samples"},
                "target": {"component": "sink.dense", "port": "records"},
            },
        ]
        components = ["source.neural", "transform.identity", "sink.dense"]
    else:
        value["components"] = [source, sink]
        value["routes"] = [
            {
                "route_id": "route.source_sink",
                "source": {"component": "source.neural", "port": "samples"},
                "target": {"component": "sink.dense", "port": "records"},
            }
        ]
        components = ["source.neural", "sink.dense"]
    value["phases"] = [
        {
            "phase_id": "phase.record",
            "components": components,
            "transitions": [],
            "retry_limit": 0,
            "resume_policy": "restart",
            "operator_confirmation": False,
        }
    ]
    value["initial_phase"] = "phase.record"
    return value


def _compile(
    suite_payload: dict,
    deployment_payload: dict,
    registry: PluginRegistry | None = None,
):
    selected_registry = registry or _registry()
    result = compile_suite(
        ProtocolSpec.from_payload(_payload("protocol.json")),
        SuiteSpec.from_payload(suite_payload),
        DeploymentSpec.from_payload(deployment_payload),
        selected_registry,
    )
    return result, selected_registry


class Phase5PlanExecutionTests(unittest.TestCase):
    def test_locked_classifier_graph_executes_without_manual_engine_components(self) -> None:
        suite = _payload("suite.json")
        suite["components"][3]["config"]["latency_seconds"] = 0.1
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.reference", 0.1),
            _dense_packet("batch.followup", 0.15, sequence_start=4),
        )
        compiled, registry = _compile(suite, deployment)

        run = ExecutionEngine.from_plan(compiled.plan, registry).run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        self.assertEqual(len(run.phase_attempts), 1)
        predictions = run.phase_results[0].emissions_from(
            "model.primary", "prediction"
        )
        self.assertEqual(len(predictions), 3)
        self.assertEqual(predictions[0].role_id, "primary")
        prediction_records = [
            record
            for record in run.evidence
            if record.record_type == "graph_emission"
            and record.payload["component_id"] == "model.primary"
        ]
        self.assertEqual(
            tuple(record.emitted_time for record in prediction_records),
            tuple(value.available_time for value in predictions),
        )
        self.assertTrue(
            any(record.record_type == "graph_emission" for record in run.evidence)
        )

    def test_recording_only_and_preprocessing_only_graphs_require_no_model_or_policy(self) -> None:
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.record", 0.1),
        )
        compiled, registry = _compile(_recording_suite(), deployment)
        engine = ExecutionEngine.from_plan(compiled.plan, registry)

        recorded = engine.run()

        self.assertEqual(recorded.status, EngineStatus.COMPLETE)
        sink = engine.runtime.node("sink.dense").component
        self.assertEqual(tuple(value.batch_id for value in sink.records), ("batch.record",))

        compiled, registry = _compile(_recording_suite(transform=True), deployment)
        engine = ExecutionEngine.from_plan(compiled.plan, registry)
        processed = engine.run()

        self.assertEqual(processed.status, EngineStatus.COMPLETE)
        transformed = engine.runtime.node("sink.dense").component.records[0]
        self.assertNotEqual(transformed.batch_id, "batch.record")
        self.assertEqual(transformed.lineage.component_id, "transform.identity")
        self.assertEqual(
            transformed.lineage.clock_mapping_revisions,
            {"mapping.device.clock.to.boundary.clock": 1},
        )

    def test_calibration_model_phase_does_not_require_a_primary_or_policy(self) -> None:
        suite = _payload("suite.json")
        suite["components"] = suite["components"][:-1]
        suite["routes"] = suite["routes"][:-1]
        suite["phases"][0]["components"] = suite["phases"][0]["components"][:-1]
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.calibration", 0.1),
        )

        compiled, registry = _compile(suite, deployment)
        run = ExecutionEngine.from_plan(compiled.plan, registry).run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        prediction = run.phase_results[0].emissions_from(
            "model.primary", "prediction"
        )[0]
        self.assertEqual(prediction.role_id, "observer")

    def test_dense_and_sparse_sources_are_merged_by_availability_and_recorded(self) -> None:
        suite = _recording_suite()
        suite["streams"].append(
            {
                "stream_id": "stream.markers",
                "modality": "markers",
                "clock_id": "marker.clock",
                "contract": {"type_id": SPARSE},
            }
        )
        suite["components"].extend(
            [
                {
                    "component_id": "source.markers",
                    "kind": "source",
                    "plugin_id": None,
                    "stream_id": "stream.markers",
                    "config": {},
                },
                {
                    "component_id": "sink.markers",
                    "kind": "sink",
                    "plugin_id": "eegle.recording.sparse_sink",
                    "version_spec": "~=0.1.0",
                    "config": {},
                },
            ]
        )
        suite["routes"].append(
            {
                "route_id": "route.markers_sink",
                "source": {"component": "source.markers", "port": "events"},
                "target": {"component": "sink.markers", "port": "records"},
            }
        )
        suite["phases"][0]["components"].extend(
            ["source.markers", "sink.markers"]
        )
        sparse_stream = StreamSpec(
            stream_id="stream.markers",
            revision=1,
            modality="markers",
            content_kind=ContentKind.SPARSE_EVENTS,
            rate_model=RateModel.EVENT,
            clock_id="marker.clock",
        )
        sparse = SparseEventBatch(
            batch_id="batch.markers",
            stream_id="stream.markers",
            stream_revision=1,
            sequence_start=0,
            events=(
                SparseEvent(
                    event_id="event.start",
                    kind="task.start",
                    event_time=TimePoint(0.04, "marker.clock"),
                    source_time=TimePoint(0.04, "marker.clock"),
                    received_time=TimePoint(0.049, "boundary.clock"),
                    available_time=TimePoint(0.05, "boundary.clock"),
                    value={"trial": 1},
                ),
            ),
        )
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.dense", 0.1),
        )
        deployment["component_bindings"].append(
            {
                "component_id": "source.markers",
                "plugin_id": "eegle.sources.packet_sequence_sparse",
                "version_spec": "~=0.1.0",
                "placement": "in_process",
                "resource_ids": ["resource.markers"],
                "config": {
                    "stream_spec": sparse_stream.to_payload(),
                    "packets": [sparse.to_payload()],
                },
            }
        )
        deployment["resources"].append(
            {
                "resource_id": "resource.markers",
                "kind": "simulator",
                "selector": {"generator": "event_sequence"},
                "capabilities": ["deterministic"],
                "contract": {"type_id": SPARSE},
            }
        )
        deployment["stream_bindings"].append(
            {
                "stream_id": "stream.markers",
                "resource_id": "resource.markers",
                "selector": {},
            }
        )
        deployment["clock_mappings"].append(
            {
                "source_clock": "marker.clock",
                "target_clock": "boundary.clock",
                "strategy": "declared_affine",
                "maximum_uncertainty_seconds": 0.001,
            }
        )

        compiled, registry = _compile(suite, deployment)
        engine = ExecutionEngine.from_plan(compiled.plan, registry)
        run = engine.run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        self.assertEqual(
            tuple(value.batch_id for value in run.phase_results[0].admitted_inputs),
            ("batch.markers", "batch.dense"),
        )
        self.assertEqual(
            engine.runtime.node("sink.markers").component.records[0].batch_id,
            "batch.markers",
        )
        self.assertEqual(
            engine.runtime.node("sink.dense").component.records[0].batch_id,
            "batch.dense",
        )

    def test_durable_calibration_multistream_fixture_compiles_and_executes(self) -> None:
        deployment = _calibration_payload("deployment.json")
        deployment["component_bindings"][0]["config"]["packets"] = [
            _dense_packet("batch.fixture-neural", 0.1).to_payload()
        ]
        marker_stream = StreamSpec.from_payload(
            deployment["component_bindings"][1]["config"]["stream_spec"]
        )
        marker_batch = SparseEventBatch(
            batch_id="batch.fixture-markers",
            stream_id="stream.markers",
            stream_revision=1,
            sequence_start=0,
            events=(
                SparseEvent(
                    event_id="event.fixture-start",
                    kind="task.start",
                    event_time=TimePoint(0.04, "marker.clock"),
                    source_time=TimePoint(0.04, "marker.clock"),
                    received_time=TimePoint(0.049, "boundary.clock"),
                    available_time=TimePoint(0.05, "boundary.clock"),
                    value={"phase": "calibration"},
                ),
            ),
        )
        deployment["component_bindings"][1]["config"]["packets"] = [
            marker_batch.to_payload()
        ]
        registry = _registry()
        compiled = compile_suite(
            ProtocolSpec.from_payload(_calibration_payload("protocol.json")),
            SuiteSpec.from_payload(_calibration_payload("suite.json")),
            DeploymentSpec.from_payload(deployment),
            registry,
        )
        engine = ExecutionEngine.from_plan(compiled.plan, registry)

        run = engine.run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        self.assertEqual(run.artifact_ids, ("calibration.result",))
        self.assertEqual(
            tuple(value.batch_id for value in run.phase_results[0].admitted_inputs),
            ("batch.fixture-markers", "batch.fixture-neural"),
        )
        self.assertEqual(marker_stream.stream_id, "stream.markers")
        self.assertEqual(
            tuple(value.phase_id for value in run.phase_attempts),
            ("phase.calibrate", "phase.evaluate"),
        )

    def test_compiled_phase_machine_drives_transitions_artifacts_and_operator_gates(self) -> None:
        suite = _recording_suite()
        first_source = suite["components"][0]
        first_sink = suite["components"][1]
        second_source = deepcopy(first_source)
        second_source["component_id"] = "source.second"
        second_sink = deepcopy(first_sink)
        second_sink["component_id"] = "sink.second"
        suite["components"] = [first_source, first_sink, second_source, second_sink]
        suite["routes"] = [
            suite["routes"][0],
            {
                "route_id": "route.second_sink",
                "source": {"component": "source.second", "port": "samples"},
                "target": {"component": "sink.second", "port": "records"},
            },
        ]
        suite["phases"] = [
            {
                "phase_id": "phase.acquire",
                "components": ["source.neural", "sink.dense"],
                "transitions": [
                    {"target_phase": "phase.review", "condition": "complete"}
                ],
            },
            {
                "phase_id": "phase.review",
                "components": ["source.second", "sink.second"],
                "transitions": [],
                "required_artifacts": ["artifact.calibration"],
                "operator_confirmation": True,
            },
        ]
        suite["initial_phase"] = "phase.acquire"
        suite["artifacts"] = [
            {
                "artifact_id": "artifact.calibration",
                "role": "calibration",
                "media_type": "application/json",
                "expected_digest": "sha256:" + "0" * 64,
            }
        ]
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.acquire", 0.1),
        )
        second_binding = deepcopy(deployment["component_bindings"][0])
        second_binding["component_id"] = "source.second"
        second_binding["config"]["packets"] = [
            _dense_packet("batch.review", 0.2, sequence_start=4).to_payload()
        ]
        deployment["component_bindings"].append(second_binding)
        compiled, registry = _compile(suite, deployment)

        missing = ExecutionEngine.from_plan(compiled.plan, registry).run()
        self.assertEqual(missing.status, EngineStatus.BLOCKED)
        self.assertIn("artifact.calibration", missing.reason)

        gated = ExecutionEngine.from_plan(compiled.plan, registry).run(
            artifacts={"artifact.calibration": _external_artifact()}
        )
        self.assertEqual(gated.status, EngineStatus.BLOCKED)
        self.assertIn("operator confirmation", gated.reason)

        completed = ExecutionEngine.from_plan(compiled.plan, registry).run(
            artifacts={"artifact.calibration": _external_artifact()},
            operator=ConfirmSingleOperatorTransition(),
        )
        self.assertEqual(completed.status, EngineStatus.COMPLETE)
        self.assertEqual(
            tuple(value.phase_id for value in completed.phase_attempts),
            ("phase.acquire", "phase.review"),
        )
        self.assertEqual(completed.transitions[0].target_phase, "phase.review")

    def test_graph_produced_artifact_unlocks_next_phase_and_is_durable(self) -> None:
        suite = _payload("suite.json")
        suite["components"].append(
            {
                "component_id": "artifact.calibration",
                "kind": "artifact",
                "plugin_id": "eegle.artifacts.prediction_summary",
                "version_spec": "~=0.1.0",
                "config": {
                    "artifact_id": "calibration.snapshot",
                    "role": "calibration_snapshot",
                },
            }
        )
        suite["routes"].append(
            {
                "route_id": "route.model_artifact",
                "source": {"component": "model.primary", "port": "prediction"},
                "target": {"component": "artifact.calibration", "port": "prediction"},
            }
        )
        suite["components"].append(
            {
                "component_id": "sink.evaluation",
                "kind": "sink",
                "plugin_id": "eegle.recording.dense_sink",
                "version_spec": "~=0.1.0",
                "config": {},
            }
        )
        suite["routes"].append(
            {
                "route_id": "route.source_evaluation",
                "source": {"component": "source.neural", "port": "samples"},
                "target": {"component": "sink.evaluation", "port": "records"},
            }
        )
        suite["phases"] = [
            {
                "phase_id": "phase.calibrate",
                "components": [
                    "source.neural",
                    "transform.identity",
                    "window.continuous",
                    "model.primary",
                    "policy.observe",
                    "artifact.calibration",
                ],
                "transitions": [
                    {"target_phase": "phase.evaluate", "condition": "complete"}
                ],
            },
            {
                "phase_id": "phase.evaluate",
                "components": ["source.neural", "sink.evaluation"],
                "transitions": [],
                "required_artifacts": ["calibration.snapshot"],
            },
        ]
        suite["initial_phase"] = "phase.calibrate"
        suite["artifacts"] = [
            {
                "artifact_id": "calibration.snapshot",
                "role": "calibration_snapshot",
                "media_type": "application/json",
                "producer_phase": "phase.calibrate",
                "producer_component": "artifact.calibration",
                "producer_port": "artifact",
            }
        ]
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.calibration-artifact", 0.1),
        )
        compiled, registry = _compile(suite, deployment)
        engine = ExecutionEngine.from_plan(compiled.plan, registry)

        run = engine.run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        self.assertEqual(run.artifact_ids, ("calibration.snapshot",))
        publication = run.artifacts[0]
        self.assertEqual(publication.producer_component_id, "artifact.calibration")
        restored_publication = type(publication).from_payload(publication.to_payload())
        self.assertEqual(restored_publication, publication)
        self.assertEqual(restored_publication.to_payload(), publication.to_payload())
        self.assertTrue(
            any(value.record_type == "artifact_registered" for value in run.evidence)
        )
        publication_evidence = next(
            value
            for value in run.evidence
            if value.record_type == "graph_emission"
            and value.payload["component_id"] == "artifact.calibration"
        )
        self.assertNotIn("materialized_payload", publication_evidence.payload["value"])
        self.assertNotIn("materialized", publication_evidence.payload["value"])
        source_stream = engine.runtime.node("source.neural").component.stream_spec
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(
                Path(directory) / "session", session_id="session.phase5.artifact"
            )
            bundle = persist_engine_run(
                session, run, plan=compiled.plan, streams=(source_stream,)
            )
            report = EvidenceReader.open(session, bundle.bundle_id).verify()
            persisted = next(
                value
                for value in bundle.artifacts
                if value.artifact_id == "calibration.snapshot"
            )
            entry = session.artifacts.get(
                f"bundles/{bundle.bundle_id}", "calibration.snapshot"
            )

        self.assertTrue(report.valid)
        self.assertTrue(persisted.embedded)
        self.assertEqual(entry.lineage.component_id, "artifact.calibration")

    def test_failed_phase_restores_source_checkpoint_and_retries(self) -> None:
        class FailOnceTransform:
            def __init__(self) -> None:
                self.calls = 0

            def process(self, input_port, value, context):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("transient fixture failure")
                return {"samples": value}

        registry = _registry()
        registry.register(
            PluginDescriptor(
                plugin_id="fixture.fail_once",
                version="1.0.0",
                kind=ComponentKind.TRANSFORM,
                config_schema={
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                input_ports=(PortSpec("samples", DENSE),),
                output_ports=(PortSpec("samples", DENSE),),
                capabilities=PluginCapabilities(
                    supported_modes=frozenset({ExecutionMode.CAUSAL}),
                    determinism=Determinism.EXTERNAL,
                    equivalence=EquivalenceLevel.TRACE,
                    state_behavior=StateBehavior.EXTERNAL,
                ),
                factory=lambda config: FailOnceTransform(),
                implementation="tests.fixture:FailOnceTransform",
                distribution="tests",
            )
        )
        suite = _recording_suite(transform=True)
        suite["components"][1]["plugin_id"] = "fixture.fail_once"
        suite["components"][1]["version_spec"] = "==1.0.0"
        suite["components"][1]["config"] = {}
        suite["phases"][0]["retry_limit"] = 1
        suite["phases"][0]["resume_policy"] = "restart"
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.retry", 0.1),
        )

        compiled, _ = _compile(suite, deployment, registry)
        engine = ExecutionEngine.from_plan(compiled.plan, registry)
        run = engine.run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        self.assertEqual(
            tuple(value.result.status.value for value in run.phase_attempts),
            ("failed", "complete"),
        )
        self.assertEqual(
            engine.runtime.node("sink.dense").component.records[0].batch_id,
            "batch.retry",
        )
        self.assertTrue(any(value.record_type == "phase_retry" for value in run.evidence))

    def test_forbidden_resume_cannot_declare_retries(self) -> None:
        suite = _recording_suite()
        suite["phases"][0]["retry_limit"] = 1
        suite["phases"][0]["resume_policy"] = "forbidden"

        with self.assertRaises(CompilationError) as raised:
            _compile(suite, _payload("deployment.json"))

        self.assertTrue(
            any(
                value.code == "phase.retry_forbidden"
                for value in raised.exception.diagnostics
            )
        )

    def test_runtime_rejects_plugin_drift_from_the_locked_graph(self) -> None:
        compiled, _ = _compile(_recording_suite(), _payload("deployment.json"))
        drifted = PluginRegistry()
        for descriptor in builtin_plugin_descriptors():
            if descriptor.plugin_id == "eegle.recording.dense_sink":
                descriptor = replace(
                    descriptor,
                    implementation="tampered.package:DifferentSink",
                )
            drifted.register(descriptor)

        with self.assertRaisesRegex(PlanConstructionError, "locked plugin drift"):
            ExecutionEngine.from_plan(compiled.plan, drifted)

    def test_source_watermarks_hold_later_work_until_an_incomplete_source_advances(self) -> None:
        delayed_stream_payload = deepcopy(
            _payload("deployment.json")["component_bindings"][0]["config"]["stream_spec"]
        )
        delayed_stream_payload["stream_id"] = "stream.delayed"
        delayed_stream_spec = StreamSpec.from_payload(delayed_stream_payload)
        delayed_packet = _dense_packet(
            "batch.delayed",
            0.05,
            stream_id="stream.delayed",
        )

        class DelayedSource:
            def __init__(self) -> None:
                self.calls = 0
                self.stream_spec = delayed_stream_spec
                self.watermark = TimePoint(0.0, "boundary.clock")
                self.exhausted = False

            def read(self):
                self.calls += 1
                if self.calls == 1:
                    return None
                self.exhausted = True
                self.watermark = delayed_packet.available_time
                return delayed_packet

            def close(self):
                self.exhausted = True

        registry = _registry()
        registry.register(
            PluginDescriptor(
                plugin_id="fixture.delayed_source",
                version="1.0.0",
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
                ),
                factory=lambda config: DelayedSource(),
                implementation="tests.fixture:DelayedSource",
                distribution="tests",
            )
        )
        suite = _recording_suite()
        delayed_stream = deepcopy(suite["streams"][0])
        delayed_stream["stream_id"] = "stream.delayed"
        suite["streams"].append(delayed_stream)
        suite["components"].insert(
            1,
            {
                "component_id": "source.delayed",
                "kind": "source",
                "plugin_id": "fixture.delayed_source",
                "version_spec": "==1.0.0",
                "stream_id": "stream.delayed",
                "config": {},
            },
        )
        suite["routes"].append(
            {
                "route_id": "route.delayed_sink",
                "source": {"component": "source.delayed", "port": "samples"},
                "target": {"component": "sink.dense", "port": "records"},
            }
        )
        suite["phases"][0]["components"].insert(1, "source.delayed")
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.later", 0.1),
        )
        deployment["stream_bindings"].append(
            {
                "stream_id": "stream.delayed",
                "resource_id": "resource.simulator",
                "selector": {},
            }
        )

        compiled, _ = _compile(suite, deployment, registry)
        engine = ExecutionEngine.from_plan(compiled.plan, registry)
        run = engine.run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        self.assertEqual(
            tuple(value.batch_id for value in run.captured_packets),
            ("batch.delayed", "batch.later"),
        )

    def test_source_cannot_emit_before_its_declared_watermark(self) -> None:
        deployment = _payload("deployment.json")
        stream_spec = StreamSpec.from_payload(
            deployment["component_bindings"][0]["config"]["stream_spec"]
        )
        packet = _dense_packet("batch.before.watermark", 0.05)

        class RegressingSource:
            def __init__(self) -> None:
                self.stream_spec = stream_spec
                self.watermark = TimePoint(0.1, "boundary.clock")
                self.exhausted = False

            def read(self):
                self.exhausted = True
                return packet

            def close(self):
                self.exhausted = True

            def snapshot_state(self):
                return {"exhausted": self.exhausted}

            def restore_state(self, state):
                self.exhausted = bool(state["exhausted"])

        compiled, registry = _compile(_recording_suite(), deployment)
        engine = ExecutionEngine.from_plan(
            compiled.plan,
            registry,
            component_overrides={"source.neural": RegressingSource()},
        )
        run = engine.run()

        self.assertEqual(run.status, EngineStatus.FAILED)
        self.assertIn("before its declared watermark", run.failure or "")

    def test_component_deadline_suppresses_late_outputs_and_records_timeout(self) -> None:
        suite = _payload("suite.json")
        suite["components"][3]["config"]["latency_seconds"] = 0.1
        suite["validation"]["component_deadlines_seconds"] = {
            "model.primary": 0.05
        }
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.deadline", 0.1),
        )

        compiled, registry = _compile(suite, deployment)
        run = ExecutionEngine.from_plan(compiled.plan, registry).run()

        timed_out = tuple(
            value
            for value in run.work
            if value.component_id == "model.primary"
            and value.status == WorkStatus.TIMED_OUT
        )
        self.assertEqual(run.status, EngineStatus.COMPLETE)
        self.assertTrue(timed_out)
        self.assertTrue(all(value.reason_code == "deadline_exceeded" for value in timed_out))
        self.assertEqual(
            run.phase_results[0].emissions_from("model.primary", "prediction"),
            (),
        )

    def test_locked_queue_limit_fails_the_graph_with_a_precise_result(self) -> None:
        suite = _recording_suite(transform=True)
        suite["validation"]["max_pending_events"] = 1
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.queue.1", 0.1),
            _dense_packet("batch.queue.2", 0.2, sequence_start=4),
        )

        compiled, registry = _compile(suite, deployment)
        run = ExecutionEngine.from_plan(compiled.plan, registry).run()

        self.assertEqual(run.status, EngineStatus.FAILED)
        self.assertIn("max_pending_events=1", run.failure or "")

    def test_plan_run_persists_as_a_plan_bearing_evidence_bundle(self) -> None:
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.bundle", 0.1),
        )
        compiled, registry = _compile(_recording_suite(), deployment)
        engine = ExecutionEngine.from_plan(compiled.plan, registry)
        run = engine.run()
        source_stream = engine.runtime.node("source.neural").component.stream_spec

        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(
                Path(directory) / "session", session_id="session.phase5.plan"
            )
            bundle = persist_engine_run(
                session,
                run,
                plan=compiled.plan,
                streams=(source_stream,),
            )
            report = EvidenceReader.open(session, bundle.bundle_id).verify()

        self.assertTrue(report.valid)
        self.assertEqual(bundle.plan_hash, compiled.plan.plan_hash)
        self.assertEqual(len(bundle.execution_captures), 1)
        self.assertEqual(
            sum(value.role == "execution_plan" for value in bundle.artifacts),
            1,
        )

    def test_bundle_replay_reconstructs_the_locked_graph_without_an_engine_factory(self) -> None:
        deployment = _with_packets(
            _payload("deployment.json"),
            _dense_packet("batch.replay", 0.1),
        )
        compiled, registry = _compile(_recording_suite(transform=True), deployment)
        engine = ExecutionEngine.from_plan(compiled.plan, registry)
        run = engine.run()
        stream = engine.runtime.node("source.neural").component.stream_spec

        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(
                Path(directory) / "session", session_id="session.phase5.replay"
            )
            bundle = persist_engine_run(
                session,
                run,
                plan=compiled.plan,
                streams=(stream,),
            )
            replay = BundleReplayRunner(registry).run(
                EvidenceReader.open(session, bundle.bundle_id)
            )

        self.assertEqual(replay.result.status, EngineStatus.COMPLETE)
        self.assertTrue(replay.equivalence.equivalent, replay.equivalence.divergences)
        self.assertEqual(replay.result.plan_hash, compiled.plan.plan_hash)


if __name__ == "__main__":
    unittest.main()
