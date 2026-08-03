"""Read-only project/session adapter for the validation-owned result contracts."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from eegle._validation import require_identifier
from eegle.compiler import read_lock, read_plan
from eegle.compiler.lock import canonical_hash
from eegle.operations.projects import PROJECT_MANIFEST_NAME, open_project
from eegle.operations.sessions import replay_session
from eegle.recording import EvidenceReader, Session
from eegle.validation import (
    ValidationLayer,
    ValidationReport,
    ValidationResult,
    ValidationSeverity,
    ValidationStatus,
    combine_validation_reports,
    validate_evidence,
    validate_locked_plan,
)


def validate_target(
    target: str | Path,
    *,
    bundle_id: str | None = None,
    replay: bool = True,
) -> ValidationReport:
    """Validate a project or session without changing, recovering, or finalizing it."""

    root = Path(target).expanduser().resolve()
    if (root / PROJECT_MANIFEST_NAME).is_file():
        return validate_project(root, bundle_id=bundle_id, replay=replay)
    return validate_session(root, bundle_id=bundle_id, replay=replay)


def validate_project(
    root: str | Path,
    *,
    bundle_id: str | None = None,
    replay: bool = True,
) -> ValidationReport:
    target = Path(root).expanduser().resolve()
    try:
        project = open_project(target)
    except Exception as exc:  # noqa: BLE001 - read-only observer boundary
        return _unavailable_report(
            _target_subject_id(target, "project"),
            "definition.project_index",
            ValidationLayer.DEFINITION,
            "Project artifacts failed read-only integrity inspection.",
            exc,
            fail=True,
        )

    reports: list[ValidationReport] = []
    try:
        plan = read_plan(project.path_for("execution_plan"))
        lock = read_lock(project.path_for("execution_lock"))
    except KeyError as exc:
        reports.append(
            _unavailable_report(
                project.manifest.project_id,
                "definition.compiled_plan",
                ValidationLayer.DEFINITION,
                "Project has no compiled execution plan and lock.",
                exc,
            )
        )
    except Exception as exc:  # noqa: BLE001 - read-only observer boundary
        reports.append(
            _unavailable_report(
                project.manifest.project_id,
                "definition.compiled_plan",
                ValidationLayer.DEFINITION,
                "Compiled project artifacts could not be decoded.",
                exc,
                fail=True,
            )
        )
    else:
        reports.append(
            validate_locked_plan(
                plan,
                lock,
                subject_id=project.manifest.project_id,
            )
        )

    if project.manifest.session_uris:
        selected = project.root / project.manifest.session_uris[-1]
        reports.append(
            validate_session(selected, bundle_id=bundle_id, replay=replay)
        )
    else:
        reports.append(
            _unavailable_report(
                project.manifest.project_id,
                "execution.session_evidence",
                ValidationLayer.EXECUTION,
                "Project validation requires at least one recorded session.",
                KeyError(bundle_id or "no session"),
            )
        )
    return combine_validation_reports(
        f"validation.{project.manifest.project_id}",
        project.manifest.project_id,
        reports,
        metadata={
            "target_kind": "project",
            "project_root": str(project.root),
            "latest_session_validated": bool(project.manifest.session_uris),
            "read_only": True,
        },
    )


def validate_session(
    root: str | Path,
    *,
    bundle_id: str | None = None,
    replay: bool = True,
) -> ValidationReport:
    target = Path(root).expanduser().resolve()
    try:
        session = Session.open(target, read_only=True, allow_legacy=False)
    except Exception as exc:  # noqa: BLE001 - read-only observer boundary
        return _unavailable_report(
            _target_subject_id(target, "session"),
            "integrity.session",
            ValidationLayer.INTEGRITY,
            "Session manifest and artifact registry could not be opened.",
            exc,
            fail=True,
        )

    if bundle_id is not None:
        selection: str | Path = bundle_id
    elif session.bundle_paths:
        selection = session.bundle_paths[-1]
    else:
        return validate_evidence(
            (),
            subject_id=session.session_id,
            integrity_status=None,
            integrity_issues=(
                {
                    "code": "session.no_published_bundles",
                    "message": "Session has no published evidence bundle.",
                },
            ),
        )
    try:
        reader = EvidenceReader.open(session, selection)
        integrity = reader.verify()
    except Exception as exc:  # noqa: BLE001 - read-only observer boundary
        return _unavailable_report(
            session.session_id,
            "integrity.bundle",
            ValidationLayer.INTEGRITY,
            "Selected evidence bundle could not be opened or verified.",
            exc,
            fail=True,
        )

    records = ()
    if integrity.valid or integrity.recoverable:
        try:
            records = reader.records(allow_recoverable=integrity.recoverable)
        except Exception as exc:  # noqa: BLE001 - read-only observer boundary
            return _unavailable_report(
                session.session_id,
                "integrity.evidence_log",
                ValidationLayer.INTEGRITY,
                "Verified bundle records could not be read.",
                exc,
                fail=True,
            )
    replay_result = None
    if replay and integrity.valid:
        replay_result = replay_session(
            session.root,
            bundle_id=reader.bundle.bundle_id,
        )
    return validate_evidence(
        records,
        subject_id=session.session_id,
        bundle_id=reader.bundle.bundle_id,
        integrity_status=integrity.status.value,
        integrity_issues=tuple(_integrity_issue_payload(value) for value in integrity.issues),
        replay_report=replay_result,
        required_source_observations=tuple(
            str(value)
            for value in reader.bundle.metadata.get(
                "source_observation_requirements", ()
            )
        ),
        source_clock_drift_tolerance_seconds=(
            None
            if reader.bundle.metadata.get(
                "source_clock_drift_tolerance_seconds"
            )
            is None
            else float(
                reader.bundle.metadata["source_clock_drift_tolerance_seconds"]
            )
        ),
    )


def _unavailable_report(
    subject_id: str,
    result_id: str,
    layer: ValidationLayer,
    summary: str,
    error: Exception,
    *,
    fail: bool = False,
) -> ValidationReport:
    status = ValidationStatus.FAIL if fail else ValidationStatus.INSUFFICIENT_EVIDENCE
    severity = ValidationSeverity.ERROR if fail else ValidationSeverity.WARNING
    return ValidationReport(
        f"validation.{subject_id}",
        subject_id,
        (
            ValidationResult(
                result_id,
                layer,
                status,
                severity,
                summary,
                details={"error": f"{type(error).__name__}: {error}"},
            ),
        ),
        {"read_only": True},
    )


def _integrity_issue_payload(value: Any) -> Mapping[str, Any]:
    return {
        "code": getattr(getattr(value, "code", None), "value", None),
        "message": getattr(value, "message", None),
        "artifact_id": getattr(value, "artifact_id", None),
        "recoverable": bool(getattr(value, "recoverable", False)),
    }


def _target_subject_id(target: Path, kind: str) -> str:
    candidate = target.name or kind
    try:
        return require_identifier(candidate, "validation subject_id")
    except ValueError:
        digest = canonical_hash({"kind": kind, "target": str(target)})[:16]
        return f"{kind}.{digest}"


__all__ = ["validate_project", "validate_session", "validate_target"]
