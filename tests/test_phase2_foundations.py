from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np
from scipy import signal

from eegle._domain import ComponentKind, ExecutionMode, Lineage
from eegle.actions import (
    ActionCommand,
    ActionReceipt,
    AuthorizationDecision,
    AuthorizationStatus,
    ReceiptStatus,
)
from eegle.compiler import (
    CanonicalizationError,
    ExecutionPlan,
    LockedPlugin,
    PlannedComponent,
    canonical_hash,
    canonical_json_bytes,
    content_hash,
)
from eegle.models.predictions import Prediction
from eegle.plugins import PluginRegistry
from eegle.plugins.testing import exercise_dense_transform
from eegle.processing import (
    BoundedBuffer,
    CausalSosFilter,
    FiniteQualityGate,
    IdentityTransform,
    QualityDecision,
    RetrospectiveSosFilter,
)
from eegle.recording import (
    ArtifactReference,
    EvidenceBundleManifest,
    EvidenceRecord,
    EvidenceStatus,
    FramedEvidenceWriter,
    Sensitivity,
    TruncatedEvidenceError,
    iter_framed_payloads,
)
from eegle.runtime import Outcome, OutcomeUse, Rejection, StateTransition, TransitionStatus
from eegle.streams import (
    ChannelSpec,
    ClockIdentity,
    ClockKind,
    ClockMapping,
    ContentKind,
    DenseSampleBatch,
    MetadataEvent,
    MissingDataPolicy,
    RateModel,
    SparseEvent,
    SparseEventBatch,
    StreamSpec,
    TimePoint,
)
from tests.fixtures.build_external_plugin_wheel import build_external_plugin_wheel


ROOT = Path(__file__).resolve().parents[1]


def _time(seconds: float, clock: str = "host.monotonic") -> TimePoint:
    return TimePoint(seconds, clock)


class FixtureExecutionContext:
    def __init__(
        self,
        *,
        component_id: str,
        current_time: TimePoint,
        component_version: str = "0.1.0",
        execution_mode: ExecutionMode = ExecutionMode.CAUSAL,
        clock_mapping_revisions: dict[str, int] | None = None,
    ) -> None:
        self.execution_id = "execution.phase2"
        self.component_id = component_id
        self.component_version = component_version
        self.execution_mode = execution_mode
        self.current_time = current_time
        self.clock_mapping_revisions = clock_mapping_revisions or {}
        self._next_sequence = 0

    def next_id(self, namespace: str) -> str:
        self._next_sequence += 1
        return f"{self.execution_id}.{self.component_id}.{namespace}.{self._next_sequence}"


def _dense_batch(
    values: np.ndarray,
    *,
    batch_id: str = "batch.input",
    sequence_start: int = 0,
    received: float = 1.0,
    available: float = 1.1,
) -> DenseSampleBatch:
    data = np.asarray(values)
    return DenseSampleBatch(
        batch_id=batch_id,
        stream_id="stream.synthetic",
        stream_revision=1,
        sequence_start=sequence_start,
        channel_ids=tuple(f"channel.{index}" for index in range(data.shape[1])),
        values=data,
        received_time=_time(received),
        available_time=_time(available),
        first_sample_time=_time(sequence_start * 0.01, "device.clock"),
        sample_period_seconds=0.01,
    )


class CanonicalAndPlanTests(unittest.TestCase):
    def test_canonical_hash_ignores_mapping_order_and_normalizes_unicode_and_zero(self) -> None:
        composed = {"b": -0.0, "a": "é"}
        decomposed = {"a": "e\u0301", "b": 0.0}

        self.assertEqual(canonical_json_bytes(composed), canonical_json_bytes(decomposed))
        self.assertEqual(canonical_hash(composed), canonical_hash(decomposed))

    def test_canonicalization_rejects_nonfinite_and_implicit_types(self) -> None:
        with self.assertRaises(CanonicalizationError):
            canonical_hash({"value": math.nan})
        with self.assertRaises(CanonicalizationError):
            canonical_hash({"path": Path("local-only")})

    def test_execution_plan_hash_round_trip_and_tamper_detection(self) -> None:
        descriptor_hash = canonical_hash({"plugin": "fixture", "version": "1.0.0"})
        spec_hash = canonical_hash({"suite": "synthetic"})
        plan = ExecutionPlan(
            plan_id="plan.synthetic.v1",
            execution_mode=ExecutionMode.CAUSAL,
            plugins=(
                LockedPlugin(
                    plugin_id="fixture.transform",
                    version="1.0.0",
                    kind=ComponentKind.TRANSFORM,
                    descriptor_hash=descriptor_hash,
                    distribution="fixture-package",
                    implementation="fixture:Transform",
                ),
            ),
            components=(
                PlannedComponent(
                    component_id="scale",
                    plugin_id="fixture.transform",
                    plugin_version="1.0.0",
                    config={"gain": 2.0, "offset": 0.0},
                ),
            ),
            spec_hashes={"suite": spec_hash},
            clock_policy={"late": "reject"},
            recording_policy={"admitted_inputs": True},
            validation_rules={"minimum_coverage": 0.95},
        )
        restored = ExecutionPlan.from_payload(plan.to_payload())

        self.assertEqual(restored.plan_hash, plan.plan_hash)
        tampered = json.loads(json.dumps(plan.to_payload()))
        tampered["components"][0]["config"]["gain"] = 3.0
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            ExecutionPlan.from_payload(tampered)


