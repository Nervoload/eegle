"""Portable scientific protocol specifications.

Protocols describe what a run is allowed to claim.  They deliberately contain
no device selectors, filesystem locations, credentials, or executable Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from typing import Any, Mapping

from eegle._domain import ExecutionMode
from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.specs.schemas import validate_payload


PROTOCOL_SPEC_SCHEMA = "eegle.protocol_spec.v1"
_JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


class ComparisonOperator(str, Enum):
    LESS_THAN = "lt"
    LESS_THAN_OR_EQUAL = "lte"
    GREATER_THAN = "gt"
    GREATER_THAN_OR_EQUAL = "gte"
    EQUAL = "eq"


@dataclass(frozen=True, slots=True)
class ClaimSpec:
    claim_id: str
    statement: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", require_identifier(self.claim_id, "claim_id"))
        if not self.statement.strip():
            raise ValueError("claim statement cannot be empty")

    def to_payload(self) -> dict[str, Any]:
        return {"claim_id": self.claim_id, "statement": self.statement}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ClaimSpec":
        return cls(claim_id=str(payload["claim_id"]), statement=str(payload["statement"]))


@dataclass(frozen=True, slots=True)
class MetricSpec:
    metric_id: str
    measure: str
    parameters: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric_id", require_identifier(self.metric_id, "metric_id"))
        object.__setattr__(self, "measure", require_identifier(self.measure, "metric measure"))
        object.__setattr__(self, "parameters", freeze_json(self.parameters or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "measure": self.measure,
            "parameters": thaw_json(self.parameters),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "MetricSpec":
        return cls(
            metric_id=str(payload["metric_id"]),
            measure=str(payload["measure"]),
            parameters=dict(payload.get("parameters") or {}),
        )


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    criterion_id: str
    metric_id: str
    operator: ComparisonOperator
    value: float | int | bool | str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "criterion_id", require_identifier(self.criterion_id, "criterion_id")
        )
        object.__setattr__(self, "metric_id", require_identifier(self.metric_id, "metric_id"))
        object.__setattr__(self, "operator", ComparisonOperator(self.operator))
        freeze_json(self.value)

    def to_payload(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "metric_id": self.metric_id,
            "operator": self.operator.value,
            "value": self.value,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AcceptanceCriterion":
        return cls(
            criterion_id=str(payload["criterion_id"]),
            metric_id=str(payload["metric_id"]),
            operator=ComparisonOperator(str(payload["operator"])),
            value=payload["value"],
        )


@dataclass(frozen=True, slots=True)
class ProtocolSpec:
    protocol_id: str
    execution_mode: ExecutionMode
    claims: tuple[ClaimSpec, ...]
    metrics: tuple[MetricSpec, ...] = ()
    acceptance: tuple[AcceptanceCriterion, ...] = ()
    annotations: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = PROTOCOL_SPEC_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PROTOCOL_SPEC_SCHEMA:
            raise ValueError(f"unsupported protocol schema: {self.schema}")
        object.__setattr__(
            self, "protocol_id", require_identifier(self.protocol_id, "protocol_id")
        )
        object.__setattr__(self, "execution_mode", ExecutionMode(self.execution_mode))
        _require_unique((value.claim_id for value in self.claims), "claim")
        _require_unique((value.metric_id for value in self.metrics), "metric")
        _require_unique((value.criterion_id for value in self.acceptance), "criterion")
        metric_ids = {value.metric_id for value in self.metrics}
        for criterion in self.acceptance:
            if criterion.metric_id not in metric_ids:
                raise ValueError(
                    f"criterion {criterion.criterion_id} references unknown metric "
                    f"{criterion.metric_id}"
                )
        object.__setattr__(self, "annotations", freeze_json(self.annotations or {}))

    @property
    def spec_hash(self) -> str:
        return canonical_hash(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "protocol_id": self.protocol_id,
            "execution_mode": self.execution_mode.value,
            "claims": [value.to_payload() for value in self.claims],
            "metrics": [value.to_payload() for value in self.metrics],
            "acceptance": [value.to_payload() for value in self.acceptance],
            "annotations": thaw_json(self.annotations),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProtocolSpec":
        validate_payload(payload, PROTOCOL_JSON_SCHEMA)
        return cls(
            schema=str(payload["schema"]),
            protocol_id=str(payload["protocol_id"]),
            execution_mode=ExecutionMode(str(payload["execution_mode"])),
            claims=tuple(ClaimSpec.from_payload(value) for value in payload["claims"]),
            metrics=tuple(MetricSpec.from_payload(value) for value in payload.get("metrics", ())),
            acceptance=tuple(
                AcceptanceCriterion.from_payload(value)
                for value in payload.get("acceptance", ())
            ),
            annotations=dict(payload.get("annotations") or {}),
        )

    @classmethod
    def load(cls, path: str | Path) -> "ProtocolSpec":
        return cls.from_payload(_load_json_object(path))


def _require_unique(values: Any, label: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{label} identities must be unique")


def _load_json_object(path: str | Path) -> Mapping[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must contain a JSON object")
    return value


PROTOCOL_JSON_SCHEMA: Mapping[str, Any] = {
    "$schema": _JSON_SCHEMA,
    "type": "object",
    "required": ["schema", "protocol_id", "execution_mode", "claims"],
    "properties": {
        "schema": {"const": PROTOCOL_SPEC_SCHEMA},
        "protocol_id": {"type": "string", "minLength": 1},
        "execution_mode": {"enum": [mode.value for mode in ExecutionMode]},
        "claims": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["claim_id", "statement"],
                "properties": {
                    "claim_id": {"type": "string", "minLength": 1},
                    "statement": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
        },
        "metrics": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["metric_id", "measure"],
                "properties": {
                    "metric_id": {"type": "string", "minLength": 1},
                    "measure": {"type": "string", "minLength": 1},
                    "parameters": {"type": "object"},
                },
                "additionalProperties": False,
            },
        },
        "acceptance": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["criterion_id", "metric_id", "operator", "value"],
                "properties": {
                    "criterion_id": {"type": "string", "minLength": 1},
                    "metric_id": {"type": "string", "minLength": 1},
                    "operator": {"enum": [value.value for value in ComparisonOperator]},
                    "value": {"type": ["number", "integer", "boolean", "string"]},
                },
                "additionalProperties": False,
            },
        },
        "annotations": {"type": "object"},
    },
    "additionalProperties": False,
}
