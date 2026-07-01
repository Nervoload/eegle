"""EEG foundation and sequence model specifications."""

from __future__ import annotations

from eegle.ml.registry_types import ModelSpec


SPECS = (
    ModelSpec(
        kind="foundation_bendr",
        family="eeg_foundation",
        description="BENDR-style pretrained EEG encoder adapter; user supplies checkpoint.",
        adapter_kind="foundation_bendr",
        train_kind=None,
        trainable=False,
        realtime_supported=False,
        primary_realtime_allowed=False,
        dependencies=("torch",),
        artifact_format="external_checkpoint",
        checkpoint_format="torchscript_or_repo_checkpoint",
        supported_targets=("condition", "attention_lapse_binary", "attention_lapse_score"),
        latency_budget_ms=250.0,
        external_checkpoint=True,
    ),
    ModelSpec(
        kind="foundation_labram",
        family="eeg_foundation",
        description="LaBraM-style channel-patch EEG foundation model adapter; user supplies checkpoint.",
        adapter_kind="foundation_labram",
        train_kind=None,
        trainable=False,
        realtime_supported=False,
        primary_realtime_allowed=False,
        dependencies=("torch",),
        artifact_format="external_checkpoint",
        checkpoint_format="torchscript_or_repo_checkpoint",
        supported_targets=("condition", "attention_lapse_binary", "attention_lapse_score"),
        latency_budget_ms=250.0,
        external_checkpoint=True,
    ),
    ModelSpec(
        kind="sequence_external",
        family="eeg_foundation",
        description="External sequence decoder target for Brain2Qwerty-style experiments.",
        aliases=("brain2qwerty_external",),
        adapter_kind="sequence_external",
        train_kind=None,
        trainable=False,
        realtime_supported=False,
        primary_realtime_allowed=False,
        dependencies=("torch",),
        artifact_format="external_checkpoint",
        checkpoint_format="external_sequence_checkpoint",
        supported_targets=("attention_lapse_binary", "attention_lapse_score"),
        latency_budget_ms=500.0,
        external_checkpoint=True,
    ),
)
