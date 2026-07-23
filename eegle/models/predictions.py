"""Framework-neutral model prediction records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._domain import Lineage
from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.streams.clocks import TimePoint


PREDICTION_RECORD_SCHEMA = "eegle.prediction.v1"


@dataclass(frozen=True, slots=True)
class Prediction:
    prediction_id: str
    model_id: str
    role: str
    outputs: Mapping[str, Any]
    produced_time: TimePoint
    available_time: TimePoint
    input_ids: tuple[str, ...]
    lineage: Lineage
    confidence: float | None = None
    schema: str = PREDICTION_RECORD_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PREDICTION_RECORD_SCHEMA:
            raise ValueError(f"unsupported prediction schema: {self.schema}")
        for field in ("prediction_id", "model_id", "role"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        inputs = tuple(require_identifier(value, "input_id") for value in self.input_ids)
        if not inputs:
            raise ValueError("prediction must reference at least one admitted input")
        object.__setattr__(self, "input_ids", inputs)
        if self.lineage.input_ids != inputs:
            raise ValueError("prediction input_ids must exactly match lineage input_ids")
        latest_input = self.lineage.latest_input_available_time
        if latest_input is None:
            raise ValueError("prediction lineage requires latest input availability")
        if self.produced_time.clock_id != self.available_time.clock_id:
            raise ValueError("prediction produced_time and available_time must share a clock")
        if self.available_time.seconds < self.produced_time.seconds:
            raise ValueError("prediction available_time cannot precede produced_time")
        if latest_input.clock_id != self.produced_time.clock_id:
            raise ValueError("prediction input availability must use the execution clock")
        if self.produced_time.seconds < latest_input.seconds:
            raise ValueError("prediction cannot be produced before its latest input was available")
        if self.confidence is not None and not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("prediction confidence must be in [0, 1]")
        if self.confidence is not None:
            object.__setattr__(self, "confidence", float(self.confidence))
        object.__setattr__(self, "outputs", freeze_json(self.outputs))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "prediction_id": self.prediction_id,
            "model_id": self.model_id,
            "role": self.role,
            "outputs": thaw_json(self.outputs),
            "produced_time": self.produced_time.to_payload(),
            "available_time": self.available_time.to_payload(),
            "input_ids": list(self.input_ids),
            "lineage": self.lineage.to_payload(),
            "confidence": self.confidence,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Prediction":
        return cls(
            schema=str(payload.get("schema", PREDICTION_RECORD_SCHEMA)),
            prediction_id=str(payload["prediction_id"]),
            model_id=str(payload["model_id"]),
            role=str(payload["role"]),
            outputs=dict(payload["outputs"]),
            produced_time=TimePoint.from_payload(payload["produced_time"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            input_ids=tuple(str(value) for value in payload["input_ids"]),
            lineage=Lineage.from_payload(payload["lineage"]),
            confidence=None
            if payload.get("confidence") is None
            else float(payload["confidence"]),
        )
