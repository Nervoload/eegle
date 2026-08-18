"""Read-only session evidence, replay comparison, and safe export services."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from eegle._domain import EquivalenceLevel
from eegle._validation import freeze_json, thaw_json
from eegle.compiler import ExecutionPlan, canonical_hash
from eegle.plugins import PluginRegistry
from eegle.recording import (
    EvidenceReader,
    EvidenceRecord,
    ExportPolicy,
    ExternalReferencePolicy,
    PortableExportManifest,
    Sensitivity,
    Session,
    discover_interrupted_runs,
    export_evidence_bundle,
)
from eegle.replay import BundleReplayRunner, load_recorded_execution
from eegle.runtime import ArtifactResolver, EngineStatus

SESSION_INSPECTION_SCHEMA_ID = "eegle.session_inspection.v1"
REPLAY_INSPECTION_SCHEMA_ID = "eegle.replay_inspection.v1"
MODEL_REPLACEMENT_COMPARISON_SCHEMA_ID = (
    "eegle.model_replacement_comparison.v1"
)
SESSION_EXPORT_SCHEMA_ID = "eegle.session_export.v1"


class OperationOutcome(str, Enum):
    """Non-throwing outcome used by read-only and export operations."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    DIVERGED = "diverged"
    UNAVAILABLE = "unavailable"
    NOT_EXPORTED = "not_exported"


@dataclass(frozen=True, slots=True)
class SessionIssue:
    """A bounded problem that does not terminate or mutate a session."""

    code: str
    section: str
    message: str
    severity: str = "warning"
    bundle_id: str | None = None
    recoverable: bool | None = None

    def __post_init__(self) -> None:
        if self.severity not in {"warning", "error"}:
            raise ValueError("session issue severity must be warning or error")

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "section": self.section,
            "message": self.message,
            "severity": self.severity,
            "bundle_id": self.bundle_id,
            "recoverable": self.recoverable,
        }


@dataclass(frozen=True, slots=True)
class SessionInspection:
    """Privacy-aware projection of one session and its structured evidence."""

    session_root: Path
    session: Mapping[str, Any]
    bundles: tuple[Mapping[str, Any], ...]
    summary: Mapping[str, Any]
    phase_timeline: tuple[Mapping[str, Any], ...] = ()
    source_health: Mapping[str, Any] = None  # type: ignore[assignment]
    work: Mapping[str, Any] = None  # type: ignore[assignment]
    models: Mapping[str, Any] = None  # type: ignore[assignment]
    latency: Mapping[str, Any] = None  # type: ignore[assignment]
    adaptation: Mapping[str, Any] = None  # type: ignore[assignment]
    actions: Mapping[str, Any] = None  # type: ignore[assignment]
    replay: Mapping[str, Any] = None  # type: ignore[assignment]
    issues: tuple[SessionIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_root", Path(self.session_root).expanduser().resolve()
        )
        object.__setattr__(self, "session", freeze_json(self.session))
        object.__setattr__(
            self, "bundles", tuple(freeze_json(value) for value in self.bundles)
        )
        object.__setattr__(self, "summary", freeze_json(self.summary))
        object.__setattr__(
            self,
            "phase_timeline",
            tuple(freeze_json(value) for value in self.phase_timeline),
        )
        for field_name in (
            "source_health",
            "work",
            "models",
            "latency",
            "adaptation",
            "actions",
            "replay",
        ):
            object.__setattr__(
                self, field_name, freeze_json(getattr(self, field_name) or {})
            )

    @property
    def valid(self) -> bool:
        return bool(self.bundles) and all(
            bool(value.get("valid")) for value in self.bundles
        )

    @property
    def outcome(self) -> OperationOutcome:
        if not self.session:
            return OperationOutcome.UNAVAILABLE
        if self.valid and not any(
            value.severity == "error" for value in self.issues
        ):
            return OperationOutcome.COMPLETE
        return OperationOutcome.PARTIAL

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": SESSION_INSPECTION_SCHEMA_ID,
            "status": self.outcome.value,
            "session_root": str(self.session_root),
            "valid": self.valid,
            "session": thaw_json(self.session),
            "bundles": [thaw_json(value) for value in self.bundles],
            "summary": thaw_json(self.summary),
            "phase_timeline": [
                thaw_json(value) for value in self.phase_timeline
            ],
            "source_health": thaw_json(self.source_health),
            "work": thaw_json(self.work),
            "models": thaw_json(self.models),
            "latency": thaw_json(self.latency),
            "adaptation": thaw_json(self.adaptation),
            "actions": thaw_json(self.actions),
            "replay": thaw_json(self.replay),
            "issues": [value.to_payload() for value in self.issues],
            "privacy": {
                "raw_participant_values_included": False,
                "participant_pseudonym_included": False,
                "deployment_details_included": False,
            },
            "operation_safety": _operation_safety(),
        }


