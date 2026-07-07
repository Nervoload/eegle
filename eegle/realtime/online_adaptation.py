"""Online adaptation state and delayed-label update helpers."""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any


ADAPTATION_STATE_SCHEMA = "eegle.online_adaptation_state.v1"
ADAPTATION_UPDATE_SCHEMA = "eegle.online_adaptation_update.v1"


@dataclass
class OnlineAdaptationState:
    model_kind: str
    target: str
    label_mode: str
    model_id: str = "primary"
    model_role: str = "primary"
    support_size: int = 0
    query_size: int = 0
    total_update_count: int = 0
    accepted_update_count: int = 0
    skipped_update_count: int = 0
    class_counts: dict[str, int] = field(default_factory=lambda: {"0": 0, "1": 0})
    last_updated_trial: int | None = None
    selected_threshold: float | None = None
    calibration_id: str | None = None
    calibration_state_hash: str | None = None
    normalizer_state: dict[str, Any] | None = None
    prototype_state: dict[str, Any] | None = None
    recent_metrics: dict[str, Any] = field(default_factory=dict)
    update_policy: dict[str, Any] = field(default_factory=dict)
    schema: str = ADAPTATION_STATE_SCHEMA

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["calibration_state_hash"] = self.state_hash(include_hash=False)
        return data

    def state_hash(self, *, include_hash: bool = False) -> str:
        payload = asdict(self)
        if not include_hash:
            payload["calibration_state_hash"] = None
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def snapshot(self, path: str | Path) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.payload()
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return payload

    @classmethod
    def load(cls, path: str | Path) -> "OnlineAdaptationState":
        with Path(path).expanduser().resolve().open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("schema") != ADAPTATION_STATE_SCHEMA:
            raise ValueError(f"unsupported online adaptation state schema: {payload.get('schema')}")
        fields = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in fields})


@dataclass
class AdaptationUpdateResult:
    status: str
    state_before: dict[str, Any] | None = None
    state_after: dict[str, Any] | None = None
    reason: str | None = None
    update_time_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


class AdaptationEventTailer:
    """Incrementally read task event JSONL rows written by the same session."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._offset = 0

    def read_new(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            handle.seek(self._offset)
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
            self._offset = handle.tell()
        return rows


def adaptation_update_row(
    *,
    trial: int | None,
    model_id: str,
    model_role: str,
    model_kind: str,
    prediction_row: dict[str, Any] | None,
    online_label: dict[str, Any],
    update_result: AdaptationUpdateResult,
    observe_only: bool = True,
) -> dict[str, Any]:
    before = dict(update_result.state_before or {})
    after = dict(update_result.state_after or {})
    return {
        "schema": ADAPTATION_UPDATE_SCHEMA,
        "trial": trial,
        "model_id": model_id,
        "model_role": model_role,
        "model_kind": model_kind,
        "pre_update_probability": None if prediction_row is None else prediction_row.get("probability_attention_lapse", prediction_row.get("probability_no_go")),
        "predicted_label": None if prediction_row is None else prediction_row.get("prediction_label"),
        "true_online_label": online_label.get("label"),
        "label_mode": online_label.get("label_mode"),
        "label_status": online_label.get("status"),
        "update_status": update_result.status,
        "skip_reason": update_result.reason or online_label.get("reason"),
        "support_size_before": before.get("support_size"),
        "support_size_after": after.get("support_size"),
        "class_counts_before": before.get("class_counts"),
        "class_counts_after": after.get("class_counts"),
        "calibration_threshold_before": before.get("selected_threshold"),
        "calibration_threshold_after": after.get("selected_threshold"),
        "calibration_state_hash_before": before.get("calibration_state_hash"),
        "calibration_state_hash_after": after.get("calibration_state_hash"),
        "update_time_ms": update_result.update_time_ms,
        "created_at_monotonic": monotonic(),
        "observe_only": observe_only,
        "metadata": update_result.metadata,
    }


def skipped_update(reason: str, state: dict[str, Any] | None = None) -> AdaptationUpdateResult:
    return AdaptationUpdateResult(status="skipped", reason=reason, state_before=state, state_after=state)


def apply_delayed_adaptation_update(
    adapter: Any,
    record: dict[str, Any],
    label: dict[str, Any],
    outcome: dict[str, Any],
    adaptation_config: dict[str, Any],
) -> AdaptationUpdateResult:
    """Apply one delayed behavior label to an adapter after prediction-time logging."""
    state = adapter.snapshot_adaptation_state() if hasattr(adapter, "snapshot_adaptation_state") else None
    role = str(record["model_role"])
    kind = str(record["model_kind"])
    if record.get("skip_update_reason"):
        return skipped_update(str(record["skip_update_reason"]), state)
    if record.get("quality_rejected"):
        return skipped_update(str(record.get("skip_reason") or "quality_rejected_epoch"), state)
    if label.get("status") != "labeled" or label.get("label") is None:
        return skipped_update(str(label.get("reason") or "label_not_available"), state)
    if role == "primary" and not bool(adaptation_config.get("update_primary", True)):
        return skipped_update("primary_updates_disabled", state)
    if role != "primary" and not bool(adaptation_config.get("update_shadows", False)):
        return skipped_update("shadow_updates_disabled", state)
    allowed = [str(value) for value in adaptation_config.get("allowed_model_kinds", [])]
    if allowed and kind not in allowed:
        return skipped_update("model_kind_not_allowed", state)
    if not bool(getattr(adapter, "supports_online_update", False)):
        return skipped_update("model_not_adaptable", state)
    metadata = {
        "trial": outcome.get("trial"),
        "label_mode": label.get("label_mode"),
        "model_id": record.get("model_id"),
        "model_role": role,
        "pre_update_probability": dict(record.get("prediction_row") or {}).get(
            "probability_attention_lapse",
            dict(record.get("prediction_row") or {}).get("probability_no_go"),
        ),
        "adaptation_config": adaptation_config,
    }
    return adapter.update_after_trial(record["prepared"], int(label["label"]), metadata)


def snapshot_adapter_state(adapter: Any, state_dir: str | Path, model_id: str) -> dict[str, Any] | None:
    if not hasattr(adapter, "snapshot_adaptation_state"):
        return None
    state = adapter.snapshot_adaptation_state()
    if not state:
        return None
    target = Path(state_dir) / f"{safe_file_id(model_id)}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state


def safe_file_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value))
