from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from eegle.compiler import canonical_hash
from eegle.operations import (
    OperationOutcome,
    export_session,
    inspect_session,
    replay_session,
)
from eegle.operations.cli import main as cli_main
from eegle.recording import (
    EvidenceRecord,
    EvidenceStatus,
    EvidenceWriter,
    ExportDisposition,
    Sensitivity,
    Session,
)
from eegle.streams import TimePoint


RAW_SENTINEL = "RAW-PARTICIPANT-VALUE-MUST-NOT-LEAK"
DEPLOYMENT_SENTINEL = "PROTECTED-DEPLOYMENT-MUST-NOT-LEAK"
PARTICIPANT_SENTINEL = "participant.private-pseudonym"


def _time(seconds: float) -> TimePoint:
    return TimePoint(seconds, "clock.session-experience")


def _record(sequence: int, record_type: str, payload: dict) -> EvidenceRecord:
    return EvidenceRecord(
        f"record.session-experience.{sequence}",
        record_type,
        sequence,
        _time(sequence / 10),
        payload,
    )


def _session_with_structured_evidence(root: Path) -> Session:
    session = Session.create(
        root,
        session_id="session.phase7-experience",
        participant_pseudonym=PARTICIPANT_SENTINEL,
        metadata={"operator_note": "private"},
    )
    writer = EvidenceWriter(
        session,
        bundle_id="bundle.phase7-experience",
        plan_hash=canonical_hash({"plan": "phase7-experience"}),
        created_time=_time(0),
        metadata={
            "engine_status": "complete",
            "equivalence_ceiling": "semantic",
        },
    )
    scientific = session.artifacts.register_json(
        "reports",
        "report.public-scientific",
        "scientific_protocol",
        {"protocol": "public-observation"},
        sensitivity=Sensitivity.PUBLIC,
    )
    deployment = session.artifacts.register_json(
        "deployment",
        "deployment.protected",
        "deployment_binding",
        {"device": DEPLOYMENT_SENTINEL},
        sensitivity=Sensitivity.PSEUDONYMIZED,
    )
    raw = session.artifacts.register_external(
        "raw",
        "raw.participant",
        "archival_raw",
        "nwb://private/session",
        canonical_hash({"raw": RAW_SENTINEL}),
        "application/x-nwb",
        1_000_000,
        sensitivity=Sensitivity.RESTRICTED,
    )
    writer.add_artifact(scientific)
    writer.add_artifact(deployment)
    writer.add_raw_recording(raw)
    records = (
        _record(0, "phase_started", {"phase_id": "phase.observe"}),
        _record(
            1,
            "graph_emission",
            {
                "component_id": "source.neural",
                "value": {
                    "schema": "eegle.dense_sample_batch.v1",
                    "stream_id": "stream.neural",
                    "stream_revision": 1,
                    "sequence_start": 0,
                    "shape": [2, 1],
                    "values": [[RAW_SENTINEL], [RAW_SENTINEL]],
                },
            },
        ),
        _record(
            2,
            "work",
            {
                "work_id": "work.completed",
                "component_id": "model.primary",
                "stage": "model",
                "status": "completed",
                "started_time": _time(0.2).to_payload(),
                "completed_time": _time(0.3).to_payload(),
                "input_ids": ["batch.private"],
            },
        ),
        _record(
            3,
            "work",
            {
                "work_id": "work.rejected",
                "component_id": "model.shadow",
                "stage": "model",
                "status": "rejected",
                "started_time": _time(0.3).to_payload(),
                "completed_time": _time(0.3).to_payload(),
                "input_ids": ["batch.private"],
                "reason_code": "quality_rejected",
            },
        ),
        _record(
            4,
            "model_result_disposition",
            {"component_id": "model.primary", "status": "emitted"},
        ),
        _record(
            5,
            "model_comparison",
            {
                "comparison_id": "comparison.1",
                "group_id": "comparison.primary-shadow",
                "status": "complete",
                "member_components": ["model.primary", "model.shadow"],
                "prediction_ids": {
                    "model.primary": "prediction.primary",
                    "model.shadow": "prediction.shadow",
                },
                "outputs_equal": False,
            },
        ),
        _record(
            6,
            "graph_emission",
            {
                "component_id": "model.primary",
                "value": {
                    "schema": "eegle.prediction.v2",
                    "prediction_id": "prediction.primary",
                    "component_id": "model.primary",
                    "model_id": "model.public-id",
                    "model_version": "1.0.0",
                    "role_id": "role.primary",
                    "role_profile": "primary",
                    "output_port": "prediction",
                    "result_digest": canonical_hash({"result": RAW_SENTINEL}),
                    "value": {"label": RAW_SENTINEL},
                    "produced_time": _time(0.6).to_payload(),
                    "available_time": _time(0.65).to_payload(),
                    "input_ids": ["features.private"],
                    "admitted_input_ids": ["batch.private"],
                    "lineage": {
                        "latest_input_available_time": _time(0.5).to_payload()
                    },
                },
            },
        ),
        _record(
            7,
            "adaptation_eligibility",
            {
                "adaptation_id": "adaptation.online",
                "model_component_id": "model.primary",
                "status": "eligible",
            },
        ),
        _record(
            8,
            "state_transition",
            {
                "transition_id": "transition.adaptation",
                "component_id": "model.primary",
                "transition_kind": "adaptation",
                "status": "applied",
                "prior_state_hash": canonical_hash({"state": 0}),
                "resulting_state_hash": canonical_hash({"state": 1}),
                "metadata": {"adaptation_id": "adaptation.online"},
            },
        ),
        _record(
            9,
            "action_request",
            {
                "request": {
                    "action_request_id": "action.1",
                    "actuator_id": "actuator.simulated",
                    "capability": "notify",
                    "parameters": {"participant_value": RAW_SENTINEL},
                }
            },
        ),
        _record(
            10,
            "authorization_decision",
            {
                "decision_id": "decision.1",
                "action_request_id": "action.1",
                "provider_id": "provider.simulated",
                "status": "authorized",
                "evidence": {"private": RAW_SENTINEL},
            },
        ),
        _record(
            11,
            "authorized_command",
            {
                "command_id": "command.1",
                "action_request_id": "action.1",
                "actuator_id": "actuator.simulated",
                "capability": "notify",
                "parameters": {"participant_value": RAW_SENTINEL},
            },
        ),
        _record(
            12,
            "action_receipt",
            {
                "receipt": {
                    "receipt_id": "receipt.1",
                    "command_id": "command.1",
                    "status": "observed",
                    "details": {"private": RAW_SENTINEL},
                }
            },
        ),
        _record(
            13,
            "phase_finished",
            {"phase_id": "phase.observe", "status": "complete"},
        ),
    )
    for record in records:
        writer.append(record)
    writer.finalize(status=EvidenceStatus.COMPLETE, completed_time=_time(2))
    return session


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class Phase7SessionExperienceTests(unittest.TestCase):
    def test_inspection_projects_all_sections_without_raw_or_protected_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            session = _session_with_structured_evidence(
                Path(temporary_directory) / "session"
            )
            before = _tree_snapshot(session.root)

            report = inspect_session(session.root)
            payload = report.to_payload()

            self.assertEqual(report.outcome, OperationOutcome.COMPLETE)
            self.assertTrue(report.valid)
            self.assertEqual(len(report.phase_timeline), 2)
            self.assertEqual(report.source_health["streams"][0]["item_count"], 2)
            self.assertEqual(report.work["rejected_or_skipped_count"], 1)
            self.assertEqual(report.models["prediction_count"], 1)
            self.assertEqual(report.models["comparison_count"], 1)
            self.assertEqual(report.adaptation["transition_count"], 1)
            self.assertEqual(report.actions["record_count"], 4)
            self.assertAlmostEqual(
                report.latency["prediction_inference_seconds_by_component"]
                ["model.primary"]["p50"],
                0.1,
            )
            encoded = json.dumps(payload, sort_keys=True)
            self.assertNotIn(RAW_SENTINEL, encoded)
            self.assertNotIn(DEPLOYMENT_SENTINEL, encoded)
            self.assertNotIn(PARTICIPANT_SENTINEL, encoded)
            self.assertFalse(payload["privacy"]["raw_participant_values_included"])
            self.assertEqual(_tree_snapshot(session.root), before)

    def test_unfinished_and_unavailable_evidence_is_reported_without_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "session"
            session = Session.create(root, session_id="session.unfinished")
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.unfinished",
                plan_hash=canonical_hash({"plan": "unfinished"}),
                created_time=_time(0),
            )
            writer.append(_record(0, "phase_started", {"phase_id": "phase.long"}))
            writer.close_unfinalized()
            before = _tree_snapshot(root)

            inspection = inspect_session(root)
            replay = replay_session(root)

            self.assertEqual(inspection.outcome, OperationOutcome.PARTIAL)
            self.assertEqual(inspection.summary["unfinished_writer_count"], 1)
            unfinished = inspection.summary["unfinished_writers"][0]
            self.assertTrue(unfinished["resumable"])
            self.assertFalse(unfinished["automatic_action_taken"])
            self.assertEqual(replay.outcome, OperationOutcome.UNAVAILABLE)
            self.assertEqual(_tree_snapshot(root), before)

            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main(["--json", "replay", str(root)])
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"]["status"], "unavailable")

            output = io.StringIO()
            with redirect_stdout(output):
                strict_code = cli_main(["--json", "replay", str(root), "--strict"])
            strict_payload = json.loads(output.getvalue())
            self.assertEqual(strict_code, 8)
            self.assertFalse(strict_payload["ok"])
            self.assertEqual(_tree_snapshot(root), before)

            missing = inspect_session(Path(temporary_directory) / "missing")
            self.assertEqual(missing.outcome, OperationOutcome.UNAVAILABLE)

            corrupt = _session_with_structured_evidence(
                Path(temporary_directory) / "corrupt-session"
            )
            ledger = (
                corrupt.root
                / "bundles"
                / "bundle.phase7-experience"
                / "semantic.eegle"
            )
            ledger.write_bytes(ledger.read_bytes() + b"incomplete-final-frame")
            corrupt_before = _tree_snapshot(corrupt.root)
            corrupt_inspection = inspect_session(corrupt.root)
            corrupt_replay = replay_session(corrupt.root)
            self.assertEqual(corrupt_inspection.outcome, OperationOutcome.PARTIAL)
            self.assertFalse(corrupt_inspection.valid)
            self.assertEqual(corrupt_replay.outcome, OperationOutcome.UNAVAILABLE)
            self.assertEqual(_tree_snapshot(corrupt.root), corrupt_before)

    def test_safe_export_is_public_only_non_overwriting_and_source_preserving(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session = _session_with_structured_evidence(root / "session")
            source_before = _tree_snapshot(session.root)
            destination = root / "portable"

            result = export_session(session.root, destination)

            self.assertTrue(result.published)
            self.assertEqual(result.outcome, OperationOutcome.COMPLETE)
            entries = {value.artifact_id: value for value in result.manifest.entries}
            self.assertEqual(
                entries["report.public-scientific"].disposition,
                ExportDisposition.INCLUDED,
            )
            self.assertEqual(
                entries["deployment.protected"].disposition,
                ExportDisposition.EXCLUDED,
            )
            self.assertEqual(
                entries["raw.participant"].disposition,
                ExportDisposition.EXCLUDED,
            )
            self.assertEqual(
                entries["bundle.phase7-experience.semantic-log"].disposition,
                ExportDisposition.EXCLUDED,
            )
            exported = "".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in destination.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(RAW_SENTINEL, exported)
            self.assertNotIn(DEPLOYMENT_SENTINEL, exported)
            self.assertNotIn(PARTICIPANT_SENTINEL, exported)
            self.assertEqual(_tree_snapshot(session.root), source_before)

            occupied = root / "occupied"
            occupied.mkdir()
            marker = occupied / "do-not-overwrite.txt"
            marker.write_text("preserve me", encoding="utf-8")
            refused = export_session(session.root, occupied)
            self.assertFalse(refused.published)
            self.assertEqual(refused.outcome, OperationOutcome.NOT_EXPORTED)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me")
            self.assertEqual(_tree_snapshot(session.root), source_before)

            output = io.StringIO()
            with redirect_stdout(output):
                code = cli_main(
                    ["--json", "export", str(session.root), str(occupied)]
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["result"]["status"], "not_exported")

            output = io.StringIO()
            with redirect_stdout(output):
                strict_code = cli_main(
                    [
                        "--json",
                        "export",
                        str(session.root),
                        str(occupied),
                        "--strict",
                    ]
                )
            strict_payload = json.loads(output.getvalue())
            self.assertEqual(strict_code, 8)
            self.assertFalse(strict_payload["ok"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve me")
            self.assertEqual(_tree_snapshot(session.root), source_before)


if __name__ == "__main__":
    unittest.main()
