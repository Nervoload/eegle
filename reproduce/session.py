"""Session folder creation using a BciPy-inspired layout."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from reproduce.config import merged_config, resolve_path, write_config
from reproduce.hardware.system import system_snapshot
from reproduce.lsl import session_marker_source_id


@dataclass
class SessionPaths:
    root: Path
    raw: Path
    events: Path
    calibration: Path
    logs: Path
    reports: Path
    realtime: Path
    process_logs: Path
    parameters: Path
    manifest: Path
    triggers: Path
    behavior_csv: Path
    events_jsonl: Path
    telemetry_jsonl: Path
    debug_jsonl: Path
    calibration_events_jsonl: Path
    calibration_metadata: Path
    calibration_eeg_csv: Path
    calibration_result: Path
    calibration_psd_csv: Path
    calibration_spectral_model_json: Path
    calibration_plot: Path
    eeg_csv: Path
    eeg_metadata: Path
    realtime_windows_jsonl: Path
    realtime_decisions_jsonl: Path
    realtime_markers_jsonl: Path
    realtime_feedback_jsonl: Path
    realtime_alpha_jsonl: Path
    realtime_event_features_jsonl: Path
    realtime_engine_capture: Path
    realtime_engine_metadata: Path
    realtime_epochs: Path
    realtime_epochs_jsonl: Path
    realtime_epochs_npz: Path
    realtime_epoch_manifest: Path
    manager_summary: Path
    completion_summary: Path


def create_session(
    config: dict[str, Any],
    task: str | None = None,
    participant_id: str | None = None,
    root: str | Path | None = None,
) -> SessionPaths:
    experiment = config.get("experiment", {})
    runtime = config.get("runtime", {})
    task_name = task or experiment.get("task", "pvt")
    participant = participant_id or experiment.get("participant_id", "example-participant")
    experiment_id = experiment.get("experiment_id", "experiment")
    session_root = resolve_path(root or runtime.get("session_root", "data"))
    now = datetime.now()
    run_stamp = now.strftime("run-%Y%m%dT%H%M%S")
    session_dir = (
        session_root
        / "participants"
        / participant
        / "sessions"
        / now.strftime("%Y-%m-%d")
        / experiment_id
        / task_name
        / run_stamp
    )

    paths = SessionPaths(
        root=session_dir,
        raw=session_dir / "raw",
        events=session_dir / "events",
        calibration=session_dir / "calibration",
        logs=session_dir / "logs",
        reports=session_dir / "reports",
        realtime=session_dir / "realtime",
        process_logs=session_dir / "logs" / "processes",
        parameters=session_dir / "parameters.json",
        manifest=session_dir / "manifest.json",
        triggers=session_dir / "triggers.txt",
        behavior_csv=session_dir / "events" / "behavior.csv",
        events_jsonl=session_dir / "events" / "events.jsonl",
        telemetry_jsonl=session_dir / "logs" / "telemetry.jsonl",
        debug_jsonl=session_dir / "logs" / "debug.jsonl",
        calibration_events_jsonl=session_dir / "calibration" / "events.jsonl",
        calibration_metadata=session_dir / "calibration" / "metadata.json",
        calibration_eeg_csv=session_dir / "calibration" / "eeg.csv",
        calibration_result=session_dir / "calibration" / "alpha_calibration.json",
        calibration_psd_csv=session_dir / "calibration" / "psd.csv",
        calibration_spectral_model_json=session_dir / "calibration" / "specparam.json",
        calibration_plot=session_dir / "calibration" / "alpha_calibration.svg",
        eeg_csv=session_dir / "raw" / "eeg.csv",
        eeg_metadata=session_dir / "raw" / "eeg_metadata.json",
        realtime_windows_jsonl=session_dir / "realtime" / "windows.jsonl",
        realtime_decisions_jsonl=session_dir / "realtime" / "decisions.jsonl",
        realtime_markers_jsonl=session_dir / "realtime" / "markers.jsonl",
        realtime_feedback_jsonl=session_dir / "realtime" / "feedback.jsonl",
        realtime_alpha_jsonl=session_dir / "realtime" / "alpha_power.jsonl",
        realtime_event_features_jsonl=session_dir / "realtime" / "event_features.jsonl",
        realtime_engine_capture=session_dir / "realtime" / "engine_input.bin",
        realtime_engine_metadata=session_dir / "realtime" / "engine_metadata.json",
        realtime_epochs=session_dir / "realtime" / "epochs",
        realtime_epochs_jsonl=session_dir / "realtime" / "epochs" / "epochs.jsonl",
        realtime_epochs_npz=session_dir / "realtime" / "epochs" / "epochs.npz",
        realtime_epoch_manifest=session_dir / "realtime" / "epochs" / "manifest.json",
        manager_summary=session_dir / "logs" / "feedback_manager.json",
        completion_summary=session_dir / "session_summary.json",
    )
    for folder in (
        paths.root,
        paths.raw,
        paths.events,
        paths.calibration,
        paths.logs,
        paths.reports,
        paths.realtime,
        paths.realtime_epochs,
        paths.process_logs,
    ):
        folder.mkdir(parents=True, exist_ok=True)

    marker_source_id = session_marker_source_id(paths.root)
    marker_config = config.get("hardware", {}).get("markers", {})
    realtime_enabled = bool(config.get("realtime", {}).get("enabled", False))
    marker_required = bool(marker_config.get("required_for_realtime", realtime_enabled))
    config_to_write = merged_config(
        config,
        {
            "experiment": {"task": task_name, "participant_id": participant},
            "hardware": {
                "markers": {
                    "source_id": marker_source_id,
                    "required_for_realtime": marker_required,
                }
            },
        },
    )
    write_config(config_to_write, paths.parameters)

    manifest = {
        "created_at": now.isoformat(timespec="seconds"),
        "participant_id": participant,
        "task": task_name,
        "experiment_id": experiment_id,
        "components": experiment.get("components", {}),
        "processes": config.get("processes", {}),
        "marker_stream": {
            "name": config_to_write.get("hardware", {}).get("markers", {}).get("lsl_stream_name", "ClosedLoopMarkers"),
            "type": config_to_write.get("hardware", {}).get("markers", {}).get("lsl_stream_type", "Markers"),
            "source_id": marker_source_id,
        },
        "display_timing": {
            "timing_model": "psychopy_callOnFlip_plus_fixed_display_latency",
            "expected_refresh_rate_hz": config_to_write.get("hardware", {}).get("display", {}).get("expected_refresh_rate_hz", 60.0),
            "fixed_display_latency_ms": config_to_write.get("hardware", {}).get("display", {}).get("fixed_display_latency_ms", 0.0),
            "photodiode_verification_enabled": bool(config_to_write.get("hardware", {}).get("display", {}).get("photodiode_patch", False)),
        },
        "telemetry": {
            "config": config.get("telemetry", {}),
            "telemetry_jsonl": str(paths.telemetry_jsonl),
            "debug_jsonl": str(paths.debug_jsonl),
        },
        "layout": "bcipy_style",
        "system": system_snapshot(),
    }
    with paths.manifest.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")

    return paths


def paths_for_existing_session(root: str | Path) -> SessionPaths:
    """Return the standard session paths for an already-created session."""
    session_dir = Path(root).expanduser().resolve()
    paths = SessionPaths(
        root=session_dir,
        raw=session_dir / "raw",
        events=session_dir / "events",
        calibration=session_dir / "calibration",
        logs=session_dir / "logs",
        reports=session_dir / "reports",
        realtime=session_dir / "realtime",
        process_logs=session_dir / "logs" / "processes",
        parameters=session_dir / "parameters.json",
        manifest=session_dir / "manifest.json",
        triggers=session_dir / "triggers.txt",
        behavior_csv=session_dir / "events" / "behavior.csv",
        events_jsonl=session_dir / "events" / "events.jsonl",
        telemetry_jsonl=session_dir / "logs" / "telemetry.jsonl",
        debug_jsonl=session_dir / "logs" / "debug.jsonl",
        calibration_events_jsonl=session_dir / "calibration" / "events.jsonl",
        calibration_metadata=session_dir / "calibration" / "metadata.json",
        calibration_eeg_csv=session_dir / "calibration" / "eeg.csv",
        calibration_result=session_dir / "calibration" / "alpha_calibration.json",
        calibration_psd_csv=session_dir / "calibration" / "psd.csv",
        calibration_spectral_model_json=session_dir / "calibration" / "specparam.json",
        calibration_plot=session_dir / "calibration" / "alpha_calibration.svg",
        eeg_csv=session_dir / "raw" / "eeg.csv",
        eeg_metadata=session_dir / "raw" / "eeg_metadata.json",
        realtime_windows_jsonl=session_dir / "realtime" / "windows.jsonl",
        realtime_decisions_jsonl=session_dir / "realtime" / "decisions.jsonl",
        realtime_markers_jsonl=session_dir / "realtime" / "markers.jsonl",
        realtime_feedback_jsonl=session_dir / "realtime" / "feedback.jsonl",
        realtime_alpha_jsonl=session_dir / "realtime" / "alpha_power.jsonl",
        realtime_event_features_jsonl=session_dir / "realtime" / "event_features.jsonl",
        realtime_engine_capture=session_dir / "realtime" / "engine_input.bin",
        realtime_engine_metadata=session_dir / "realtime" / "engine_metadata.json",
        realtime_epochs=session_dir / "realtime" / "epochs",
        realtime_epochs_jsonl=session_dir / "realtime" / "epochs" / "epochs.jsonl",
        realtime_epochs_npz=session_dir / "realtime" / "epochs" / "epochs.npz",
        realtime_epoch_manifest=session_dir / "realtime" / "epochs" / "manifest.json",
        manager_summary=session_dir / "logs" / "feedback_manager.json",
        completion_summary=session_dir / "session_summary.json",
    )
    for folder in (
        paths.raw,
        paths.events,
        paths.calibration,
        paths.logs,
        paths.reports,
        paths.realtime,
        paths.realtime_epochs,
        paths.process_logs,
    ):
        folder.mkdir(parents=True, exist_ok=True)
    return paths
