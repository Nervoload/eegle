from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import numpy as np

from eegle.compiler.lock import canonical_hash
from eegle.recording import (
    ArtifactLineage,
    EvidenceReader,
    EvidenceRecord,
    EvidenceStatus,
    EvidenceWriter,
    IntegrityIssueCode,
    IntegrityStatus,
    SampleStore,
    Sensitivity,
    Session,
    SessionPaths,
    SessionStatus,
    persist_engine_run,
    read_framed_sample_store,
    recover_framed_prefix,
)
from eegle.recording.stores import FramedSampleStore, SampleStorePurpose
from eegle.streams import (
    ChannelSpec,
    ContentKind,
    DenseSampleBatch,
    MetadataEvent,
    RateModel,
    SparseEvent,
    SparseEventBatch,
    StreamSpec,
    TimePoint,
)


def _time(seconds: float, clock: str = "host.virtual") -> TimePoint:
    return TimePoint(seconds, clock)


def _stream() -> StreamSpec:
    return StreamSpec(
        stream_id="stream.synthetic",
        revision=1,
        modality="synthetic",
        content_kind=ContentKind.DENSE_SAMPLES,
        rate_model=RateModel.REGULAR,
        clock_id="device.synthetic",
        channels=(ChannelSpec("signal.0", "signal", "a.u."),),
        sample_rate_hz=100.0,
        sample_dtype="float64",
    )


def _packet() -> DenseSampleBatch:
    return DenseSampleBatch(
        batch_id="batch.1",
        stream_id="stream.synthetic",
        stream_revision=1,
        sequence_start=0,
        channel_ids=("signal.0",),
        values=np.asarray([[1.0], [2.0]], dtype=np.float64),
        received_time=_time(0.9),
        available_time=_time(1.0),
        first_sample_time=_time(0.0, "device.synthetic"),
        sample_period_seconds=0.01,
    )


def _digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


class SessionAndArtifactStoreTests(unittest.TestCase):
    def test_namespaced_registry_and_compatibility_paths_are_manifest_backed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(
                Path(tmp) / "session",
                session_id="session.phase4",
                participant_pseudonym="participant.pseudo-1",
            )
            first = session.artifacts.register_bytes(
                "models/primary",
                "weights",
                "model_weights",
                b"primary",
                "application/octet-stream",
            )
            second = session.artifacts.register_bytes(
                "models/shadow",
                "weights",
                "model_weights",
                b"shadow",
                "application/octet-stream",
                lineage=ArtifactLineage(
                    component_id="trainer.shadow",
                    input_artifact_ids=("weights",),
                    input_digests=(first.digest,),
                ),
            )
            paths = SessionPaths.from_session(session)

            self.assertNotEqual(first.digest, second.digest)
            self.assertEqual(
                session.artifacts.get("models/shadow", "weights").lineage.input_digests,
                (first.digest,),
            )
            with self.assertRaisesRegex(ValueError, "namespace"):
                session.artifacts.register_bytes(
                    "models/../../escape",
                    "unsafe",
                    "fixture",
                    b"unsafe",
                    "application/octet-stream",
                )
            self.assertEqual(paths.realtime_engine_capture, session.root / "realtime/engine_input.bin")

            reopened = Session.open(session.root)
            self.assertEqual(reopened.artifacts.manifest, session.artifacts.manifest)
            self.assertEqual(reopened.session_id, "session.phase4")

    def test_existing_recipe_session_is_discovered_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "legacy"
            (root / "raw").mkdir(parents=True)
            (root / "raw" / "eeg.csv").write_text("time,Cz\n0,1\n", encoding="utf-8")
            (root / "manifest.json").write_text(
                '{"created_at":"2025-01-01T00:00:00","layout":"bcipy_style"}\n',
                encoding="utf-8",
            )
            before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

            session = Session.open(root)
            paths = SessionPaths.from_session(session)
            after = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

            self.assertTrue(session.legacy)
            self.assertTrue(session.read_only)
            self.assertEqual(session.status, SessionStatus.PARTIAL)
            self.assertEqual(paths.eeg_csv.read_text(encoding="utf-8"), "time,Cz\n0,1\n")
            self.assertEqual(before, after)
            with self.assertRaises(PermissionError):
                session.finalize()


