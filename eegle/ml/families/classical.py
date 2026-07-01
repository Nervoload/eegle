"""Classical EEG model specifications."""

from __future__ import annotations

from eegle.ml.registry_types import ModelSpec


SPECS = (
    ModelSpec(
        kind="erp_roi_logreg",
        family="classical",
        description="Baseline-corrected ERP ROI features with logistic regression.",
        adapter_kind="erp_roi_logreg",
        train_kind="erp_roi_logreg",
        trainable=True,
        realtime_supported=True,
        primary_realtime_allowed=True,
        dependencies=("sklearn", "joblib"),
        artifact_format="joblib",
        checkpoint_format="joblib",
        supported_targets=("condition", "attention_lapse_binary", "attention_lapse_score"),
        latency_budget_ms=50.0,
    ),
    ModelSpec(
        kind="pyriemann_erp_cov",
        family="classical",
        description="pyRiemann xDAWN covariance plus tangent-space logistic regression.",
        adapter_kind="pyriemann_erp_cov",
        train_kind="pyriemann_erp_cov",
        trainable=True,
        realtime_supported=True,
        primary_realtime_allowed=True,
        dependencies=("sklearn", "joblib", "pyriemann"),
        artifact_format="joblib",
        checkpoint_format="joblib",
        supported_targets=("condition", "attention_lapse_binary", "attention_lapse_score"),
        latency_budget_ms=50.0,
    ),
    ModelSpec(
        kind="sklearn_flatten_lda",
        family="classical",
        description="Flattened baseline-corrected epoch LDA baseline.",
        aliases=("sklearn_xdawn_lda",),
        adapter_kind="sklearn_flatten_lda",
        train_kind="sklearn_flatten_lda",
        trainable=True,
        realtime_supported=True,
        primary_realtime_allowed=True,
        dependencies=("sklearn", "joblib"),
        artifact_format="joblib",
        checkpoint_format="joblib",
        supported_targets=("condition", "attention_lapse_binary", "attention_lapse_score"),
        latency_budget_ms=50.0,
    ),
)
