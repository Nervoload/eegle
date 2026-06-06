"""Decision policies that convert model predictions into bounded task actions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import monotonic
from typing import Any

from reproduce.realtime.models import ModelPrediction


DEFAULT_ACTIONS = (
    "increase_no_go_probability",
    "adjust_isi",
    "repeat_condition",
    "show_reward",
    "set_visual_alpha",
    "observe_only",
)


@dataclass
class TaskAction:
    """Explicit task action emitted by a decision policy and consumed by tasks."""

    action: str
    boundary: str
    reason: str
    action_id: str
    value: Any = None
    parameters: dict[str, Any] = field(default_factory=dict)
    source_model: str | None = None
    source_label: str | None = None
    source_score: float | None = None
    source_probability: float | None = None
    source_epoch_index: int | None = None
    source_trial: int | None = None
    target_trial_index: int | None = None
    block_index: int | None = None
    created_at_monotonic: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = 1
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TaskAction":
        return cls(
            action=str(payload["action"]),
            boundary=str(payload.get("boundary", "between_trials")),
            reason=str(payload.get("reason", "")),
            action_id=str(payload.get("action_id") or f"external:{payload.get('action')}:{payload.get('created_at_monotonic')}"),
            value=payload.get("value"),
            parameters=dict(payload.get("parameters") or {}),
            source_model=payload.get("source_model"),
            source_label=payload.get("source_label"),
            source_score=_optional_float(payload.get("source_score")),
            source_probability=_optional_float(payload.get("source_probability")),
            source_epoch_index=_optional_int(payload.get("source_epoch_index")),
            source_trial=_optional_int(payload.get("source_trial")),
            target_trial_index=_optional_int(payload.get("target_trial_index")),
            block_index=_optional_int(payload.get("block_index")),
            created_at_monotonic=_optional_float(payload.get("created_at_monotonic")),
            metadata=dict(payload.get("metadata") or {}),
        )


class ConservativeDecisionPolicy:
    """Conservative P300 policy with clamped values and trial cooldowns."""

    kind = "conservative_p300"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        self.enabled = bool(self.config.get("enabled", True))
        self.allow_task_adaptation = bool(self.config.get("allow_task_adaptation", True))
        self.actions = tuple(str(action) for action in self.config.get("actions", DEFAULT_ACTIONS))
        self.bounds = dict(self.config.get("bounds") or {})
        self.cooldowns = dict(self.config.get("cooldowns") or {})
        self.cooldown_trials = int(self.cooldowns.get("trials", self.config.get("cooldown_trials", 2)))
        self.probability_threshold = float(self.config.get("probability_threshold", 0.6))
        self.lower_probability_threshold = float(self.config.get("lower_probability_threshold", 0.4))
        self.no_go_probability_step = float(self.config.get("no_go_probability_step", 0.05))
        self.isi_step_seconds = float(self.config.get("isi_step_seconds", 0.05))
        self.visual_alpha_value = float(self.config.get("visual_alpha_value", 0.85))
        self.boundary = str(self.config.get("boundary", "between_trials"))
        self._last_action_trial: dict[str, int] = {}
        self._sequence = 0

    def decide(self, prediction: ModelPrediction, epoch_metadata: dict[str, Any]) -> list[TaskAction]:
        source_trial = _optional_int(epoch_metadata.get("trial"))
        probability = _prediction_probability(prediction)
        if not self.enabled or not self.allow_task_adaptation:
            return [self._observe(prediction, epoch_metadata, "task adaptation disabled")]

        if probability >= self.probability_threshold:
            for action in ("increase_no_go_probability", "set_visual_alpha", "show_reward", "repeat_condition"):
                if action in self.actions and self._cooldown_ok(action, source_trial):
                    return [self._make_action(action, prediction, epoch_metadata, source_trial, probability)]
        elif probability <= self.lower_probability_threshold and "adjust_isi" in self.actions and self._cooldown_ok("adjust_isi", source_trial):
            return [self._make_action("adjust_isi", prediction, epoch_metadata, source_trial, probability)]
        return [self._observe(prediction, epoch_metadata, "prediction inside observe band")]

    def _make_action(
        self,
        action: str,
        prediction: ModelPrediction,
        epoch_metadata: dict[str, Any],
        source_trial: int | None,
        probability: float,
    ) -> TaskAction:
        self._sequence += 1
        if source_trial is not None:
            self._last_action_trial[action] = source_trial
        params: dict[str, Any] = {}
        value: Any = None
        reason = f"{prediction.label} probability {probability:.3f}"
        if action == "increase_no_go_probability":
            low, high = self._bounds("no_go_probability", [0.15, 0.45])
            value = clamp(self.no_go_probability_step, -abs(high - low), abs(high - low))
            params = {"delta": value, "min": low, "max": high}
        elif action == "adjust_isi":
            low, high = self._bounds("isi_seconds", [0.5, 1.25])
            value = self.isi_step_seconds
            params = {"delta": value, "min": low, "max": high}
        elif action == "set_visual_alpha":
            low, high = self._bounds("visual_alpha", [0.5, 1.0])
            value = clamp(self.visual_alpha_value, low, high)
            params = {"min": low, "max": high}
        elif action == "repeat_condition":
            value = epoch_metadata.get("condition")
            params = {"condition": value}
        elif action == "show_reward":
            value = True

        return TaskAction(
            action=action,
            boundary=self.boundary,
            reason=reason,
            action_id=self._action_id(prediction, epoch_metadata, action),
            value=value,
            parameters=params,
            source_model=prediction.model_kind,
            source_label=prediction.label,
            source_score=prediction.score,
            source_probability=prediction.probability,
            source_epoch_index=_optional_int(epoch_metadata.get("epoch_index")),
            source_trial=source_trial,
            target_trial_index=None if source_trial is None else source_trial + 1,
            created_at_monotonic=monotonic(),
            metadata={"policy": self.kind},
        )

    def _observe(self, prediction: ModelPrediction, epoch_metadata: dict[str, Any], reason: str) -> TaskAction:
        self._sequence += 1
        source_trial = _optional_int(epoch_metadata.get("trial"))
        return TaskAction(
            action="observe_only",
            boundary=self.boundary,
            reason=reason,
            action_id=self._action_id(prediction, epoch_metadata, "observe_only"),
            source_model=prediction.model_kind,
            source_label=prediction.label,
            source_score=prediction.score,
            source_probability=prediction.probability,
            source_epoch_index=_optional_int(epoch_metadata.get("epoch_index")),
            source_trial=source_trial,
            target_trial_index=None if source_trial is None else source_trial + 1,
            created_at_monotonic=monotonic(),
            metadata={"policy": self.kind},
        )

    def _cooldown_ok(self, action: str, source_trial: int | None) -> bool:
        if source_trial is None or self.cooldown_trials <= 0:
            return True
        previous = self._last_action_trial.get(action)
        return previous is None or source_trial - previous >= self.cooldown_trials

    def _bounds(self, name: str, default: list[float]) -> tuple[float, float]:
        raw = self.bounds.get(name, default)
        low, high = float(raw[0]), float(raw[1])
        return (low, high) if low <= high else (high, low)

    def _action_id(self, prediction: ModelPrediction, epoch_metadata: dict[str, Any], action: str) -> str:
        epoch_index = epoch_metadata.get("epoch_index", "none")
        return f"{self.kind}:{prediction.model_kind}:{epoch_index}:{action}:{self._sequence}"


class ObserveOnlyPolicy(ConservativeDecisionPolicy):
    kind = "observe_only"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__({**dict(config or {}), "allow_task_adaptation": False})


def make_decision_policy(kind: str, config: dict[str, Any] | None = None) -> ConservativeDecisionPolicy:
    normalized = (kind or "conservative_p300").lower()
    if normalized in {"default", "conservative_p300"}:
        return ConservativeDecisionPolicy(config)
    if normalized in {"observe_only", "disabled"}:
        return ObserveOnlyPolicy(config)
    raise NotImplementedError(f"decision policy '{kind}' is not implemented")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _prediction_probability(prediction: ModelPrediction) -> float:
    if prediction.probability is not None:
        return clamp(float(prediction.probability), 0.0, 1.0)
    return clamp(float(prediction.score), 0.0, 1.0)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
