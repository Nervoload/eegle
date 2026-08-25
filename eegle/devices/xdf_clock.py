"""Canonical XDF clock-normalization contract shared by validation and analysis."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np


XDF_CLOCK_NORMALIZATION_SCHEMA = "eegle.xdf_clock_normalization.v1"


@dataclass(frozen=True)
class XdfClockNormalization:
    method: str
    origin_local_lsl_timestamp: float
    eeg_source_to_local_scale: float | None = None
    eeg_source_to_local_offset_seconds: float | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "XdfClockNormalization":
        if payload.get("schema") != XDF_CLOCK_NORMALIZATION_SCHEMA:
            raise ValueError("XDF clock normalization schema is missing or unsupported")
        method = str(payload.get("method") or "")
        origin = _finite_float(payload.get("origin_local_lsl_timestamp"), "origin")
        if method == "affine_eeg_source_to_pc_local_lsl":
            scale = _finite_float(payload.get("eeg_source_to_local_scale"), "scale")
            offset = _finite_float(
                payload.get("eeg_source_to_local_offset_seconds"),
                "offset",
            )
            if scale <= 0.0:
                raise ValueError("XDF clock normalization scale must be positive")
            return cls(method, origin, scale, offset)
        if method == "pyxdf_synchronized_timestamps":
            return cls(method, origin)
        raise ValueError(f"unsupported XDF clock normalization method {method!r}")

    @property
    def synchronize_xdf_clocks(self) -> bool:
        return self.method == "pyxdf_synchronized_timestamps"

    def normalize_eeg_timestamps(self, timestamps: Iterable[float]) -> np.ndarray:
        values = np.asarray(timestamps, dtype=float)
        if self.method == "affine_eeg_source_to_pc_local_lsl":
            assert self.eeg_source_to_local_scale is not None
            assert self.eeg_source_to_local_offset_seconds is not None
            values = (
                values * self.eeg_source_to_local_scale
                + self.eeg_source_to_local_offset_seconds
            )
        return values - self.origin_local_lsl_timestamp

    def normalize_marker_timestamps(self, timestamps: Iterable[float]) -> np.ndarray:
        return np.asarray(timestamps, dtype=float) - self.origin_local_lsl_timestamp

    def normalize_marker_timestamp(self, timestamp: float) -> float:
        return float(timestamp) - self.origin_local_lsl_timestamp


def load_xdf_clock_normalization(
    metadata_path: str | Path,
) -> tuple[XdfClockNormalization, dict[str, Any]]:
    path = Path(metadata_path).expanduser().resolve()
    with path.open("r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    validation = dict(metadata.get("validation") or {})
    if validation.get("status") not in {"pass", "warning"}:
        raise ValueError("XDF must pass integrity validation before epoch extraction")
    coverage = dict(validation.get("recording_coverage") or {})
    payload = dict(coverage.get("clock_normalization") or {})
    return XdfClockNormalization.from_mapping(payload), metadata


def build_affine_xdf_clock_normalization(
    *,
    evidence_source: str,
    origin_local_lsl_timestamp: float,
    scale: float,
    offset_seconds: float,
    eeg_start_source_timestamp: float,
    eeg_end_source_timestamp: float,
    marker_end_local_lsl_timestamp: float,
) -> dict[str, Any]:
    normalizer = XdfClockNormalization(
        "affine_eeg_source_to_pc_local_lsl",
        origin_local_lsl_timestamp,
        scale,
        offset_seconds,
    )
    eeg_bounds = normalizer.normalize_eeg_timestamps(
        [eeg_start_source_timestamp, eeg_end_source_timestamp]
    )
    return {
        "schema": XDF_CLOCK_NORMALIZATION_SCHEMA,
        "method": normalizer.method,
        "evidence_source": evidence_source,
        "origin_definition": "first_required_marker_pyxdf_synchronized_timestamp",
        "origin_local_lsl_timestamp": origin_local_lsl_timestamp,
        "eeg_source_to_local_scale": scale,
        "eeg_source_to_local_offset_seconds": offset_seconds,
        "formula": (
            "normalized_seconds = eeg_source_lsl_timestamp * "
            "eeg_source_to_local_scale + eeg_source_to_local_offset_seconds - "
            "origin_local_lsl_timestamp"
        ),
        "normalized_eeg_start_seconds": float(eeg_bounds[0]),
        "normalized_eeg_end_seconds": float(eeg_bounds[1]),
        "normalized_marker_start_seconds": 0.0,
        "normalized_marker_end_seconds": (
            marker_end_local_lsl_timestamp - origin_local_lsl_timestamp
        ),
        "raw_xdf_timestamps_modified": False,
    }


def build_direct_xdf_clock_normalization(
    *,
    first_eeg: float,
    last_eeg: float,
    first_marker: float,
    last_marker: float,
) -> dict[str, Any]:
    return {
        "schema": XDF_CLOCK_NORMALIZATION_SCHEMA,
        "method": "pyxdf_synchronized_timestamps",
        "evidence_source": "xdf",
        "origin_definition": "first_required_marker_pyxdf_synchronized_timestamp",
        "origin_local_lsl_timestamp": first_marker,
        "normalized_eeg_start_seconds": first_eeg - first_marker,
        "normalized_eeg_end_seconds": last_eeg - first_marker,
        "normalized_marker_start_seconds": 0.0,
        "normalized_marker_end_seconds": last_marker - first_marker,
        "raw_xdf_timestamps_modified": False,
    }


def _finite_float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"XDF clock normalization {name} is missing") from exc
    if not math.isfinite(result):
        raise ValueError(f"XDF clock normalization {name} must be finite")
    return result
