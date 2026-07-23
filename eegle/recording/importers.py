"""One-time import of explicitly supported historical EEGle sessions.

This module is not a legacy execution runtime. It copies selected durable
artifacts into a new generic session, leaves the source unchanged, and reports
every source file as imported, omitted, or invalid. New import metadata is
reported separately as derived.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.recording.artifacts import (
    ArtifactLineage,
    ArtifactReference,
    Sensitivity,
)
from eegle.recording.bundles import EvidenceBundle, EvidenceWriter
from eegle.recording.evidence import EvidenceRecord, EvidenceStatus
from eegle.recording.legacy_capture import read_legacy_engine_capture
from eegle.recording.session import Session, SessionStatus
from eegle.streams.clocks import TimePoint


LEGACY_IMPORT_REPORT_SCHEMA = "eegle.legacy_import_report.v1"
SUPPORTED_LEGACY_FAMILY = "eegle.legacy_session.bcipy_style.v1"
LEGACY_CAPTURE_MEDIA_TYPE = "application/vnd.eegle.legacy-engine-capture"


@dataclass(frozen=True, slots=True)
class LegacyImportItem:
    item_id: str
    disposition: str
    reason: str
    source_uri: str | None = None
    source_digest: str | None = None
    source_size_bytes: int | None = None
    artifact: ArtifactReference | None = None
    details: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "item_id", require_identifier(self.item_id, "item_id"))
        if self.disposition not in {"imported", "derived", "omitted", "invalid"}:
            raise ValueError(f"unsupported import disposition: {self.disposition}")
        if not self.reason.strip():
            raise ValueError("legacy import item reason cannot be empty")
        if self.source_uri is not None:
            source = Path(self.source_uri)
            if source.is_absolute() or not source.parts or ".." in source.parts:
                raise ValueError("legacy import source_uri must be a safe relative path")
            object.__setattr__(self, "source_uri", source.as_posix())
        if self.source_digest is not None:
            object.__setattr__(
                self,
                "source_digest",
                require_digest(self.source_digest, "source_digest"),
            )
        if self.source_size_bytes is not None:
            object.__setattr__(self, "source_size_bytes", int(self.source_size_bytes))
            if self.source_size_bytes < 0:
                raise ValueError("legacy import source_size_bytes cannot be negative")
        object.__setattr__(self, "details", freeze_json(self.details or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "disposition": self.disposition,
            "reason": self.reason,
            "source_uri": self.source_uri,
            "source_digest": self.source_digest,
            "source_size_bytes": self.source_size_bytes,
            "artifact": None if self.artifact is None else self.artifact.to_payload(),
            "details": thaw_json(self.details),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LegacyImportItem":
        artifact = payload.get("artifact")
        return cls(
            item_id=str(payload["item_id"]),
            disposition=str(payload["disposition"]),
            reason=str(payload["reason"]),
            source_uri=(
                None if payload.get("source_uri") is None else str(payload["source_uri"])
            ),
            source_digest=(
                None
                if payload.get("source_digest") is None
                else str(payload["source_digest"])
            ),
            source_size_bytes=(
                None
                if payload.get("source_size_bytes") is None
                else int(payload["source_size_bytes"])
            ),
            artifact=(
                None if artifact is None else ArtifactReference.from_payload(artifact)
            ),
            details=dict(payload.get("details") or {}),
        )


@dataclass(frozen=True, slots=True)
class LegacyImportReport:
    source_family: str
    source_session_id: str
    source_fingerprint: str
    destination_session_id: str
    imported: tuple[LegacyImportItem, ...] = ()
    derived: tuple[LegacyImportItem, ...] = ()
    omitted: tuple[LegacyImportItem, ...] = ()
    invalid: tuple[LegacyImportItem, ...] = ()
    producer_integrity: str = "unavailable"
    schema: str = LEGACY_IMPORT_REPORT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != LEGACY_IMPORT_REPORT_SCHEMA:
            raise ValueError(f"unsupported legacy import report schema: {self.schema}")
        if self.source_family != SUPPORTED_LEGACY_FAMILY:
            raise ValueError(f"unsupported legacy source family: {self.source_family}")
        if self.producer_integrity not in {"unavailable", "partial", "verified"}:
            raise ValueError(
                f"unsupported producer integrity classification: {self.producer_integrity}"
            )
        object.__setattr__(
            self,
            "source_session_id",
            require_identifier(self.source_session_id, "source_session_id"),
        )
        object.__setattr__(
            self,
            "destination_session_id",
            require_identifier(self.destination_session_id, "destination_session_id"),
        )
        object.__setattr__(
            self,
            "source_fingerprint",
            require_digest(self.source_fingerprint, "source_fingerprint"),
        )
        for disposition in ("imported", "derived", "omitted", "invalid"):
            if any(item.disposition != disposition for item in getattr(self, disposition)):
                raise ValueError(f"{disposition} report list contains a mismatched item")
        identifiers = [
            item.item_id
            for collection in (self.imported, self.derived, self.omitted, self.invalid)
            for item in collection
        ]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("legacy import report item IDs must be unique")

    @property
    def report_hash(self) -> str:
        return canonical_hash(self.content_payload())

    @property
    def counts(self) -> dict[str, int]:
        return {
            "imported": len(self.imported),
            "derived": len(self.derived),
            "omitted": len(self.omitted),
            "invalid": len(self.invalid),
        }

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "source_family": self.source_family,
            "source_session_id": self.source_session_id,
            "source_fingerprint": self.source_fingerprint,
            "destination_session_id": self.destination_session_id,
            "producer_integrity": self.producer_integrity,
            "counts": self.counts,
            "imported": [item.to_payload() for item in self.imported],
            "derived": [item.to_payload() for item in self.derived],
            "omitted": [item.to_payload() for item in self.omitted],
            "invalid": [item.to_payload() for item in self.invalid],
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["report_hash"] = self.report_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LegacyImportReport":
        report = cls(
            schema=str(payload.get("schema", LEGACY_IMPORT_REPORT_SCHEMA)),
            source_family=str(payload["source_family"]),
            source_session_id=str(payload["source_session_id"]),
            source_fingerprint=str(payload["source_fingerprint"]),
            destination_session_id=str(payload["destination_session_id"]),
            producer_integrity=str(payload.get("producer_integrity", "unavailable")),
            imported=tuple(
                LegacyImportItem.from_payload(item) for item in payload.get("imported", ())
            ),
            derived=tuple(
                LegacyImportItem.from_payload(item) for item in payload.get("derived", ())
            ),
            omitted=tuple(
                LegacyImportItem.from_payload(item) for item in payload.get("omitted", ())
            ),
            invalid=tuple(
                LegacyImportItem.from_payload(item) for item in payload.get("invalid", ())
            ),
        )
        if payload.get("report_hash") != report.report_hash:
            raise ValueError("legacy import report hash mismatch")
        return report


@dataclass(frozen=True, slots=True)
class LegacyImportResult:
    session: Session
    bundle: EvidenceBundle
    report: LegacyImportReport


@dataclass(frozen=True, slots=True)
class LegacyImportRule:
    category: str
    role: str
    media_type: str
    sensitivity: Sensitivity
    bundle_group: str = "artifact"

    def __post_init__(self) -> None:
        object.__setattr__(self, "category", require_identifier(self.category, "category"))
        object.__setattr__(self, "role", require_identifier(self.role, "role"))
        if not self.media_type.strip():
            raise ValueError("legacy import media_type cannot be empty")
        object.__setattr__(self, "sensitivity", Sensitivity(self.sensitivity))
        if self.bundle_group not in {"artifact", "execution", "raw"}:
            raise ValueError(f"unsupported legacy bundle group: {self.bundle_group}")


_RULES: dict[str, LegacyImportRule] = {
    "parameters.json": LegacyImportRule("protocol", "protocol_parameters", "application/json", Sensitivity.INTERNAL),
    "manifest.json": LegacyImportRule("protocol", "scientific_manifest", "application/json", Sensitivity.RESTRICTED),
    "triggers.txt": LegacyImportRule("events", "trigger_ledger", "text/plain", Sensitivity.PSEUDONYMIZED),
    "events/behavior.csv": LegacyImportRule("events", "behavior_events", "text/csv", Sensitivity.PSEUDONYMIZED),
    "events/events.jsonl": LegacyImportRule("events", "event_ledger", "application/x-ndjson", Sensitivity.PSEUDONYMIZED),
    "events/stimulus_manifest.json": LegacyImportRule("events", "scientific_event_manifest", "application/json", Sensitivity.PSEUDONYMIZED),
    "calibration/events.jsonl": LegacyImportRule("calibration", "calibration_events", "application/x-ndjson", Sensitivity.PSEUDONYMIZED),
    "calibration/metadata.json": LegacyImportRule("calibration", "calibration_metadata", "application/json", Sensitivity.INTERNAL),
    "calibration/eeg.csv": LegacyImportRule("raw", "archival_raw", "text/csv", Sensitivity.RESTRICTED, "raw"),
    "calibration/alpha_calibration.json": LegacyImportRule("calibration", "calibration_result", "application/json", Sensitivity.PSEUDONYMIZED),
    "calibration/psd.csv": LegacyImportRule("calibration", "spectral_result", "text/csv", Sensitivity.PSEUDONYMIZED),
    "calibration/specparam.json": LegacyImportRule("calibration", "spectral_model", "application/json", Sensitivity.PSEUDONYMIZED),
    "raw/eeg.csv": LegacyImportRule("raw", "archival_raw", "text/csv", Sensitivity.RESTRICTED, "raw"),
    "raw/eeg_metadata.json": LegacyImportRule("raw", "raw_metadata", "application/json", Sensitivity.RESTRICTED),
    "raw/lsl_markers_received.csv": LegacyImportRule("raw", "archival_event_raw", "text/csv", Sensitivity.RESTRICTED),
    "raw/lsl_markers_received_metadata.json": LegacyImportRule("raw", "raw_metadata", "application/json", Sensitivity.RESTRICTED),
    "realtime/windows.jsonl": LegacyImportRule("runtime", "window_ledger", "application/x-ndjson", Sensitivity.PSEUDONYMIZED),
    "realtime/decisions.jsonl": LegacyImportRule("runtime", "decision_ledger", "application/x-ndjson", Sensitivity.PSEUDONYMIZED),
    "realtime/model_predictions.jsonl": LegacyImportRule("runtime", "prediction_ledger", "application/x-ndjson", Sensitivity.PSEUDONYMIZED),
    "realtime/markers.jsonl": LegacyImportRule("runtime", "marker_ledger", "application/x-ndjson", Sensitivity.PSEUDONYMIZED),
    "realtime/feedback.jsonl": LegacyImportRule("runtime", "feedback_ledger", "application/x-ndjson", Sensitivity.PSEUDONYMIZED),
    "realtime/alpha_power.jsonl": LegacyImportRule("runtime", "feature_ledger", "application/x-ndjson", Sensitivity.PSEUDONYMIZED),
    "realtime/event_features.jsonl": LegacyImportRule("runtime", "feature_ledger", "application/x-ndjson", Sensitivity.PSEUDONYMIZED),
    "realtime/adaptation_updates.jsonl": LegacyImportRule("runtime", "adaptation_ledger", "application/x-ndjson", Sensitivity.RESTRICTED),
    "realtime/demo_predictions.jsonl": LegacyImportRule("runtime", "simulated_prediction_ledger", "application/x-ndjson", Sensitivity.PSEUDONYMIZED),
    "realtime/engine_input.bin": LegacyImportRule("runtime", "execution_capture", LEGACY_CAPTURE_MEDIA_TYPE, Sensitivity.PSEUDONYMIZED, "execution"),
    "realtime/engine_metadata.json": LegacyImportRule("runtime", "execution_metadata", "application/json", Sensitivity.INTERNAL),
    "realtime/epochs/epochs.jsonl": LegacyImportRule("epochs", "epoch_ledger", "application/x-ndjson", Sensitivity.PSEUDONYMIZED),
    "realtime/epochs/epochs.npz": LegacyImportRule("epochs", "epoch_arrays", "application/x-npz", Sensitivity.RESTRICTED),
    "realtime/epochs/manifest.json": LegacyImportRule("epochs", "epoch_manifest", "application/json", Sensitivity.PSEUDONYMIZED),
}


class LegacySessionImporter:
    """Import the selected BciPy-style historical family into a new session."""

    def __init__(
        self,
        *,
        additional_rules: Mapping[str, LegacyImportRule] | None = None,
    ) -> None:
        rules = dict(_RULES)
        for uri, rule in (additional_rules or {}).items():
            source = Path(str(uri))
            if source.is_absolute() or not source.parts or ".." in source.parts:
                raise ValueError("legacy import rule URI must be a safe relative path")
            if not isinstance(rule, LegacyImportRule):
                raise TypeError("legacy import rules require LegacyImportRule values")
            rules[source.as_posix()] = rule
        self._rules = rules

    def import_session(
        self,
        source_root: str | Path,
        destination_root: str | Path,
        *,
        session_id: str,
        participant_pseudonym: str | None = None,
    ) -> LegacyImportResult:
        source_path = Path(source_root).expanduser().resolve()
        _confirm_supported_source(source_path)
        source = Session.open(source_path, allow_legacy=True)
        if not source.legacy or source.manifest.origin_schema != SUPPORTED_LEGACY_FAMILY:
            raise ValueError("source is not the explicitly supported historical session family")
        source_files = tuple(
            sorted(
                (path for path in source.root.rglob("*") if path.is_file()),
                key=lambda path: path.relative_to(source.root).as_posix(),
            )
        )
        inventory = tuple(
            _source_inventory_item(source.root, path) for path in source_files
        )
        source_fingerprint = canonical_hash(
            {
                "family": SUPPORTED_LEGACY_FAMILY,
                "files": [
                    {"uri": uri, "digest": digest, "size_bytes": size}
                    for uri, digest, size in inventory
                ],
            }
        )
        destination = Session.create(
            destination_root,
            session_id=session_id,
            participant_pseudonym=participant_pseudonym,
            metadata={
                "importer_schema": LEGACY_IMPORT_REPORT_SCHEMA,
                "source_family": SUPPORTED_LEGACY_FAMILY,
                "source_fingerprint": source_fingerprint,
            },
        )

        imported: list[LegacyImportItem] = []
        omitted: list[LegacyImportItem] = []
        invalid: list[LegacyImportItem] = []
        references: list[tuple[ArtifactReference, str]] = []
        for index, (uri, digest, size) in enumerate(inventory):
            rule = self._rule_for(uri)
            item_id = f"legacy.item.{index}"
            if rule is None:
                omitted.append(
                    LegacyImportItem(
                        item_id=item_id,
                        disposition="omitted",
                        reason=_omission_reason(uri),
                        source_uri=uri,
                        source_digest=digest,
                        source_size_bytes=size,
                    )
                )
                continue
            source_path = source.root / uri
            try:
                validation = _validate_source_file(source_path, rule)
            except (OSError, UnicodeDecodeError, ValueError, zipfile.BadZipFile) as exc:
                invalid.append(
                    LegacyImportItem(
                        item_id=item_id,
                        disposition="invalid",
                        reason="source_validation_failed",
                        source_uri=uri,
                        source_digest=digest,
                        source_size_bytes=size,
                        details={"error": str(exc)},
                    )
                )
                continue
            artifact_id = _artifact_id(uri, index)
            reference = destination.artifacts.register_file(
                f"legacy-import/{rule.category}",
                artifact_id,
                rule.role,
                source_path,
                rule.media_type,
                sensitivity=rule.sensitivity,
                lineage=ArtifactLineage(
                    component_id="eegle.legacy-importer",
                    component_version="1",
                    input_artifact_ids=(f"legacy.source.{index}",),
                    input_digests=(digest,),
                    metadata={
                        "source_uri": uri,
                        "source_family": SUPPORTED_LEGACY_FAMILY,
                        "source_integrity": "observed_at_import",
                    },
                ),
            )
            if reference.digest != digest or reference.size_bytes != size:
                raise RuntimeError(f"legacy source changed during import: {uri}")
            imported.append(
                LegacyImportItem(
                    item_id=item_id,
                    disposition="imported",
                    reason="selected_durable_artifact",
                    source_uri=uri,
                    source_digest=digest,
                    source_size_bytes=size,
                    artifact=reference,
                    details={
                        "integrity_basis": "observed_at_import",
                        **validation,
                    },
                )
            )
            references.append((reference, rule.bundle_group))

        derived = (
            LegacyImportItem(
                item_id="legacy.derived.destination-session",
                disposition="derived",
                reason="generic_session_identity_derived_from_import_request",
                details={"session_id": destination.session_id},
            ),
            LegacyImportItem(
                item_id="legacy.derived.import-report",
                disposition="derived",
                reason="classification_and_provenance_generated_by_importer",
                details={"source_fingerprint": source_fingerprint},
            ),
        )
        report = LegacyImportReport(
            source_family=SUPPORTED_LEGACY_FAMILY,
            source_session_id=source.session_id,
            source_fingerprint=source_fingerprint,
            destination_session_id=destination.session_id,
            imported=tuple(imported),
            derived=derived,
            omitted=tuple(omitted),
            invalid=tuple(invalid),
        )
        report_reference = destination.artifacts.register_json(
            "legacy-import/report",
            "legacy.import-report",
            "import_report",
            report.to_payload(),
            sensitivity=Sensitivity.INTERNAL,
            lineage=ArtifactLineage(
                component_id="eegle.legacy-importer",
                component_version="1",
                input_artifact_ids=tuple(
                    f"legacy.source.{index}" for index in range(len(inventory))
                ),
                input_digests=tuple(digest for _, digest, _ in inventory),
                metadata={"source_family": SUPPORTED_LEGACY_FAMILY},
            ),
        )

        clock_id = "import.sequence"
        writer = EvidenceWriter(
            destination,
            bundle_id="bundle.legacy-import",
            plan_hash=canonical_hash(
                {
                    "importer_schema": LEGACY_IMPORT_REPORT_SCHEMA,
                    "source_family": SUPPORTED_LEGACY_FAMILY,
                    "source_fingerprint": source_fingerprint,
                }
            ),
            created_time=TimePoint(0.0, clock_id),
            metadata={
                "operation": "historical_session_import",
                "source_family": SUPPORTED_LEGACY_FAMILY,
                "source_fingerprint": source_fingerprint,
                "producer_integrity": "unavailable",
            },
        )
        records = [
            EvidenceRecord(
                "legacy.import.0",
                "legacy_import_started",
                0,
                TimePoint(0.0, clock_id),
                {
                    "source_family": SUPPORTED_LEGACY_FAMILY,
                    "source_fingerprint": source_fingerprint,
                },
            )
        ]
        all_items = (*report.imported, *report.derived, *report.omitted, *report.invalid)
        for sequence, item in enumerate(all_items, start=1):
            records.append(
                EvidenceRecord(
                    f"legacy.import.{sequence}",
                    "legacy_import_item",
                    sequence,
                    TimePoint(float(sequence), clock_id),
                    item.to_payload(),
                )
            )
        summary_sequence = len(records)
        records.append(
            EvidenceRecord(
                f"legacy.import.{summary_sequence}",
                "legacy_import_completed",
                summary_sequence,
                TimePoint(float(summary_sequence), clock_id),
                {"counts": report.counts, "report_hash": report.report_hash},
            )
        )
        for record in records:
            writer.append(record)
        for reference, group in references:
            if group == "execution":
                writer.add_execution_capture(reference)
            elif group == "raw":
                writer.add_raw_recording(reference)
            else:
                writer.add_artifact(reference)
        writer.add_artifact(report_reference)
        evidence_status = EvidenceStatus.PARTIAL if report.invalid else EvidenceStatus.COMPLETE
        bundle = writer.finalize(
            status=evidence_status,
            completed_time=TimePoint(float(summary_sequence), clock_id),
        )
        destination.finalize(
            SessionStatus.PARTIAL if report.invalid else SessionStatus.COMPLETE
        )
        return LegacyImportResult(destination, bundle, report)

    def _rule_for(self, uri: str) -> LegacyImportRule | None:
        rule = self._rules.get(uri)
        if rule is not None:
            return rule
        if uri.startswith("realtime/models/"):
            return LegacyImportRule(
                "models",
                "model_snapshot",
                "application/octet-stream",
                Sensitivity.RESTRICTED,
            )
        if uri.startswith("realtime/adaptation_state/"):
            return LegacyImportRule(
                "models",
                "adaptation_state",
                "application/octet-stream",
                Sensitivity.RESTRICTED,
            )
        return None


def _source_inventory_item(root: Path, path: Path) -> tuple[str, str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return path.relative_to(root).as_posix(), f"sha256:{digest.hexdigest()}", size


def _confirm_supported_source(root: Path) -> None:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(
            "historical import requires manifest.json for explicit family detection"
        )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("historical manifest is not valid JSON") from exc
    if not isinstance(payload, dict) or payload.get("layout") != "bcipy_style":
        raise ValueError(
            "historical manifest does not declare the supported bcipy_style family"
        )


def _omission_reason(uri: str) -> str:
    if uri.startswith("reports/") or uri.endswith((".html", ".svg", ".png")):
        return "regenerable_presentation_artifact"
    if uri.startswith("logs/"):
        return "operational_debug_or_status_artifact"
    if uri == "session_summary.json":
        return "legacy_status_summary_replaced_by_generic_session_lifecycle"
    return "unrecognized_or_unsupported_legacy_artifact"


def _validate_source_file(path: Path, rule: LegacyImportRule) -> dict[str, Any]:
    if rule.media_type == "application/json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {"json_type": type(payload).__name__}
    if rule.media_type == "application/x-ndjson":
        count = 0
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL at line {line_number}: {exc.msg}") from exc
                count += 1
        return {"record_count": count}
    if rule.media_type in {"text/plain", "text/csv"}:
        with path.open("r", encoding="utf-8") as handle:
            line_count = sum(1 for _ in handle)
        return {"line_count": line_count}
    if rule.media_type == "application/x-npz":
        with zipfile.ZipFile(path) as archive:
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise ValueError(f"NPZ member checksum failed: {corrupt_member}")
            return {"member_count": len(archive.infolist())}
    if rule.media_type == LEGACY_CAPTURE_MEDIA_TYPE:
        header, rows = read_legacy_engine_capture(path, marker_factory=lambda **values: values)
        count = sum(1 for _ in rows)
        return {"frame_count": count, "header_keys": sorted(str(key) for key in header)}
    return {}


def _artifact_id(uri: str, index: int) -> str:
    stem = "-".join(part for part in Path(uri).parts)
    normalized = "".join(
        character if character.isalnum() or character in ".-_" else "-"
        for character in stem
    )
    return require_identifier(f"legacy.{index}.{normalized}"[:250], "artifact_id")
