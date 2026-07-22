"""Explicit rejection and adaptive/component state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.streams.clocks import TimePoint


REJECTION_SCHEMA = "eegle.rejection.v1"
STATE_TRANSITION_SCHEMA = "eegle.state_transition.v1"


@dataclass(frozen=True, slots=True)
class Rejection:
    rejection_id: str
    work_id: str
    component_id: str
    stage: str
    reason_code: str
    rejected_time: TimePoint
    input_ids: tuple[str, ...] = ()
    details: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = REJECTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != REJECTION_SCHEMA:
            raise ValueError(f"unsupported rejection schema: {self.schema}")
        for field in ("rejection_id", "work_id", "component_id", "stage", "reason_code"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(
            self,
            "input_ids",
            tuple(require_identifier(value, "input_id") for value in self.input_ids),
        )
        object.__setattr__(self, "details", freeze_json(self.details or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "rejection_id": self.rejection_id,
            "work_id": self.work_id,
            "component_id": self.component_id,
            "stage": self.stage,
            "reason_code": self.reason_code,
            "rejected_time": self.rejected_time.to_payload(),
            "input_ids": list(self.input_ids),
            "details": thaw_json(self.details),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Rejection":
        return cls(
            schema=str(payload.get("schema", REJECTION_SCHEMA)),
            rejection_id=str(payload["rejection_id"]),
            work_id=str(payload["work_id"]),
            component_id=str(payload["component_id"]),
            stage=str(payload["stage"]),
            reason_code=str(payload["reason_code"]),
            rejected_time=TimePoint.from_payload(payload["rejected_time"]),
            input_ids=tuple(str(value) for value in payload.get("input_ids", ())),
            details=dict(payload.get("details") or {}),
        )


class TransitionStatus(str, Enum):
    APPLIED = "applied"
    REJECTED = "rejected"
    NO_OP = "no_op"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StateTransition:
    transition_id: str
    component_id: str
    status: TransitionStatus
    transition_kind: str
    transition_time: TimePoint
    prior_state_hash: str
    resulting_state_hash: str
    trigger_ids: tuple[str, ...]
    reason: str | None = None
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = STATE_TRANSITION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != STATE_TRANSITION_SCHEMA:
            raise ValueError(f"unsupported state transition schema: {self.schema}")
        for field in ("transition_id", "component_id", "transition_kind"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "status", TransitionStatus(self.status))
        object.__setattr__(
            self, "prior_state_hash", require_digest(self.prior_state_hash, "prior_state_hash")
        )
        object.__setattr__(
            self,
            "resulting_state_hash",
            require_digest(self.resulting_state_hash, "resulting_state_hash"),
        )
        triggers = tuple(require_identifier(value, "trigger_id") for value in self.trigger_ids)
        if not triggers:
            raise ValueError("state transition must reference at least one trigger")
        object.__setattr__(self, "trigger_ids", triggers)
        object.__setattr__(self, "metadata", freeze_json(self.metadata or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "transition_id": self.transition_id,
            "component_id": self.component_id,
            "status": self.status.value,
            "transition_kind": self.transition_kind,
            "transition_time": self.transition_time.to_payload(),
            "prior_state_hash": self.prior_state_hash,
            "resulting_state_hash": self.resulting_state_hash,
            "trigger_ids": list(self.trigger_ids),
            "reason": self.reason,
            "metadata": thaw_json(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StateTransition":
        return cls(
            schema=str(payload.get("schema", STATE_TRANSITION_SCHEMA)),
            transition_id=str(payload["transition_id"]),
            component_id=str(payload["component_id"]),
            status=TransitionStatus(str(payload["status"])),
            transition_kind=str(payload["transition_kind"]),
            transition_time=TimePoint.from_payload(payload["transition_time"]),
            prior_state_hash=str(payload["prior_state_hash"]),
            resulting_state_hash=str(payload["resulting_state_hash"]),
            trigger_ids=tuple(str(value) for value in payload["trigger_ids"]),
            reason=None if payload.get("reason") is None else str(payload["reason"]),
            metadata=dict(payload.get("metadata") or {}),
        )
