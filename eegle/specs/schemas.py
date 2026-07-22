"""JSON Schema 2020-12 validation with stable, path-oriented diagnostics."""

from __future__ import annotations

from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from eegle._validation import thaw_json


class SchemaValidationError(ValueError):
    def __init__(self, message: str, *, path: str = "$") -> None:
        self.path = path
        super().__init__(f"{path}: {message}")


def validate_schema(schema: Mapping[str, Any]) -> None:
    schema_payload = thaw_json(schema)
    try:
        Draft202012Validator.check_schema(schema_payload)
    except SchemaError as exc:
        path = _format_path(exc.absolute_schema_path)
        raise SchemaValidationError(exc.message, path=path) from exc


def validate_payload(payload: Any, schema: Mapping[str, Any]) -> None:
    validate_schema(schema)
    validator = Draft202012Validator(thaw_json(schema))
    errors = sorted(
        validator.iter_errors(payload),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        raise SchemaValidationError(error.message, path=_format_path(error.absolute_path))


def _format_path(parts: Any) -> str:
    path = "$"
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return path
