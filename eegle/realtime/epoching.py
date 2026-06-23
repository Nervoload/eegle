"""Marker-aligned EEG epoch extraction for online and offline workflows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from eegle.hardware.enobio import expected_profile


DEFAULT_MARKER_PREFIX = "go_nogo_stimulus_onset"


@dataclass(frozen=True)
class EpochingConfig:
    """Configuration for event-locked EEG epoch extraction."""

    enabled: bool = True
    marker_prefix: str = DEFAULT_MARKER_PREFIX
    tmin_seconds: float = -0.2
    tmax_seconds: float = 0.8
    timebase: str = "lsl"
    sample_tolerance_seconds: float = 0.01
    drop_incomplete: bool = True
    include_practice_trials: bool = False

    @property
    def duration_seconds(self) -> float:
        return self.tmax_seconds - self.tmin_seconds

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "EpochingConfig":
        cfg = dict(value or {})
        return cls(
            enabled=bool(cfg.get("enabled", True)),
            marker_prefix=str(cfg.get("marker_prefix", DEFAULT_MARKER_PREFIX)),
            tmin_seconds=float(cfg.get("tmin_seconds", -0.2)),
            tmax_seconds=float(cfg.get("tmax_seconds", 0.8)),
            timebase=str(cfg.get("timebase", "lsl")),
            sample_tolerance_seconds=float(cfg.get("sample_tolerance_seconds", 0.01)),
            drop_incomplete=bool(cfg.get("drop_incomplete", True)),
            include_practice_trials=bool(cfg.get("include_practice_trials", False)),
        )


@dataclass(frozen=True)
class MarkerEvent:
    """A task marker in the same timebase as an EEG timestamp column."""

    label: str
    timestamp: float
    timebase: str = "lsl"
    source: str = "lsl"
    metadata: dict[str, Any] = field(default_factory=dict)

    def parsed_metadata(self, marker_prefix: str = DEFAULT_MARKER_PREFIX) -> dict[str, Any]:
        parsed = parse_marker_label(self.label, marker_prefix)
        return {**parsed, **self.metadata}


@dataclass
class ExtractedEpoch:
    """An event-locked EEG epoch held in memory for online inference or export."""

    epoch_index: int
    marker: MarkerEvent
    data: np.ndarray
    timestamps: np.ndarray
    relative_times: np.ndarray
    sample_start: int
    sample_stop: int
    sample_rate_hz: float
    channel_names: list[str]
    config: EpochingConfig

    @property
    def model_input(self) -> np.ndarray:
        """Return model/training input as channels x samples."""
        return self.data.T

    def metadata_payload(self, include_data_shape: bool = True) -> dict[str, Any]:
        parsed = self.marker.parsed_metadata(self.config.marker_prefix)
        payload = {
            "schema_version": 1,
            "status": "ready",
            "epoch_index": self.epoch_index,
            "marker": {
                "label": self.marker.label,
                "timestamp": self.marker.timestamp,
                "timebase": self.marker.timebase,
                "source": self.marker.source,
                "metadata": parsed,
            },
            "epoch_window_seconds": [self.config.tmin_seconds, self.config.tmax_seconds],
            "sample_start": self.sample_start,
            "sample_stop": self.sample_stop,
            "sample_count": int(self.data.shape[0]),
            "sample_rate_hz": self.sample_rate_hz,
            "channel_names": self.channel_names,
            "start_timestamp": float(self.timestamps[0]),
            "end_timestamp": float(self.timestamps[-1]),
            "relative_start_seconds": float(self.relative_times[0]),
            "relative_end_seconds": float(self.relative_times[-1]),
            "condition": parsed.get("condition"),
            "trial": parsed.get("trial"),
            "training_label": training_label_from_marker(parsed),
        }
        if include_data_shape:
            payload["data_shape"] = list(self.model_input.shape)
            payload["data_layout"] = "channels_x_samples"
        return payload


@dataclass
class EpochAttempt:
    status: str
    reason: str
    marker: MarkerEvent
    epoch: ExtractedEpoch | None = None

    def payload(self, config: EpochingConfig) -> dict[str, Any]:
        parsed = self.marker.parsed_metadata(config.marker_prefix)
        return {
            "schema_version": 1,
            "status": self.status,
            "reason": self.reason,
            "marker": {
                "label": self.marker.label,
                "timestamp": self.marker.timestamp,
                "timebase": self.marker.timebase,
                "source": self.marker.source,
                "metadata": parsed,
            },
            "condition": parsed.get("condition"),
            "trial": parsed.get("trial"),
            "training_label": training_label_from_marker(parsed),
            "epoch_window_seconds": [config.tmin_seconds, config.tmax_seconds],
        }


@dataclass
class EegCsvBundle:
    timestamps: np.ndarray
    data: np.ndarray
    channel_names: list[str]
    sample_rate_hz: float
    timestamp_column: str


class RealtimeEpocher:
    """Hold marker events until the raw EEG buffer has enough post-stimulus samples."""

    def __init__(self, config: EpochingConfig) -> None:
        self.config = config
        self._pending: list[MarkerEvent] = []
        self._epoch_index = 0

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def oldest_pending_timestamp(self) -> float | None:
        return None if not self._pending else min(marker.timestamp for marker in self._pending)

    def add_marker(self, marker: MarkerEvent) -> bool:
        if not should_epoch_marker(marker, self.config):
            return False
        self._pending.append(marker)
        return True

    def reject_pending(self, reason: str) -> list[EpochAttempt]:
        rejected = [EpochAttempt("rejected", reason, marker) for marker in self._pending]
        self._pending = []
        return rejected

    def extract_ready(
        self,
        timestamps: np.ndarray,
        data: np.ndarray,
        sample_rate_hz: float,
        channel_names: list[str],
    ) -> tuple[list[ExtractedEpoch], list[EpochAttempt]]:
        ready: list[ExtractedEpoch] = []
        rejected: list[EpochAttempt] = []
        remaining: list[MarkerEvent] = []
        for marker in self._pending:
            attempt = extract_epoch_from_arrays(
                timestamps=timestamps,
                data=data,
                marker=marker,
                sample_rate_hz=sample_rate_hz,
                channel_names=channel_names,
                config=self.config,
                epoch_index=self._epoch_index + 1,
            )
            if attempt.status == "ready" and attempt.epoch is not None:
                ready.append(attempt.epoch)
                self._epoch_index += 1
            elif attempt.status == "pending":
                remaining.append(marker)
            else:
                rejected.append(attempt)
        self._pending = remaining
        return ready, rejected


def marker_matches(label: str, marker_prefix: str = DEFAULT_MARKER_PREFIX) -> bool:
    return label == marker_prefix or label.startswith(f"{marker_prefix}_")


def should_epoch_marker(marker: MarkerEvent, config: EpochingConfig) -> bool:
    if not marker_matches(marker.label, config.marker_prefix):
        return False
    parsed = marker.parsed_metadata(config.marker_prefix)
    trial = parsed.get("trial")
    if not config.include_practice_trials and isinstance(trial, int) and trial < 1:
        return False
    return True


def parse_marker_label(label: str, marker_prefix: str = DEFAULT_MARKER_PREFIX) -> dict[str, Any]:
    """Parse the scaffold's Go/No-go marker labels into training metadata."""
    metadata: dict[str, Any] = {"label": label}
    if not marker_matches(label, marker_prefix):
        return metadata

    remainder = label[len(marker_prefix) :].lstrip("_")
    if not remainder:
        return metadata

    parts = remainder.split("_")
    if parts and parts[0].lstrip("-").isdigit():
        metadata["trial"] = int(parts.pop(0))
    if parts:
        if len(parts) >= 2 and parts[0] == "no" and parts[1] == "go":
            metadata["condition"] = "no_go"
            parts = parts[2:]
        else:
            metadata["condition"] = parts.pop(0)
    if parts:
        metadata["shape"] = parts.pop(0)
    if parts:
        metadata["color"] = "_".join(parts)
    return metadata


