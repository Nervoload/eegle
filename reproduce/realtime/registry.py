"""Registries for realtime preprocessing, models, and feedback emitters."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from reproduce.realtime.emitters import DisabledFeedbackEmitter, JsonlFeedbackEmitter, LslFeedbackEmitter
from reproduce.realtime.models import BaseModelAdapter, make_model_adapter
from reproduce.realtime.policy import ConservativeDecisionPolicy, make_decision_policy
from reproduce.realtime.preprocessing import CausalBandpassNotchPreprocessor


def make_stream_preprocessor(
    kind: str,
    sample_rate_hz: float,
    channel_count: int,
    config: dict[str, Any],
) -> CausalBandpassNotchPreprocessor:
    if kind in {"causal_bandpass_notch", "default"}:
        return CausalBandpassNotchPreprocessor(sample_rate_hz, channel_count, config)
    raise NotImplementedError(f"realtime preprocessor '{kind}' is not implemented")


def make_model(kind: str, config: dict[str, Any]) -> BaseModelAdapter:
    return make_model_adapter(kind, config)


def make_policy(kind: str, config: dict[str, Any]) -> ConservativeDecisionPolicy:
    return make_decision_policy(kind, config)


def make_feedback_emitter(kind: str, config: dict[str, Any], feedback_jsonl: str | Path) -> Any:
    if kind in {"disabled", "none"}:
        return DisabledFeedbackEmitter()
    if kind == "jsonl":
        return JsonlFeedbackEmitter(feedback_jsonl)
    if kind == "lsl":
        return LslFeedbackEmitter(
            name=config.get("lsl_stream_name", "ClosedLoopFeedback"),
            stream_type=config.get("lsl_stream_type", "Feedback"),
        )
    raise NotImplementedError(f"feedback emitter '{kind}' is not implemented")
