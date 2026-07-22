"""Portable content-addressed references to embedded or external artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import require_digest, require_identifier


ARTIFACT_REFERENCE_SCHEMA = "eegle.artifact_reference.v1"


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PSEUDONYMIZED = "pseudonymized"
    RESTRICTED = "restricted"


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    role: str
    uri: str
    digest: str
    media_type: str
    size_bytes: int
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    embedded: bool = False
    schema: str = ARTIFACT_REFERENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ARTIFACT_REFERENCE_SCHEMA:
            raise ValueError(f"unsupported artifact reference schema: {self.schema}")
        object.__setattr__(self, "artifact_id", require_identifier(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "role", require_identifier(self.role, "role"))
        object.__setattr__(self, "digest", require_digest(self.digest))
        if not self.uri.strip() or not self.media_type.strip():
            raise ValueError("artifact URI and media type cannot be empty")
        object.__setattr__(self, "size_bytes", int(self.size_bytes))
        if self.size_bytes < 0:
            raise ValueError("artifact size cannot be negative")
        object.__setattr__(self, "sensitivity", Sensitivity(self.sensitivity))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "artifact_id": self.artifact_id,
            "role": self.role,
            "uri": self.uri,
            "digest": self.digest,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sensitivity": self.sensitivity.value,
            "embedded": self.embedded,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArtifactReference":
        return cls(
            schema=str(payload.get("schema", ARTIFACT_REFERENCE_SCHEMA)),
            artifact_id=str(payload["artifact_id"]),
            role=str(payload["role"]),
            uri=str(payload["uri"]),
            digest=str(payload["digest"]),
            media_type=str(payload["media_type"]),
            size_bytes=int(payload["size_bytes"]),
            sensitivity=Sensitivity(str(payload.get("sensitivity", "internal"))),
            embedded=bool(payload.get("embedded", False)),
        )
