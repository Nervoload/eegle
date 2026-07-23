"""Framework-neutral model contracts, bundles, predictions, roles, and calibration."""

from eegle.models.bundles import (
    ModelBundle,
    file_sha256,
    import_runtime_bundle,
    load_model_bundle,
    load_model_bundle_object,
    snapshot_model_bundle,
    write_model_bundle,
)
from eegle.models.calibration import (
    CalibrationAdapter,
    CalibrationState,
    calibration_state_hash,
    make_prototype_state,
    make_threshold_state,
    read_calibration_state,
    write_calibration_state,
)
from eegle.models.builtins import MeanThresholdModel
from eegle.models.contracts import ModelContract, PreprocessingContract, TargetContract
from eegle.models.predictions import Prediction
from eegle.models.roles import ModelRole, ModelRoleKind


__all__ = [
    "CalibrationAdapter",
    "CalibrationState",
    "ModelBundle",
    "ModelContract",
    "ModelRole",
    "ModelRoleKind",
    "MeanThresholdModel",
    "Prediction",
    "PreprocessingContract",
    "TargetContract",
    "file_sha256",
    "calibration_state_hash",
    "import_runtime_bundle",
    "load_model_bundle",
    "load_model_bundle_object",
    "make_prototype_state",
    "make_threshold_state",
    "read_calibration_state",
    "snapshot_model_bundle",
    "write_calibration_state",
    "write_model_bundle",
]
