"""Schema identifiers for EEGle public artifacts."""

from eegle.models.bundles import BUNDLE_SCHEMA
from eegle.models.calibration import CALIBRATION_STATE_SCHEMA
from eegle.models.contracts import MODEL_CONTRACT_SCHEMA
from eegle.realtime.classification import PREDICTION_SCHEMA


SESSION_MANIFEST_SCHEMA = "eegle.session_manifest.v1"
EPOCH_DATASET_SCHEMA = "eegle.epoch_dataset.v1"


__all__ = [
    "BUNDLE_SCHEMA",
    "CALIBRATION_STATE_SCHEMA",
    "EPOCH_DATASET_SCHEMA",
    "MODEL_CONTRACT_SCHEMA",
    "PREDICTION_SCHEMA",
    "SESSION_MANIFEST_SCHEMA",
]