class EvidenceBundleTests(unittest.TestCase):
    def test_bundle_keeps_execution_capture_and_external_raw_reference_independent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp) / "session", session_id="session.bundle")
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.1",
                plan_hash=canonical_hash({"plan": 1}),
                created_time=_time(0.0),
            )
            writer.append(EvidenceRecord("evidence.0", "run_started", 0, _time(0.0), {}))
            execution = writer.capture_packets((_stream(),), (_packet(),))
            external_raw = session.artifacts.register_external(
                "raw/source",
                "raw.recording.1",
                "archival_raw",
                "nwb://archive/session-1",
                _digest(b"external-raw-fixture"),
                "application/x-nwb",
                len(b"external-raw-fixture"),
                sensitivity=Sensitivity.RESTRICTED,
            )
            writer.add_raw_recording(external_raw)
            state = writer.snapshot_component(
                component_id="model.primary",
                component_version="1.0.0",
                sequence=0,
                captured_time=_time(1.0),
                state={"threshold": 0.5, "updates": 0},
            )
            bundle = writer.finalize(status=EvidenceStatus.COMPLETE, completed_time=_time(2.0))

            reader = EvidenceReader.open(Session.open(session.root), "bundle.1")
            report = reader.verify()
            captured = read_framed_sample_store(session.artifacts.resolve(execution))

            self.assertTrue(report.valid, report.issues)
            self.assertTrue(execution.embedded)
            self.assertFalse(external_raw.embedded)
            self.assertEqual(bundle.execution_captures, (execution,))
            self.assertEqual(bundle.raw_recordings, (external_raw,))
            self.assertEqual(captured.packets, (_packet(),))
            self.assertEqual(state.state_hash, state.artifact.digest)
            self.assertEqual(reader.records()[0].record_type, "run_started")

    def test_truncated_capture_returns_precise_recoverable_integrity_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp) / "session", session_id="session.truncated")
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.truncated",
                plan_hash=canonical_hash({"plan": "truncated"}),
                created_time=_time(0.0),
            )
            writer.append(EvidenceRecord("evidence.0", "run_started", 0, _time(0.0), {}))
            capture = writer.capture_packets((_stream(),), (_packet(),))
            writer.finalize(status=EvidenceStatus.COMPLETE, completed_time=_time(2.0))
            capture_path = session.artifacts.resolve(capture)
            capture_path.write_bytes(capture_path.read_bytes()[:-10])

            report = EvidenceReader.open(session, "bundle.truncated").verify()
            truncation = next(
                issue
                for issue in report.issues
                if issue.code == IntegrityIssueCode.TRUNCATED_FRAME_PAYLOAD
            )

            self.assertEqual(report.status, IntegrityStatus.RECOVERABLE)
            self.assertTrue(truncation.recoverable)
            self.assertIsNotNone(truncation.frame_offset)
            self.assertIsNotNone(truncation.last_complete_offset)
            self.assertGreater(int(truncation.expected), int(truncation.observed))
            recovered = recover_framed_prefix(capture_path, Path(tmp) / "recovered.eegle")
            self.assertEqual(recovered.status, IntegrityStatus.VALID)

    def test_capture_checksum_corruption_is_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp) / "session", session_id="session.corrupt")
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.corrupt",
                plan_hash=canonical_hash({"plan": "corrupt"}),
                created_time=_time(0.0),
            )
            writer.append(EvidenceRecord("evidence.0", "run_started", 0, _time(0.0), {}))
            capture = writer.capture_packets((_stream(),), (_packet(),))
            writer.finalize(status=EvidenceStatus.COMPLETE, completed_time=_time(2.0))
            capture_path = session.artifacts.resolve(capture)
            corrupted = bytearray(capture_path.read_bytes())
            corrupted[-1] ^= 0x01
            capture_path.write_bytes(corrupted)

            report = EvidenceReader.open(session, "bundle.corrupt").verify()

            self.assertEqual(report.status, IntegrityStatus.UNRECOVERABLE)
            self.assertIn(
                IntegrityIssueCode.CHECKSUM_MISMATCH,
                {issue.code for issue in report.issues},
            )

    def test_engine_result_persists_as_self_describing_bundle_with_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp) / "session", session_id="session.engine")
            plan_hash = canonical_hash({"plan": "engine"})
            evidence = (
                EvidenceRecord("evidence.0", "run_started", 0, _time(0.0), {}),
                EvidenceRecord(
                    "evidence.1",
                    "component_started",
                    1,
                    _time(0.1),
                    {"component_id": "model.primary", "component_version": "1.2.3"},
                ),
                EvidenceRecord(
                    "evidence.2",
                    "component_state",
                    2,
                    _time(1.0),
                    {"component_id": "model.primary", "state": {"threshold": 0.25}},
                ),
            )
            result = SimpleNamespace(
                execution_id="execution.1",
                plan_hash=plan_hash,
                status=SimpleNamespace(value="complete"),
                evidence=evidence,
                captured_packets=(_packet(),),
            )

            bundle = persist_engine_run(session, result, streams=(_stream(),))
            report = EvidenceReader.open(session, bundle.bundle_id).verify()

            self.assertTrue(report.valid, report.issues)
            self.assertEqual(bundle.status, EvidenceStatus.COMPLETE)
            self.assertEqual(len(bundle.component_states), 1)
            self.assertEqual(bundle.component_states[0].component_version, "1.2.3")
            self.assertEqual(session.bundle_paths, (session.root / "bundles/bundle.execution.1/bundle.json",))

    def test_framed_reference_store_satisfies_sample_store_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp) / "session", session_id="session.protocol")
            store = FramedSampleStore(
                session.artifacts,
                store_id="capture.protocol",
                purpose=SampleStorePurpose.EXECUTION_CAPTURE,
                namespace="captures",
                relative_uri="captures/protocol.eegle",
            )
            self.assertIsInstance(store, SampleStore)
            dense_stream = _stream()
            sparse_stream = StreamSpec(
                stream_id="stream.spikes",
                revision=1,
                modality="electrophysiology",
                content_kind=ContentKind.SPARSE_EVENTS,
                rate_model=RateModel.EVENT,
                clock_id="device.synthetic",
            )
            sparse = SparseEventBatch(
                "spikes.1",
                "stream.spikes",
                1,
                0,
                (
                    SparseEvent(
                        "spike.1",
                        "spike",
                        _time(0.1, "device.synthetic"),
                        _time(0.9),
                        _time(1.0),
                    ),
                ),
            )
            metadata = MetadataEvent(
                "metadata.1",
                "stream.synthetic",
                1,
                2,
                "impedance",
                _time(0.2, "device.synthetic"),
                _time(1.1),
                _time(1.2),
                {"valid": True},
            )
            store.open_stream(dense_stream)
            store.open_stream(sparse_stream)
            store.append(_packet())
            store.append(sparse)
            store.append(metadata)
            reference = store.close()
            restored = read_framed_sample_store(session.artifacts.resolve(reference))
            self.assertEqual(reference.role, "execution_capture")
            self.assertEqual(restored.streams, (dense_stream, sparse_stream))
            self.assertEqual(restored.packets, (_packet(), sparse, metadata))


if __name__ == "__main__":
    unittest.main()
