"""Classical model adapter imports."""

from eegle.realtime.models import ErpRoiLogisticRegressionAdapter, PyriemannErpCovAdapter, SklearnFlattenLdaAdapter

SklearnAdapter = SklearnFlattenLdaAdapter


__all__ = ["ErpRoiLogisticRegressionAdapter", "PyriemannErpCovAdapter", "SklearnAdapter", "SklearnFlattenLdaAdapter"]
