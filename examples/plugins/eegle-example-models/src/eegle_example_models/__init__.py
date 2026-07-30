"""Two deliberately different model implementations for EEGle examples."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from eegle.models import ModelResult, Prediction
from eegle.plugins import (
    ComponentKind,
    Determinism,
    EquivalenceLevel,
    ExecutionMode,
    PluginCapabilities,
    PluginDescriptor,
    PortSpec,
    StateBehavior,
)
from eegle.processing import DenseWindow
from eegle.runtime import AdaptationResult, Outcome, TransitionStatus

_DENSE_WINDOW = "eegle.dense_window.v1"
_PREDICTION = "eegle.prediction.v2"
_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


class MeanThresholdModel:
    """Score a window by the arithmetic mean of its valid samples."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.threshold = float(config.get("threshold", 0.0))

    def predict(self, item: DenseWindow, context: Any) -> ModelResult:
        values = _valid_values(item)
        if values.size == 0:
            return _abstention(self.threshold)
        score = float(np.mean(values))
        return _result(score, self.threshold)


class PeakThresholdModel:
    """Score a window by its largest absolute valid sample."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.threshold = float(config.get("threshold", 0.0))

    def predict(self, item: DenseWindow, context: Any) -> ModelResult:
        values = _valid_values(item)
        if values.size == 0:
            return _abstention(self.threshold)
        score = float(np.max(np.abs(values)))
        return _result(score, self.threshold)


class AdaptiveMeanModel:
    """Shift a mean-score bias only when an authorized outcome reaches it."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.threshold = float(config.get("threshold", 0.0))
        self.bias = float(config.get("initial_bias", 0.0))

    def predict(self, item: DenseWindow, context: Any) -> ModelResult:
        values = _valid_values(item)
        if values.size == 0:
            return _abstention(self.threshold)
        score = float(np.mean(values)) + self.bias
        return _result(score, self.threshold)

    def adapt(
        self,
        prediction: Prediction,
        outcome: Outcome,
        context: Any,
    ) -> AdaptationResult:
        value = dict(outcome.value) if isinstance(outcome.value, Mapping) else {}
        delta = value.get("delta")
        if delta is None:
            return AdaptationResult(
                TransitionStatus.NO_OP,
                reason="outcome_has_no_delta",
            )
        self.bias += float(delta)
        return AdaptationResult(
            TransitionStatus.APPLIED,
            metadata={"bias": self.bias},
        )

    def snapshot_state(self) -> Mapping[str, Any]:
        return {
            "schema": "eegle.example_models.adaptive_mean_state.v1",
            "bias": self.bias,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        if state.get("schema") != "eegle.example_models.adaptive_mean_state.v1":
            raise ValueError("unsupported adaptive mean state")
        self.bias = float(state["bias"])


def _result(score: float, threshold: float) -> ModelResult:
    return ModelResult(
        {
            "label": "positive" if score >= threshold else "negative",
            "score": score,
            "threshold": threshold,
        }
    )


def _abstention(threshold: float) -> ModelResult:
    return ModelResult(
        {
            "label": "unavailable",
            "score": 0.0,
            "threshold": threshold,
        },
        abstained=True,
        abstention_reason="all_samples_invalid",
    )


def _valid_values(item: DenseWindow) -> np.ndarray:
    valid = np.isfinite(item.values)
    if item.validity_mask is not None:
        valid &= item.validity_mask
    return np.asarray(item.values[valid], dtype=float)


def plugin_descriptors() -> tuple[PluginDescriptor, ...]:
    """Return descriptors discovered through the public entry-point contract."""

    config_schema = {
        "$schema": _SCHEMA,
        "type": "object",
        "properties": {"threshold": {"type": "number"}},
        "additionalProperties": False,
    }
    capabilities = PluginCapabilities(
        supported_modes=frozenset(ExecutionMode),
        determinism=Determinism.DETERMINISTIC,
        equivalence=EquivalenceLevel.NUMERIC,
        state_behavior=StateBehavior.STATELESS,
    )
    adaptive_capabilities = PluginCapabilities(
        supported_modes=frozenset(ExecutionMode),
        determinism=Determinism.DETERMINISTIC,
        equivalence=EquivalenceLevel.NUMERIC,
        state_behavior=StateBehavior.SNAPSHOT_RESTORE,
    )
    return (
        PluginDescriptor(
            "eegle.example_models.mean_threshold",
            "1.0.0",
            ComponentKind.MODEL,
            config_schema,
            (PortSpec("window", _DENSE_WINDOW),),
            (PortSpec("prediction", _PREDICTION),),
            capabilities,
            MeanThresholdModel,
            "eegle_example_models:MeanThresholdModel",
            distribution="eegle-example-models",
        ),
        PluginDescriptor(
            "eegle.example_models.peak_threshold",
            "1.0.0",
            ComponentKind.MODEL,
            config_schema,
            (PortSpec("window", _DENSE_WINDOW),),
            (PortSpec("prediction", _PREDICTION),),
            capabilities,
            PeakThresholdModel,
            "eegle_example_models:PeakThresholdModel",
            distribution="eegle-example-models",
        ),
        PluginDescriptor(
            "eegle.example_models.adaptive_mean",
            "1.0.0",
            ComponentKind.MODEL,
            {
                **config_schema,
                "properties": {
                    **config_schema["properties"],
                    "initial_bias": {"type": "number"},
                },
            },
            (PortSpec("window", _DENSE_WINDOW),),
            (PortSpec("prediction", _PREDICTION),),
            adaptive_capabilities,
            AdaptiveMeanModel,
            "eegle_example_models:AdaptiveMeanModel",
            distribution="eegle-example-models",
        ),
    )


__all__ = [
    "AdaptiveMeanModel",
    "MeanThresholdModel",
    "PeakThresholdModel",
    "plugin_descriptors",
]
