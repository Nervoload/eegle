"""Per-channel EEG and Enobio contact-quality diagnostics."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import numpy as np


def analyze_signal_quality(session_dir: str | Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
    target = root / "reports" / "quality" / "channel_quality.json"
    eeg_path = root / "raw" / "eeg.csv"
    if not eeg_path.exists():
        return _write(target, {"status": "missing", "reason": "raw_eeg_missing", "eeg_file": str(eeg_path)})
    channels, timestamps, values = _read_signal_csv(eeg_path)
    if values.size == 0:
        return _write(target, {"status": "empty", "eeg_file": str(eeg_path), "channels": []})
    sample_rate = _sample_rate(timestamps, float(dict(config or {}).get("expected_sample_rate_hz", 500.0)))
    cfg = dict(config or {})
    rows = [
        _channel_metrics(values[:, index], sample_rate, cfg, name)
        for index, name in enumerate(channels)
    ]
    quality_path = root / "raw" / "quality.csv"
    quality = _quality_stream_summary(quality_path)
    payload = {
        "schema_version": 1,
        "status": "ok",
        "eeg_file": str(eeg_path),
        "quality_stream_file": str(quality_path),
        "sample_rate_hz": sample_rate,
        "sample_count": int(values.shape[0]),
        "channel_count": int(values.shape[1]),
        "channels": rows,
        "quality_stream": quality,
    }
    return _write(target, payload)


def _channel_metrics(values: np.ndarray, sample_rate: float, config: dict[str, Any], name: str) -> dict[str, Any]:
    finite = np.asarray(values, dtype=float)
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        return {"channel": name, "status": "invalid", "finite_fraction": 0.0}
    centered = finite - np.median(finite)
    window = max(1, min(finite.size, int(round(sample_rate * float(config.get("drift_window_seconds", 5.0))))))
    window_medians = np.asarray([np.median(finite[start : start + window]) for start in range(0, finite.size, window)])
    differences = np.diff(finite)
    transient_threshold = float(config.get("transient_threshold_uv", 250.0))
    derivative_threshold = float(config.get("transient_derivative_threshold_uv", 100.0))
    line_ratio = _line_noise_ratio(centered, sample_rate, float(config.get("line_noise_hz", 60.0)))
    return {
        "channel": name,
        "status": "ok",
        "dc_offset_uv": float(np.median(finite)),
        "drift_uv": float(np.median(finite[-window:]) - np.median(finite[:window])),
        "drift_span_uv": float(np.ptp(window_medians)) if window_medians.size else 0.0,
        "line_noise_60hz_ratio": line_ratio,
        "flat_fraction": float(np.mean(np.abs(differences) <= float(config.get("flat_step_uv", 1e-9)))) if differences.size else 1.0,
        "transient_artifact_fraction": float(
            np.mean((np.abs(centered) >= transient_threshold) | _sample_derivative_mask(differences, centered.size, derivative_threshold))
        ),
        "standard_deviation_uv": float(np.std(centered)),
        "peak_to_peak_uv": float(np.ptp(centered)),
    }


def _line_noise_ratio(values: np.ndarray, sample_rate: float, line_hz: float) -> float | None:
    if sample_rate <= line_hz * 2.0 or values.size < 16:
        return None
    from scipy import signal

    frequencies, psd = signal.welch(values, fs=sample_rate, nperseg=min(values.size, max(256, int(sample_rate * 2))))
    line = (frequencies >= line_hz - 1.0) & (frequencies <= line_hz + 1.0)
    neighborhood = (frequencies >= line_hz - 5.0) & (frequencies <= line_hz + 5.0) & ~line
    if not line.any() or not neighborhood.any():
        return None
    return float(np.mean(psd[line]) / max(float(np.mean(psd[neighborhood])), 1e-20))


def _sample_derivative_mask(differences: np.ndarray, sample_count: int, threshold: float) -> np.ndarray:
    mask = np.zeros(sample_count, dtype=bool)
    if differences.size:
        indices = np.flatnonzero(np.abs(differences) >= threshold)
        mask[indices] = True
        mask[np.minimum(indices + 1, sample_count - 1)] = True
    return mask


def _quality_stream_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing"}
    channels, _, values = _read_signal_csv(path)
    if values.size == 0:
        return {"status": "empty"}
    return {
        "status": "ok",
        "sample_count": int(values.shape[0]),
        "value_semantics": "contact_quality_or_impedance_proxy",
        "channels": [
            {
                "channel": name,
                "median": float(np.nanmedian(values[:, index])),
                "minimum": float(np.nanmin(values[:, index])),
                "maximum": float(np.nanmax(values[:, index])),
            }
            for index, name in enumerate(channels)
        ],
    }


def _read_signal_csv(path: Path) -> tuple[list[str], np.ndarray, np.ndarray]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        rows = [[float(value) for value in row] for row in reader if row]
    matrix = np.asarray(rows, dtype=float)
    if matrix.ndim != 2 or matrix.shape[1] < 3:
        return [], np.empty((0,), dtype=float), np.empty((0, 0), dtype=float)
    return [str(value) for value in header[2:]], matrix[:, 0], matrix[:, 2:]


def _sample_rate(timestamps: np.ndarray, fallback: float) -> float:
    differences = np.diff(timestamps)
    valid = differences[np.isfinite(differences) & (differences > 0)]
    return float(1.0 / np.median(valid)) if valid.size else fallback


def _write(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["report_file"] = str(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload
