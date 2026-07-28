"""Independent scikit-learn estimator adapter used by the Phase 7 gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import joblib
import numpy as np

from eegle.models import ModelResult
from eegle.plugins import (
    ComponentKind,
    ConstructionAPI,
    Determinism,
    EquivalenceLevel,
    ExecutionMode,
    PluginCapabilities,
    PluginDescriptor,
    PortSpec,
    StateBehavior,
)
from eegle.plugins.construction import ModelConstructionContext
from eegle.streams import TimePoint


FEATURE_VECTOR_SCHEMA = "fixture.phase7.feature_vector.v1"
PREDICTION_SCHEMA = "eegle.prediction.v2"


@dataclass(frozen=True, slots=True)
class FeatureVector:
    record_id: str
    values: tuple[float, ...]
    available_time: TimePoint
    schema: str = FEATURE_VECTOR_SCHEMA

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "record_id": self.record_id,
            "values": list(self.values),
            "available_time": self.available_time.to_payload(),
        }


class SklearnEstimatorAdapter:
    """Thin adapter over a trusted, already-created estimator artifact.

    Joblib artifacts can execute code while loading. The plugin therefore only
    loads the local path supplied by EEGle after digest and size verification;
    operators must still admit the package as trusted executable model data.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        construction: ModelConstructionContext,
    ) -> None:
        artifact_id = str(config.get("artifact_id", "artifact.estimator"))
        try:
            artifact = construction.artifacts[artifact_id]
        except KeyError as exc:
            raise ValueError(f"missing estimator artifact {artifact_id}") from exc
        self.estimator = joblib.load(artifact.location)
        if not callable(getattr(self.estimator, "predict", None)):
            raise TypeError("estimator artifact has no predict method")

    def predict(self, item: FeatureVector, context: Any) -> ModelResult:
        matrix = np.asarray([item.values], dtype=np.float64)
        raw_label = self.estimator.predict(matrix)[0]
        label = raw_label.item() if hasattr(raw_label, "item") else raw_label
        value: dict[str, Any] = {"label": int(label)}
        predict_proba = getattr(self.estimator, "predict_proba", None)
        if callable(predict_proba):
            value["probabilities"] = [
                float(number) for number in predict_proba(matrix)[0]
            ]
        else:
            value["probabilities"] = []
        return ModelResult(value)


def plugin() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="fixture.phase7.sklearn_estimator",
        version="1.0.0",
        kind=ComponentKind.MODEL,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"artifact_id": {"type": "string"}},
            "additionalProperties": False,
        },
        input_ports=(PortSpec("features", FEATURE_VECTOR_SCHEMA, required=False),),
        output_ports=(PortSpec("prediction", PREDICTION_SCHEMA),),
        capabilities=PluginCapabilities(
            supported_modes=frozenset({ExecutionMode.CAUSAL}),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.NUMERIC,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=SklearnEstimatorAdapter,
        implementation="phase7_sklearn_model:SklearnEstimatorAdapter",
        construction_api=ConstructionAPI.MODEL_CONTEXT_V1,
    )
