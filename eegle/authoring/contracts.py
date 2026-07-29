"""Shared authoring vocabulary used by source maps and public diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class SourceKind(str, Enum):
    PYTHON = "python"
    YAML = "yaml"
    TEMPLATE = "template"
    CLI = "cli"
    DETECTION = "detection"
    MIGRATION = "migration"
    GENERATED = "generated"


class AuthoringOrigin(str, Enum):
    USER_EXPLICIT = "user_explicit"
    TEMPLATE_DEFAULT = "template_default"
    AUTHORING_DEFAULT = "authoring_default"
    AUTHORING_DERIVED = "authoring_derived"
    DETECTION_PROPOSAL = "detection_proposal"
    MIGRATION_GENERATED = "migration_generated"


class ScientificMateriality(str, Enum):
    SCIENTIFIC = "scientific"
    OPERATIONAL = "operational"
    PRESENTATIONAL = "presentational"
    UNKNOWN = "unknown"


class ConfirmationState(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DECLINED = "declined"


class CanonicalArtifact(str, Enum):
    PROTOCOL = "protocol"
    SUITE = "suite"
    DEPLOYMENT = "deployment"


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """A location in an authoring source, not a canonical semantic value."""

    kind: SourceKind
    locator: str | None = None
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None
    symbol: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", SourceKind(self.kind))
        for field in ("locator", "symbol"):
            value = getattr(self, field)
            if value is not None:
                normalized = str(value)
                if not normalized.strip():
                    raise ValueError(f"source {field} cannot be empty")
                object.__setattr__(self, field, normalized)
        for field in ("line", "column", "end_line", "end_column"):
            value = getattr(self, field)
            if value is not None:
                normalized = int(value)
                if normalized <= 0:
                    raise ValueError(f"source {field} must be positive")
                object.__setattr__(self, field, normalized)
        if self.column is not None and self.line is None:
            raise ValueError("source column requires line")
        if self.end_line is not None and self.line is None:
            raise ValueError("source end_line requires line")
        if self.end_column is not None and self.line is None:
            raise ValueError("source end_column requires line")
        if (
            self.end_line is not None
            and self.line is not None
            and self.end_line < self.line
        ):
            raise ValueError("source end_line cannot precede line")
        if (
            self.end_column is not None
            and (self.end_line is None or self.end_line == self.line)
            and self.column is not None
            and self.end_column < self.column
        ):
            raise ValueError("source end_column cannot precede column on the same line")

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind.value}
        for field in (
            "locator",
            "line",
            "column",
            "end_line",
            "end_column",
            "symbol",
        ):
            value = getattr(self, field)
            if value is not None:
                payload[field] = value
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SourceLocation":
        return cls(
            kind=SourceKind(str(payload["kind"])),
            locator=None if payload.get("locator") is None else str(payload["locator"]),
            line=None if payload.get("line") is None else int(payload["line"]),
            column=None if payload.get("column") is None else int(payload["column"]),
            end_line=None
            if payload.get("end_line") is None
            else int(payload["end_line"]),
            end_column=None
            if payload.get("end_column") is None
            else int(payload["end_column"]),
            symbol=None if payload.get("symbol") is None else str(payload["symbol"]),
        )
