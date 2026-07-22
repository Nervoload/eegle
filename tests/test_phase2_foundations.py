from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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
from eegle.processing import (
    BoundedBuffer,
    CausalSosFilter,
    FiniteQualityGate,
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
from tests.fixtures.phase2_external_plugin import plugin as external_plugin


ROOT = Path(__file__).resolve().parents[1]


def _time(seconds: float, clock: str = "host.monotonic") -> TimePoint:
    return TimePoint(seconds, clock)


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
            source_clock_id="device.amp",
            target_clock_id="host.monotonic",
            offset_seconds=10.0,
            uncertainty_seconds=0.001,
            valid_source_start=0.0,
            valid_source_end=100.0,
        )
        stream = StreamSpec(
            stream_id="neural.primary",
            modality="opm-meg",
            content_kind=ContentKind.DENSE_SAMPLES,
            rate_model=RateModel.REGULAR,
            clock_id=clock.clock_id,
            channels=(
                ChannelSpec("sensor.001", "magnetometer", "tesla"),
                ChannelSpec("sensor.002", "magnetometer", "tesla"),
            ),
            sample_rate_hz=1000.0,
            missing_data_policy=MissingDataPolicy.VALIDITY_MASK,
        )
        values = np.array([[1.0, 2.0], [3.0, np.nan], [5.0, 6.0]], dtype=np.float64)
        mask = np.array([[True, True], [True, False], [True, True]])
        batch = DenseSampleBatch(
            batch_id="batch.1",
            stream_id=stream.stream_id,
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
        self.assertEqual(mapping.map_time(_time(2.0, "device.amp")), _time(12.0))
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
        batch = SparseEventBatch("events.1", "spikes", 5, (event,))
        metadata = MetadataEvent(
            event_id="montage.2",
            stream_id="neural.primary",
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
        lineage = Lineage("model.primary", ("window.1",), "1.0.0", prior)
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
        quality = FiniteQualityGate().evaluate(
            DenseSampleBatch(
                batch_id="batch.quality",
                stream_id="stream.quality",
                sequence_start=0,
                channel_ids=("c1",),
                values=np.ones((2, 1)),
                first_sample_time=_time(0.0, "device.clock"),
                sample_period_seconds=0.01,
                received_time=_time(1.0),
                available_time=_time(1.1),
            ),
            decision_id="quality.1",
            decided_time=_time(1.2),
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


class PluginAndProcessingTests(unittest.TestCase):
    def test_third_party_entry_point_resolves_validates_constructs_and_runs(self) -> None:
        class Distribution:
            name = "fixture-external-package"

        class EntryPoint:
            name = "fixture-scale"
            value = "tests.fixtures.phase2_external_plugin:plugin"
            dist = Distribution()

            @staticmethod
            def load():
                return external_plugin

        class EntryPoints(tuple):
            def select(self, **kwargs):
                return self if kwargs.get("group") == "eegle.plugins" else ()

        registry = PluginRegistry()
        with mock.patch(
            "eegle.plugins.registry.metadata.entry_points",
            return_value=EntryPoints((EntryPoint(),)),
        ):
            loaded = registry.load_entry_points()

        component = registry.create(
            "fixture.external.scale",
            {"scale": 2.5},
            ">=1,<2",
            mode=ExecutionMode.CAUSAL,
        )
        np.testing.assert_allclose(component.update(np.array([[1.0], [2.0]])), [[2.5], [5.0]])
        self.assertEqual(loaded, ("fixture.external.scale",))
        self.assertEqual(
            registry.resolve("fixture.external.scale").distribution,
            "fixture-external-package",
        )
        with self.assertRaisesRegex(ValueError, "required property"):
            registry.create("fixture.external.scale", {})

    def test_causal_filter_chunking_matches_one_pass_and_restores_state(self) -> None:
        sos = signal.butter(2, 0.2, output="sos")
        samples = np.linspace(-1.0, 1.0, 80).reshape(40, 2)
        expected = signal.sosfilt(sos, samples, axis=0)
        transform = CausalSosFilter(sos, channel_count=2)
        first = transform.update(samples[:17])
        snapshot = transform.snapshot_state()
        second = transform.update(samples[17:])
        restored = CausalSosFilter(sos, channel_count=2)
        restored.restore_state(snapshot)

        np.testing.assert_allclose(np.vstack([first, second]), expected)
        np.testing.assert_allclose(restored.update(samples[17:]), second)
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
