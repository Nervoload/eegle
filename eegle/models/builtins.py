"""Small framework-neutral models used by the reference execution slice."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from eegle._domain import Lineage
from eegle.models.predictions import Prediction
from eegle.processing.windows import DenseWindow
from eegle.streams.clocks import TimePoint

if TYPE_CHECKING:
    from eegle.plugins.contracts import ExecutionContext


class MeanThresholdModel:
    """Classify a dense window using its finite-value mean.

    This model is intentionally scientifically uninteresting.  It provides a
    deterministic, modality-neutral executable for engine and plugin contract
    tests without introducing sklearn or Torch into the base runtime.
    """

    def __init__(
        self,
        *,
        model_id: str,
        role: str,
        threshold: float = 0.0,
        negative_label: str = "negative",
        positive_label: str = "positive",
        latency_seconds: float = 0.0,
    ) -> None:
        self.model_id = str(model_id)
        self.role = str(role)
        self.threshold = float(threshold)
        self.negative_label = str(negative_label)
        self.positive_label = str(positive_label)
        self.latency_seconds = float(latency_seconds)
        if not np.isfinite(self.threshold):
            raise ValueError("threshold must be finite")
        if not np.isfinite(self.latency_seconds) or self.latency_seconds < 0:
            raise ValueError("latency_seconds must be finite and non-negative")
        if not self.negative_label or not self.positive_label:
            raise ValueError("model labels cannot be empty")

    def predict(self, item: Any, context: "ExecutionContext") -> Prediction:
        if not isinstance(item, DenseWindow):
            raise TypeError("mean threshold model requires DenseWindow input")
        if context.current_time.clock_id != item.available_time.clock_id:
            raise ValueError("model context and window availability must share a clock")
        if context.current_time.seconds < item.available_time.seconds:
            raise ValueError("model cannot consume a window before it is available")
        valid = np.isfinite(item.values)
        if item.validity_mask is not None:
            valid &= item.validity_mask
        if not bool(np.any(valid)):
            raise ValueError("mean threshold model requires at least one valid value")
        score = float(np.mean(item.values[valid]))
        label = self.positive_label if score >= self.threshold else self.negative_label
        produced = TimePoint(
            context.current_time.seconds + self.latency_seconds,
            context.current_time.clock_id,
        )
        lineage = Lineage(
            component_id=context.component_id,
            component_version=context.component_version,
            input_ids=item.input_ids,
            latest_input_available_time=item.available_time,
            clock_mapping_revisions=item.lineage.clock_mapping_revisions,
            stream_revisions=item.lineage.stream_revisions,
        )
        return Prediction(
            prediction_id=context.next_id("prediction"),
            model_id=self.model_id,
            role=self.role,
            outputs={"label": label, "score": score, "threshold": self.threshold},
            produced_time=produced,
            available_time=produced,
            input_ids=item.input_ids,
            lineage=lineage,
        )
