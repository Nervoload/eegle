"""Versioned evidence-bundle lifecycle, writing, reading, and verification."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash, canonical_json_bytes
from eegle.recording.artifacts import (
    ArtifactLineage,
    ArtifactReference,
    ArtifactStore,
    Sensitivity,
)
from eegle.recording.evidence import EvidenceRecord, EvidenceStatus
from eegle.recording.publications import ArtifactPublication
from eegle.recording.external import (
    ExternalArtifactVerification,
    ExternalArtifactVerifier,
    ExternalVerificationStatus,
    reference_only,
)
from eegle.recording.framing import (
    IntegrityIssue,
    IntegrityIssueCode,
    IntegrityStatus,
    inspect_framed_payloads,
    recover_framed_prefix,
)
from eegle.recording.ledgers import EvidenceLedgerWriter, read_evidence_ledger
from eegle.recording.stores import (
    FRAMED_SAMPLE_MEDIA_TYPE,
    FramedSampleStore,
    SampleStorePurpose,
    read_framed_sample_store,
)
from eegle.recording.writer_state import (
    EvidenceWriterState,
    WriterPhase,
    read_writer_state,
    recovery_token_hash,
    write_writer_state,
)
from eegle.streams.channels import StreamSpec
from eegle.streams.clocks import TimePoint
from eegle.streams.packets import Packet

if TYPE_CHECKING:
    from eegle.recording.session import Session


EVIDENCE_BUNDLE_SCHEMA = "eegle.evidence_bundle.v1"
COMPONENT_STATE_SNAPSHOT_SCHEMA = "eegle.component_state_snapshot.v1"
EVIDENCE_LOG_MEDIA_TYPE = "application/vnd.eegle.evidence-framed+json"
INTERRUPTED_EVIDENCE_LOG_MEDIA_TYPE = (
    "application/vnd.eegle.interrupted-evidence-framed+json"
)


@dataclass(frozen=True, slots=True)
class ComponentStateSnapshot:
    snapshot_id: str
    component_id: str
    component_version: str
    sequence: int
    captured_time: TimePoint
    state_hash: str
    artifact: ArtifactReference
    schema: str = COMPONENT_STATE_SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != COMPONENT_STATE_SNAPSHOT_SCHEMA:
            raise ValueError(f"unsupported component-state schema: {self.schema}")
        object.__setattr__(self, "snapshot_id", require_identifier(self.snapshot_id, "snapshot_id"))
        object.__setattr__(self, "component_id", require_identifier(self.component_id, "component_id"))
        if not self.component_version.strip():
            raise ValueError("component_version cannot be empty")
        object.__setattr__(self, "sequence", int(self.sequence))
        if self.sequence < 0:
            raise ValueError("component-state sequence cannot be negative")
        object.__setattr__(self, "state_hash", require_digest(self.state_hash, "state_hash"))
        if self.artifact.digest != self.state_hash:
            raise ValueError("component-state artifact digest must equal the canonical state hash")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "snapshot_id": self.snapshot_id,
            "component_id": self.component_id,
            "component_version": self.component_version,
            "sequence": self.sequence,
            "captured_time": self.captured_time.to_payload(),
            "state_hash": self.state_hash,
            "artifact": self.artifact.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ComponentStateSnapshot":
        return cls(
            schema=str(payload.get("schema", COMPONENT_STATE_SNAPSHOT_SCHEMA)),
            snapshot_id=str(payload["snapshot_id"]),
            component_id=str(payload["component_id"]),
            component_version=str(payload["component_version"]),
            sequence=int(payload["sequence"]),
            captured_time=TimePoint.from_payload(payload["captured_time"]),
            state_hash=str(payload["state_hash"]),
            artifact=ArtifactReference.from_payload(payload["artifact"]),
        )


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    bundle_id: str
    session_id: str
    plan_hash: str
    status: EvidenceStatus
    created_time: TimePoint
    semantic_log: ArtifactReference
    execution_captures: tuple[ArtifactReference, ...] = ()
    raw_recordings: tuple[ArtifactReference, ...] = ()
    artifacts: tuple[ArtifactReference, ...] = ()
    component_states: tuple[ComponentStateSnapshot, ...] = ()
    completed_time: TimePoint | None = None
    last_sequence: int | None = None
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = EVIDENCE_BUNDLE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != EVIDENCE_BUNDLE_SCHEMA:
            raise ValueError(f"unsupported evidence bundle schema: {self.schema}")
        object.__setattr__(self, "bundle_id", _bundle_identifier(self.bundle_id))
        object.__setattr__(self, "session_id", require_identifier(self.session_id, "session_id"))
        object.__setattr__(self, "plan_hash", require_digest(self.plan_hash, "plan_hash"))
        object.__setattr__(self, "status", EvidenceStatus(self.status))
        if self.semantic_log.role != "evidence_log":
            raise ValueError("semantic_log must have the evidence_log role")
        if not self.semantic_log.embedded:
            raise ValueError("semantic_log must be embedded in the evidence session")
        if self.status == EvidenceStatus.OPEN:
            raise ValueError("a published evidence bundle cannot have open status")
        if self.completed_time is None:
            raise ValueError("a published evidence bundle requires completed_time")
        if self.completed_time.clock_id != self.created_time.clock_id:
            raise ValueError("bundle created_time and completed_time must share a clock")
        if self.completed_time.seconds < self.created_time.seconds:
            raise ValueError("bundle completed_time cannot precede created_time")
        if self.last_sequence is not None and int(self.last_sequence) < 0:
            raise ValueError("last_sequence cannot be negative")
        object.__setattr__(self, "metadata", freeze_json(self.metadata or {}))

    @property
    def bundle_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "bundle_id": self.bundle_id,
            "session_id": self.session_id,
            "plan_hash": self.plan_hash,
            "status": self.status.value,
            "created_time": self.created_time.to_payload(),
            "completed_time": None
            if self.completed_time is None
            else self.completed_time.to_payload(),
            "last_sequence": self.last_sequence,
            "semantic_log": self.semantic_log.to_payload(),
            "execution_captures": [ref.to_payload() for ref in self.execution_captures],
            "raw_recordings": [ref.to_payload() for ref in self.raw_recordings],
            "artifacts": [ref.to_payload() for ref in self.artifacts],
            "component_states": [state.to_payload() for state in self.component_states],
            "metadata": thaw_json(self.metadata),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["bundle_hash"] = self.bundle_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EvidenceBundle":
        completed = payload.get("completed_time")
        bundle = cls(
            schema=str(payload.get("schema", EVIDENCE_BUNDLE_SCHEMA)),
            bundle_id=str(payload["bundle_id"]),
            session_id=str(payload["session_id"]),
            plan_hash=str(payload["plan_hash"]),
            status=EvidenceStatus(str(payload["status"])),
            created_time=TimePoint.from_payload(payload["created_time"]),
            completed_time=None if completed is None else TimePoint.from_payload(completed),
            last_sequence=None
            if payload.get("last_sequence") is None
            else int(payload["last_sequence"]),
            semantic_log=ArtifactReference.from_payload(payload["semantic_log"]),
            execution_captures=tuple(
                ArtifactReference.from_payload(item)
                for item in payload.get("execution_captures", ())
            ),
            raw_recordings=tuple(
                ArtifactReference.from_payload(item) for item in payload.get("raw_recordings", ())
            ),
            artifacts=tuple(
                ArtifactReference.from_payload(item) for item in payload.get("artifacts", ())
            ),
            component_states=tuple(
                ComponentStateSnapshot.from_payload(item)
                for item in payload.get("component_states", ())
            ),
            metadata=dict(payload.get("metadata") or {}),
        )
        if payload.get("bundle_hash") != bundle.bundle_hash:
            raise ValueError("evidence bundle hash mismatch")
        return bundle

    @property
    def references(self) -> tuple[ArtifactReference, ...]:
        return (
            self.semantic_log,
            *self.execution_captures,
            *self.raw_recordings,
            *self.artifacts,
            *(state.artifact for state in self.component_states),
        )


@dataclass(frozen=True, slots=True)
class BundleIntegrityReport:
    status: IntegrityStatus
    bundle_id: str
    valid_record_count: int
    last_complete_sequence: int | None
    issues: tuple[IntegrityIssue, ...] = ()
    external_artifacts: tuple[ExternalArtifactVerification, ...] = ()

    @property
    def valid(self) -> bool:
        return self.status == IntegrityStatus.VALID

    @property
    def recoverable(self) -> bool:
        return self.status == IntegrityStatus.RECOVERABLE


class EvidenceWriter:
    """Assemble semantic evidence and independent sample references into a bundle."""

    def __init__(
        self,
        session: "Session",
        *,
        bundle_id: str,
        plan_hash: str,
        created_time: TimePoint,
        durable: bool = False,
        metadata: Mapping[str, Any] | None = None,
        resume_token: str | None = None,
    ) -> None:
        if session.read_only:
            raise PermissionError("cannot write evidence to a read-only session")
        if session.status.value != "open":
            raise RuntimeError("cannot write evidence to a finalized session")
        self.session = session
        self.bundle_id = _bundle_identifier(bundle_id)
        self.plan_hash = require_digest(plan_hash, "plan_hash")
        self.created_time = created_time
        self.metadata = freeze_json(metadata or {})
        self._namespace = f"bundles/{self.bundle_id}"
        self._bundle_dir = session.root / "bundles" / self.bundle_id
        if self._bundle_dir.exists():
            raise FileExistsError(f"evidence bundle already exists: {self._bundle_dir}")
        self._bundle_dir.mkdir(parents=True)
        self._ledger_path = self._bundle_dir / "semantic.eegle"
        self._ledger = EvidenceLedgerWriter(self._ledger_path, durable=durable)
        self._execution: list[ArtifactReference] = []
        self._raw: list[ArtifactReference] = []
        self._artifacts: list[ArtifactReference] = []
        self._states: list[ComponentStateSnapshot] = []
        self._last_sequence: int | None = None
        self._closed = False
        self._resume_token = resume_token or secrets.token_urlsafe(32)
        self._state = EvidenceWriterState(
            bundle_id=self.bundle_id,
            session_id=session.session_id,
            plan_hash=self.plan_hash,
            phase=WriterPhase.OPEN,
            created_time=self.created_time,
            ledger_uri=self._ledger_path.relative_to(session.root).as_posix(),
            resume_token_hash=recovery_token_hash(self._resume_token),
            metadata=thaw_json(self.metadata),
        )
        write_writer_state(self.session, self._state)

    @property
    def resume_token(self) -> str:
        """Return the secret needed to resume or finish this writer.

        The token is never stored in the session. Operators that require
        restart recovery must keep it in an appropriate deployment secret
        store for the lifetime of the open run.
        """

        return self._resume_token

    @classmethod
    def resume(
        cls,
        session: "Session",
        *,
        bundle_id: str,
        resume_token: str,
        durable: bool = False,
    ) -> "EvidenceWriter":
        """Resume an unpublished writer from a proven ledger boundary.

        A recoverable partial final frame is copied to a new ledger before
        append resumes. The interrupted source is never truncated or changed.
        """

        if session.read_only or session.status.value != "open":
            raise PermissionError("cannot resume evidence in a read-only or finalized session")
        state = read_writer_state(session, bundle_id)
        state.authorize(resume_token)
        if state.phase != WriterPhase.OPEN:
            raise RuntimeError(
                f"evidence writer is {state.phase.value}; only open writers can resume"
            )
        ledger_path = session.root / state.ledger_uri
        if not ledger_path.is_file():
            raise FileNotFoundError(f"open evidence ledger is missing: {state.ledger_uri}")
        result = read_evidence_ledger(ledger_path)
        if result.integrity.status == IntegrityStatus.UNRECOVERABLE:
            detail = result.integrity.issues[0].message if result.integrity.issues else "unknown"
            raise ValueError(f"cannot resume an unrecoverable evidence ledger: {detail}")
        if result.integrity.status == IntegrityStatus.RECOVERABLE:
            recovery_index = 1 + sum(
                reference.role == "interrupted_evidence_log"
                for reference in state.artifacts
            )
            interrupted_reference = session.artifacts.register_file(
                f"bundles/{state.bundle_id}",
                f"{state.bundle_id}.interrupted-semantic-{recovery_index}",
                "interrupted_evidence_log",
                ledger_path,
                INTERRUPTED_EVIDENCE_LOG_MEDIA_TYPE,
                sensitivity=Sensitivity.PSEUDONYMIZED,
                lineage=ArtifactLineage(
                    component_id="eegle.evidence-recovery",
                    component_version="1",
                    metadata={
                        "source_uri": state.ledger_uri,
                        "integrity_status": result.integrity.status.value,
                        "last_complete_offset": result.integrity.last_complete_offset,
                        "issues": [issue.code.value for issue in result.integrity.issues],
                    },
                ),
                copy=False,
            )
            if interrupted_reference not in state.artifacts:
                state = state.replacing(
                    artifacts=(*state.artifacts, interrupted_reference)
                )
            recovered_path = _recovery_path(ledger_path, recovery_index)
            if recovered_path.exists():
                existing = inspect_framed_payloads(recovered_path)
                if (
                    existing.status != IntegrityStatus.VALID
                    or existing.file_size != result.integrity.last_complete_offset
                ):
                    raise ValueError(
                        f"existing recovery ledger cannot be proven equivalent: "
                        f"{recovered_path}"
                    )
            else:
                recover_framed_prefix(ledger_path, recovered_path)
            ledger_path = recovered_path
            result = read_evidence_ledger(ledger_path)
        authoritative_last = result.records[-1].sequence if result.records else None

        self = cls.__new__(cls)
        self.session = session
        self.bundle_id = state.bundle_id
        self.plan_hash = state.plan_hash
        self.created_time = state.created_time
        self.metadata = freeze_json(thaw_json(state.metadata))
        self._namespace = f"bundles/{state.bundle_id}"
        self._bundle_dir = session.root / "bundles" / state.bundle_id
        self._ledger_path = ledger_path
        self._ledger = EvidenceLedgerWriter(ledger_path, durable=durable)
        self._execution = list(state.execution_captures)
        self._raw = list(state.raw_recordings)
        self._artifacts = list(state.artifacts)
        self._states = [
            ComponentStateSnapshot.from_payload(payload) for payload in state.component_states
        ]
        self._last_sequence = authoritative_last
        self._closed = False
        self._resume_token = resume_token
        self._state = state.replacing(
            ledger_uri=ledger_path.relative_to(session.root).as_posix(),
            last_sequence=authoritative_last,
        )
        self._assert_registered_references()
        write_writer_state(session, self._state)
        return self

    def append(self, record: EvidenceRecord) -> None:
        self._require_open()
        self._ledger.append(record)
        self._last_sequence = record.sequence
        self._sync_state()

    def add_execution_capture(self, reference: ArtifactReference) -> None:
        self._require_open()
        self._add_unique(self._execution, reference, expected_role="execution_capture")
        self._sync_state()

    def add_raw_recording(self, reference: ArtifactReference) -> None:
        self._require_open()
        self._add_unique(self._raw, reference, expected_role="archival_raw")
        self._sync_state()

    def add_artifact(self, reference: ArtifactReference) -> None:
        self._require_open()
        self._add_unique(self._artifacts, reference)
        self._sync_state()

    def add_execution_plan(self, plan: Any) -> ArtifactReference:
        """Attach the immutable plan whose hash authorizes this execution."""

        if getattr(plan, "plan_hash", None) != self.plan_hash:
            raise ValueError("execution plan hash does not match evidence writer")
        to_payload = getattr(plan, "to_payload", None)
        if not callable(to_payload):
            raise TypeError("execution plan must implement to_payload()")
        reference = self.session.artifacts.register_json(
            self._namespace,
            f"{self.bundle_id}.execution-plan",
            "execution_plan",
            to_payload(),
            sensitivity=Sensitivity.INTERNAL,
        )
        self.add_artifact(reference)
        return reference

    def capture_packets(
        self,
        streams: Iterable[StreamSpec],
        packets: Iterable[Packet],
        *,
        store_id: str | None = None,
    ) -> ArtifactReference:
        self._require_open()
        identifier = store_id or f"{self.bundle_id}.execution-capture"
        sample_store = FramedSampleStore(
            self.session.artifacts,
            store_id=identifier,
            purpose=SampleStorePurpose.EXECUTION_CAPTURE,
            namespace=self._namespace,
            relative_uri=f"bundles/{self.bundle_id}/execution-capture.eegle",
        )
        for stream in streams:
            sample_store.open_stream(stream)
        sample_store.append_all(packets)
        reference = sample_store.close()
        self.add_execution_capture(reference)
        return reference

    def snapshot_component(
        self,
        *,
        component_id: str,
        component_version: str,
        sequence: int,
        captured_time: TimePoint,
        state: Mapping[str, Any],
    ) -> ComponentStateSnapshot:
        self._require_open()
        snapshot_id = f"{self.bundle_id}.state.{component_id}.{sequence}"
        state_payload = thaw_json(freeze_json(state))
        reference = self.session.artifacts.register_json(
            self._namespace,
            snapshot_id,
            "component_state",
            state_payload,
            sensitivity=Sensitivity.INTERNAL,
            lineage=ArtifactLineage(
                component_id=component_id,
                component_version=component_version,
                metadata={"evidence_sequence": int(sequence)},
            ),
        )
        snapshot = ComponentStateSnapshot(
            snapshot_id=snapshot_id,
            component_id=component_id,
            component_version=component_version,
            sequence=sequence,
            captured_time=captured_time,
            state_hash=canonical_hash(state_payload),
            artifact=reference,
        )
        self._states.append(snapshot)
        self._sync_state()
        return snapshot

    def close_unfinalized(self) -> None:
        """Close local handles while leaving the run explicitly discoverable."""

        self._require_open()
        self._ledger.close()
        self._closed = True

    def write_engine_result(
        self,
        result: Any,
        *,
        streams: Iterable[StreamSpec],
    ) -> None:
        """Persist any semantic engine result exposing the typed evidence contract."""

        if result.plan_hash != self.plan_hash:
            raise ValueError("engine result plan hash does not match evidence writer")
        for record in result.evidence:
            self.append(record)
        packets = tuple(result.captured_packets)
        if packets:
            self.capture_packets(streams, packets)
        component_versions: dict[str, str] = {}
        for record in result.evidence:
            if record.record_type == "component_started":
                component_versions[str(record.payload["component_id"])] = str(
                    record.payload["component_version"]
                )
            elif record.record_type == "component_state":
                component_id = str(record.payload["component_id"])
                self.snapshot_component(
                    component_id=component_id,
                    component_version=component_versions.get(component_id, "unknown"),
                    sequence=record.sequence,
                    captured_time=record.emitted_time,
                    state=dict(record.payload["state"]),
                )

    def finalize(
        self,
        *,
        status: EvidenceStatus,
        completed_time: TimePoint,
    ) -> EvidenceBundle:
        self._require_open()
        normalized_status = EvidenceStatus(status)
        if normalized_status == EvidenceStatus.OPEN:
            raise ValueError("finalized evidence status cannot be open")
        self._ledger.close()
        self._closed = True
        self._state = self._state.replacing(
            phase=WriterPhase.FINALIZING,
            ledger_uri=self._ledger_path.relative_to(self.session.root).as_posix(),
            execution_captures=tuple(self._execution),
            raw_recordings=tuple(self._raw),
            artifacts=tuple(self._artifacts),
            component_states=tuple(state.to_payload() for state in self._states),
            last_sequence=self._last_sequence,
            final_status=normalized_status,
            completed_time=completed_time,
        )
        write_writer_state(self.session, self._state)
        bundle, self._state = _publish_finalizing_state(self.session, self._state)
        return bundle

    def _add_unique(
        self,
        target: list[ArtifactReference],
        reference: ArtifactReference,
        *,
        expected_role: str | None = None,
    ) -> None:
        if not any(entry.reference == reference for entry in self.session.artifacts.entries):
            raise ValueError(
                f"artifact must be registered in the session store before bundling: "
                f"{reference.artifact_id}"
            )
        if expected_role is not None and reference.role != expected_role:
            raise ValueError(
                f"artifact {reference.artifact_id} must have role {expected_role}, "
                f"observed {reference.role}"
            )
        known = {
            ref
            for ref in (*self._execution, *self._raw, *self._artifacts)
        }
        known.update(state.artifact for state in self._states)
        if reference in known:
            raise ValueError(f"artifact is already part of this bundle: {reference.artifact_id}")
        target.append(reference)

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("evidence writer is closed")

    def _sync_state(self) -> None:
        self._state = self._state.replacing(
            execution_captures=tuple(self._execution),
            raw_recordings=tuple(self._raw),
            artifacts=tuple(self._artifacts),
            component_states=tuple(state.to_payload() for state in self._states),
            last_sequence=self._last_sequence,
        )
        write_writer_state(self.session, self._state)

    def _assert_registered_references(self) -> None:
        registered = {entry.reference for entry in self.session.artifacts.entries}
        references = {
            *self._execution,
            *self._raw,
            *self._artifacts,
            *(state.artifact for state in self._states),
        }
        missing = sorted(ref.artifact_id for ref in references if ref not in registered)
        if missing:
            raise ValueError(
                "writer state references artifacts absent from the session registry: "
                + ", ".join(missing)
            )


class EvidenceReader:
    def __init__(self, session: "Session", bundle: EvidenceBundle, manifest_path: Path) -> None:
        self.session = session
        self.bundle = bundle
        self.manifest_path = manifest_path

    @classmethod
    def open(
        cls,
        session: "Session",
        bundle: str | Path,
    ) -> "EvidenceReader":
        candidate = Path(bundle)
        if not candidate.is_absolute():
            if candidate.suffix == ".json" or "/" in candidate.as_posix():
                candidate = (session.root / candidate).resolve()
                try:
                    candidate.relative_to(session.root)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("relative evidence bundle path escapes the session") from exc
            else:
                identifier = _bundle_identifier(str(bundle))
                candidate = session.root / "bundles" / identifier / "bundle.json"
        payload = _read_json(candidate)
        parsed = EvidenceBundle.from_payload(payload)
        if parsed.session_id != session.session_id:
            raise ValueError("evidence bundle belongs to a different session")
        return cls(session, parsed, candidate)

    def records(self, *, allow_recoverable: bool = False) -> tuple[EvidenceRecord, ...]:
        path = self.session.artifacts.resolve(self.bundle.semantic_log)
        result = read_evidence_ledger(path)
        if result.integrity.status != IntegrityStatus.VALID and not (
            allow_recoverable and result.integrity.status == IntegrityStatus.RECOVERABLE
        ):
            raise ValueError(
                f"evidence ledger integrity is {result.integrity.status.value}: "
                f"{result.integrity.issues[0].message if result.integrity.issues else 'unknown issue'}"
            )
        return result.records

    def verify(
        self,
        external_verifier: ExternalArtifactVerifier | None = None,
    ) -> BundleIntegrityReport:
        issues: list[IntegrityIssue] = []
        external_artifacts: list[ExternalArtifactVerification] = []
        registered = {entry.reference for entry in self.session.artifacts.entries}
        for reference in self.bundle.references:
            if reference not in registered:
                issues.append(
                    IntegrityIssue(
                        code=IntegrityIssueCode.MANIFEST_MISMATCH,
                        message=(
                            f"bundle artifact is absent from the session registry: "
                            f"{reference.artifact_id}"
                        ),
                        artifact_id=reference.artifact_id,
                    )
                )
        log_path = self.session.artifacts.resolve(self.bundle.semantic_log)
        if not log_path.is_file():
            issues.append(
                IntegrityIssue(
                    code=IntegrityIssueCode.ARTIFACT_MISSING,
                    message=f"artifact missing: {self.bundle.semantic_log.artifact_id}",
                    artifact_id=self.bundle.semantic_log.artifact_id,
                )
            )
            records = ()
        else:
            ledger = read_evidence_ledger(log_path)
            records = ledger.records
            issues.extend(
                _with_artifact(issue, self.bundle.semantic_log.artifact_id)
                for issue in ledger.integrity.issues
            )
            _verify_reference(
                self.session.artifacts,
                self.bundle.semantic_log,
                issues,
                suppress_digest=ledger.integrity.status == IntegrityStatus.RECOVERABLE,
            )

        for reference in self.bundle.execution_captures:
            if not reference.embedded:
                continue
            path = self.session.artifacts.resolve(reference)
            if not path.is_file():
                issues.append(
                    IntegrityIssue(
                        code=IntegrityIssueCode.ARTIFACT_MISSING,
                        message=f"artifact missing: {reference.artifact_id}",
                        artifact_id=reference.artifact_id,
                    )
                )
                continue
            if reference.media_type == FRAMED_SAMPLE_MEDIA_TYPE:
                framing = inspect_framed_payloads(path)
                issues.extend(
                    _with_artifact(issue, reference.artifact_id) for issue in framing.issues
                )
                if framing.status != IntegrityStatus.RECOVERABLE:
                    _verify_reference(self.session.artifacts, reference, issues)
                # For a recoverable final frame, the declared size/digest
                # describe the pre-interruption object; framing supplies the
                # actionable proven boundary.
                try:
                    read_framed_sample_store(path)
                except ValueError as exc:
                    issues.append(
                        IntegrityIssue(
                            code=IntegrityIssueCode.MANIFEST_MISMATCH,
                            message=str(exc),
                            artifact_id=reference.artifact_id,
                        )
                    )
            else:
                _verify_reference(self.session.artifacts, reference, issues)

        for reference in (
            *self.bundle.raw_recordings,
            *self.bundle.artifacts,
            *(state.artifact for state in self.bundle.component_states),
        ):
            _verify_reference(self.session.artifacts, reference, issues)

        seen_external: set[tuple[str, str]] = set()
        for reference in self.bundle.references:
            if reference.embedded or (reference.artifact_id, reference.digest) in seen_external:
                continue
            seen_external.add((reference.artifact_id, reference.digest))
            verification = (
                reference_only(reference)
                if external_verifier is None
                else external_verifier.verify(reference)
            )
            if (
                verification.artifact_id != reference.artifact_id
                or verification.expected_digest != reference.digest
                or verification.expected_size_bytes != reference.size_bytes
            ):
                raise ValueError(
                    f"external verifier returned a result for the wrong reference: "
                    f"{reference.artifact_id}"
                )
            external_artifacts.append(verification)
            if verification.status == ExternalVerificationStatus.MISMATCH:
                code = (
                    IntegrityIssueCode.ARTIFACT_SIZE_MISMATCH
                    if verification.observed_size_bytes != verification.expected_size_bytes
                    else IntegrityIssueCode.ARTIFACT_DIGEST_MISMATCH
                )
                issues.append(
                    IntegrityIssue(
                        code=code,
                        message=verification.message or "external artifact mismatch",
                        artifact_id=verification.artifact_id,
                        expected=(
                            verification.expected_size_bytes
                            if code == IntegrityIssueCode.ARTIFACT_SIZE_MISMATCH
                            else verification.expected_digest
                        ),
                        observed=(
                            verification.observed_size_bytes
                            if code == IntegrityIssueCode.ARTIFACT_SIZE_MISMATCH
                            else verification.observed_digest
                        ),
                    )
                )

        if self.bundle.last_sequence is None:
            observed_last = None
        else:
            observed_last = records[-1].sequence if records else None
            if observed_last != self.bundle.last_sequence and not any(
                issue.recoverable for issue in issues
            ):
                issues.append(
                    IntegrityIssue(
                        code=IntegrityIssueCode.MANIFEST_MISMATCH,
                        message=(
                            f"bundle last_sequence mismatch: expected {self.bundle.last_sequence}, "
                            f"observed {observed_last}"
                        ),
                        expected=self.bundle.last_sequence,
                        observed=observed_last,
                    )
                )
        status = IntegrityStatus.VALID
        if any(not issue.recoverable for issue in issues):
            status = IntegrityStatus.UNRECOVERABLE
        elif issues:
            status = IntegrityStatus.RECOVERABLE
        return BundleIntegrityReport(
            status=status,
            bundle_id=self.bundle.bundle_id,
            valid_record_count=len(records),
            last_complete_sequence=records[-1].sequence if records else None,
            issues=tuple(issues),
            external_artifacts=tuple(external_artifacts),
        )


def persist_engine_run(
    session: "Session",
    result: Any,
    *,
    plan: Any,
    streams: Iterable[StreamSpec],
    bundle_id: str | None = None,
) -> EvidenceBundle:
    """Create a complete/partial/failed bundle from one semantic engine result."""

    records = tuple(result.evidence)
    if not records:
        raise ValueError("an engine result without evidence cannot form an evidence bundle")
    if getattr(plan, "plan_hash", None) != result.plan_hash:
        raise ValueError("execution plan hash does not match engine result")
    if not callable(getattr(plan, "to_payload", None)):
        raise TypeError("execution plan must implement to_payload()")
    identifier = bundle_id or f"bundle.{result.execution_id}"
    equivalence = getattr(getattr(result, "equivalence_ceiling", None), "value", None)
    writer = EvidenceWriter(
        session,
        bundle_id=identifier,
        plan_hash=result.plan_hash,
        created_time=records[0].emitted_time,
        metadata={
            "execution_id": result.execution_id,
            "engine_status": result.status.value,
            "equivalence_ceiling": equivalence,
            "failure": getattr(result, "failure", None),
        },
    )
    writer.add_execution_plan(plan)
    namespace = f"bundles/{identifier}"
    for value in getattr(result, "artifacts", ()):
        if isinstance(value, ArtifactPublication):
            lineage = ArtifactLineage(
                component_id=value.producer_component_id,
                component_version=value.producer_component_version,
                metadata={
                    "publication_id": value.publication_id,
                    "input_ids": list(value.input_ids),
                    **thaw_json(value.metadata),
                },
            )
            if value.materialized_payload is not None:
                reference = session.artifacts.register_json(
                    namespace,
                    value.artifact_id,
                    value.reference.role,
                    thaw_json(value.materialized_payload),
                    sensitivity=value.reference.sensitivity,
                    lineage=lineage,
                )
                if reference.digest != value.reference.digest:
                    raise ValueError(
                        f"materialized artifact {value.artifact_id} changed digest during persistence"
                    )
            elif value.reference.embedded:
                if not any(
                    entry.reference == value.reference
                    for entry in session.artifacts.entries
                ):
                    raise ValueError(
                        f"embedded artifact {value.artifact_id} is not registered in the session"
                    )
                reference = value.reference
            else:
                reference = session.artifacts.register_external(
                    namespace,
                    value.artifact_id,
                    value.reference.role,
                    value.reference.uri,
                    value.reference.digest,
                    value.reference.media_type,
                    value.reference.size_bytes,
                    sensitivity=value.reference.sensitivity,
                    lineage=lineage,
                )
        elif isinstance(value, ArtifactReference) and value.embedded:
            if not any(entry.reference == value for entry in session.artifacts.entries):
                raise ValueError(
                    f"embedded artifact {value.artifact_id} is not registered in the session"
                )
            reference = value
        elif isinstance(value, ArtifactReference):
            reference = session.artifacts.register_external(
                namespace,
                value.artifact_id,
                value.role,
                value.uri,
                value.digest,
                value.media_type,
                value.size_bytes,
                sensitivity=value.sensitivity,
                lineage=ArtifactLineage(
                    component_id="eegle.external-artifact",
                    component_version="1",
                    metadata={"source_embedded": value.embedded},
                ),
            )
        else:
            raise TypeError(
                "engine result artifacts must be ArtifactReference or ArtifactPublication"
            )
        writer.add_artifact(reference)
    writer.write_engine_result(result, streams=streams)
    status_value = str(result.status.value)
    evidence_status = {
        "complete": EvidenceStatus.COMPLETE,
        "partial": EvidenceStatus.PARTIAL,
        "cancelled": EvidenceStatus.PARTIAL,
        "blocked": EvidenceStatus.PARTIAL,
        "failed": EvidenceStatus.FAILED,
    }.get(status_value, EvidenceStatus.FAILED)
    return writer.finalize(status=evidence_status, completed_time=records[-1].emitted_time)


def complete_interrupted_finalization(
    session: "Session",
    *,
    bundle_id: str,
    resume_token: str,
) -> EvidenceBundle:
    """Complete a publication transaction that stopped after intent was saved."""

    state = read_writer_state(session, bundle_id)
    state.authorize(resume_token)
    if state.phase == WriterPhase.FINALIZED:
        manifest_path = session.root / "bundles" / state.bundle_id / "bundle.json"
        if not manifest_path.is_file():
            raise ValueError("finalized writer state is missing bundle.json")
        return EvidenceBundle.from_payload(_read_json(manifest_path))
    if state.phase != WriterPhase.FINALIZING:
        raise RuntimeError(
            f"evidence writer is {state.phase.value}; no finalization intent is available"
        )
    bundle, _ = _publish_finalizing_state(session, state)
    return bundle


def _publish_finalizing_state(
    session: "Session",
    state: EvidenceWriterState,
) -> tuple[EvidenceBundle, EvidenceWriterState]:
    if session.read_only or session.status.value != "open":
        raise PermissionError("cannot publish evidence in a read-only or finalized session")
    if state.phase != WriterPhase.FINALIZING:
        raise RuntimeError("writer state does not contain a finalization transaction")
    ledger_path = session.root / state.ledger_uri
    ledger = read_evidence_ledger(ledger_path)
    if ledger.integrity.status != IntegrityStatus.VALID:
        detail = ledger.integrity.issues[0].message if ledger.integrity.issues else "unknown"
        raise ValueError(
            f"cannot finalize an evidence ledger with {ledger.integrity.status.value} "
            f"integrity: {detail}"
        )
    observed_last = ledger.records[-1].sequence if ledger.records else None
    if observed_last != state.last_sequence:
        raise ValueError(
            f"writer finalization sequence mismatch: expected {state.last_sequence}, "
            f"observed {observed_last}"
        )
    namespace = f"bundles/{state.bundle_id}"
    interrupted = tuple(
        reference
        for reference in state.artifacts
        if reference.role == "interrupted_evidence_log"
    )
    semantic_log = session.artifacts.register_file(
        namespace,
        f"{state.bundle_id}.semantic-log",
        "evidence_log",
        ledger_path,
        EVIDENCE_LOG_MEDIA_TYPE,
        sensitivity=Sensitivity.PSEUDONYMIZED,
        lineage=(
            None
            if not interrupted
            else ArtifactLineage(
                component_id="eegle.evidence-recovery",
                component_version="1",
                input_artifact_ids=tuple(
                    reference.artifact_id for reference in interrupted
                ),
                input_digests=tuple(reference.digest for reference in interrupted),
                metadata={"recovery_source_count": len(interrupted)},
            )
        ),
        copy=False,
    )
    publishing_state = state.replacing(semantic_log=semantic_log)
    write_writer_state(session, publishing_state)
    bundle = EvidenceBundle(
        bundle_id=state.bundle_id,
        session_id=session.session_id,
        plan_hash=state.plan_hash,
        status=state.final_status,  # type: ignore[arg-type]
        created_time=state.created_time,
        completed_time=state.completed_time,
        last_sequence=state.last_sequence,
        semantic_log=semantic_log,
        execution_captures=state.execution_captures,
        raw_recordings=state.raw_recordings,
        artifacts=state.artifacts,
        component_states=tuple(
            ComponentStateSnapshot.from_payload(payload) for payload in state.component_states
        ),
        metadata=thaw_json(state.metadata),
    )
    manifest_path = session.root / "bundles" / state.bundle_id / "bundle.json"
    if manifest_path.exists():
        existing = EvidenceBundle.from_payload(_read_json(manifest_path))
        if existing.bundle_hash != bundle.bundle_hash:
            raise ValueError("existing bundle.json conflicts with writer finalization intent")
    else:
        _atomic_write(manifest_path, canonical_json_bytes(bundle.to_payload()))
    session.register_bundle(manifest_path.relative_to(session.root).as_posix())
    finalized_state = publishing_state.replacing(phase=WriterPhase.FINALIZED)
    write_writer_state(session, finalized_state)
    return bundle, finalized_state


def _recovery_path(ledger_path: Path, index: int) -> Path:
    if index < 1:
        raise ValueError("recovery index must be positive")
    return ledger_path.parent / f"semantic.recovered-{index}.eegle"


def _with_artifact(issue: IntegrityIssue, artifact_id: str) -> IntegrityIssue:
    return IntegrityIssue(
        code=issue.code,
        message=issue.message,
        artifact_id=artifact_id,
        frame_offset=issue.frame_offset,
        last_complete_offset=issue.last_complete_offset,
        expected=issue.expected,
        observed=issue.observed,
        recoverable=issue.recoverable,
    )


def _bundle_identifier(value: str) -> str:
    identifier = require_identifier(value, "bundle_id")
    if "/" in identifier or identifier in {".", ".."}:
        raise ValueError("bundle_id must be one safe path component")
    return identifier


def _verify_reference(
    store: ArtifactStore,
    reference: ArtifactReference,
    issues: list[IntegrityIssue],
    *,
    suppress_digest: bool = False,
) -> None:
    if not reference.embedded:
        return
    valid, message = store.verify(reference)
    if valid or message is None:
        return
    if suppress_digest and ("size mismatch" in message or "digest mismatch" in message):
        return
    if "missing" in message:
        code = IntegrityIssueCode.ARTIFACT_MISSING
    elif "size mismatch" in message:
        code = IntegrityIssueCode.ARTIFACT_SIZE_MISMATCH
    else:
        code = IntegrityIssueCode.ARTIFACT_DIGEST_MISMATCH
    issues.append(
        IntegrityIssue(code=code, message=message, artifact_id=reference.artifact_id)
    )


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid evidence bundle JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("evidence bundle manifest must be a JSON object")
    return payload


def _atomic_write(path: Path, data: bytes) -> None:
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
