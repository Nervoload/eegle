"""Stable, structured compiler diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from eegle._validation import freeze_json, require_identifier, thaw_json


class DiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class CompilationDiagnostic:
    code: str
    path: str
    message: str
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR
    details: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", require_identifier(self.code, "diagnostic code"))
        if not self.path.startswith("$"):
            raise ValueError("diagnostic path must start at '$'")
        if not self.message.strip():
            raise ValueError("diagnostic message cannot be empty")
        object.__setattr__(self, "severity", DiagnosticSeverity(self.severity))
        object.__setattr__(self, "details", freeze_json(self.details or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "severity": self.severity.value,
            "details": thaw_json(self.details),
        }


def sort_diagnostics(
    diagnostics: Iterable[CompilationDiagnostic],
) -> tuple[CompilationDiagnostic, ...]:
    return tuple(
        sorted(
            diagnostics,
            key=lambda value: (
                value.path,
                value.severity.value,
                value.code,
                value.message,
            ),
        )
    )


class CompilationError(ValueError):
    def __init__(self, diagnostics: Iterable[CompilationDiagnostic]) -> None:
        self.diagnostics = sort_diagnostics(diagnostics)
        if not self.diagnostics:
            raise ValueError("CompilationError requires at least one diagnostic")
        summary = "; ".join(
            f"{value.code} at {value.path}: {value.message}"
            for value in self.diagnostics
        )
        super().__init__(summary)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.compilation_diagnostics.v1",
            "valid": False,
            "diagnostics": [value.to_payload() for value in self.diagnostics],
        }
