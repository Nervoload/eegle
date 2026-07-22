"""Typed generic window specifications and produced windows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._validation import require_finite, require_identifier
from eegle.streams.clocks import TimePoint


@dataclass(frozen=True, slots=True)
class ContinuousWindowSpec:
    duration_seconds: float
    step_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "duration_seconds", require_finite(self.duration_seconds, "duration_seconds")
        )
        object.__setattr__(self, "step_seconds", require_finite(self.step_seconds, "step_seconds"))
        if self.duration_seconds <= 0 or self.step_seconds <= 0:
            raise ValueError("continuous window duration and step must be positive")


@dataclass(frozen=True, slots=True)
class EventWindowSpec:
    start_offset_seconds: float
    end_offset_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "start_offset_seconds",
            require_finite(self.start_offset_seconds, "start_offset_seconds"),
        )
        object.__setattr__(
            self,
            "end_offset_seconds",
            require_finite(self.end_offset_seconds, "end_offset_seconds"),
        )
        if self.end_offset_seconds <= self.start_offset_seconds:
            raise ValueError("event window end must follow its start")


@dataclass(frozen=True, slots=True)
class Window:
    window_id: str
    stream_id: str
    start_time: TimePoint
    end_time: TimePoint
    input_ids: tuple[str, ...]
    trigger_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "window_id", require_identifier(self.window_id, "window_id"))
        object.__setattr__(self, "stream_id", require_identifier(self.stream_id, "stream_id"))
        if self.start_time.clock_id != self.end_time.clock_id:
            raise ValueError("window start and end must share a clock")
        if self.end_time.seconds <= self.start_time.seconds:
            raise ValueError("window end must follow its start")
        inputs = tuple(require_identifier(value, "input_id") for value in self.input_ids)
        if not inputs:
            raise ValueError("window must reference at least one input")
        object.__setattr__(self, "input_ids", inputs)
        if self.trigger_id is not None:
            object.__setattr__(self, "trigger_id", require_identifier(self.trigger_id, "trigger_id"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.window.v1",
            "window_id": self.window_id,
            "stream_id": self.stream_id,
            "start_time": self.start_time.to_payload(),
            "end_time": self.end_time.to_payload(),
            "input_ids": list(self.input_ids),
            "trigger_id": self.trigger_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Window":
        if payload.get("schema") != "eegle.window.v1":
            raise ValueError(f"unsupported window schema: {payload.get('schema')}")
        return cls(
            window_id=str(payload["window_id"]),
            stream_id=str(payload["stream_id"]),
            start_time=TimePoint.from_payload(payload["start_time"]),
            end_time=TimePoint.from_payload(payload["end_time"]),
            input_ids=tuple(str(value) for value in payload["input_ids"]),
            trigger_id=None if payload.get("trigger_id") is None else str(payload["trigger_id"]),
        )
