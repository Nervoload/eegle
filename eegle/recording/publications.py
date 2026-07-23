"""Typed graph publications for artifacts produced during an execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.compiler.lock import canonical_json_bytes, content_hash
from eegle.recording.artifacts import ArtifactReference
from eegle.streams.clocks import TimePoint


ARTIFACT_PUBLICATION_SCHEMA = "eegle.artifact_publication.v1"


@dataclass(frozen=True, slots=True)
class ArtifactPublication:
    """A content reference plus the execution facts that produced it.

    ``materialized_payload`` is intentionally limited to JSON-shaped content.
    Large or source-native artifacts remain reference-only and use a storage
    adapter outside the execution kernel.
    """

    publication_id: str
    reference: ArtifactReference
    producer_component_id: str
    producer_component_version: str
    produced_time: TimePoint
    input_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]
    materialized_payload: Mapping[str, Any] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    schema: str = ARTIFACT_PUBLICATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ARTIFACT_PUBLICATION_SCHEMA:
            raise ValueError(f"unsupported artifact publication schema: {self.schema}")
        for field in ("publication_id", "producer_component_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        if not str(self.producer_component_version).strip():
            raise ValueError("producer_component_version cannot be empty")
        object.__setattr__(
            self,
            "input_ids",
            tuple(require_identifier(value, "input_id") for value in self.input_ids),
        )
        object.__setattr__(self, "metadata", freeze_json(self.metadata or {}))
        if self.materialized_payload is not None:
            payload = freeze_json(self.materialized_payload)
            if not isinstance(payload, MappingProxyType):
                raise TypeError("materialized artifact payload must be a JSON object")
            if self.reference.media_type != "application/json":
                raise ValueError("materialized JSON requires application/json media type")
            observed = content_hash(canonical_json_bytes(thaw_json(payload)))
            if observed != self.reference.digest:
                raise ValueError("materialized artifact payload digest does not match reference")
            object.__setattr__(self, "materialized_payload", payload)

    @property
    def artifact_id(self) -> str:
        return self.reference.artifact_id

    @property
    def available_time(self) -> TimePoint:
        return self.produced_time

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "publication_id": self.publication_id,
            "reference": self.reference.to_payload(),
            "producer_component_id": self.producer_component_id,
            "producer_component_version": self.producer_component_version,
            "produced_time": self.produced_time.to_payload(),
            "input_ids": list(self.input_ids),
            "metadata": thaw_json(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArtifactPublication":
        return cls(
            schema=str(payload.get("schema", ARTIFACT_PUBLICATION_SCHEMA)),
            publication_id=str(payload["publication_id"]),
            reference=ArtifactReference.from_payload(payload["reference"]),
            producer_component_id=str(payload["producer_component_id"]),
            producer_component_version=str(payload["producer_component_version"]),
            produced_time=TimePoint.from_payload(payload["produced_time"]),
            input_ids=tuple(str(value) for value in payload.get("input_ids", ())),
            metadata=dict(payload.get("metadata") or {}),
            materialized_payload=None,
        )
