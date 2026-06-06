"""Feedback/adaptation messages for the task GUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FeedbackDecision:
    action: str
    intensity: float
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


def decision_from_prediction(label: str, score: float, allow_task_adaptation: bool = True) -> FeedbackDecision:
    if not allow_task_adaptation:
        return FeedbackDecision("observe_only", 0.0, "task adaptation disabled", {"score": score, "label": label})
    if label == "increase_feedback":
        return FeedbackDecision("make_task_easier_or_reward", min(score, 2.0), "model score above threshold", {"score": score})
    return FeedbackDecision("make_task_harder_or_warn", max(0.0, 1.0 - score), "model score below threshold", {"score": score})
