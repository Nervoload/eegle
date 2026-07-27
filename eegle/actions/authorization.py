"""Typed records for independent deployment authorization."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import (
    freeze_json,
    require_digest,
    require_finite,
    require_identifier,
    thaw_json,
)
from eegle.compiler.lock import canonical_hash
from eegle.streams.clocks import TimePoint


AUTHORIZATION_REQUEST_SCHEMA = "eegle.authorization_request.v1"
AUTHORIZATION_DECISION_SCHEMA = "eegle.authorization_decision.v2"
ACTION_DISPOSITION_SCHEMA = "eegle.action_disposition.v1"
ACTION_CANCELLATION_SCHEMA = "eegle.action_cancellation.v1"


class AuthorizationStatus(str, Enum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    DENIED = "denied"
    OBSERVE_ONLY = "observe_only"
    INTERLOCKED = "interlocked"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ActionDispositionStatus(str, Enum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    SUBMITTED = "submitted"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    ACTUATOR_REJECTED = "actuator_rejected"
    OBSERVE_ONLY = "observe_only"
    DENIED = "denied"
    INTERLOCKED = "interlocked"
    EXPIRED = "expired"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    authorization_request_id: str
    action_request_id: str
    permission_id: str
    provider_id: str
    actuator_id: str
    capability: str
    parameters_digest: str
    requested_time: TimePoint
    available_time: TimePoint
    expires_at: TimePoint | None = None
    schema: str = AUTHORIZATION_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != AUTHORIZATION_REQUEST_SCHEMA:
            raise ValueError(f"unsupported authorization request schema: {self.schema}")
        for field in (
            "authorization_request_id",
            "action_request_id",
            "permission_id",
            "provider_id",
            "actuator_id",
            "capability",
        ):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(
            self,
            "parameters_digest",
            require_digest(self.parameters_digest, "parameters_digest"),
        )
        if self.requested_time.clock_id != self.available_time.clock_id:
            raise ValueError("authorization request times must share a clock")
        if self.available_time.seconds < self.requested_time.seconds:
            raise ValueError("authorization cannot be available before it was requested")
        if self.expires_at is not None:
            if self.expires_at.clock_id != self.requested_time.clock_id:
                raise ValueError("authorization expiry must use the request clock")
            if self.expires_at.seconds < self.available_time.seconds:
                raise ValueError("authorization expiry cannot precede availability")

    @classmethod
    def from_action(
        cls,
        *,
        authorization_request_id: str,
        action_request_id: str,
        permission_id: str,
        provider_id: str,
        actuator_id: str,
        capability: str,
        parameters: Mapping[str, Any],
        requested_time: TimePoint,
        available_time: TimePoint,
        expires_at: TimePoint | None,
    ) -> "AuthorizationRequest":
        return cls(
            authorization_request_id=authorization_request_id,
            action_request_id=action_request_id,
            permission_id=permission_id,
            provider_id=provider_id,
            actuator_id=actuator_id,
            capability=capability,
            parameters_digest=canonical_hash(parameters),
            requested_time=requested_time,
            available_time=available_time,
            expires_at=expires_at,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "authorization_request_id": self.authorization_request_id,
            "action_request_id": self.action_request_id,
            "permission_id": self.permission_id,
            "provider_id": self.provider_id,
            "actuator_id": self.actuator_id,
            "capability": self.capability,
            "parameters_digest": self.parameters_digest,
            "requested_time": self.requested_time.to_payload(),
            "available_time": self.available_time.to_payload(),
            "expires_at": None if self.expires_at is None else self.expires_at.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AuthorizationRequest":
        return cls(
            schema=str(payload.get("schema", AUTHORIZATION_REQUEST_SCHEMA)),
            authorization_request_id=str(payload["authorization_request_id"]),
            action_request_id=str(payload["action_request_id"]),
            permission_id=str(payload["permission_id"]),
            provider_id=str(payload["provider_id"]),
            actuator_id=str(payload["actuator_id"]),
            capability=str(payload["capability"]),
            parameters_digest=str(payload["parameters_digest"]),
            requested_time=TimePoint.from_payload(payload["requested_time"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            expires_at=None
            if payload.get("expires_at") is None
            else TimePoint.from_payload(payload["expires_at"]),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationEvaluation:
    """Runtime-only canonical parameters paired with persistent request evidence."""

    request: AuthorizationRequest
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        parameters = freeze_json(self.parameters)
        if canonical_hash(thaw_json(parameters)) != self.request.parameters_digest:
            raise ValueError("authorization parameter digest mismatch")
        object.__setattr__(self, "parameters", parameters)


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    """Provider-returned result without plan or evidence identity authority."""

    status: AuthorizationStatus
    completion_delay_seconds: float = 0.0
    valid_for_seconds: float | None = None
    reason: str | None = None
    evidence: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", AuthorizationStatus(self.status))
        if self.status in {AuthorizationStatus.EXPIRED, AuthorizationStatus.CANCELLED}:
            raise ValueError("provider cannot self-declare runtime expiry or cancellation")
        delay = require_finite(self.completion_delay_seconds, "completion_delay_seconds")
        if delay < 0:
            raise ValueError("authorization completion delay cannot be negative")
        object.__setattr__(self, "completion_delay_seconds", delay)
        if self.valid_for_seconds is not None:
            duration = require_finite(self.valid_for_seconds, "valid_for_seconds")
            if duration < 0:
                raise ValueError("authorization validity cannot be negative")
            object.__setattr__(self, "valid_for_seconds", duration)
        if self.status == AuthorizationStatus.PENDING and delay <= 0:
            raise ValueError("pending authorization requires a positive completion delay")
        if self.status != AuthorizationStatus.PENDING and delay != 0:
            raise ValueError("terminal authorization results cannot be delayed")
        if self.status != AuthorizationStatus.AUTHORIZED and self.valid_for_seconds is not None:
            raise ValueError("only authorized results may declare validity")
        object.__setattr__(self, "evidence", freeze_json(self.evidence or {}))


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    decision_id: str
    authorization_request_id: str
    action_request_id: str
    permission_id: str
    provider_id: str
    status: AuthorizationStatus
    decided_time: TimePoint
    available_time: TimePoint
    valid_until: TimePoint | None = None
    next_check_time: TimePoint | None = None
    reason: str | None = None
    evidence: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = AUTHORIZATION_DECISION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != AUTHORIZATION_DECISION_SCHEMA:
            raise ValueError(f"unsupported authorization schema: {self.schema}")
        for field in (
            "decision_id",
            "authorization_request_id",
            "action_request_id",
            "permission_id",
            "provider_id",
        ):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "status", AuthorizationStatus(self.status))
        if self.decided_time.clock_id != self.available_time.clock_id:
            raise ValueError("authorization decision times must share a clock")
        if self.available_time.seconds < self.decided_time.seconds:
            raise ValueError("authorization decision cannot be available before it is made")
        for field, value in (
            ("valid_until", self.valid_until),
            ("next_check_time", self.next_check_time),
        ):
            if value is not None and value.clock_id != self.decided_time.clock_id:
                raise ValueError(f"{field} must use the decision clock")
        if self.status == AuthorizationStatus.PENDING:
            if self.next_check_time is None:
                raise ValueError("pending decision requires next_check_time")
            if self.next_check_time.seconds <= self.available_time.seconds:
                raise ValueError("pending next_check_time must follow decision availability")
        elif self.next_check_time is not None:
            raise ValueError("terminal authorization cannot declare next_check_time")
        if self.status != AuthorizationStatus.AUTHORIZED and self.valid_until is not None:
            raise ValueError("only authorized decisions may declare valid_until")
        if (
            self.valid_until is not None
            and self.valid_until.seconds < self.available_time.seconds
        ):
            raise ValueError("authorization validity cannot end before decision availability")
        object.__setattr__(self, "evidence", freeze_json(self.evidence or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "decision_id": self.decision_id,
            "authorization_request_id": self.authorization_request_id,
            "action_request_id": self.action_request_id,
            "permission_id": self.permission_id,
            "provider_id": self.provider_id,
            "status": self.status.value,
            "decided_time": self.decided_time.to_payload(),
            "available_time": self.available_time.to_payload(),
            "valid_until": None if self.valid_until is None else self.valid_until.to_payload(),
            "next_check_time": None
            if self.next_check_time is None
            else self.next_check_time.to_payload(),
            "reason": self.reason,
            "evidence": thaw_json(self.evidence),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AuthorizationDecision":
        return cls(
            schema=str(payload.get("schema", AUTHORIZATION_DECISION_SCHEMA)),
            decision_id=str(payload["decision_id"]),
            authorization_request_id=str(payload["authorization_request_id"]),
            action_request_id=str(payload["action_request_id"]),
            permission_id=str(payload["permission_id"]),
            provider_id=str(payload["provider_id"]),
            status=AuthorizationStatus(str(payload["status"])),
            decided_time=TimePoint.from_payload(payload["decided_time"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            valid_until=None
            if payload.get("valid_until") is None
            else TimePoint.from_payload(payload["valid_until"]),
            next_check_time=None
            if payload.get("next_check_time") is None
            else TimePoint.from_payload(payload["next_check_time"]),
            reason=None if payload.get("reason") is None else str(payload["reason"]),
            evidence=dict(payload.get("evidence") or {}),
        )


@dataclass(frozen=True, slots=True)
class ActionDisposition:
    disposition_id: str
    action_request_id: str
    actuator_id: str
    status: ActionDispositionStatus
    decided_time: TimePoint
    authorization_request_id: str | None = None
    authorization_decision_id: str | None = None
    command_id: str | None = None
    receipt_id: str | None = None
    reason: str | None = None
    terminal: bool = True
    schema: str = ACTION_DISPOSITION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ACTION_DISPOSITION_SCHEMA:
            raise ValueError(f"unsupported action disposition schema: {self.schema}")
        for field in ("disposition_id", "action_request_id", "actuator_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        for field in (
            "authorization_request_id",
            "authorization_decision_id",
            "command_id",
            "receipt_id",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, require_identifier(value, field))
        object.__setattr__(self, "status", ActionDispositionStatus(self.status))
        if self.status in {
            ActionDispositionStatus.PENDING,
            ActionDispositionStatus.AUTHORIZED,
            ActionDispositionStatus.SUBMITTED,
        } and self.terminal:
            raise ValueError("pending, authorized, and submitted dispositions are nonterminal")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "disposition_id": self.disposition_id,
            "action_request_id": self.action_request_id,
            "actuator_id": self.actuator_id,
            "status": self.status.value,
            "decided_time": self.decided_time.to_payload(),
            "authorization_request_id": self.authorization_request_id,
            "authorization_decision_id": self.authorization_decision_id,
            "command_id": self.command_id,
            "receipt_id": self.receipt_id,
            "reason": self.reason,
            "terminal": self.terminal,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ActionDisposition":
        return cls(
            schema=str(payload.get("schema", ACTION_DISPOSITION_SCHEMA)),
            disposition_id=str(payload["disposition_id"]),
            action_request_id=str(payload["action_request_id"]),
            actuator_id=str(payload["actuator_id"]),
            status=ActionDispositionStatus(str(payload["status"])),
            decided_time=TimePoint.from_payload(payload["decided_time"]),
            authorization_request_id=None
            if payload.get("authorization_request_id") is None
            else str(payload["authorization_request_id"]),
            authorization_decision_id=None
            if payload.get("authorization_decision_id") is None
            else str(payload["authorization_decision_id"]),
            command_id=None
            if payload.get("command_id") is None
            else str(payload["command_id"]),
            receipt_id=None
            if payload.get("receipt_id") is None
            else str(payload["receipt_id"]),
            reason=None if payload.get("reason") is None else str(payload["reason"]),
            terminal=bool(payload.get("terminal", True)),
        )


@dataclass(frozen=True, slots=True)
class ActionCancellation:
    cancellation_id: str
    action_request_id: str
    actuator_id: str
    cancelled_time: TimePoint
    reason: str
    authorization_request_id: str | None = None
    authorization_decision_id: str | None = None
    schema: str = ACTION_CANCELLATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ACTION_CANCELLATION_SCHEMA:
            raise ValueError(f"unsupported action cancellation schema: {self.schema}")
        for field in ("cancellation_id", "action_request_id", "actuator_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        for field in ("authorization_request_id", "authorization_decision_id"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, require_identifier(value, field))
        if not self.reason.strip():
            raise ValueError("action cancellation reason cannot be empty")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "cancellation_id": self.cancellation_id,
            "action_request_id": self.action_request_id,
            "actuator_id": self.actuator_id,
            "cancelled_time": self.cancelled_time.to_payload(),
            "reason": self.reason,
            "authorization_request_id": self.authorization_request_id,
            "authorization_decision_id": self.authorization_decision_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ActionCancellation":
        return cls(
            schema=str(payload.get("schema", ACTION_CANCELLATION_SCHEMA)),
            cancellation_id=str(payload["cancellation_id"]),
            action_request_id=str(payload["action_request_id"]),
            actuator_id=str(payload["actuator_id"]),
            cancelled_time=TimePoint.from_payload(payload["cancelled_time"]),
            reason=str(payload["reason"]),
            authorization_request_id=None
            if payload.get("authorization_request_id") is None
            else str(payload["authorization_request_id"]),
            authorization_decision_id=None
            if payload.get("authorization_decision_id") is None
            else str(payload["authorization_decision_id"]),
        )
