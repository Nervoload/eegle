"""Machine-readable truth boundary for optional research integrations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import Enum
from importlib.resources import files
from typing import Any

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash

RESEARCH_SUPPORT_MATRIX_SCHEMA_ID = "eegle.research_integration_support.v1"
_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$"


class SupportValidationLevel(str, Enum):
    CONTRACT = "contract"
    UNIT = "unit"
    SIMULATED_ENGINE = "simulated_engine"
    INSTALLED_ARTIFACT = "installed_artifact"
    EXTERNAL_REFERENCE = "external_reference"


RESEARCH_SUPPORT_MATRIX_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": RESEARCH_SUPPORT_MATRIX_SCHEMA_ID,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "snapshot_date", "capabilities", "matrix_hash"],
        "properties": {
            "schema": {"const": RESEARCH_SUPPORT_MATRIX_SCHEMA_ID},
            "snapshot_date": {"type": "string", "format": "date"},
            "matrix_hash": {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"},
            "capabilities": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "capability_id",
                        "integration",
                        "representable",
                        "adapter_available",
                        "validated",
                        "validation_levels",
                        "reference_supported",
                        "evidence",
                        "limitations",
                    ],
                    "properties": {
                        "capability_id": {"type": "string", "pattern": _IDENTIFIER},
                        "integration": {"type": "string", "pattern": _IDENTIFIER},
                        "representable": {"type": "boolean"},
                        "adapter_available": {"type": "boolean"},
                        "validated": {"type": "boolean"},
                        "validation_levels": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {
                                "enum": [value.value for value in SupportValidationLevel]
                            },
                        },
                        "reference_supported": {"type": "boolean"},
                        "evidence": {
                            "type": "array",
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


@dataclass(frozen=True, slots=True)
class IntegrationCapabilitySupport:
    capability_id: str
    integration: str
    representable: bool
    adapter_available: bool
    validated: bool
    validation_levels: tuple[SupportValidationLevel, ...]
    reference_supported: bool
    evidence: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "capability_id",
            require_identifier(self.capability_id, "capability_id"),
        )
        object.__setattr__(
            self,
            "integration",
            require_identifier(self.integration, "integration"),
        )
        levels = tuple(SupportValidationLevel(value) for value in self.validation_levels)
        if len(levels) != len(set(levels)):
            raise ValueError("support validation levels must be unique")
        object.__setattr__(self, "validation_levels", levels)
        if self.adapter_available and not self.representable:
            raise ValueError("an available adapter requires a representable capability")
        if self.validated != bool(levels):
            raise ValueError("validated must exactly reflect available validation evidence")
        if self.validated and not self.adapter_available:
            raise ValueError("adapter validation requires an available adapter")
        evidence = tuple(str(value).strip() for value in self.evidence)
        limitations = tuple(str(value).strip() for value in self.limitations)
        if any(not value for value in (*evidence, *limitations)):
            raise ValueError("support evidence and limitations cannot contain empty text")
        if len(evidence) != len(set(evidence)) or len(limitations) != len(set(limitations)):
            raise ValueError("support evidence and limitations must be unique")
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "limitations", limitations)

    def to_payload(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "integration": self.integration,
            "representable": bool(self.representable),
            "adapter_available": bool(self.adapter_available),
            "validated": bool(self.validated),
            "validation_levels": [value.value for value in self.validation_levels],
            "reference_supported": bool(self.reference_supported),
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> IntegrationCapabilitySupport:
        return cls(
            capability_id=str(payload["capability_id"]),
            integration=str(payload["integration"]),
            representable=bool(payload["representable"]),
            adapter_available=bool(payload["adapter_available"]),
            validated=bool(payload["validated"]),
            validation_levels=tuple(
                SupportValidationLevel(str(value))
                for value in payload["validation_levels"]
            ),
            reference_supported=bool(payload["reference_supported"]),
            evidence=tuple(str(value) for value in payload["evidence"]),
            limitations=tuple(str(value) for value in payload["limitations"]),
        )


@dataclass(frozen=True, slots=True)
class ResearchIntegrationSupportMatrix:
    snapshot_date: str
    capabilities: tuple[IntegrationCapabilitySupport, ...]
    schema: str = RESEARCH_SUPPORT_MATRIX_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != RESEARCH_SUPPORT_MATRIX_SCHEMA_ID:
            raise ValueError(f"unsupported research support schema: {self.schema}")
        try:
            date.fromisoformat(self.snapshot_date)
        except ValueError as exc:
            raise ValueError("support matrix snapshot_date must be ISO-8601") from exc
        capabilities = tuple(self.capabilities)
        identifiers = tuple(value.capability_id for value in capabilities)
        if not capabilities or identifiers != tuple(sorted(identifiers)):
            raise ValueError("support capabilities must be non-empty and sorted by identity")
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("support capability identities must be unique")
        object.__setattr__(self, "capabilities", capabilities)

    @property
    def matrix_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "snapshot_date": self.snapshot_date,
            "capabilities": [value.to_payload() for value in self.capabilities],
        }

    def to_payload(self) -> dict[str, Any]:
        return {**self.content_payload(), "matrix_hash": self.matrix_hash}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ResearchIntegrationSupportMatrix:
        validate_research_support_matrix_payload(payload)
        value = cls(
            snapshot_date=str(payload["snapshot_date"]),
            capabilities=tuple(
                IntegrationCapabilitySupport.from_payload(item)
                for item in payload["capabilities"]
            ),
            schema=str(payload["schema"]),
        )
        expected = require_digest(str(payload["matrix_hash"]), "matrix_hash")
        if value.matrix_hash != expected:
            raise ValueError("research support matrix hash mismatch")
        return value


def validate_research_support_matrix_payload(payload: Mapping[str, Any]) -> None:
    from eegle.specs.schemas import validate_payload

    validate_payload(
        thaw_json(freeze_json(payload)),
        RESEARCH_SUPPORT_MATRIX_JSON_SCHEMA,
    )


def research_integration_support_matrix() -> ResearchIntegrationSupportMatrix:
    """Load and verify the support matrix shipped in the installed artifact."""

    resource = files("eegle.integrations").joinpath("research_support.json")
    payload = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("research support matrix must contain a JSON object")
    return ResearchIntegrationSupportMatrix.from_payload(payload)


__all__ = [
    "RESEARCH_SUPPORT_MATRIX_JSON_SCHEMA",
    "RESEARCH_SUPPORT_MATRIX_SCHEMA_ID",
    "IntegrationCapabilitySupport",
    "ResearchIntegrationSupportMatrix",
    "SupportValidationLevel",
    "research_integration_support_matrix",
    "validate_research_support_matrix_payload",
]
