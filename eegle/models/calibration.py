"""Calibration protocols and threshold helpers for EEGle models."""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from eegle.ml.calibration import binary_metrics_at_threshold, select_binary_threshold, threshold_candidates


CALIBRATION_STATE_SCHEMA = "eegle.calibration_state.v1"


@dataclass(frozen=True)
class CalibrationState:
    """Versioned adaptation state that can be logged and replayed."""

    kind: str
    parameters: dict[str, Any] = field(default_factory=dict)
    schema: str = CALIBRATION_STATE_SCHEMA
    source: str = "unspecified"
    update_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CalibrationState":
        if payload.get("schema") != CALIBRATION_STATE_SCHEMA:
            raise ValueError(f"unsupported calibration state schema: {payload.get('schema')}")
        return cls(
            kind=str(payload["kind"]),
            parameters=dict(payload.get("parameters") or {}),
            schema=str(payload.get("schema", CALIBRATION_STATE_SCHEMA)),
            source=str(payload.get("source", "unspecified")),
            update_count=int(payload.get("update_count", 0)),
            metadata=dict(payload.get("metadata") or {}),
        )


class CalibrationAdapter(Protocol):
    """Protocol for explicit, replayable calibration and adaptation."""

    kind: str

    def fit(self, support_set: Any) -> CalibrationState:
        ...

    def transform_epoch(self, epoch: np.ndarray, state: CalibrationState) -> np.ndarray:
        ...

    def transform_window(self, window: np.ndarray, state: CalibrationState) -> np.ndarray:
        ...

    def update_unsupervised(self, window: np.ndarray, state: CalibrationState) -> CalibrationState:
        ...

    def update_supervised(self, epoch: np.ndarray, label: Any, state: CalibrationState) -> CalibrationState:
        ...

    def export_state(self, state: CalibrationState, path: str | Path) -> None:
        ...

    def load_state(self, path: str | Path) -> CalibrationState:
        ...


def write_calibration_state(state: CalibrationState, path: str | Path) -> CalibrationState:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(state.payload(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return state


def read_calibration_state(path: str | Path) -> CalibrationState:
    with Path(path).expanduser().resolve().open("r", encoding="utf-8") as handle:
        return CalibrationState.from_payload(json.load(handle))


def calibration_state_hash(state: CalibrationState | dict[str, Any]) -> str:
    payload = state.payload() if isinstance(state, CalibrationState) else dict(state)
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_threshold_state(
    calibration: dict[str, Any],
    *,
    source: str,
    support_size: int | None = None,
    query_size: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> CalibrationState:
    parameters = {
        "selected_threshold": calibration.get("selected_threshold"),
        "metric": calibration.get("metric"),
        "selected_metrics": calibration.get("selected_metrics"),
    }
    if support_size is not None:
        parameters["support_size"] = int(support_size)
    if query_size is not None:
        parameters["query_size"] = int(query_size)
    return CalibrationState(
        kind="threshold",
        parameters={key: value for key, value in parameters.items() if value is not None},
        source=source,
        update_count=0,
        metadata=dict(metadata or {}),
    )


def make_prototype_state(
    *,
    lapse_prototype: np.ndarray,
    non_lapse_prototype: np.ndarray,
    support_counts: dict[str, int],
    distance_metric: str,
    alpha: float,
    bias: float,
    selected_threshold: float,
    normalizer: dict[str, Any] | None = None,
    source: str = "support_set",
    metadata: dict[str, Any] | None = None,
) -> CalibrationState:
    return CalibrationState(
        kind="prototype",
        parameters={
            "lapse_prototype": np.asarray(lapse_prototype, dtype=float).tolist(),
            "non_lapse_prototype": np.asarray(non_lapse_prototype, dtype=float).tolist(),
            "support_counts": {str(key): int(value) for key, value in support_counts.items()},
            "distance_metric": str(distance_metric),
            "alpha": float(alpha),
            "bias": float(bias),
            "selected_threshold": float(selected_threshold),
            "normalizer": dict(normalizer or {}),
        },
        source=source,
        update_count=0,
        metadata=dict(metadata or {}),
    )
