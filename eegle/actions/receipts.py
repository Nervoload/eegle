"""Observed actuator responses; never inferred from the command alone."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.streams.clocks import TimePoint

ACTION_RECEIPT_SCHEMA = "eegle.action_receipt.v1"


class ReceiptStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    receipt_id: str
    command_id: str
    actuator_id: str
    status: ReceiptStatus
    observed_time: TimePoint
    authorization_decision_id: str
    delivered_time: TimePoint | None = None
    details: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = ACTION_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ACTION_RECEIPT_SCHEMA:
            raise ValueError(f"unsupported action receipt schema: {self.schema}")
        for field in ("receipt_id", "command_id", "actuator_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(
            self,
            "authorization_decision_id",
            require_identifier(self.authorization_decision_id, "authorization_decision_id"),
        )
        object.__setattr__(self, "status", ReceiptStatus(self.status))
        if self.status == ReceiptStatus.DELIVERED:
            if self.delivered_time is None:
                raise ValueError("delivered receipt requires delivered_time")
            if self.delivered_time.clock_id != self.observed_time.clock_id:
                raise ValueError(
                    "delivered_time and observed_time must use the same clock"
                )
            if self.delivered_time.seconds < 0:
                raise ValueError("delivered_time cannot be negative")
            if self.delivered_time.seconds > self.observed_time.seconds:
                raise ValueError("delivered_time cannot follow observed_time")
        elif self.delivered_time is not None:
            raise ValueError("only a delivered receipt may include delivered_time")
        object.__setattr__(self, "details", freeze_json(self.details or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "receipt_id": self.receipt_id,
            "command_id": self.command_id,
            "actuator_id": self.actuator_id,
            "status": self.status.value,
            "observed_time": self.observed_time.to_payload(),
            "delivered_time": None
            if self.delivered_time is None
            else self.delivered_time.to_payload(),
            "authorization_decision_id": self.authorization_decision_id,
            "details": thaw_json(self.details),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ActionReceipt":
        delivered = payload.get("delivered_time")
        return cls(
            schema=str(payload["schema"]),
            receipt_id=str(payload["receipt_id"]),
            command_id=str(payload["command_id"]),
            actuator_id=str(payload["actuator_id"]),
            status=ReceiptStatus(str(payload["status"])),
            observed_time=TimePoint.from_payload(payload["observed_time"]),
            delivered_time=None if delivered is None else TimePoint.from_payload(delivered),
            authorization_decision_id=str(payload["authorization_decision_id"]),
            details=dict(payload.get("details") or {}),
        )
