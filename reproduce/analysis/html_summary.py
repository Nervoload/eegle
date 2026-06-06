"""Self-contained post-session HTML report with alpha and EEG replay views."""

from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path
from typing import Any

from reproduce.analysis.alpha import run_alpha_validation


def generate_experiment_html_report(
    session_dir: str | Path,
    config: dict[str, Any] | None = None,
    output: str | Path | None = None,
    *,
    max_raw_points: int = 120000,
    max_alpha_points: int = 12000,
) -> dict[str, Any]:
    """Generate a self-contained HTML replay report for a completed session."""
    root = Path(session_dir).expanduser().resolve()
    parameters = _load_json(root / "parameters.json") or {}
    effective_config = config or parameters
    alpha_config = dict(effective_config.get("analysis", {}).get("alpha", {}))
    alpha_summary = run_alpha_validation(root, alpha_config)
    report_data = _build_report_data(root, alpha_summary, max_raw_points, max_alpha_points)
    target = Path(output).expanduser().resolve() if output else root / "reports" / "experiment_summary.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_html(report_data), encoding="utf-8")
    staged_alpha_count = int(report_data["staged_features"].get("alpha_packet_count", 0) or 0)
    return {
        "status": "ok",
        "html_file": str(target),
        "session_dir": str(root),
        "raw_status": report_data["raw"].get("status"),
        "raw_sample_count": report_data["raw"].get("sample_count", 0),
        "marker_count": len(report_data["markers"]),
        "offline_alpha_status": report_data["offline_alpha"].get("status"),
        "live_alpha_count": staged_alpha_count if staged_alpha_count else len(report_data["live_alpha"]),
        "live_alpha_source": "staged_event_features" if staged_alpha_count else "legacy_continuous_alpha",
        "staged_alpha_packet_count": staged_alpha_count,
        "legacy_continuous_alpha_count": len(report_data["live_alpha"]),
        "display_timebase": report_data.get("display_timebase"),
        "timebase_diagnostics": report_data.get("timebase_diagnostics"),
    }


def _build_report_data(root: Path, alpha_summary: dict[str, Any], max_raw_points: int, max_alpha_points: int) -> dict[str, Any]:
    raw = _load_raw_preview(root / "raw" / "eeg.csv", max_raw_points)
    raw_lsl_start = raw.get("lsl_start")
    raw_monotonic_start = raw.get("monotonic_start")
    display_timebase, timebase_diagnostics = _choose_display_timebase(root, raw)
    _apply_raw_display_timebase(raw, display_timebase)
    calibration = _load_json(root / "calibration" / "alpha_calibration.json") or {}
    markers = _load_markers(root, raw_lsl_start, raw_monotonic_start, display_timebase)
    segments = _build_segments(root, raw, raw_lsl_start, raw_monotonic_start, display_timebase)
    offline_alpha = _load_offline_alpha(
        root / "reports" / "alpha" / "offline_alpha_timeseries.csv",
        raw_lsl_start,
        raw_monotonic_start,
        max_alpha_points,
        display_timebase,
    )
    live_alpha = _load_live_alpha(root / "realtime" / "alpha_power.jsonl", raw_lsl_start, raw_monotonic_start, max_alpha_points, display_timebase)
    staged_features = _load_staged_features(root / "realtime" / "event_features.jsonl", raw_lsl_start, max_alpha_points)
    marker_categories = sorted({marker["category"] for marker in markers})
    replay_summary = _load_json(root / "reports" / "realtime_features" / "replay_summary.json") or {"status": "missing"}
    behavior_features = _load_json(root / "reports" / "realtime_features" / "behavior_feature_summary.json") or {"status": "missing"}
    engine_metadata = _load_json(root / "realtime" / "engine_metadata.json") or {"status": "missing"}
    return {
        "schema_version": 1,
        "session": {
            "session_dir": str(root),
            "summary": _report_session_summary(_load_json(root / "session_summary.json") or {}),
            "analysis_summary": _report_analysis_summary(_load_json(root / "reports" / "summary.json") or {}),
            "alpha_summary": alpha_summary,
            "calibration_status": calibration.get("status"),
            "online_band": calibration.get("online_band"),
        },
        "raw": raw,
        "display_timebase": display_timebase,
        "timebase_diagnostics": timebase_diagnostics,
        "segments": segments,
        "markers": markers,
        "marker_categories": marker_categories,
        "offline_alpha": offline_alpha,
        "live_alpha": live_alpha,
        "staged_features": staged_features,
        "realtime_features": {
            "replay": replay_summary,
            "behavior": behavior_features,
            "engine": engine_metadata,
        },
        "files": {
            "raw_eeg": str(root / "raw" / "eeg.csv"),
            "events_jsonl": str(root / "events" / "events.jsonl"),
            "stimulus_manifest": str(root / "events" / "stimulus_manifest.json"),
            "realtime_markers": str(root / "realtime" / "markers.jsonl"),
            "live_alpha": str(root / "realtime" / "alpha_power.jsonl"),
            "staged_features": str(root / "realtime" / "event_features.jsonl"),
            "offline_alpha": str(root / "reports" / "alpha" / "offline_alpha_timeseries.csv"),
        },
    }


def _report_session_summary(summary: dict[str, Any]) -> dict[str, Any]:
    task = summary.get("task") if isinstance(summary.get("task"), dict) else {}
    eeg = summary.get("eeg") if isinstance(summary.get("eeg"), dict) else {}
    return {
        "status": summary.get("status"),
        "session_dir": summary.get("session_dir"),
        "task": task,
        "eeg": {
            "status": eeg.get("status"),
            "sample_count": eeg.get("sample_count"),
            "duration_seconds": eeg.get("duration_seconds"),
            "stream": eeg.get("stream"),
        },
    }


def _report_analysis_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis_status": summary.get("analysis_status"),
        "updated_at": summary.get("updated_at"),
        "alpha": summary.get("alpha"),
        "erp": summary.get("erp"),
    }


def _load_raw_preview(path: Path, max_points: int) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "file": str(path), "channels": [], "times": [], "samples": []}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, [])
        rows = [[float(value) for value in row] for row in reader if row]
    channel_names = [str(value) for value in header[2:]]
    if not rows:
        return {"status": "empty", "file": str(path), "channels": channel_names, "times": [], "samples": []}
    stride = max(1, math.ceil(len(rows) / max(1, int(max_points))))
    sampled = rows[::stride]
    lsl_start = rows[0][0]
    monotonic_start = rows[0][1] if len(rows[0]) > 1 else rows[0][0]
    times = []
    samples = []
    for row in sampled:
        lsl = row[0]
        monotonic = row[1] if len(row) > 1 else row[0]
        times.append(
            {
                "lsl": _round(lsl - lsl_start),
                "monotonic": _round(monotonic - monotonic_start),
            }
        )
        samples.append([_round(value) for value in row[2:]])
    return {
        "status": "ok",
        "file": str(path),
        "sample_count": len(rows),
        "display_sample_count": len(sampled),
        "decimation_stride": stride,
        "channels": channel_names,
        "times": times,
        "samples": samples,
        "lsl_start": lsl_start,
        "monotonic_start": monotonic_start,
        "lsl_duration_seconds": _round(rows[-1][0] - lsl_start),
        "monotonic_duration_seconds": _round((rows[-1][1] if len(rows[-1]) > 1 else rows[-1][0]) - monotonic_start),
        "duration_seconds": _round(rows[-1][0] - lsl_start),
    }


