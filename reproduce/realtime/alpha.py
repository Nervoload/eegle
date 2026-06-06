"""Posterior alpha calibration and realtime alpha-power helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Any

import numpy as np


DEFAULT_POSTERIOR_CHANNELS = ("P3", "P4", "PO3", "PO4", "Pz", "O1", "O2", "Oz")
DEFAULT_ALPHA_BAND = (8.0, 12.0)


@dataclass(frozen=True)
class AlphaBand:
    low_hz: float
    high_hz: float
    center_hz: float | None = None
    bandwidth_hz: float | None = None
    source: str = "configured"
    confidence: str = "unknown"

    @property
    def width_hz(self) -> float:
        return self.high_hz - self.low_hz

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AlphaPeakCandidate:
    center_hz: float
    power: float
    bandwidth_hz: float
    fit_r_squared: float | None = None
    source: str = "specparam"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def bounded_alpha_band(
    center_hz: float,
    bandwidth_hz: float | None,
    *,
    low_bound_hz: float = 6.0,
    high_bound_hz: float = 15.0,
    min_half_width_hz: float = 1.5,
    min_width_hz: float = 2.0,
    max_width_hz: float = 6.0,
    source: str = "specparam",
    confidence: str = "accepted",
) -> AlphaBand:
    """Convert an alpha peak into a bounded individualized online band."""
    center = float(center_hz)
    raw_half_width = None if bandwidth_hz is None else max(0.0, float(bandwidth_hz) / 2.0)
    half_width = max(min_half_width_hz, raw_half_width or min_half_width_hz)
    low = max(float(low_bound_hz), center - half_width)
    high = min(float(high_bound_hz), center + half_width)
    width = high - low
    if width < min_width_hz:
        half_width = min_width_hz / 2.0
        low = max(float(low_bound_hz), center - half_width)
        high = min(float(high_bound_hz), center + half_width)
    width = high - low
    if width > max_width_hz:
        half_width = max_width_hz / 2.0
        low = max(float(low_bound_hz), center - half_width)
        high = min(float(high_bound_hz), center + half_width)
    return AlphaBand(
        low_hz=float(low),
        high_hz=float(high),
        center_hz=center,
        bandwidth_hz=None if bandwidth_hz is None else float(bandwidth_hz),
        source=source,
        confidence=confidence,
    )


def fallback_alpha_band(reason: str = "no_accepted_peak") -> AlphaBand:
    return AlphaBand(
        low_hz=DEFAULT_ALPHA_BAND[0],
        high_hz=DEFAULT_ALPHA_BAND[1],
        center_hz=10.0,
        bandwidth_hz=4.0,
        source=reason,
        confidence="low_confidence_fallback",
    )


def spectral_peak_candidates(model: Any, alpha_range_hz: tuple[float, float] = (7.0, 14.0)) -> list[AlphaPeakCandidate]:
    """Extract alpha-range peak candidates from spectral-parameterization objects."""
    peaks = getattr(model, "peak_params_", None)
    if peaks is None and isinstance(model, dict):
        peaks = model.get("peak_params") or model.get("peak_params_")
    if peaks is None:
        return []
    r_squared = getattr(model, "r_squared_", None)
    if r_squared is None and isinstance(model, dict):
        r_squared = model.get("r_squared") or model.get("r_squared_")
    source = str(model.get("source", "specparam")) if isinstance(model, dict) else "specparam"
    low, high = alpha_range_hz
    candidates: list[AlphaPeakCandidate] = []
    for row in np.asarray(peaks, dtype=float).reshape(-1, 3):
        center, power, bandwidth = float(row[0]), float(row[1]), float(row[2])
        if low <= center <= high:
            candidates.append(
                AlphaPeakCandidate(
                    center_hz=center,
                    power=power,
                    bandwidth_hz=bandwidth,
                    fit_r_squared=None if r_squared is None else float(r_squared),
                    source=source,
                )
            )
    return sorted(candidates, key=lambda item: item.power, reverse=True)


def accept_alpha_candidate(
    candidate: AlphaPeakCandidate | None,
    *,
    min_peak_power: float = 0.05,
    min_bandwidth_hz: float = 0.5,
    max_bandwidth_hz: float = 8.0,
    eyes_closed_open_ratio: float | None = None,
    min_eyes_closed_open_ratio: float = 1.05,
) -> tuple[bool, list[str]]:
    """Apply conservative alpha-peak acceptance checks."""
    reasons: list[str] = []
    if candidate is None:
        return False, ["no_alpha_candidate"]
    if candidate.power < min_peak_power:
        reasons.append("peak_power_below_threshold")
    if candidate.bandwidth_hz < min_bandwidth_hz:
        reasons.append("bandwidth_too_narrow")
    if candidate.bandwidth_hz > max_bandwidth_hz:
        reasons.append("bandwidth_too_wide")
    if eyes_closed_open_ratio is not None and eyes_closed_open_ratio < min_eyes_closed_open_ratio:
        reasons.append("eyes_closed_alpha_not_higher_than_eyes_open")
    return not reasons, reasons


def channel_indices(channel_names: list[str], requested: list[str] | tuple[str, ...]) -> list[int]:
    return [channel_names.index(name) for name in requested if name in channel_names]


class ArtifactGate:
    """Simple online artifact gate for microvolt-scale EEG arrays."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.max_abs_uv = float(cfg.get("max_abs_uv", 150.0))
        self.max_peak_to_peak_uv = float(cfg.get("max_peak_to_peak_uv", 250.0))
        self.max_nan_fraction = float(cfg.get("max_nan_fraction", 0.0))

    def check(self, data: np.ndarray) -> dict[str, Any]:
        values = np.asarray(data, dtype=float)
        if values.size == 0:
            return {"ok": False, "reason": "empty"}
        nan_fraction = float(np.mean(~np.isfinite(values)))
        finite = values[np.isfinite(values)]
        if finite.size == 0:
            return {"ok": False, "reason": "all_nonfinite", "nan_fraction": nan_fraction}
        max_abs = float(np.max(np.abs(finite)))
        peak_to_peak = float(np.ptp(finite))
        ok = nan_fraction <= self.max_nan_fraction and max_abs <= self.max_abs_uv and peak_to_peak <= self.max_peak_to_peak_uv
        reason = "ok"
        if nan_fraction > self.max_nan_fraction:
            reason = "too_many_nonfinite"
        elif max_abs > self.max_abs_uv:
            reason = "max_abs_exceeded"
        elif peak_to_peak > self.max_peak_to_peak_uv:
            reason = "peak_to_peak_exceeded"
        return {
            "ok": ok,
            "reason": reason,
            "nan_fraction": nan_fraction,
            "max_abs_uv": max_abs,
            "peak_to_peak_uv": peak_to_peak,
        }