@dataclass(frozen=True, slots=True)
class ReplayInspection:
    """A replay result that can also represent unavailable evidence safely."""

    session_root: Path
    bundle_id: str | None = None
    result_status: EngineStatus | None = None
    equivalent: bool | None = None
    requested_level: str | None = None
    evaluated_level: str | None = None
    compared_record_count: int = 0
    divergences: tuple[Mapping[str, Any], ...] = ()
    issues: tuple[SessionIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_root", Path(self.session_root).expanduser().resolve()
        )
        if self.result_status is not None:
            object.__setattr__(self, "result_status", EngineStatus(self.result_status))
        object.__setattr__(
            self,
            "divergences",
            tuple(freeze_json(value) for value in self.divergences),
        )

    @property
    def outcome(self) -> OperationOutcome:
        if self.result_status is None or self.equivalent is None:
            return OperationOutcome.UNAVAILABLE
        if self.equivalent and self.result_status == EngineStatus.COMPLETE:
            return OperationOutcome.COMPLETE
        if not self.equivalent:
            return OperationOutcome.DIVERGED
        return OperationOutcome.PARTIAL

    @property
    def first_divergence(self) -> Mapping[str, Any] | None:
        return self.divergences[0] if self.divergences else None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": REPLAY_INSPECTION_SCHEMA_ID,
            "status": self.outcome.value,
            "session_root": str(self.session_root),
            "bundle_id": self.bundle_id,
            "result_status": (
                None if self.result_status is None else self.result_status.value
            ),
            "equivalent": self.equivalent,
            "requested_level": self.requested_level,
            "evaluated_level": self.evaluated_level,
            "compared_record_count": self.compared_record_count,
            "first_divergence": (
                None
                if self.first_divergence is None
                else thaw_json(self.first_divergence)
            ),
            "divergences": [thaw_json(value) for value in self.divergences],
            "issues": [value.to_payload() for value in self.issues],
            "operation_safety": _operation_safety(),
        }


@dataclass(frozen=True, slots=True)
class ModelReplacementComparison:
    """Counterfactual replacement replay with localized comparison evidence."""

    session_root: Path
    bundle_id: str | None
    original_plan_hash: str | None
    replacement_plan_hash: str
    replaced_components: tuple[str, ...]
    result_status: EngineStatus | None = None
    equivalent: bool | None = None
    requested_level: str | None = None
    evaluated_level: str | None = None
    compared_record_count: int = 0
    divergences: tuple[Mapping[str, Any], ...] = ()
    issues: tuple[SessionIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_root", Path(self.session_root).expanduser().resolve()
        )
        if self.result_status is not None:
            object.__setattr__(self, "result_status", EngineStatus(self.result_status))
        object.__setattr__(
            self,
            "replaced_components",
            tuple(sorted(str(value) for value in self.replaced_components)),
        )
        object.__setattr__(
            self,
            "divergences",
            tuple(freeze_json(value) for value in self.divergences),
        )

    @property
    def outcome(self) -> OperationOutcome:
        if self.result_status is None or self.equivalent is None:
            return OperationOutcome.UNAVAILABLE
        if self.result_status != EngineStatus.COMPLETE:
            return OperationOutcome.PARTIAL
        if self.equivalent:
            return OperationOutcome.COMPLETE
        return OperationOutcome.DIVERGED

    @property
    def first_divergence(self) -> Mapping[str, Any] | None:
        return self.divergences[0] if self.divergences else None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": MODEL_REPLACEMENT_COMPARISON_SCHEMA_ID,
            "status": self.outcome.value,
            "counterfactual": True,
            "session_root": str(self.session_root),
            "bundle_id": self.bundle_id,
            "original_plan_hash": self.original_plan_hash,
            "replacement_plan_hash": self.replacement_plan_hash,
            "replaced_components": list(self.replaced_components),
            "result_status": (
                None if self.result_status is None else self.result_status.value
            ),
            "equivalent": self.equivalent,
            "requested_level": self.requested_level,
            "evaluated_level": self.evaluated_level,
            "compared_record_count": self.compared_record_count,
            "first_divergence": (
                None
                if self.first_divergence is None
                else thaw_json(self.first_divergence)
            ),
            "divergences": [thaw_json(value) for value in self.divergences],
            "issues": [value.to_payload() for value in self.issues],
            "operation_safety": _operation_safety(),
        }


@dataclass(frozen=True, slots=True)
class SessionExport:
    """Non-overwriting export publication result."""

    session_root: Path
    destination: Path
    bundle_id: str | None
    policy_hash: str
    safe_default: bool
    manifest: PortableExportManifest | None = None
    issues: tuple[SessionIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "session_root", Path(self.session_root).expanduser().resolve()
        )
        object.__setattr__(
            self, "destination", Path(self.destination).expanduser().resolve()
        )

    @property
    def published(self) -> bool:
        return self.manifest is not None

    @property
    def outcome(self) -> OperationOutcome:
        return (
            OperationOutcome.COMPLETE
            if self.published
            else OperationOutcome.NOT_EXPORTED
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": SESSION_EXPORT_SCHEMA_ID,
            "status": self.outcome.value,
            "session_root": str(self.session_root),
            "destination": str(self.destination),
            "bundle_id": self.bundle_id,
            "policy_hash": self.policy_hash,
            "safe_default": self.safe_default,
            "published": self.published,
            "source_preserved": True,
            "manifest": None if self.manifest is None else self.manifest.to_payload(),
            "issues": [value.to_payload() for value in self.issues],
            "privacy": {
                "safe_default_allows_only_public_artifacts": self.safe_default,
                "source_session_id_included": False if self.safe_default else None,
                "participant_pseudonym_included": False if self.safe_default else None,
                "external_references_included": False if self.safe_default else None,
            },
            "operation_safety": _operation_safety(),
        }


def safe_session_export_policy() -> ExportPolicy:
    """Return the conservative P7 safe-default portable export policy."""

    return ExportPolicy(
        policy_id="portable.session-safe-default",
        allowed_sensitivities=(Sensitivity.PUBLIC,),
        exclude_roles=(
            "archival_raw",
            "component_state",
            "deployment_binding",
            "evidence_log",
            "execution_capture",
            "execution_plan",
            "interrupted_evidence_log",
        ),
        include_source_session_id=False,
        include_participant_pseudonym=False,
        external_references=ExternalReferencePolicy.EXCLUDE,
    )


