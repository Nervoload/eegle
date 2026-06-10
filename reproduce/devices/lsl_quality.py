"""Recorder for Enobio/NIC2 Quality contact streams."""

from __future__ import annotations

import csv
import json
import threading
from pathlib import Path
from time import monotonic
from typing import Any

from reproduce.devices.lsl_eeg import EegRecorderSummary, _channel_labels, _default_channel_labels, _stream_dict
from reproduce.hardware.enobio import mapped_channel_names
from reproduce.lsl import inlet_time_correction, lsl_processing_flags


class LslQualityRecorder:
    """Record the low-rate Enobio Quality stream alongside EEG."""

    def __init__(
        self,
        quality_config: dict[str, Any],
        eeg_config: dict[str, Any],
        raw_file: str | Path,
        metadata_file: str | Path,
        stream_timeout_seconds: float = 5.0,
    ) -> None:
        self.quality_config = quality_config
        self.eeg_config = eeg_config
        self.raw_file = Path(raw_file)
        self.metadata_file = Path(metadata_file)
        self.stream_timeout_seconds = stream_timeout_seconds
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._summary = EegRecorderSummary("initialized", str(self.raw_file), str(self.metadata_file))

    def start(self) -> None:
        self.raw_file.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._record, name="lsl-quality-recorder", daemon=True)
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
        if self._summary.first_lsl_timestamp is not None and self._summary.last_lsl_timestamp is not None:
            self._summary.duration_seconds = self._summary.last_lsl_timestamp - self._summary.first_lsl_timestamp
        self._write_metadata()
        return self._summary.as_dict()

    def _record(self) -> None:
        try:
            import pylsl

            info, stream = _select_quality_info(pylsl, self.quality_config, self.stream_timeout_seconds)
            if info is None:
                raise RuntimeError("no matching LSL Quality stream found")
            inlet = pylsl.StreamInlet(
                info,
                max_buflen=300,
                max_chunklen=32,
                recover=True,
                processing_flags=lsl_processing_flags(pylsl, dejitter=True),
            )
            inlet.open_stream(timeout=self.stream_timeout_seconds)
            raw_labels = _channel_labels(info) or _default_channel_labels(info.channel_count())
            labels, mapping_source = mapped_channel_names(raw_labels, self.eeg_config)
            self._summary.stream = {
                **stream,
                "channel_names": labels,
                "channel_mapping_source": mapping_source,
                "value_semantics": self.quality_config.get("value_semantics", "contact_quality_or_impedance_proxy"),
                "initial_time_correction_seconds": inlet_time_correction(inlet),
            }
            self._summary.status = "recording"
            self._ready.set()
            with self.raw_file.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["lsl_timestamp", "local_received_time", *labels])
                while not self._stop.is_set():
                    samples, timestamps = inlet.pull_chunk(timeout=0.2, max_samples=64)
                    received_at = monotonic()
                    for sample, timestamp in zip(samples, timestamps):
                        writer.writerow([f"{timestamp:.9f}", f"{received_at:.9f}", *sample])
                        self._summary.sample_count += 1
                        self._summary.first_lsl_timestamp = (
                            float(timestamp) if self._summary.first_lsl_timestamp is None else self._summary.first_lsl_timestamp
                        )
                        self._summary.last_lsl_timestamp = float(timestamp)
                    if samples:
                        handle.flush()
            inlet.close_stream()
            self._summary.status = "stopped"
        except Exception as exc:
            self._summary.status = "failed"
            self._summary.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            self._write_metadata()

    def _write_metadata(self) -> None:
        payload = self._summary.as_dict()
        payload["quality_config"] = self.quality_config
        self.metadata_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _select_quality_info(pylsl: Any, config: dict[str, Any], timeout: float) -> tuple[Any | None, dict[str, Any] | None]:
    expected_type = str(config.get("lsl_stream_type", "Quality")).lower()
    patterns = [str(value).lower() for value in config.get("lsl_name_patterns", ["quality"])]
    matches = []
    for info in pylsl.resolve_streams(wait_time=timeout):
        stream = _stream_dict(info)
        name = str(stream.get("name", "")).lower()
        if str(stream.get("type", "")).lower() == expected_type and (not patterns or any(value in name for value in patterns)):
            matches.append((info, stream))
    if not matches and bool(config.get("allow_type_only_fallback", True)):
        matches = [
            (info, _stream_dict(info))
            for info in pylsl.resolve_streams(wait_time=0.1)
            if str(info.type()).lower() == expected_type
        ]
    return matches[0] if len(matches) == 1 else (None, None)
