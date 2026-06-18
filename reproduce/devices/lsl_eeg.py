"""LSL EEG recorder for Enobio/NIC2 streams."""

from __future__ import annotations

import csv
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

from reproduce.hardware.eeg_device import matching_eeg_streams
from reproduce.hardware.enobio import mapped_channel_names
from reproduce.lsl import inlet_time_correction, lsl_processing_flags


@dataclass
class EegRecorderSummary:
    status: str
    raw_file: str
    metadata_file: str
    stream: dict[str, Any] | None = None
    sample_count: int = 0
    first_lsl_timestamp: float | None = None
    last_lsl_timestamp: float | None = None
    duration_seconds: float | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "raw_file": self.raw_file,
            "metadata_file": self.metadata_file,
            "stream": self.stream,
            "sample_count": self.sample_count,
            "first_lsl_timestamp": self.first_lsl_timestamp,
            "last_lsl_timestamp": self.last_lsl_timestamp,
            "duration_seconds": self.duration_seconds,
            "error": self.error,
            "notes": self.notes,
        }


class LslEegRecorder:
    """Record a single LSL EEG stream to CSV while a task runs."""

    def __init__(
        self,
        eeg_config: dict[str, Any],
        raw_file: str | Path,
        metadata_file: str | Path,
        stream_timeout_seconds: float = 5.0,
    ) -> None:
        self.eeg_config = eeg_config
        self.raw_file = Path(raw_file)
        self.metadata_file = Path(metadata_file)
        self.stream_timeout_seconds = stream_timeout_seconds
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._summary = EegRecorderSummary(
            status="initialized",
            raw_file=str(self.raw_file),
            metadata_file=str(self.metadata_file),
        )
        self._started_at: float | None = None
        self._finished_at: float | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("EEG recorder is already running")
        self.raw_file.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._record, name="lsl-eeg-recorder", daemon=True)
        self._thread.start()

    def wait_until_ready(self, timeout: float = 10.0) -> bool:
        return self._ready.wait(timeout=timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def snapshot(self) -> dict[str, Any]:
        return self._summary.as_dict()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._finished_at = monotonic()
        if self._summary.first_lsl_timestamp is not None and self._summary.last_lsl_timestamp is not None:
            self._summary.duration_seconds = self._summary.last_lsl_timestamp - self._summary.first_lsl_timestamp
        self._write_metadata()
        return self._summary.as_dict()

    def _record(self) -> None:
        try:
            import pylsl
        except Exception as exc:
            self._summary.status = "failed"
            self._summary.error = f"pylsl import failed: {type(exc).__name__}: {exc}"
            self._ready.set()
            self._write_metadata()
            return

        try:
            info, stream = _select_lsl_info(pylsl, self.eeg_config, self.stream_timeout_seconds)
            if info is None:
                self._summary.status = "failed"
                self._summary.error = "no matching LSL EEG stream found"
                self._ready.set()
                self._write_metadata()
                return

            inlet = pylsl.StreamInlet(
                info,
                max_buflen=60,
                max_chunklen=32,
                recover=True,
                processing_flags=lsl_processing_flags(pylsl, dejitter=True),
            )
            inlet.open_stream(timeout=self.stream_timeout_seconds)
            raw_channel_labels = _channel_labels(info) or _default_channel_labels(info.channel_count())
            channel_labels, mapping_source = mapped_channel_names(raw_channel_labels, self.eeg_config)
            stream = dict(stream or {})
            stream.update(
                {
                    "channel_names": channel_labels,
                    "channel_mapping_source": mapping_source,
                    "lsl_processing": ["clocksync", "dejitter", "monotonize"],
                    "initial_time_correction_seconds": inlet_time_correction(inlet),
                }
            )
            self._summary.stream = stream
            self._started_at = monotonic()
            self._summary.status = "recording"
            self._ready.set()

            with self.raw_file.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["lsl_timestamp", "local_received_time", *channel_labels])
                while not self._stop.is_set():
                    samples, timestamps = inlet.pull_chunk(timeout=0.2, max_samples=64)
                    if not samples:
                        continue
                    received_at = monotonic()
                    for sample, timestamp in zip(samples, timestamps):
                        writer.writerow([f"{timestamp:.9f}", f"{received_at:.9f}", *sample])
                        self._summary.sample_count += 1
                        if self._summary.first_lsl_timestamp is None:
                            self._summary.first_lsl_timestamp = float(timestamp)
                        self._summary.last_lsl_timestamp = float(timestamp)
                    handle.flush()
            self._summary.status = "stopped"
            inlet.close_stream()
        except Exception as exc:
            self._summary.status = "failed"
            self._summary.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            self._write_metadata()

    def _write_metadata(self) -> None:
        payload = self._summary.as_dict()
        payload["started_at_monotonic"] = self._started_at
        payload["finished_at_monotonic"] = self._finished_at
        payload["eeg_config"] = self.eeg_config
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        with self.metadata_file.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")