def inspect_session(root: str | Path) -> SessionInspection:
    """Inspect all published and unfinished evidence without changing the session.

    Missing, partial, and corrupt evidence is represented in the returned report.
    The service never performs recovery, truncation, finalization, deletion, or
    process control.
    """

    target = Path(root).expanduser().resolve()
    issues: list[SessionIssue] = []
    try:
        session = Session.open(target, read_only=True, allow_legacy=False)
    except Exception as exc:
        return _unavailable_inspection(target, exc)

    session_projection = _session_projection(session)
    interrupted = _interrupted_projection(session, issues)
    bundle_summaries: list[Mapping[str, Any]] = []
    records: list[tuple[str, EvidenceRecord]] = []
    for bundle_path in session.bundle_paths:
        try:
            reader = EvidenceReader.open(session, bundle_path)
            report = reader.verify()
            bundle_summaries.append(_bundle_projection(reader, report))
            for issue in report.issues:
                issues.append(
                    SessionIssue(
                        f"integrity.{issue.code.value}",
                        "integrity",
                        issue.message,
                        severity="warning" if issue.recoverable else "error",
                        bundle_id=reader.bundle.bundle_id,
                        recoverable=issue.recoverable,
                    )
                )
            if report.valid or report.recoverable:
                try:
                    records.extend(
                        (reader.bundle.bundle_id, record)
                        for record in reader.records(
                            allow_recoverable=report.recoverable
                        )
                    )
                except Exception as exc:
                    issues.append(
                        _issue_from_exception(
                            "evidence.records_unavailable",
                            "evidence",
                            exc,
                            bundle_id=reader.bundle.bundle_id,
                        )
                    )
        except Exception as exc:
            issues.append(
                _issue_from_exception(
                    "evidence.bundle_unavailable",
                    "integrity",
                    exc,
                )
            )
    if not session.bundle_paths:
        issues.append(
            SessionIssue(
                "session.no_published_bundles",
                "integrity",
                "Session has no published evidence bundle; unfinished writers remain untouched.",
                severity="warning",
            )
        )

    projections: dict[str, Any] = {}
    projection_builders: tuple[
        tuple[str, Callable[[list[tuple[str, EvidenceRecord]]], Any]], ...
    ] = (
        ("phase_timeline", _phase_timeline),
        ("source_health", _source_health),
        ("work", _work_projection),
        ("models", _model_projection),
        ("latency", _latency_projection),
        ("adaptation", _adaptation_projection),
        ("actions", _action_projection),
    )
    for name, builder in projection_builders:
        try:
            projections[name] = builder(records)
        except Exception as exc:  # pragma: no cover - independent fallback boundary
            projections[name] = () if name == "phase_timeline" else {}
            issues.append(
                _issue_from_exception(
                    f"projection.{name}_unavailable",
                    name,
                    exc,
                )
            )
    replay = _replay_projection(bundle_summaries)
    summary = {
        "session_id": session.session_id,
        "session_status": session.status.value,
        "published_bundle_count": len(bundle_summaries),
        "evidence_record_count": len(records),
        "unfinished_writer_count": len(interrupted),
        "unfinished_writers": interrupted,
        "attention_required": bool(interrupted)
        or any(not bool(value.get("valid")) for value in bundle_summaries),
    }
    return SessionInspection(
        session.root,
        session_projection,
        tuple(bundle_summaries),
        summary,
        phase_timeline=tuple(projections["phase_timeline"]),
        source_health=projections["source_health"],
        work=projections["work"],
        models=projections["models"],
        latency=projections["latency"],
        adaptation=projections["adaptation"],
        actions=projections["actions"],
        replay=replay,
        issues=tuple(issues),
    )


def replay_session(
    root: str | Path,
    *,
    bundle_id: str | None = None,
) -> ReplayInspection:
    """Replay a bundle, returning unavailable/diverged evidence instead of raising."""

    target = Path(root).expanduser().resolve()
    session, reader, issues = _select_reader(target, bundle_id)
    if session is None or reader is None:
        return ReplayInspection(target, bundle_id=bundle_id, issues=tuple(issues))
    try:
        integrity = reader.verify()
        if not integrity.valid:
            issues.extend(_integrity_issues(reader, integrity))
            return ReplayInspection(
                session.root,
                bundle_id=reader.bundle.bundle_id,
                issues=tuple(issues),
            )
        recorded = load_recorded_execution(reader)
        execution = BundleReplayRunner(_registry_for(recorded.plan)).run(reader)
        comparison = execution.equivalence
        return ReplayInspection(
            session.root,
            reader.bundle.bundle_id,
            execution.result.status,
            comparison.equivalent,
            comparison.requested_level.value,
            comparison.evaluated_level.value,
            comparison.compared_record_count,
            _divergence_projection(comparison.divergences),
            tuple(issues),
        )
    except Exception as exc:
        issues.append(
            _issue_from_exception(
                "replay.unavailable",
                "replay",
                exc,
                bundle_id=reader.bundle.bundle_id,
            )
        )
        return ReplayInspection(
            session.root,
            bundle_id=reader.bundle.bundle_id,
            issues=tuple(issues),
        )


