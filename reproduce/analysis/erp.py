"""ERP analysis and dashboard generation for task sessions."""

from __future__ import annotations

import csv
import html
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from reproduce.hardware.enobio import expected_profile


DEFAULT_ERP_CONFIG: dict[str, Any] = {
    "enabled": True,
    "task": "go_nogo",
    "tmin_seconds": -0.2,
    "tmax_seconds": 0.8,
    "baseline_seconds": [-0.2, 0.0],
    "n2_window_seconds": [0.2, 0.35],
    "p300_window_seconds": [0.25, 0.6],
    "filter_low_hz": 0.1,
    "filter_high_hz": 30.0,
    "notch_hz": 60.0,
    "input_units": "microvolts",
    "roi_channels": ["Pz", "Cz", "CPz", "P3", "P4"],
    "n2_roi_channels": ["Fz", "Cz"],
    "dashboard_montage_times_ms": [-200, 0, 100, 200, 300, 400, 500, 600, 700, 800],
    "dashboard_px_per_second": 220,
}


@dataclass
class EegBundle:
    raw: Any
    lsl_timestamps: np.ndarray
    local_received_times: np.ndarray
    channel_names: list[str]
    sample_rate_hz: float
    alignment_note: str


def run_erp_analysis(session_dir: str | Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run Go/No-go ERP analysis for a completed session."""
    cfg = _merged_erp_config(config)
    root = Path(session_dir).expanduser().resolve()
    outdir = root / "reports" / "erp"
    outdir.mkdir(parents=True, exist_ok=True)
    summary_path = outdir / "erp_summary.json"

    if not cfg.get("enabled", True):
        return _write_summary(summary_path, {"status": "disabled", "reason": "ERP analysis disabled"})

    try:
        import mne
    except Exception as exc:
        return _write_summary(
            summary_path,
            {"status": "missing_dependency", "error": f"mne import failed: {type(exc).__name__}: {exc}"},
        )

    trials = _load_go_nogo_trials(root)
    if not trials:
        return _write_summary(summary_path, {"status": "missing", "reason": "no Go/No-go stimulus trials found"})

    raw_path = root / "raw" / "eeg.csv"
    if not raw_path.exists():
        return _write_summary(
            summary_path,
            {
                "status": "missing",
                "reason": "raw EEG CSV not found",
                "raw_file": str(raw_path),
                "trial_count": len(trials),
            },
        )

    try:
        eeg = _load_eeg_csv(root, cfg, mne)
    except Exception as exc:
        return _write_summary(
            summary_path,
            {"status": "failed", "stage": "load_eeg", "error": f"{type(exc).__name__}: {exc}"},
        )

    events, valid_trials, event_id, event_notes = _events_from_trials(
        trials,
        eeg.local_received_times,
        cfg,
        eeg.raw.n_times,
    )
    if events.size == 0:
        return _write_summary(
            summary_path,
            {
                "status": "no_epochs",
                "reason": "no stimulus onsets fell inside the EEG recording epoch window",
                "raw_file": str(raw_path),
                "trial_count": len(trials),
                "alignment_note": eeg.alignment_note,
                "event_notes": event_notes,
            },
        )

    raw = eeg.raw.copy()
    _filter_raw(raw, cfg)
    baseline = tuple(float(value) for value in cfg.get("baseline_seconds", [-0.2, 0.0]))
    tmin = float(cfg.get("tmin_seconds", -0.2))
    tmax = float(cfg.get("tmax_seconds", 0.8))
    try:
        epochs = mne.Epochs(
            raw,
            events,
            event_id=event_id,
            tmin=tmin,
            tmax=tmax,
            baseline=baseline,
            picks="eeg",
            preload=True,
            reject_by_annotation=False,
            event_repeated="drop",
            verbose=False,
        )
    except Exception as exc:
        return _write_summary(
            summary_path,
            {"status": "failed", "stage": "epoch", "error": f"{type(exc).__name__}: {exc}"},
        )

    if len(epochs) == 0:
        return _write_summary(
            summary_path,
            {
                "status": "no_epochs",
                "reason": "MNE produced zero usable epochs",
                "event_count": int(events.shape[0]),
                "event_notes": event_notes,
            },
        )

    evokeds = _make_evokeds(epochs, event_id)
    roi_channels, roi_indices = _resolve_roi(epochs.ch_names, cfg)
    n2_roi_channels, n2_roi_indices = _resolve_named_roi(epochs.ch_names, cfg.get("n2_roi_channels", ["Fz", "Cz"]))
    p300 = _p300_summary(evokeds, roi_indices, cfg)
    n2 = _n2_summary(evokeds, n2_roi_indices, cfg)
    trial_epochs = _trial_epoch_records(epochs, valid_trials, roi_indices, n2_roi_indices, cfg)
    waveform_csv = outdir / "erp_roi_waveforms.csv"
    trial_csv = outdir / "p300_by_trial.csv"
    waveform_png = outdir / "erp_waveforms.png"
    dashboard_html = outdir / "go_nogo_erp_dashboard.html"

    _write_waveform_csv(waveform_csv, evokeds, roi_indices, n2_roi_indices)
    _write_trial_p300_csv(trial_csv, trial_epochs)
    _write_waveform_plot(waveform_png, evokeds, roi_indices, n2_roi_indices, cfg)
    _write_dashboard(dashboard_html, waveform_png.name, trial_epochs, evokeds, cfg, p300, n2)

    condition_counts = {
        name: int(np.sum(events[:, 2] == code))
        for name, code in event_id.items()
    }
    summary = {
        "status": "ok",
        "session_dir": str(root),
        "task": "go_nogo",
        "raw_file": str(raw_path),
        "trial_count": len(trials),
        "epoch_count": int(len(epochs)),
        "dashboard_epoch_count": len(trial_epochs),
        "condition_counts": condition_counts,
        "sample_rate_hz": eeg.sample_rate_hz,
        "channel_names": eeg.channel_names,
        "roi_channels": roi_channels,
        "n2_roi_channels": n2_roi_channels,
        "alignment": {
            "method": "stimulus_monotonic_to_eeg_local_received_time",
            "note": eeg.alignment_note,
            "event_notes": event_notes,
        },
        "epoch_window_seconds": [tmin, tmax],
        "baseline_seconds": list(baseline),
        "p300_window_seconds": list(cfg.get("p300_window_seconds", [0.25, 0.6])),
        "n2_window_seconds": list(cfg.get("n2_window_seconds", [0.2, 0.35])),
        "p300": p300,
        "n2": n2,
        "files": {
            "summary_json": str(summary_path),
            "waveforms_csv": str(waveform_csv),
            "trial_p300_csv": str(trial_csv),
            "waveform_png": str(waveform_png),
            "dashboard_html": str(dashboard_html),
        },
    }
    return _write_summary(summary_path, summary)


def _merged_erp_config(config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(DEFAULT_ERP_CONFIG)
    if config:
        cfg.update(config)
    return cfg


def _load_go_nogo_trials(root: Path) -> list[dict[str, Any]]:
    manifest = _load_json(root / "events" / "stimulus_manifest.json")
    if manifest and manifest.get("task") == "go_nogo":
        return [
            {
                "trial": int(row["trial"]),
                "stimulus_id": row.get("stimulus_id"),
                "stimulus": row.get("stimulus", {}),
                "onset_monotonic": float(row["onset_monotonic"]),
                "offset_monotonic": float(row["offset_monotonic"]),
                "timestamp_since_start": float(row.get("timestamp_since_start", 0.0)),
                "response": row.get("response", {}),
            }
            for row in manifest.get("trials", [])
        ]

    results = _load_json(root / "events" / "go_nogo_results.json")
    if not results or results.get("task") != "go_nogo":
        return []
    trials = []
    for row in results.get("trials", []):
        stimulus = row.get("stimulus") or {
            "id": row.get("stimulus_id"),
            "kind": "geometry",
            "shape": row.get("shape"),
            "color": row.get("color"),
            "is_no_go": bool(row.get("is_no_go")),
            "label": "NO-GO" if row.get("is_no_go") else "GO",
            "asset_path": None,
            "asset_sha256": None,
        }
        trials.append(
            {
                "trial": int(row["stimulus_number"]),
                "stimulus_id": row.get("stimulus_id"),
                "stimulus": stimulus,
                "onset_monotonic": float(row["stimulus_onset_monotonic"]),
                "offset_monotonic": float(row["stimulus_offset_monotonic"]),
                "timestamp_since_start": float(row.get("timestamp_since_start", 0.0)),
                "response": {
                    "reaction_time_seconds": row.get("reaction_time_seconds"),
                    "button_press_count": row.get("button_press_count"),
                    "correct_press": row.get("correct_press"),
                    "presses": row.get("presses", []),
                },
            }
        )
    return trials


def _load_eeg_csv(root: Path, cfg: dict[str, Any], mne: Any) -> EegBundle:
    import pandas as pd

    raw_path = root / "raw" / "eeg.csv"
    metadata = _load_json(root / "raw" / "eeg_metadata.json") or {}
    parameters = _load_json(root / "parameters.json") or {}
    frame = pd.read_csv(raw_path)
    if "lsl_timestamp" not in frame or "local_received_time" not in frame:
        raise ValueError("EEG CSV must include lsl_timestamp and local_received_time columns")

    channel_columns = [column for column in frame.columns if column not in {"lsl_timestamp", "local_received_time"}]
    if not channel_columns:
        raise ValueError("EEG CSV has no channel columns")

    lsl_timestamps = frame["lsl_timestamp"].to_numpy(dtype=float)
    local_received_times = frame["local_received_time"].to_numpy(dtype=float)
    sample_rate = _infer_sample_rate(lsl_timestamps, metadata, parameters)
    channel_names = _infer_channel_names(channel_columns, parameters)
    ch_types = ["stim" if name.upper() in {"TRG", "STI", "STIM"} else "eeg" for name in channel_names]
    data = frame[channel_columns].to_numpy(dtype=float).T * _unit_scale_to_volts(str(cfg.get("input_units", "microvolts")))
    info = mne.create_info(channel_names, sample_rate, ch_types=ch_types)
    raw = mne.io.RawArray(data, info, verbose=False)
    try:
        montage = mne.channels.make_standard_montage("standard_1020")
        raw.set_montage(montage, on_missing="ignore", verbose=False)
    except Exception:
        pass
    return EegBundle(
        raw=raw,
        lsl_timestamps=lsl_timestamps,
        local_received_times=local_received_times,
        channel_names=channel_names,
        sample_rate_hz=sample_rate,
        alignment_note=(
            "Stimulus onsets are stored with Python monotonic time. The current CSV recorder stores EEG LSL timestamps "
            "and local receipt monotonic times, so ERP event samples are estimated from local receipt time. Use XDF or "
            "recorded marker streams for tighter timing validation."
        ),
    )


def _infer_sample_rate(lsl_timestamps: np.ndarray, metadata: dict[str, Any], parameters: dict[str, Any]) -> float:
    stream = metadata.get("stream") or {}
    nominal = float(stream.get("nominal_srate") or 0.0)
    if nominal > 0:
        return nominal
    diffs = np.diff(lsl_timestamps)
    diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    if diffs.size:
        return float(1.0 / np.median(diffs))
    return float(parameters.get("hardware", {}).get("eeg", {}).get("expected_sample_rate_hz", 500.0))


def _infer_channel_names(channel_columns: list[str], parameters: dict[str, Any]) -> list[str]:
    profile_name = parameters.get("hardware", {}).get("eeg", {}).get("profile")
    if profile_name:
        try:
            profile = expected_profile(str(profile_name))
            if len(profile.channel_names) == len(channel_columns):
                return list(profile.channel_names)
        except Exception:
            pass
    return [str(column) for column in channel_columns]


def _unit_scale_to_volts(units: str) -> float:
    normalized = units.lower()
    if normalized in {"v", "volt", "volts"}:
        return 1.0
    if normalized in {"mv", "millivolt", "millivolts"}:
        return 1e-3
    if normalized in {"uv", "microvolt", "microvolts"}:
        return 1e-6
    return 1e-6


def _events_from_trials(
    trials: list[dict[str, Any]],
    local_received_times: np.ndarray,
    cfg: dict[str, Any],
    n_times: int,
) -> tuple[np.ndarray, list[dict[str, Any]], dict[str, int], list[str]]:
    tmin = float(cfg.get("tmin_seconds", -0.2))
    tmax = float(cfg.get("tmax_seconds", 0.8))
    event_id = {"go": 1, "no_go": 2}
    index_times, index_values = _unique_time_to_sample_index(local_received_times)
    notes: list[str] = []
    events = []
    valid_trials = []
    if index_times.size == 0:
        return np.empty((0, 3), dtype=int), [], event_id, ["EEG local receipt time column is empty"]
    first_time = float(index_times[0])
    last_time = float(index_times[-1])
    sfreq = float(n_times - 1) / max(1e-9, local_received_times[-1] - local_received_times[0])
    pre_samples = int(math.ceil(abs(tmin) * sfreq))
    post_samples = int(math.ceil(tmax * sfreq))
    for trial in trials:
        onset = float(trial["onset_monotonic"])
        if onset < first_time or onset > last_time:
            notes.append(f"trial {trial['trial']} onset outside EEG local time range")
            continue
        sample = int(round(float(np.interp(onset, index_times, index_values))))
        if sample - pre_samples < 0 or sample + post_samples >= n_times:
            notes.append(f"trial {trial['trial']} epoch window outside EEG samples")
            continue
        stimulus = trial.get("stimulus", {})
        condition = "no_go" if stimulus.get("is_no_go") else "go"
        events.append([sample, 0, event_id[condition]])
        valid_trials.append(trial)
    events_array = np.asarray(events, dtype=int)
    if events_array.size:
        order = np.argsort(events_array[:, 0])
        events_array = events_array[order]
        valid_trials = [valid_trials[int(idx)] for idx in order]
    return events_array.reshape((-1, 3)), valid_trials, event_id, notes


def _unique_time_to_sample_index(times: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    finite = np.asarray(times, dtype=float)
    indices = np.arange(finite.shape[0], dtype=float)
    mask = np.isfinite(finite)
    finite = finite[mask]
    indices = indices[mask]
    if finite.size == 0:
        return finite, indices
    unique, inverse = np.unique(finite, return_inverse=True)
    sums = np.bincount(inverse, weights=indices)
    counts = np.bincount(inverse)
    return unique, sums / np.maximum(counts, 1)


def _filter_raw(raw: Any, cfg: dict[str, Any]) -> None:
    notch = cfg.get("notch_hz")
    if notch:
        try:
            raw.notch_filter(
                freqs=[float(notch)],
                picks="eeg",
                method="iir",
                verbose=False,
            )
        except Exception:
            pass
    low = cfg.get("filter_low_hz")
    high = cfg.get("filter_high_hz")
    if low or high:
        raw.filter(
            l_freq=None if low is None else float(low),
            h_freq=None if high is None else float(high),
            picks="eeg",
            method="iir",
            iir_params={"order": 4, "ftype": "butter"},
            verbose=False,
        )


def _make_evokeds(epochs: Any, event_id: dict[str, int]) -> dict[str, Any]:
    evokeds = {"all": epochs.average()}
    for condition, code in event_id.items():
        if int(np.sum(epochs.events[:, 2] == code)) > 0:
            evokeds[condition] = epochs[condition].average()
    return evokeds


def _resolve_roi(ch_names: list[str], cfg: dict[str, Any]) -> tuple[list[str], list[int]]:
    desired = [str(name) for name in cfg.get("roi_channels", [])]
    selected = [name for name in desired if name in ch_names]
    if not selected:
        selected = list(ch_names)
    return selected, [ch_names.index(name) for name in selected]


def _resolve_named_roi(ch_names: list[str], desired: Any) -> tuple[list[str], list[int]]:
    selected = [str(name) for name in (desired or []) if str(name) in ch_names]
    return selected, [ch_names.index(name) for name in selected]


def _p300_summary(evokeds: dict[str, Any], roi_indices: list[int], cfg: dict[str, Any]) -> dict[str, Any]:
    pmin, pmax = [float(value) for value in cfg.get("p300_window_seconds", [0.25, 0.6])]
    summary: dict[str, Any] = {}
    for condition, evoked in evokeds.items():
        times = evoked.times
        mask = (times >= pmin) & (times <= pmax)
        if not mask.any():
            continue
        roi_uv = evoked.data[roi_indices].mean(axis=0) * 1e6
        mask_indices = np.flatnonzero(mask)
        peak_idx = int(mask_indices[np.argmax(roi_uv[mask])])
        summary[condition] = {
            "amplitude_uv": float(roi_uv[peak_idx]),
            "latency_ms": float(times[peak_idx] * 1000.0),
        }
    if "go" in summary and "no_go" in summary:
        summary["no_go_minus_go"] = {
            "amplitude_uv": float(summary["no_go"]["amplitude_uv"] - summary["go"]["amplitude_uv"]),
        }
    return summary


def _n2_summary(evokeds: dict[str, Any], roi_indices: list[int], cfg: dict[str, Any]) -> dict[str, Any]:
    if not roi_indices:
        return {"status": "missing_required_roi", "required_channels": list(cfg.get("n2_roi_channels", ["Fz", "Cz"]))}
    nmin, nmax = [float(value) for value in cfg.get("n2_window_seconds", [0.2, 0.35])]
    summary: dict[str, Any] = {}
    for condition, evoked in evokeds.items():
        times = evoked.times
        mask = (times >= nmin) & (times <= nmax)
        if not mask.any():
            continue
        roi_uv = evoked.data[roi_indices].mean(axis=0) * 1e6
        mask_indices = np.flatnonzero(mask)
        peak_idx = int(mask_indices[np.argmin(roi_uv[mask])])
        summary[condition] = {
            "amplitude_uv": float(roi_uv[peak_idx]),
            "mean_amplitude_uv": float(np.mean(roi_uv[mask])),
            "latency_ms_exploratory": float(times[peak_idx] * 1000.0),
        }
    if "go" in summary and "no_go" in summary:
        summary["no_go_minus_go"] = {
            "amplitude_uv": float(summary["no_go"]["amplitude_uv"] - summary["go"]["amplitude_uv"]),
            "mean_amplitude_uv": float(summary["no_go"]["mean_amplitude_uv"] - summary["go"]["mean_amplitude_uv"]),
        }
    return summary


def _write_waveform_csv(path: Path, evokeds: dict[str, Any], roi_indices: list[int], n2_roi_indices: list[int]) -> None:
    conditions = [name for name in ("all", "go", "no_go") if name in evokeds]
    times = evokeds["all"].times
    waves = {
        condition: evokeds[condition].data[roi_indices].mean(axis=0) * 1e6
        for condition in conditions
    }
    n2_waves = {
        condition: evokeds[condition].data[n2_roi_indices].mean(axis=0) * 1e6
        for condition in conditions
    } if n2_roi_indices else {}
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "time_ms",
            *[f"{condition}_p3_roi_uv" for condition in conditions],
            *[f"{condition}_n2_roi_uv" for condition in conditions if condition in n2_waves],
        ])
        for idx, time_value in enumerate(times):
            writer.writerow([
                f"{time_value * 1000.0:.3f}",
                *[f"{waves[condition][idx]:.6f}" for condition in conditions],
                *[f"{n2_waves[condition][idx]:.6f}" for condition in conditions if condition in n2_waves],
            ])


def _trial_epoch_records(
    epochs: Any,
    valid_trials: list[dict[str, Any]],
    roi_indices: list[int],
    n2_roi_indices: list[int],
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    pmin, pmax = [float(value) for value in cfg.get("p300_window_seconds", [0.25, 0.6])]
    times = epochs.times
    mask = (times >= pmin) & (times <= pmax)
    data_uv = epochs.get_data() * 1e6
    records: list[dict[str, Any]] = []
    if not mask.any():
        return records
    mask_indices = np.flatnonzero(mask)
    nmin, nmax = [float(value) for value in cfg.get("n2_window_seconds", [0.2, 0.35])]
    n2_mask = (times >= nmin) & (times <= nmax)
    n2_mask_indices = np.flatnonzero(n2_mask)
    for epoch_idx, original_idx in enumerate(epochs.selection):
        if int(original_idx) >= len(valid_trials):
            continue
        trial = valid_trials[int(original_idx)]
        stimulus = trial.get("stimulus", {})
        roi_wave = data_uv[epoch_idx, roi_indices, :].mean(axis=0)
        peak_idx = int(mask_indices[np.argmax(roi_wave[mask])])
        n2_wave = data_uv[epoch_idx, n2_roi_indices, :].mean(axis=0) if n2_roi_indices else np.full_like(roi_wave, np.nan)
        n2_peak_idx = int(n2_mask_indices[np.argmin(n2_wave[n2_mask])]) if n2_roi_indices and n2_mask.any() else None
        condition = "no_go" if stimulus.get("is_no_go") else "go"
        records.append(
            {
                "trial": int(trial["trial"]),
                "condition": condition,
                "stimulus_id": trial.get("stimulus_id") or stimulus.get("id"),
                "stimulus": stimulus,
                "timestamp_since_start": float(trial.get("timestamp_since_start", 0.0)),
                "response": trial.get("response", {}),
                "times_ms": (times * 1000.0).astype(float),
                "channel_names": list(epochs.ch_names),
                "epoch_uv": data_uv[epoch_idx],
                "roi_wave_uv": roi_wave.astype(float),
                "p300_amplitude_uv": float(roi_wave[peak_idx]),
                "p300_latency_ms": float(times[peak_idx] * 1000.0),
                "n2_mean_amplitude_uv": None if n2_peak_idx is None else float(np.mean(n2_wave[n2_mask])),
                "n2_amplitude_uv": None if n2_peak_idx is None else float(n2_wave[n2_peak_idx]),
                "n2_latency_ms_exploratory": None if n2_peak_idx is None else float(times[n2_peak_idx] * 1000.0),
            }
        )
    return records


def _write_trial_p300_csv(path: Path, trial_epochs: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "trial", "condition", "stimulus_id", "shape", "color",
            "n2_mean_amplitude_uv", "n2_amplitude_uv", "n2_latency_ms_exploratory",
            "p300_amplitude_uv", "p300_latency_ms",
        ])
        for record in trial_epochs:
            stimulus = record.get("stimulus", {})
            writer.writerow(
                [
                    record["trial"],
                    record["condition"],
                    record.get("stimulus_id"),
                    stimulus.get("shape"),
                    stimulus.get("color"),
                    "" if record.get("n2_mean_amplitude_uv") is None else f"{float(record['n2_mean_amplitude_uv']):.6f}",
                    "" if record.get("n2_amplitude_uv") is None else f"{float(record['n2_amplitude_uv']):.6f}",
                    "" if record.get("n2_latency_ms_exploratory") is None else f"{float(record['n2_latency_ms_exploratory']):.3f}",
                    f"{float(record['p300_amplitude_uv']):.6f}",
                    f"{float(record['p300_latency_ms']):.3f}",
                ]
            )


def _write_waveform_plot(path: Path, evokeds: dict[str, Any], roi_indices: list[int], n2_roi_indices: list[int], cfg: dict[str, Any]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    pmin, pmax = [float(value) * 1000.0 for value in cfg.get("p300_window_seconds", [0.25, 0.6])]
    nmin, nmax = [float(value) * 1000.0 for value in cfg.get("n2_window_seconds", [0.2, 0.35])]
    colors = {"all": "#303030", "go": "#2970ff", "no_go": "#cc2b5e"}
    rows = 2 if n2_roi_indices else 1
    fig, axes = plt.subplots(rows, 1, figsize=(8.5, 4.3 * rows), dpi=140, squeeze=False)
    panels = [(axes[0, 0], roi_indices, "P3 ROI waveform", pmin, pmax, "#f6c453", "P3 window")]
    if n2_roi_indices:
        panels.append((axes[1, 0], n2_roi_indices, "N2 ROI waveform", nmin, nmax, "#93c5fd", "N2 window"))
    for ax, indices, title, window_min, window_max, shade, window_label in panels:
        for condition in ("all", "go", "no_go"):
            if condition not in evokeds:
                continue
            evoked = evokeds[condition]
            roi_uv = evoked.data[indices].mean(axis=0) * 1e6
            ax.plot(evoked.times * 1000.0, roi_uv, label=condition.replace("_", "-"), color=colors.get(condition))
        ax.axvline(0, color="#111111", linewidth=1, alpha=0.7)
        ax.axvspan(window_min, window_max, color=shade, alpha=0.25, label=window_label)
        ax.set_xlabel("Time from stimulus onset (ms)")
        ax.set_ylabel("ROI amplitude (uV)")
        ax.set_title(title)
        ax.legend(loc="best")
        ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def _write_dashboard(
    path: Path,
    waveform_png_name: str,
    trial_epochs: list[dict[str, Any]],
    evokeds: dict[str, Any],
    cfg: dict[str, Any],
    p300: dict[str, Any],
    n2: dict[str, Any],
) -> None:
    frame_times_ms = [int(value) for value in cfg.get("dashboard_montage_times_ms", [])]
    if not frame_times_ms:
        frame_times_ms = [-200, 0, 100, 200, 300, 400, 500, 600, 700, 800]
    scale_uv = _dashboard_scale_uv(evokeds, trial_epochs)
    p300_html = _erp_metrics_html(p300, n2)
    trial_panels = "".join(_trial_interval_panel_html(record, frame_times_ms, scale_uv, cfg) for record in trial_epochs)
    if not trial_panels:
        trial_panels = '<div class="empty-state">No stimulus-locked epochs were available for the dashboard.</div>'
    body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Go/No-go ERP Dashboard</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f6f2; color: #171717; }}
    main {{ padding: 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    h2 {{ margin: 0 0 12px; font-size: 16px; }}
    .subtle {{ color: #62625d; margin: 0 0 20px; max-width: 960px; line-height: 1.45; }}
    .summary {{ display: grid; grid-template-columns: minmax(280px, 420px) minmax(420px, 1fr); gap: 20px; align-items: start; margin-bottom: 20px; }}
    .panel {{ background: white; border: 1px solid #d8d8d2; border-radius: 8px; padding: 16px; }}
    .panel img {{ max-width: 100%; display: block; }}
    .scroller {{ overflow-x: auto; overflow-y: hidden; border: 1px solid #cfcfc8; background: white; border-radius: 8px; padding: 14px; }}
    .epoch-strip {{ display: flex; gap: 16px; min-width: max-content; align-items: stretch; }}
    .trial-panel {{ flex: 0 0 860px; border: 1px solid #d9d9d2; border-radius: 8px; background: #fff; overflow: hidden; }}
    .trial-header {{ display: grid; grid-template-columns: 172px 1fr 180px; gap: 14px; align-items: center; padding: 14px; border-bottom: 1px solid #ededdf; background: #fbfbf8; min-height: 148px; }}
    .stim-preview svg {{ display: block; width: 150px; height: 116px; border-radius: 6px; box-shadow: 0 1px 5px rgba(0,0,0,.18); }}
    .condition {{ display: inline-block; font-size: 12px; font-weight: 700; letter-spacing: .02em; text-transform: uppercase; padding: 3px 7px; border-radius: 999px; background: #e7eefc; color: #1f4fa3; }}
    .condition.nogo {{ background: #fbe5ec; color: #b51f55; }}
    .trial-title {{ margin: 8px 0 3px; font-size: 18px; font-weight: 700; }}
    .metadata {{ color: #575753; font-size: 13px; line-height: 1.45; }}
    .p3-box {{ border-left: 1px solid #deded8; padding-left: 14px; font-size: 13px; color: #393936; }}
    .p3-box strong {{ display: block; font-size: 21px; color: #181815; margin-bottom: 3px; }}
    .interval-body {{ padding: 14px; }}
    .wave-svg {{ display: block; width: 100%; height: 172px; margin-bottom: 12px; border: 1px solid #e2e2dc; border-radius: 6px; background: #fff; }}
    .frame-row {{ display: flex; gap: 8px; align-items: flex-start; border-top: 1px solid #ededdf; padding-top: 12px; }}
    .montage-frame {{ width: 74px; flex: 0 0 74px; font-size: 10px; text-align: center; color: #454541; }}
    .montage-frame svg {{ display: block; margin: 0 auto 4px; width: 70px; height: 76px; }}
    .frame-label {{ display: block; white-space: nowrap; }}
    .empty-state {{ width: 620px; padding: 40px; color: #61615b; }}
    table {{ border-collapse: collapse; width: 100%; }}
    td, th {{ border-bottom: 1px solid #e5e5e0; padding: 6px 8px; text-align: left; }}
    @media (max-width: 820px) {{
      main {{ padding: 16px; }}
      .summary {{ grid-template-columns: 1fr; }}
      .trial-panel {{ flex-basis: 760px; }}
      .trial-header {{ grid-template-columns: 150px 1fr 160px; }}
    }}
  </style>
</head>
<body>
<main>
  <h1>Go/No-go ERP Dashboard</h1>
    <p class="subtle">The aggregate report includes separate N2 and P3 ROI waveforms and windows. Each horizontal card is one stimulus-locked interval with exploratory peak estimates; the montage strip shows EEG topography at fixed times around that same stimulus onset.</p>
  <section class="summary">
    <div class="panel">{p300_html}</div>
    <div class="panel"><img src="{html.escape(waveform_png_name)}" alt="ERP ROI waveform"></div>
  </section>
  <section class="scroller" aria-label="Stimulus-locked Go/No-go ERP intervals">
    <div class="epoch-strip">{trial_panels}</div>
  </section>
</main>
</body>
</html>
"""
    path.write_text(body, encoding="utf-8")


def _dashboard_scale_uv(evokeds: dict[str, Any], trial_epochs: list[dict[str, Any]]) -> float:
    values = []
    for evoked in evokeds.values():
        values.append(np.nanmax(np.abs(evoked.data * 1e6)))
    for record in trial_epochs:
        epoch = record.get("epoch_uv")
        if isinstance(epoch, np.ndarray) and epoch.size:
            values.append(float(np.nanpercentile(np.abs(epoch), 98)))
    return max(1.0, float(np.nanmax(values)) if values else 1.0)


def _trial_interval_panel_html(record: dict[str, Any], frame_times_ms: list[int], scale_uv: float, cfg: dict[str, Any]) -> str:
    stimulus = record.get("stimulus", {})
    condition = str(record.get("condition", "go"))
    response = record.get("response", {}) or {}
    rt = response.get("reaction_time_seconds")
    correct = response.get("correct_press")
    press_count = response.get("button_press_count")
    condition_label = "NO-GO" if condition == "no_go" else "GO"
    condition_class = "nogo" if condition == "no_go" else "go"
    shape = stimulus.get("shape", "")
    color = stimulus.get("color", "")
    p3_amp = float(record.get("p300_amplitude_uv", 0.0))
    p3_latency = float(record.get("p300_latency_ms", 0.0))
    n2_amp = record.get("n2_mean_amplitude_uv")
    n2_latency = record.get("n2_latency_ms_exploratory")
    wave_svg = _roi_waveform_svg(record["times_ms"], record["roi_wave_uv"], p3_latency, p3_amp, cfg)
    montage_frames = []
    epoch_uv = record["epoch_uv"]
    times_ms = record["times_ms"]
    for frame_ms in frame_times_ms:
        idx = int(np.argmin(np.abs(times_ms - float(frame_ms))))
        values = epoch_uv[:, idx]
        svg = _montage_svg(record["channel_names"], values, scale_uv)
        montage_frames.append(
            '<div class="montage-frame" '
            f'title="Trial {record["trial"]} {frame_ms:+d} ms">{svg}'
            f'<span class="frame-label">{frame_ms:+d} ms</span></div>'
        )

    rt_text = "none" if rt in {None, ""} else f"{float(rt) * 1000.0:.0f} ms"
    correct_text = "n/a" if correct in {None, ""} else ("yes" if bool(int(correct)) else "no")
    press_text = "n/a" if press_count in {None, ""} else str(press_count)
    return f"""
      <article class="trial-panel">
        <header class="trial-header">
          <div class="stim-preview">{_stimulus_svg(stimulus)}</div>
          <div>
            <span class="condition {condition_class}">{condition_label}</span>
            <div class="trial-title">Trial {int(record["trial"])}</div>
            <div class="metadata">
              Stimulus: {html.escape(str(color))} {html.escape(str(shape))}<br>
              Session onset: {float(record.get("timestamp_since_start", 0.0)):.3f} s<br>
              Response: {html.escape(rt_text)} · presses: {html.escape(press_text)} · correct: {html.escape(correct_text)}
            </div>
          </div>
          <div class="p3-box">
            <strong>{p3_amp:.2f} uV</strong>
            P3 estimate<br>
            {p3_latency:.0f} ms latency<br><br>
            <strong>{'n/a' if n2_amp is None else f'{float(n2_amp):.2f} uV'}</strong>
            N2 mean<br>
            {'n/a' if n2_latency is None else f'{float(n2_latency):.0f} ms exploratory latency'}
          </div>
        </header>
        <div class="interval-body">
          {wave_svg}
          <div class="frame-row">{''.join(montage_frames)}</div>
        </div>
      </article>
    """


def _roi_waveform_svg(
    times_ms: np.ndarray,
    roi_wave_uv: np.ndarray,
    p3_latency_ms: float,
    p3_amplitude_uv: float,
    cfg: dict[str, Any],
) -> str:
    width = 820
    height = 172
    left = 54
    right = 806
    top = 18
    bottom = 132
    mid = (top + bottom) / 2
    t_min = float(np.nanmin(times_ms))
    t_max = float(np.nanmax(times_ms))
    amp_scale = max(1.0, float(np.nanpercentile(np.abs(roi_wave_uv), 98)), abs(float(p3_amplitude_uv)))
    x = left + (times_ms - t_min) / max(1e-9, t_max - t_min) * (right - left)
    y = mid - (roi_wave_uv / amp_scale) * ((bottom - top) / 2)
    step = max(1, int(math.ceil(len(x) / 700)))
    points = " ".join(f"{float(px):.1f},{float(py):.1f}" for px, py in zip(x[::step], y[::step]))
    zero_x = left + (0.0 - t_min) / max(1e-9, t_max - t_min) * (right - left)
    pmin, pmax = [float(value) * 1000.0 for value in cfg.get("p300_window_seconds", [0.25, 0.6])]
    p_left = left + (pmin - t_min) / max(1e-9, t_max - t_min) * (right - left)
    p_right = left + (pmax - t_min) / max(1e-9, t_max - t_min) * (right - left)
    p3_x = left + (p3_latency_ms - t_min) / max(1e-9, t_max - t_min) * (right - left)
    p3_y = mid - (p3_amplitude_uv / amp_scale) * ((bottom - top) / 2)
    ticks = []
    for tick in _nice_time_ticks(t_min, t_max):
        tx = left + (tick - t_min) / max(1e-9, t_max - t_min) * (right - left)
        ticks.append(
            f'<line x1="{tx:.1f}" y1="{bottom}" x2="{tx:.1f}" y2="{bottom + 5}" stroke="#777" />'
            f'<text x="{tx:.1f}" y="{bottom + 20}" text-anchor="middle" font-size="10" fill="#555">{tick:.0f}</text>'
        )
    return f"""
      <svg class="wave-svg" viewBox="0 0 {width} {height}" role="img" aria-label="Stimulus-locked ROI waveform">
        <rect x="0" y="0" width="{width}" height="{height}" fill="#fff" />
        <rect x="{p_left:.1f}" y="{top}" width="{max(0.0, p_right - p_left):.1f}" height="{bottom - top}" fill="#f6c453" opacity=".18" />
        <line x1="{left}" y1="{mid:.1f}" x2="{right}" y2="{mid:.1f}" stroke="#c8c8c0" />
        <line x1="{zero_x:.1f}" y1="{top}" x2="{zero_x:.1f}" y2="{bottom}" stroke="#222" stroke-width="1.1" />
        <line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" stroke="#999" />
        <line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" stroke="#999" />
        <polyline points="{points}" fill="none" stroke="#235fba" stroke-width="2.1" />
        <circle cx="{p3_x:.1f}" cy="{p3_y:.1f}" r="5" fill="#c62828" stroke="#fff" stroke-width="1.4" />
        <text x="{p3_x + 8:.1f}" y="{max(top + 12, p3_y - 8):.1f}" font-size="11" fill="#a51d1d">P3 {p3_latency_ms:.0f} ms</text>
        <text x="12" y="18" font-size="11" fill="#555">ROI uV</text>
        <text x="{right}" y="{height - 8}" text-anchor="end" font-size="11" fill="#555">ms from stimulus onset</text>
        {''.join(ticks)}
      </svg>
    """


def _nice_time_ticks(t_min: float, t_max: float) -> list[float]:
    start = int(math.ceil(t_min / 200.0) * 200)
    stop = int(math.floor(t_max / 200.0) * 200)
    ticks = list(range(start, stop + 1, 200))
    if 0 not in ticks and t_min <= 0 <= t_max:
        ticks.append(0)
    return sorted(float(tick) for tick in ticks)


def _stimulus_card_html(trial: dict[str, Any], stimulus: dict[str, Any], left: int) -> str:
    condition = "nogo" if stimulus.get("is_no_go") else "go"
    svg = _stimulus_svg(stimulus)
    label = stimulus.get("label") or ("NO-GO" if stimulus.get("is_no_go") else "GO")
    shape = stimulus.get("shape", "")
    color = stimulus.get("color", "")
    return (
        f'<div class="stim-card {condition}" style="left:{left}px">'
        f"{svg}<strong>{html.escape(str(label))}</strong><br>"
        f'Trial {int(trial["trial"])}<br>{html.escape(str(color))} {html.escape(str(shape))}</div>'
    )


def _stimulus_svg(stimulus: dict[str, Any]) -> str:
    shape = str(stimulus.get("shape", "")).strip().lower()
    color = html.escape(str(stimulus.get("color", "white")).strip() or "white", quote=True)
    if shape == "circle":
        glyph = f'<circle cx="55" cy="50" r="22" fill="{color}" />'
    elif shape == "square":
        glyph = f'<rect x="34" y="29" width="42" height="42" fill="{color}" />'
    elif shape == "triangle":
        glyph = f'<polygon points="55,24 80,72 30,72" fill="{color}" />'
    elif shape == "hexagon":
        glyph = f'<polygon points="55,22 80,36 80,64 55,78 30,64 30,36" fill="{color}" />'
    elif shape == "star":
        glyph = f'<polygon points="{_star_svg_points(55, 52, 28, 12)}" fill="{color}" />'
    elif shape == "x":
        glyph = f'<text x="55" y="68" fill="{color}" font-size="58" font-weight="800" text-anchor="middle">X</text>'
    else:
        glyph = f'<text x="55" y="58" fill="{color}" font-size="22" font-weight="700" text-anchor="middle">{html.escape(shape.upper())}</text>'
    return f'<svg width="110" height="100" viewBox="0 0 110 100" role="img"><rect width="110" height="100" fill="#050505" />{glyph}</svg>'


def _star_svg_points(cx: float, cy: float, outer: float, inner: float) -> str:
    points = []
    for idx in range(10):
        radius = outer if idx % 2 == 0 else inner
        angle = -math.pi / 2 + idx * math.pi / 5
        points.append(f"{cx + math.cos(angle) * radius:.1f},{cy + math.sin(angle) * radius:.1f}")
    return " ".join(points)


def _evoked_values_at_ms(evoked: Any, time_ms: int) -> np.ndarray:
    idx = int(np.argmin(np.abs(evoked.times * 1000.0 - float(time_ms))))
    return evoked.data[:, idx] * 1e6


def _montage_svg(ch_names: list[str], values_uv: np.ndarray, scale_uv: float) -> str:
    coords = _channel_positions(ch_names)
    circles = []
    for name, value in zip(ch_names, values_uv):
        x, y = coords.get(name, (0.5, 0.5))
        color = _value_color(float(value), scale_uv)
        circles.append(
            f'<circle cx="{x * 70 + 10:.1f}" cy="{(1 - y) * 70 + 10:.1f}" r="5.5" fill="{color}" stroke="#222" stroke-width=".7">'
            f"<title>{html.escape(name)} {value:.2f} uV</title></circle>"
        )
    return (
        '<svg width="78" height="86" viewBox="0 0 90 96" role="img">'
        '<circle cx="45" cy="45" r="36" fill="#f8f8f8" stroke="#333" stroke-width="1.2" />'
        '<path d="M39 9 L45 1 L51 9" fill="none" stroke="#333" stroke-width="1.2" />'
        f"{''.join(circles)}</svg>"
    )


def _channel_positions(ch_names: list[str]) -> dict[str, tuple[float, float]]:
    known = {
        "Fp1": (0.34, 0.93), "Fp2": (0.66, 0.93), "F7": (0.12, 0.72), "F3": (0.32, 0.72),
        "Fz": (0.50, 0.76), "F4": (0.68, 0.72), "F8": (0.88, 0.72), "FC5": (0.20, 0.62),
        "FC1": (0.40, 0.62), "FC2": (0.60, 0.62), "FC6": (0.80, 0.62), "T7": (0.08, 0.50),
        "C3": (0.30, 0.50), "Cz": (0.50, 0.50), "C4": (0.70, 0.50), "T8": (0.92, 0.50),
        "CP5": (0.20, 0.38), "CP1": (0.40, 0.38), "CP2": (0.60, 0.38), "CP6": (0.80, 0.38),
        "P7": (0.14, 0.28), "P3": (0.34, 0.28), "Pz": (0.50, 0.24), "P4": (0.66, 0.28),
        "P8": (0.86, 0.28), "POz": (0.50, 0.16), "O1": (0.38, 0.08), "Oz": (0.50, 0.06),
        "O2": (0.62, 0.08), "M1": (0.02, 0.42), "M2": (0.98, 0.42),
    }
    if all(name in known for name in ch_names):
        return {name: known[name] for name in ch_names}
    coords = {}
    count = len(ch_names)
    for idx, name in enumerate(ch_names):
        angle = -math.pi / 2 + (2 * math.pi * idx / max(1, count))
        coords[name] = (0.5 + 0.36 * math.cos(angle), 0.5 - 0.36 * math.sin(angle))
    return coords


def _value_color(value: float, scale: float) -> str:
    norm = max(-1.0, min(1.0, value / max(scale, 1e-9)))
    if norm >= 0:
        return _rgb_to_hex(_interp((255, 255, 255), (198, 40, 40), norm))
    return _rgb_to_hex(_interp((255, 255, 255), (45, 94, 180), abs(norm)))


def _interp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[idx] + (b[idx] - a[idx]) * t)) for idx in range(3))


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _axis_html(min_time: float, max_time: float, px_per_second: int) -> str:
    ticks = []
    start = math.floor(min_time)
    stop = math.ceil(max_time)
    for sec in range(start, stop + 1):
        left = int((sec - min_time) * px_per_second)
        ticks.append(f'<div class="tick" style="left:{left}px"><span>{sec:+d}s</span></div>')
    return "".join(ticks)


def _erp_metrics_html(p300: dict[str, Any], n2: dict[str, Any]) -> str:
    return _component_metrics_html("N2 ROI Metrics", n2, "latency_ms_exploratory") + _component_metrics_html(
        "P300 ROI Metrics", p300, "latency_ms"
    )


def _component_metrics_html(title: str, metrics: dict[str, Any], latency_key: str) -> str:
    rows = []
    for condition in ("all", "go", "no_go", "no_go_minus_go"):
        if condition not in metrics:
            continue
        metric = metrics[condition]
        if not isinstance(metric, dict):
            continue
        latency = metric.get(latency_key)
        rows.append(
            "<tr>"
            f"<th>{html.escape(condition.replace('_', '-'))}</th>"
            f"<td>{float(metric.get('amplitude_uv', 0.0)):.3f} uV</td>"
            f"<td>{'' if latency is None else f'{float(latency):.1f} ms'}</td>"
            "</tr>"
        )
    return (
        f"<h2>{html.escape(title)}</h2>"
        "<table><thead><tr><th>Condition</th><th>Amplitude</th><th>Latency</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _write_summary(path: Path, summary: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return summary


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
