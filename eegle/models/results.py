"""Framework-neutral values returned by executable model plugins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._validation import freeze_json, require_finite, thaw_json
from eegle.compiler.lock import canonical_hash


MODEL_RESULT_SCHEMA = "eegle.model_result.v1"


@dataclass(frozen=True, slots=True)
class ModelResult:
    """Untrusted, contract-bound output returned by a model implementation.

    A result deliberately has no prediction identity, model identity, role,
    artifact reference, lineage, state reference, or absolute timestamp.  The
    plan-owned runtime adds those authorities only after validating the value
    against the compiled model binding.
    """

    value: Any
    uncertainty: Any | None = None
    validity: Any | None = None
    abstained: bool = False
    abstention_reason: str | None = None
    completion_delay_seconds: float = 0.0
    schema: str = MODEL_RESULT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEL_RESULT_SCHEMA:
            raise ValueError(f"unsupported model result schema: {self.schema}")
        object.__setattr__(self, "value", freeze_json(self.value))
        if self.uncertainty is not None:
            object.__setattr__(self, "uncertainty", freeze_json(self.uncertainty))
        if self.validity is not None:
            object.__setattr__(self, "validity", freeze_json(self.validity))
        delay = require_finite(
            self.completion_delay_seconds,
            "completion_delay_seconds",
        )
        if delay < 0:
            raise ValueError("completion_delay_seconds cannot be negative")
        object.__setattr__(self, "completion_delay_seconds", delay)
        if self.abstained:
            if self.abstention_reason is None or not self.abstention_reason.strip():
                raise ValueError("an abstained model result requires abstention_reason")
        elif self.abstention_reason is not None:
            raise ValueError("a non-abstained model result cannot have abstention_reason")

    @property
    def result_hash(self) -> str:
        return canonical_hash(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "value": thaw_json(self.value),
            "uncertainty": None
            if self.uncertainty is None
            else thaw_json(self.uncertainty),
            "validity": None if self.validity is None else thaw_json(self.validity),
            "abstained": self.abstained,
            "abstention_reason": self.abstention_reason,
            "completion_delay_seconds": self.completion_delay_seconds,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelResult":
        return cls(
            schema=str(payload.get("schema", MODEL_RESULT_SCHEMA)),
            value=payload.get("value"),
            uncertainty=payload.get("uncertainty"),
            validity=payload.get("validity"),
            abstained=bool(payload.get("abstained", False)),
            abstention_reason=None
            if payload.get("abstention_reason") is None
            else str(payload["abstention_reason"]),
            completion_delay_seconds=float(
                payload.get("completion_delay_seconds", 0.0)
            ),
        )
