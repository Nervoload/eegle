"""Clock identities, clock-bearing time points, and measured mappings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import freeze_json, require_finite, require_identifier, thaw_json


CLOCK_IDENTITY_SCHEMA = "eegle.clock_identity.v1"
CLOCK_MAPPING_SCHEMA = "eegle.clock_mapping.v1"


class ClockKind(str, Enum):
    DEVICE = "device"
    HOST = "host"
    TASK = "task"
    ACTUATOR = "actuator"
    VIRTUAL = "virtual"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class TimePoint:
    seconds: float
    clock_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "seconds", require_finite(self.seconds, "seconds"))
        object.__setattr__(self, "clock_id", require_identifier(self.clock_id, "clock_id"))

    def to_payload(self) -> dict[str, Any]:
        return {"seconds": self.seconds, "clock_id": self.clock_id}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TimePoint":
        return cls(seconds=float(payload["seconds"]), clock_id=str(payload["clock_id"]))


@dataclass(frozen=True, slots=True)
class ClockIdentity:
    clock_id: str
    kind: ClockKind
    name: str | None = None
    provenance: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = CLOCK_IDENTITY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CLOCK_IDENTITY_SCHEMA:
            raise ValueError(f"unsupported clock identity schema: {self.schema}")
        object.__setattr__(self, "clock_id", require_identifier(self.clock_id, "clock_id"))
        object.__setattr__(self, "kind", ClockKind(self.kind))
        if self.name is not None and not str(self.name).strip():
            raise ValueError("clock name cannot be empty")
        object.__setattr__(self, "provenance", freeze_json(self.provenance or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "clock_id": self.clock_id,
            "kind": self.kind.value,
            "name": self.name,
            "provenance": thaw_json(self.provenance),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ClockIdentity":
        return cls(
            schema=str(payload.get("schema", CLOCK_IDENTITY_SCHEMA)),
            clock_id=str(payload["clock_id"]),
            kind=ClockKind(str(payload["kind"])),
            name=None if payload.get("name") is None else str(payload["name"]),
            provenance=dict(payload.get("provenance") or {}),
        )


@dataclass(frozen=True, slots=True)
class ClockMapping:
    mapping_id: str
    revision: int
    source_clock_id: str
    target_clock_id: str
    offset_seconds: float
    available_time: TimePoint
    scale: float = 1.0
    uncertainty_seconds: float = 0.0
    valid_source_start: float | None = None
    valid_source_end: float | None = None
    measured_at: TimePoint | None = None
    supersedes_mapping_id: str | None = None
    provenance: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = CLOCK_MAPPING_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CLOCK_MAPPING_SCHEMA:
            raise ValueError(f"unsupported clock mapping schema: {self.schema}")
        object.__setattr__(self, "mapping_id", require_identifier(self.mapping_id, "mapping_id"))
        object.__setattr__(self, "revision", int(self.revision))
        if self.revision <= 0:
            raise ValueError("clock mapping revision must be positive")
        object.__setattr__(
            self, "source_clock_id", require_identifier(self.source_clock_id, "source_clock_id")
        )
        object.__setattr__(
            self, "target_clock_id", require_identifier(self.target_clock_id, "target_clock_id")
        )
        if self.source_clock_id == self.target_clock_id:
            raise ValueError("clock mapping source and target must be different")
        if self.available_time.clock_id != self.target_clock_id:
            raise ValueError("clock mapping available_time must use the target clock")
        if self.supersedes_mapping_id is not None:
            object.__setattr__(
                self,
                "supersedes_mapping_id",
                require_identifier(self.supersedes_mapping_id, "supersedes_mapping_id"),
            )
            if self.supersedes_mapping_id == self.mapping_id:
                raise ValueError("clock mapping cannot supersede itself")
        object.__setattr__(
            self, "offset_seconds", require_finite(self.offset_seconds, "offset_seconds")
        )
        object.__setattr__(self, "scale", require_finite(self.scale, "scale"))
        if self.scale <= 0:
            raise ValueError("clock mapping scale must be positive")
        object.__setattr__(
            self,
            "uncertainty_seconds",
            require_finite(self.uncertainty_seconds, "uncertainty_seconds"),
        )
        if self.uncertainty_seconds < 0:
            raise ValueError("clock mapping uncertainty cannot be negative")
        if self.valid_source_start is not None:
            object.__setattr__(
                self,
                "valid_source_start",
                require_finite(self.valid_source_start, "valid_source_start"),
            )
        if self.valid_source_end is not None:
            object.__setattr__(
                self,
                "valid_source_end",
                require_finite(self.valid_source_end, "valid_source_end"),
            )
        if (
            self.valid_source_start is not None
            and self.valid_source_end is not None
            and self.valid_source_end < self.valid_source_start
        ):
            raise ValueError("clock mapping validity end precedes start")
        object.__setattr__(self, "provenance", freeze_json(self.provenance or {}))

    def map_time(self, source: TimePoint, *, as_of: TimePoint) -> TimePoint:
        if as_of.clock_id != self.available_time.clock_id:
            raise ValueError("clock mapping use time must share the availability clock")
        if as_of.seconds < self.available_time.seconds:
            raise ValueError("clock mapping was not available at the requested use time")
        if source.clock_id != self.source_clock_id:
            raise ValueError(
                f"mapping expects clock {self.source_clock_id}, received {source.clock_id}"
            )
        if self.valid_source_start is not None and source.seconds < self.valid_source_start:
            raise ValueError("source time precedes clock mapping validity interval")
        if self.valid_source_end is not None and source.seconds > self.valid_source_end:
            raise ValueError("source time follows clock mapping validity interval")
        return TimePoint(
            seconds=self.offset_seconds + self.scale * source.seconds,
            clock_id=self.target_clock_id,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "mapping_id": self.mapping_id,
            "revision": self.revision,
            "source_clock_id": self.source_clock_id,
            "target_clock_id": self.target_clock_id,
            "offset_seconds": self.offset_seconds,
            "available_time": self.available_time.to_payload(),
            "scale": self.scale,
            "uncertainty_seconds": self.uncertainty_seconds,
            "valid_source_start": self.valid_source_start,
            "valid_source_end": self.valid_source_end,
            "measured_at": None if self.measured_at is None else self.measured_at.to_payload(),
            "supersedes_mapping_id": self.supersedes_mapping_id,
            "provenance": thaw_json(self.provenance),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ClockMapping":
        measured = payload.get("measured_at")
        return cls(
            schema=str(payload.get("schema", CLOCK_MAPPING_SCHEMA)),
            mapping_id=str(payload["mapping_id"]),
            revision=int(payload["revision"]),
            source_clock_id=str(payload["source_clock_id"]),
            target_clock_id=str(payload["target_clock_id"]),
            offset_seconds=float(payload["offset_seconds"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            scale=float(payload.get("scale", 1.0)),
            uncertainty_seconds=float(payload.get("uncertainty_seconds", 0.0)),
            valid_source_start=None
            if payload.get("valid_source_start") is None
            else float(payload["valid_source_start"]),
            valid_source_end=None
            if payload.get("valid_source_end") is None
            else float(payload["valid_source_end"]),
            measured_at=None if measured is None else TimePoint.from_payload(measured),
            supersedes_mapping_id=None
            if payload.get("supersedes_mapping_id") is None
            else str(payload["supersedes_mapping_id"]),
            provenance=dict(payload.get("provenance") or {}),
        )
