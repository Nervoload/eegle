"""Small dependency-free validation helpers shared by EEGle foundations."""

from __future__ import annotations

import math
import re
from types import MappingProxyType
from typing import Any, Mapping

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_FIELD_SUFFIXES = frozenset(
    {
        "api_key",
        "api_token",
        "access_token",
        "refresh_token",
        "client_secret",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "secret",
        "token",
    }
)
_SECRET_REFERENCE_SUFFIXES = frozenset(
    {
        "credential_id",
        "credential_ref",
        "secret_id",
        "secret_ref",
        "secret_reference",
    }
)


def require_identifier(value: str, field: str) -> str:
    normalized = str(value)
    if not _IDENTIFIER.fullmatch(normalized):
        raise ValueError(
            f"{field} must start with an alphanumeric character and contain only "
            "letters, digits, '.', '_', ':', '/', or '-'"
        )
    return normalized


def require_digest(value: str, field: str = "digest") -> str:
    normalized = str(value)
    if not _DIGEST.fullmatch(normalized):
        raise ValueError(f"{field} must be a lowercase sha256:<64 hex characters> digest")
    return normalized


def require_finite(value: float, field: str) -> float:
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field} must be finite")
    return normalized


def is_literal_secret_field(value: str) -> bool:
    """Recognize secret-shaped field names across snake, kebab, and camel case."""

    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value).strip())
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", expanded).strip("_").casefold()
    if any(
        normalized == suffix or normalized.endswith(f"_{suffix}")
        for suffix in _SECRET_REFERENCE_SUFFIXES
    ):
        return False
    return any(
        normalized == suffix or normalized.endswith(f"_{suffix}")
        for suffix in _SECRET_FIELD_SUFFIXES
    )


def freeze_json(value: Any) -> Any:
    """Deep-copy a JSON-compatible value into immutable containers.

    This function deliberately rejects implicit conversions. Arrays, paths,
    sets, bytes, datetimes, and arbitrary objects require an explicit schema
    representation before they cross a foundation contract.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return require_finite(value, "JSON number")
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            frozen[key] = freeze_json(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError(f"value of type {type(value).__name__} is not explicitly JSON-compatible")


def thaw_json(value: Any) -> Any:
    """Return ordinary JSON containers from :func:`freeze_json` output."""

    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value
