"""EEGle: reproducible realtime EEG/BCI experiment infrastructure."""

from eegle.core import Session, SessionPaths
from eegle.models import (
    CalibrationAdapter,
    CalibrationState,
    ModelBundle,
    ModelContract,
    ModelPrediction,
    ModelSpec,
)
from eegle.realtime.epoching import EpochingConfig, ExtractedEpoch, MarkerEvent
from eegle.realtime.policy import TaskAction
from eegle.analysis.classification import ReplayResult


__all__ = [
    "CalibrationAdapter",
    "CalibrationState",
    "EpochingConfig",
    "ExtractedEpoch",
    "MarkerEvent",
    "ModelBundle",
    "ModelContract",
    "ModelPrediction",
    "ModelSpec",
    "ReplayResult",
    "Session",
    "SessionPaths",
    "TaskAction",
    "__version__",
]

__version__ = "0.1.0"