def compare_session_models(
    root: str | Path,
    replacement_plan: ExecutionPlan,
    *,
    replaced_components: Iterable[str],
    bundle_id: str | None = None,
    registry: PluginRegistry | None = None,
    artifact_resolver: ArtifactResolver | None = None,
) -> ModelReplacementComparison:
    """Run one guarded model replacement without changing recorded evidence."""

    target = Path(root).expanduser().resolve()
    components = tuple(str(value) for value in replaced_components)
    replacement_hash = replacement_plan.plan_hash
    session, reader, issues = _select_reader(target, bundle_id)
    if session is None or reader is None:
        return ModelReplacementComparison(
            target,
            bundle_id,
            None,
            replacement_hash,
            components,
            issues=tuple(issues),
        )
    original_hash = reader.bundle.plan_hash
    try:
        integrity = reader.verify()
        if not integrity.valid:
            issues.extend(_integrity_issues(reader, integrity))
            return ModelReplacementComparison(
                session.root,
                reader.bundle.bundle_id,
                original_hash,
                replacement_hash,
                components,
                issues=tuple(issues),
            )
        selected_registry = registry or _registry_for(replacement_plan)
        comparison = BundleReplayRunner(
            selected_registry,
            artifact_resolver=artifact_resolver,
        ).run_with_model_replacements(
            reader,
            replacement_plan,
            replaced_components=components,
        )
        equivalence = comparison.execution.equivalence
        return ModelReplacementComparison(
            session.root,
            reader.bundle.bundle_id,
            comparison.original_plan_hash,
            comparison.replacement_plan_hash,
            comparison.replaced_components,
            comparison.execution.result.status,
            equivalence.equivalent,
            equivalence.requested_level.value,
            equivalence.evaluated_level.value,
            equivalence.compared_record_count,
            _divergence_projection(equivalence.divergences),
            tuple(issues),
        )
    except Exception as exc:
        issues.append(
            _issue_from_exception(
                "comparison.unavailable",
                "comparison",
                exc,
                bundle_id=reader.bundle.bundle_id,
            )
        )
        return ModelReplacementComparison(
            session.root,
            reader.bundle.bundle_id,
            original_hash,
            replacement_hash,
            components,
            issues=tuple(issues),
        )


def export_session(
    root: str | Path,
    destination: str | Path,
    *,
    bundle_id: str | None = None,
    policy: ExportPolicy | None = None,
) -> SessionExport:
    """Publish one non-overwriting portable export without mutating its source."""

    target = Path(root).expanduser().resolve()
    output = Path(destination).expanduser().resolve()
    selected = policy or safe_session_export_policy()
    session, reader, issues = _select_reader(target, bundle_id)
    if session is None or reader is None:
        return SessionExport(
            target,
            output,
            bundle_id,
            selected.policy_hash,
            policy is None,
            issues=tuple(issues),
        )
    try:
        integrity = reader.verify()
        if not integrity.valid:
            issues.extend(_integrity_issues(reader, integrity))
            return SessionExport(
                session.root,
                output,
                reader.bundle.bundle_id,
                selected.policy_hash,
                policy is None,
                issues=tuple(issues),
            )
        manifest = export_evidence_bundle(reader, output, selected)
        return SessionExport(
            session.root,
            output,
            reader.bundle.bundle_id,
            selected.policy_hash,
            policy is None,
            manifest,
            tuple(issues),
        )
    except Exception as exc:
        issues.append(
            _issue_from_exception(
                "export.not_published",
                "export",
                exc,
                bundle_id=reader.bundle.bundle_id,
            )
        )
        return SessionExport(
            session.root,
            output,
            reader.bundle.bundle_id,
            selected.policy_hash,
            policy is None,
            issues=tuple(issues),
        )


def _unavailable_inspection(root: Path, error: Exception) -> SessionInspection:
    issue = _issue_from_exception(
        "session.unavailable",
        "session",
        error,
    )
    return SessionInspection(
        root,
        {},
        (),
        {
            "published_bundle_count": 0,
            "evidence_record_count": 0,
            "unfinished_writer_count": 0,
            "attention_required": True,
        },
        source_health=_source_health([]),
        work=_work_projection([]),
        models=_model_projection([]),
        latency=_latency_projection([]),
        adaptation=_adaptation_projection([]),
        actions=_action_projection([]),
        replay=_replay_projection([]),
        issues=(issue,),
    )


def _session_projection(session: Session) -> Mapping[str, Any]:
    manifest = session.manifest
    return {
        "schema": manifest.schema,
        "session_id": manifest.session_id,
        "created_at": manifest.created_at,
        "completed_at": manifest.completed_at,
        "status": manifest.status.value,
        "origin_schema": manifest.origin_schema,
        "manifest_hash": manifest.manifest_hash,
        "participant_pseudonym_present": manifest.participant_pseudonym is not None,
        "private_metadata_present": bool(manifest.metadata),
    }


def _interrupted_projection(
    session: Session,
    issues: list[SessionIssue],
) -> list[Mapping[str, Any]]:
    try:
        interrupted = discover_interrupted_runs(session)
    except Exception as exc:
        issues.append(
            _issue_from_exception(
                "session.unfinished_writer_inspection_failed",
                "integrity",
                exc,
            )
        )
        return []
    projected = []
    for value in interrupted:
        projected.append(
            {
                "bundle_id": value.bundle_id,
                "writer_phase": value.phase.value,
                "integrity_status": value.integrity.value,
                "valid_record_count": value.valid_record_count,
                "last_complete_sequence": value.last_complete_sequence,
                "resumable": value.resumable,
                "finalizable": value.finalizable,
                "automatic_action_taken": False,
            }
        )
        issues.append(
            SessionIssue(
                "session.unfinished_writer",
                "integrity",
                "An unfinished evidence writer was observed and left unchanged.",
                severity="warning",
                bundle_id=value.bundle_id,
                recoverable=value.resumable or value.finalizable,
            )
        )
    return projected