def training_label_from_marker(metadata: dict[str, Any]) -> int:
    condition = str(metadata.get("condition", "")).lower()
    if condition in {"no_go", "nogo", "target", "oddball"}:
        return 1
    if condition in {"go", "non_target", "nontarget", "standard"}:
        return 0
    return -1


def expected_sample_count(sample_rate_hz: float, config: EpochingConfig) -> int:
    return int(round(config.duration_seconds * sample_rate_hz)) + 1


def extract_epoch_from_arrays(
    timestamps: np.ndarray,
    data: np.ndarray,
    marker: MarkerEvent,
    sample_rate_hz: float,
    channel_names: list[str],
    config: EpochingConfig,
    epoch_index: int = 1,
) -> EpochAttempt:
    ts = np.asarray(timestamps, dtype=float)
    eeg = np.asarray(data, dtype=float)
    if ts.ndim != 1 or eeg.ndim != 2:
        return EpochAttempt("rejected", "timestamps must be 1D and EEG data must be samples x channels", marker)
    if ts.shape[0] != eeg.shape[0]:
        return EpochAttempt("rejected", "timestamp and sample counts do not match", marker)
    if ts.size == 0:
        return EpochAttempt("pending", "no EEG samples available yet", marker)

    sample_count = expected_sample_count(sample_rate_hz, config)
    index_times, index_values = _unique_time_to_sample_index(ts)
    if index_times.size == 0:
        return EpochAttempt("pending", "no finite EEG timestamps available", marker)

    epoch_start_time = marker.timestamp + config.tmin_seconds
    epoch_end_time = marker.timestamp + config.tmax_seconds
    if epoch_start_time < index_times[0] - config.sample_tolerance_seconds:
        return EpochAttempt("rejected", "pre-stimulus samples are no longer in the EEG buffer", marker)
    if epoch_end_time > index_times[-1] + config.sample_tolerance_seconds:
        return EpochAttempt("pending", "waiting for post-stimulus EEG samples", marker)

    start_sample = int(round(float(np.interp(epoch_start_time, index_times, index_values))))
    stop_sample = start_sample + sample_count
    if start_sample < 0:
        return EpochAttempt("rejected", "epoch starts before EEG sample zero", marker)
    if stop_sample > eeg.shape[0]:
        return EpochAttempt("pending", "waiting for enough indexed EEG samples", marker)

    epoch_data = eeg[start_sample:stop_sample]
    epoch_timestamps = ts[start_sample:stop_sample]
    if epoch_data.shape[0] != sample_count:
        return EpochAttempt("pending", "epoch sample count is incomplete", marker)

    relative_times = epoch_timestamps - marker.timestamp
    epoch = ExtractedEpoch(
        epoch_index=epoch_index,
        marker=marker,
        data=epoch_data,
        timestamps=epoch_timestamps,
        relative_times=relative_times,
        sample_start=start_sample,
        sample_stop=stop_sample,
        sample_rate_hz=float(sample_rate_hz),
        channel_names=list(channel_names),
        config=config,
    )
    return EpochAttempt("ready", "epoch complete", marker, epoch)