def probe_eeg_stream(eeg_config: dict[str, Any], seconds: float = 2.0, timeout: float = 5.0) -> dict[str, Any]:
    """Connect to a matching EEG stream and count samples for a short period."""
    try:
        import pylsl
    except Exception as exc:
        return {"status": "failed", "error": f"pylsl import failed: {type(exc).__name__}: {exc}"}

    info, stream = _select_lsl_info(pylsl, eeg_config, timeout)
    if info is None:
        return {"status": "missing", "error": "no matching LSL EEG stream found"}

    try:
        inlet = pylsl.StreamInlet(
            info,
            max_buflen=10,
            max_chunklen=32,
            recover=True,
            processing_flags=lsl_processing_flags(pylsl, dejitter=True),
        )
        inlet.open_stream(timeout=timeout)
        deadline = monotonic() + seconds
        sample_count = 0
        first_ts = None
        last_ts = None
        while monotonic() < deadline:
            samples, timestamps = inlet.pull_chunk(timeout=0.2, max_samples=64)
            sample_count += len(samples)
            if timestamps:
                first_ts = timestamps[0] if first_ts is None else first_ts
                last_ts = timestamps[-1]
        inlet.close_stream()
        return {
            "status": "ok" if sample_count > 0 else "warn",
            "stream": stream,
            "sample_count": sample_count,
            "first_lsl_timestamp": first_ts,
            "last_lsl_timestamp": last_ts,
            "probe_seconds": seconds,
        }
    except Exception as exc:
        return {"status": "failed", "stream": stream, "error": f"{type(exc).__name__}: {exc}"}


def _select_lsl_info(pylsl: Any, eeg_config: dict[str, Any], timeout: float) -> tuple[Any | None, dict[str, Any] | None]:
    infos = pylsl.resolve_streams(wait_time=timeout)
    stream_infos = [_stream_dict(info) for info in infos]
    matches = matching_eeg_streams(stream_infos, eeg_config)
    if matches:
        for match in matches:
            for info, stream in zip(infos, stream_infos):
                if stream == match:
                    return info, stream

    if bool(eeg_config.get("allow_type_only_fallback", False)):
        expected_type = str(eeg_config.get("lsl_stream_type", "EEG")).lower()
        type_matches = [
            (info, stream)
            for info, stream in zip(infos, stream_infos)
            if str(stream.get("type", "")).lower() == expected_type
        ]
        if len(type_matches) == 1:
            return type_matches[0]
    return None, None


def _stream_dict(info: Any) -> dict[str, Any]:
    return {
        "name": info.name(),
        "type": info.type(),
        "channel_count": info.channel_count(),
        "nominal_srate": info.nominal_srate(),
        "source_id": info.source_id(),
    }


def _channel_labels(info: Any) -> list[str]:
    labels: list[str] = []
    try:
        channel = info.desc().child("channels").child("channel")
        for _ in range(info.channel_count()):
            label = channel.child_value("label")
            labels.append(label or f"ch_{len(labels) + 1:03d}")
            channel = channel.next_sibling()
    except Exception:
        return []
    return labels


def _default_channel_labels(channel_count: int) -> list[str]:
    return [f"ch_{idx + 1:03d}" for idx in range(channel_count)]
