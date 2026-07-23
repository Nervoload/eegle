"""Durable state and discovery for interrupted evidence writers.

The state file is deliberately separate from the published evidence bundle.
It is mutable operational state while a writer is open; a bundle manifest is
immutable scientific evidence once finalization succeeds.
"""

from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash, canonical_json_bytes
from eegle.recording.artifacts import ArtifactReference
from eegle.recording.evidence import EvidenceStatus
from eegle.recording.framing import IntegrityIssue, IntegrityIssueCode, IntegrityStatus
from eegle.recording.ledgers import read_evidence_ledger
from eegle.streams.clocks import TimePoint

if TYPE_CHECKING:
    from eegle.recording.session import Session


EVIDENCE_WRITER_STATE_SCHEMA = "eegle.evidence_writer_state.v1"
WRITER_STATE_NAME = "writer-state.json"


class WriterPhase(str, Enum):
    OPEN = "open"
    FINALIZING = "finalizing"
    FINALIZED = "finalized"


@dataclass(frozen=True, slots=True)
class EvidenceWriterState:
    bundle_id: str
    session_id: str
    plan_hash: str
    phase: WriterPhase
    created_time: TimePoint
    ledger_uri: str
    resume_token_hash: str
    execution_captures: tuple[ArtifactReference, ...] = ()
    raw_recordings: tuple[ArtifactReference, ...] = ()
    artifacts: tuple[ArtifactReference, ...] = ()
    component_states: tuple[Mapping[str, Any], ...] = ()
    last_sequence: int | None = None
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]
    final_status: EvidenceStatus | None = None
    completed_time: TimePoint | None = None
    semantic_log: ArtifactReference | None = None
    schema: str = EVIDENCE_WRITER_STATE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != EVIDENCE_WRITER_STATE_SCHEMA:
            raise ValueError(f"unsupported evidence-writer state schema: {self.schema}")
        object.__setattr__(self, "bundle_id", _bundle_identifier(self.bundle_id))
        object.__setattr__(self, "session_id", require_identifier(self.session_id, "session_id"))
        object.__setattr__(self, "plan_hash", require_digest(self.plan_hash, "plan_hash"))
        object.__setattr__(self, "phase", WriterPhase(self.phase))
        object.__setattr__(
            self,
            "resume_token_hash",
            require_digest(self.resume_token_hash, "resume_token_hash"),
        )
        ledger = Path(str(self.ledger_uri))
        if ledger.is_absolute() or not ledger.parts or ".." in ledger.parts:
            raise ValueError("writer ledger_uri must be a safe relative path")
        object.__setattr__(self, "ledger_uri", ledger.as_posix())
        if self.last_sequence is not None:
            object.__setattr__(self, "last_sequence", int(self.last_sequence))
            if self.last_sequence < 0:
                raise ValueError("writer last_sequence cannot be negative")
        object.__setattr__(self, "metadata", freeze_json(self.metadata or {}))
        object.__setattr__(
            self,
            "component_states",
            tuple(freeze_json(dict(value)) for value in self.component_states),
        )
        if self.final_status is not None:
            object.__setattr__(self, "final_status", EvidenceStatus(self.final_status))
            if self.final_status == EvidenceStatus.OPEN:
                raise ValueError("writer final_status cannot be open")
        if self.phase == WriterPhase.OPEN:
            if self.final_status is not None or self.completed_time is not None:
                raise ValueError("open writer state cannot contain finalization intent")
            if self.semantic_log is not None:
                raise ValueError("open writer state cannot contain a published semantic log")
        else:
            if self.final_status is None or self.completed_time is None:
                raise ValueError("finalizing writer state requires status and completed_time")
            if self.completed_time.clock_id != self.created_time.clock_id:
                raise ValueError("writer created_time and completed_time must share a clock")
            if self.completed_time.seconds < self.created_time.seconds:
                raise ValueError("writer completed_time cannot precede created_time")
        if self.phase == WriterPhase.FINALIZED and self.semantic_log is None:
            raise ValueError("finalized writer state requires its semantic-log reference")

    @property
    def state_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "bundle_id": self.bundle_id,
            "session_id": self.session_id,
            "plan_hash": self.plan_hash,
            "phase": self.phase.value,
            "created_time": self.created_time.to_payload(),
            "ledger_uri": self.ledger_uri,
            "resume_token_hash": self.resume_token_hash,
            "execution_captures": [value.to_payload() for value in self.execution_captures],
            "raw_recordings": [value.to_payload() for value in self.raw_recordings],
            "artifacts": [value.to_payload() for value in self.artifacts],
            "component_states": [thaw_json(value) for value in self.component_states],
            "last_sequence": self.last_sequence,
            "metadata": thaw_json(self.metadata),
            "final_status": None if self.final_status is None else self.final_status.value,
            "completed_time": (
                None if self.completed_time is None else self.completed_time.to_payload()
            ),
            "semantic_log": None if self.semantic_log is None else self.semantic_log.to_payload(),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["state_hash"] = self.state_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EvidenceWriterState":
        final_status = payload.get("final_status")
        completed_time = payload.get("completed_time")
        semantic_log = payload.get("semantic_log")
        state = cls(
            schema=str(payload.get("schema", EVIDENCE_WRITER_STATE_SCHEMA)),
            bundle_id=str(payload["bundle_id"]),
            session_id=str(payload["session_id"]),
            plan_hash=str(payload["plan_hash"]),
            phase=WriterPhase(str(payload["phase"])),
            created_time=TimePoint.from_payload(payload["created_time"]),
            ledger_uri=str(payload["ledger_uri"]),
            resume_token_hash=str(payload["resume_token_hash"]),
            execution_captures=tuple(
                ArtifactReference.from_payload(value)
                for value in payload.get("execution_captures", ())
            ),
            raw_recordings=tuple(
                ArtifactReference.from_payload(value)
                for value in payload.get("raw_recordings", ())
            ),
            artifacts=tuple(
                ArtifactReference.from_payload(value) for value in payload.get("artifacts", ())
            ),
            component_states=tuple(
                dict(value) for value in payload.get("component_states", ())
            ),
            last_sequence=(
                None if payload.get("last_sequence") is None else int(payload["last_sequence"])
            ),
            metadata=dict(payload.get("metadata") or {}),
            final_status=(
                None if final_status is None else EvidenceStatus(str(final_status))
            ),
            completed_time=(
                None if completed_time is None else TimePoint.from_payload(completed_time)
            ),
            semantic_log=(
                None if semantic_log is None else ArtifactReference.from_payload(semantic_log)
            ),
        )
        if payload.get("state_hash") != state.state_hash:
            raise ValueError("evidence-writer state hash mismatch")
        return state

    def replacing(self, **changes: Any) -> "EvidenceWriterState":
        return replace(self, **changes)

    def authorize(self, resume_token: str) -> None:
        observed = recovery_token_hash(resume_token)
        if not hmac.compare_digest(observed, self.resume_token_hash):
            raise PermissionError("evidence writer recovery token is invalid")