class StreamRecordTests(unittest.TestCase):
    def test_clock_stream_and_dense_batch_round_trip(self) -> None:
        clock = ClockIdentity("device.amp", ClockKind.DEVICE, provenance={"serial": "redacted"})
        mapping = ClockMapping(
            mapping_id="map.amp.host.1",
            revision=1,
            source_clock_id="device.amp",
            target_clock_id="host.monotonic",
            offset_seconds=10.0,
            available_time=_time(10.5),
            uncertainty_seconds=0.001,
            valid_source_start=0.0,
            valid_source_end=100.0,
        )
        stream = StreamSpec(
            stream_id="neural.primary",
            revision=3,
            modality="opm-meg",
            content_kind=ContentKind.DENSE_SAMPLES,
            rate_model=RateModel.REGULAR,
            clock_id=clock.clock_id,
            channels=(
                ChannelSpec("sensor.001", "magnetometer", "tesla"),
                ChannelSpec("sensor.002", "magnetometer", "tesla"),
            ),
            sample_rate_hz=1000.0,
            sample_dtype="float64",
            missing_data_policy=MissingDataPolicy.VALIDITY_MASK,
            coordinate_frame="device",
            geometry_reference="artifact.geometry.opm.3",
            geometry_revision="geometry.3",
        )
        values = np.array([[1.0, 2.0], [3.0, np.nan], [5.0, 6.0]], dtype=np.float64)
        mask = np.array([[True, True], [True, False], [True, True]])
        batch = DenseSampleBatch(
            batch_id="batch.1",
            stream_id=stream.stream_id,
            stream_revision=stream.revision,
            sequence_start=12,
            channel_ids=tuple(channel.channel_id for channel in stream.channels),
            values=values,
            validity_mask=mask,
            first_sample_time=_time(1.0, clock.clock_id),
            sample_period_seconds=0.001,
            received_time=_time(11.1),
            available_time=_time(11.2),
        )

        self.assertEqual(ClockIdentity.from_payload(clock.to_payload()), clock)
        self.assertEqual(ClockMapping.from_payload(mapping.to_payload()), mapping)
        self.assertEqual(
            mapping.map_time(_time(2.0, "device.amp"), as_of=_time(11.0)),
            _time(12.0),
        )
        with self.assertRaisesRegex(ValueError, "not available"):
            mapping.map_time(_time(2.0, "device.amp"), as_of=_time(10.0))
        self.assertEqual(StreamSpec.from_payload(stream.to_payload()), stream)
        restored = DenseSampleBatch.from_payload(batch.to_payload())
        self.assertEqual(restored, batch)
        np.testing.assert_allclose(restored.values, batch.values, equal_nan=True)
        np.testing.assert_array_equal(restored.validity_mask, batch.validity_mask)
        self.assertFalse(restored.values.flags.writeable)
        self.assertEqual(restored.sequence_end, 14)

    def test_sparse_and_metadata_records_round_trip(self) -> None:
        event = SparseEvent(
            event_id="spike.1",
            kind="spike",
            value={"unit": "cluster.7", "amplitude": 0.8},
            event_time=_time(1.0, "probe.clock"),
            source_time=_time(1.0, "probe.clock"),
            received_time=_time(2.0),
            available_time=_time(2.1),
        )
        batch = SparseEventBatch("events.1", "spikes", 2, 5, (event,))
        metadata = MetadataEvent(
            event_id="montage.2",
            stream_id="neural.primary",
            stream_revision=3,
            sequence=6,
            kind="sensor_geometry_change",
            event_time=_time(3.0, "device.amp"),
            received_time=_time(13.0),
            available_time=_time(13.1),
            metadata={"geometry_ref": "artifact.geometry.2"},
        )

        self.assertEqual(SparseEventBatch.from_payload(batch.to_payload()), batch)
        self.assertEqual(MetadataEvent.from_payload(metadata.to_payload()), metadata)

    def test_dense_batch_enforces_shape_timing_and_missingness(self) -> None:
        base = dict(
            batch_id="batch.bad",
            stream_id="stream.1",
            stream_revision=1,
            sequence_start=0,
            channel_ids=("c1",),
            received_time=_time(1.0),
            available_time=_time(1.1),
            first_sample_time=_time(0.0, "device.clock"),
            sample_period_seconds=0.01,
        )
        with self.assertRaisesRegex(ValueError, "validity_mask"):
            DenseSampleBatch(values=np.array([[np.nan]]), **base)
        with self.assertRaisesRegex(ValueError, "second dimension"):
            DenseSampleBatch(values=np.ones((2, 2)), **base)


