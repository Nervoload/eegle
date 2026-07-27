"""Requested actions and actuator-ready commands are separate authorities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.streams.clocks import TimePoint


ACTION_REQUEST_SCHEMA = "eegle.action_request.v1"
AUTHORIZED_COMMAND_SCHEMA = "eegle.authorized_command.v1"


def _validate_action_times(
    requested_time: TimePoint,
    available_time: TimePoint,
    intended_delivery_time: TimePoint | None,
    expires_at: TimePoint | None,
) -> None:
    if requested_time.clock_id != available_time.clock_id:
        raise ValueError("requested_time and available_time must share a clock")
    if available_time.seconds < requested_time.seconds:
        raise ValueError("action available_time cannot precede requested_time")
    for field, value in (
        ("intended_delivery_time", intended_delivery_time),
        ("expires_at", expires_at),
    ):
        if value is not None and value.clock_id != requested_time.clock_id:
            raise ValueError(f"{field} must use the action boundary clock")
    if (
        intended_delivery_time is not None
        and intended_delivery_time.seconds < requested_time.seconds
    ):
        raise ValueError("intended_delivery_time cannot precede requested_time")
    if expires_at is not None and expires_at.seconds < available_time.seconds:
        raise ValueError("expires_at cannot precede action availability")


@dataclass(frozen=True, slots=True)
class ActionRequest:
    """Policy intent with no authority to reach an actuator."""

    request_id: str
    capability: str
    requested_by: str
    parameters: Mapping[str, Any]
    requested_time: TimePoint
    available_time: TimePoint
    intended_delivery_time: TimePoint | None = None
    expires_at: TimePoint | None = None
    prediction_id: str | None = None
    policy_state_hash: str | None = None
    schema: str = ACTION_REQUEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ACTION_REQUEST_SCHEMA:
            raise ValueError(f"unsupported action request schema: {self.schema}")
        for field in ("request_id", "capability", "requested_by"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        if self.prediction_id is not None:
            object.__setattr__(
                self, "prediction_id", require_identifier(self.prediction_id, "prediction_id")
            )
        if self.policy_state_hash is not None:
            object.__setattr__(
                self,
                "policy_state_hash",
                require_digest(self.policy_state_hash, "policy_state_hash"),
            )
        _validate_action_times(
            self.requested_time,
            self.available_time,
            self.intended_delivery_time,
            self.expires_at,
        )
        object.__setattr__(self, "parameters", freeze_json(self.parameters))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "request_id": self.request_id,
            "capability": self.capability,
            "requested_by": self.requested_by,
            "parameters": thaw_json(self.parameters),
            "requested_time": self.requested_time.to_payload(),
            "available_time": self.available_time.to_payload(),
            "intended_delivery_time": None
            if self.intended_delivery_time is None
            else self.intended_delivery_time.to_payload(),
            "expires_at": None if self.expires_at is None else self.expires_at.to_payload(),
            "prediction_id": self.prediction_id,
            "policy_state_hash": self.policy_state_hash,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ActionRequest":
        intended = payload.get("intended_delivery_time")
        expires = payload.get("expires_at")
        return cls(
            schema=str(payload.get("schema", ACTION_REQUEST_SCHEMA)),
            request_id=str(payload["request_id"]),
            capability=str(payload["capability"]),
            requested_by=str(payload["requested_by"]),
            parameters=dict(payload.get("parameters") or {}),
            requested_time=TimePoint.from_payload(payload["requested_time"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            intended_delivery_time=None
            if intended is None
            else TimePoint.from_payload(intended),
            expires_at=None if expires is None else TimePoint.from_payload(expires),
            prediction_id=None
            if payload.get("prediction_id") is None
            else str(payload["prediction_id"]),
            policy_state_hash=None
            if payload.get("policy_state_hash") is None
            else str(payload["policy_state_hash"]),
        )


@dataclass(frozen=True, slots=True)
class AuthorizedCommand:
    """Plan-owned command envelope accepted by actuator adapters."""

    command_id: str
    request_id: str
    authorization_request_id: str
    authorization_decision_id: str
    permission_id: str
    provider_id: str
    actuator_id: str
    capability: str
    parameters: Mapping[str, Any]
    requested_time: TimePoint
    available_time: TimePoint
    intended_delivery_time: TimePoint | None = None
    expires_at: TimePoint | None = None
    prediction_id: str | None = None
    policy_state_hash: str | None = None
    schema: str = AUTHORIZED_COMMAND_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != AUTHORIZED_COMMAND_SCHEMA:
            raise ValueError(f"unsupported authorized command schema: {self.schema}")
        for field in (
            "command_id",
            "request_id",
            "authorization_request_id",
            "authorization_decision_id",
            "permission_id",
            "provider_id",
            "actuator_id",
            "capability",
        ):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        if self.prediction_id is not None:
            object.__setattr__(
                self, "prediction_id", require_identifier(self.prediction_id, "prediction_id")
            )
        if self.policy_state_hash is not None:
            object.__setattr__(
                self,
                "policy_state_hash",
                require_digest(self.policy_state_hash, "policy_state_hash"),
            )
        _validate_action_times(
            self.requested_time,
            self.available_time,
            self.intended_delivery_time,
            self.expires_at,
        )
        object.__setattr__(self, "parameters", freeze_json(self.parameters))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "command_id": self.command_id,
            "request_id": self.request_id,
            "authorization_request_id": self.authorization_request_id,
            "authorization_decision_id": self.authorization_decision_id,
            "permission_id": self.permission_id,
            "provider_id": self.provider_id,
            "actuator_id": self.actuator_id,
            "capability": self.capability,
            "parameters": thaw_json(self.parameters),
            "requested_time": self.requested_time.to_payload(),
            "available_time": self.available_time.to_payload(),
            "intended_delivery_time": None
            if self.intended_delivery_time is None
            else self.intended_delivery_time.to_payload(),
            "expires_at": None if self.expires_at is None else self.expires_at.to_payload(),
            "prediction_id": self.prediction_id,
            "policy_state_hash": self.policy_state_hash,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AuthorizedCommand":
        intended = payload.get("intended_delivery_time")
        expires = payload.get("expires_at")
        return cls(
            schema=str(payload.get("schema", AUTHORIZED_COMMAND_SCHEMA)),
            command_id=str(payload["command_id"]),
            request_id=str(payload["request_id"]),
            authorization_request_id=str(payload["authorization_request_id"]),
            authorization_decision_id=str(payload["authorization_decision_id"]),
            permission_id=str(payload["permission_id"]),
            provider_id=str(payload["provider_id"]),
            actuator_id=str(payload["actuator_id"]),
            capability=str(payload["capability"]),
            parameters=dict(payload.get("parameters") or {}),
            requested_time=TimePoint.from_payload(payload["requested_time"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            intended_delivery_time=None
            if intended is None
            else TimePoint.from_payload(intended),
            expires_at=None if expires is None else TimePoint.from_payload(expires),
            prediction_id=None
            if payload.get("prediction_id") is None
            else str(payload["prediction_id"]),
            policy_state_hash=None
            if payload.get("policy_state_hash") is None
            else str(payload["policy_state_hash"]),
        )