def load_eeg_csv_for_epoching(
    raw_path: str | Path,
    metadata_path: str | Path | None = None,
    parameters_path: str | Path | None = None,
    timebase: str = "lsl",
) -> EegCsvBundle:
    import pandas as pd

    raw = Path(raw_path).expanduser().resolve()
    frame = pd.read_csv(raw)
    timestamp_column = "local_received_time" if timebase in {"local", "local_received", "monotonic"} else "lsl_timestamp"
    if timestamp_column not in frame:
        raise ValueError(f"EEG CSV must include {timestamp_column}")
    if "lsl_timestamp" not in frame:
        raise ValueError("EEG CSV must include lsl_timestamp")

    channel_columns = [column for column in frame.columns if column not in {"lsl_timestamp", "local_received_time"}]
    metadata = _load_json(metadata_path) if metadata_path else {}
    parameters = _load_json(parameters_path) if parameters_path else {}
    lsl_timestamps = frame["lsl_timestamp"].to_numpy(dtype=float)
    sample_rate = _infer_sample_rate(lsl_timestamps, metadata, parameters)
    channel_names = _infer_channel_names(channel_columns, parameters)
    return EegCsvBundle(
        timestamps=frame[timestamp_column].to_numpy(dtype=float),
        data=frame[channel_columns].to_numpy(dtype=float),
        channel_names=channel_names,
        sample_rate_hz=sample_rate,
        timestamp_column=timestamp_column,
    )


def load_markers_jsonl(path: str | Path, config: EpochingConfig) -> list[MarkerEvent]:
    markers: list[MarkerEvent] = []
    target = Path(path)
    if not target.exists():
        return markers
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            label = str(row.get("label", ""))
            if "lsl_timestamp" not in row:
                continue
            marker = MarkerEvent(label=label, timestamp=float(row["lsl_timestamp"]), timebase="lsl", source=str(target))
            if should_epoch_marker(marker, config):
                markers.append(marker)
    return markers


def load_events_jsonl(path: str | Path, config: EpochingConfig) -> list[MarkerEvent]:
    markers: list[MarkerEvent] = []
    target = Path(path)
    if not target.exists():
        return markers
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            label = str(row.get("label", ""))
            marker = MarkerEvent(
                label=label,
                timestamp=float(row["timestamp"]),
                timebase="local_received",
                source=str(target),
                metadata=dict(row.get("metadata") or {}),
            )
            if should_epoch_marker(marker, config):
                markers.append(marker)
    return markers


