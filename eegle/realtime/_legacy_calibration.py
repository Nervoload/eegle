"""Extraction-only calibration helpers for the retired realtime model stack.

The v1 calibration authority is :mod:`eegle.models.calibration`.  These
task-specific prototype helpers remain private to the legacy realtime boundary
until their remaining Phase 8 evidence is replaced and the stack is deleted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class LegacyCalibrationState:
    """Historical payload shape consumed by extraction-only model code."""

    kind: str
    parameters: dict[str, Any] = field(default_factory=dict)
    schema: str = "eegle.calibration_state.v1"
    source: str = "unspecified"
    update_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def calibration_state_hash(state: LegacyCalibrationState | dict[str, Any]) -> str:
    """Return the historical unprefixed digest used by extraction fixtures."""

    payload = state.payload() if isinstance(state, LegacyCalibrationState) else dict(state)
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
) -> LegacyCalibrationState:
    """Construct the historical attention-prototype state payload."""

    return LegacyCalibrationState(
        kind="prototype",
        parameters={
            "lapse_prototype": np.asarray(lapse_prototype, dtype=float).tolist(),
            "non_lapse_prototype": np.asarray(non_lapse_prototype, dtype=float).tolist(),
            "support_counts": {
                str(key): int(value) for key, value in support_counts.items()
            },
            "distance_metric": str(distance_metric),
            "alpha": float(alpha),
            "bias": float(bias),
            "selected_threshold": float(selected_threshold),
            "normalizer": dict(normalizer or {}),
        },
        source=source,
        metadata=dict(metadata or {}),
    )