class AlphaPowerEstimator:
    """Causal alpha-band estimator with finite-window Hilbert envelope snapshots."""

    def __init__(
        self,
        sample_rate_hz: float,
        channel_names: list[str],
        config: dict[str, Any] | None = None,
    ) -> None:
        from scipy import signal

        cfg = dict(config or {})
        band_cfg = dict(cfg.get("band") or {})
        low = float(band_cfg.get("low_hz", cfg.get("low_hz", DEFAULT_ALPHA_BAND[0])))
        high = float(band_cfg.get("high_hz", cfg.get("high_hz", DEFAULT_ALPHA_BAND[1])))
        self.band = AlphaBand(
            low_hz=low,
            high_hz=high,
            center_hz=band_cfg.get("center_hz"),
            bandwidth_hz=band_cfg.get("bandwidth_hz"),
            source=str(band_cfg.get("source", "configured")),
            confidence=str(band_cfg.get("confidence", "unknown")),
        )
        self.sample_rate_hz = float(sample_rate_hz)
        self.channel_names = list(channel_names)
        self.posterior_channels = [str(name) for name in cfg.get("posterior_channels", DEFAULT_POSTERIOR_CHANNELS)]
        self.posterior_indices = channel_indices(self.channel_names, self.posterior_channels)
        if not self.posterior_indices:
            raise ValueError(
                "none of the configured posterior alpha channels are present; "
                f"requested={self.posterior_channels}, available={self.channel_names}"
            )
        self.smoothing_seconds = float(cfg.get("smoothing_seconds", 0.2))
        self.window_seconds = float(cfg.get("window_seconds", 1.0))
        self.baseline_mean = _optional_float(cfg.get("baseline_mean_power"))
        self.baseline_std = _optional_float(cfg.get("baseline_std_power"))
        self.input_units = str(cfg.get("input_units", "microvolts")).lower()
        self.artifact_gate = ArtifactGate(cfg.get("artifact_gate", {}))
        self._sos = signal.butter(4, [self.band.low_hz, self.band.high_hz], btype="bandpass", fs=self.sample_rate_hz, output="sos")
        self._zi: np.ndarray | None = None
        self._timestamps: list[float] = []
        self._filtered: list[list[float]] = []
        self._last_artifact: dict[str, Any] = {"ok": True, "reason": "not_checked"}

    @property
    def ready(self) -> bool:
        return len(self._filtered) >= max(3, int(round(self.smoothing_seconds * self.sample_rate_hz)))

    def check_artifact(self, data: np.ndarray) -> dict[str, Any]:
        values = np.asarray(data, dtype=float)
        if values.ndim != 2:
            raise ValueError("expected EEG data with shape samples x channels")
        selected = values[:, self.posterior_indices]
        if self.input_units in {"v", "volt", "volts"}:
            selected = selected * 1e6
        return self.artifact_gate.check(selected)

    def process_chunk(self, timestamps: np.ndarray, data: np.ndarray, artifact_result: dict[str, Any] | None = None) -> None:
        from scipy import signal

        ts = np.asarray(timestamps, dtype=float)
        values = np.asarray(data, dtype=float)
        if ts.size == 0 or values.size == 0:
            return
        if values.ndim != 2:
            raise ValueError("expected EEG data with shape samples x channels")
        selected = values[:, self.posterior_indices]
        self._last_artifact = dict(artifact_result) if artifact_result is not None else self.check_artifact(values)
        if self.input_units in {"v", "volt", "volts"}:
            selected = selected * 1e6
        if self._zi is None:
            zi = signal.sosfilt_zi(self._sos)
            self._zi = zi[:, :, np.newaxis] * selected[0][np.newaxis, np.newaxis, :]
        filtered, self._zi = signal.sosfilt(self._sos, selected, axis=0, zi=self._zi)
        self._timestamps.extend(float(item) for item in ts)
        self._filtered.extend([float(value) for value in row] for row in filtered)
        max_samples = max(1, int(round(max(self.window_seconds, self.smoothing_seconds) * self.sample_rate_hz * 3)))
        if len(self._filtered) > max_samples:
            self._timestamps = self._timestamps[-max_samples:]
            self._filtered = self._filtered[-max_samples:]

    def snapshot(self) -> dict[str, Any] | None:
        if not self.ready:
            return None
        from scipy import signal

        smooth_samples = max(3, int(round(self.smoothing_seconds * self.sample_rate_hz)))
        segment = np.asarray(self._filtered[-smooth_samples:], dtype=float)
        analytic = signal.hilbert(segment, axis=0)
        channel_powers = np.mean(np.abs(analytic) ** 2, axis=0)
        power = float(np.nanmean(channel_powers))
        z_power = None
        if self.baseline_mean is not None and self.baseline_std is not None and self.baseline_std > 1e-12:
            z_power = float((power - self.baseline_mean) / self.baseline_std)
        selected_names = [self.channel_names[index] for index in self.posterior_indices]
        latency_ms = float((self.smoothing_seconds + 2.0 / max(self.band.high_hz - self.band.low_hz, 1e-6)) * 1000.0)
        return {
            "schema_version": 1,
            "created_at_monotonic": monotonic(),
            "window_start_lsl_timestamp": float(self._timestamps[-smooth_samples]),
            "window_end_lsl_timestamp": float(self._timestamps[-1]),
            "method": "causal_bandpass_hilbert_envelope",
            "alpha_power": power,
            "alpha_power_z": z_power,
            "channel_alpha_power": {name: float(value) for name, value in zip(selected_names, channel_powers)},
            "band": self.band.as_dict(),
            "posterior_channels": selected_names,
            "artifact": dict(self._last_artifact),
            "valid": bool(self._last_artifact.get("ok", False)),
            "latency_estimate_ms": latency_ms,
        }


