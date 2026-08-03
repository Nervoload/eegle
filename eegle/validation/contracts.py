"""Versioned, layer-neutral validation result contracts.

Validation reports describe what the available evidence supports.  They do not
replace schema, compiler, runtime, recording, or replay authorities; adapters
project those owners' results into this common vocabulary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash

VALIDATION_OBSERVATION_SCHEMA_ID = "eegle.validation_observation.v1"
VALIDATION_RESULT_SCHEMA_ID = "eegle.validation_result.v1"
VALIDATION_REPORT_SCHEMA_ID = "eegle.validation_report.v1"
EVIDENCE_REFERENCE_SCHEMA_ID = "eegle.validation_evidence_reference.v1"
_JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$"


class ValidationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    NOT_APPLICABLE = "not_applicable"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ValidationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ValidationLayer(str, Enum):
    DEFINITION = "definition"
    COMPATIBILITY = "compatibility"
    INTEGRITY = "integrity"
    CLOCKS_TIMING = "clocks_timing"
    CAUSALITY = "causality"
    QUALITY_COVERAGE = "quality_coverage"
    EXECUTION = "execution"
    MODEL = "model"
    ADAPTATION = "adaptation"
    ACTIONS = "actions"
    REPLAY = "replay"
    PROTOCOL = "protocol"


def _evidence_reference_shape() -> dict[str, Any]:
    nullable_identifier = {
        "oneOf": [
            {"type": "null"},
            {"type": "string", "pattern": _IDENTIFIER},
        ]
    }
    return {
        "type": "object",
        "required": [
            "schema",
            "subject_id",
            "bundle_id",
            "artifact_id",
            "record_id",
            "sequence",
            "path",
        ],
        "properties": {
            "schema": {"const": EVIDENCE_REFERENCE_SCHEMA_ID},
            "subject_id": {"type": "string", "pattern": _IDENTIFIER},
            "bundle_id": nullable_identifier,
            "artifact_id": nullable_identifier,
            "record_id": nullable_identifier,
            "sequence": {
                "oneOf": [{"type": "null"}, {"type": "integer", "minimum": 0}]
            },
            "path": {
                "oneOf": [
                    {"type": "null"},
                    {"type": "string", "pattern": r"^\$"},
                ]
            },
        },
        "additionalProperties": False,
    }


def _observation_shape() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "schema",
            "observation_id",
            "status",
            "value",
            "evidence_count",
            "reason",
            "evidence",
        ],
        "properties": {
            "schema": {"const": VALIDATION_OBSERVATION_SCHEMA_ID},
            "observation_id": {"type": "string", "pattern": _IDENTIFIER},
            "status": {"enum": [value.value for value in ValidationStatus]},
            "value": {},
            "evidence_count": {"type": "integer", "minimum": 0},
            "reason": {
                "oneOf": [
                    {"type": "null"},
                    {"type": "string", "minLength": 1},
                ]
            },
            "evidence": {
                "type": "array",
                "items": _evidence_reference_shape(),
            },
        },
        "additionalProperties": False,
    }


def _result_shape() -> dict[str, Any]:
    return {
        "type": "object",
        "required": [
            "schema",
            "result_id",
            "layer",
            "status",
            "severity",
            "summary",
            "observations",
            "evidence",
            "details",
        ],
        "properties": {
            "schema": {"const": VALIDATION_RESULT_SCHEMA_ID},
            "result_id": {"type": "string", "pattern": _IDENTIFIER},
            "layer": {"enum": [value.value for value in ValidationLayer]},
            "status": {"enum": [value.value for value in ValidationStatus]},
            "severity": {"enum": [value.value for value in ValidationSeverity]},
            "summary": {"type": "string", "minLength": 1},
            "observations": {"type": "array", "items": _observation_shape()},
            "evidence": {
                "type": "array",
                "items": _evidence_reference_shape(),
            },
            "details": {"type": "object"},
        },
        "additionalProperties": False,
    }


EVIDENCE_REFERENCE_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {"$schema": _JSON_SCHEMA, "title": "EEGle validation evidence reference v1", **_evidence_reference_shape()}
)
VALIDATION_OBSERVATION_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {"$schema": _JSON_SCHEMA, "title": "EEGle validation observation v1", **_observation_shape()}
)
VALIDATION_RESULT_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {"$schema": _JSON_SCHEMA, "title": "EEGle validation result v1", **_result_shape()}
)
VALIDATION_REPORT_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {
        "$schema": _JSON_SCHEMA,
        "title": "EEGle validation report v1",
        "type": "object",
        "required": [
            "schema",
            "report_id",
            "subject_id",
            "status",
            "conclusive",
            "passed",
            "results",
            "metadata",
            "report_hash",
        ],
        "properties": {
            "schema": {"const": VALIDATION_REPORT_SCHEMA_ID},
            "report_id": {"type": "string", "pattern": _IDENTIFIER},
            "subject_id": {"type": "string", "pattern": _IDENTIFIER},
            "status": {"enum": [value.value for value in ValidationStatus]},
            "conclusive": {"type": "boolean"},
            "passed": {"type": "boolean"},
            "results": {"type": "array", "items": _result_shape()},
            "metadata": {"type": "object"},
            "report_hash": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
        },
        "additionalProperties": False,
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    """A stable pointer to evidence without embedding its sensitive payload."""

    subject_id: str
    bundle_id: str | None = None
    artifact_id: str | None = None
    record_id: str | None = None
    sequence: int | None = None
    path: str | None = None
    schema: str = EVIDENCE_REFERENCE_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != EVIDENCE_REFERENCE_SCHEMA_ID:
            raise ValueError(f"unsupported evidence reference schema: {self.schema}")
        object.__setattr__(
            self, "subject_id", require_identifier(self.subject_id, "subject_id")
        )
        for field_name in ("bundle_id", "artifact_id", "record_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    require_identifier(value, field_name),
                )
        if self.sequence is not None:
            sequence = int(self.sequence)
            if sequence < 0:
                raise ValueError("evidence reference sequence cannot be negative")
            object.__setattr__(self, "sequence", sequence)
        if self.path is not None and not self.path.startswith("$"):
            raise ValueError("evidence reference path must start at '$'")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "subject_id": self.subject_id,
            "bundle_id": self.bundle_id,
            "artifact_id": self.artifact_id,
            "record_id": self.record_id,
            "sequence": self.sequence,
            "path": self.path,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> EvidenceReference:
        return cls(
            schema=str(payload["schema"]),
            subject_id=str(payload["subject_id"]),
            bundle_id=None
            if payload.get("bundle_id") is None
            else str(payload["bundle_id"]),
            artifact_id=None
            if payload.get("artifact_id") is None
            else str(payload["artifact_id"]),
            record_id=None
            if payload.get("record_id") is None
            else str(payload["record_id"]),
            sequence=None
            if payload.get("sequence") is None
            else int(payload["sequence"]),
            path=None if payload.get("path") is None else str(payload["path"]),
        )


@dataclass(frozen=True, slots=True)
class ValidationObservation:
    observation_id: str
    status: ValidationStatus
    value: Any = None
    evidence_count: int = 0
    reason: str | None = None
    evidence: tuple[EvidenceReference, ...] = ()
    schema: str = VALIDATION_OBSERVATION_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != VALIDATION_OBSERVATION_SCHEMA_ID:
            raise ValueError(f"unsupported validation observation schema: {self.schema}")
        object.__setattr__(
            self,
            "observation_id",
            require_identifier(self.observation_id, "observation_id"),
        )
        object.__setattr__(self, "status", ValidationStatus(self.status))
        count = int(self.evidence_count)
        if count < 0:
            raise ValueError("observation evidence_count cannot be negative")
        object.__setattr__(self, "evidence_count", count)
        if self.reason is not None and not self.reason.strip():
            raise ValueError("observation reason cannot be empty")
        object.__setattr__(self, "value", freeze_json(self.value))
        references = tuple(self.evidence)
        if not all(isinstance(value, EvidenceReference) for value in references):
            raise TypeError("observation evidence must contain EvidenceReference values")
        object.__setattr__(self, "evidence", references)

    @property
    def sufficient(self) -> bool:
        return self.status != ValidationStatus.INSUFFICIENT_EVIDENCE

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "observation_id": self.observation_id,
            "status": self.status.value,
            "value": thaw_json(self.value),
            "evidence_count": self.evidence_count,
            "reason": self.reason,
            "evidence": [value.to_payload() for value in self.evidence],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ValidationObservation:
        return cls(
            schema=str(payload["schema"]),
            observation_id=str(payload["observation_id"]),
            status=ValidationStatus(str(payload["status"])),
            value=payload.get("value"),
            evidence_count=int(payload.get("evidence_count", 0)),
            reason=None if payload.get("reason") is None else str(payload["reason"]),
            evidence=tuple(
                EvidenceReference.from_payload(value)
                for value in payload.get("evidence", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class ValidationResult:
    result_id: str
    layer: ValidationLayer
    status: ValidationStatus
    severity: ValidationSeverity
    summary: str
    observations: tuple[ValidationObservation, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    details: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = VALIDATION_RESULT_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != VALIDATION_RESULT_SCHEMA_ID:
            raise ValueError(f"unsupported validation result schema: {self.schema}")
        object.__setattr__(
            self, "result_id", require_identifier(self.result_id, "result_id")
        )
        object.__setattr__(self, "layer", ValidationLayer(self.layer))
        object.__setattr__(self, "status", ValidationStatus(self.status))
        object.__setattr__(self, "severity", ValidationSeverity(self.severity))
        if not self.summary.strip():
            raise ValueError("validation result summary cannot be empty")
        observations = tuple(self.observations)
        if not all(isinstance(value, ValidationObservation) for value in observations):
            raise TypeError(
                "validation result observations must contain ValidationObservation values"
            )
        object.__setattr__(self, "observations", observations)
        references = tuple(self.evidence)
        if not all(isinstance(value, EvidenceReference) for value in references):
            raise TypeError("validation result evidence must contain EvidenceReference values")
        object.__setattr__(self, "evidence", references)
        object.__setattr__(self, "details", freeze_json(self.details or {}))

    @property
    def conclusive(self) -> bool:
        return self.status != ValidationStatus.INSUFFICIENT_EVIDENCE

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "result_id": self.result_id,
            "layer": self.layer.value,
            "status": self.status.value,
            "severity": self.severity.value,
            "summary": self.summary,
            "observations": [value.to_payload() for value in self.observations],
            "evidence": [value.to_payload() for value in self.evidence],
            "details": thaw_json(self.details),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ValidationResult:
        return cls(
            schema=str(payload["schema"]),
            result_id=str(payload["result_id"]),
            layer=ValidationLayer(str(payload["layer"])),
            status=ValidationStatus(str(payload["status"])),
            severity=ValidationSeverity(str(payload["severity"])),
            summary=str(payload["summary"]),
            observations=tuple(
                ValidationObservation.from_payload(value)
                for value in payload.get("observations", ())
            ),
            evidence=tuple(
                EvidenceReference.from_payload(value)
                for value in payload.get("evidence", ())
            ),
            details=dict(payload.get("details") or {}),
        )


@dataclass(frozen=True, slots=True)
class ValidationReport:
    report_id: str
    subject_id: str
    results: tuple[ValidationResult, ...]
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = VALIDATION_REPORT_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != VALIDATION_REPORT_SCHEMA_ID:
            raise ValueError(f"unsupported validation report schema: {self.schema}")
        object.__setattr__(
            self, "report_id", require_identifier(self.report_id, "report_id")
        )
        object.__setattr__(
            self, "subject_id", require_identifier(self.subject_id, "subject_id")
        )
        results = tuple(self.results)
        if not all(isinstance(value, ValidationResult) for value in results):
            raise TypeError("validation report results must contain ValidationResult values")
        if len({value.result_id for value in results}) != len(results):
            raise ValueError("validation result identities must be unique within a report")
        object.__setattr__(self, "results", results)
        object.__setattr__(self, "metadata", freeze_json(self.metadata or {}))

    @property
    def status(self) -> ValidationStatus:
        return aggregate_validation_status(value.status for value in self.results)

    @property
    def conclusive(self) -> bool:
        return bool(self.results) and all(value.conclusive for value in self.results)

    @property
    def passed(self) -> bool:
        return self.status in {
            ValidationStatus.PASS,
            ValidationStatus.WARNING,
            ValidationStatus.NOT_APPLICABLE,
        }

    @property
    def report_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "report_id": self.report_id,
            "subject_id": self.subject_id,
            "status": self.status.value,
            "conclusive": self.conclusive,
            "passed": self.passed,
            "results": [value.to_payload() for value in self.results],
            "metadata": thaw_json(self.metadata),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["report_hash"] = self.report_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ValidationReport:
        report = cls(
            schema=str(payload["schema"]),
            report_id=str(payload["report_id"]),
            subject_id=str(payload["subject_id"]),
            results=tuple(
                ValidationResult.from_payload(value)
                for value in payload.get("results", ())
            ),
            metadata=dict(payload.get("metadata") or {}),
        )
        if payload.get("status") != report.status.value:
            raise ValueError("validation report status mismatch")
        if bool(payload.get("conclusive")) != report.conclusive:
            raise ValueError("validation report conclusive flag mismatch")
        if bool(payload.get("passed")) != report.passed:
            raise ValueError("validation report passed flag mismatch")
        if payload.get("report_hash") != report.report_hash:
            raise ValueError("validation report hash mismatch")
        return report


def aggregate_validation_status(
    statuses: Iterable[ValidationStatus],
) -> ValidationStatus:
    values = tuple(ValidationStatus(value) for value in statuses)
    if not values:
        return ValidationStatus.INSUFFICIENT_EVIDENCE
    for status in (
        ValidationStatus.FAIL,
        ValidationStatus.INSUFFICIENT_EVIDENCE,
        ValidationStatus.WARNING,
        ValidationStatus.PASS,
        ValidationStatus.NOT_APPLICABLE,
    ):
        if status in values:
            return status
    raise AssertionError("unreachable validation status aggregation")


def _validate_payload(payload: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    from eegle.specs.schemas import validate_payload

    validate_payload(thaw_json(freeze_json(payload)), schema)


def validate_evidence_reference_payload(payload: Mapping[str, Any]) -> None:
    _validate_payload(payload, EVIDENCE_REFERENCE_JSON_SCHEMA)


def validate_validation_observation_payload(payload: Mapping[str, Any]) -> None:
    _validate_payload(payload, VALIDATION_OBSERVATION_JSON_SCHEMA)


def validate_validation_result_payload(payload: Mapping[str, Any]) -> None:
    _validate_payload(payload, VALIDATION_RESULT_JSON_SCHEMA)


def validate_validation_report_payload(payload: Mapping[str, Any]) -> None:
    _validate_payload(payload, VALIDATION_REPORT_JSON_SCHEMA)
