"""Dynamic registry for EEGle model-family declarations."""

from __future__ import annotations

from collections.abc import Iterable
from importlib import metadata

from eegle.ml.families.classical import SPECS as CLASSICAL_SPECS
from eegle.ml.families.cnn import SPECS as CNN_SPECS
from eegle.ml.families.eeg_foundation import SPECS as EEG_FOUNDATION_SPECS
from eegle.ml.registry_types import ModelSpec


_SPECS: dict[str, ModelSpec] = {}
_ALIASES: dict[str, str] = {}
_ENTRY_POINTS_LOADED = False


def register_model_spec(spec: ModelSpec, *, replace: bool = False) -> ModelSpec:
    """Register a model specification for built-ins or optional adapter packages."""
    kind = str(spec.kind).strip().lower()
    if not kind:
        raise ValueError("model spec kind cannot be empty")
    if kind in _SPECS and not replace:
        raise ValueError(f"model kind '{kind}' is already registered")
    for alias in spec.aliases:
        normalized_alias = str(alias).strip().lower()
        if normalized_alias and normalized_alias in _ALIASES and _ALIASES[normalized_alias] != kind and not replace:
            raise ValueError(f"model alias '{normalized_alias}' is already registered")
    _SPECS[kind] = spec
    for alias in spec.aliases:
        normalized_alias = str(alias).strip().lower()
        if normalized_alias:
            _ALIASES[normalized_alias] = kind
    _ALIASES["default"] = "erp_roi_logreg"
    return spec


def unregister_model_spec(kind: str) -> None:
    """Remove a registered model spec; intended for tests and plugin reloads."""
    resolved = resolve_model_kind(kind)
    spec = _SPECS.pop(resolved, None)
    if spec is None:
        return
    for alias in spec.aliases:
        _ALIASES.pop(str(alias).strip().lower(), None)
    for alias, target in list(_ALIASES.items()):
        if target == resolved and alias != "default":
            _ALIASES.pop(alias, None)


def load_entry_point_model_specs(group: str = "eegle.models") -> list[ModelSpec]:
    """Load plugin-provided model specs from Python package entry points."""
    loaded: list[ModelSpec] = []
    try:
        entry_points = metadata.entry_points()
        candidates = entry_points.select(group=group) if hasattr(entry_points, "select") else entry_points.get(group, [])
    except Exception:
        return loaded
    for entry_point in candidates:
        value = entry_point.load()
        specs = value() if callable(value) else value
        for spec in _iter_specs(specs):
            register_model_spec(spec, replace=True)
            loaded.append(spec)
    return loaded


def resolve_model_kind(kind: str | None) -> str:
    _ensure_entry_points_loaded()
    value = str(kind or "default").strip().lower()
    return _ALIASES.get(value, value)


def get_model_spec(kind: str | None) -> ModelSpec:
    resolved = resolve_model_kind(kind)
    try:
        return _SPECS[resolved]
    except KeyError as exc:
        raise NotImplementedError(f"model kind '{kind}' is not registered") from exc


def list_model_specs() -> list[ModelSpec]:
    _ensure_entry_points_loaded()
    return sorted(_SPECS.values(), key=lambda spec: (spec.family, spec.kind))


def list_model_kinds(*, include_aliases: bool = False, trainable: bool | None = None) -> list[str]:
    kinds = [
        spec.kind
        for spec in list_model_specs()
        if trainable is None or bool(spec.trainable) == bool(trainable)
    ]
    if include_aliases:
        for spec in list_model_specs():
            if trainable is None or bool(spec.trainable) == bool(trainable):
                kinds.extend(spec.aliases)
    return sorted(dict.fromkeys(kinds))


def _ensure_entry_points_loaded() -> None:
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    load_entry_point_model_specs()


def _iter_specs(value: object) -> Iterable[ModelSpec]:
    if isinstance(value, ModelSpec):
        yield value
        return
    if isinstance(value, Iterable):
        for item in value:
            if not isinstance(item, ModelSpec):
                raise TypeError(f"entry-point model spec must be ModelSpec, got {type(item).__name__}")
            yield item
        return
    raise TypeError(f"entry-point model spec must be ModelSpec or iterable, got {type(value).__name__}")


for _spec in (*CLASSICAL_SPECS, *CNN_SPECS, *EEG_FOUNDATION_SPECS):
    register_model_spec(_spec, replace=True)
