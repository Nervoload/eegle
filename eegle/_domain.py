"""Shared enums and lineage records for the modality-neutral foundation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json

if TYPE_CHECKING:
    from eegle.streams.clocks import TimePoint


class ExecutionMode(str, Enum):
    CAUSAL = "causal"
    RETROSPECTIVE = "retrospective"
    ORACLE = "oracle"


class EquivalenceLevel(str, Enum):
    BITWISE = "bitwise"
    NUMERIC = "numeric"
    SEMANTIC = "semantic"
    TRACE = "trace"
    NON_REPLAYABLE = "non_replayable"


class Determinism(str, Enum):
    DETERMINISTIC = "deterministic"
    SEEDED = "seeded"
    NONDETERMINISTIC = "nondeterministic"
    EXTERNAL = "external"


class ComponentKind(str, Enum):
    SOURCE = "source"
    TRANSFORM = "transform"
    WINDOW = "window"
    QUALITY = "quality"
    MODEL = "model"
    OUTCOME = "outcome"
    ADAPTER = "adapter"
    POLICY = "policy"
    ACTUATOR = "actuator"
    SINK = "sink"


class WorkStatus(str, Enum):
    PREDICTED = "predicted"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    PENDING = "pending"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Lineage:
    """References the exact inputs and component state that produced a record."""

    component_id: str
    input_ids: tuple[str, ...] = ()
    component_version: str | None = None
    state_hash: str | None = None
    latest_input_available_time: "TimePoint | None" = None
    clock_mapping_revisions: Mapping[str, int] = None  # type: ignore[assignment]
    stream_revisions: Mapping[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "component_id", require_identifier(self.component_id, "component_id"))
        object.__setattr__(
            self,
            "input_ids",
            tuple(require_identifier(value, "input_id") for value in self.input_ids),
        )
        if self.component_version is not None and not str(self.component_version).strip():
            raise ValueError("component_version cannot be empty")
        if self.state_hash is not None:
            object.__setattr__(self, "state_hash", require_digest(self.state_hash, "state_hash"))
        if self.input_ids and self.latest_input_available_time is None:
            raise ValueError("derived lineage requires latest_input_available_time")
        if not self.input_ids and self.latest_input_available_time is not None:
            raise ValueError("source lineage cannot declare input availability without inputs")
        if self.latest_input_available_time is not None:
            from eegle.streams.clocks import TimePoint

            if not isinstance(self.latest_input_available_time, TimePoint):
                raise TypeError("latest_input_available_time must be a TimePoint")
        object.__setattr__(
            self,
            "clock_mapping_revisions",
            freeze_json(
                _positive_revisions(
                    self.clock_mapping_revisions or {},
                    field="clock_mapping_revisions",
                )
            ),
        )
        object.__setattr__(
            self,
            "stream_revisions",
            freeze_json(
                _positive_revisions(self.stream_revisions or {}, field="stream_revisions")
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "input_ids": list(self.input_ids),
            "component_version": self.component_version,
            "state_hash": self.state_hash,
            "latest_input_available_time": None
            if self.latest_input_available_time is None
            else self.latest_input_available_time.to_payload(),
            "clock_mapping_revisions": thaw_json(self.clock_mapping_revisions),
            "stream_revisions": thaw_json(self.stream_revisions),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Lineage":
        from eegle.streams.clocks import TimePoint

        available = payload.get("latest_input_available_time")
        return cls(
            component_id=str(payload["component_id"]),
            input_ids=tuple(str(value) for value in payload.get("input_ids", ())),
            component_version=None
            if payload.get("component_version") is None
            else str(payload["component_version"]),
            state_hash=None if payload.get("state_hash") is None else str(payload["state_hash"]),
            latest_input_available_time=None
            if available is None
            else TimePoint.from_payload(available),
            clock_mapping_revisions={
                str(key): int(value)
                for key, value in dict(payload.get("clock_mapping_revisions") or {}).items()
            },
            stream_revisions={
                str(key): int(value)
                for key, value in dict(payload.get("stream_revisions") or {}).items()
            },
        )


def _positive_revisions(values: Mapping[str, int], *, field: str) -> dict[str, int]:
    revisions: dict[str, int] = {}
    for key, value in values.items():
        identifier = require_identifier(str(key), f"{field} key")
        revision = int(value)
        if revision <= 0:
            raise ValueError(f"{field}[{identifier}] must be positive")
        revisions[identifier] = revision
    return revisions
