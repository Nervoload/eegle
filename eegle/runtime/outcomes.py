"""Delayed, missing, and multi-purpose outcome records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import freeze_json, require_finite, require_identifier, thaw_json
from eegle.streams.clocks import TimePoint


OUTCOME_SCHEMA = "eegle.outcome.v1"


class OutcomeUse(str, Enum):
    METRICS = "metrics"
    CALIBRATION = "calibration"
    ADAPTATION = "adaptation"
    POLICY = "policy"


class PendingPredictionOverflow(str, Enum):
    EXPIRE_OLDEST = "expire_oldest"
    REJECT_NEWEST = "reject_newest"


@dataclass(frozen=True, slots=True)
class OutcomeRoutingPolicy:
    """Bounded, deterministic delayed-outcome routing owned by the engine."""

    max_pending_predictions: int = 128
    prediction_ttl_seconds: float = 300.0
    overflow: PendingPredictionOverflow = PendingPredictionOverflow.EXPIRE_OLDEST
    allowed_uses: frozenset[OutcomeUse] = frozenset(OutcomeUse)

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_pending_predictions", int(self.max_pending_predictions))
        if self.max_pending_predictions <= 0:
            raise ValueError("max_pending_predictions must be positive")
        object.__setattr__(
            self,
            "prediction_ttl_seconds",
            require_finite(self.prediction_ttl_seconds, "prediction_ttl_seconds"),
        )
        if self.prediction_ttl_seconds < 0:
            raise ValueError("prediction_ttl_seconds cannot be negative")
        object.__setattr__(self, "overflow", PendingPredictionOverflow(self.overflow))
        object.__setattr__(
            self,
            "allowed_uses",
            frozenset(OutcomeUse(value) for value in self.allowed_uses),
        )


@dataclass(frozen=True, slots=True)
class Outcome:
    outcome_id: str
    subject_id: str
    source_id: str
    value: Any
    event_time: TimePoint
    available_time: TimePoint
    permitted_uses: frozenset[OutcomeUse]
    prediction_ids: tuple[str, ...] = ()
    schema: str = OUTCOME_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != OUTCOME_SCHEMA:
            raise ValueError(f"unsupported outcome schema: {self.schema}")
        for field in ("outcome_id", "subject_id", "source_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(
            self,
            "prediction_ids",
            tuple(require_identifier(value, "prediction_id") for value in self.prediction_ids),
        )
        uses = frozenset(OutcomeUse(value) for value in self.permitted_uses)
        if not uses:
            raise ValueError("outcome must permit at least one explicit use")
        object.__setattr__(self, "permitted_uses", uses)
        object.__setattr__(self, "value", freeze_json(self.value))
        if (
            self.event_time.clock_id == self.available_time.clock_id
            and self.available_time.seconds < self.event_time.seconds
        ):
            raise ValueError("outcome available_time cannot precede event_time on the same clock")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "outcome_id": self.outcome_id,
            "subject_id": self.subject_id,
            "source_id": self.source_id,
            "value": thaw_json(self.value),
            "event_time": self.event_time.to_payload(),
            "available_time": self.available_time.to_payload(),
            "permitted_uses": sorted(value.value for value in self.permitted_uses),
            "prediction_ids": list(self.prediction_ids),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Outcome":
        return cls(
            schema=str(payload.get("schema", OUTCOME_SCHEMA)),
            outcome_id=str(payload["outcome_id"]),
            subject_id=str(payload["subject_id"]),
            source_id=str(payload["source_id"]),
            value=payload.get("value"),
            event_time=TimePoint.from_payload(payload["event_time"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            permitted_uses=frozenset(OutcomeUse(str(value)) for value in payload["permitted_uses"]),
            prediction_ids=tuple(str(value) for value in payload.get("prediction_ids", ())),
        )
