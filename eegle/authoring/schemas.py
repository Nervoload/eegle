"""Versioned envelope schemas for Phase 7 drafts and provenance sidecars."""

from __future__ import annotations

from typing import Any, Mapping

from eegle._validation import freeze_json, thaw_json
from eegle.authoring.contracts import (
    AuthoringOrigin,
    CanonicalArtifact,
    ConfirmationState,
    ScientificMateriality,
    SourceKind,
    SourceLocation,
)


EXPERIMENT_DRAFT_SCHEMA_ID = "eegle.experiment_draft.v1"
AUTHORING_PROVENANCE_SCHEMA_ID = "eegle.authoring_provenance.v1"
DEPLOYMENT_REQUIREMENTS_SCHEMA_ID = "eegle.deployment_requirements.v1"
TEMPLATE_AUTHORING_SCHEMA_ID = "eegle.template_authoring.v1"
_JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$"
_DIGEST = r"^sha256:[0-9a-f]{64}$"
_JSON_POINTER = r"^(?:/(?:[^~/]|~[01])*)*$"


_SOURCE_LOCATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["kind"],
    "properties": {
        "kind": {"enum": [value.value for value in SourceKind]},
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


EXPERIMENT_DRAFT_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {
        "$schema": _JSON_SCHEMA,
        "title": "EEGle experiment draft envelope v1",
        "type": "object",
        "required": ["schema", "draft_id", "revision", "intent"],
        "properties": {
            "schema": {"const": EXPERIMENT_DRAFT_SCHEMA_ID},
            "draft_id": {"type": "string", "pattern": _IDENTIFIER},
            "revision": {"type": "integer", "minimum": 1},
            "intent": {
                "type": "object",
                "minProperties": 1,
                "properties": {
                    "study": {"type": "object"},
                    "signals": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "events": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "processing": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "windows": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "models": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "phases": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "outcomes": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "actions": {
                        "type": "array",
                        "items": {"type": "object"},
                    },
                    "recording": {"type": "object"},
                    "acceptance": {
                        "oneOf": [
                            {"type": "object"},
                            {"type": "array", "items": {"type": "object"}},
                        ]
                    },
                    "deployment_requirements": {"type": "object"},
                },
                "additionalProperties": False,
            },
            "template": {
                "type": ["object", "null"],
                "required": [
                    "template_id",
                    "version",
                    "manifest_digest",
                    "parameters",
                ],
                "properties": {
                    "template_id": {"type": "string", "pattern": _IDENTIFIER},
                    "version": {"type": "string", "minLength": 1},
                    "manifest_digest": {"type": "string", "pattern": _DIGEST},
                    "parameters": {"type": "object"},
                },
                "additionalProperties": False,
            },
            "unresolved": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path", "kind", "prompt"],
                    "properties": {
                        "path": {"type": "string", "pattern": _JSON_POINTER},
                        "kind": {"enum": ["required", "choice", "detection"]},
                        "prompt": {"type": "string", "minLength": 1},
                        "options": {"type": "array"},
                    },
                    "additionalProperties": False,
                },
                "uniqueItems": True,
            },
        },
        "additionalProperties": False,
    }
)


