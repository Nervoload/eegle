"""Component factories selected by experiment config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eegle.devices.lsl_eeg import LslEegRecorder
from eegle.tasks.go_nogo import GoNoGoTask
from eegle.tasks.pvt import PVTTask


def make_task_component(
    component_name: str,
    config: dict[str, Any],
    task_mode: str,
    trials: int | None,
    participant_id: str | None,
) -> PVTTask | GoNoGoTask:
    if component_name == "pvt":
        return PVTTask(config, mode=task_mode, trials=trials, participant_id=participant_id)
    if component_name == "go_nogo":
        return GoNoGoTask(config, mode=task_mode, trials=trials, participant_id=participant_id)
    raise NotImplementedError(f"task component '{component_name}' is not implemented")


def make_eeg_recorder_component(
    component_name: str,
    config: dict[str, Any],
    raw_file: str | Path,
    metadata_file: str | Path,
) -> LslEegRecorder | None:
    if component_name == "disabled":
        return None
    if component_name == "lsl_csv":
        eeg_config = config.get("hardware", {}).get("eeg", {})
        return LslEegRecorder(
            eeg_config=eeg_config,
            raw_file=raw_file,
            metadata_file=metadata_file,
            stream_timeout_seconds=float(eeg_config.get("stream_timeout_seconds", 5.0)),
        )
    raise NotImplementedError(f"EEG recorder component '{component_name}' is not implemented")
