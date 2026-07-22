"""Versioned evidence envelopes and bundle manifests."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.recording.artifacts import ArtifactReference
from eegle.streams.clocks import TimePoint


EVIDENCE_RECORD_SCHEMA = "eegle.evidence_record.v1"
EVIDENCE_BUNDLE_MANIFEST_SCHEMA = "eegle.evidence_bundle_manifest.v1"


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    record_id: str
    record_type: str
    sequence: int
    emitted_time: TimePoint
    payload: Mapping[str, Any]
    schema: str = EVIDENCE_RECORD_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != EVIDENCE_RECORD_SCHEMA:
            raise ValueError(f"unsupported evidence record schema: {self.schema}")
        object.__setattr__(self, "record_id", require_identifier(self.record_id, "record_id"))
        object.__setattr__(self, "record_type", require_identifier(self.record_type, "record_type"))
        object.__setattr__(self, "sequence", int(self.sequence))
        if self.sequence < 0:
            raise ValueError("evidence record sequence cannot be negative")
        object.__setattr__(self, "payload", freeze_json(self.payload))

    @property
    def record_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "record_id": self.record_id,
            "record_type": self.record_type,
            "sequence": self.sequence,
            "emitted_time": self.emitted_time.to_payload(),
            "payload": thaw_json(self.payload),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["record_hash"] = self.record_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EvidenceRecord":
        record = cls(
            schema=str(payload.get("schema", EVIDENCE_RECORD_SCHEMA)),
            record_id=str(payload["record_id"]),
            record_type=str(payload["record_type"]),
            sequence=int(payload["sequence"]),
            emitted_time=TimePoint.from_payload(payload["emitted_time"]),
            payload=dict(payload["payload"]),
        )
        if payload.get("record_hash") != record.record_hash:
            raise ValueError("evidence record hash mismatch")
        return record


class EvidenceStatus(str, Enum):
    OPEN = "open"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EvidenceBundleManifest:
    bundle_id: str
    plan_hash: str
    status: EvidenceStatus
    created_time: TimePoint
    record_logs: tuple[ArtifactReference, ...]
    artifacts: tuple[ArtifactReference, ...] = ()
    completed_time: TimePoint | None = None
    last_sequence: int | None = None
    schema: str = EVIDENCE_BUNDLE_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != EVIDENCE_BUNDLE_MANIFEST_SCHEMA:
            raise ValueError(f"unsupported evidence manifest schema: {self.schema}")
        object.__setattr__(self, "bundle_id", require_identifier(self.bundle_id, "bundle_id"))
        object.__setattr__(self, "plan_hash", require_digest(self.plan_hash, "plan_hash"))
        object.__setattr__(self, "status", EvidenceStatus(self.status))
        if not self.record_logs:
            raise ValueError("evidence bundle requires at least one record log")
        if self.last_sequence is not None and int(self.last_sequence) < 0:
            raise ValueError("last_sequence cannot be negative")
        if self.status == EvidenceStatus.COMPLETE and self.completed_time is None:
            raise ValueError("complete evidence bundle requires completed_time")

    @property
    def manifest_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "bundle_id": self.bundle_id,
            "plan_hash": self.plan_hash,
            "status": self.status.value,
            "created_time": self.created_time.to_payload(),
            "completed_time": None
            if self.completed_time is None
            else self.completed_time.to_payload(),
            "last_sequence": self.last_sequence,
            "record_logs": [value.to_payload() for value in self.record_logs],
            "artifacts": [value.to_payload() for value in self.artifacts],
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["manifest_hash"] = self.manifest_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EvidenceBundleManifest":
        manifest = cls(
            schema=str(payload.get("schema", EVIDENCE_BUNDLE_MANIFEST_SCHEMA)),
            bundle_id=str(payload["bundle_id"]),
            plan_hash=str(payload["plan_hash"]),
            status=EvidenceStatus(str(payload["status"])),
            created_time=TimePoint.from_payload(payload["created_time"]),
            completed_time=None
            if payload.get("completed_time") is None
            else TimePoint.from_payload(payload["completed_time"]),
            last_sequence=None
            if payload.get("last_sequence") is None
            else int(payload["last_sequence"]),
            record_logs=tuple(
                ArtifactReference.from_payload(item) for item in payload["record_logs"]
            ),
            artifacts=tuple(
                ArtifactReference.from_payload(item) for item in payload.get("artifacts", ())
            ),
        )
        if payload.get("manifest_hash") != manifest.manifest_hash:
            raise ValueError("evidence bundle manifest hash mismatch")
        return manifest