def load_alpha_config(config: dict[str, Any], session_dir: str | Path | None = None) -> dict[str, Any]:
    realtime_alpha = dict(config.get("realtime", {}).get("alpha", {}))
    result_path = realtime_alpha.get("calibration_result_path")
    if result_path is None and session_dir is not None:
        candidate = Path(session_dir) / "calibration" / "alpha_calibration.json"
        if candidate.exists():
            result_path = str(candidate)
    result = _load_json(result_path) if result_path else {}
    band = dict(realtime_alpha.get("band") or result.get("online_band") or {})
    if not band:
        band = fallback_alpha_band("missing_calibration_result").as_dict()
    calibration = result.get("calibration") or result
    return {
        **realtime_alpha,
        "band": band,
        "posterior_channels": realtime_alpha.get("posterior_channels") or calibration.get("posterior_channels") or list(DEFAULT_POSTERIOR_CHANNELS),
        "baseline_mean_power": realtime_alpha.get("baseline_mean_power", calibration.get("baseline_mean_power")),
        "baseline_std_power": realtime_alpha.get("baseline_std_power", calibration.get("baseline_std_power")),
        "calibration_status": calibration.get("status") or result.get("status"),
        "calibration_result_path": result_path,
    }


def sliding_window_psd_alpha_power(
    data: np.ndarray,
    sample_rate_hz: float,
    band: AlphaBand | tuple[float, float],
    *,
    nperseg_seconds: float = 1.0,
) -> float:
    from scipy import signal

    values = np.asarray(data, dtype=float)
    low, high = (band.low_hz, band.high_hz) if isinstance(band, AlphaBand) else band
    nperseg = max(8, int(round(nperseg_seconds * sample_rate_hz)))
    if values.ndim == 1:
        freqs, psd = signal.welch(values, fs=sample_rate_hz, nperseg=min(nperseg, values.shape[0]))
    else:
        freqs, psd = signal.welch(values, fs=sample_rate_hz, nperseg=min(nperseg, values.shape[0]), axis=0)
    mask = (freqs >= low) & (freqs <= high)
    if not mask.any():
        return 0.0
    band_power = np.trapezoid(psd[mask], freqs[mask], axis=0)
    return float(np.nanmean(band_power))


def append_alpha_payload(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_json(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    target = Path(path)
    if not target.exists():
        return {}
    with target.open("r", encoding="utf-8") as handle:
        return json.load(handle)
