"""Framework-neutral model contracts, manifests, predictions, and roles.

The public symbols are loaded lazily so compiler plans may depend on canonical
model records without making ``models`` import the recording/runtime graph.
Legacy training, framework-family, and local bundle materialization helpers are
not part of this package-level surface.
"""

from importlib import import_module
from typing import Any


__all__ = [
    "CalibrationArtifact",
    "CallableModel",
    "ModelContract",
    "ModelImplementationRequirement",
    "ModelInputContract",
    "ModelManifest",
    "ModelOutputContract",
    "ModelResult",
    "ModelStateBehavior",
    "ModelStateContract",
    "ModelStateArtifact",
    "Prediction",
    "PreprocessingOwnership",
    "PreprocessingRequirement",
]


_EXPORTS = {
    "CalibrationArtifact": ("eegle.models.calibration", "CalibrationArtifact"),
    "CallableModel": ("eegle.models.callable", "CallableModel"),
    "ModelContract": ("eegle.models.contracts", "ModelContract"),
    "ModelImplementationRequirement": (
        "eegle.models.manifests",
        "ModelImplementationRequirement",
    ),
    "ModelInputContract": ("eegle.models.contracts", "ModelInputContract"),
    "ModelManifest": ("eegle.models.manifests", "ModelManifest"),
    "ModelOutputContract": ("eegle.models.contracts", "ModelOutputContract"),
    "ModelResult": ("eegle.models.results", "ModelResult"),
    "ModelStateBehavior": ("eegle.models.contracts", "ModelStateBehavior"),
    "ModelStateContract": ("eegle.models.contracts", "ModelStateContract"),
    "ModelStateArtifact": ("eegle.models.state_artifacts", "ModelStateArtifact"),
    "Prediction": ("eegle.models.predictions", "Prediction"),
    "PreprocessingOwnership": (
        "eegle.models.contracts",
        "PreprocessingOwnership",
    ),
    "PreprocessingRequirement": (
        "eegle.models.contracts",
        "PreprocessingRequirement",
    ),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    value = getattr(import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
