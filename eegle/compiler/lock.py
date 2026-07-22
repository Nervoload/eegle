"""Canonical JSON and content hashing for plans and evidence manifests."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from dataclasses import is_dataclass
from enum import Enum
from typing import Any, Mapping


class CanonicalizationError(ValueError):
    pass


def _normalize(value: Any, path: str = "$") -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(f"{path} contains a non-finite number")
        return 0.0 if value == 0.0 else value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Enum):
        return _normalize(value.value, path)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path} has a non-string object key")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise CanonicalizationError(
                    f"{path} contains keys that collide after Unicode normalization"
                )
            normalized[normalized_key] = _normalize(item, f"{path}.{normalized_key}")
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize(item, f"{path}[{index}]") for index, item in enumerate(value)]
    to_payload = getattr(value, "to_payload", None)
    if callable(to_payload):
        return _normalize(to_payload(), path)
    if is_dataclass(value):
        raise CanonicalizationError(
            f"{path} is a dataclass without an explicit to_payload() representation"
        )
    raise CanonicalizationError(
        f"{path} contains unsupported type {type(value).__name__}; encode it explicitly"
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize explicit JSON data using EEGle canonicalization v1.

    Rules: UTF-8, NFC strings and keys, lexicographically sorted object keys,
    compact separators, finite numbers only, and negative zero normalized to
    positive zero. Paths, bytes, arrays, sets, and arbitrary objects are never
    converted implicitly.
    """

    normalized = _normalize(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_hash(value: Any) -> str:
    return content_hash(canonical_json_bytes(value))


def verify_hash(value: Any, expected: str) -> None:
    observed = canonical_hash(value)
    if observed != expected:
        raise ValueError(f"canonical hash mismatch: expected {expected}, observed {observed}")
