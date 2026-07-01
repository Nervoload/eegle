"""CNN EEG model specifications."""

from __future__ import annotations

from eegle.ml.registry_types import ModelSpec


SPECS = (
    ModelSpec(
        kind="torch_eegnet",
        family="cnn",
        description="Trainable TorchScript EEGNet-style compact convolutional epoch model.",
        aliases=("cnn_eegnet",),
        adapter_kind="torch_eegnet",
        train_kind="torch_eegnet",
        trainable=True,
        realtime_supported=True,
        primary_realtime_allowed=True,
        dependencies=("torch",),
        artifact_format="torchscript",
        checkpoint_format="torchscript",
        supported_targets=("condition", "attention_lapse_binary", "attention_lapse_score"),
        latency_budget_ms=50.0,
    ),
    ModelSpec(
        kind="torch_shallowconvnet",
        family="cnn",
        description="External TorchScript shallow ConvNet-style epoch model.",
        aliases=("cnn_shallowconvnet",),
        adapter_kind="torch_shallowconvnet",
        train_kind=None,
        trainable=False,
        realtime_supported=True,
        primary_realtime_allowed=False,
        dependencies=("torch",),
        artifact_format="torchscript",
        checkpoint_format="torchscript",
        supported_targets=("condition", "attention_lapse_binary", "attention_lapse_score"),
        latency_budget_ms=100.0,
    ),
)
