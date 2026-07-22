"""Shared contracts for Go/No-go EEG condition classifiers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from eegle.ml.contracts import normalize_input_contract, select_contract_channels, validate_supported_resampling
from eegle.realtime.epoch_arrays import epoch_to_channels_samples, relative_times
PREDICTION_SCHEMA = "eegle.model_prediction.v1"
SAFE_MODEL_METADATA = {
    "relative_times",
    "sample_rate_hz",
    "channel_names",
    "epoch_window_seconds",
}
DEFAULT_ROI_CONFIG: dict[str, Any] = {
    "baseline_seconds": [-0.2, 0.0],
    "n2_window_seconds": [0.20, 0.35],
    "p3_window_seconds": [0.30, 0.60],
    "late_window_seconds": [0.45, 0.75],
    "n2_channels": ["Fz", "Cz"],
    "p3_channels": ["Cz", "Pz", "P3", "P4"],
    "occipital_channels": ["Oz", "Pz"],
    "input_units": "microvolts",
}


@dataclass(frozen=True)
class EpochQualityResult:
    valid: bool
    reasons: tuple[str, ...]
    metrics: dict[str, float]

    def payload(self) -> dict[str, Any]:
        return {"valid": self.valid, "reasons": list(self.reasons), "metrics": self.metrics}


def sanitize_model_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Remove labels, stimulus identity, and behavior before model inference."""
    return {key: metadata[key] for key in SAFE_MODEL_METADATA if key in metadata}


def model_prediction_row(
    epoch_payload: dict[str, Any],
    prediction: dict[str, Any],
    *,
    model_id: str,
    role: str,
    latency_ms: float | None,
    quality: dict[str, Any],
    preprocessing_latency_ms: float | None = None,
    prediction_source: str = "live",
) -> dict[str, Any]:
    """Build the label-blind normalized realtime prediction record."""
    metadata_value = dict(prediction.get("metadata") or {})
    target = str(metadata_value.get("target", "condition"))
    probability = prediction.get("probability")
    probability_no_go = probability if target == "condition" else None
    probability_attention_lapse = probability if target.startswith("attention_lapse") else None
    return {
        "schema": PREDICTION_SCHEMA,
        "status": "predicted",
        "epoch_index": epoch_payload.get("epoch_index"),
        "trial": epoch_payload.get("trial"),
        "marker_timestamp_lsl": dict(epoch_payload.get("marker") or {}).get("timestamp"),
        "epoch_window_seconds": epoch_payload.get("epoch_window_seconds"),
        "prediction_window_seconds": metadata_value.get("prediction_window_seconds", epoch_payload.get("epoch_window_seconds")),
        "prediction_horizon": metadata_value.get("prediction_horizon"),
        "prediction_source": prediction_source,
        "model_id": model_id,
        "model_role": role,
        "model_kind": prediction.get("model_kind"),
        "model_version": prediction.get("model_version"),
        "model_family": metadata_value.get("model_family"),
        "bundle_hash": metadata_value.get("bundle_hash"),
        "target": target,
        "prediction_label": prediction.get("label"),
        "predicted_condition": prediction.get("label") if target == "condition" else None,
        "probability_no_go": probability_no_go,
        "probability_attention_lapse": probability_attention_lapse,
        "calibrated_threshold": metadata_value.get("calibrated_threshold"),
        "calibration_id": metadata_value.get("calibration_id"),
        "calibration_state_hash": metadata_value.get("calibration_state_hash"),
        "support_size": metadata_value.get("support_size"),
        "query_size": metadata_value.get("query_size"),
        "score": prediction.get("score"),
        "features": prediction.get("features", {}),
        "processing_latency_ms": latency_ms,
        "preprocessing_latency_ms": metadata_value.get("preprocessing_latency_ms", preprocessing_latency_ms),
        "quality": quality,
        "quality_status": "ok" if bool(quality.get("valid", False)) else "rejected",
        "ground_truth_joined_during_inference": False,
        "observe_only": True,
    }


