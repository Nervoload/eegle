"""Compatibility wrapper for the public :mod:`eegle.models.registry` API."""

from __future__ import annotations

from eegle.models.registry import (
    get_model_spec,
    list_model_kinds,
    list_model_specs,
    load_entry_point_model_specs,
    register_model_spec,
    resolve_model_kind,
    unregister_model_spec,
)
from eegle.ml.registry_types import ModelSpec


__all__ = [
    "ModelSpec",
    "get_model_spec",
    "list_model_kinds",
    "list_model_specs",
    "load_entry_point_model_specs",
    "register_model_spec",
    "resolve_model_kind",
    "unregister_model_spec",
]
