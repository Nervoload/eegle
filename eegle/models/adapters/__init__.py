"""Public model-adapter imports."""

from eegle.realtime.models import (
    BaseModelAdapter,
    ErpRoiLogisticRegressionAdapter,
    FoundationBendrAdapter,
    FoundationLabramAdapter,
    ModelAdapter,
    OnnxP300Adapter,
    PyriemannErpCovAdapter,
    SequenceExternalAdapter,
    SklearnFlattenLdaAdapter,
    TorchEEGNetAdapter,
    TorchEpochAdapter,
    TorchShallowConvNetAdapter,
    make_model_adapter,
)

SklearnAdapter = SklearnFlattenLdaAdapter
TorchScriptAdapter = TorchEpochAdapter


__all__ = [
    "BaseModelAdapter",
    "ErpRoiLogisticRegressionAdapter",
    "FoundationBendrAdapter",
    "FoundationLabramAdapter",
    "ModelAdapter",
    "OnnxP300Adapter",
    "PyriemannErpCovAdapter",
    "SequenceExternalAdapter",
    "SklearnAdapter",
    "SklearnFlattenLdaAdapter",
    "TorchEEGNetAdapter",
    "TorchEpochAdapter",
    "TorchScriptAdapter",
    "TorchShallowConvNetAdapter",
    "make_model_adapter",
]