class DomainRecordTests(unittest.TestCase):
    def test_prediction_quality_outcome_state_action_records_round_trip(self) -> None:
        prior = canonical_hash({"weights": [0.1]})
        resulting = canonical_hash({"weights": [0.2]})
        lineage = Lineage(
            component_id="model.primary",
            input_ids=("window.1",),
            component_version="1.0.0",
            state_hash=prior,
            latest_input_available_time=_time(3.9),
            clock_mapping_revisions={"map.amp.host": 2},
            stream_revisions={"neural.primary": 3},
        )
        prediction = Prediction(
            prediction_id="prediction.1",
            model_id="model.primary",
            role="primary",
            outputs={"label": "state.a", "probabilities": [0.8, 0.2]},
            produced_time=_time(4.0),
            available_time=_time(4.01),
            input_ids=("window.1",),
            lineage=lineage,
            confidence=0.8,
        )
        quality_batch = DenseSampleBatch(
                batch_id="batch.quality",
                stream_id="stream.quality",
                stream_revision=1,
                sequence_start=0,
                channel_ids=("c1",),
                values=np.ones((2, 1)),
                first_sample_time=_time(0.0, "device.clock"),
                sample_period_seconds=0.01,
                received_time=_time(1.0),
                available_time=_time(1.1),
            )
        quality = FiniteQualityGate().evaluate(
            quality_batch,
            FixtureExecutionContext(
                component_id="quality.finite",
                current_time=_time(1.2),
            ),
        )
        rejection = Rejection(
            "rejection.1",
            "work.1",
            "quality.finite",
            "quality",
            "nonfinite",
            _time(1.2),
            ("batch.quality",),
        )
        outcome = Outcome(
            "outcome.1",
            "trial.1",
            "behavior",
            {"correct": True},
            _time(5.0, "task.clock"),
            _time(6.0),
            frozenset({OutcomeUse.METRICS, OutcomeUse.ADAPTATION}),
            (prediction.prediction_id,),
        )
        transition = StateTransition(
            "transition.1",
            "model.primary",
            TransitionStatus.APPLIED,
            "online_update",
            _time(6.1),
            prior,
            resulting,
            (prediction.prediction_id, outcome.outcome_id),
        )
        command = ActionCommand(
            "command.1",
            "audio.tone",
            "policy.observe",
            {"frequency_hz": 440.0},
            _time(7.0),
            _time(7.0),
            prediction_id=prediction.prediction_id,
        )
        authorization = AuthorizationDecision(
            "authorization.1",
            command.command_id,
            "site.observe_only",
            AuthorizationStatus.OBSERVE_ONLY,
            _time(7.01),
            reason="deployment is observe-only",
        )
        receipt = ActionReceipt(
            "receipt.1",
            command.command_id,
            "actuator.simulated",
            ReceiptStatus.REJECTED,
            _time(7.02),
            authorization_decision_id=authorization.decision_id,
            details={"reason": "observe_only"},
        )

        self.assertEqual(Prediction.from_payload(prediction.to_payload()), prediction)
        self.assertEqual(QualityDecision.from_payload(quality.to_payload()), quality)
        self.assertEqual(Rejection.from_payload(rejection.to_payload()), rejection)
        self.assertEqual(Outcome.from_payload(outcome.to_payload()), outcome)
        self.assertEqual(StateTransition.from_payload(transition.to_payload()), transition)
        self.assertEqual(ActionCommand.from_payload(command.to_payload()), command)
        self.assertEqual(
            AuthorizationDecision.from_payload(authorization.to_payload()), authorization
        )
        self.assertEqual(ActionReceipt.from_payload(receipt.to_payload()), receipt)

    def test_prediction_rejects_input_lineage_that_was_not_yet_available(self) -> None:
        lineage = Lineage(
            component_id="model.primary",
            input_ids=("window.future",),
            latest_input_available_time=_time(4.1),
            stream_revisions={"neural.primary": 1},
        )
        with self.assertRaisesRegex(ValueError, "before its latest input"):
            Prediction(
                prediction_id="prediction.invalid",
                model_id="model.primary",
                role="primary",
                outputs={"label": "state.a"},
                produced_time=_time(4.0),
                available_time=_time(4.2),
                input_ids=("window.future",),
                lineage=lineage,
            )


