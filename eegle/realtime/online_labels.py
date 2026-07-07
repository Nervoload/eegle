"""Causal online behavior labels for attention-lapse adaptation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


LABEL_SCHEMA = "eegle.online_label.v1"


@dataclass
class OnlineLabelResult:
    status: str
    trial: int | None
    label: int | None = None
    label_name: str = "attention_lapse"
    label_mode: str = "slow_go_rt"
    threshold_value: float | None = None
    reason: str | None = None
    used_for_adaptation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    schema: str = LABEL_SCHEMA

    def payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "status": self.status,
            "trial": self.trial,
            "label": self.label,
            "label_name": self.label_name,
            "label_mode": self.label_mode,
            "threshold_value": self.threshold_value,
            "reason": self.reason,
            "used_for_adaptation": self.used_for_adaptation,
            "metadata": self.metadata,
        }


class OnlineAttentionLapseLabeler:
    """Compute delayed labels from trial-complete events without future trials."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.label_mode = str(cfg.get("label_mode", cfg.get("attention_lapse_label", "slow_go_rt")))
        self.slow_rt_quantile = float(cfg.get("slow_rt_quantile", 0.8))
        self.min_correct_go_rts = max(0, int(cfg.get("min_correct_go_rts_for_threshold", 20)))
        self.warmup_trials = max(0, int(cfg.get("warmup_trials", 0)))
        self.correct_go_rts: list[float] = [
            float(value)
            for value in cfg.get("initial_correct_go_rts", [])
            if value is not None and np.isfinite(float(value))
        ]
        self.seen_trials: set[int] = set()

    def label_trial_complete(self, event: dict[str, Any]) -> OnlineLabelResult:
        trial = _optional_int(event.get("trial"))
        if trial is None:
            return self._result("skipped", None, reason="missing_trial")
        if trial in self.seen_trials:
            return self._result("skipped", trial, reason="duplicate_trial_complete")
        self.seen_trials.add(trial)
        if bool(event.get("practice", False)) or trial < 1:
            self._observe_after_label(event)
            return self._result("skipped", trial, reason="practice_trial")
        if trial <= self.warmup_trials:
            self._observe_after_label(event)
            return self._result("skipped", trial, reason="warmup_trial")

        mode = self.label_mode
        if mode in {"slow_go_rt", "slow_trial", "attention_lapse_slow_rt", "go_only_slow_rt"}:
            result = self._slow_go_rt_label(event, go_only=(mode == "go_only_slow_rt"))
        elif mode in {"omission", "omission_error"}:
            result = self._binary_event_label(event, "omission_error")
        elif mode in {"commission", "commission_error"}:
            result = self._binary_event_label(event, "commission_error")
        elif mode in {"composite", "composite_lapse"}:
            result = self._composite_label(event)
        else:
            result = self._result("skipped", trial, reason=f"unsupported_label_mode:{mode}")
        self._observe_after_label(event)
        return result

    def _slow_go_rt_label(self, event: dict[str, Any], *, go_only: bool) -> OnlineLabelResult:
        trial = _optional_int(event.get("trial"))
        if bool(event.get("is_no_go", False)):
            return self._result("skipped", trial, reason="not_go_trial" if go_only else "slow_go_rt_requires_correct_go")
        if not bool(event.get("correct", event.get("correct_press", False))):
            return self._result("skipped", trial, reason="slow_go_rt_requires_correct_go")
        rt = _optional_float(event.get("reaction_time_seconds"))
        if rt is None:
            return self._result("skipped", trial, reason="missing_reaction_time")
        required_rts = max(1, self.min_correct_go_rts)
        if len(self.correct_go_rts) < required_rts:
            return self._result(
                "skipped",
                trial,
                reason="insufficient_past_correct_go_rts",
                metadata={"past_correct_go_rt_count": len(self.correct_go_rts), "required": required_rts},
            )
        threshold = float(np.quantile(np.asarray(self.correct_go_rts, dtype=float), self.slow_rt_quantile))
        return self._result(
            "labeled",
            trial,
            label=int(rt >= threshold),
            threshold_value=threshold,
            used_for_adaptation=True,
            metadata={"past_correct_go_rt_count": len(self.correct_go_rts), "reaction_time_seconds": rt},
        )

    def _binary_event_label(self, event: dict[str, Any], field: str) -> OnlineLabelResult:
        trial = _optional_int(event.get("trial"))
        value = event.get(field)
        if value is None:
            return self._result("skipped", trial, reason=f"missing_{field}")
        return self._result("labeled", trial, label=int(bool(value)), used_for_adaptation=True)

    def _composite_label(self, event: dict[str, Any]) -> OnlineLabelResult:
        trial = _optional_int(event.get("trial"))
        value = bool(event.get("omission_error")) or bool(event.get("commission_error"))
        if not bool(event.get("is_no_go", False)) and bool(event.get("correct", event.get("correct_press", False))):
            rt = _optional_float(event.get("reaction_time_seconds"))
            required_rts = max(1, self.min_correct_go_rts)
            if rt is not None and len(self.correct_go_rts) >= required_rts:
                threshold = float(np.quantile(np.asarray(self.correct_go_rts, dtype=float), self.slow_rt_quantile))
                value = value or rt >= threshold
                return self._result("labeled", trial, label=int(value), threshold_value=threshold, used_for_adaptation=True)
            if rt is not None and len(self.correct_go_rts) < required_rts:
                return self._result("skipped", trial, reason="insufficient_past_correct_go_rts")
        return self._result("labeled", trial, label=int(value), used_for_adaptation=True)

    def _observe_after_label(self, event: dict[str, Any]) -> None:
        if bool(event.get("is_no_go", False)):
            return
        if not bool(event.get("correct", event.get("correct_press", False))):
            return
        rt = _optional_float(event.get("reaction_time_seconds"))
        if rt is not None:
            self.correct_go_rts.append(rt)

    def _result(
        self,
        status: str,
        trial: int | None,
        *,
        label: int | None = None,
        threshold_value: float | None = None,
        reason: str | None = None,
        used_for_adaptation: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> OnlineLabelResult:
        return OnlineLabelResult(
            status=status,
            trial=trial,
            label=label,
            label_mode=self.label_mode,
            threshold_value=threshold_value,
            reason=reason,
            used_for_adaptation=used_for_adaptation,
            metadata=dict(metadata or {}),
        )


def trial_complete_from_event_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a task event JSONL row into a trial-complete payload."""
    if record.get("label") != "go_nogo_trial_complete":
        return None
    metadata = dict(record.get("metadata") or {})
    return {
        **metadata,
        "trial": _optional_int(record.get("trial", metadata.get("trial"))),
        "event_timestamp_monotonic": record.get("timestamp"),
        "event_record_schema": "eegle.task_event_jsonl",
    }


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
