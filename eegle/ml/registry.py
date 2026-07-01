"""Registry for EEGle model families and model-kind aliases."""

from __future__ import annotations

from eegle.ml.families.classical import SPECS as CLASSICAL_SPECS
from eegle.ml.families.cnn import SPECS as CNN_SPECS
from eegle.ml.families.eeg_foundation import SPECS as EEG_FOUNDATION_SPECS
from eegle.ml.registry_types import ModelSpec


_SPECS: dict[str, ModelSpec] = {
    spec.kind: spec
    for spec in (*CLASSICAL_SPECS, *CNN_SPECS, *EEG_FOUNDATION_SPECS)
}
_ALIASES: dict[str, str] = {
    alias: spec.kind
    for spec in _SPECS.values()
    for alias in spec.aliases
}
_ALIASES["default"] = "erp_roi_logreg"


def resolve_model_kind(kind: str | None) -> str:
    value = str(kind or "default").strip().lower()
    return _ALIASES.get(value, value)


def get_model_spec(kind: str | None) -> ModelSpec:
    resolved = resolve_model_kind(kind)
    try:
        return _SPECS[resolved]
    except KeyError as exc:
        raise NotImplementedError(f"model kind '{kind}' is not registered") from exc


def list_model_specs() -> list[ModelSpec]:
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
