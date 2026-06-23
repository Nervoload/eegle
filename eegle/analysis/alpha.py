"""Alpha validation reports for Go/No-go sessions."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import numpy as np


def run_alpha_validation(session_dir: str | Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
    outdir = root / "reports" / "alpha"
    outdir.mkdir(parents=True, exist_ok=True)
    summary_path = outdir / "alpha_summary.json"
    trial_csv = outdir / "trial_alpha.csv"
    alpha_path = root / "realtime" / "alpha_power.jsonl"
    manifest_path = root / "events" / "stimulus_manifest.json"
    calibration_path = root / "calibration" / "alpha_calibration.json"
    calibration = _load_json(calibration_path)
    offline_alpha = _compute_offline_alpha_timeseries(root, config or {}, calibration or {})

    estimates = _load_jsonl(alpha_path)
    staged_packets = _load_jsonl(root / "realtime" / "event_features.jsonl")
    staged_alpha_quality = _assess_staged_alpha_quality(staged_packets)
    trials = _load_trials(manifest_path)
    if not estimates:
        if staged_alpha_quality["estimate_count"] > 0:
            return _write_summary(
                summary_path,
                {
                    "status": "ok_staged_event_features",
                    "reason": "continuous live alpha disabled; inhibition8 staged alpha features available",
                    "alpha_file": str(alpha_path),
                    "staged_feature_file": str(root / "realtime" / "event_features.jsonl"),
                    "trial_file": str(manifest_path),
                    "calibration": calibration,
                    "offline_alpha": offline_alpha,
                    "continuous_live_alpha": {
                        "status": "disabled_or_missing",
                        "estimate_count": 0,
                    },
                    "live_alpha_quality": staged_alpha_quality,
                    "staged_alpha_quality": staged_alpha_quality,
                    "behavior_prediction": {
                        "status": "delegated_to_exploratory_feature_behavior_analysis",
                    },
                },
            )
        return _write_summary(
            summary_path,
            {
                "status": "missing",
                "reason": "no realtime alpha estimates found",
                "alpha_file": str(alpha_path),
                "trial_file": str(manifest_path),
                "calibration": calibration,
                "offline_alpha": offline_alpha,
                "staged_alpha_quality": staged_alpha_quality,
            },
        )
    if not trials:
        return _write_summary(
            summary_path,
            {
                "status": "missing",
                "reason": "no Go/No-go trials found",
                "alpha_file": str(alpha_path),
                "trial_file": str(manifest_path),
                "estimate_count": len(estimates),
                "calibration": calibration,
                "offline_alpha": offline_alpha,
            },
        )

    live_alpha_quality = _assess_live_alpha_quality(estimates)
    usable_estimates = estimates if live_alpha_quality["usable"] else []
    timebase = _select_timebase(trials, estimates)
    trial_rows = [_trial_alpha_row(trial, usable_estimates, timebase) for trial in trials]
    _write_trial_csv(trial_csv, trial_rows)
    rt_pairs = [
        (row["pre_alpha"], row["reaction_time_seconds"])
        for row in trial_rows
        if row["pre_alpha"] is not None and row["reaction_time_seconds"] is not None
    ]
    acc_pairs = [
        (row["pre_alpha"], row["correct_press"])
        for row in trial_rows
        if row["pre_alpha"] is not None and row["correct_press"] is not None
    ]
    summary = {
        "status": "ok" if live_alpha_quality["usable"] else "invalid_live_alpha",
        "session_dir": str(root),
        "alpha_file": str(alpha_path),
        "trial_file": str(manifest_path),
        "trial_alpha_csv": str(trial_csv),
        "estimate_count": len(estimates),
        "trial_count": len(trial_rows),
        "timebase": timebase,
        "calibration": calibration,
        "offline_alpha": offline_alpha,
        "live_alpha_quality": live_alpha_quality,
        "staged_alpha_quality": staged_alpha_quality,
        "behavior_prediction": {
            "status": "ok" if live_alpha_quality["usable"] else "not_run_invalid_live_alpha",
            "pre_alpha_rt_correlation": _correlation(rt_pairs),
            "pre_alpha_accuracy_correlation": _correlation(acc_pairs),
            "n_rt_pairs": len(rt_pairs),
            "n_accuracy_pairs": len(acc_pairs),
        },
    }
    return _write_summary(summary_path, summary)


def _assess_staged_alpha_quality(packets: list[dict[str, Any]]) -> dict[str, Any]:
    alpha_stages = {"prestim_state", "alpha_erd"}
    rows = [row for row in packets if str(row.get("stage")) in alpha_stages and _packet_trial(row) >= 1]
    readiness = [
        value
        for row in rows
        if str(row.get("stage")) == "prestim_state"
        if (value := _optional_float(dict(row.get("features") or {}).get("readiness_alpha_power"))) is not None
    ]
    erd = [
        value
        for row in rows
        if str(row.get("stage")) == "alpha_erd"
        if (value := _optional_float(dict(row.get("features") or {}).get("alpha_erd_percent"))) is not None
    ]
    valid_count = sum(bool(row.get("valid", False)) for row in rows)
    reasons: list[str] = []
    if not readiness:
        reasons.append("readiness_alpha_missing")
    if not erd:
        reasons.append("alpha_erd_missing")
    valid_fraction = valid_count / len(rows) if rows else 0.0
    if rows and valid_fraction < 0.5:
        reasons.append("too_few_valid_staged_alpha_packets")
    usable = bool(rows) and not reasons
    return {
        "status": "usable" if usable else ("missing" if not rows else "invalid"),
        "usable": usable,
        "source": "inhibition8_staged_event_features",
        "estimate_count": len(rows),
        "readiness_estimate_count": len(readiness),
        "erd_estimate_count": len(erd),
        "valid_fraction": valid_fraction,
        "reasons": reasons,
        "readiness_power_min": min(readiness) if readiness else None,
        "readiness_power_max": max(readiness) if readiness else None,
        "readiness_power_std": float(np.std(readiness)) if readiness else None,
        "alpha_erd_percent_min": min(erd) if erd else None,
        "alpha_erd_percent_max": max(erd) if erd else None,
        "alpha_erd_percent_std": float(np.std(erd)) if erd else None,
    }


def _packet_trial(row: dict[str, Any]) -> int:
    try:
        return int(row.get("trial"))
    except (TypeError, ValueError):
        return -1


def _assess_live_alpha_quality(estimates: list[dict[str, Any]]) -> dict[str, Any]:
    powers = np.asarray(
        [value for row in estimates if (value := _optional_float(row.get("alpha_power"))) is not None],
        dtype=float,
    )
    z_values = [value for row in estimates if (value := _optional_float(row.get("alpha_power_z"))) is not None]
    valid_count = sum(bool(row.get("valid", True)) for row in estimates)
    channel_power_count = sum(isinstance(row.get("channel_alpha_power"), dict) and bool(row.get("channel_alpha_power")) for row in estimates)
    reasons: list[str] = []
    warnings: list[str] = []
    if powers.size == 0:
        reasons.append("no_finite_alpha_power")
        power_min = power_max = power_mean = power_std = None
    else:
        power_min = float(np.min(powers))
        power_max = float(np.max(powers))
        power_mean = float(np.mean(powers))
        power_std = float(np.std(powers))
        if powers.size >= 3 and power_max <= 1e-12:
            reasons.append("alpha_power_at_numerical_noise_scale")
        if powers.size >= 3 and power_std <= 1e-12:
            reasons.append("alpha_power_has_effectively_zero_variance")
    valid_fraction = valid_count / len(estimates) if estimates else 0.0
    z_fraction = len(z_values) / len(estimates) if estimates else 0.0
    channel_power_fraction = channel_power_count / len(estimates) if estimates else 0.0
    if valid_fraction < 0.5:
        reasons.append("too_few_artifact_valid_estimates")
    if z_fraction == 0.0:
        warnings.append("baseline_standardization_missing")
    if channel_power_fraction == 0.0:
        warnings.append("per_channel_alpha_power_missing")
    usable = not reasons
    return {
        "status": "usable" if usable and not warnings else ("usable_with_warnings" if usable else "invalid"),
        "usable": usable,
        "reasons": reasons,
        "warnings": warnings,
        "estimate_count": len(estimates),
        "finite_power_count": int(powers.size),
        "valid_fraction": valid_fraction,
        "z_scored_fraction": z_fraction,
        "per_channel_power_fraction": channel_power_fraction,
        "power_min": power_min,
        "power_max": power_max,
        "power_mean": power_mean,
        "power_std": power_std,
    }


def _compute_offline_alpha_timeseries(root: Path, config: dict[str, Any], calibration: dict[str, Any]) -> dict[str, Any]:
    raw_path = root / "raw" / "eeg.csv"
    outdir = root / "reports" / "alpha"
    outdir.mkdir(parents=True, exist_ok=True)
    output = outdir / "offline_alpha_timeseries.csv"
    raw = _load_raw_eeg(raw_path)
    if raw is None:
        return {"status": "missing", "reason": "raw EEG CSV not found", "file": str(output)}
    data = raw["data"]
    if data.size == 0:
        return {"status": "missing", "reason": "raw EEG CSV is empty", "file": str(output)}

    alpha_cfg = dict((config or {}).get("alpha", {}))
    realtime_alpha = dict((config or {}).get("realtime", {}).get("alpha", {}))
    band = dict(alpha_cfg.get("band") or realtime_alpha.get("band") or calibration.get("online_band") or {})
    low = float(band.get("low_hz", 8.0))
    high = float(band.get("high_hz", 12.0))
    window_seconds = float(alpha_cfg.get("offline_window_seconds", realtime_alpha.get("window_seconds", 1.0)))
    step_seconds = float(alpha_cfg.get("offline_step_seconds", realtime_alpha.get("step_seconds", 0.1)))
    sample_rate = float(raw["sample_rate_hz"])
    window = max(8, int(round(window_seconds * sample_rate)))
    step = max(1, int(round(step_seconds * sample_rate)))
    if data.shape[0] < window:
        return {"status": "missing", "reason": "not enough raw EEG samples for offline alpha window", "file": str(output)}

    channel_names = list(raw["channel_names"])
    posterior_channels = list(
        alpha_cfg.get("posterior_channels")
        or realtime_alpha.get("posterior_channels")
        or calibration.get("posterior_channels")
        or channel_names
    )
    posterior_indices = [channel_names.index(name) for name in posterior_channels if name in channel_names]
    if not posterior_indices:
        posterior_indices = list(range(len(channel_names)))
    fields = [
        "window_start_lsl_timestamp",
        "window_end_lsl_timestamp",
        "window_mid_lsl_timestamp",
        "window_start_monotonic",
        "window_end_monotonic",
        "window_mid_monotonic",
        "posterior_alpha_power",
    ]
    channel_fields = [f"{_safe_column_name(name)}_alpha_power" for name in channel_names]
    rows = []
    for start in range(0, data.shape[0] - window + 1, step):
        end = start + window
        chunk = data[start:end, :]
        powers = _channel_alpha_powers(chunk, sample_rate, low, high)
        posterior_power = float(np.nanmean(powers[posterior_indices])) if posterior_indices else float(np.nanmean(powers))
        lsl_start = float(raw["lsl_timestamps"][start])
        lsl_end = float(raw["lsl_timestamps"][end - 1])
        mono_start = float(raw["monotonic_timestamps"][start])
        mono_end = float(raw["monotonic_timestamps"][end - 1])
        row = {
            "window_start_lsl_timestamp": lsl_start,
            "window_end_lsl_timestamp": lsl_end,
            "window_mid_lsl_timestamp": (lsl_start + lsl_end) / 2.0,
            "window_start_monotonic": mono_start,
            "window_end_monotonic": mono_end,
            "window_mid_monotonic": (mono_start + mono_end) / 2.0,
            "posterior_alpha_power": posterior_power,
        }
        for field, value in zip(channel_fields, powers):
            row[field] = float(value)
        rows.append(row)

    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*fields, *channel_fields])
        writer.writeheader()
        writer.writerows(rows)
    return {
        "status": "ok",
        "file": str(output),
        "window_count": len(rows),
        "channel_count": len(channel_names),
        "posterior_channels": [channel_names[index] for index in posterior_indices],
        "band": {"low_hz": low, "high_hz": high},
        "window_seconds": window_seconds,
        "step_seconds": step_seconds,
    }


def _trial_alpha_row(trial: dict[str, Any], estimates: list[dict[str, Any]], timebase: str) -> dict[str, Any]:
    onset = trial.get("onset_lsl_timestamp") if timebase == "lsl" else trial.get("onset_monotonic")
    offset = trial.get("offset_lsl_timestamp") if timebase == "lsl" else trial.get("offset_monotonic")
    if onset is None:
        pre_values: list[float] = []
        stim_values: list[float] = []
    else:
        onset = float(onset)
        offset = float(offset if offset is not None else onset)
        pre_values = [_estimate_value(row) for row in estimates if _estimate_time(row, timebase) is not None and onset - 1.0 <= _estimate_time(row, timebase) <= onset]
        stim_values = [_estimate_value(row) for row in estimates if _estimate_time(row, timebase) is not None and onset <= _estimate_time(row, timebase) <= offset]
        pre_values = [value for value in pre_values if value is not None]
        stim_values = [value for value in stim_values if value is not None]
    response = dict(trial.get("response") or {})
    stimulus = dict(trial.get("stimulus") or {})
    return {
        "trial": int(trial.get("trial", -1)),
        "condition": "no_go" if stimulus.get("is_no_go") else "go",
        "pre_alpha": _mean_or_none(pre_values),
        "stimulus_alpha": _mean_or_none(stim_values),
        "reaction_time_seconds": _optional_float(response.get("reaction_time_seconds")),
        "correct_press": _optional_float(response.get("correct_press")),
        "button_press_count": _optional_float(response.get("button_press_count")),
    }


def _select_timebase(trials: list[dict[str, Any]], estimates: list[dict[str, Any]]) -> str:
    lsl_score = _timebase_overlap_score(trials, estimates, "lsl")
    monotonic_score = _timebase_overlap_score(trials, estimates, "monotonic")
    if monotonic_score > lsl_score:
        return "monotonic"
    if lsl_score > 0:
        return "lsl"
    return "monotonic" if monotonic_score > 0 else "lsl"


def _timebase_overlap_score(trials: list[dict[str, Any]], estimates: list[dict[str, Any]], timebase: str) -> int:
    onsets: list[float] = []
    offsets: list[float] = []
    for trial in trials:
        onset = _trial_time(trial, timebase, "onset")
        offset = _trial_time(trial, timebase, "offset")
        if onset is None:
            continue
        onsets.append(onset)
        offsets.append(offset if offset is not None else onset)
    estimate_times = [time for row in estimates if (time := _estimate_time(row, timebase)) is not None]
    if not onsets or not estimate_times:
        return 0
    lower = min(onsets) - 2.0
    upper = max(offsets) + 2.0
    estimate_hits = sum(1 for value in estimate_times if lower <= value <= upper)
    trial_hits = 0
    for onset, offset in zip(onsets, offsets):
        if any(onset - 1.0 <= value <= offset for value in estimate_times):
            trial_hits += 1
    return estimate_hits + trial_hits * 10


def _trial_time(trial: dict[str, Any], timebase: str, edge: str) -> float | None:
    if timebase == "lsl":
        return _optional_float(trial.get(f"{edge}_lsl_timestamp"))
    return _optional_float(trial.get(f"{edge}_monotonic"))


def _estimate_time(row: dict[str, Any], timebase: str) -> float | None:
    if timebase == "lsl":
        start = _optional_float(row.get("window_start_lsl_timestamp"))
        end = _optional_float(row.get("window_end_lsl_timestamp"))
        if start is not None and end is not None:
            return (start + end) / 2.0
    start = _optional_float(row.get("window_start_monotonic"))
    end = _optional_float(row.get("window_end_monotonic"))
    if start is not None and end is not None:
        return (start + end) / 2.0
    return _optional_float(row.get("created_at_monotonic"))


def _estimate_value(row: dict[str, Any]) -> float | None:
    z_value = _optional_float(row.get("alpha_power_z"))
    if z_value is not None:
        return z_value
    return _optional_float(row.get("alpha_power"))


def _load_trials(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    if not payload:
        return []
    return list(payload.get("trials", []))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_raw_eeg(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        rows = [[float(value) for value in row] for row in reader if row]
    if not rows:
        return {
            "data": np.empty((0, 0)),
            "lsl_timestamps": np.empty((0,)),
            "monotonic_timestamps": np.empty((0,)),
            "channel_names": header[2:],
            "sample_rate_hz": 0.0,
        }
    array = np.asarray(rows, dtype=float)
    lsl = array[:, 0]
    monotonic = array[:, 1] if len(header) > 1 and header[1] == "local_received_time" else array[:, 0]
    return {
        "data": array[:, 2:],
        "lsl_timestamps": lsl,
        "monotonic_timestamps": monotonic,
        "channel_names": [str(value) for value in header[2:]],
        "sample_rate_hz": _infer_sample_rate(lsl),
    }


def _infer_sample_rate(timestamps: np.ndarray) -> float:
    diffs = np.diff(np.asarray(timestamps, dtype=float))
    diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    if diffs.size == 0:
        return 1.0
    return float(1.0 / np.median(diffs))


def _channel_alpha_powers(chunk: np.ndarray, sample_rate_hz: float, low_hz: float, high_hz: float) -> np.ndarray:
    from scipy import signal

    values = np.asarray(chunk, dtype=float)
    nperseg = max(8, min(values.shape[0], int(round(sample_rate_hz))))
    freqs, psd = signal.welch(values, fs=sample_rate_hz, nperseg=nperseg, axis=0)
    mask = (freqs >= low_hz) & (freqs <= high_hz)
    if not mask.any():
        return np.zeros(values.shape[1], dtype=float)
    return np.trapezoid(psd[mask, :], freqs[mask], axis=0)


def _write_trial_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "trial",
                "condition",
                "pre_alpha",
                "stimulus_alpha",
                "reaction_time_seconds",
                "correct_press",
                "button_press_count",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_summary(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return payload


def _mean_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return float(np.mean(np.asarray(values, dtype=float)))


def _correlation(pairs: list[tuple[float | None, float | None]]) -> float | None:
    values = np.asarray([(x, y) for x, y in pairs if x is not None and y is not None], dtype=float)
    if values.shape[0] < 3:
        return None
    if np.std(values[:, 0]) <= 1e-12 or np.std(values[:, 1]) <= 1e-12:
        return None
    return float(np.corrcoef(values[:, 0], values[:, 1])[0, 1])


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
        return None if math.isnan(number) else number
    except (TypeError, ValueError):
        return None


def _safe_column_name(name: str) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in str(name))
    return cleaned.strip("_") or "channel"