def _bundle_projection(reader: EvidenceReader, report: Any) -> Mapping[str, Any]:
    bundle = reader.bundle
    return {
        "bundle_id": bundle.bundle_id,
        "bundle_hash": bundle.bundle_hash,
        "plan_hash": bundle.plan_hash,
        "evidence_status": bundle.status.value,
        "integrity_status": report.status.value,
        "valid": report.valid,
        "recoverable": report.recoverable,
        "valid_record_count": report.valid_record_count,
        "last_complete_sequence": report.last_complete_sequence,
        "equivalence_ceiling": bundle.metadata.get("equivalence_ceiling"),
        "engine_status": bundle.metadata.get("engine_status"),
        "terminal_reason": bundle.metadata.get("terminal_reason"),
        "execution_capture_count": len(bundle.execution_captures),
        "raw_recording_count": len(bundle.raw_recordings),
        "artifact_count": len(bundle.artifacts),
        "component_state_count": len(bundle.component_states),
        "issues": [
            {
                "code": issue.code.value,
                "message": issue.message,
                "artifact_id": issue.artifact_id,
                "recoverable": issue.recoverable,
            }
            for issue in report.issues
        ],
    }


_PHASE_RECORD_TYPES = frozenset(
    {
        "phase_started",
        "phase_resumed",
        "phase_finished",
        "phase_retry",
        "phase_transition",
        "phase_timeout",
        "phase_failure",
        "phase_blocked",
        "plan_failure",
    }
)


def _phase_timeline(
    records: list[tuple[str, EvidenceRecord]],
) -> tuple[Mapping[str, Any], ...]:
    fields = (
        "phase_id",
        "source_phase",
        "target_phase",
        "condition",
        "status",
        "attempt",
        "completed_attempt",
        "next_attempt",
        "resume_policy",
        "timeout_seconds",
        "admitted_input_count",
        "emission_count",
        "work_count",
        "reason",
    )
    timeline = []
    for bundle_id, record in records:
        if record.record_type not in _PHASE_RECORD_TYPES:
            continue
        payload = record.payload
        entry = _record_header(bundle_id, record)
        entry.update(
            {field: thaw_json(payload[field]) for field in fields if field in payload}
        )
        entry["failure_recorded"] = bool(payload.get("failure"))
        timeline.append(entry)
    return tuple(timeline)


def _source_health(records: list[tuple[str, EvidenceRecord]]) -> Mapping[str, Any]:
    streams: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    reconnects: Counter[tuple[str, str]] = Counter()
    losses: Counter[tuple[str, str]] = Counter()
    for bundle_id, record in records:
        lowered = record.record_type.lower()
        if "reconnect" in lowered:
            reconnects[(bundle_id, str(record.payload.get("stream_id") or "unknown"))] += 1
        if "loss" in lowered or "gap" in lowered:
            losses[(bundle_id, str(record.payload.get("stream_id") or "unknown"))] += 1
        packet = _packet_projection_source(record)
        if packet is None:
            continue
        component_id, payload = packet
        stream_id = str(payload.get("stream_id") or "unknown")
        revision = int(payload.get("stream_revision") or 0)
        sequence = int(payload.get("sequence_start", payload.get("sequence", 0)))
        count = _packet_item_count(payload)
        key = (bundle_id, component_id, stream_id, revision)
        value = streams.setdefault(
            key,
            {
                "bundle_id": bundle_id,
                "component_id": component_id,
                "stream_id": stream_id,
                "stream_revision": revision,
                "packet_count": 0,
                "item_count": 0,
                "first_sequence": sequence,
                "last_sequence": sequence - 1,
                "estimated_missing_items": 0,
                "overlap_or_reorder_count": 0,
                "event_kind_counts": Counter(),
            },
        )
        expected = int(value["last_sequence"]) + 1
        if value["packet_count"] and sequence > expected:
            value["estimated_missing_items"] += sequence - expected
        elif value["packet_count"] and sequence < expected:
            value["overlap_or_reorder_count"] += 1
        value["packet_count"] += 1
        value["item_count"] += count
        value["last_sequence"] = max(int(value["last_sequence"]), sequence + count - 1)
        events = payload.get("events")
        if isinstance(events, (list, tuple)):
            kinds = value["event_kind_counts"]
            for event in events:
                if isinstance(event, Mapping) and event.get("kind") is not None:
                    kinds[str(event["kind"])] += 1
    rows = []
    for key in sorted(streams):
        value = streams[key]
        value["event_kind_counts"] = _counter_payload(value["event_kind_counts"])
        evidence_key = (key[0], key[2])
        value["reconnect_evidence_count"] = reconnects[evidence_key]
        value["loss_evidence_count"] = losses[evidence_key]
        value["status"] = (
            "degraded"
            if value["estimated_missing_items"]
            or value["overlap_or_reorder_count"]
            or value["loss_evidence_count"]
            else "observed"
        )
        rows.append(value)
    return {
        "observed": bool(rows),
        "stream_count": len(rows),
        "streams": rows,
    }


