"""Canonical calibration artifacts admitted as initial model state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.recording.artifacts import ArtifactReference
from eegle.streams.clocks import TimePoint


CALIBRATION_ARTIFACT_SCHEMA = "eegle.calibration_artifact.v1"


@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    calibration_id: str
    algorithm_id: str
    model_id: str
    state_reference: ArtifactReference
    support_input_ids: tuple[str, ...]
    support_outcome_ids: tuple[str, ...]
    produced_time: TimePoint
    provenance: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = CALIBRATION_ARTIFACT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CALIBRATION_ARTIFACT_SCHEMA:
            raise ValueError(f"unsupported calibration artifact schema: {self.schema}")
        for field in ("calibration_id", "algorithm_id", "model_id"):
            object.__setattr__(
                self, field, require_identifier(getattr(self, field), field)
            )
        if not isinstance(self.state_reference, ArtifactReference):
            raise TypeError("calibration state must be a generic artifact reference")
        inputs = tuple(
            require_identifier(value, "support_input_id")
            for value in self.support_input_ids
        )
        outcomes = tuple(
            require_identifier(value, "support_outcome_id")
            for value in self.support_outcome_ids
        )
        if not inputs or not outcomes:
            raise ValueError("calibration artifact requires support inputs and outcomes")
        if len(inputs) != len(set(inputs)) or len(outcomes) != len(set(outcomes)):
            raise ValueError("calibration support identities must be unique")
        object.__setattr__(self, "support_input_ids", inputs)
        object.__setattr__(self, "support_outcome_ids", outcomes)
        object.__setattr__(self, "provenance", freeze_json(self.provenance or {}))

    @property
    def calibration_digest(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "calibration_id": self.calibration_id,
            "algorithm_id": self.algorithm_id,
            "model_id": self.model_id,
            "state_reference": self.state_reference.to_payload(),
            "support_input_ids": list(self.support_input_ids),
            "support_outcome_ids": list(self.support_outcome_ids),
            "produced_time": self.produced_time.to_payload(),
            "provenance": thaw_json(self.provenance),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["calibration_digest"] = self.calibration_digest
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CalibrationArtifact":
        value = cls(
            schema=str(payload["schema"]),
            calibration_id=str(payload["calibration_id"]),
            algorithm_id=str(payload["algorithm_id"]),
            model_id=str(payload["model_id"]),
            state_reference=ArtifactReference.from_payload(payload["state_reference"]),
            support_input_ids=tuple(str(item) for item in payload["support_input_ids"]),
            support_outcome_ids=tuple(str(item) for item in payload["support_outcome_ids"]),
            produced_time=TimePoint.from_payload(payload["produced_time"]),
            provenance=dict(payload.get("provenance") or {}),
        )
        if payload.get("calibration_digest") != value.calibration_digest:
            raise ValueError("calibration artifact digest mismatch")
        return value
