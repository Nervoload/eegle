"""A deliberately small built-in transform set.

Dense arrays are always samples x channels. Causal SOS filters retain state
between chunks. Retrospective zero-phase filters explicitly require a complete
segment and cannot validate for causal execution.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
from scipy import signal

from eegle._domain import Determinism, ExecutionMode
from eegle.compiler.lock import canonical_hash
from eegle.processing.capabilities import TransformCapabilities


class IdentityTransform:
    capabilities = TransformCapabilities(
        supported_modes=frozenset(
            {ExecutionMode.CAUSAL, ExecutionMode.RETROSPECTIVE, ExecutionMode.ORACLE}
        ),
        stateful=False,
        requires_future=False,
    )

    def update(self, values: np.ndarray) -> np.ndarray:
        data = _dense_array(values)
        result = data.copy()
        result.setflags(write=False)
        return result


class CausalSosFilter:
    capabilities = TransformCapabilities(
        supported_modes=frozenset({ExecutionMode.CAUSAL, ExecutionMode.RETROSPECTIVE}),
        stateful=True,
        requires_future=False,
        determinism=Determinism.DETERMINISTIC,
    )

    def __init__(self, sos: np.ndarray, channel_count: int) -> None:
        self.sos = _sos_array(sos)
        self.channel_count = int(channel_count)
        if self.channel_count <= 0:
            raise ValueError("channel_count must be positive")
        self._state = np.zeros((self.sos.shape[0], 2, self.channel_count), dtype=float)

    def update(self, values: np.ndarray) -> np.ndarray:
        data = _dense_array(values, channel_count=self.channel_count)
        filtered, state = signal.sosfilt(self.sos, data, axis=0, zi=self._state)
        self._state = np.asarray(state, dtype=float)
        filtered.setflags(write=False)
        return filtered

    def snapshot_state(self) -> dict[str, Any]:
        payload = {
            "schema": "eegle.causal_sos_state.v1",
            "sos_hash": canonical_hash(self.sos.tolist()),
            "channel_count": self.channel_count,
            "state": self._state.tolist(),
        }
        payload["state_hash"] = canonical_hash(payload)
        return payload

    def restore_state(self, payload: Mapping[str, Any]) -> None:
        if payload.get("schema") != "eegle.causal_sos_state.v1":
            raise ValueError(f"unsupported causal SOS state schema: {payload.get('schema')}")
        if payload.get("sos_hash") != canonical_hash(self.sos.tolist()):
            raise ValueError("causal SOS state was produced by different coefficients")
        if int(payload.get("channel_count", -1)) != self.channel_count:
            raise ValueError("causal SOS state channel count does not match")
        without_hash = {key: value for key, value in payload.items() if key != "state_hash"}
        if payload.get("state_hash") != canonical_hash(without_hash):
            raise ValueError("causal SOS state hash mismatch")
        restored = np.asarray(payload["state"], dtype=float)
        if restored.shape != self._state.shape or not bool(np.all(np.isfinite(restored))):
            raise ValueError("invalid causal SOS state array")
        self._state = restored.copy()


class RetrospectiveSosFilter:
    capabilities = TransformCapabilities(
        supported_modes=frozenset({ExecutionMode.RETROSPECTIVE, ExecutionMode.ORACLE}),
        stateful=False,
        requires_future=True,
        determinism=Determinism.DETERMINISTIC,
    )

    def __init__(self, sos: np.ndarray) -> None:
        self.sos = _sos_array(sos)

    def update(self, values: np.ndarray) -> np.ndarray:
        data = _dense_array(values)
        filtered = np.asarray(signal.sosfiltfilt(self.sos, data, axis=0), dtype=float)
        filtered.setflags(write=False)
        return filtered


def _sos_array(sos: np.ndarray) -> np.ndarray:
    coefficients = np.asarray(sos, dtype=float)
    if coefficients.ndim != 2 or coefficients.shape[1] != 6 or coefficients.shape[0] == 0:
        raise ValueError("SOS coefficients must have shape sections x 6")
    if not bool(np.all(np.isfinite(coefficients))):
        raise ValueError("SOS coefficients must be finite")
    return coefficients.copy()


def _dense_array(values: np.ndarray, channel_count: int | None = None) -> np.ndarray:
    data = np.asarray(values, dtype=float)
    if data.ndim != 2 or data.shape[0] == 0 or data.shape[1] == 0:
        raise ValueError("transform input must be a non-empty samples x channels array")
    if channel_count is not None and data.shape[1] != channel_count:
        raise ValueError(
            f"transform expected {channel_count} channels, received {data.shape[1]}"
        )
    if not bool(np.all(np.isfinite(data))):
        raise ValueError("built-in transforms require finite admitted values")
    return data