def _packet_projection_source(
    record: EvidenceRecord,
) -> tuple[str, Mapping[str, Any]] | None:
    payload = record.payload
    if record.record_type == "graph_emission":
        value = payload.get("value")
        if isinstance(value, Mapping) and any(
            field in value for field in ("stream_id", "sequence_start", "sequence")
        ):
            return str(payload.get("component_id") or "unknown"), value
    if record.record_type in {"input_admitted", "packet_produced"}:
        value = payload.get("packet", payload)
        if isinstance(value, Mapping):
            return str(payload.get("component_id") or "external"), value
    return None


def _packet_item_count(payload: Mapping[str, Any]) -> int:
    shape = payload.get("shape")
    if isinstance(shape, (list, tuple)) and shape:
        return max(1, int(shape[0]))
    events = payload.get("events")
    if isinstance(events, (list, tuple)):
        return max(1, len(events))
    return 1


def _work_projection(records: list[tuple[str, EvidenceRecord]]) -> Mapping[str, Any]:
    rows = []
    statuses: Counter[str] = Counter()
    components: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for bundle_id, record in records:
        if record.record_type != "work":
            continue
        payload = record.payload.get("work", record.payload)
        if not isinstance(payload, Mapping):
            continue
        status = str(payload.get("status") or "unknown")
        component = str(payload.get("component_id") or "unknown")
        reason = payload.get("reason_code")
        statuses[status] += 1
        components[component] += 1
        if reason is not None:
            reasons[str(reason)] += 1
        rows.append(
            {
                "bundle_id": bundle_id,
                "sequence": record.sequence,
                "work_id": payload.get("work_id"),
                "component_id": component,
                "stage": payload.get("stage"),
                "role": payload.get("role"),
                "status": status,
                "reason_code": reason,
                "input_count": len(payload.get("input_ids") or ()),
                "duration_seconds": _duration(
                    payload.get("started_time"), payload.get("completed_time")
                ),
            }
        )
    rejected = statuses["rejected"] + statuses["skipped"]
    return {
        "work_item_count": len(rows),
        "admitted_count": len(rows) - rejected,
        "rejected_or_skipped_count": rejected,
        "status_counts": _counter_payload(statuses),
        "component_counts": _counter_payload(components),
        "reason_counts": _counter_payload(reasons),
        "items": rows,
    }


def _model_projection(records: list[tuple[str, EvidenceRecord]]) -> Mapping[str, Any]:
    dispositions: Counter[str] = Counter()
    component_dispositions: dict[str, Counter[str]] = defaultdict(Counter)
    comparisons: Counter[str] = Counter()
    comparison_groups: Counter[str] = Counter()
    prediction_rows = []
    comparison_rows = []
    for bundle_id, record in records:
        payload = record.payload
        if record.record_type == "model_result_disposition":
            status = str(payload.get("status") or "unknown")
            component = str(payload.get("component_id") or "unknown")
            dispositions[status] += 1
            component_dispositions[component][status] += 1
        elif record.record_type == "model_comparison":
            status = str(payload.get("status") or "unknown")
            group = str(payload.get("group_id") or "unknown")
            comparisons[status] += 1
            comparison_groups[group] += 1
            comparison_rows.append(
                {
                    "bundle_id": bundle_id,
                    "sequence": record.sequence,
                    "comparison_id": payload.get("comparison_id"),
                    "group_id": group,
                    "status": status,
                    "member_components": list(payload.get("member_components") or ()),
                    "observed_member_count": len(payload.get("prediction_ids") or {}),
                    "missing_members": list(payload.get("missing_members") or ()),
                    "outputs_equal": payload.get("outputs_equal"),
                    "reason_code": payload.get("reason_code"),
                }
            )
        elif record.record_type == "graph_emission":
            value = payload.get("value")
            if not isinstance(value, Mapping) or value.get("schema") != "eegle.prediction.v2":
                continue
            prediction_rows.append(
                {
                    "bundle_id": bundle_id,
                    "sequence": record.sequence,
                    "prediction_id": value.get("prediction_id"),
                    "component_id": value.get("component_id"),
                    "model_id": value.get("model_id"),
                    "model_version": value.get("model_version"),
                    "role_id": value.get("role_id"),
                    "role_profile": value.get("role_profile"),
                    "comparison_group": value.get("comparison_group"),
                    "output_port": value.get("output_port"),
                    "result_digest": value.get("result_digest"),
                    "input_count": len(value.get("input_ids") or ()),
                    "admitted_input_count": len(
                        value.get("admitted_input_ids") or ()
                    ),
                    "abstained": bool(value.get("abstained", False)),
                    "inference_latency_seconds": _prediction_latency(value),
                    "availability_delay_seconds": _duration(
                        value.get("produced_time"), value.get("available_time")
                    ),
                }
            )
    denominator = sum(dispositions.values())
    coverage = None if not denominator else dispositions["emitted"] / denominator
    return {
        "prediction_count": len(prediction_rows),
        "result_disposition_counts": _counter_payload(dispositions),
        "component_result_dispositions": {
            key: _counter_payload(component_dispositions[key])
            for key in sorted(component_dispositions)
        },
        "emission_coverage": coverage,
        "predictions": prediction_rows,
        "comparison_count": len(comparison_rows),
        "comparison_status_counts": _counter_payload(comparisons),
        "comparison_group_counts": _counter_payload(comparison_groups),
        "comparisons": comparison_rows,
    }


