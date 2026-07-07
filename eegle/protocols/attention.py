"""Attention-lapse protocol declarations."""

from __future__ import annotations

from eegle.protocols.spec import ProtocolTarget, ScientificProtocol


def attention_lapse_protocol() -> ScientificProtocol:
    """Return the v1 observe-only attention-lapse system-test protocol."""
    return ScientificProtocol(
        name="attention_lapse_go_nogo8_v1",
        task="go_nogo",
        primary_endpoint="attention_lapse_risk",
        prediction_window_seconds=(-2.0, 0.0),
        prediction_horizon="same_trial_response",
        targets=(
            ProtocolTarget(
                "attention_lapse_binary",
                positive=("slow_go_rt",),
                metadata={
                    "primary_label": "slow_go_rt",
                    "slow_rt_quantile": 0.8,
                    "secondary_labels": ["omission_error", "commission_error", "composite_lapse"],
                },
            ),
        ),
        splits=("temporal_support_query", "practice_excluded"),
        baselines=(
            "causal_bandpower_logreg",
            "riemann_tangent_logreg",
            "torch_eegnet",
            "foundation_head_logreg_shadow",
            "foundation_prototype_shadow",
        ),
        metrics=(
            "auprc",
            "roc_auc",
            "brier_score",
            "ece",
            "false_alarms_per_minute",
            "coverage",
            "latency_ms",
        ),
        metadata={
            "observe_only": True,
            "allow_task_adaptation": False,
            "allow_stimulation": False,
            "epoch_data_source": "causal_preprocessed",
            "support_query_default_grid": [0, 20, 50, 100],
            "online_support_trials_default": 50,
            "external_workbench": "eegle-model",
            "runtime_import_boundary": "hashed_eegle_runtime_zip",
        },
    )
