from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from eegle.compiler import canonical_hash
from eegle.recording import (
    EvidenceReader,
    EvidenceRecord,
    EvidenceStatus,
    EvidenceWriter,
    ExportDisposition,
    ExportPolicy,
    ExternalVerificationStatus,
    JsonFieldRule,
    JsonRedactionAction,
    JsonRedactionSpec,
    LocalFileArtifactVerifier,
    RetentionAction,
    RetentionPolicy,
    Sensitivity,
    Session,
    evaluate_retention,
    export_evidence_bundle,
    read_portable_export,
)
from eegle.streams import TimePoint


def _time(seconds: float) -> TimePoint:
    return TimePoint(seconds, "host.virtual")


def _bundle_with_artifacts(
    root: Path,
    *,
    deployment: dict[str, object],
) -> tuple[Session, EvidenceReader, object, object]:
    session = Session.create(
        root,
        session_id="session.export",
        participant_pseudonym="participant.001",
    )
    plan_hash = canonical_hash({"scientific_protocol": "attention-validation"})
    writer = EvidenceWriter(
        session,
        bundle_id="bundle.export",
        plan_hash=plan_hash,
        created_time=_time(0.0),
    )
    writer.append(EvidenceRecord("evidence.0", "run_started", 0, _time(0.0), {}))
    scientific = session.artifacts.register_json(
        "portable",
        "protocol.1",
        "scientific_protocol",
        {"protocol_id": "attention-validation", "minimum_coverage": 0.95},
        sensitivity=Sensitivity.PUBLIC,
    )
    deployment_reference = session.artifacts.register_json(
        "deployment",
        "deployment.1",
        "deployment_binding",
        deployment,
        sensitivity=Sensitivity.PSEUDONYMIZED,
    )
    raw = session.artifacts.register_external(
        "raw",
        "raw.1",
        "archival_raw",
        "nwb://restricted/session-1",
        canonical_hash({"raw": "external"}),
        "application/x-nwb",
        10_000_000_000,
        sensitivity=Sensitivity.RESTRICTED,
    )
    writer.add_artifact(scientific)
    writer.add_artifact(deployment_reference)
    writer.add_raw_recording(raw)
    bundle = writer.finalize(status=EvidenceStatus.COMPLETE, completed_time=_time(1.0))
    return session, EvidenceReader.open(session, bundle.bundle_id), scientific, deployment_reference