def _latency_projection(records: list[tuple[str, EvidenceRecord]]) -> Mapping[str, Any]:
    work_by_component: dict[str, list[float]] = defaultdict(list)
    inference_by_component: dict[str, list[float]] = defaultdict(list)
    availability_by_component: dict[str, list[float]] = defaultdict(list)
    for _, record in records:
        payload = record.payload
        if record.record_type == "work":
            work = payload.get("work", payload)
            if isinstance(work, Mapping):
                duration = _duration(work.get("started_time"), work.get("completed_time"))
                if duration is not None:
                    work_by_component[str(work.get("component_id") or "unknown")].append(
                        duration
                    )
        elif record.record_type == "graph_emission":
            value = payload.get("value")
            if isinstance(value, Mapping) and value.get("schema") == "eegle.prediction.v2":
                component = str(value.get("component_id") or "unknown")
                inference = _prediction_latency(value)
                availability = _duration(
                    value.get("produced_time"), value.get("available_time")
                )
                if inference is not None:
                    inference_by_component[component].append(inference)
                if availability is not None:
                    availability_by_component[component].append(availability)
    return {
        "work_seconds_by_component": _metric_groups(work_by_component),
        "prediction_inference_seconds_by_component": _metric_groups(
            inference_by_component
        ),
        "prediction_availability_delay_seconds_by_component": _metric_groups(
            availability_by_component
        ),
    }


def _adaptation_projection(records: list[tuple[str, EvidenceRecord]]) -> Mapping[str, Any]:
    eligibility: Counter[str] = Counter()
    transitions: Counter[str] = Counter()
    eligibility_rows = []
    transition_rows = []
    for bundle_id, record in records:
        payload = record.payload
        if record.record_type == "adaptation_eligibility":
            status = str(payload.get("status") or "unknown")
            eligibility[status] += 1
            eligibility_rows.append(
                {
                    "bundle_id": bundle_id,
                    "sequence": record.sequence,
                    "adaptation_id": payload.get("adaptation_id"),
                    "model_component_id": payload.get("model_component_id"),
                    "status": status,
                    "reason_code": payload.get("reason_code"),
                }
            )
        elif record.record_type == "state_transition":
            transition = payload.get("transition", payload)
            if not isinstance(transition, Mapping):
                continue
            metadata = transition.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            kind = str(transition.get("transition_kind") or "")
            if "adapt" not in kind and "adaptation_id" not in metadata:
                continue
            status = str(transition.get("status") or "unknown")
            transitions[status] += 1
            transition_rows.append(
                {
                    "bundle_id": bundle_id,
                    "sequence": record.sequence,
                    "transition_id": transition.get("transition_id"),
                    "adaptation_id": metadata.get("adaptation_id"),
                    "component_id": transition.get("component_id"),
                    "transition_kind": kind,
                    "status": status,
                    "prior_state_hash": transition.get("prior_state_hash"),
                    "resulting_state_hash": transition.get("resulting_state_hash"),
                    "reason_recorded": bool(transition.get("reason")),
                }
            )
    return {
        "eligibility_count": len(eligibility_rows),
        "eligibility_status_counts": _counter_payload(eligibility),
        "eligibility": eligibility_rows,
        "transition_count": len(transition_rows),
        "transition_status_counts": _counter_payload(transitions),
        "transitions": transition_rows,
    }


def _action_projection(records: list[tuple[str, EvidenceRecord]]) -> Mapping[str, Any]:
    types = frozenset(
        {
            "action_request",
            "authorization_request",
            "authorization_decision",
            "authorized_command",
            "action_disposition",
            "action_cancellation",
            "action_receipt",
        }
    )
    counts: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    rows = []
    fields = (
        "request_id",
        "action_request_id",
        "authorization_request_id",
        "authorization_decision_id",
        "decision_id",
        "permission_id",
        "provider_id",
        "actuator_id",
        "capability",
        "command_id",
        "receipt_id",
        "status",
        "terminal",
    )
    for bundle_id, record in records:
        if record.record_type not in types:
            continue
        payload: Mapping[str, Any] = record.payload
        if record.record_type == "action_request" and isinstance(
            payload.get("request"), Mapping
        ):
            payload = payload["request"]
        elif record.record_type == "action_receipt" and isinstance(
            payload.get("receipt"), Mapping
        ):
            payload = payload["receipt"]
        counts[record.record_type] += 1
        if payload.get("status") is not None:
            statuses[str(payload["status"])] += 1
        row = _record_header(bundle_id, record)
        row.update({field: thaw_json(payload[field]) for field in fields if field in payload})
        row["reason_recorded"] = bool(payload.get("reason"))
        rows.append(row)
    return {
        "record_count": len(rows),
        "type_counts": _counter_payload(counts),
        "status_counts": _counter_payload(statuses),
        "records": rows,
    }


