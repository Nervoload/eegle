"""Closed-loop controller used by simulated CLI smoke runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from eegle.realtime.models import ModelPrediction
from eegle.realtime.policy import TaskAction
from eegle.realtime.preprocessing import preprocess_window
from eegle.realtime.registry import make_model, make_policy


@dataclass
class ControllerResult:
    prediction: ModelPrediction
    actions: list[TaskAction]

    @property
    def prediction_label(self) -> str:
        return self.prediction.label

    @property
    def prediction_score(self) -> float:
        return self.prediction.score


class ClosedLoopController:
    def __init__(self, config: dict[str, Any], sample_rate_hz: float) -> None:
        self.config = config
        self.sample_rate_hz = sample_rate_hz
        realtime = config.get("realtime", {})
        self.preprocessing = realtime.get("preprocessing", {})
        model_config = realtime.get("model", {})
        self.model = make_model(str(model_config.get("kind", "erp_peak_baseline")), model_config)
        feedback_config = realtime.get("feedback", {})
        policy_config = dict(realtime.get("decision_policy", {}))
        policy_config.setdefault("allow_task_adaptation", bool(feedback_config.get("allow_task_adaptation", True)))
        self.policy = make_policy(str(policy_config.get("kind", "conservative_p300")), policy_config)

    def process_window(self, data: np.ndarray) -> ControllerResult:
        processed = preprocess_window(data, self.sample_rate_hz, self.preprocessing)
        channel_names = [f"ch_{idx + 1:03d}" for idx in range(processed.shape[1])]
        prediction = self.model.predict_epoch(
            processed,
            self.sample_rate_hz,
            channel_names,
            {"source": "simulated_window", "epoch_window_seconds": [-0.2, 0.8]},
        )
        actions = self.policy.decide(prediction, {"source": "simulated_window"})
        return ControllerResult(prediction, actions)
