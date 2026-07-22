"""Register a shadow-only external foundation model spec."""

from __future__ import annotations

from eegle.ml.registry_types import ModelSpec
from eegle.models.registry import register_model_spec


def main() -> None:
    spec = ModelSpec(
        kind="foundation_example_shadow",
        family="eeg_foundation",
        description="Example shadow-only external checkpoint adapter.",
        adapter_kind="sequence_external",
        train_kind=None,
        trainable=False,
        realtime_supported=True,
        primary_realtime_allowed=False,
        dependencies=("torch",),
        artifact_format="external_checkpoint",
        checkpoint_format="user_supplied",
        supported_targets=("condition", "attention_lapse_binary"),
        latency_budget_ms=250.0,
        external_checkpoint=True,
    )
    register_model_spec(spec, replace=True)
    print(spec.payload())


if __name__ == "__main__":
    main()
