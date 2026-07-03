"""Model-training infrastructure shared by offline and realtime EEGle flows."""

from eegle.ml.registry import (
    ModelSpec,
    get_model_spec,
    list_model_specs,
    list_model_kinds,
    register_model_spec,
    resolve_model_kind,
    unregister_model_spec,
)

__all__ = [
    "ModelSpec",
    "get_model_spec",
    "list_model_specs",
    "list_model_kinds",
    "register_model_spec",
    "resolve_model_kind",
    "unregister_model_spec",
]