def load_stimulus_manifest_markers(path: str | Path, config: EpochingConfig) -> list[MarkerEvent]:
    target = Path(path)
    manifest = _load_json(target)
    if not manifest:
        return []
    markers = []
    for trial in manifest.get("trials", []):
        stimulus = dict(trial.get("stimulus") or {})
        condition = "no_go" if stimulus.get("is_no_go") else "go"
        shape = stimulus.get("shape", "unknown")
        color = stimulus.get("color", "unknown")
        trial_number = int(trial["trial"])
        label = f"{config.marker_prefix}_{trial_number}_{condition}_{shape}_{color}"
        marker = MarkerEvent(
            label=label,
            timestamp=float(trial["onset_monotonic"]),
            timebase="local_received",
            source=str(target),
            metadata={
                "trial": trial_number,
                "condition": condition,
                "stimulus_id": trial.get("stimulus_id"),
                "stimulus": stimulus,
                "response": trial.get("response", {}),
            },
        )
        if should_epoch_marker(marker, config):
            markers.append(marker)
    return markers


def extract_epochs_for_session(
    session_dir: str | Path,
    config: dict[str, Any],
    source: str = "auto",
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
    epoch_cfg = EpochingConfig.from_dict(config.get("realtime", {}).get("epoching", {}))
    selected_source, marker_path, markers = _load_session_markers(root, source, epoch_cfg)
    if selected_source == "markers_jsonl":
        timebase = "lsl"
    else:
        timebase = "local_received"
    epoch_cfg = replace(epoch_cfg, timebase=timebase)
    eeg = load_eeg_csv_for_epoching(
        root / "raw" / "eeg.csv",
        metadata_path=root / "raw" / "eeg_metadata.json",
        parameters_path=root / "parameters.json",
        timebase=timebase,
    )
    attempts = [
        extract_epoch_from_arrays(
            timestamps=eeg.timestamps,
            data=eeg.data,
            marker=marker,
            sample_rate_hz=eeg.sample_rate_hz,
            channel_names=eeg.channel_names,
            config=epoch_cfg,
            epoch_index=index,
        )
        for index, marker in enumerate(markers, start=1)
    ]
    epochs = [attempt.epoch for attempt in attempts if attempt.status == "ready" and attempt.epoch is not None]
    rejected = [attempt for attempt in attempts if attempt.status != "ready"]
    target_dir = Path(output_dir).expanduser().resolve() if output_dir else root / "realtime" / "epochs"
    return write_epoch_dataset(
        epochs=epochs,
        rejected=rejected,
        output_dir=target_dir,
        raw_path=root / "raw" / "eeg.csv",
        marker_source_path=marker_path,
        source=selected_source,
        timestamp_column=eeg.timestamp_column,
        config=epoch_cfg,
        channel_names=eeg.channel_names,
        sample_rate_hz=eeg.sample_rate_hz,
    )


def write_epoch_dataset(
    epochs: list[ExtractedEpoch],
    rejected: list[EpochAttempt],
    output_dir: str | Path,
    raw_path: str | Path,
    marker_source_path: str | Path | None,
    source: str,
    timestamp_column: str,
    config: EpochingConfig,
    channel_names: list[str],
    sample_rate_hz: float,
) -> dict[str, Any]:
    raw = Path(raw_path).expanduser().resolve()
    target = Path(output_dir).expanduser().resolve()
    if target == raw.parent or raw.parent in target.parents:
        raise ValueError("epoch outputs must be written outside the raw EEG directory")
    target.mkdir(parents=True, exist_ok=True)
    raw_hash_before = file_sha256(raw)

    sample_count = expected_sample_count(sample_rate_hz, config)
    if epochs:
        epoch_array = np.stack([epoch.model_input for epoch in epochs], axis=0)
        times = np.arange(sample_count, dtype=float) / sample_rate_hz + config.tmin_seconds
        epoch_timestamps = np.stack([epoch.timestamps.astype(float) for epoch in epochs], axis=0)
        labels = np.asarray([epoch.metadata_payload()["training_label"] for epoch in epochs], dtype=int)
        marker_timestamps = np.asarray([epoch.marker.timestamp for epoch in epochs], dtype=float)
        trials = np.asarray([epoch.metadata_payload().get("trial") or -1 for epoch in epochs], dtype=int)
        conditions = np.asarray([str(epoch.metadata_payload().get("condition") or "unknown") for epoch in epochs], dtype=object)
    else:
        epoch_array = np.empty((0, len(channel_names), sample_count), dtype=float)
        times = np.arange(sample_count, dtype=float) / sample_rate_hz + config.tmin_seconds
        epoch_timestamps = np.empty((0, sample_count), dtype=float)
        labels = np.empty((0,), dtype=int)
        marker_timestamps = np.empty((0,), dtype=float)
        trials = np.empty((0,), dtype=int)
        conditions = np.empty((0,), dtype=object)

    npz_path = target / "epochs.npz"
    tmp_npz = npz_path.with_suffix(npz_path.suffix + ".tmp")
    with tmp_npz.open("wb") as handle:
        np.savez_compressed(
            handle,
            X=epoch_array,
            y=labels,
            times=times,
            epoch_timestamps=epoch_timestamps,
            marker_timestamps=marker_timestamps,
            trials=trials,
            conditions=conditions,
            channel_names=np.asarray(channel_names, dtype=object),
            sample_rate_hz=np.asarray([sample_rate_hz], dtype=float),
        )
    tmp_npz.replace(npz_path)

    jsonl_path = target / "epochs.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for epoch in epochs:
            handle.write(json.dumps(epoch.metadata_payload(), sort_keys=True) + "\n")
        for attempt in rejected:
            handle.write(json.dumps(attempt.payload(config), sort_keys=True) + "\n")

    raw_hash_after = file_sha256(raw)
    marker_hash = file_sha256(marker_source_path) if marker_source_path and Path(marker_source_path).exists() else None
    manifest = {
        "schema_version": 1,
        "status": "ok",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "raw_file": str(raw),
        "raw_sha256": raw_hash_before,
        "raw_sha256_after_epoch_export": raw_hash_after,
        "raw_file_unchanged": raw_hash_before == raw_hash_after,
        "marker_source_file": None if marker_source_path is None else str(Path(marker_source_path).expanduser().resolve()),
        "marker_source_sha256": marker_hash,
        "timestamp_column": timestamp_column,
        "epoch_count": len(epochs),
        "rejected_count": len(rejected),
        "sample_rate_hz": sample_rate_hz,
        "channel_names": channel_names,
        "data_file": str(npz_path),
        "metadata_file": str(jsonl_path),
        "data_layout": "epochs_x_channels_x_samples",
        "data_key": "X",
        "label_key": "y",
        "relative_time_key": "times",
        "epoch_timestamp_key": "epoch_timestamps",
        "epoch_shape": list(epoch_array.shape),
        "epoching_config": asdict(config),
    }
    manifest_path = target / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
        handle.write("\n")
    if not manifest["raw_file_unchanged"]:
        raise RuntimeError("raw EEG file hash changed while exporting derived epochs")
    return manifest


def file_sha256(path: str | Path) -> str:
    target = Path(path)
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_session_markers(root: Path, source: str, config: EpochingConfig) -> tuple[str, Path | None, list[MarkerEvent]]:
    options: list[tuple[str, Path, Iterable[MarkerEvent]]]
    marker_jsonl = root / "realtime" / "markers.jsonl"
    events_jsonl = root / "events" / "events.jsonl"
    stimulus_manifest = root / "events" / "stimulus_manifest.json"
    if source == "markers_jsonl":
        return source, marker_jsonl, load_markers_jsonl(marker_jsonl, config)
    if source == "events_jsonl":
        return source, events_jsonl, load_events_jsonl(events_jsonl, config)
    if source == "stimulus_manifest":
        return source, stimulus_manifest, load_stimulus_manifest_markers(stimulus_manifest, config)
    if source != "auto":
        raise ValueError(f"unknown marker source '{source}'")

    options = [
        ("markers_jsonl", marker_jsonl, load_markers_jsonl(marker_jsonl, config)),
        ("stimulus_manifest", stimulus_manifest, load_stimulus_manifest_markers(stimulus_manifest, config)),
        ("events_jsonl", events_jsonl, load_events_jsonl(events_jsonl, config)),
    ]
    for name, path, markers in options:
        markers_list = list(markers)
        if markers_list:
            return name, path, markers_list
    return "auto", None, []


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


def _load_json(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    target = Path(path)
    if not target.exists():
        return {}
    with target.open("r", encoding="utf-8") as handle:
        return json.load(handle)
