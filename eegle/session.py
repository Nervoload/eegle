"""Session folder creation using a BciPy-inspired layout."""

from __future__ import annotations

import json
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from eegle.config import merged_config, resolve_path, resolve_session_root, write_config
from eegle.hardware.system import system_snapshot
from eegle.lsl import session_marker_source_id


_WINDOWS_RESERVED_COMPONENTS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_INVALID_COMPONENT_CHARACTERS = set('<>:"/\\|?*')


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
    eeg_xdf: Path
    xdf_metadata: Path
    realtime_windows_jsonl: Path
    realtime_decisions_jsonl: Path
    realtime_model_predictions_jsonl: Path
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
    realtime_model_snapshots: Path
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
    participant = str(participant_id or experiment.get("participant_id", "example-participant"))
    participant_component = participant_storage_component(participant)
    experiment_id = experiment.get("experiment_id", "experiment")
    session_root = resolve_session_root(config, root)
    now = datetime.now()
    run_stamp = now.strftime("run-%Y%m%dT%H%M%S")
    participant_root = session_root / "participants"
    participant_dir = participant_root / participant_component
    assert_lexically_contained(participant_root, participant_dir)
    session_dir = _unique_session_dir(
        participant_dir
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
        eeg_xdf=session_dir / "raw" / "recording.xdf",
        xdf_metadata=session_dir / "raw" / "xdf_metadata.json",
        realtime_windows_jsonl=session_dir / "realtime" / "windows.jsonl",
        realtime_decisions_jsonl=session_dir / "realtime" / "decisions.jsonl",
        realtime_model_predictions_jsonl=session_dir / "realtime" / "model_predictions.jsonl",
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
        realtime_model_snapshots=session_dir / "realtime" / "models",
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
        paths.realtime_model_snapshots,
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
            "runtime": {"session_root": str(session_root)},
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
        "participant_storage_component": participant_component,
        "task": task_name,
        "experiment_id": experiment_id,
        "components": experiment.get("components", {}),
        "processes": config.get("processes", {}),
        "marker_stream": {
            "name": config_to_write.get("hardware", {}).get("markers", {}).get("lsl_stream_name", "EEGleMarkers"),
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


def participant_storage_component(participant_id: str) -> str:
    """Return a stable Windows-safe directory component without restricting the ID.

    Ordinary single-component names are retained verbatim. Only values which
    could redirect a path or cannot be created as a Windows directory are
    represented by a stable digest. The original identifier remains in session
    parameters and manifests.
    """

    value = str(participant_id)
    if _participant_component_is_safe(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"participant-{digest}"


def _participant_component_is_safe(value: str) -> bool:
    if not value or value in {".", ".."} or value[-1:] in {" ", "."}:
        return False
    if any(ord(character) < 32 for character in value):
        return False
    if any(character in _WINDOWS_INVALID_COMPONENT_CHARACTERS for character in value):
        return False
    windows = PureWindowsPath(value)
    if windows.is_absolute() or windows.drive or windows.root or len(windows.parts) != 1:
        return False
    if Path(value).is_absolute() or len(Path(value).parts) != 1:
        return False
    device_name = value.split(".", 1)[0].upper()
    return device_name not in _WINDOWS_RESERVED_COMPONENTS


def assert_lexically_contained(parent: Path, child: Path) -> None:
    """Reject a child path which lexically leaves its intended parent."""

    parent_absolute = os.path.abspath(os.fspath(parent))
    child_absolute = os.path.abspath(os.fspath(child))
    try:
        common = os.path.commonpath((parent_absolute, child_absolute))
    except ValueError as exc:
        raise ValueError(f"participant session path is outside the configured root: {child}") from exc
    if os.path.normcase(common) != os.path.normcase(parent_absolute):
        raise ValueError(f"participant session path is outside the configured root: {child}")


def _unique_session_dir(candidate: Path) -> Path:
    """Avoid reusing a child recording directory when attempts start in one second."""
    if not candidate.exists():
        return candidate
    for suffix in range(1, 1000):
        alternate = candidate.with_name(f"{candidate.name}-{suffix:02d}")
        if not alternate.exists():
            return alternate
    raise RuntimeError(f"could not allocate a unique session directory beside {candidate}")


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
        eeg_xdf=session_dir / "raw" / "recording.xdf",
        xdf_metadata=session_dir / "raw" / "xdf_metadata.json",
        realtime_windows_jsonl=session_dir / "realtime" / "windows.jsonl",
        realtime_decisions_jsonl=session_dir / "realtime" / "decisions.jsonl",
        realtime_model_predictions_jsonl=session_dir / "realtime" / "model_predictions.jsonl",
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
        realtime_model_snapshots=session_dir / "realtime" / "models",
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
        paths.realtime_model_snapshots,
        paths.process_logs,
    ):
        folder.mkdir(parents=True, exist_ok=True)
    return paths
