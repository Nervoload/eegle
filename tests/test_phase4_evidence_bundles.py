from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import numpy as np

from eegle.compiler.lock import canonical_hash
from eegle.integrations.legacy_sessions import legacy_bcipy_recipe_importer
from eegle.recording import (
    ArtifactLineage,
    EvidenceReader,
    EvidenceRecord,
    EvidenceStatus,
    EvidenceWriter,
    IntegrityIssueCode,
    IntegrityStatus,
    LegacyImportReport,
    LegacySessionImporter,
    SampleStore,
    Sensitivity,
    Session,
    SessionPaths,
    SessionStatus,
    WriterPhase,
    complete_interrupted_finalization,
    discover_interrupted_runs,
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
    def test_interrupted_writer_is_discovered_authorized_resumed_and_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp) / "session", session_id="session.resume")
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.resume",
                plan_hash=canonical_hash({"plan": "resume"}),
                created_time=_time(0.0),
                resume_token="recovery-token-for-resume-test",
            )
            writer.append(EvidenceRecord("evidence.0", "run_started", 0, _time(0.0), {}))
            writer.close_unfinalized()
            state_text = (
                session.root / "bundles/bundle.resume/writer-state.json"
            ).read_text(encoding="utf-8")

            interrupted = discover_interrupted_runs(Session.open(session.root))

            self.assertNotIn("recovery-token-for-resume-test", state_text)
            self.assertEqual(len(interrupted), 1)
            self.assertEqual(interrupted[0].phase, WriterPhase.OPEN)
            self.assertEqual(interrupted[0].integrity, IntegrityStatus.VALID)
            self.assertTrue(interrupted[0].resumable)
            self.assertEqual(interrupted[0].last_complete_sequence, 0)
            with self.assertRaises(PermissionError):
                EvidenceWriter.resume(
                    session,
                    bundle_id="bundle.resume",
                    resume_token="wrong-token",
                )

            resumed = EvidenceWriter.resume(
                session,
                bundle_id="bundle.resume",
                resume_token="recovery-token-for-resume-test",
            )
            resumed.append(EvidenceRecord("evidence.1", "run_completed", 1, _time(1.0), {}))
            bundle = resumed.finalize(
                status=EvidenceStatus.COMPLETE,
                completed_time=_time(1.0),
            )

            self.assertEqual(discover_interrupted_runs(session), ())
            self.assertEqual(bundle.last_sequence, 1)
            self.assertEqual(
                [
                    record.record_type
                    for record in EvidenceReader.open(session, bundle.bundle_id).records()
                ],
                ["run_started", "run_completed"],
            )

    def test_resume_copies_a_truncated_ledger_prefix_without_mutating_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp) / "session", session_id="session.recover-ledger")
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.recover-ledger",
                plan_hash=canonical_hash({"plan": "recover-ledger"}),
                created_time=_time(0.0),
                resume_token="recovery-token-for-prefix-test",
            )
            writer.append(EvidenceRecord("evidence.0", "run_started", 0, _time(0.0), {}))
            writer.append(EvidenceRecord("evidence.1", "interrupted", 1, _time(0.5), {}))
            writer.close_unfinalized()
            original = session.root / "bundles/bundle.recover-ledger/semantic.eegle"
            original.write_bytes(original.read_bytes()[:-9])
            interrupted_bytes = original.read_bytes()

            discovery = discover_interrupted_runs(session)[0]
            self.assertEqual(discovery.integrity, IntegrityStatus.RECOVERABLE)
            self.assertEqual(discovery.last_complete_sequence, 0)

            resumed = EvidenceWriter.resume(
                session,
                bundle_id="bundle.recover-ledger",
                resume_token="recovery-token-for-prefix-test",
            )
            resumed.append(EvidenceRecord("evidence.1b", "run_recovered", 1, _time(1.0), {}))
            bundle = resumed.finalize(
                status=EvidenceStatus.PARTIAL,
                completed_time=_time(1.0),
            )

            self.assertEqual(original.read_bytes(), interrupted_bytes)
            recovered = (
                session.root
                / "bundles/bundle.recover-ledger/semantic.recovered-1.eegle"
            )
            self.assertTrue(recovered.is_file())
            interrupted_reference = next(
                reference
                for reference in bundle.artifacts
                if reference.role == "interrupted_evidence_log"
            )
            semantic_entry = session.artifacts.get(
                "bundles/bundle.recover-ledger",
                "bundle.recover-ledger.semantic-log",
            )
            self.assertEqual(
                semantic_entry.lineage.input_digests,
                (interrupted_reference.digest,),
            )
            self.assertEqual(
                [
                    record.record_type
                    for record in EvidenceReader.open(
                        session, "bundle.recover-ledger"
                    ).records()
                ],
                ["run_started", "run_recovered"],
            )

    def test_interrupted_finalization_can_be_completed_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(
                Path(tmp) / "session",
                session_id="session.finalize-recovery",
            )
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.finalize-recovery",
                plan_hash=canonical_hash({"plan": "finalize-recovery"}),
                created_time=_time(0.0),
                resume_token="recovery-token-for-finalize-test",
            )
            writer.append(EvidenceRecord("evidence.0", "run_started", 0, _time(0.0), {}))
            with mock.patch.object(
                session.artifacts,
                "register_file",
                side_effect=RuntimeError("simulated publication interruption"),
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated publication interruption"):
                    writer.finalize(
                        status=EvidenceStatus.PARTIAL,
                        completed_time=_time(1.0),
                    )

            interrupted = discover_interrupted_runs(session)
            self.assertEqual(len(interrupted), 1)
            self.assertEqual(interrupted[0].phase, WriterPhase.FINALIZING)
            self.assertTrue(interrupted[0].finalizable)

            bundle = complete_interrupted_finalization(
                session,
                bundle_id="bundle.finalize-recovery",
                resume_token="recovery-token-for-finalize-test",
            )
            repeated = complete_interrupted_finalization(
                session,
                bundle_id="bundle.finalize-recovery",
                resume_token="recovery-token-for-finalize-test",
            )

            self.assertEqual(bundle.bundle_hash, repeated.bundle_hash)
            self.assertEqual(discover_interrupted_runs(session), ())
            self.assertTrue(EvidenceReader.open(session, bundle.bundle_id).verify().valid)

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
            plan = SimpleNamespace(
                plan_hash=plan_hash,
                to_payload=lambda: {"schema": "fixture.plan.v1", "plan_hash": plan_hash},
            )

            bundle = persist_engine_run(session, result, plan=plan, streams=(_stream(),))
            report = EvidenceReader.open(session, bundle.bundle_id).verify()

            self.assertTrue(report.valid, report.issues)
            self.assertEqual(bundle.status, EvidenceStatus.COMPLETE)
            self.assertEqual(
                [reference.role for reference in bundle.artifacts].count("execution_plan"),
                1,
            )
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


class HistoricalImporterTests(unittest.TestCase):
    def test_recipe_specific_import_rules_are_an_integration_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy"
            (source / "events").mkdir(parents=True)
            (source / "manifest.json").write_text(
                '{"created_at":"2025-01-01T00:00:00","layout":"bcipy_style"}\n',
                encoding="utf-8",
            )
            (source / "events/dynamic_sart_results.json").write_text(
                '{"status":"complete"}\n',
                encoding="utf-8",
            )

            result = legacy_bcipy_recipe_importer().import_session(
                source,
                Path(tmp) / "imported",
                session_id="session.recipe-import",
            )

            imported = {
                item.source_uri: item.artifact.role
                for item in result.report.imported
            }
            self.assertEqual(
                imported["events/dynamic_sart_results.json"],
                "behavior_result",
            )

    def test_importer_rejects_an_undeclared_family_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy"
            source.mkdir()
            (source / "manifest.json").write_text(
                '{"created_at":"2025-01-01T00:00:00","layout":"unknown"}\n',
                encoding="utf-8",
            )
            destination = Path(tmp) / "imported"

            with self.assertRaisesRegex(ValueError, "supported bcipy_style family"):
                LegacySessionImporter().import_session(
                    source,
                    destination,
                    session_id="session.unsupported",
                )

            self.assertFalse(destination.exists())

    def test_importer_classifies_every_file_and_preserves_the_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "legacy"
            (source / "raw").mkdir(parents=True)
            (source / "events").mkdir()
            (source / "realtime").mkdir()
            (source / "reports").mkdir()
            (source / "manifest.json").write_text(
                '{"created_at":"2025-01-01T00:00:00","layout":"bcipy_style"}\n',
                encoding="utf-8",
            )
            (source / "raw/eeg.csv").write_text("time,Cz\n0,1\n", encoding="utf-8")
            (source / "events/events.jsonl").write_text(
                '{"event":"stimulus","time":0.0}\n',
                encoding="utf-8",
            )
            (source / "realtime/model_predictions.jsonl").write_text(
                '{"prediction":',
                encoding="utf-8",
            )
            (source / "reports/summary.html").write_text(
                "<p>derived</p>",
                encoding="utf-8",
            )
            (source / "unknown.bin").write_bytes(b"unknown")
            before = {
                path.relative_to(source).as_posix(): _digest(path.read_bytes())
                for path in source.rglob("*")
                if path.is_file()
            }

            result = LegacySessionImporter().import_session(
                source,
                Path(tmp) / "imported",
                session_id="session.imported",
                participant_pseudonym="participant.imported",
            )
            after = {
                path.relative_to(source).as_posix(): _digest(path.read_bytes())
                for path in source.rglob("*")
                if path.is_file()
            }

            self.assertEqual(before, after)
            self.assertEqual(
                result.report.counts,
                {"imported": 3, "derived": 2, "omitted": 2, "invalid": 1},
            )
            classified_sources = {
                item.source_uri
                for collection in (
                    result.report.imported,
                    result.report.omitted,
                    result.report.invalid,
                )
                for item in collection
            }
            self.assertEqual(classified_sources, set(before))
            self.assertEqual(result.bundle.status, EvidenceStatus.PARTIAL)
            self.assertEqual(result.session.status, SessionStatus.PARTIAL)
            self.assertEqual(len(result.bundle.raw_recordings), 1)
            self.assertTrue(
                EvidenceReader.open(
                    result.session,
                    result.bundle.bundle_id,
                ).verify().valid
            )

            report_entry = result.session.artifacts.get(
                "legacy-import/report",
                "legacy.import-report",
            )
            parsed_report = LegacyImportReport.from_payload(
                json.loads(
                    result.session.artifacts.resolve(report_entry.reference).read_text(
                        encoding="utf-8"
                    )
                )
            )
            self.assertEqual(parsed_report.report_hash, result.report.report_hash)
            self.assertEqual(result.report.invalid[0].reason, "source_validation_failed")


if __name__ == "__main__":
    unittest.main()