def _load_markers(root: Path, raw_lsl_start: float | None, raw_monotonic_start: float | None, display_timebase: str) -> list[dict[str, Any]]:
    markers: list[dict[str, Any]] = []
    for row in _load_jsonl(root / "calibration" / "events.jsonl"):
        event_name = str(row.get("event", "calibration_event"))
        phase = str(row.get("phase", "") or "calibration")
        metadata = dict(row.get("metadata") or {})
        trial = metadata.get("trial")
        category = _marker_category(event_name)
        markers.append(
            _marker_payload(
                label=f"{phase} {event_name}",
                source="calibration_events",
                lsl_timestamp=_optional_float(row.get("lsl_timestamp")),
                monotonic_timestamp=_optional_float(row.get("timestamp_monotonic")),
                trial=trial,
                raw_lsl_start=raw_lsl_start,
                raw_monotonic_start=raw_monotonic_start,
                display_timebase=display_timebase,
                category="calibration" if category == "marker" else category,
                phase=phase,
            )
        )
    manifest = _load_json(root / "events" / "stimulus_manifest.json") or {}
    manifest_has_trials = bool(manifest.get("trials"))
    for row in _load_jsonl(root / "events" / "events.jsonl"):
        label = str(row.get("label", ""))
        event_trial = _optional_int(row.get("trial"))
        if manifest_has_trials and _manifest_covers_task_marker(label) and (event_trial is None or event_trial >= 1):
            continue
        metadata = dict(row.get("metadata") or {})
        markers.append(
            _marker_payload(
                label=label,
                source="task_events",
                lsl_timestamp=_optional_float(metadata.get("lsl_timestamp")),
                monotonic_timestamp=_optional_float(row.get("timestamp")),
                trial=event_trial,
                raw_lsl_start=raw_lsl_start,
                raw_monotonic_start=raw_monotonic_start,
                display_timebase=display_timebase,
                phase="task_practice" if event_trial is not None and event_trial < 1 else "task",
            )
        )
    for trial in manifest.get("trials", []):
        trial_index = trial.get("trial")
        stimulus = dict(trial.get("stimulus") or {})
        label_suffix = f"{trial_index} {'NO-GO' if stimulus.get('is_no_go') else 'GO'}"
        markers.append(
            _marker_payload(
                label=f"{'NO-GO' if stimulus.get('is_no_go') else 'stimulus_on'} {label_suffix}",
                category="no_go" if stimulus.get("is_no_go") else "stimulus_on",
                source="stimulus_manifest",
                lsl_timestamp=_optional_float(trial.get("onset_lsl_timestamp")),
                monotonic_timestamp=_optional_float(trial.get("onset_monotonic")),
                trial=trial_index,
                raw_lsl_start=raw_lsl_start,
                raw_monotonic_start=raw_monotonic_start,
                display_timebase=display_timebase,
                phase="task",
            )
        )
        markers.append(
            _marker_payload(
                label=f"stimulus_off {label_suffix}",
                category="stimulus_off",
                source="stimulus_manifest",
                lsl_timestamp=_optional_float(trial.get("offset_lsl_timestamp")),
                monotonic_timestamp=_optional_float(trial.get("offset_monotonic")),
                trial=trial_index,
                raw_lsl_start=raw_lsl_start,
                raw_monotonic_start=raw_monotonic_start,
                display_timebase=display_timebase,
                phase="task",
            )
        )
        onset_lsl = _optional_float(trial.get("onset_lsl_timestamp"))
        onset_monotonic = _optional_float(trial.get("onset_monotonic"))
        for press in dict(trial.get("response") or {}).get("presses", []):
            rt = _optional_float(press.get("rt_seconds"))
            markers.append(
                _marker_payload(
                    label=f"button_press {press.get('key', '')} trial {trial_index}",
                    category="button_press",
                    source="stimulus_manifest",
                    lsl_timestamp=None if onset_lsl is None or rt is None else onset_lsl + rt,
                    monotonic_timestamp=_optional_float(press.get("timestamp_monotonic")) or (None if onset_monotonic is None or rt is None else onset_monotonic + rt),
                    trial=trial_index,
                    raw_lsl_start=raw_lsl_start,
                    raw_monotonic_start=raw_monotonic_start,
                    display_timebase=display_timebase,
                    phase="task",
                )
            )
    for row in _load_jsonl(root / "realtime" / "markers.jsonl"):
        realtime_trial = _trial_from_marker_label(str(row.get("label", "")))
        markers.append(
            _marker_payload(
                label=str(row.get("label", "")),
                source="realtime_markers",
                lsl_timestamp=_optional_float(row.get("lsl_timestamp")),
                monotonic_timestamp=None,
                trial=realtime_trial,
                raw_lsl_start=raw_lsl_start,
                raw_monotonic_start=raw_monotonic_start,
                display_timebase=display_timebase,
                phase="task_practice" if realtime_trial is not None and realtime_trial < 1 else "realtime",
            )
        )
    return [
        marker
        for marker in sorted(markers, key=lambda item: item["time"] if item.get("time") is not None else float("inf"))
        if marker.get("time") is not None
    ]


def _manifest_covers_task_marker(label: str) -> bool:
    value = label.lower()
    return value.startswith("go_nogo_stimulus_onset") or value.startswith("go_nogo_stimulus_offset") or value == "button_press"


def _trial_from_marker_label(label: str) -> int | None:
    match = re.search(r"go_nogo_stimulus_(?:onset|offset)_(-?\d+)(?:_|$)", label.lower())
    return None if match is None else int(match.group(1))


def _marker_payload(
    *,
    label: str,
    source: str,
    raw_lsl_start: float | None,
    raw_monotonic_start: float | None,
    lsl_timestamp: float | None = None,
    monotonic_timestamp: float | None = None,
    trial: Any = None,
    category: str | None = None,
    display_timebase: str = "lsl",
    phase: str | None = None,
) -> dict[str, Any]:
    lsl_time = None if lsl_timestamp is None or raw_lsl_start is None else lsl_timestamp - raw_lsl_start
    monotonic_time = None if monotonic_timestamp is None or raw_monotonic_start is None else monotonic_timestamp - raw_monotonic_start
    time, timebase = _select_relative_time(lsl_time, monotonic_time, display_timebase)
    return {
        "label": label,
        "category": category or _marker_category(label),
        "source": source,
        "time": None if time is None else _round(time),
        "timebase": timebase,
        "lsl_time": None if lsl_time is None else _round(lsl_time),
        "monotonic_time": None if monotonic_time is None else _round(monotonic_time),
        "trial": trial,
        "phase": phase,
        "lsl_timestamp": lsl_timestamp,
        "monotonic_timestamp": monotonic_timestamp,
    }


def _build_segments(
    root: Path,
    raw: dict[str, Any],
    raw_lsl_start: float | None,
    raw_monotonic_start: float | None,
    display_timebase: str,
) -> list[dict[str, Any]]:
    duration = _optional_float(raw.get("duration_seconds")) or 0.0
    segments: list[dict[str, Any]] = [
        _segment_payload("full", "Full Time Series", 0.0, duration, "All recorded raw EEG, calibration, task, and post-task data.")
    ]
    phase_labels = {
        "eyes_open_fixation": "Eyes-open Fixation",
        "eyes_closed_rest": "Eyes-closed Rest",
        "go_nogo_practice": "Calibration Go/No-go Practice",
    }
    calibration_events = _load_jsonl(root / "calibration" / "events.jsonl")
    for phase, label in phase_labels.items():
        start = _first_event_time(calibration_events, phase, "recording_start", raw_lsl_start, raw_monotonic_start, display_timebase)
        end = _first_event_time(calibration_events, phase, "recording_end", raw_lsl_start, raw_monotonic_start, display_timebase)
        if start is not None and end is not None and end > start:
            segments.append(_segment_payload(phase, label, start, end, "Calibration recording phase."))
    practice_segment = _task_practice_segment(root, raw_lsl_start, raw_monotonic_start, display_timebase)
    if practice_segment is not None:
        segments.append(practice_segment)
    task_segment = _task_segment(root, raw_lsl_start, raw_monotonic_start, display_timebase)
    if task_segment is not None:
        segments.append(task_segment)
    return [_clamp_segment(segment, duration) for segment in segments if segment["end"] > segment["start"]]


def _task_practice_segment(
    root: Path,
    raw_lsl_start: float | None,
    raw_monotonic_start: float | None,
    display_timebase: str,
) -> dict[str, Any] | None:
    times: list[float] = []
    for row in _load_jsonl(root / "events" / "events.jsonl"):
        trial = _optional_int(row.get("trial"))
        label = str(row.get("label", ""))
        if trial is None or trial >= 1 or not _manifest_covers_task_marker(label):
            continue
        metadata = dict(row.get("metadata") or {})
        time = _relative_timestamp(
            _optional_float(metadata.get("lsl_timestamp")),
            _optional_float(row.get("timestamp")),
            raw_lsl_start,
            raw_monotonic_start,
            display_timebase,
        )
        if time is not None:
            times.append(time)
    if not times:
        return None
    return _segment_payload(
        "task_practice",
        "Task Practice",
        max(0.0, min(times) - 0.5),
        max(times) + 0.5,
        "Pre-task Go/No-go practice trials only.",
    )