def model_rejection_row(
    epoch_payload: dict[str, Any],
    reason: str,
    quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    marker = dict(epoch_payload.get("marker") or {})
    parsed = dict(marker.get("metadata") or {})
    return {
        "schema": PREDICTION_SCHEMA,
        "status": "rejected",
        "trial": epoch_payload.get("trial", parsed.get("trial")),
        "epoch_index": epoch_payload.get("epoch_index"),
        "marker_timestamp_lsl": marker.get("timestamp"),
        "reason": reason,
        "quality": quality or {},
        "ground_truth_joined_during_inference": False,
        "observe_only": True,
    }


def model_skip_row(
    epoch_payload: dict[str, Any],
    *,
    model_id: str,
    role: str,
    model_kind: str,
    reason: str,
    quality: dict[str, Any] | None = None,
    queue_depth: int | None = None,
    primary_latency_ms: float | None = None,
) -> dict[str, Any]:
    """Build an explicit row for budget-driven shadow inference skipping."""
    marker = dict(epoch_payload.get("marker") or {})
    parsed = dict(marker.get("metadata") or {})
    return {
        "schema": PREDICTION_SCHEMA,
        "status": "skipped",
        "trial": epoch_payload.get("trial", parsed.get("trial")),
        "epoch_index": epoch_payload.get("epoch_index"),
        "marker_timestamp_lsl": marker.get("timestamp"),
        "epoch_window_seconds": epoch_payload.get("epoch_window_seconds"),
        "model_id": model_id,
        "model_role": role,
        "model_kind": model_kind,
        "reason": reason,
        "quality": quality or {},
        "queue_depth": queue_depth,
        "primary_latency_ms": primary_latency_ms,
        "ground_truth_joined_during_inference": False,
        "observe_only": True,
    }


def assess_epoch_quality(epoch: np.ndarray, config: dict[str, Any] | None = None) -> EpochQualityResult:
    cfg = dict(config or {})
    values = np.asarray(epoch, dtype=float)
    reasons: list[str] = []
    if values.ndim != 2 or values.size == 0:
        return EpochQualityResult(False, ("invalid_shape",), {})
    finite_fraction = float(np.mean(np.isfinite(values)))
    finite = values[np.isfinite(values)]
    metrics = {
        "finite_fraction": finite_fraction,
        "max_abs_uv": float(np.max(np.abs(finite))) if finite.size else float("inf"),
        "peak_to_peak_uv": float(np.ptp(finite)) if finite.size else float("inf"),
        "minimum_channel_std_uv": float(np.min(np.nanstd(values, axis=0))) if values.shape[0] > 1 else 0.0,
    }
    if finite_fraction < float(cfg.get("minimum_finite_fraction", 1.0)):
        reasons.append("non_finite")
    if metrics["minimum_channel_std_uv"] < float(cfg.get("minimum_channel_std_uv", 0.01)):
        reasons.append("flatline")
    if metrics["max_abs_uv"] > float(cfg.get("max_abs_uv", 250.0)):
        reasons.append("max_abs_exceeded")
    if metrics["peak_to_peak_uv"] > float(cfg.get("max_peak_to_peak_uv", 400.0)):
        reasons.append("peak_to_peak_exceeded")
    return EpochQualityResult(not reasons, tuple(reasons), metrics)


def baseline_correct(
    channels_samples: np.ndarray,
    times: np.ndarray,
    baseline_seconds: list[float] | tuple[float, float] = (-0.2, 0.0),
) -> np.ndarray:
    values = np.asarray(channels_samples, dtype=float)
    relative = np.asarray(times, dtype=float)
    if values.ndim != 2 or relative.size != values.shape[1]:
        raise ValueError("baseline correction requires channels x samples data and one relative time per sample")
    low, high = float(baseline_seconds[0]), float(baseline_seconds[1])
    mask = (relative >= low) & (relative <= high)
    if not mask.any():
        raise ValueError(f"epoch has no samples inside baseline window [{low}, {high}]")
    return values - np.nanmean(values[:, mask], axis=1, keepdims=True)


def prepare_classifier_epoch(
    epoch: np.ndarray,
    sample_rate_hz: float,
    channel_names: list[str],
    metadata_value: dict[str, Any],
    contract: dict[str, Any],
) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Validate, reorder, convert units, and baseline-correct one epoch."""
    normalized_contract = normalize_input_contract(contract, fallback_channel_names=channel_names)
    validate_supported_resampling(normalized_contract)
    values = epoch_to_channels_samples(epoch, channel_names, str(normalized_contract.get("input_layout", "auto")))
    indices, required = select_contract_channels(channel_names, normalized_contract)
    expected_rate = float(normalized_contract.get("sample_rate_hz", sample_rate_hz) or sample_rate_hz)
    if abs(expected_rate - float(sample_rate_hz)) > float(normalized_contract.get("sample_rate_tolerance_hz", 0.01)):
        raise ValueError(f"model sample rate {expected_rate:g} Hz does not match stream {sample_rate_hz:g} Hz")
    values = np.asarray(values[indices], dtype=float)
    expected_samples = int(normalized_contract.get("sample_count", values.shape[1]) or values.shape[1])
    if values.shape[1] != expected_samples:
        raise ValueError(f"model sample count {expected_samples} does not match epoch sample count {values.shape[1]}")
    input_units = str(normalized_contract.get("input_units", "microvolts")).lower()
    if input_units in {"v", "volt", "volts"}:
        values = values * 1e6
    times = relative_times(metadata_value, values.shape[1], sample_rate_hz)
    expected_window = normalized_contract.get("epoch_window_seconds")
    if expected_window:
        actual_window = metadata_value.get("epoch_window_seconds", [float(times[0]), float(times[-1])])
        tolerance = float(normalized_contract.get("epoch_window_tolerance_seconds", 0.01))
        if any(abs(float(a) - float(b)) > tolerance for a, b in zip(actual_window, expected_window)):
            raise ValueError(f"model epoch window {expected_window} does not match runtime window {actual_window}")
    baseline = normalized_contract.get("baseline_seconds", [-0.2, 0.0])
    return baseline_correct(values, times, baseline), required, times


def extract_erp_roi_features(
    channels_samples: np.ndarray,
    times: np.ndarray,
    channel_names: list[str],
    config: dict[str, Any] | None = None,
) -> dict[str, float]:
    """Extract deterministic, interpretable ERP features from a corrected epoch."""
    cfg = {**DEFAULT_ROI_CONFIG, **dict(config or {})}
    values = np.asarray(channels_samples, dtype=float)
    relative = np.asarray(times, dtype=float)
    if values.shape != (len(channel_names), relative.size):
        raise ValueError("ROI extraction requires channels x samples matching channel_names and times")

    n2 = _roi_wave(values, channel_names, cfg["n2_channels"])
    p3 = _roi_wave(values, channel_names, cfg["p3_channels"])
    occipital = _roi_wave(values, channel_names, cfg["occipital_channels"])
    n2_mask = _time_mask(relative, cfg["n2_window_seconds"], "N2")
    p3_mask = _time_mask(relative, cfg["p3_window_seconds"], "P3")
    late_mask = _time_mask(relative, cfg["late_window_seconds"], "late")
    return {
        "n2_mean_uv": float(np.mean(n2[n2_mask])),
        "n2_min_uv": float(np.min(n2[n2_mask])),
        "p3_mean_uv": float(np.mean(p3[p3_mask])),
        "p3_max_uv": float(np.max(p3[p3_mask])),
        "late_mean_uv": float(np.mean(p3[late_mask])),
        "p3_peak_to_peak_uv": float(np.ptp(p3)),
        "occipital_mean_uv": float(np.mean(occipital[p3_mask])),
    }


def feature_vector(features: dict[str, float], feature_names: list[str] | None = None) -> tuple[np.ndarray, list[str]]:
    names = list(feature_names or sorted(features))
    return np.asarray([features[name] for name in names], dtype=float), names


def _roi_wave(values: np.ndarray, channel_names: list[str], requested: list[str]) -> np.ndarray:
    indices = [channel_names.index(name) for name in requested if name in channel_names]
    if not indices:
        raise ValueError("none of the requested ROI channels are available: " + ", ".join(requested))
    return np.mean(values[indices], axis=0)


def _time_mask(times: np.ndarray, window: list[float], label: str) -> np.ndarray:
    mask = (times >= float(window[0])) & (times <= float(window[1]))
    if not mask.any():
        raise ValueError(f"epoch has no samples inside {label} feature window {window}")
    return mask


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