class PrivacyExportRetentionTests(unittest.TestCase):
    def test_safe_default_export_omits_identity_restricted_content_and_external_uris(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session, reader, scientific, _ = _bundle_with_artifacts(
                Path(tmp) / "session",
                deployment={"site": "lab-a", "device": "amplifier-1"},
            )
            destination = Path(tmp) / "portable"

            manifest = export_evidence_bundle(reader, destination)
            entries = {value.artifact_id: value for value in manifest.entries}

            self.assertIsNone(manifest.participant_pseudonym)
            self.assertIsNone(manifest.source_session_id)
            self.assertEqual(manifest.source_plan_hash, reader.bundle.plan_hash)
            self.assertEqual(entries[scientific.artifact_id].export_digest, scientific.digest)
            self.assertEqual(entries["raw.1"].disposition, ExportDisposition.EXCLUDED)
            self.assertIsNone(entries["raw.1"].external_uri)
            self.assertNotIn("nwb://restricted", (destination / "export.json").read_text())
            self.assertTrue((destination / "export.json").is_file())
            self.assertEqual(read_portable_export(destination).export_hash, manifest.export_hash)
            self.assertEqual(
                ExportPolicy.from_payload(ExportPolicy().to_payload()).policy_hash,
                ExportPolicy().policy_hash,
            )
            self.assertEqual(
                RetentionPolicy.from_payload(RetentionPolicy().to_payload()).policy_hash,
                RetentionPolicy().policy_hash,
            )

            before = tuple(sorted(path.relative_to(session.root) for path in session.root.rglob("*")))
            decisions = evaluate_retention(session, RetentionPolicy())
            after = tuple(sorted(path.relative_to(session.root) for path in session.root.rglob("*")))
            self.assertEqual(before, after)
            self.assertTrue(decisions)
            self.assertIn(RetentionAction.REVIEW, {value.action for value in decisions})

    def test_explicit_redaction_changes_deployment_digest_not_scientific_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, reader, scientific, deployment = _bundle_with_artifacts(
                Path(tmp) / "session",
                deployment={
                    "site": "lab-a",
                    "device_path": "/dev/tty.usb-device",
                    "api_token": "do-not-export",
                },
            )
            policy = ExportPolicy(
                redactions=(
                    JsonRedactionSpec(
                        deployment.digest,
                        (
                            JsonFieldRule("/device_path", JsonRedactionAction.REMOVE),
                            JsonFieldRule("/api_token", JsonRedactionAction.REMOVE),
                        ),
                    ),
                )
            )
            destination = Path(tmp) / "portable"

            manifest = export_evidence_bundle(reader, destination, policy)
            entries = {value.artifact_id: value for value in manifest.entries}
            exported_deployment = entries[deployment.artifact_id]
            payload = json.loads((destination / str(exported_deployment.export_uri)).read_text())

            self.assertEqual(exported_deployment.disposition, ExportDisposition.REDACTED)
            self.assertNotEqual(exported_deployment.export_digest, deployment.digest)
            self.assertEqual(entries[scientific.artifact_id].export_digest, scientific.digest)
            self.assertEqual(manifest.source_plan_hash, reader.bundle.plan_hash)
            self.assertEqual(payload, {"site": "lab-a"})

    def test_secret_shaped_json_fails_before_export_is_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, reader, _, _ = _bundle_with_artifacts(
                Path(tmp) / "session",
                deployment={"site": "lab-a", "api_key": "must-not-leak"},
            )
            destination = Path(tmp) / "portable"

            with self.assertRaisesRegex(ValueError, "secret-shaped field"):
                export_evidence_bundle(reader, destination)

            self.assertFalse(destination.exists())


class ExternalArtifactAndBoundaryTests(unittest.TestCase):
    def test_large_source_native_file_is_not_copied_and_is_verifiably_referenced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "source-native.nwb"
            source.write_bytes(b"EEGLE-NWB-FIXTURE\n" + b"\0" * (8 * 1024 * 1024))
            session = Session.create(base / "session", session_id="session.external")
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.external",
                plan_hash=canonical_hash({"plan": "external"}),
                created_time=_time(0.0),
            )
            writer.append(EvidenceRecord("evidence.0", "run_started", 0, _time(0.0), {}))
            raw = session.artifacts.register_external_file(
                "raw/source-native",
                "raw.source-native.1",
                "archival_raw",
                source,
                "application/x-nwb",
            )
            writer.add_raw_recording(raw)
            bundle = writer.finalize(status=EvidenceStatus.COMPLETE, completed_time=_time(1.0))
            reader = EvidenceReader.open(session, bundle.bundle_id)

            reference_only = reader.verify()
            verified = reader.verify(LocalFileArtifactVerifier())

            self.assertTrue(reference_only.valid)
            self.assertEqual(
                reference_only.external_artifacts[0].status,
                ExternalVerificationStatus.REFERENCE_ONLY,
            )
            self.assertTrue(verified.valid)
            self.assertEqual(
                verified.external_artifacts[0].status,
                ExternalVerificationStatus.VERIFIED,
            )
            self.assertFalse(raw.embedded)
            self.assertEqual(raw.uri, source.resolve().as_uri())
            self.assertEqual(
                session.artifacts.verify(raw),
                (
                    False,
                    "external artifact content was not verified: raw.source-native.1; "
                    "use an ExternalArtifactVerifier",
                ),
            )
            self.assertLess(
                sum(path.stat().st_size for path in session.root.rglob("*") if path.is_file()),
                source.stat().st_size,
            )

            with source.open("r+b") as handle:
                handle.seek(-1, 2)
                handle.write(b"X")
            mismatch = reader.verify(LocalFileArtifactVerifier())
            self.assertFalse(mismatch.valid)
            self.assertEqual(
                mismatch.external_artifacts[0].status,
                ExternalVerificationStatus.MISMATCH,
            )
            source.unlink()
            unavailable = reader.verify(LocalFileArtifactVerifier())
            self.assertTrue(unavailable.valid)
            self.assertEqual(
                unavailable.external_artifacts[0].status,
                ExternalVerificationStatus.UNAVAILABLE,
            )

    def test_target_foundations_do_not_import_session_paths(self) -> None:
        package_root = Path(__file__).parents[1] / "eegle"
        target_packages = (
            "actions",
            "compiler",
            "models",
            "plugins",
            "processing",
            "replay",
            "runtime",
            "specs",
            "streams",
        )
        offenders = []
        for package in target_packages:
            for source in (package_root / package).rglob("*.py"):
                if "SessionPaths" in source.read_text(encoding="utf-8"):
                    offenders.append(source.relative_to(package_root).as_posix())
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
