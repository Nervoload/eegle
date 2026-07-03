"""Stable model APIs for EEGle bundles, contracts, registries, and calibration."""

from eegle.models.bundles import ModelBundle, file_sha256, load_model_bundle, load_model_bundle_object, snapshot_model_bundle, write_model_bundle
from eegle.models.calibration import CalibrationAdapter, CalibrationState, read_calibration_state, write_calibration_state
from eegle.models.contracts import ModelContract, PreprocessingContract, TargetContract
from eegle.models.registry import (
    get_model_spec,
    list_model_kinds,
    list_model_specs,
    load_entry_point_model_specs,
    register_model_spec,
    resolve_model_kind,
    unregister_model_spec,
)
from eegle.ml.registry_types import ModelSpec
from eegle.realtime.models import ModelAdapter, ModelPrediction


__all__ = [
    "CalibrationAdapter",
    "CalibrationState",
    "ModelAdapter",
    "ModelBundle",
    "ModelContract",
    "ModelPrediction",
    "ModelSpec",
    "PreprocessingContract",
    "TargetContract",
    "file_sha256",
    "get_model_spec",
    "list_model_kinds",
    "list_model_specs",
    "load_entry_point_model_specs",
    "load_model_bundle",
    "load_model_bundle_object",
    "read_calibration_state",
    "register_model_spec",
    "resolve_model_kind",
    "snapshot_model_bundle",
    "unregister_model_spec",
    "write_calibration_state",
    "write_model_bundle",
]
