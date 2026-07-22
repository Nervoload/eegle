"""Shared enums and lineage records for the modality-neutral foundation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import require_digest, require_identifier


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

    def to_payload(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "input_ids": list(self.input_ids),
            "component_version": self.component_version,
            "state_hash": self.state_hash,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Lineage":
        return cls(
            component_id=str(payload["component_id"]),
            input_ids=tuple(str(value) for value in payload.get("input_ids", ())),
            component_version=None
            if payload.get("component_version") is None
            else str(payload["component_version"]),
            state_hash=None if payload.get("state_hash") is None else str(payload["state_hash"]),
        )
