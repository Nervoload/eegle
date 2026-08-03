"""Machine-readable performance budgets and fault-qualification expectations."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from importlib.resources import files
from typing import Any

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash

QUALIFICATION_PROFILE_SCHEMA_ID = "eegle.performance_fault_qualification.v1"
_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$"


class BudgetDirection(str, Enum):
    MINIMUM = "minimum"
    MAXIMUM = "maximum"


class FaultOutcome(str, Enum):
    RECOVERABLE_PREFIX = "recoverable_prefix"
    UNRECOVERABLE = "unrecoverable"
    TIMED_OUT = "timed_out"
    WARNING = "warning"
    REJECTED = "rejected"


QUALIFICATION_PROFILE_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": QUALIFICATION_PROFILE_SCHEMA_ID,
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "snapshot_date",
            "performance_budgets",
            "fault_scenarios",
            "profile_hash",
        ],
        "properties": {
            "schema": {"const": QUALIFICATION_PROFILE_SCHEMA_ID},
            "snapshot_date": {"type": "string", "format": "date"},
            "profile_hash": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
            "performance_budgets": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "budget_id",
                        "metric",
                        "unit",
                        "direction",
                        "threshold",
                        "workload",
                        "evidence",
                        "limitations",
                    ],
                    "properties": {
                        "budget_id": {"type": "string", "pattern": _IDENTIFIER},
                        "metric": {"type": "string", "pattern": _IDENTIFIER},
                        "unit": {"type": "string", "minLength": 1},
                        "direction": {
                            "enum": [value.value for value in BudgetDirection]
                        },
                        "threshold": {"type": "number"},
                        "workload": {"type": "object"},
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                        },
                        "limitations": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
            "fault_scenarios": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "scenario_id",
                        "expected_outcome",
                        "expected_codes",
                        "evidence",
                        "limitations",
                    ],
                    "properties": {
                        "scenario_id": {"type": "string", "pattern": _IDENTIFIER},
                        "expected_outcome": {
                            "enum": [value.value for value in FaultOutcome]
                        },
                        "expected_codes": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string", "pattern": _IDENTIFIER},
                        },
                        "evidence": {
                            "type": "array",
                            "minItems": 1,
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                        },
                        "limitations": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string", "minLength": 1},
                        },
                    },
                },
            },
        },
    }
)


def _text_values(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(str(value).strip() for value in values)
    if any(not value for value in normalized):
        raise ValueError(f"{field_name} cannot contain empty text")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must be unique")
    return normalized


@dataclass(frozen=True, slots=True)
class PerformanceBudget:
    budget_id: str
    metric: str
    unit: str
    direction: BudgetDirection
    threshold: float
    workload: Mapping[str, Any]
    evidence: tuple[str, ...]
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "budget_id", require_identifier(self.budget_id, "budget_id"))
        object.__setattr__(self, "metric", require_identifier(self.metric, "metric"))
        unit = str(self.unit).strip()
        if not unit:
            raise ValueError("performance budget unit cannot be empty")
        object.__setattr__(self, "unit", unit)
        object.__setattr__(self, "direction", BudgetDirection(self.direction))
        threshold = float(self.threshold)
        if not math.isfinite(threshold):
            raise ValueError("performance budget threshold must be finite")
        object.__setattr__(self, "threshold", threshold)
        object.__setattr__(self, "workload", freeze_json(self.workload))
        evidence = _text_values(tuple(self.evidence), "performance budget evidence")
        if not evidence:
            raise ValueError("performance budget evidence cannot be empty")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(
            self,
            "limitations",
            _text_values(tuple(self.limitations), "performance budget limitations"),
        )

    def accepts(self, observed: float) -> bool:
        value = float(observed)
        if not math.isfinite(value):
            return False
        if self.direction == BudgetDirection.MINIMUM:
            return value >= self.threshold
        return value <= self.threshold

    def to_payload(self) -> dict[str, Any]:
        return {
            "budget_id": self.budget_id,
            "metric": self.metric,
            "unit": self.unit,
            "direction": self.direction.value,
            "threshold": self.threshold,
            "workload": thaw_json(self.workload),
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> PerformanceBudget:
        return cls(
            budget_id=str(payload["budget_id"]),
            metric=str(payload["metric"]),
            unit=str(payload["unit"]),
            direction=BudgetDirection(str(payload["direction"])),
            threshold=float(payload["threshold"]),
            workload=payload["workload"],
            evidence=tuple(str(value) for value in payload["evidence"]),
            limitations=tuple(str(value) for value in payload["limitations"]),
        )


@dataclass(frozen=True, slots=True)
class FaultScenario:
    scenario_id: str
    expected_outcome: FaultOutcome
    expected_codes: tuple[str, ...]
    evidence: tuple[str, ...]
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scenario_id",
            require_identifier(self.scenario_id, "scenario_id"),
        )
        object.__setattr__(self, "expected_outcome", FaultOutcome(self.expected_outcome))
        codes = tuple(
            require_identifier(value, "expected_code") for value in self.expected_codes
        )
        if not codes or len(codes) != len(set(codes)):
            raise ValueError("fault expected codes must be non-empty and unique")
        object.__setattr__(self, "expected_codes", codes)
        evidence = _text_values(tuple(self.evidence), "fault evidence")
        if not evidence:
            raise ValueError("fault evidence cannot be empty")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(
            self,
            "limitations",
            _text_values(tuple(self.limitations), "fault limitations"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "expected_outcome": self.expected_outcome.value,
            "expected_codes": list(self.expected_codes),
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> FaultScenario:
        return cls(
            scenario_id=str(payload["scenario_id"]),
            expected_outcome=FaultOutcome(str(payload["expected_outcome"])),
            expected_codes=tuple(str(value) for value in payload["expected_codes"]),
            evidence=tuple(str(value) for value in payload["evidence"]),
            limitations=tuple(str(value) for value in payload["limitations"]),
        )


@dataclass(frozen=True, slots=True)
class QualificationProfile:
    snapshot_date: str
    performance_budgets: tuple[PerformanceBudget, ...]
    fault_scenarios: tuple[FaultScenario, ...]
    schema: str = QUALIFICATION_PROFILE_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != QUALIFICATION_PROFILE_SCHEMA_ID:
            raise ValueError(f"unsupported qualification schema: {self.schema}")
        try:
            date.fromisoformat(self.snapshot_date)
        except ValueError as exc:
            raise ValueError("qualification snapshot_date must be ISO-8601") from exc
        budgets = tuple(self.performance_budgets)
        faults = tuple(self.fault_scenarios)
        budget_ids = tuple(value.budget_id for value in budgets)
        fault_ids = tuple(value.scenario_id for value in faults)
        if not budgets or budget_ids != tuple(sorted(budget_ids)):
            raise ValueError("performance budgets must be non-empty and sorted")
        if not faults or fault_ids != tuple(sorted(fault_ids)):
            raise ValueError("fault scenarios must be non-empty and sorted")
        if len(budget_ids) != len(set(budget_ids)):
            raise ValueError("performance budget identities must be unique")
        if len(fault_ids) != len(set(fault_ids)):
            raise ValueError("fault scenario identities must be unique")
        object.__setattr__(self, "performance_budgets", budgets)
        object.__setattr__(self, "fault_scenarios", faults)

    @property
    def profile_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def budget(self, budget_id: str) -> PerformanceBudget:
        for value in self.performance_budgets:
            if value.budget_id == budget_id:
                return value
        raise KeyError(budget_id)

    def fault(self, scenario_id: str) -> FaultScenario:
        for value in self.fault_scenarios:
            if value.scenario_id == scenario_id:
                return value
        raise KeyError(scenario_id)

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "snapshot_date": self.snapshot_date,
            "performance_budgets": [
                value.to_payload() for value in self.performance_budgets
            ],
            "fault_scenarios": [value.to_payload() for value in self.fault_scenarios],
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self.content_payload(), "profile_hash": self.profile_hash}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> QualificationProfile:
        validate_qualification_profile_payload(payload)
        value = cls(
            snapshot_date=str(payload["snapshot_date"]),
            performance_budgets=tuple(
                PerformanceBudget.from_payload(item)
                for item in payload["performance_budgets"]
            ),
            fault_scenarios=tuple(
                FaultScenario.from_payload(item) for item in payload["fault_scenarios"]
            ),
            schema=str(payload["schema"]),
        )
        if value.profile_hash != require_digest(
            str(payload["profile_hash"]), "profile_hash"
        ):
            raise ValueError("qualification profile hash mismatch")
        return value


def validate_qualification_profile_payload(payload: Mapping[str, Any]) -> None:
    from eegle.specs.schemas import validate_payload

    validate_payload(thaw_json(freeze_json(payload)), QUALIFICATION_PROFILE_JSON_SCHEMA)


def performance_fault_qualification_profile() -> QualificationProfile:
    """Load and verify the qualification profile shipped in the artifact."""

    resource = files("eegle.validation").joinpath("qualification_profile.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("qualification profile must contain a JSON object")
    return QualificationProfile.from_payload(payload)


__all__ = [
    "QUALIFICATION_PROFILE_JSON_SCHEMA",
    "QUALIFICATION_PROFILE_SCHEMA_ID",
    "BudgetDirection",
    "FaultOutcome",
    "FaultScenario",
    "PerformanceBudget",
    "QualificationProfile",
    "performance_fault_qualification_profile",
    "validate_qualification_profile_payload",
]