@dataclass(frozen=True, slots=True)
class InterruptedRun:
    bundle_id: str
    phase: WriterPhase
    integrity: IntegrityStatus
    ledger_uri: str
    valid_record_count: int
    last_complete_sequence: int | None
    resumable: bool
    finalizable: bool
    issues: tuple[IntegrityIssue, ...] = ()


def recovery_token_hash(token: str) -> str:
    value = str(token)
    if not value:
        raise ValueError("recovery token cannot be empty")
    return canonical_hash({"evidence_writer_recovery_token": value})


def writer_state_path(session: "Session", bundle_id: str) -> Path:
    return session.root / "bundles" / _bundle_identifier(bundle_id) / WRITER_STATE_NAME


def read_writer_state(session: "Session", bundle_id: str) -> EvidenceWriterState:
    path = writer_state_path(session, bundle_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid evidence-writer state JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("evidence-writer state must be a JSON object")
    state = EvidenceWriterState.from_payload(payload)
    if state.session_id != session.session_id:
        raise ValueError("evidence-writer state belongs to a different session")
    return state


def write_writer_state(session: "Session", state: EvidenceWriterState) -> None:
    if session.read_only:
        raise PermissionError("cannot update writer state in a read-only session")
    if state.session_id != session.session_id:
        raise ValueError("evidence-writer state belongs to a different session")
    _atomic_write(
        writer_state_path(session, state.bundle_id),
        canonical_json_bytes(state.to_payload()),
    )


def discover_interrupted_runs(session: "Session") -> tuple[InterruptedRun, ...]:
    """Return non-published writers with ledger integrity and restart options."""

    bundles_root = session.root / "bundles"
    if not bundles_root.is_dir():
        return ()
    discovered: list[InterruptedRun] = []
    for path in sorted(bundles_root.glob(f"*/{WRITER_STATE_NAME}")):
        state = read_writer_state(session, path.parent.name)
        bundle_manifest = path.parent / "bundle.json"
        if state.phase == WriterPhase.FINALIZED and bundle_manifest.is_file():
            continue
        ledger_path = session.root / state.ledger_uri
        if ledger_path.is_file():
            ledger = read_evidence_ledger(ledger_path)
            integrity = ledger.integrity.status
            issues = ledger.integrity.issues
            last_sequence = ledger.records[-1].sequence if ledger.records else None
            count = len(ledger.records)
        else:
            issue = IntegrityIssue(
                code=IntegrityIssueCode.ARTIFACT_MISSING,
                message=f"open evidence ledger is missing: {state.ledger_uri}",
                recoverable=False,
            )
            integrity = IntegrityStatus.UNRECOVERABLE
            issues = (issue,)
            last_sequence = None
            count = 0
        if state.phase == WriterPhase.FINALIZED and not bundle_manifest.is_file():
            issues = (
                *issues,
                IntegrityIssue(
                    code=IntegrityIssueCode.MANIFEST_MISMATCH,
                    message="writer is finalized but bundle.json is missing",
                    recoverable=False,
                ),
            )
            integrity = IntegrityStatus.UNRECOVERABLE
        discovered.append(
            InterruptedRun(
                bundle_id=state.bundle_id,
                phase=state.phase,
                integrity=integrity,
                ledger_uri=state.ledger_uri,
                valid_record_count=count,
                last_complete_sequence=last_sequence,
                resumable=(
                    state.phase == WriterPhase.OPEN
                    and integrity in {IntegrityStatus.VALID, IntegrityStatus.RECOVERABLE}
                ),
                finalizable=(
                    state.phase == WriterPhase.FINALIZING
                    and integrity == IntegrityStatus.VALID
                ),
                issues=tuple(issues),
            )
        )
    return tuple(discovered)


def _bundle_identifier(value: str) -> str:
    identifier = require_identifier(value, "bundle_id")
    if "/" in identifier or identifier in {".", ".."}:
        raise ValueError("bundle_id must be one safe path component")
    return identifier


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
