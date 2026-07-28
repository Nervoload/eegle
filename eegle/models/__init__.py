"""Framework-neutral model contracts, manifests, predictions, and roles.

The public symbols are loaded lazily so compiler plans may depend on canonical
model records without making ``models`` import the recording/runtime graph.
Legacy training, framework-family, registry, and executable-bundle helpers are
not part of this package-level surface. Deterministic package transport remains
framework-neutral and is loaded only when selected.
"""

from importlib import import_module
from typing import Any


__all__ = [
    "CalibrationArtifact",
    "CallableModel",
    "MODEL_PACKAGE_CHECK_SCHEMA_ID",
    "MODEL_PACKAGE_MEDIA_TYPE",
    "MODEL_PACKAGE_SCHEMA_ID",
    "PACKED_MODEL_SCHEMA_ID",
    "ModelContract",
    "ModelImplementationRequirement",
    "ModelInputContract",
    "ModelManifest",
    "ModelOutputContract",
    "ModelArtifactSource",
    "ModelPackageCheckReport",
    "ModelPackageIndex",
    "ModelPackageIntegrityError",
    "ModelPackageIssue",
    "ModelResult",
    "ModelStateBehavior",
    "ModelStateContract",
    "ModelStateArtifact",
    "Prediction",
    "PackedModel",
    "PreprocessingOwnership",
    "PreprocessingRequirement",
    "SyntheticModelTestVector",
    "SYNTHETIC_TEST_VECTOR_SCHEMA_ID",
    "SYNTHETIC_TEST_VECTOR_SET_SCHEMA_ID",
    "VerifiedModelPackage",
    "MaterializedModelPackage",
    "build_model_manifest",
    "build_model_package",
    "check_model_package",
    "load_synthetic_test_vectors",
    "materialize_model_package",
    "open_model_package",
    "pack_model",
]


_EXPORTS = {
    "CalibrationArtifact": ("eegle.models.calibration", "CalibrationArtifact"),
    "CallableModel": ("eegle.models.callable", "CallableModel"),
    "MODEL_PACKAGE_CHECK_SCHEMA_ID": (
        "eegle.models.packaging",
        "MODEL_PACKAGE_CHECK_SCHEMA_ID",
    ),
    "MODEL_PACKAGE_MEDIA_TYPE": (
        "eegle.models.packaging",
        "MODEL_PACKAGE_MEDIA_TYPE",
    ),
    "MODEL_PACKAGE_SCHEMA_ID": (
        "eegle.models.packaging",
        "MODEL_PACKAGE_SCHEMA_ID",
    ),
    "PACKED_MODEL_SCHEMA_ID": (
        "eegle.models.packaging",
        "PACKED_MODEL_SCHEMA_ID",
    ),
    "ModelContract": ("eegle.models.contracts", "ModelContract"),
    "ModelImplementationRequirement": (
        "eegle.models.manifests",
        "ModelImplementationRequirement",
    ),
    "ModelInputContract": ("eegle.models.contracts", "ModelInputContract"),
    "ModelManifest": ("eegle.models.manifests", "ModelManifest"),
    "ModelOutputContract": ("eegle.models.contracts", "ModelOutputContract"),
    "ModelArtifactSource": ("eegle.models.packaging", "ModelArtifactSource"),
    "ModelPackageCheckReport": (
        "eegle.models.packaging",
        "ModelPackageCheckReport",
    ),
    "ModelPackageIndex": ("eegle.models.packaging", "ModelPackageIndex"),
    "ModelPackageIntegrityError": (
        "eegle.models.packaging",
        "ModelPackageIntegrityError",
    ),
    "ModelPackageIssue": ("eegle.models.packaging", "ModelPackageIssue"),
    "ModelResult": ("eegle.models.results", "ModelResult"),
    "ModelStateBehavior": ("eegle.models.contracts", "ModelStateBehavior"),
    "ModelStateContract": ("eegle.models.contracts", "ModelStateContract"),
    "ModelStateArtifact": ("eegle.models.state_artifacts", "ModelStateArtifact"),
    "Prediction": ("eegle.models.predictions", "Prediction"),
    "PackedModel": ("eegle.models.packaging", "PackedModel"),
    "PreprocessingOwnership": (
        "eegle.models.contracts",
        "PreprocessingOwnership",
    ),
    "PreprocessingRequirement": (
        "eegle.models.contracts",
        "PreprocessingRequirement",
    ),
    "SyntheticModelTestVector": (
        "eegle.models.packaging",
        "SyntheticModelTestVector",
    ),
    "SYNTHETIC_TEST_VECTOR_SCHEMA_ID": (
        "eegle.models.packaging",
        "SYNTHETIC_TEST_VECTOR_SCHEMA_ID",
    ),
    "SYNTHETIC_TEST_VECTOR_SET_SCHEMA_ID": (
        "eegle.models.packaging",
        "SYNTHETIC_TEST_VECTOR_SET_SCHEMA_ID",
    ),
    "VerifiedModelPackage": (
        "eegle.models.packaging",
        "VerifiedModelPackage",
    ),
    "MaterializedModelPackage": (
        "eegle.models.packaging",
        "MaterializedModelPackage",
    ),
    "build_model_manifest": ("eegle.models.packaging", "build_model_manifest"),
    "build_model_package": ("eegle.models.packaging", "build_model_package"),
    "check_model_package": ("eegle.models.packaging", "check_model_package"),
    "load_synthetic_test_vectors": (
        "eegle.models.packaging",
        "load_synthetic_test_vectors",
    ),
    "materialize_model_package": (
        "eegle.models.packaging",
        "materialize_model_package",
    ),
    "open_model_package": ("eegle.models.packaging", "open_model_package"),
    "pack_model": ("eegle.models.packaging", "pack_model"),
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