class PluginAndProcessingTests(unittest.TestCase):
    def test_installed_external_wheel_discovers_and_satisfies_transform_contract(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "phase2_external_plugin_dist"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel = build_external_plugin_wheel(fixture, root / "wheel")
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
            script = r'''
import sys
import numpy as np

sys.path.insert(0, __TARGET__)

from eegle.plugins import ExecutionMode, PluginRegistry
from eegle.streams import DenseSampleBatch, TimePoint
from eegle.plugins.testing import exercise_dense_transform

class Context:
    execution_id = "execution.external"
    component_id = "scale.external"
    component_version = "1.2.0"
    execution_mode = ExecutionMode.CAUSAL
    current_time = TimePoint(1.2, "host.monotonic")
    clock_mapping_revisions = {"map.device.host": 1}
    sequence = 0

    def next_id(self, namespace):
        self.sequence += 1
        return f"external.{namespace}.{self.sequence}"

packet = DenseSampleBatch(
    batch_id="batch.external.input",
    stream_id="stream.external",
    stream_revision=4,
    sequence_start=0,
    channel_ids=("channel.0",),
    values=np.array([[1.0], [2.0]]),
    received_time=TimePoint(1.0, "host.monotonic"),
    available_time=TimePoint(1.1, "host.monotonic"),
    first_sample_time=TimePoint(0.0, "device.clock"),
    sample_period_seconds=0.01,
)
registry = PluginRegistry()
loaded = registry.load_entry_points()
assert "fixture.external.scale" in loaded, loaded
component = registry.create(
    "fixture.external.scale",
    {"scale": 2.5},
    ">=1,<2",
    mode=ExecutionMode.CAUSAL,
)
output = exercise_dense_transform(component, packet, Context())
np.testing.assert_allclose(output.values, [[2.5], [5.0]])
descriptor = registry.resolve("fixture.external.scale")
assert descriptor.distribution == "eegle-phase2-external-fixture", descriptor.distribution
print(descriptor.plugin_id, descriptor.version)
'''.replace("__TARGET__", repr(str(target)))
            executed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertIn("fixture.external.scale 1.2.0", executed.stdout)

    def test_builtin_factories_satisfy_packet_and_quality_contracts(self) -> None:
        registry = PluginRegistry()
        registered = registry.register_builtins()
        self.assertIn("eegle.processing.causal_sos", registered)
        packet = _dense_batch(np.array([[1.0], [2.0]]))
        identity = registry.create(
            "eegle.processing.identity",
            {},
            mode=ExecutionMode.CAUSAL,
        )
        identity_context = FixtureExecutionContext(
            component_id="transform.identity",
            current_time=_time(1.2),
            clock_mapping_revisions={"map.device.host": 1},
        )
        identity_output = exercise_dense_transform(identity, packet, identity_context)
        np.testing.assert_array_equal(identity_output.values, packet.values)
        self.assertEqual(
            identity_output.lineage.clock_mapping_revisions["map.device.host"],
            1,
        )
        with self.assertRaisesRegex(ValueError, "before it is available"):
            identity.update(
                packet,
                FixtureExecutionContext(
                    component_id="transform.identity",
                    current_time=_time(1.0),
                ),
            )

        quality = registry.create(
            "eegle.processing.finite_quality",
            {"minimum_valid_fraction": 1.0},
            mode=ExecutionMode.CAUSAL,
        )
        quality_context = FixtureExecutionContext(
            component_id="quality.finite",
            current_time=_time(1.3),
        )
        decision = quality.evaluate(identity_output, quality_context)
        self.assertEqual(decision.item_id, identity_output.batch_id)
        self.assertEqual(decision.decided_time, quality_context.current_time)

    def test_registry_rejects_factories_that_violate_role_or_state_claims(self) -> None:
        registry = PluginRegistry()
        registry.register_builtins()
        identity_descriptor = registry.resolve("eegle.processing.identity")
        registry.register(
            replace(
                identity_descriptor,
                plugin_id="fixture.invalid.transform",
                factory=lambda config: object(),
                distribution="fixture-invalid",
            )
        )
        with self.assertRaisesRegex(TypeError, "without callable update"):
            registry.create("fixture.invalid.transform", {})

        causal_descriptor = registry.resolve("eegle.processing.causal_sos")
        registry.register(
            replace(
                causal_descriptor,
                plugin_id="fixture.invalid.state",
                factory=lambda config: IdentityTransform(),
                distribution="fixture-invalid",
            )
        )
        with self.assertRaisesRegex(TypeError, "declares snapshot_restore"):
            registry.create(
                "fixture.invalid.state",
                {
                    "sos": signal.butter(2, 0.2, output="sos").tolist(),
                    "channel_count": 1,
                },
            )

    def test_causal_filter_chunking_matches_one_pass_and_restores_state(self) -> None:
        sos = signal.butter(2, 0.2, output="sos")
        samples = np.linspace(-1.0, 1.0, 80).reshape(40, 2)
        expected = signal.sosfilt(sos, samples, axis=0)
        registry = PluginRegistry()
        registry.register_builtins()
        config = {"sos": sos.tolist(), "channel_count": 2}
        transform = registry.create(
            "eegle.processing.causal_sos",
            config,
            mode=ExecutionMode.CAUSAL,
        )
        self.assertIsInstance(transform, CausalSosFilter)
        first_packet = _dense_batch(
            samples[:17],
            batch_id="batch.chunk.1",
            received=1.0,
            available=1.1,
        )
        second_packet = _dense_batch(
            samples[17:],
            batch_id="batch.chunk.2",
            sequence_start=17,
            received=1.3,
            available=1.4,
        )
        initial_state = transform.snapshot_state()
        with self.assertRaisesRegex(ValueError, "before it is available"):
            transform.update(
                first_packet,
                FixtureExecutionContext(
                    component_id="filter.causal",
                    current_time=_time(1.0),
                ),
            )
        self.assertEqual(transform.snapshot_state(), initial_state)
        first = exercise_dense_transform(
            transform,
            first_packet,
            FixtureExecutionContext(
                component_id="filter.causal",
                current_time=_time(1.2),
            ),
        )
        snapshot = transform.snapshot_state()
        second = exercise_dense_transform(
            transform,
            second_packet,
            FixtureExecutionContext(
                component_id="filter.causal",
                current_time=_time(1.5),
            ),
        )
        restored = registry.create(
            "eegle.processing.causal_sos",
            config,
            mode=ExecutionMode.CAUSAL,
        )
        restored.restore_state(snapshot)

        np.testing.assert_allclose(np.vstack([first.values, second.values]), expected)
        restored_second = exercise_dense_transform(
            restored,
            second_packet,
            FixtureExecutionContext(
                component_id="filter.causal",
                current_time=_time(1.5),
            ),
        )
        np.testing.assert_allclose(restored_second.values, second.values)
        self.assertEqual(restored_second.lineage.state_hash, second.lineage.state_hash)
        transform.capabilities.validate_mode(ExecutionMode.CAUSAL)

    def test_retrospective_filter_is_machine_rejected_for_causal_execution(self) -> None:
        transform = RetrospectiveSosFilter(signal.butter(2, 0.2, output="sos"))
        with self.assertRaisesRegex(ValueError, "does not support causal"):
            transform.capabilities.validate_mode(ExecutionMode.CAUSAL)

    def test_bounded_buffer_accounts_for_eviction_and_order(self) -> None:
        buffer = BoundedBuffer[str](2)
        self.assertIsNone(buffer.append(1, "one"))
        self.assertIsNone(buffer.append(2, "two"))
        evicted = buffer.append(3, "three")
        self.assertEqual((evicted.sequence, evicted.item), (1, "one"))
        self.assertEqual(buffer.evicted_count, 1)
        self.assertEqual([entry.item for entry in buffer], ["two", "three"])
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            buffer.append(3, "duplicate")


class EvidenceFoundationTests(unittest.TestCase):
    def test_framed_log_is_appendable_checksummed_and_truncation_aware(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.eegle"
            records = [
                EvidenceRecord("record.1", "prediction", 1, _time(1.0), {"value": 1}),
                EvidenceRecord("record.2", "outcome", 2, _time(2.0), {"value": 2}),
            ]
            with FramedEvidenceWriter(path) as writer:
                writer.append(records[0])
            with FramedEvidenceWriter(path) as writer:
                writer.append(records[1])
            loaded = [EvidenceRecord.from_payload(item) for item in iter_framed_payloads(path)]
            self.assertEqual(loaded, records)

            truncated = Path(tmp) / "truncated.eegle"
            truncated.write_bytes(path.read_bytes()[:-10])
            with self.assertRaises(TruncatedEvidenceError) as caught:
                tuple(iter_framed_payloads(truncated))
            self.assertGreater(caught.exception.last_complete_offset, 0)
            recovered = tuple(
                iter_framed_payloads(truncated, allow_truncated_final_frame=True)
            )
            self.assertEqual(len(recovered), 1)

    def test_evidence_manifest_round_trip_verifies_integrity(self) -> None:
        plan_hash = canonical_hash({"plan": "fixture"})
        log_bytes = b"fixture log"
        log = ArtifactReference(
            "artifact.log.1",
            "evidence_log",
            "records/primary.eegle",
            content_hash(log_bytes),
            "application/vnd.eegle.evidence-framed+json",
            len(log_bytes),
            Sensitivity.PSEUDONYMIZED,
            embedded=True,
        )
        manifest = EvidenceBundleManifest(
            "bundle.fixture.1",
            plan_hash,
            EvidenceStatus.COMPLETE,
            _time(1.0),
            (log,),
            completed_time=_time(2.0),
            last_sequence=2,
        )
        restored = EvidenceBundleManifest.from_payload(manifest.to_payload())
        self.assertEqual(restored, manifest)
        tampered = manifest.to_payload()
        tampered["last_sequence"] = 3
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            EvidenceBundleManifest.from_payload(tampered)


class PackageBoundaryTests(unittest.TestCase):
    def test_base_foundations_import_with_optional_packages_blocked(self) -> None:
        script = r'''
import importlib.abc
import sys

blocked = {"pylsl", "mne", "mne_lsl", "psychopy", "sklearn", "torch", "braindecode", "moabb", "matplotlib", "pandas", "pyriemann", "onnxruntime", "pyglet", "specparam"}
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in blocked:
            raise ImportError("blocked optional package: " + fullname)
        return None
sys.meta_path.insert(0, Blocker())

import eegle
import eegle.actions
import eegle.compiler
import eegle.models
import eegle.plugins
import eegle.processing
import eegle.recording
import eegle.runtime
import eegle.specs
import eegle.streams
assert "eegle.realtime.models" not in sys.modules
assert "eegle.integrations.task_environment" not in sys.modules
'''
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_foundation_sources_contain_no_reference_recipe_names_or_project_root(self) -> None:
        targets = (
            ROOT / "eegle" / "actions",
            ROOT / "eegle" / "compiler",
            ROOT / "eegle" / "plugins",
            ROOT / "eegle" / "processing",
            ROOT / "eegle" / "recording",
            ROOT / "eegle" / "runtime",
            ROOT / "eegle" / "specs",
        )
        forbidden = ("classify8", "attention8", "dsart", "go_nogo", "PROJECT_ROOT")
        for directory in targets:
            for path in directory.glob("*.py"):
                source = path.read_text(encoding="utf-8")
                for token in forbidden:
                    with self.subTest(path=path.name, token=token):
                        self.assertNotIn(token, source)

    def test_python_and_base_dependency_policy_is_declared(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('requires-python = ">=3.11"', pyproject)
        for dependency in ("numpy", "scipy", "jsonschema", "packaging"):
            self.assertRegex(pyproject, rf'"{dependency}>=[^\"]+"')

    def test_legacy_classifier_and_model_modules_no_longer_form_a_direct_cycle(self) -> None:
        classification = (ROOT / "eegle" / "realtime" / "classification.py").read_text(
            encoding="utf-8"
        )
        models = (ROOT / "eegle" / "realtime" / "models.py").read_text(encoding="utf-8")
        self.assertNotIn("eegle.realtime.models import", classification)
        self.assertIn("eegle.realtime.classification import", models)


if __name__ == "__main__":
    unittest.main()