TEMPLATE_AUTHORING_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {
        "$schema": _JSON_SCHEMA,
        "title": "EEGle exact-template authoring source v1",
        "type": "object",
        "required": ["schema", "draft_id", "template"],
        "properties": {
            "schema": {"const": TEMPLATE_AUTHORING_SCHEMA_ID},
            "draft_id": {"type": "string", "pattern": _IDENTIFIER},
            "revision": {"type": "integer", "minimum": 1},
            "template": {
                "type": "object",
                "required": ["template_id", "version", "parameters"],
                "properties": {
                    "template_id": {"type": "string", "pattern": _IDENTIFIER},
                    "version": {"type": "string", "minLength": 1},
                    "parameters": {"type": "object"},
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }
)


AUTHORING_PROVENANCE_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {
        "$schema": _JSON_SCHEMA,
        "title": "EEGle authoring provenance sidecar v1",
        "type": "object",
        "required": [
            "schema",
            "draft_id",
            "draft_revision",
            "draft_digest",
            "canonical_targets",
            "entries",
        ],
        "properties": {
            "schema": {"const": AUTHORING_PROVENANCE_SCHEMA_ID},
            "draft_id": {"type": "string", "pattern": _IDENTIFIER},
            "draft_revision": {"type": "integer", "minimum": 1},
            "draft_digest": {"type": "string", "pattern": _DIGEST},
            "canonical_targets": {
                "type": "object",
                "required": ["protocol", "suite"],
                "properties": {
                    value.value: {
                        "type": "object",
                        "required": ["schema", "digest"],
                        "properties": {
                            "schema": {"type": "string", "pattern": _IDENTIFIER},
                            "digest": {"type": "string", "pattern": _DIGEST},
                        },
                        "additionalProperties": False,
                    }
                    for value in CanonicalArtifact
                },
                "additionalProperties": False,
            },
            "entries": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "type": "object",
                    "required": [
                        "target_artifact",
                        "target_path",
                        "origin",
                        "source",
                        "materiality",
                        "confirmation",
                    ],
                    "properties": {
                        "target_artifact": {
                            "enum": [value.value for value in CanonicalArtifact]
                        },
                        "target_path": {
                            "type": "string",
                            "pattern": _JSON_POINTER,
                        },
                        "origin": {"enum": [value.value for value in AuthoringOrigin]},
                        "source": _SOURCE_LOCATION_SCHEMA,
                        "materiality": {
                            "enum": [value.value for value in ScientificMateriality]
                        },
                        "confirmation": {
                            "enum": [value.value for value in ConfirmationState]
                        },
                        "template": {
                            "type": "object",
                            "required": ["template_id", "version"],
                            "properties": {
                                "template_id": {"type": "string", "pattern": _IDENTIFIER},
                                "version": {"type": "string", "minLength": 1},
                                "parameter_path": {
                                    "type": "string",
                                    "pattern": _JSON_POINTER,
                                },
                            },
                            "additionalProperties": False,
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    }
)


DEPLOYMENT_REQUIREMENTS_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {
        "$schema": _JSON_SCHEMA,
        "title": "EEGle deployment requirements v1",
        "type": "object",
        "required": ["schema", "suite_id", "requirements"],
        "properties": {
            "schema": {"const": DEPLOYMENT_REQUIREMENTS_SCHEMA_ID},
            "suite_id": {"type": "string", "pattern": _IDENTIFIER},
            "requirements": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": [
                        "requirement_id",
                        "kind",
                        "component_id",
                        "stream_id",
                        "contract",
                        "source_clock",
                        "target_clock",
                        "storage_kind",
                        "manifest_digest",
                        "action_capability",
                        "required_capabilities",
                    ],
                    "properties": {
                        "requirement_id": {"type": "string", "pattern": _IDENTIFIER},
                        "kind": {
                            "enum": [
                                "source_binding",
                                "storage",
                                "clock_mapping",
                                "model_artifact",
                                "authorization",
                            ]
                        },
                        "component_id": {"type": ["string", "null"]},
                        "stream_id": {"type": ["string", "null"]},
                        "contract": {"type": ["object", "null"]},
                        "source_clock": {"type": ["string", "null"]},
                        "target_clock": {"type": ["string", "null"]},
                        "storage_kind": {"type": ["string", "null"]},
                        "manifest_digest": {
                            "type": ["string", "null"],
                            "pattern": _DIGEST,
                        },
                        "action_capability": {
                            "type": ["string", "null"],
                            "pattern": _IDENTIFIER,
                        },
                        "required_capabilities": {
                            "type": "array",
                            "items": {"type": "string", "pattern": _IDENTIFIER},
                            "uniqueItems": True,
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    }
)


def validate_experiment_draft_payload(payload: Mapping[str, Any]) -> None:
    """Validate an explicit JSON draft envelope without lowering it."""

    _validate_explicit_payload(payload, EXPERIMENT_DRAFT_JSON_SCHEMA)


def validate_template_authoring_payload(payload: Mapping[str, Any]) -> None:
    """Validate a parser-independent exact-template authoring source."""

    _validate_explicit_payload(payload, TEMPLATE_AUTHORING_JSON_SCHEMA)


def validate_authoring_provenance_payload(payload: Mapping[str, Any]) -> None:
    """Validate the non-hashing source/provenance sidecar envelope."""

    explicit = _validate_explicit_payload(payload, AUTHORING_PROVENANCE_JSON_SCHEMA)
    for entry in explicit["entries"]:
        SourceLocation.from_payload(entry["source"])


def validate_deployment_requirements_payload(payload: Mapping[str, Any]) -> None:
    """Validate portable needs without accepting site bindings."""

    explicit = _validate_explicit_payload(
        payload,
        DEPLOYMENT_REQUIREMENTS_JSON_SCHEMA,
    )
    identities = [value["requirement_id"] for value in explicit["requirements"]]
    if len(identities) != len(set(identities)):
        raise ValueError("deployment requirement identities must be unique")
    for requirement in explicit["requirements"]:
        kind = requirement["kind"]
        if kind == "source_binding" and any(
            requirement[field] is None
            for field in ("component_id", "stream_id", "contract")
        ):
            raise ValueError(
                "source binding requirements need component, stream, and contract"
            )
        if kind == "clock_mapping" and any(
            requirement[field] is None
            for field in ("source_clock", "target_clock")
        ):
            raise ValueError(
                "clock mapping requirements need source and target clocks"
            )
        if kind == "storage" and requirement["storage_kind"] is None:
            raise ValueError("storage requirements need storage_kind")
        if kind == "model_artifact" and any(
            requirement[field] is None
            for field in ("component_id", "manifest_digest")
        ):
            raise ValueError(
                "model artifact requirements need component and manifest digest"
            )
        if kind == "authorization" and any(
            requirement[field] is None
            for field in ("component_id", "action_capability")
        ):
            raise ValueError(
                "authorization requirements need component and action capability"
            )


def _validate_explicit_payload(
    payload: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    from eegle.specs.schemas import validate_payload

    explicit = thaw_json(freeze_json(payload))
    validate_payload(explicit, schema)
    return explicit