def _task_segment(root: Path, raw_lsl_start: float | None, raw_monotonic_start: float | None, display_timebase: str) -> dict[str, Any] | None:
    manifest = _load_json(root / "events" / "stimulus_manifest.json") or {}
    times: list[float] = []
    for trial in manifest.get("trials", []):
        trial_index = trial.get("trial")
        if isinstance(trial_index, (int, float)) and trial_index < 1:
            continue
        for lsl_field, monotonic_field in (("onset_lsl_timestamp", "onset_monotonic"), ("offset_lsl_timestamp", "offset_monotonic")):
            time = _relative_timestamp(
                _optional_float(trial.get(lsl_field)),
                _optional_float(trial.get(monotonic_field)),
                raw_lsl_start,
                raw_monotonic_start,
                display_timebase,
            )
            if time is not None:
                times.append(time)
    if times:
        start = max(0.0, min(times) - 0.5)
        end = max(times) + 0.5
        return _segment_payload("task", "Full Task", start, end, "Main Go/No-go task trials only.")
    task_events = _load_jsonl(root / "events" / "events.jsonl")
    start = _first_task_label_time(task_events, "task_start", raw_lsl_start, raw_monotonic_start, display_timebase)
    end = _first_task_label_time(task_events, "task_end", raw_lsl_start, raw_monotonic_start, display_timebase)
    if start is None or end is None or end <= start:
        return None
    return _segment_payload("task", "Full Task", start, end, "Main Go/No-go task period.")


def _segment_payload(key: str, label: str, start: float, end: float, description: str) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "start": _round(start),
        "end": _round(end),
        "duration": _round(end - start),
        "description": description,
    }


def _clamp_segment(segment: dict[str, Any], duration: float) -> dict[str, Any]:
    start = max(0.0, _optional_float(segment.get("start")) or 0.0)
    end = min(max(start, duration), _optional_float(segment.get("end")) or start)
    updated = dict(segment)
    updated["start"] = _round(start)
    updated["end"] = _round(end)
    updated["duration"] = _round(end - start)
    return updated


def _first_event_time(
    events: list[dict[str, Any]],
    phase: str,
    event_name: str,
    raw_lsl_start: float | None,
    raw_monotonic_start: float | None,
    display_timebase: str,
) -> float | None:
    for row in events:
        if row.get("phase") != phase or row.get("event") != event_name:
            continue
        return _relative_timestamp(
            _optional_float(row.get("lsl_timestamp")),
            _optional_float(row.get("timestamp_monotonic")),
            raw_lsl_start,
            raw_monotonic_start,
            display_timebase,
        )
    return None


def _first_task_label_time(
    events: list[dict[str, Any]],
    label: str,
    raw_lsl_start: float | None,
    raw_monotonic_start: float | None,
    display_timebase: str,
) -> float | None:
    for row in events:
        if row.get("label") != label:
            continue
        metadata = dict(row.get("metadata") or {})
        return _relative_timestamp(
            _optional_float(metadata.get("lsl_timestamp")),
            _optional_float(row.get("timestamp")),
            raw_lsl_start,
            raw_monotonic_start,
            display_timebase,
        )
    return None


def _relative_timestamp(
    lsl_timestamp: float | None,
    monotonic_timestamp: float | None,
    raw_lsl_start: float | None,
    raw_monotonic_start: float | None,
    display_timebase: str,
) -> float | None:
    lsl_time = None if lsl_timestamp is None or raw_lsl_start is None else lsl_timestamp - raw_lsl_start
    monotonic_time = None if monotonic_timestamp is None or raw_monotonic_start is None else monotonic_timestamp - raw_monotonic_start
    time, _timebase = _select_relative_time(lsl_time, monotonic_time, display_timebase)
    return None if time is None else float(time)


def _load_offline_alpha(
    path: Path,
    raw_lsl_start: float | None,
    raw_monotonic_start: float | None,
    max_points: int,
    display_timebase: str,
) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "file": str(path), "times": [], "posterior": [], "channels": []}
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        return {"status": "empty", "file": str(path), "times": [], "posterior": [], "channels": []}
    stride = max(1, math.ceil(len(rows) / max(1, int(max_points))))
    sampled = rows[::stride]
    channel_fields = [field for field in sampled[0].keys() if field.endswith("_alpha_power") and field != "posterior_alpha_power"]
    channels = [
        {
            "name": field.removesuffix("_alpha_power"),
            "values": [_round(_optional_float(row.get(field)) or 0.0) for row in sampled],
        }
        for field in channel_fields
    ]
    times = []
    posterior = []
    for row in sampled:
        lsl_mid = _optional_float(row.get("window_mid_lsl_timestamp"))
        mono_mid = _optional_float(row.get("window_mid_monotonic"))
        lsl_time = None if lsl_mid is None or raw_lsl_start is None else lsl_mid - raw_lsl_start
        monotonic_time = None if mono_mid is None or raw_monotonic_start is None else mono_mid - raw_monotonic_start
        time, _timebase = _select_relative_time(lsl_time, monotonic_time, display_timebase)
        times.append(None if time is None else _round(time))
        posterior.append(_round(_optional_float(row.get("posterior_alpha_power")) or 0.0))
    return {
        "status": "ok",
        "file": str(path),
        "window_count": len(rows),
        "display_window_count": len(sampled),
        "times": times,
        "posterior": posterior,
        "channels": channels,
        "timebase": display_timebase,
    }


def _load_live_alpha(
    path: Path,
    raw_lsl_start: float | None,
    raw_monotonic_start: float | None,
    max_points: int,
    display_timebase: str,
) -> list[dict[str, Any]]:
    rows = _load_jsonl(path)
    if not rows:
        return []
    stride = max(1, math.ceil(len(rows) / max(1, int(max_points))))
    payload = []
    for row in rows[::stride]:
        start = _optional_float(row.get("window_start_lsl_timestamp"))
        end = _optional_float(row.get("window_end_lsl_timestamp"))
        created = _optional_float(row.get("created_at_monotonic"))
        lsl_time = None if start is None or end is None or raw_lsl_start is None else ((start + end) / 2.0) - raw_lsl_start
        monotonic_time = None if created is None or raw_monotonic_start is None else created - raw_monotonic_start
        time, timebase = _select_relative_time(lsl_time, monotonic_time, display_timebase)
        payload.append(
            {
                "time": None if time is None else _round(time),
                "timebase": timebase,
                "alpha_power": _optional_float(row.get("alpha_power")) or 0.0,
                "alpha_power_z": _optional_float(row.get("alpha_power_z")),
                "valid": bool(row.get("valid", True)),
            }
        )
    return [row for row in payload if row["time"] is not None]


def _load_staged_features(path: Path, raw_lsl_start: float | None, max_points: int) -> dict[str, Any]:
    rows = _load_jsonl(path)
    if not rows:
        return {"status": "missing", "file": str(path), "packet_count": 0, "points": []}
    stride = max(1, math.ceil(len(rows) / max(1, int(max_points))))
    points: list[dict[str, Any]] = []
    alpha_rows = [row for row in rows if str(row.get("stage")) in {"prestim_state", "alpha_erd"}]
    main_alpha_rows = [row for row in alpha_rows if (_optional_int(row.get("trial")) or 0) >= 1]
    practice_alpha_rows = [row for row in alpha_rows if (_optional_int(row.get("trial")) or 0) < 0]
    alpha_packets = len(main_alpha_rows)
    valid_alpha_packets = sum(bool(row.get("valid", False)) for row in main_alpha_rows)
    for row in rows[::stride]:
        deadline = _optional_float(row.get("stage_deadline_lsl"))
        if deadline is None or raw_lsl_start is None:
            continue
        features = dict(row.get("features") or {})
        stage = str(row.get("stage", "unknown"))
        points.append(
            {
                "time": _round(deadline - raw_lsl_start),
                "trial": _optional_int(row.get("trial")),
                "stage": stage,
                "valid": bool(row.get("valid", False)),
                "readiness_alpha_power": _optional_float(features.get("readiness_alpha_power")),
                "early_theta_power": _optional_float(features.get("early_theta_power")),
                "n2_mean_uv": _optional_float(features.get("n2_mean_uv")),
                "p3_mean_uv": _optional_float(features.get("p3_mean_uv")),
                "alpha_erd_percent": _optional_float(features.get("alpha_erd_percent")),
            }
        )
    main_rows = [row for row in rows if (_optional_int(row.get("trial")) or 0) >= 1]
    practice_rows = [row for row in rows if (_optional_int(row.get("trial")) or 0) < 0]
    return {
        "status": "ok",
        "file": str(path),
        "packet_count": len(rows),
        "display_packet_count": len(points),
        "main_task_packet_count": len(main_rows),
        "practice_packet_count": len(practice_rows),
        "alpha_packet_count": alpha_packets,
        "valid_alpha_packet_count": valid_alpha_packets,
        "all_alpha_packet_count": len(alpha_rows),
        "practice_alpha_packet_count": len(practice_alpha_rows),
        "points": points,
    }


