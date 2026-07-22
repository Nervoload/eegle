"""Requested actions, separate from authorization and observed delivery."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.streams.clocks import TimePoint


ACTION_COMMAND_SCHEMA = "eegle.action_command.v1"


@dataclass(frozen=True, slots=True)
class ActionCommand:
    command_id: str
    capability: str
    requested_by: str
    parameters: Mapping[str, Any]
    requested_time: TimePoint
    available_time: TimePoint
    intended_delivery_time: TimePoint | None = None
    expires_at: TimePoint | None = None
    prediction_id: str | None = None
    policy_state_hash: str | None = None
    schema: str = ACTION_COMMAND_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ACTION_COMMAND_SCHEMA:
            raise ValueError(f"unsupported action command schema: {self.schema}")
        object.__setattr__(self, "command_id", require_identifier(self.command_id, "command_id"))
        object.__setattr__(self, "capability", require_identifier(self.capability, "capability"))
        object.__setattr__(self, "requested_by", require_identifier(self.requested_by, "requested_by"))
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
        if self.requested_time.clock_id != self.available_time.clock_id:
            raise ValueError("requested_time and available_time must share a clock")
        if self.available_time.seconds < self.requested_time.seconds:
            raise ValueError("action available_time cannot precede requested_time")
        if self.expires_at is not None:
            if self.expires_at.clock_id != self.requested_time.clock_id:
                raise ValueError("expires_at must use the command boundary clock")
            if self.expires_at.seconds < self.requested_time.seconds:
                raise ValueError("expires_at cannot precede requested_time")
        object.__setattr__(self, "parameters", freeze_json(self.parameters))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "command_id": self.command_id,
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
    def from_payload(cls, payload: Mapping[str, Any]) -> "ActionCommand":
        intended = payload.get("intended_delivery_time")
        expires = payload.get("expires_at")
        return cls(
            schema=str(payload.get("schema", ACTION_COMMAND_SCHEMA)),
            command_id=str(payload["command_id"]),
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
