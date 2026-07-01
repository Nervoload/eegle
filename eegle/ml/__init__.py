"""Model-training infrastructure shared by offline and realtime EEGle flows."""

from eegle.ml.registry import (
    ModelSpec,
    get_model_spec,
    list_model_specs,
    list_model_kinds,
    resolve_model_kind,
)

__all__ = [
    "ModelSpec",
    "get_model_spec",
    "list_model_specs",
    "list_model_kinds",
    "resolve_model_kind",
]