def _marker_category(label: str) -> str:
    value = label.lower()
    if ("stimulus_onset" in value or value.startswith("no-go")) and ("no-go" in value or "no_go" in value or "nogo" in value):
        return "no_go"
    if "stimulus_onset" in value or value.startswith("stimulus_on"):
        return "stimulus_on"
    if "stimulus_offset" in value or value.startswith("stimulus_off"):
        return "stimulus_off"
    if "button_press" in value or "response" in value:
        return "button_press"
    if "calibration" in value:
        return "calibration"
    if "feedback" in value:
        return "feedback"
    if "countdown" in value or "task_start" in value or "task_end" in value or "abort" in value:
        return "system"
    return "marker"


def _render_html(data: dict[str, Any]) -> str:
    data_json = json.dumps(data, sort_keys=True, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Closedloop EEG Experiment Summary</title>
<style>
:root {{
  --ink: #18202a;
  --muted: #617083;
  --line: #d7dde4;
  --panel: #f7f9fb;
  --accent: #0f766e;
  --accent-2: #7c3aed;
  --warn: #b45309;
  --event: #dc2626;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; color: var(--ink); background: #ffffff; }}
header {{ padding: 18px 22px; border-bottom: 1px solid var(--line); background: #fbfcfd; }}
h1 {{ margin: 0 0 4px; font-size: 22px; font-weight: 650; letter-spacing: 0; }}
h2 {{ margin: 0 0 12px; font-size: 15px; font-weight: 650; letter-spacing: 0; }}
main {{ padding: 18px 22px 28px; display: grid; gap: 16px; }}
.meta {{ color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }}
.summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; }}
.metric {{ border: 1px solid var(--line); border-radius: 6px; padding: 10px 12px; background: var(--panel); }}
.metric b {{ display: block; font-size: 18px; margin-bottom: 2px; }}
.metric span {{ color: var(--muted); font-size: 12px; }}
.panel {{ border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fff; }}
.toolbar {{ display: flex; flex-wrap: wrap; gap: 8px 12px; align-items: center; margin-bottom: 10px; }}
button, select, input[type="number"], input[type="range"] {{
  border: 1px solid var(--line);
  border-radius: 6px;
  background: #ffffff;
  color: var(--ink);
  min-height: 32px;
  padding: 5px 9px;
  font: inherit;
}}
button.primary {{ background: var(--accent); border-color: var(--accent); color: white; }}
button:active {{ transform: translateY(1px); }}
label {{ display: inline-flex; align-items: center; gap: 5px; color: var(--muted); font-size: 12px; }}
.canvas-wrap {{ width: 100%; min-height: 420px; border: 1px solid var(--line); border-radius: 6px; overflow: hidden; background: #fff; }}
.canvas-wrap.alpha-replay {{ min-height: 220px; }}
#replayCanvas {{ width: 100%; height: 260px; display: block; }}
#rawStackPanel {{ display: grid; gap: 8px; margin-top: 10px; }}
#rawStackPanel.hidden {{ display: none; }}
.channel-replay {{ border: 1px solid var(--line); border-radius: 6px; background: #fff; overflow: hidden; }}
.channel-replay canvas {{ width: 100%; height: 104px; display: block; }}
#alphaPanel {{ display: none; }}
#alphaPanel.open {{ display: block; }}
.resizable {{ resize: both; overflow: auto; min-width: 520px; min-height: 300px; width: 100%; height: 430px; border: 1px solid var(--line); border-radius: 6px; background: #fff; }}
#alphaCanvas {{ width: 100%; height: 100%; display: block; }}
#featureCanvas {{ width: 100%; height: 420px; display: block; }}
.checks {{ display: flex; flex-wrap: wrap; gap: 8px 12px; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 8px 14px; align-items: center; margin: 8px 0 10px; color: var(--muted); font-size: 12px; }}
.legend-item {{ display: inline-flex; align-items: center; gap: 5px; }}
.legend-swatch {{ width: 18px; height: 3px; border-radius: 2px; display: inline-block; }}
.legend-band {{ width: 18px; height: 12px; border-radius: 2px; display: inline-block; border: 1px solid rgba(0,0,0,0.12); }}
.status {{ color: var(--muted); font-size: 12px; }}
.facts {{ margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, monospace; color: var(--ink); }}
@media (max-width: 720px) {{
  main {{ padding: 12px; }}
  header {{ padding: 14px 12px; }}
  .canvas-wrap {{ min-height: 360px; }}
  #replayCanvas {{ height: 430px; }}
  .resizable {{ min-width: 280px; }}
}}
</style>
</head>
<body>
<header>
  <h1>Closedloop EEG Experiment Summary</h1>
  <div class="meta" id="sessionPath"></div>
</header>
<main>
  <section class="summary-grid" id="metrics"></section>

  <section class="panel">
    <h2>Realtime Feature Replay</h2>
    <pre class="facts" id="replayFacts"></pre>
  </section>

  <section class="panel">
    <h2>Exploratory Feature-Behavior Analysis</h2>
    <pre class="facts" id="behaviorFacts"></pre>
  </section>

  <section class="panel">
    <div class="toolbar">
      <h2 style="margin:0">Staged Realtime Features</h2>
      <label>View
        <select class="segmentControl" id="featureSegment"></select>
      </label>
      <span class="status" id="featureStatus"></span>
    </div>
    <div class="canvas-wrap">
      <canvas id="featureCanvas"></canvas>
    </div>
  </section>

  <section class="panel">
    <div class="toolbar">
      <label>View
        <select class="segmentControl" id="alphaSegment"></select>
      </label>
      <button id="toggleAlpha" class="primary">Show Alpha Channels</button>
      <button id="alphaReset">Reset Alpha Zoom</button>
      <button id="alphaZoomIn">Zoom In</button>
      <button id="alphaZoomOut">Zoom Out</button>
      <label><input id="showAlphaGraphMarkers" type="checkbox" checked> Markers</label>
      <span class="status" id="alphaStatus"></span>
    </div>
    <div id="alphaPanel">
      <div class="resizable">
        <canvas id="alphaCanvas"></canvas>
      </div>
    </div>
  </section>

  <section class="panel">
    <h2>Replay</h2>
    <div class="toolbar">
      <label>View
        <select class="segmentControl" id="replaySegment"></select>
      </label>
      <button id="playPause" class="primary">Play</button>
      <label>Speed
        <select id="speed">
          <option value="0.25">0.25x</option>
          <option value="0.5">0.5x</option>
          <option value="1" selected>1x</option>
          <option value="2">2x</option>
          <option value="4">4x</option>
        </select>
      </label>
      <label>Window s <input id="windowSeconds" type="number" value="10" min="2" max="60" step="1"></label>
      <label><input id="showRawStack" type="checkbox" checked> Raw EEG stack</label>
      <label>uV scale <input id="uvScale" type="range" min="10" max="250" value="80"></label>
      <span class="status" id="timeReadout"></span>
    </div>
    <input id="timeSlider" type="range" min="0" max="1" value="0" step="0.001" style="width:100%; margin-bottom: 10px;">
    <div class="checks" id="markerToggles"></div>
    <div class="legend" id="markerLegend"></div>
    <div class="canvas-wrap alpha-replay">
      <canvas id="replayCanvas"></canvas>
    </div>
    <div id="rawStackPanel"></div>
  </section>
</main>
<script id="report-data" type="application/json">{data_json}</script>
<script>
const DATA = JSON.parse(document.getElementById('report-data').textContent);
const raw = DATA.raw || {{}};
const offlineAlpha = DATA.offline_alpha || {{}};
const liveAlpha = DATA.live_alpha || [];
const stagedFeatures = DATA.staged_features || {{status:'missing', points:[]}};
const markers = DATA.markers || [];
const categories = DATA.marker_categories || [];
const segments = (DATA.segments && DATA.segments.length ? DATA.segments : [{{key:'full', label:'Full Time Series', start:0, end:Math.max(1, raw.duration_seconds || 1), duration:Math.max(1, raw.duration_seconds || 1)}}])
  .filter(segment => Number.isFinite(segment.start) && Number.isFinite(segment.end) && segment.end > segment.start);
const colors = ['#0f766e','#7c3aed','#b45309','#2563eb','#db2777','#16a34a','#dc2626','#475569'];
const segmentByKey = Object.fromEntries(segments.map(segment => [segment.key, segment]));
const defaultSegmentKey = segmentByKey.task ? 'task' : 'full';
const featureDefinitions = [
  ['readiness_alpha_power', 'readiness alpha power', '#0f766e'],
  ['early_theta_power', 'early theta power', '#b45309'],
  ['n2_mean_uv', 'N2 mean uV', '#2563eb'],
  ['p3_mean_uv', 'P3 mean uV', '#dc2626'],
  ['alpha_erd_percent', 'alpha ERD percent', '#7c3aed']
];

const state = {{
  playing: false,
  segmentKey: defaultSegmentKey,
  time: segmentByKey[defaultSegmentKey]?.start || 0,
  lastFrame: null,
  alphaStart: segmentByKey[defaultSegmentKey]?.start || 0,
  alphaEnd: segmentByKey[defaultSegmentKey]?.end || Math.max(1, raw.duration_seconds || 1),
  alphaDragging: false,
  alphaDragX: 0,
  showAlphaGraphMarkers: true,
  enabledCategories: new Set(categories)
}};

function finiteValues(values) {{ return values.filter(v => Number.isFinite(v)); }}
function maxDuration() {{
  const rawDuration = raw.duration_seconds || 0;
  const alphaTimes = finiteValues(offlineAlpha.times || []);
  const markerTimes = markers.map(m => m.time).filter(t => Number.isFinite(t));
  return Math.max(rawDuration, alphaTimes.at(-1) || 0, markerTimes.at(-1) || 0, 1);
}}
const duration = maxDuration();

function setup() {{
  document.getElementById('sessionPath').textContent = DATA.session.session_dir || '';
  renderMetrics();
  renderFeatureFacts();
  setupSegments();
  setupMarkers();
  setupControls();
  syncSegmentControls();
  updateSliderBounds();
  drawReplay();
  drawAlphaGraph();
  drawFeatureGraph();
  requestAnimationFrame(tick);
}}

function renderFeatureFacts() {{
  const features = DATA.realtime_features || {{}};
  const replay = features.replay || {{status:'missing'}};
  const engine = features.engine || {{}};
  const behavior = features.behavior || {{status:'missing'}};
  document.getElementById('replayFacts').textContent = JSON.stringify({{
    status: replay.status,
    quality_status: replay.quality_status,
    reasons: replay.reasons,
    online_packet_count: replay.online_packet_count,
    replay_packet_count: replay.replay_packet_count,
    material_difference_count: replay.material_difference_count,
    stage_packet_counts: replay.stage_packet_counts,
    valid_stage_packet_counts: replay.valid_stage_packet_counts,
    reference_contamination_packets: replay.reference_contamination_packets,
    filter_warmup_invalid_packets: replay.filter_warmup_invalid_packets,
    roi_resolution: engine.roi_resolution,
    missing_declared_channels: engine.missing_declared_channels,
    fixed_reference_channels: engine.fixed_reference_channels,
    filter_profiles: engine.filter_profiles
    ,acceptance_scope: replay.acceptance_scope
    ,acceptance_online_packet_count: replay.acceptance_online_packet_count
    ,excluded_practice_online_packet_count: replay.excluded_practice_online_packet_count
    ,excluded_practice_replay_packet_count: replay.excluded_practice_replay_packet_count
  }}, null, 2);
  document.getElementById('behaviorFacts').textContent = JSON.stringify(behavior, null, 2);
}}

function renderMetrics() {{
  const band = DATA.session.online_band || {{}};
  const summary = DATA.session.summary || {{}};
  const task = summary.task || {{}};
  const taskSummary = task.summary || {{}};
  const liveQuality = DATA.session.alpha_summary?.live_alpha_quality || {{}};
  const stagedQuality = DATA.session.alpha_summary?.staged_alpha_quality || liveQuality || {{}};
  const stagedAlphaCount = stagedFeatures.alpha_packet_count || stagedQuality.estimate_count || 0;
  const continuousAlphaLabel = liveAlpha.length ? liveAlpha.length : 'disabled';
  const items = [
    [DATA.session.calibration_status || 'unknown', 'calibration'],
    [band.low_hz != null ? `${{Number(band.low_hz).toFixed(4)}}-${{Number(band.high_hz).toFixed(4)}} Hz` : 'missing', 'alpha band'],
    [DATA.display_timebase || raw.display_timebase || 'unknown', 'display timebase'],
    [raw.status === 'ok' ? raw.sample_count : raw.status || 'missing', 'raw EEG samples'],
    [markers.length, 'event markers'],
    [offlineAlpha.status || 'missing', 'offline alpha'],
    [stagedAlphaCount, 'staged alpha packets'],
    [stagedQuality.status || 'unknown', 'staged alpha quality'],
    [continuousAlphaLabel, 'legacy continuous alpha'],
    [DATA.realtime_features?.replay?.status || 'missing', 'causal replay'],
    [DATA.realtime_features?.replay?.quality_status || 'missing', 'feature quality'],
    [DATA.realtime_features?.behavior?.status || 'missing', 'feature behavior'],
    [taskSummary.trials ?? 'unknown', 'Go/No-go trials'],
    [taskSummary.accuracy != null ? `${{(taskSummary.accuracy * 100).toFixed(1)}}%` : 'unknown', 'accuracy']
  ];
  document.getElementById('metrics').innerHTML = items.map(([value, label]) =>
    `<div class="metric"><b>${{escapeHtml(String(value))}}</b><span>${{escapeHtml(label)}}</span></div>`
  ).join('');
}}

function setupSegments() {{
  const options = segments.map(segment =>
    `<option value="${{escapeHtml(segment.key)}}">${{escapeHtml(segment.label)}} (${{formatDuration(segment.duration)}})</option>`
  ).join('');
  document.querySelectorAll('.segmentControl').forEach(select => {{
    select.innerHTML = options;
    select.value = state.segmentKey;
    select.addEventListener('change', () => setSegment(select.value));
  }});
}}

function setupMarkers() {{
  const host = document.getElementById('markerToggles');
  host.innerHTML = categories.map(category =>
    `<label><input type="checkbox" data-category="${{escapeHtml(category)}}" checked> ${{escapeHtml(category.replaceAll('_',' '))}}</label>`
  ).join('');
  host.querySelectorAll('input').forEach(input => {{
    input.addEventListener('change', () => {{
      if (input.checked) state.enabledCategories.add(input.dataset.category);
      else state.enabledCategories.delete(input.dataset.category);
      drawReplay();
      drawAlphaGraph();
      drawFeatureGraph();
    }});
  }});
  renderMarkerLegend();
}}

function setupControls() {{
  document.getElementById('playPause').addEventListener('click', () => {{
    state.playing = !state.playing;
    document.getElementById('playPause').textContent = state.playing ? 'Pause' : 'Play';
  }});
  document.getElementById('speed').addEventListener('change', drawReplay);
  document.getElementById('windowSeconds').addEventListener('input', drawReplay);
  document.getElementById('uvScale').addEventListener('input', drawReplay);
  document.getElementById('showRawStack').addEventListener('change', drawReplay);
  document.getElementById('showAlphaGraphMarkers').addEventListener('change', event => {{
    state.showAlphaGraphMarkers = event.target.checked;
    drawAlphaGraph();
  }});
  const slider = document.getElementById('timeSlider');
  slider.addEventListener('input', () => {{
    state.time = Number(slider.value);
    state.playing = false;
    document.getElementById('playPause').textContent = 'Play';
    drawReplay();
  }});
  document.getElementById('toggleAlpha').addEventListener('click', () => {{
    const panel = document.getElementById('alphaPanel');
    panel.classList.toggle('open');
    document.getElementById('toggleAlpha').textContent = panel.classList.contains('open') ? 'Hide Alpha Channels' : 'Show Alpha Channels';
    drawAlphaGraph();
  }});
  document.getElementById('alphaReset').addEventListener('click', () => {{
    const segment = activeSegment();
    state.alphaStart = segment.start;
    state.alphaEnd = segment.end;
    drawAlphaGraph();
  }});
  document.getElementById('alphaZoomIn').addEventListener('click', () => zoomAlpha(0.5));
  document.getElementById('alphaZoomOut').addEventListener('click', () => zoomAlpha(2));
  const alphaCanvas = document.getElementById('alphaCanvas');
  alphaCanvas.addEventListener('mousedown', event => {{
    state.alphaDragging = true;
    state.alphaDragX = event.clientX;
  }});
  window.addEventListener('mouseup', () => state.alphaDragging = false);
  window.addEventListener('mousemove', event => {{
    if (!state.alphaDragging) return;
    const span = state.alphaEnd - state.alphaStart;
    const dx = event.clientX - state.alphaDragX;
    state.alphaDragX = event.clientX;
    const shift = -dx / Math.max(1, alphaCanvas.clientWidth) * span;
    setAlphaRange(state.alphaStart + shift, state.alphaEnd + shift);
  }});
  alphaCanvas.addEventListener('wheel', event => {{
    event.preventDefault();
    zoomAlpha(event.deltaY > 0 ? 1.2 : 0.8);
  }});
  new ResizeObserver(() => {{ drawReplay(); drawAlphaGraph(); drawFeatureGraph(); }}).observe(document.body);
}}

function activeSegment() {{
  return segmentByKey[state.segmentKey] || segments[0] || {{key:'full', label:'Full Time Series', start:0, end:duration, duration}};
}}

function setSegment(key) {{
  const segment = segmentByKey[key] || activeSegment();
  state.segmentKey = segment.key;
  state.time = segment.start;
  state.alphaStart = segment.start;
  state.alphaEnd = segment.end;
  state.playing = false;
  document.getElementById('playPause').textContent = 'Play';
  syncSegmentControls();
  updateSliderBounds();
  drawReplay();
  drawAlphaGraph();
  drawFeatureGraph();
}}

function syncSegmentControls() {{
  document.querySelectorAll('.segmentControl').forEach(select => {{
    if (select.value !== state.segmentKey) select.value = state.segmentKey;
  }});
}}

function updateSliderBounds() {{
  const segment = activeSegment();
  const slider = document.getElementById('timeSlider');
  slider.min = String(segment.start);
  slider.max = String(segment.end);
  slider.value = String(state.time);
}}

function tick(now) {{
  if (state.lastFrame == null) state.lastFrame = now;
  const dt = (now - state.lastFrame) / 1000;
  state.lastFrame = now;
  const segment = activeSegment();
  if (state.playing) {{
    state.time += dt * Number(document.getElementById('speed').value);
    if (state.time >= segment.end) {{
      state.time = segment.end;
      state.playing = false;
      document.getElementById('playPause').textContent = 'Play';
    }}
    drawReplay();
  }}
  requestAnimationFrame(tick);
}}

function canvasContext(id) {{
  return canvasContextForElement(document.getElementById(id));
}}

function canvasContextForElement(canvas) {{
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width * dpr));
  const height = Math.max(1, Math.floor(rect.height * dpr));
  if (canvas.width !== width || canvas.height !== height) {{
    canvas.width = width;
    canvas.height = height;
  }}
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return {{canvas, ctx, width: rect.width, height: rect.height}};
}}

function drawReplay() {{
  updateSliderBounds();
  drawReplayAlpha();
  drawRawStack();
}}

function replayWindow() {{
  const segment = activeSegment();
  const segmentSpan = Math.max(0.1, segment.end - segment.start);
  const requested = Math.max(0.1, Number(document.getElementById('windowSeconds').value));
  const span = Math.min(requested, segmentSpan);
  let start = state.time - span / 2;
  let end = state.time + span / 2;
  if (start < segment.start) {{
    start = segment.start;
    end = start + span;
  }}
  if (end > segment.end) {{
    end = segment.end;
    start = end - span;
  }}
  return {{start, end, segment}};
}}

function drawReplayAlpha() {{
  const {{ctx, width, height}} = canvasContext('replayCanvas');
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);
  const {{start, end, segment}} = replayWindow();
  drawGrid(ctx, width, 18, height - 42, start, end);
  drawReplayMarkers(ctx, width, 18, height - 42, start, end, false);
  drawAlphaOverlay(ctx, width, 18, height - 42, start, end);
  drawReplayMarkers(ctx, width, 18, height - 42, start, end, true);
  const cursorX = map(state.time, start, end, 54, width - 12);
  ctx.strokeStyle = '#111827';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(cursorX, 18);
  ctx.lineTo(cursorX, height - 24);
  ctx.stroke();
  document.getElementById('timeSlider').value = String(state.time);
  document.getElementById('timeReadout').textContent =
    `${{segment.label}}: ${{(state.time - segment.start).toFixed(2)}} s / ${{(segment.end - segment.start).toFixed(2)}} s`;
}}

function drawRawStack() {{
  const host = document.getElementById('rawStackPanel');
  const showRaw = document.getElementById('showRawStack').checked;
  host.classList.toggle('hidden', !showRaw);
  if (!showRaw) return;
  const channels = raw.channels || [];
  if (raw.status !== 'ok' || !raw.samples.length || !channels.length) {{
    host.innerHTML = `<div class="channel-replay"><canvas></canvas></div>`;
    const {{ctx, width, height}} = canvasContextForElement(host.querySelector('canvas'));
    centeredText(ctx, width, height, `Raw EEG is ${{raw.status || 'missing'}}`);
    return;
  }}
  ensureRawCanvases(channels);
  const {{start, end}} = replayWindow();
  const displayTimebase = raw.display_timebase || DATA.display_timebase || 'lsl';
  const times = raw.times.map(row => Number.isFinite(row.display) ? row.display : row[displayTimebase]);
  const uvScale = Math.max(1, Number(document.getElementById('uvScale').value));
  channels.forEach((name, channelIndex) => {{
    const canvas = host.querySelector(`canvas[data-channel-index="${{channelIndex}}"]`);
    if (!canvas) return;
    const channelValues = visibleChannelValues(times, channelIndex, start, end);
    const centerUv = median(channelValues);
    const {{ctx, width, height}} = canvasContextForElement(canvas);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, width, height);
    drawGrid(ctx, width, 10, height - 20, start, end);
    drawReplayMarkers(ctx, width, 10, height - 20, start, end, false);
    const base = height / 2;
    if (!Number.isFinite(centerUv)) {{
      centeredText(ctx, width, height, `${{name}} has no visible samples`);
      return;
    }}
    ctx.strokeStyle = colors[channelIndex % colors.length];
    ctx.lineWidth = 1;
    ctx.beginPath();
    let started = false;
    for (let i = 0; i < times.length; i++) {{
      const t = times[i];
      if (t < start || t > end) continue;
      const x = map(t, start, end, 54, width - 12);
      const sample = raw.samples[i] || [];
      const value = Number(sample[channelIndex]);
      if (!Number.isFinite(value)) continue;
      const y = base - ((value - centerUv) / uvScale) * (height * 0.34);
      if (!started) {{ ctx.moveTo(x, y); started = true; }}
      else ctx.lineTo(x, y);
    }}
    ctx.stroke();
    ctx.fillStyle = '#334155';
    ctx.font = '12px sans-serif';
    ctx.fillText(name, 8, 18);
    ctx.fillStyle = '#64748b';
    ctx.font = '10px sans-serif';
    ctx.fillText(`center ${{centerUv.toFixed(1)}}`, 8, height - 10);
    ctx.strokeStyle = '#e5e7eb';
    ctx.beginPath();
    ctx.moveTo(54, base);
    ctx.lineTo(width - 12, base);
    ctx.stroke();
    const cursorX = map(state.time, start, end, 54, width - 12);
    ctx.strokeStyle = '#111827';
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.moveTo(cursorX, 10);
    ctx.lineTo(cursorX, height - 10);
    ctx.stroke();
  }});
}}

function visibleChannelValues(times, channelIndex, start, end) {{
  const values = [];
  for (let i = 0; i < times.length; i++) {{
    const t = times[i];
    if (!Number.isFinite(t) || t < start || t > end) continue;
    const sample = raw.samples[i] || [];
    const value = Number(sample[channelIndex]);
    if (Number.isFinite(value)) values.push(value);
  }}
  return values;
}}

function median(values) {{
  if (!values.length) return NaN;
  const sorted = values.slice().sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}}

function ensureRawCanvases(channels) {{
  const host = document.getElementById('rawStackPanel');
  if (host.dataset.channelCount === String(channels.length)) return;
  host.dataset.channelCount = String(channels.length);
  host.innerHTML = channels.map((name, index) =>
    `<div class="channel-replay"><canvas data-channel-index="${{index}}" aria-label="${{escapeHtml(name)}} raw EEG replay"></canvas></div>`
  ).join('');
}}

function drawGrid(ctx, width, top, height, start, end) {{
  ctx.strokeStyle = '#edf0f3';
  ctx.lineWidth = 1;
  const step = niceStep((end - start) / 6);
  for (let t = Math.ceil(start / step) * step; t <= end; t += step) {{
    const x = map(t, start, end, 54, width - 12);
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, top + height);
    ctx.stroke();
    ctx.fillStyle = '#64748b';
    ctx.font = '11px sans-serif';
    ctx.fillText(`${{t.toFixed(1)}}s`, x + 3, top + 12);
  }}
}}

function drawReplayMarkers(ctx, width, top, height, start, end, showLabels = true, x0 = 54, x1 = width - 12) {{
  const visible = markers.filter(marker => Number.isFinite(marker.time) && marker.time >= start && marker.time <= end && state.enabledCategories.has(marker.category));
  visible.forEach(marker => {{
    const x = map(marker.time, start, end, x0, x1);
    const noGo = marker.category === 'no_go';
    const bandWidth = noGo ? 12 : 4;
    ctx.fillStyle = markerColor(marker.category, noGo ? 0.26 : 0.13);
    ctx.fillRect(x - bandWidth / 2, top, bandWidth, height);
    ctx.strokeStyle = markerColor(marker.category, 0.9);
    ctx.lineWidth = noGo ? 2.2 : 1;
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, top + height);
    ctx.stroke();
    if (!showLabels) return;
    const label = markerDisplayLabel(marker);
    if (noGo) {{
      ctx.save();
      ctx.fillStyle = markerColor(marker.category, 0.94);
      ctx.fillRect(Math.min(x + 6, x1 - 72), top + 8, 64, 18);
      ctx.fillStyle = '#ffffff';
      ctx.font = 'bold 11px sans-serif';
      ctx.fillText(label, Math.min(x + 10, x1 - 68), top + 21);
      ctx.restore();
      return;
    }}
    ctx.save();
    ctx.translate(x + 5, top + 12);
    ctx.rotate(-Math.PI / 2);
    ctx.fillStyle = markerColor(marker.category, 1);
    ctx.font = '11px sans-serif';
    ctx.fillText(label.slice(0, 72), 0, 0);
    ctx.restore();
  }});
}}

function drawAlphaOverlay(ctx, width, top, height, start, end) {{
  ctx.fillStyle = '#f8fafc';
  ctx.fillRect(54, top, width - 66, height);
  ctx.strokeStyle = '#cbd5e1';
  ctx.strokeRect(54, top, width - 66, height);
  ctx.fillStyle = '#334155';
  ctx.font = '12px sans-serif';
  ctx.fillText('alpha', 8, top + 18);
  if (offlineAlpha.status !== 'ok') {{
    centeredText(ctx, width, height, `Offline alpha is ${{offlineAlpha.status || 'missing'}}`);
    return;
  }}
  const values = finiteValues(offlineAlpha.posterior || []);
  const liveValues = finiteValues(liveAlpha.map(row => row.alpha_power));
  const minY = Math.min(...values, ...liveValues, 0);
  const maxY = Math.max(...values, ...liveValues, 1);
  drawLine(ctx, offlineAlpha.times || [], offlineAlpha.posterior || [], start, end, 54, width - 12, top + height - 8, top + 8, minY, maxY, '#0f766e');
  ctx.fillStyle = '#7c3aed';
  liveAlpha.forEach(row => {{
    if (!Number.isFinite(row.time) || row.time < start || row.time > end) return;
    const x = map(row.time, start, end, 54, width - 12);
    const y = map(row.alpha_power, minY, maxY, top + height - 8, top + 8);
    ctx.beginPath();
    ctx.arc(x, y, row.valid ? 2.5 : 4, 0, Math.PI * 2);
    ctx.fill();
  }});
}}

function drawAlphaGraph() {{
  const {{ctx, width, height}} = canvasContext('alphaCanvas');
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);
  const channels = offlineAlpha.channels || [];
  const times = offlineAlpha.times || [];
  const segment = activeSegment();
  document.getElementById('alphaStatus').textContent = offlineAlpha.status === 'ok'
    ? `${{segment.label}}; ${{channels.length}} channels, ${{offlineAlpha.window_count}} windows`
    : `offline alpha is ${{offlineAlpha.status || 'missing'}}`;
  if (offlineAlpha.status !== 'ok' || !channels.length) {{
    centeredText(ctx, width, height, `Offline alpha is ${{offlineAlpha.status || 'missing'}}`);
    return;
  }}
  const start = state.alphaStart;
  const end = state.alphaEnd;
  const left = 58;
  const right = 18;
  const top = 18;
  const bottom = 34;
  const values = finiteValues(channels.flatMap(channel => channel.values));
  const minY = Math.min(...values, 0);
  const maxY = Math.max(...values, 1);
  ctx.strokeStyle = '#e5e7eb';
  ctx.strokeRect(left, top, width - left - right, height - top - bottom);
  channels.forEach((channel, index) => {{
    drawLine(ctx, times, channel.values, start, end, left, width - right, height - bottom, top, minY, maxY, colors[index % colors.length]);
  }});
  if (state.showAlphaGraphMarkers) {{
    drawReplayMarkers(ctx, width, top, height - top - bottom, start, end, true, left, width - right);
  }}
  channels.slice(0, 12).forEach((channel, index) => {{
    const x = left + 8 + (index % 4) * 120;
    const y = top + 16 + Math.floor(index / 4) * 16;
    ctx.fillStyle = colors[index % colors.length];
    ctx.fillRect(x, y - 8, 10, 2);
    ctx.fillStyle = '#334155';
    ctx.font = '11px sans-serif';
    ctx.fillText(channel.name, x + 14, y - 4);
  }});
  ctx.fillStyle = '#64748b';
  ctx.font = '11px sans-serif';
  ctx.fillText(`${{start.toFixed(1)}}s`, left, height - 12);
  ctx.fillText(`${{end.toFixed(1)}}s`, width - right - 52, height - 12);
}}

function drawFeatureGraph() {{
  const {{ctx, width, height}} = canvasContext('featureCanvas');
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, width, height);
  const segment = activeSegment();
  const points = (stagedFeatures.points || []).filter(point =>
    Number.isFinite(point.time) && point.time >= segment.start && point.time <= segment.end
  );
  document.getElementById('featureStatus').textContent = stagedFeatures.status === 'ok'
    ? `${{segment.label}}; ${{points.length}} staged packets`
    : `staged features are ${{stagedFeatures.status || 'missing'}}`;
  if (stagedFeatures.status !== 'ok' || !points.length) {{
    centeredText(ctx, width, height, `No staged realtime features in ${{segment.label}}`);
    return;
  }}
  const left = 150;
  const right = 18;
  const top = 18;
  const laneHeight = (height - top - 28) / featureDefinitions.length;
  featureDefinitions.forEach(([key, label, color], laneIndex) => {{
    const laneTop = top + laneIndex * laneHeight;
    const laneBottom = laneTop + laneHeight - 8;
    const values = points.map(point => point[key]).filter(Number.isFinite);
    ctx.fillStyle = laneIndex % 2 ? '#fbfcfd' : '#f7f9fb';
    ctx.fillRect(left, laneTop, width - left - right, laneHeight - 2);
    ctx.strokeStyle = '#e5e7eb';
    ctx.strokeRect(left, laneTop, width - left - right, laneHeight - 2);
    ctx.fillStyle = color;
    ctx.font = '12px sans-serif';
    ctx.fillText(label, 8, laneTop + 18);
    if (!values.length) {{
      ctx.fillStyle = '#94a3b8';
      ctx.fillText('no valid values', 8, laneTop + 36);
      return;
    }}
    const minValue = Math.min(...values);
    const maxValue = Math.max(...values);
    const padding = Math.max(1e-9, (maxValue - minValue) * 0.08);
    points.forEach(point => {{
      const value = point[key];
      if (!Number.isFinite(value)) return;
      const x = map(point.time, segment.start, segment.end, left, width - right);
      const y = map(value, minValue - padding, maxValue + padding, laneBottom, laneTop + 6);
      ctx.fillStyle = point.valid ? color : '#94a3b8';
      ctx.beginPath();
      ctx.arc(x, y, point.valid ? 2.5 : 4, 0, Math.PI * 2);
      ctx.fill();
    }});
    ctx.fillStyle = '#64748b';
    ctx.font = '10px sans-serif';
    ctx.fillText(`${{minValue.toPrecision(3)}} to ${{maxValue.toPrecision(3)}}`, 8, laneTop + 34);
  }});
  drawReplayMarkers(ctx, width, top, height - top - 28, segment.start, segment.end, false, left, width - right);
  ctx.fillStyle = '#64748b';
  ctx.font = '11px sans-serif';
  ctx.fillText(`${{segment.start.toFixed(1)}}s`, left, height - 8);
  ctx.fillText(`${{segment.end.toFixed(1)}}s`, width - right - 52, height - 8);
}}

function drawLine(ctx, times, values, start, end, x0, x1, y0, y1, minY, maxY, color) {{
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  let started = false;
  for (let i = 0; i < times.length; i++) {{
    const t = times[i];
    const value = values[i];
    if (!Number.isFinite(t) || !Number.isFinite(value) || t < start || t > end) continue;
    const x = map(t, start, end, x0, x1);
    const y = map(value, minY, maxY || minY + 1, y0, y1);
    if (!started) {{ ctx.moveTo(x, y); started = true; }}
    else ctx.lineTo(x, y);
  }}
  ctx.stroke();
}}

function zoomAlpha(multiplier) {{
  const center = (state.alphaStart + state.alphaEnd) / 2;
  const span = Math.max(0.5, (state.alphaEnd - state.alphaStart) * multiplier);
  setAlphaRange(center - span / 2, center + span / 2);
}}

function setAlphaRange(start, end) {{
  const segment = activeSegment();
  const span = Math.max(0.5, end - start);
  let nextStart = Math.max(segment.start, start);
  let nextEnd = nextStart + span;
  if (nextEnd > segment.end) {{
    nextEnd = segment.end;
    nextStart = Math.max(segment.start, nextEnd - span);
  }}
  state.alphaStart = nextStart;
  state.alphaEnd = nextEnd;
  drawAlphaGraph();
}}

function map(value, inMin, inMax, outMin, outMax) {{
  if (inMax === inMin) return (outMin + outMax) / 2;
  return outMin + (value - inMin) / (inMax - inMin) * (outMax - outMin);
}}

function niceStep(value) {{
  const rawStep = Math.max(0.1, value);
  const magnitude = Math.pow(10, Math.floor(Math.log10(rawStep)));
  const normalized = rawStep / magnitude;
  const nice = normalized < 1.5 ? 1 : normalized < 3 ? 2 : normalized < 7 ? 5 : 10;
  return nice * magnitude;
}}

function formatDuration(seconds) {{
  if (!Number.isFinite(seconds)) return '?';
  if (seconds < 60) return `${{seconds.toFixed(1)}}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds - minutes * 60);
  return `${{minutes}}m ${{remainder}}s`;
}}

function markerColor(category, alpha) {{
  const lookup = {{
    no_go: [190, 18, 60],
    stimulus_on: [37, 99, 235],
    stimulus_off: [15, 118, 110],
    button_press: [220, 38, 38],
    calibration: [180, 83, 9],
    feedback: [124, 58, 237],
    system: [71, 85, 105],
    marker: [219, 39, 119]
  }};
  const rgb = lookup[category] || lookup.marker;
  return `rgba(${{rgb[0]}},${{rgb[1]}},${{rgb[2]}},${{alpha}})`;
}}

function markerDisplayLabel(marker) {{
  if (marker.category === 'no_go') return marker.trial != null ? `NO-GO ${{marker.trial}}` : 'NO-GO';
  if (marker.category === 'stimulus_on') return marker.trial != null ? `stim on ${{marker.trial}}` : 'stim on';
  if (marker.category === 'stimulus_off') return marker.trial != null ? `stim off ${{marker.trial}}` : 'stim off';
  if (marker.category === 'button_press') return marker.trial != null ? `press ${{marker.trial}}` : 'press';
  return marker.label || marker.category;
}}

function renderMarkerLegend() {{
  const host = document.getElementById('markerLegend');
  host.innerHTML = categories.map(category => {{
    const noGo = category === 'no_go';
    const swatchClass = noGo ? 'legend-band' : 'legend-swatch';
    return `<span class="legend-item"><span class="${{swatchClass}}" style="background:${{markerColor(category, noGo ? 0.85 : 0.95)}}"></span>${{escapeHtml(category.replaceAll('_',' '))}}</span>`;
  }}).join('');
}}

function centeredText(ctx, width, height, text) {{
  ctx.fillStyle = '#64748b';
  ctx.font = '14px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText(text, width / 2, height / 2);
  ctx.textAlign = 'start';
}}

function escapeHtml(value) {{
  return value.replace(/[&<>"']/g, char => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[char]));
}}

setup();
</script>
</body>
</html>
"""


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _choose_display_timebase(root: Path, raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    raw_lsl_start = _optional_float(raw.get("lsl_start"))
    raw_monotonic_start = _optional_float(raw.get("monotonic_start"))
    raw_lsl_duration = _optional_float(raw.get("lsl_duration_seconds")) or _optional_float(raw.get("duration_seconds")) or 0.0
    raw_monotonic_duration = _optional_float(raw.get("monotonic_duration_seconds")) or raw_lsl_duration
    lsl_timestamps: list[float] = []
    monotonic_timestamps: list[float] = []
    for row in _load_jsonl(root / "events" / "events.jsonl"):
        metadata = dict(row.get("metadata") or {})
        if (value := _optional_float(metadata.get("lsl_timestamp"))) is not None:
            lsl_timestamps.append(value)
        if (value := _optional_float(row.get("timestamp"))) is not None:
            monotonic_timestamps.append(value)
    manifest = _load_json(root / "events" / "stimulus_manifest.json") or {}
    for trial in manifest.get("trials", []):
        for field in ("onset_lsl_timestamp", "offset_lsl_timestamp"):
            if (value := _optional_float(trial.get(field))) is not None:
                lsl_timestamps.append(value)
        for field in ("onset_monotonic", "offset_monotonic"):
            if (value := _optional_float(trial.get(field))) is not None:
                monotonic_timestamps.append(value)
    lsl_hits = _overlap_count(lsl_timestamps, raw_lsl_start, raw_lsl_duration)
    monotonic_hits = _overlap_count(monotonic_timestamps, raw_monotonic_start, raw_monotonic_duration)
    timebase = "monotonic" if monotonic_hits > lsl_hits else "lsl"
    if lsl_hits == 0 and monotonic_hits > 0:
        timebase = "monotonic"
    return timebase, {
        "lsl_overlap_markers": lsl_hits,
        "monotonic_overlap_markers": monotonic_hits,
        "raw_lsl_start": raw_lsl_start,
        "raw_monotonic_start": raw_monotonic_start,
        "raw_lsl_duration_seconds": raw_lsl_duration,
        "raw_monotonic_duration_seconds": raw_monotonic_duration,
    }


def _apply_raw_display_timebase(raw: dict[str, Any], display_timebase: str) -> None:
    raw["display_timebase"] = display_timebase
    if display_timebase == "monotonic":
        raw["duration_seconds"] = raw.get("monotonic_duration_seconds") or raw.get("duration_seconds")
    else:
        raw["duration_seconds"] = raw.get("lsl_duration_seconds") or raw.get("duration_seconds")
    for row in raw.get("times", []):
        row["display"] = row.get(display_timebase)


def _overlap_count(timestamps: list[float], raw_start: float | None, duration_seconds: float | None) -> int:
    if raw_start is None or duration_seconds is None:
        return 0
    lower = raw_start - 2.0
    upper = raw_start + max(0.0, duration_seconds) + 2.0
    return sum(1 for value in timestamps if lower <= value <= upper)


def _select_relative_time(lsl_time: float | None, monotonic_time: float | None, display_timebase: str) -> tuple[float | None, str | None]:
    if display_timebase == "monotonic" and monotonic_time is not None:
        return monotonic_time, "monotonic"
    if display_timebase == "lsl" and lsl_time is not None:
        return lsl_time, "lsl"
    if monotonic_time is not None:
        return monotonic_time, "monotonic"
    if lsl_time is not None:
        return lsl_time, "lsl"
    return None, None


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _round(value: Any, digits: int = 6) -> float | None:
    number = _optional_float(value)
    if number is None:
        return None
    return round(number, digits)
