"""Delayed, missing, and multi-purpose outcome records."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import freeze_json, require_finite, require_identifier, thaw_json
from eegle.streams.clocks import TimePoint


OUTCOME_SCHEMA = "eegle.outcome.v2"
OUTCOME_REFERENCE_SCHEMA = "eegle.outcome_reference.v1"
OUTCOME_DISPOSITION_SCHEMA = "eegle.outcome_disposition.v1"


class OutcomeUse(str, Enum):
    METRICS = "metrics"
    CALIBRATION = "calibration"
    ADAPTATION = "adaptation"
    POLICY = "policy"


class PendingPredictionOverflow(str, Enum):
    EXPIRE_OLDEST = "expire_oldest"
    REJECT_NEWEST = "reject_newest"


class OutcomeReferenceKind(str, Enum):
    PREDICTION = "prediction"
    EVENT = "event"
    WINDOW = "window"
    ACTION = "action"
    ARTIFACT = "artifact"
    PHASE = "phase"


@dataclass(frozen=True, slots=True)
class OutcomeReference:
    reference_kind: OutcomeReferenceKind
    reference_id: str
    schema: str = OUTCOME_REFERENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != OUTCOME_REFERENCE_SCHEMA:
            raise ValueError(f"unsupported outcome reference schema: {self.schema}")
        object.__setattr__(self, "reference_kind", OutcomeReferenceKind(self.reference_kind))
        object.__setattr__(
            self, "reference_id", require_identifier(self.reference_id, "reference_id")
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "reference_kind": self.reference_kind.value,
            "reference_id": self.reference_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "OutcomeReference":
        return cls(
            schema=str(payload.get("schema", OUTCOME_REFERENCE_SCHEMA)),
            reference_kind=OutcomeReferenceKind(str(payload["reference_kind"])),
            reference_id=str(payload["reference_id"]),
        )


class OutcomeDispositionStatus(str, Enum):
    PENDING = "pending"
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    EXPIRED = "expired"
    OVERFLOWED = "overflowed"
    DUPLICATE = "duplicate"
    DISPUTED = "disputed"
    RETROSPECTIVE_ONLY = "retrospective_only"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    PENDING_AT_CLOSE = "pending_at_close"


@dataclass(frozen=True, slots=True)
class OutcomeDisposition:
    disposition_id: str
    expectation_id: str
    reference: OutcomeReference
    status: OutcomeDispositionStatus
    decided_time: TimePoint
    outcome_id: str | None = None
    prediction_id: str | None = None
    uses_granted: tuple[OutcomeUse, ...] = ()
    uses_denied: tuple[OutcomeUse, ...] = ()
    reason_code: str | None = None
    schema: str = OUTCOME_DISPOSITION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != OUTCOME_DISPOSITION_SCHEMA:
            raise ValueError(f"unsupported outcome disposition schema: {self.schema}")
        for field in ("disposition_id", "expectation_id"):
            object.__setattr__(
                self, field, require_identifier(getattr(self, field), field)
            )
        object.__setattr__(self, "status", OutcomeDispositionStatus(self.status))
        if not isinstance(self.reference, OutcomeReference):
            raise TypeError("outcome disposition requires a typed direct reference")
        if self.outcome_id is not None:
            object.__setattr__(
                self, "outcome_id", require_identifier(self.outcome_id, "outcome_id")
            )
        if self.prediction_id is not None:
            object.__setattr__(
                self,
                "prediction_id",
                require_identifier(self.prediction_id, "prediction_id"),
            )
        granted = tuple(OutcomeUse(value) for value in self.uses_granted)
        denied = tuple(OutcomeUse(value) for value in self.uses_denied)
        if len(granted) != len(set(granted)) or len(denied) != len(set(denied)):
            raise ValueError("outcome disposition uses must be unique")
        if set(granted).intersection(denied):
            raise ValueError("outcome uses cannot be both granted and denied")
        object.__setattr__(self, "uses_granted", granted)
        object.__setattr__(self, "uses_denied", denied)
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                require_identifier(self.reason_code, "reason_code"),
            )
        if self.status == OutcomeDispositionStatus.PENDING:
            if self.outcome_id is not None or self.prediction_id is None:
                raise ValueError("pending disposition requires only prediction identity")
        elif self.reason_code is None and self.status != OutcomeDispositionStatus.MATCHED:
            raise ValueError("non-matched terminal outcome disposition requires a reason")
        if self.status == OutcomeDispositionStatus.MATCHED:
            if self.outcome_id is None or self.prediction_id is None or not granted:
                raise ValueError("matched disposition requires outcome, prediction, and use")

    @property
    def terminal(self) -> bool:
        return self.status != OutcomeDispositionStatus.PENDING

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "disposition_id": self.disposition_id,
            "expectation_id": self.expectation_id,
            "reference": self.reference.to_payload(),
            "status": self.status.value,
            "decided_time": self.decided_time.to_payload(),
            "outcome_id": self.outcome_id,
            "prediction_id": self.prediction_id,
            "uses_granted": [value.value for value in self.uses_granted],
            "uses_denied": [value.value for value in self.uses_denied],
            "reason_code": self.reason_code,
            "terminal": self.terminal,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "OutcomeDisposition":
        value = cls(
            schema=str(payload.get("schema", OUTCOME_DISPOSITION_SCHEMA)),
            disposition_id=str(payload["disposition_id"]),
            expectation_id=str(payload["expectation_id"]),
            reference=OutcomeReference.from_payload(payload["reference"]),
            status=OutcomeDispositionStatus(str(payload["status"])),
            decided_time=TimePoint.from_payload(payload["decided_time"]),
            outcome_id=None if payload.get("outcome_id") is None else str(payload["outcome_id"]),
            prediction_id=None
            if payload.get("prediction_id") is None
            else str(payload["prediction_id"]),
            uses_granted=tuple(OutcomeUse(str(item)) for item in payload.get("uses_granted", ())),
            uses_denied=tuple(OutcomeUse(str(item)) for item in payload.get("uses_denied", ())),
            reason_code=None
            if payload.get("reason_code") is None
            else str(payload["reason_code"]),
        )
        if bool(payload.get("terminal", value.terminal)) != value.terminal:
            raise ValueError("outcome disposition terminal flag mismatch")
        return value


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
    references: tuple[OutcomeReference, ...] = ()
    retrospective_only: bool = False
    schema: str = OUTCOME_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != OUTCOME_SCHEMA:
            raise ValueError(f"unsupported outcome schema: {self.schema}")
        for field in ("outcome_id", "subject_id", "source_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(
            self,
            "references", tuple(self.references)
        )
        if not self.references or not all(
            isinstance(value, OutcomeReference) for value in self.references
        ):
            raise ValueError("outcome requires at least one typed direct reference")
        reference_keys = tuple(
            (value.reference_kind, value.reference_id) for value in self.references
        )
        if len(reference_keys) != len(set(reference_keys)):
            raise ValueError("outcome direct references must be unique")
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
            "references": [value.to_payload() for value in self.references],
            "retrospective_only": self.retrospective_only,
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
            references=tuple(
                OutcomeReference.from_payload(value)
                for value in payload.get("references", ())
            ),
            retrospective_only=bool(payload.get("retrospective_only", False)),
        )

    @property
    def prediction_ids(self) -> tuple[str, ...]:
        return tuple(
            value.reference_id
            for value in self.references
            if value.reference_kind == OutcomeReferenceKind.PREDICTION
        )
