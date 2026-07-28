"""Versioned diagnostics and exit semantics for public application services."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any, Iterable, Mapping

from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.authoring.contracts import ScientificMateriality, SourceKind, SourceLocation


OPERATION_ERROR_SCHEMA_ID = "eegle.operation_error.v1"
OPERATION_RESULT_SCHEMA_ID = "eegle.operation_result.v1"
_JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$"


class ExitCode(IntEnum):
    SUCCESS = 0
    USAGE_ERROR = 2
    INVALID_INPUT = 3
    REJECTED = 4
    UNAVAILABLE = 5
    EXECUTION_FAILED = 6
    INTEGRITY_FAILED = 7
    INSUFFICIENT_EVIDENCE = 8
    INTERNAL_ERROR = 70


class OperationCategory(str, Enum):
    USAGE = "usage"
    AUTHORING = "authoring"
    SCHEMA = "schema"
    COMPILATION = "compilation"
    PREFLIGHT = "preflight"
    EXECUTION = "execution"
    INTEGRITY = "integrity"
    AVAILABILITY = "availability"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INTERNAL = "internal"


class OperationIssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class RepairKind(str, Enum):
    MANUAL = "manual"
    AUTOMATIC_PROPOSAL = "automatic_proposal"


@dataclass(frozen=True, slots=True)
class RepairOption:
    repair_id: str
    title: str
    kind: RepairKind = RepairKind.MANUAL
    changes_scientific_semantics: bool = False
    description: str | None = None
    proposal: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "repair_id",
            require_identifier(self.repair_id, "repair_id"),
        )
        if not self.title.strip():
            raise ValueError("repair title cannot be empty")
        object.__setattr__(self, "kind", RepairKind(self.kind))
        if not isinstance(self.changes_scientific_semantics, bool):
            raise TypeError("changes_scientific_semantics must be boolean")
        if self.description is not None:
            description = str(self.description)
            if not description.strip():
                raise ValueError("repair description cannot be empty")
            object.__setattr__(self, "description", description)
        object.__setattr__(self, "proposal", freeze_json(self.proposal or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "repair_id": self.repair_id,
            "title": self.title,
            "kind": self.kind.value,
            "changes_scientific_semantics": self.changes_scientific_semantics,
            "description": self.description,
            "proposal": thaw_json(self.proposal),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RepairOption":
        return cls(
            repair_id=str(payload["repair_id"]),
            title=str(payload["title"]),
            kind=RepairKind(str(payload["kind"])),
            changes_scientific_semantics=payload.get(
                "changes_scientific_semantics", False
            ),
            description=None
            if payload.get("description") is None
            else str(payload["description"]),
            proposal=dict(payload.get("proposal") or {}),
        )


@dataclass(frozen=True, slots=True)
class OperationDiagnostic:
    code: str
    category: OperationCategory
    title: str
    message: str
    likely_cause: str | None = None
    severity: OperationIssueSeverity = OperationIssueSeverity.ERROR
    path: str | None = None
    source: SourceLocation | None = None
    scientific_impact: ScientificMateriality = ScientificMateriality.UNKNOWN
    repairs: tuple[RepairOption, ...] = ()
    documentation: str | None = None
    details: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", require_identifier(self.code, "diagnostic code"))
        object.__setattr__(self, "category", OperationCategory(self.category))
        object.__setattr__(self, "severity", OperationIssueSeverity(self.severity))
        object.__setattr__(
            self,
            "scientific_impact",
            ScientificMateriality(self.scientific_impact),
        )
        if not self.title.strip():
            raise ValueError("diagnostic title cannot be empty")
        if not self.message.strip():
            raise ValueError("diagnostic message cannot be empty")
        if self.likely_cause is not None:
            cause = str(self.likely_cause)
            if not cause.strip():
                raise ValueError("diagnostic likely_cause cannot be empty")
            object.__setattr__(self, "likely_cause", cause)
        if self.path is not None and not (
            self.path.startswith("$") or self.path.startswith("/")
        ):
            raise ValueError("diagnostic path must be a compiler path or JSON pointer")
        if self.source is not None and not isinstance(self.source, SourceLocation):
            raise TypeError("diagnostic source must be a SourceLocation")
        repairs = tuple(self.repairs)
        if len({value.repair_id for value in repairs}) != len(repairs):
            raise ValueError("diagnostic repair identities must be unique")
        object.__setattr__(self, "repairs", repairs)
        if self.documentation is not None:
            normalized = str(self.documentation)
            if not normalized.strip():
                raise ValueError("diagnostic documentation reference cannot be empty")
            object.__setattr__(self, "documentation", normalized)
        object.__setattr__(self, "details", freeze_json(self.details or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "category": self.category.value,
            "title": self.title,
            "message": self.message,
            "likely_cause": self.likely_cause,
            "severity": self.severity.value,
            "path": self.path,
            "source": None if self.source is None else self.source.to_payload(),
            "scientific_impact": self.scientific_impact.value,
            "repairs": [value.to_payload() for value in self.repairs],
            "documentation": self.documentation,
            "details": thaw_json(self.details),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "OperationDiagnostic":
        source = payload.get("source")
        return cls(
            code=str(payload["code"]),
            category=OperationCategory(str(payload["category"])),
            title=str(payload["title"]),
            message=str(payload["message"]),
            likely_cause=None
            if payload.get("likely_cause") is None
            else str(payload["likely_cause"]),
            severity=OperationIssueSeverity(str(payload["severity"])),
            path=None if payload.get("path") is None else str(payload["path"]),
            source=None if source is None else SourceLocation.from_payload(source),
            scientific_impact=ScientificMateriality(
                str(payload.get("scientific_impact", ScientificMateriality.UNKNOWN.value))
            ),
            repairs=tuple(
                RepairOption.from_payload(value) for value in payload.get("repairs", ())
            ),
            documentation=None
            if payload.get("documentation") is None
            else str(payload["documentation"]),
            details=dict(payload.get("details") or {}),
        )


class OperationError(RuntimeError):
    """A typed public-service failure with one stable process exit meaning."""

    def __init__(
        self,
        operation: str,
        exit_code: ExitCode,
        diagnostics: Iterable[OperationDiagnostic],
    ) -> None:
        self.operation = require_identifier(operation, "operation")
        self.exit_code = ExitCode(exit_code)
        if self.exit_code == ExitCode.SUCCESS:
            raise ValueError("OperationError cannot use the success exit code")
        self.diagnostics = _sort_diagnostics(diagnostics)
        if not self.diagnostics:
            raise ValueError("OperationError requires at least one diagnostic")
        summary = "; ".join(
            f"{value.code}: {value.message}" for value in self.diagnostics
        )
        super().__init__(summary)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": OPERATION_ERROR_SCHEMA_ID,
            "ok": False,
            "operation": self.operation,
            "exit_code": int(self.exit_code),
            "diagnostics": [value.to_payload() for value in self.diagnostics],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "OperationError":
        validate_operation_error_payload(payload)
        return cls(
            operation=str(payload["operation"]),
            exit_code=ExitCode(int(payload["exit_code"])),
            diagnostics=tuple(
                OperationDiagnostic.from_payload(value)
                for value in payload["diagnostics"]
            ),
        )


_SOURCE_LOCATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["kind"],
    "properties": {
        "kind": {
            "enum": [value.value for value in SourceKind]
        },
        "locator": {"type": "string", "minLength": 1},
        "line": {"type": "integer", "minimum": 1},
        "column": {"type": "integer", "minimum": 1},
        "end_line": {"type": "integer", "minimum": 1},
        "end_column": {"type": "integer", "minimum": 1},
        "symbol": {"type": "string", "minLength": 1},
    },
    "dependentRequired": {
        "column": ["line"],
        "end_line": ["line"],
        "end_column": ["line"],
    },
    "additionalProperties": False,
}


OPERATION_ERROR_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {
        "$schema": _JSON_SCHEMA,
        "title": "EEGle public operation error v1",
        "type": "object",
        "required": ["schema", "ok", "operation", "exit_code", "diagnostics"],
        "properties": {
            "schema": {"const": OPERATION_ERROR_SCHEMA_ID},
            "ok": {"const": False},
            "operation": {"type": "string", "pattern": _IDENTIFIER},
            "exit_code": {
                "enum": [
                    int(value)
                    for value in ExitCode
                    if value != ExitCode.SUCCESS
                ]
            },
            "diagnostics": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": [
                        "code",
                        "category",
                        "title",
                        "message",
                        "severity",
                        "path",
                        "source",
                        "scientific_impact",
                        "repairs",
                        "documentation",
                        "details",
                    ],
                    "properties": {
                        "code": {"type": "string", "pattern": _IDENTIFIER},
                        "category": {
                            "enum": [value.value for value in OperationCategory]
                        },
                        "title": {"type": "string", "minLength": 1},
                        "message": {"type": "string", "minLength": 1},
                        "likely_cause": {
                            "oneOf": [
                                {"type": "null"},
                                {"type": "string", "minLength": 1},
                            ]
                        },
                        "severity": {
                            "enum": [value.value for value in OperationIssueSeverity]
                        },
                        "path": {
                            "oneOf": [
                                {"type": "null"},
                                {"type": "string", "pattern": r"^(?:\$|/).*$"},
                            ]
                        },
                        "source": {
                            "oneOf": [_SOURCE_LOCATION_SCHEMA, {"type": "null"}]
                        },
                        "scientific_impact": {
                            "enum": [value.value for value in ScientificMateriality]
                        },
                        "repairs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": [
                                    "repair_id",
                                    "title",
                                    "kind",
                                    "changes_scientific_semantics",
                                ],
                                "properties": {
                                    "repair_id": {
                                        "type": "string",
                                        "pattern": _IDENTIFIER,
                                    },
                                    "title": {"type": "string", "minLength": 1},
                                    "kind": {
                                        "enum": [value.value for value in RepairKind]
                                    },
                                    "changes_scientific_semantics": {
                                        "type": "boolean"
                                    },
                                    "description": {
                                        "oneOf": [
                                            {"type": "null"},
                                            {"type": "string", "minLength": 1},
                                        ]
                                    },
                                    "proposal": {"type": "object"},
                                },
                                "additionalProperties": False,
                            },
                        },
                        "documentation": {
                            "oneOf": [
                                {"type": "null"},
                                {"type": "string", "minLength": 1},
                            ]
                        },
                        "details": {"type": "object"},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    }
)


OPERATION_RESULT_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {
        "$schema": _JSON_SCHEMA,
        "title": "EEGle public operation result v1",
        "type": "object",
        "required": ["schema", "ok", "operation", "exit_code", "result"],
        "properties": {
            "schema": {"const": OPERATION_RESULT_SCHEMA_ID},
            "ok": {"const": True},
            "operation": {"type": "string", "pattern": _IDENTIFIER},
            "exit_code": {"const": int(ExitCode.SUCCESS)},
            "result": {"type": "object"},
        },
        "additionalProperties": False,
    }
)


def validate_operation_error_payload(payload: Mapping[str, Any]) -> None:
    from eegle.specs.schemas import validate_payload

    explicit = thaw_json(freeze_json(payload))
    validate_payload(explicit, OPERATION_ERROR_JSON_SCHEMA)


def validate_operation_result_payload(payload: Mapping[str, Any]) -> None:
    from eegle.specs.schemas import validate_payload

    explicit = thaw_json(freeze_json(payload))
    validate_payload(explicit, OPERATION_RESULT_JSON_SCHEMA)


def _sort_diagnostics(
    diagnostics: Iterable[OperationDiagnostic],
) -> tuple[OperationDiagnostic, ...]:
    severity_order = {
        OperationIssueSeverity.ERROR: 0,
        OperationIssueSeverity.WARNING: 1,
        OperationIssueSeverity.INFO: 2,
    }
    return tuple(
        sorted(
            diagnostics,
            key=lambda value: (
                severity_order[value.severity],
                value.category.value,
                value.path or "",
                value.code,
                value.message,
            ),
        )
    )