def _replay_projection(bundles: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    rows = [
        {
            "bundle_id": value.get("bundle_id"),
            "equivalence_ceiling": value.get("equivalence_ceiling"),
            "integrity_status": value.get("integrity_status"),
            "replay_ready": bool(value.get("valid"))
            and bool(value.get("execution_capture_count")),
        }
        for value in bundles
    ]
    levels = [str(value["equivalence_ceiling"]) for value in rows if value["equivalence_ceiling"]]
    rank = {
        EquivalenceLevel.BITWISE.value: 0,
        EquivalenceLevel.NUMERIC.value: 1,
        EquivalenceLevel.SEMANTIC.value: 2,
        EquivalenceLevel.TRACE.value: 3,
        EquivalenceLevel.NON_REPLAYABLE.value: 4,
    }
    weakest = max(levels, key=lambda value: rank.get(value, 5)) if levels else None
    return {
        "bundle_count": len(rows),
        "replay_ready_count": sum(bool(value["replay_ready"]) for value in rows),
        "weakest_equivalence_ceiling": weakest,
        "bundles": rows,
    }


def _select_reader(
    root: Path,
    bundle_id: str | None,
) -> tuple[Session | None, EvidenceReader | None, list[SessionIssue]]:
    issues: list[SessionIssue] = []
    try:
        session = Session.open(root, read_only=True, allow_legacy=False)
    except Exception as exc:
        issues.append(_issue_from_exception("session.unavailable", "session", exc))
        return None, None, issues
    if bundle_id is not None:
        selected: str | Path = bundle_id
    elif not session.bundle_paths:
        issues.append(
            SessionIssue(
                "session.no_published_bundles",
                "integrity",
                "Session has no published evidence bundle; no source data was changed.",
                severity="error",
            )
        )
        return session, None, issues
    else:
        selected = session.bundle_paths[-1]
        if len(session.bundle_paths) > 1:
            issues.append(
                SessionIssue(
                    "session.latest_bundle_selected",
                    "selection",
                    "No bundle was named; the most recently registered bundle was selected.",
                    severity="warning",
                )
            )
    try:
        reader = EvidenceReader.open(session, selected)
    except Exception as exc:
        issues.append(
            _issue_from_exception(
                "evidence.bundle_unavailable",
                "integrity",
                exc,
                bundle_id=bundle_id,
            )
        )
        return session, None, issues
    return session, reader, issues


def _registry_for(plan: ExecutionPlan) -> PluginRegistry:
    registry = PluginRegistry()
    registry.register_builtins()
    installed = {
        (value.plugin_id, value.version) for value in registry.descriptors()
    }
    required = {(value.plugin_id, value.version) for value in plan.plugins}
    if required - installed:
        registry.load_entry_points()
    return registry


def _integrity_issues(reader: EvidenceReader, report: Any) -> list[SessionIssue]:
    if not report.issues:
        return [
            SessionIssue(
                "integrity.invalid",
                "integrity",
                f"Bundle integrity is {report.status.value}; replay/export was not started.",
                severity="error",
                bundle_id=reader.bundle.bundle_id,
                recoverable=report.recoverable,
            )
        ]
    return [
        SessionIssue(
            f"integrity.{issue.code.value}",
            "integrity",
            issue.message,
            severity="warning" if issue.recoverable else "error",
            bundle_id=reader.bundle.bundle_id,
            recoverable=issue.recoverable,
        )
        for issue in report.issues
    ]


def _divergence_projection(values: Iterable[Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        {
            "record_index": value.record_index,
            "record_type": value.record_type,
            "path": value.path,
            "message": value.message,
            "reference": _bounded_difference_value(value.reference),
            "candidate": _bounded_difference_value(value.candidate),
        }
        for value in values
    )


def _bounded_difference_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str) and len(value) <= 120:
        return value
    try:
        return {
            "redacted": True,
            "digest": canonical_hash(value),
            "value_type": type(value).__name__,
        }
    except (TypeError, ValueError):
        return {"redacted": True, "value_type": type(value).__name__}


def _record_header(bundle_id: str, record: EvidenceRecord) -> dict[str, Any]:
    return {
        "bundle_id": bundle_id,
        "sequence": record.sequence,
        "record_type": record.record_type,
        "emitted_time": record.emitted_time.to_payload(),
    }


def _duration(start: Any, end: Any) -> float | None:
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        return None
    if start.get("clock_id") != end.get("clock_id"):
        return None
    try:
        value = float(end["seconds"]) - float(start["seconds"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _prediction_latency(value: Mapping[str, Any]) -> float | None:
    lineage = value.get("lineage")
    if not isinstance(lineage, Mapping):
        return None
    return _duration(lineage.get("latest_input_available_time"), value.get("produced_time"))


def _metric_groups(values: Mapping[str, list[float]]) -> Mapping[str, Any]:
    return {key: _metrics(values[key]) for key in sorted(values)}


def _metrics(values: list[float]) -> Mapping[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "minimum": None, "p50": None, "p95": None, "maximum": None}
    return {
        "count": len(ordered),
        "minimum": ordered[0],
        "p50": _quantile(ordered, 0.50),
        "p95": _quantile(ordered, 0.95),
        "maximum": ordered[-1],
    }


def _quantile(values: list[float], probability: float) -> float:
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return values[lower] + (values[upper] - values[lower]) * fraction


def _counter_payload(counter: Counter[str]) -> Mapping[str, int]:
    return {key: counter[key] for key in sorted(counter)}


def _issue_from_exception(
    code: str,
    section: str,
    error: Exception,
    *,
    bundle_id: str | None = None,
) -> SessionIssue:
    message = str(error).strip() or type(error).__name__
    return SessionIssue(
        code,
        section,
        message,
        severity="error",
        bundle_id=bundle_id,
    )


def _operation_safety() -> Mapping[str, bool]:
    return {
        "source_mutated": False,
        "recovery_attempted": False,
        "data_deleted": False,
        "external_process_signalled": False,
    }


__all__ = [
    "MODEL_REPLACEMENT_COMPARISON_SCHEMA_ID",
    "REPLAY_INSPECTION_SCHEMA_ID",
    "SESSION_EXPORT_SCHEMA_ID",
    "SESSION_INSPECTION_SCHEMA_ID",
    "ModelReplacementComparison",
    "OperationOutcome",
    "ReplayInspection",
    "SessionExport",
    "SessionInspection",
    "SessionIssue",
    "compare_session_models",
    "export_session",
    "inspect_session",
    "replay_session",
    "safe_session_export_policy",
]
