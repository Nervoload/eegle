"""Site authorization is distinct from suite intent and model policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.streams.clocks import TimePoint


AUTHORIZATION_SCHEMA = "eegle.authorization_decision.v1"


class AuthorizationStatus(str, Enum):
    AUTHORIZED = "authorized"
    DENIED = "denied"
    OBSERVE_ONLY = "observe_only"
    INTERLOCKED = "interlocked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    decision_id: str
    command_id: str
    provider_id: str
    status: AuthorizationStatus
    decided_time: TimePoint
    reason: str | None = None
    evidence: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = AUTHORIZATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != AUTHORIZATION_SCHEMA:
            raise ValueError(f"unsupported authorization schema: {self.schema}")
        for field in ("decision_id", "command_id", "provider_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "status", AuthorizationStatus(self.status))
        object.__setattr__(self, "evidence", freeze_json(self.evidence or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "decision_id": self.decision_id,
            "command_id": self.command_id,
            "provider_id": self.provider_id,
            "status": self.status.value,
            "decided_time": self.decided_time.to_payload(),
            "reason": self.reason,
            "evidence": thaw_json(self.evidence),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AuthorizationDecision":
        return cls(
            schema=str(payload.get("schema", AUTHORIZATION_SCHEMA)),
            decision_id=str(payload["decision_id"]),
            command_id=str(payload["command_id"]),
            provider_id=str(payload["provider_id"]),
            status=AuthorizationStatus(str(payload["status"])),
            decided_time=TimePoint.from_payload(payload["decided_time"]),
            reason=None if payload.get("reason") is None else str(payload["reason"]),
            evidence=dict(payload.get("evidence") or {}),
        )
