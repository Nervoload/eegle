"""Read-only post-run integrity and quality validation for Study 1 visits."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic_ns, sleep
from typing import Any, Iterable

from eegle.config import DEFAULT_CONFIG, load_config, resolve_session_root
from eegle.devices.xdf_integrity import validate_xdf_recording
from eegle.pipelines.dsart_recording import (
    _baseline_recording_validation,
    _countdown_event_integrity,
    _task_marker_integrity,
)
from eegle.quality import (
    collect_recording_quality_issues,
    electrode_quality_issues,
    quality_issue,
)
from eegle.session import paths_for_existing_session
from eegle.tasks.dynamic_sart import score_dynamic_sart_trial
from eegle.tasks.dynamic_sart_schema import DynamicSartConfig


REPORT_SCHEMA = "eegle.study1_post_run_validation.v1"
VISIT_SCHEMA = "eegle.study1.visit.v1"
UNREADABLE_EXIT_CODE = 2
INTERNAL_ERROR_EXIT_CODE = 1


@dataclass(frozen=True)
class ValidationOptions:
    config_path: str | Path
    participant_id: str | None = None
    target: str | Path | None = None
    visit_number: int | None = None
    session_root: str | Path | None = None
    mode: str = "quick"
    backup_root: str | Path | None = None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    options = ValidationOptions(
        config_path=args.config,
        participant_id=args.participant,
        target=args.target,
        visit_number=args.visit_number,
        session_root=args.session_root,
        mode=args.mode,
        backup_root=args.backup_root,
    )
    try:
        report = validate_study1_data(options)
    except (FileNotFoundError, ValueError) as exc:
        print(f"study1-validate: {type(exc).__name__}: {exc}", file=sys.stderr)
        return UNREADABLE_EXIT_CODE
    except Exception as exc:
        print(f"study1-validate: {type(exc).__name__}: {exc}", file=sys.stderr)
        return INTERNAL_ERROR_EXIT_CODE
    print_validation_summary(report)
    return UNREADABLE_EXIT_CODE if report["core_errors"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="study1-validate",
        description="Validate a retained Study 1 visit without changing its raw data or completion state",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--participant", help="Select this participant's newest completed visit")
    identity.add_argument(
        "--target",
        help="Exact visit/session/run directory or visit_manifest.json path",
    )
    parser.add_argument("--visit", dest="visit_number", type=int, choices=[1, 2], default=None)
    parser.add_argument("--session-root", default=None)
    parser.add_argument("--mode", choices=["quick", "comprehensive"], default="quick")
    parser.add_argument(
        "--backup-root",
        default=None,
        help="Optional mirror of the EEGle DataRoot; valid only in comprehensive mode",
    )
    return parser


def validate_study1_data(options: ValidationOptions) -> dict[str, Any]:
    if options.backup_root is not None and options.mode != "comprehensive":
        raise ValueError("--backup-root requires --mode comprehensive")
    manifest_path, manifest, data_root = resolve_visit(options)
    visit_dir = manifest_path.parent
    sessions = _visit_sessions(manifest)
    core_errors: list[str] = []
    warnings: list[str] = []
    quality_issues: list[dict[str, Any]] = []
    session_reports = []
    artifacts: list[dict[str, Any]] = []

    visit_artifact = _inspect_artifact(
        manifest_path,
        role="visit_manifest",
        data_root=data_root,
        required=True,
        comprehensive=options.mode == "comprehensive",
        core_errors=core_errors,
    )
    if visit_artifact:
        artifacts.append(visit_artifact)

    for path, role, required in _visit_artifact_specs(manifest_path, manifest):
        artifact = _inspect_artifact(
            path,
            role=role,
            data_root=data_root,
            required=required,
            comprehensive=options.mode == "comprehensive",
            core_errors=core_errors,
        )
        if artifact:
            artifacts.append(artifact)
        if not path.is_file():
            continue
        try:
            payload = _read_json_object(path)
            if artifact is not None:
                artifact["declared_schema"] = _declared_schema(payload)
            if role == "preflight_report":
                warnings.extend(
                    f"preflight: {message}"
                    for message in [
                        *list(payload.get("failures") or []),
                        *list(payload.get("warnings") or []),
                    ]
                )
                quality_issues.extend(list(payload.get("quality_issues") or []))
            elif role == "electrode_quality_report":
                if not isinstance(payload.get("channels"), list):
                    raise ValueError("electrode report is missing the channels array")
                retained_issues = list(payload.get("quality_issues") or [])
                quality_issues.extend(
                    retained_issues
                    or electrode_quality_issues(payload, phase="preflight")
                )
        except Exception as exc:
            if required:
                core_errors.append(
                    f"{role} is unreadable: {type(exc).__name__}: {exc}: {path}"
                )

    if not sessions:
        warnings.append("visit manifest does not reference a retained baseline or task session")
    for phase, session_dir in sessions:
        session_report, session_artifacts = _validate_session(
            session_dir,
            phase=phase,
            manifest=manifest,
            data_root=data_root,
            mode=options.mode,
        )
        session_reports.append(session_report)
        artifacts.extend(session_artifacts)
        core_errors.extend(session_report["core_errors"])
        warnings.extend(session_report["warnings"])
        quality_issues.extend(session_report["quality_issues"])

    artifacts = _deduplicate_artifacts(artifacts)
    canonical_artifacts = [row for row in artifacts if row.get("canonical")]
    backup = (
        _verify_backup(canonical_artifacts, data_root=data_root, backup_root=Path(options.backup_root))
        if options.backup_root is not None
        else {"status": "not_checked", "backup_root": None, "warnings": [], "files": []}
    )
    warnings.extend(backup["warnings"])
    report = {
        "schema": REPORT_SCHEMA,
        "validator_version": REPORT_SCHEMA,
        "created_at": _now(),
        "mode": options.mode,
        "status": "unreadable" if core_errors else ("warning" if warnings or quality_issues else "ok"),
        "participant_id": manifest.get("participant_id"),
        "visit_number": manifest.get("visit_number"),
        "visit_id": manifest.get("visit_id"),
        "visit_status": manifest.get("status"),
        "visit_manifest": str(manifest_path),
        "visit_directory": str(visit_dir),
        "data_root": str(data_root),
        "raw_data_modified": False,
        "completion_manifest_modified": False,
        "core_errors": list(dict.fromkeys(core_errors)),
        "warnings": list(dict.fromkeys(warnings)),
        "quality_issues": _deduplicate_issues(quality_issues),
        "sessions": session_reports,
        "canonical_inventory": canonical_artifacts if options.mode == "comprehensive" else [],
        "backup_verification": backup,
    }
    report_dir = visit_dir / "reports" / "post_run_validation"
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    report_path = report_dir / f"{timestamp}-{options.mode}.json"
    report["report_file"] = str(report_path)
    _write_json_atomic(report_path, report)
    return report


def resolve_visit(options: ValidationOptions) -> tuple[Path, dict[str, Any], Path]:
    if options.target is not None:
        target = Path(options.target).expanduser()
        if not target.exists():
            raise FileNotFoundError(f"validation target does not exist: {target}")
        manifest_path = _manifest_from_target(target)
        if manifest_path is None:
            raise FileNotFoundError(f"validation target does not belong to a Study 1 visit: {target}")
        manifest = _read_json_object(manifest_path)
        if manifest.get("schema") != VISIT_SCHEMA:
            raise ValueError(f"target manifest has unsupported schema: {manifest_path}")
        return manifest_path, manifest, _data_root_for_manifest(manifest_path)

    participant = str(options.participant_id or "")
    if not participant:
        raise ValueError("--participant must be nonempty")
    config = load_config(options.config_path)
    root = resolve_session_root(config, options.session_root)
    matches = []
    for manifest_path in (root / "study1").glob("*/visits/visit-*/*/visit_manifest.json"):
        try:
            manifest = _read_json_object(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if manifest.get("participant_id") != participant or manifest.get("status") != "completed":
            continue
        if options.visit_number is not None and int(manifest.get("visit_number") or 0) != options.visit_number:
            continue
        matches.append((manifest_path, manifest))
    if not matches:
        visit_text = "" if options.visit_number is None else f" Visit {options.visit_number}"
        raise FileNotFoundError(f"no completed{visit_text} Study 1 visit found for participant {participant!r}")
    matches.sort(key=lambda row: row[0].stat().st_mtime, reverse=True)
    manifest_path, manifest = matches[0]
    return manifest_path, manifest, root


def _manifest_from_target(target: Path) -> Path | None:
    resolved = target.resolve()
    if resolved.is_file() and resolved.name == "visit_manifest.json":
        return resolved
    for candidate in (resolved, *resolved.parents):
        manifest = candidate / "visit_manifest.json"
        if manifest.is_file():
            return manifest
    data_root = next(
        (parent.parent for parent in (resolved, *resolved.parents) if parent.name == "participants"),
        None,
    )
    if data_root is None:
        return None
    matches = []
    for manifest_path in (data_root / "study1").glob("*/visits/visit-*/*/visit_manifest.json"):
        try:
            payload = _read_json_object(manifest_path)
        except Exception:
            continue
        absolute_values = {
            Path(value).expanduser().resolve()
            for value in _nested_strings(payload)
            if Path(value).expanduser().is_absolute()
        }
        if resolved in absolute_values:
            matches.append(manifest_path)
    return matches[0] if len(matches) == 1 else None


def _visit_sessions(manifest: dict[str, Any]) -> list[tuple[str, Path]]:
    sessions = []
    seen = set()
    phase_order = list(manifest.get("phase_order") or [])
    phases = dict(manifest.get("phases") or {})
    for phase in phase_order:
        attempts = list(dict(phases.get(phase) or {}).get("attempts") or [])
        result = next(
            (
                dict(attempt.get("result") or {})
                for attempt in reversed(attempts)
                if isinstance(attempt.get("result"), dict) and attempt.get("result", {}).get("session_dir")
            ),
            {},
        )
        session_value = result.get("session_dir")
        if not session_value:
            continue
        session = Path(str(session_value)).expanduser()
        key = os.path.normcase(os.path.abspath(os.fspath(session)))
        if key not in seen:
            sessions.append((str(phase), session))
            seen.add(key)
    for phase, session_value in dict(manifest.get("session_directories") or {}).items():
        if not session_value:
            continue
        session = Path(str(session_value)).expanduser()
        key = os.path.normcase(os.path.abspath(os.fspath(session)))
        if key not in seen:
            sessions.append((str(phase), session))
            seen.add(key)
    return sessions


def _validate_session(
    session_dir: Path,
    *,
    phase: str,
    manifest: dict[str, Any],
    data_root: Path,
    mode: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    core_errors: list[str] = []
    warnings: list[str] = []
    quality_issues: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    if not session_dir.is_dir():
        return (
            {
                "phase": phase,
                "session_dir": str(session_dir),
                "status": "unreadable",
                "core_errors": [f"{phase}: retained session directory is missing: {session_dir}"],
                "warnings": [],
                "quality_issues": [],
            },
            [],
        )
    role = "baseline" if "baseline" in phase else "task"
    specifications = _canonical_artifact_specs(session_dir, role=role, record_eeg=bool(manifest.get("record_eeg", True)))
    parsed: dict[str, Any] = {}
    for path, artifact_role, required, parse_kind in specifications:
        artifact = _inspect_artifact(
            path,
            role=artifact_role,
            data_root=data_root,
            required=required,
            comprehensive=mode == "comprehensive",
            core_errors=core_errors,
        )
        if artifact:
            artifacts.append(artifact)
        if not path.exists() or not path.is_file():
            if not required:
                warnings.append(f"{phase}: optional artifact is missing: {path}")
            continue
        try:
            if parse_kind == "json":
                parsed[artifact_role] = _read_json_object(path)
            elif parse_kind == "jsonl":
                parsed[artifact_role] = _read_jsonl(path)
            elif parse_kind == "csv":
                parsed[artifact_role] = _read_csv(path)
            if parse_kind is not None:
                _validate_parsed_artifact(artifact_role, parsed[artifact_role])
                if artifact is not None:
                    artifact["declared_schema"] = _declared_schema(parsed[artifact_role])
        except Exception as exc:
            message = f"{phase}: {artifact_role} is unreadable: {type(exc).__name__}: {exc}"
            if required:
                core_errors.append(message)
            else:
                warnings.append(message)

    xdf_path = session_dir / "raw" / "recording.xdf"
    if xdf_path.exists():
        try:
            header = _resolve_xdf_headers(xdf_path)
        except Exception as exc:
            core_errors.append(f"{phase}: authoritative XDF headers are unreadable: {type(exc).__name__}: {exc}")
            header = []
        if header:
            stream_types = [str(row.get("type") or "") for row in header]
            if not any(value.casefold() == "eeg" for value in stream_types):
                warnings.append(f"{phase}: XDF header does not expose an EEG stream")
            if not any(value.casefold() == "markers" for value in stream_types):
                warnings.append(f"{phase}: XDF header does not expose a marker stream")

    if role == "task":
        quick_warnings, quick_issues = _quick_task_reconciliation(
            parsed,
            phase=phase,
            session_dir=session_dir,
        )
        warnings.extend(quick_warnings)
        quality_issues.extend(quick_issues)
    warnings.extend(
        _session_reference_warnings(
            parsed,
            phase=phase,
            session_dir=session_dir,
            visit_manifest=manifest,
        )
    )
    if mode == "comprehensive":
        if xdf_path.exists() and not any("XDF" in row and "unreadable" in row for row in core_errors):
            xdf_validation = validate_xdf_recording(
                session_dir,
                required=bool(manifest.get("record_eeg", True)),
                persist_report=False,
            )
            unreadable_failures, integrity_warnings = _classify_xdf_failures(xdf_validation)
            core_errors.extend(f"{phase}: {message}" for message in unreadable_failures)
            warnings.extend(f"{phase}: {message}" for message in integrity_warnings)
            validation_payload = {"validation": {"raw_integrity": {"xdf_integrity": xdf_validation}}}
            quality_issues.extend(
                collect_recording_quality_issues(
                    validation_payload,
                    phase=phase,
                    session_dir=session_dir,
                    trial_rows=list(parsed.get("trial_ledger") or []),
                )
            )
        if role == "task":
            parameters = parsed.get("parameters") or {}
            marker = _task_marker_integrity(
                session_dir,
                parameters,
                require_markers=bool(manifest.get("record_eeg", True)),
                require_display_flip=manifest.get("task_mode") == "psychopy",
            )
            warnings.extend(f"{phase}: {message}" for message in [*marker.get("failures", []), *marker.get("warnings", [])])
            quality_issues.extend(
                collect_recording_quality_issues(
                    {"validation": {"marker_integrity": marker}},
                    phase=phase,
                    session_dir=session_dir,
                )
            )
            scoring_warnings, scoring_issues = _recompute_task_scoring(
                parsed,
                session_dir=session_dir,
                phase=phase,
            )
            warnings.extend(scoring_warnings)
            quality_issues.extend(scoring_issues)
            countdown = _countdown_event_integrity(session_dir)
            warnings.extend(
                f"{phase}: {message}"
                for message in [*countdown.get("failures", []), *countdown.get("warnings", [])]
            )
        else:
            baseline_result = parsed.get("baseline_results") or parsed.get("session_summary") or {}
            if baseline_result:
                baseline = _baseline_recording_validation(
                    paths_for_existing_session(session_dir),
                    baseline_result,
                    record_eeg=bool(manifest.get("record_eeg", True)),
                )
                warnings.extend(
                    f"{phase}: {message}"
                    for message in [*baseline.get("failures", []), *baseline.get("warnings", [])]
                )
                quality_issues.extend(
                    collect_recording_quality_issues(
                        {"validation": baseline},
                        phase=phase,
                        session_dir=session_dir,
                    )
                )

    report = {
        "phase": phase,
        "role": role,
        "session_dir": str(session_dir),
        "status": "unreadable" if core_errors else ("warning" if warnings or quality_issues else "ok"),
        "core_errors": list(dict.fromkeys(core_errors)),
        "warnings": list(dict.fromkeys(warnings)),
        "quality_issues": _deduplicate_issues(quality_issues),
        "artifact_count": len(artifacts),
    }
    return report, artifacts


def _canonical_artifact_specs(
    session_dir: Path,
    *,
    role: str,
    record_eeg: bool,
) -> list[tuple[Path, str, bool, str | None]]:
    rows = [
        (session_dir / "manifest.json", "session_manifest", True, "json"),
        (session_dir / "parameters.json", "parameters", True, "json"),
    ]
    if record_eeg:
        rows.extend(
            [
                (session_dir / "raw" / "recording.xdf", "authoritative_xdf", True, None),
                (session_dir / "raw" / "xdf_metadata.json", "xdf_metadata", True, "json"),
                (session_dir / "raw" / "lsl_markers_received.csv", "independent_marker_receipt", True, "csv"),
                (
                    session_dir / "logs" / "processes" / "recorder.status.json",
                    "recorder_process_status",
                    True,
                    "json",
                ),
            ]
        )
    if role == "task":
        rows.extend(
            [
                (session_dir / "events" / "stimulus_manifest.json", "stimulus_manifest", True, "json"),
                (session_dir / "events" / "dynamic_sart_trials.jsonl", "trial_ledger", True, "jsonl"),
                (session_dir / "events" / "dynamic_sart_key_events.jsonl", "key_event_ledger", True, "jsonl"),
                (session_dir / "events" / "events.jsonl", "event_ledger", True, "jsonl"),
                (session_dir / "events" / "dynamic_sart_results.json", "task_results", True, "json"),
                (session_dir / "events" / "dynamic_sart_trials.csv", "trial_csv_mirror", False, "csv"),
                (session_dir / "events" / "behavior.csv", "behavior_csv_mirror", False, "csv"),
                (session_dir / "triggers.txt", "trigger_text_mirror", False, None),
            ]
        )
    else:
        rows.extend(
            [
                (session_dir / "events" / "events.jsonl", "event_ledger", True, "jsonl"),
                (session_dir / "events" / "dsart_baseline_results.json", "baseline_results", True, "json"),
                (session_dir / "session_summary.json", "session_summary", False, "json"),
            ]
        )
    return rows


def _visit_artifact_specs(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> list[tuple[Path, str, bool]]:
    rows: list[tuple[Path, str, bool]] = [
        (manifest_path.parents[3] / "participant_manifest.json", "participant_manifest", True)
    ]
    attempts = list(
        dict(manifest.get("phases", {}).get("preflight", {}) or {}).get("attempts") or []
    )
    result = next(
        (
            dict(attempt.get("result") or {})
            for attempt in reversed(attempts)
            if isinstance(attempt.get("result"), dict)
        ),
        {},
    )
    for field, role in (
        ("report_file", "preflight_report"),
        ("electrode_quality_file", "electrode_quality_report"),
    ):
        value = result.get(field)
        if value:
            rows.append((Path(str(value)).expanduser(), role, True))
    unique = {}
    for path, role, required in rows:
        unique[(os.path.normcase(os.path.abspath(os.fspath(path))), role)] = (
            path,
            role,
            required,
        )
    return list(unique.values())


def _quick_task_reconciliation(
    parsed: dict[str, Any],
    *,
    phase: str,
    session_dir: Path,
) -> tuple[list[str], list[dict[str, Any]]]:
    warnings: list[str] = []
    issues: list[dict[str, Any]] = []
    trials = list(parsed.get("trial_ledger") or [])
    keys = list(parsed.get("key_event_ledger") or [])
    manifest = dict(parsed.get("stimulus_manifest") or {})
    key_ids = [str(row.get("event_id")) for row in keys if row.get("event_id")]
    if len(key_ids) != len(set(key_ids)):
        warnings.append(f"{phase}: key-event ledger contains duplicate event IDs")
        duplicates = sorted({value for value in key_ids if key_ids.count(value) > 1})
        issues.append(
            quality_issue(
                "keypress.duplicate_event_id",
                "keypress",
                "Key-event ledger contains duplicate event identifiers",
                phase=phase,
                session_dir=session_dir,
                details=({"event_id": value} for value in duplicates),
                count=len(key_ids) - len(set(key_ids)),
                recoverability="partially_recoverable",
                suggested_action="Use ledger order and timestamps to resolve duplicated identifiers before trial scoring.",
                detector="study1_validate.quick_reconciliation",
            )
        )
    available = set(key_ids)
    missing_refs = []
    referenced_by_trial: dict[str, list[Any]] = {}
    for trial in trials:
        trial_index = trial.get("global_trial_index")
        for event_id in list(trial.get("all_key_event_ids") or []):
            event_key = str(event_id)
            referenced_by_trial.setdefault(event_key, []).append(trial_index)
            if event_key not in available:
                missing_refs.append({"trial_index": trial_index, "event_id": event_id})
    if missing_refs:
        preview = ", ".join(
            f"trial {row['trial_index']} -> {row['event_id']}" for row in missing_refs[:10]
        )
        warnings.append(f"{phase}: {len(missing_refs)} trial/key references are missing ({preview})")
        issues.append(
            quality_issue(
                "keypress.trial_reference_missing",
                "keypress",
                "Trial records reference key events absent from the canonical key ledger",
                phase=phase,
                session_dir=session_dir,
                trial_indices=(row["trial_index"] for row in missing_refs),
                details=missing_refs,
                count=len(missing_refs),
                recoverability="not_recoverable",
                suggested_action="Reconcile against event JSONL; exclude affected response outcomes if the key event cannot be recovered.",
                detector="study1_validate.quick_reconciliation",
            )
        )
    multiply_assigned = [
        {"event_id": event_id, "trial_indices": trial_values}
        for event_id, trial_values in referenced_by_trial.items()
        if len(trial_values) > 1
    ]
    if multiply_assigned:
        warnings.append(f"{phase}: {len(multiply_assigned)} key event(s) are referenced by multiple trials")
        issues.append(
            quality_issue(
                "keypress.multiple_trial_assignment",
                "keypress",
                "Key events are referenced by more than one trial",
                phase=phase,
                session_dir=session_dir,
                trial_indices=(
                    trial_index
                    for row in multiply_assigned
                    for trial_index in row["trial_indices"]
                ),
                details=multiply_assigned,
                count=len(multiply_assigned),
                recoverability="likely_correctable",
                suggested_action="Assign each key event once using its captured trial identity and timestamp.",
                detector="study1_validate.quick_reconciliation",
            )
        )
    trial_indices = [row.get("global_trial_index") for row in trials]
    if len(trial_indices) != len(set(trial_indices)):
        warnings.append(f"{phase}: trial ledger contains duplicate global trial indices")
        duplicate_trials = sorted(
            {value for value in trial_indices if value is not None and trial_indices.count(value) > 1}
        )
        issues.append(
            quality_issue(
                "trial.duplicate_index",
                "trial",
                "Trial ledger contains duplicate global trial indices",
                phase=phase,
                session_dir=session_dir,
                trial_indices=duplicate_trials,
                count=len(trial_indices) - len(set(trial_indices)),
                recoverability="partially_recoverable",
                suggested_action="Use sequence identity, event order, and timestamps to identify duplicate rows.",
                detector="study1_validate.quick_reconciliation",
            )
        )
    sequence_ids = {str(row.get("sequence_id")) for row in trials if row.get("sequence_id")}
    expected_sequence = str(manifest.get("sequence_id") or "")
    if expected_sequence and sequence_ids != {expected_sequence}:
        warnings.append(
            f"{phase}: trial sequence identity {sorted(sequence_ids)} does not match stimulus manifest {expected_sequence}"
        )
        issues.append(
            quality_issue(
                "trial.sequence_identity_mismatch",
                "trial",
                "Trial ledger and stimulus manifest sequence identities differ",
                phase=phase,
                session_dir=session_dir,
                observed={"trial_sequence_ids": sorted(sequence_ids)},
                expected={"stimulus_manifest_sequence_id": expected_sequence},
                recoverability="unknown",
                suggested_action="Do not pool the session until the prepared-sequence provenance has been resolved.",
                detector="study1_validate.quick_reconciliation",
            )
        )
    presented = [row for row in list(manifest.get("trials") or []) if row.get("presented")]
    if presented and len(presented) != len(trials):
        warnings.append(
            f"{phase}: stimulus manifest/trial ledger count mismatch: manifest={len(presented)}, trials={len(trials)}"
        )
        issues.append(
            quality_issue(
                "trial.stimulus_manifest_count_mismatch",
                "trial",
                "Presented stimulus-manifest and trial-ledger counts differ",
                phase=phase,
                session_dir=session_dir,
                observed={"trial_ledger_count": len(trials)},
                expected={"presented_manifest_count": len(presented)},
                recoverability="partially_recoverable",
                suggested_action="Reconcile presented flags, stimulus events, and trial rows by global trial index.",
                detector="study1_validate.quick_reconciliation",
            )
        )
    result_summary = dict(dict(parsed.get("task_results") or {}).get("summary") or {})
    declared_count = result_summary.get("experimental_trials")
    experimental_rows = [row for row in trials if not bool(row.get("is_practice"))]
    if declared_count is not None and int(declared_count) != len(experimental_rows):
        warnings.append(
            f"{phase}: task result declares {declared_count} experimental trials; ledger has {len(experimental_rows)}"
        )
        issues.append(
            quality_issue(
                "trial.declared_count_mismatch",
                "trial",
                "Task summary and canonical trial-ledger counts differ",
                phase=phase,
                session_dir=session_dir,
                observed={"task_summary_experimental_trials": int(declared_count)},
                expected={"trial_ledger_experimental_trials": len(experimental_rows)},
                recoverability="likely_correctable",
                suggested_action="Recompute behavioral summaries from the canonical trial ledger.",
                detector="study1_validate.quick_reconciliation",
            )
        )
    return warnings, issues


def _recompute_task_scoring(
    parsed: dict[str, Any],
    *,
    session_dir: Path,
    phase: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    warnings: list[str] = []
    issues: list[dict[str, Any]] = []
    parameters = dict(parsed.get("parameters") or {})
    try:
        config = DynamicSartConfig.from_mapping(parameters.get("tasks", {}).get("dynamic_sart", {}))
    except Exception as exc:
        return (
            [f"{phase}: task scoring could not be reconstructed from parameters: {type(exc).__name__}: {exc}"],
            [],
        )
    key_rows = list(parsed.get("key_event_ledger") or [])
    keys_by_id = {
        str(row.get("event_id")): row
        for row in key_rows
        if row.get("event_id")
    }
    mismatches = []
    for trial in list(parsed.get("trial_ledger") or []):
        if trial.get("aborted") or trial.get("invalid"):
            continue
        required_numbers = [
            trial.get("stimulus_onset_monotonic"),
            trial.get("stimulus_offset_monotonic"),
            trial.get("scheduled_response_window_close_monotonic"),
            trial.get("response_window_close_monotonic"),
        ]
        if any(value is None for value in required_numbers):
            mismatches.append((trial.get("global_trial_index"), "missing_scoring_timestamps", {}))
            continue
        trial_index = trial.get("global_trial_index")
        referenced_events = [
            keys_by_id[str(value)]
            for value in list(trial.get("all_key_event_ids") or [])
            if str(value) in keys_by_id
        ]
        onset = float(trial["stimulus_onset_monotonic"])
        scheduled_close = float(trial["scheduled_response_window_close_monotonic"])
        events = [
            row
            for row in key_rows
            if (
                _optional_float(row.get("timestamp_monotonic")) is not None
                and onset <= float(row["timestamp_monotonic"]) <= scheduled_close
            )
        ]
        window_ids = {str(row.get("event_id")) for row in events if row.get("event_id")}
        referenced_window_ids = {
            str(row.get("event_id"))
            for row in referenced_events
            if (
                _optional_float(row.get("timestamp_monotonic")) is not None
                and onset <= float(row["timestamp_monotonic"]) <= scheduled_close
            )
        }
        if window_ids != referenced_window_ids:
            mismatches.append(
                (
                    trial_index,
                    "key_event_window_assignment",
                    {
                        "timestamp_assigned_event_ids": sorted(window_ids),
                        "recorded_window_event_ids": sorted(referenced_window_ids),
                    },
                )
            )
        task_start = onset - float(trial.get("time_on_task_seconds") or 0.0)
        try:
            recomputed = score_dynamic_sart_trial(
                trial,
                events,
                config,
                session_id=str(trial.get("session_id") or session_dir.name),
                participant_id=str(trial.get("participant_id") or ""),
                task_start_monotonic=task_start,
                stimulus_onset_monotonic=onset,
                stimulus_onset_lsl=_optional_float(trial.get("stimulus_onset_lsl")),
                stimulus_offset_monotonic=float(trial["stimulus_offset_monotonic"]),
                stimulus_offset_lsl=_optional_float(trial.get("stimulus_offset_lsl")),
                scheduled_response_window_close_monotonic=float(trial["scheduled_response_window_close_monotonic"]),
                scheduled_response_window_close_lsl=_optional_float(trial.get("scheduled_response_window_close_lsl")),
                response_window_close_monotonic=float(trial["response_window_close_monotonic"]),
                response_window_close_lsl=_optional_float(trial.get("response_window_close_lsl")),
                scheduled_next_trial_onset_monotonic=_optional_float(trial.get("scheduled_next_trial_onset_monotonic")),
                display_timing=dict(parsed.get("stimulus_manifest", {}).get("display_timing") or {}),
                time_since_break_seconds=_optional_float(trial.get("time_since_break_seconds")),
                previous_trial_index=trial.get("previous_trial_index"),
                trials_since_no_go=trial.get("trials_since_no_go"),
                applied_task_actions=list(trial.get("applied_task_actions") or []),
            )
        except Exception as exc:
            mismatches.append(
                (
                    trial.get("global_trial_index"),
                    "recompute_error",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
            )
            continue
        for field in (
            "primary_outcome",
            "correct",
            "commission_error",
            "omission_error",
            "too_fast_response",
            "multiple_response",
            "wrong_key_response",
            "response_key_count",
        ):
            if recomputed.get(field) != trial.get(field):
                mismatches.append(
                    (
                        trial.get("global_trial_index"),
                        field,
                        {"recorded": trial.get(field), "recomputed": recomputed.get(field)},
                    )
                )
        recorded_rt = _optional_float(trial.get("reaction_time_seconds"))
        recomputed_rt = _optional_float(recomputed.get("reaction_time_seconds"))
        if (recorded_rt is None) != (recomputed_rt is None) or (
            recorded_rt is not None and recomputed_rt is not None and abs(recorded_rt - recomputed_rt) > 1e-9
        ):
            mismatches.append(
                (
                    trial.get("global_trial_index"),
                    "reaction_time_seconds",
                    {"recorded": recorded_rt, "recomputed": recomputed_rt},
                )
            )
    if mismatches:
        preview = ", ".join(f"trial {trial}: {field}" for trial, field, _detail in mismatches[:20])
        warnings.append(f"{phase}: {len(mismatches)} recomputed scoring field(s) differ ({preview})")
        issues.append(
            quality_issue(
                "trial.recomputed_scoring_mismatch",
                "trial",
                "Recorded trial response assignment or scoring differs from recomputation",
                phase=phase,
                session_dir=session_dir,
                trial_indices=(trial for trial, _field, _detail in mismatches),
                details=(
                    {"trial_index": trial, "field": field, **detail}
                    for trial, field, detail in mismatches
                ),
                count=len(mismatches),
                recoverability="likely_correctable",
                suggested_action="Regenerate behavioral outcomes from the canonical key-event ledger and retained trial windows.",
                detector="study1_validate.comprehensive_scoring",
            )
        )
    return warnings, issues


def _classify_xdf_failures(validation: dict[str, Any]) -> tuple[list[str], list[str]]:
    unreadable = []
    warnings = list(validation.get("warnings") or [])
    for message in list(validation.get("failures") or []):
        text = str(message)
        lowered = text.casefold()
        if any(
            token in lowered
            for token in (
                "recording is missing",
                "recording is empty",
                "structure could not be read",
                "sample chunks could not be read",
                "corrupt or unreadable chunks",
                "pyxdf import failed",
            )
        ):
            unreadable.append(text)
        else:
            warnings.append(text)
    return unreadable, warnings


def _inspect_artifact(
    path: Path,
    *,
    role: str,
    data_root: Path,
    required: bool,
    comprehensive: bool,
    core_errors: list[str],
) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        if required:
            core_errors.append(f"required canonical artifact is missing: {path}")
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        if required:
            core_errors.append(f"required canonical artifact is unreadable: {path}: {type(exc).__name__}: {exc}")
        return None
    if required and size <= 0:
        core_errors.append(f"required canonical artifact is empty: {path}")
    digest = None
    if comprehensive:
        try:
            digest = _sha256_file(path)
        except OSError as exc:
            if required:
                core_errors.append(
                    f"required canonical artifact could not be hashed: {path}: "
                    f"{type(exc).__name__}: {exc}"
                )
    relative, beneath_data_root = _relative_to_root(path, data_root)
    return {
        "relative_path": relative,
        "absolute_path": str(path),
        "role": role,
        "canonical": bool(required),
        "required": required,
        "beneath_data_root": beneath_data_root,
        "media_type": _media_type(path),
        "validated_at": _now(),
        "size_bytes": size,
        "sha256": digest,
    }


def _verify_backup(
    artifacts: list[dict[str, Any]],
    *,
    data_root: Path,
    backup_root: Path,
) -> dict[str, Any]:
    warnings = []
    files = []
    for artifact in artifacts:
        relative = Path(str(artifact["relative_path"]))
        backup_path = backup_root.expanduser() / relative
        row = {
            "relative_path": str(relative),
            "backup_path": str(backup_path),
            "status": "missing",
            "size_matches": False,
            "sha256_matches": False,
        }
        if not backup_path.is_file():
            warnings.append(f"backup is missing {relative}")
        else:
            try:
                size = backup_path.stat().st_size
                digest = _sha256_file(backup_path)
                row.update(
                    {
                        "backup_size_bytes": size,
                        "backup_sha256": digest,
                        "size_matches": size == artifact.get("size_bytes"),
                        "sha256_matches": digest == artifact.get("sha256"),
                    }
                )
                row["status"] = "match" if row["size_matches"] and row["sha256_matches"] else "mismatch"
                if row["status"] != "match":
                    warnings.append(f"backup differs from acquisition copy: {relative}")
            except OSError as exc:
                row["status"] = "unreadable"
                warnings.append(f"backup is unreadable {relative}: {type(exc).__name__}: {exc}")
        files.append(row)
    return {
        "status": "warning" if warnings else "match",
        "data_root": str(data_root),
        "backup_root": str(backup_root),
        "warnings": warnings,
        "files": files,
    }


def print_validation_summary(report: dict[str, Any]) -> None:
    print(f"Study 1 post-run validation: {report['status'].upper()} ({report['mode']})")
    print(f"Participant: {report.get('participant_id')}")
    print(f"Visit: {report.get('visit_number')} / {report.get('visit_id')}")
    print(f"Sessions checked: {len(report.get('sessions') or [])}")
    print(f"Core unreadable errors: {len(report.get('core_errors') or [])}")
    print(f"Warnings: {len(report.get('warnings') or [])}")
    print(f"Structured quality issues: {len(report.get('quality_issues') or [])}")
    print(f"Report: {report.get('report_file')}")
    for message in list(report.get("core_errors") or []):
        print(f"  CORE: {message}")
    for message in list(report.get("warnings") or [])[:20]:
        print(f"  WARN: {message}")


def _resolve_xdf_headers(path: Path) -> list[dict[str, Any]]:
    import pyxdf

    return list(pyxdf.resolve_streams(str(path)))


def _read_json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("expected a JSON object")
    return payload


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"line {line_number}: expected a JSON object")
            rows.append(row)
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        if not reader.fieldnames:
            raise ValueError("CSV header is missing")
        return list(reader)


def _validate_parsed_artifact(role: str, payload: Any) -> None:
    required_fields = {
        "independent_marker_receipt": {"marker_label", "lsl_timestamp"},
        "trial_ledger": {"global_trial_index", "sequence_id", "all_key_event_ids"},
        "key_event_ledger": {"event_id", "key", "timestamp_monotonic"},
        "event_ledger": {"label"},
    }
    fields = required_fields.get(role)
    if role == "stimulus_manifest":
        if not isinstance(payload.get("trials"), list) or not payload.get("sequence_id"):
            raise ValueError("stimulus manifest is missing sequence_id or trials")
        return
    if role == "task_results":
        if not isinstance(payload.get("summary"), dict):
            raise ValueError("task results are missing the summary object")
        return
    if fields is None:
        return
    if not isinstance(payload, list):
        raise ValueError("expected a row ledger")
    for row_number, row in enumerate(payload, start=1):
        missing = sorted(field for field in fields if field not in row)
        if missing:
            raise ValueError(f"row {row_number} is missing required field(s): {', '.join(missing)}")


def _declared_schema(payload: Any) -> str | None:
    if isinstance(payload, dict):
        value = payload.get("schema") or payload.get("schema_version")
        return None if value is None else str(value)
    if isinstance(payload, list):
        values = {
            str(row.get("schema") or row.get("schema_version"))
            for row in payload
            if isinstance(row, dict) and (row.get("schema") is not None or row.get("schema_version") is not None)
        }
        return ",".join(sorted(values)) or None
    return None


def _session_reference_warnings(
    parsed: dict[str, Any],
    *,
    phase: str,
    session_dir: Path,
    visit_manifest: dict[str, Any],
) -> list[str]:
    warnings = []
    session_manifest = dict(parsed.get("session_manifest") or {})
    parameters = dict(parsed.get("parameters") or {})
    identities = {
        "visit manifest": visit_manifest.get("participant_id"),
        "session manifest": session_manifest.get("participant_id"),
        "session parameters": parameters.get("experiment", {}).get("participant_id"),
    }
    present_identities = {label: str(value) for label, value in identities.items() if value not in {None, ""}}
    if len(set(present_identities.values())) > 1:
        warnings.append(f"{phase}: participant identity differs across manifests: {present_identities}")
    expected_tasks = (
        {"resting_baseline", "dsart_baseline", "study1_baseline"}
        if "baseline" in phase
        else {"dynamic_sart"}
    )
    if session_manifest.get("task") and session_manifest.get("task") not in expected_tasks:
        warnings.append(
            f"{phase}: session manifest task={session_manifest.get('task')!r} does not match the phase"
        )
    session_ids = {
        str(row.get("session_id"))
        for row in list(parsed.get("trial_ledger") or [])
        if row.get("session_id")
    }
    if session_ids and session_ids != {session_dir.name}:
        warnings.append(
            f"{phase}: trial session identities {sorted(session_ids)} do not match directory {session_dir.name}"
        )
    expected_sequence = dict(visit_manifest.get("sequence_hashes") or {}).get(phase)
    if expected_sequence is None:
        attempts = list(
            dict(visit_manifest.get("phases", {}).get(phase, {}) or {}).get("attempts") or []
        )
        expected_sequence = next(
            (
                dict(attempt.get("result") or {}).get("sequence_hash")
                for attempt in reversed(attempts)
                if isinstance(attempt.get("result"), dict)
                and dict(attempt.get("result") or {}).get("sequence_hash")
            ),
            None,
        )
    observed_sequences = {
        "stimulus manifest": dict(parsed.get("stimulus_manifest") or {}).get("sequence_id"),
        "task results": dict(parsed.get("task_results") or {}).get("sequence_id"),
    }
    observed_sequences.update(
        {
            "trial ledger": next(
                iter(
                    {
                        str(row.get("sequence_id"))
                        for row in list(parsed.get("trial_ledger") or [])
                        if row.get("sequence_id")
                    }
                ),
                None,
            )
        }
    )
    for label, observed in observed_sequences.items():
        if expected_sequence and observed and str(observed) != str(expected_sequence):
            warnings.append(
                f"{phase}: {label} sequence identity {observed!r} does not match visit phase {expected_sequence!r}"
            )
    recorder_status = dict(parsed.get("recorder_process_status") or {})
    if recorder_status and recorder_status.get("status") != "stopped":
        warnings.append(
            f"{phase}: recorder process status is {recorder_status.get('status')!r}; expected 'stopped'"
        )
    return warnings


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_to_root(path: Path, root: Path) -> tuple[str, bool]:
    try:
        return str(path.resolve().relative_to(root.resolve())), True
    except ValueError:
        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:20]
        return str(Path("external") / digest / path.name), False


def _media_type(path: Path) -> str:
    return {
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".csv": "text/csv",
        ".xdf": "application/x-xdf",
        ".txt": "text/plain",
    }.get(path.suffix.casefold(), "application/octet-stream")


def _data_root_for_manifest(path: Path) -> Path:
    for parent in path.parents:
        if parent.name == "study1":
            return parent.parent
    raise ValueError(f"visit manifest is not beneath a study1 data directory: {path}")


def _nested_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _nested_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _nested_strings(item)


def _deduplicate_artifacts(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {}
    for row in rows:
        unique[str(row.get("absolute_path"))] = row
    return sorted(unique.values(), key=lambda row: str(row.get("relative_path")))


def _deduplicate_issues(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = []
    seen = set()
    for row in rows:
        key = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{monotonic_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        _replace_atomic_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_atomic_file(source: Path, target: Path) -> None:
    last_error: OSError | None = None
    for delay in (0.0, 0.05, 0.1, 0.2):
        if delay:
            sleep(delay)
        try:
            os.replace(source, target)
            return
        except OSError as exc:
            if not (
                isinstance(exc, PermissionError)
                or getattr(exc, "winerror", None) in {5, 32, 33}
            ):
                raise
            last_error = exc
    if last_error is not None:
        raise last_error


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
