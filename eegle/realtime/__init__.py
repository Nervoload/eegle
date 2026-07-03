"""Realtime closed-loop components."""

from eegle.realtime.epoching import EpochingConfig, ExtractedEpoch, MarkerEvent, RealtimeEpocher
from eegle.realtime.models import ModelAdapter, ModelPrediction
from eegle.realtime.policy import TaskAction, make_decision_policy


__all__ = [
    "EpochingConfig",
    "ExtractedEpoch",
    "MarkerEvent",
    "ModelAdapter",
    "ModelPrediction",
    "RealtimeEpocher",
    "TaskAction",
    "make_decision_policy",
]
