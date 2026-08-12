"""LSL EEG recorder for configured EEG streams."""

from __future__ import annotations

import csv
import json
import os
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, monotonic_ns, sleep
from typing import Any

import numpy as np

from eegle.hardware.eeg_device import matching_eeg_streams
from eegle.hardware.profiles import mapped_channel_names
from eegle.lsl import inlet_time_correction, lsl_processing_flags


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
    timestamp_gap_count: int = 0
    largest_timestamp_gap_seconds: float = 0.0
    estimated_missing_samples: int = 0
    nonmonotonic_timestamp_count: int = 0
    minimum_free_bytes_observed: int | None = None
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
            "timestamp_gap_count": self.timestamp_gap_count,
            "largest_timestamp_gap_seconds": self.largest_timestamp_gap_seconds,
            "estimated_missing_samples": self.estimated_missing_samples,
            "nonmonotonic_timestamp_count": self.nonmonotonic_timestamp_count,
            "minimum_free_bytes_observed": self.minimum_free_bytes_observed,
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
        self.maximum_timestamp_gap_seconds = max(
            0.01,
            float(eeg_config.get("maximum_timestamp_gap_seconds", 0.1)),
        )
        self.abort_on_timestamp_gap = bool(eeg_config.get("abort_on_timestamp_gap", False))
        self.minimum_free_bytes = max(
            0,
            int(eeg_config.get("minimum_free_bytes_during_recording", 512 * 1024 * 1024)),
        )
        self.disk_check_interval_seconds = max(
            1.0,
            float(eeg_config.get("disk_check_interval_seconds", 5.0)),
        )
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
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                self._summary.status = "failed"
                self._summary.error = "EEG recorder thread did not stop within 10 seconds"
                self._summary.notes.append("raw CSV writer may not have closed cleanly")
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

        inlet: Any | None = None
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
                processing_flags=_eeg_inlet_processing_flags(pylsl, self.eeg_config),
            )
            inlet.open_stream(timeout=self.stream_timeout_seconds)
            raw_channel_labels = _channel_labels(info) or _default_channel_labels(info.channel_count())
            channel_labels, mapping_source = mapped_channel_names(raw_channel_labels, self.eeg_config)
            source_preserving = _source_preserving_recording(self.eeg_config)
            time_correction = inlet_time_correction(inlet)
            stream = dict(stream or {})
            stream.update(
                {
                    "channel_names": channel_labels,
                    "original_channel_names": raw_channel_labels,
                    "channel_mapping_source": mapping_source,
                    "channel_value_order_changed": False,
                    "lsl_processing": [] if source_preserving else ["clocksync", "dejitter", "monotonize"],
                    "recording_timestamp_mode": "source_preserving" if source_preserving else "legacy_processed",
                    "initial_time_correction_seconds": time_correction,
                    "amplitude_transformations": [],
                }
            )
            self._summary.stream = stream
            self._started_at = monotonic()
            self._summary.status = "recording"
            self._ready.set()

            with self.raw_file.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                timestamp_columns = ["lsl_timestamp", "local_received_time"]
                if source_preserving:
                    timestamp_columns.extend(["source_lsl_timestamp", "lsl_time_correction_seconds"])
                writer.writerow([*timestamp_columns, *channel_labels])
                last_correction_refresh = monotonic()
                last_disk_check = monotonic()
                last_source_timestamp: float | None = None
                expected_sample_rate = float(
                    stream.get("nominal_srate")
                    or self.eeg_config.get("expected_sample_rate_hz")
                    or 0.0
                )
                self._check_disk_space()
                while not self._stop.is_set():
                    samples, timestamps = inlet.pull_chunk(timeout=0.2, max_samples=64)
                    if not samples:
                        continue
                    if len(samples) != len(timestamps):
                        raise RuntimeError(
                            f"LSL EEG chunk sample/timestamp count mismatch: {len(samples)} samples, "
                            f"{len(timestamps)} timestamps"
                        )
                    received_at = monotonic()
                    if received_at - last_disk_check >= self.disk_check_interval_seconds:
                        self._check_disk_space()
                        last_disk_check = received_at
                    if source_preserving and received_at - last_correction_refresh >= 10.0:
                        refreshed = inlet_time_correction(inlet, timeout=0.05)
                        if refreshed is not None:
                            time_correction = refreshed
                        last_correction_refresh = received_at
                    received_times = _local_received_times_for_chunk(timestamps, received_at)
                    for sample, source_timestamp, received_time in zip(samples, timestamps, received_times):
                        if len(sample) != len(channel_labels):
                            raise RuntimeError(
                                f"LSL EEG sample width changed: expected {len(channel_labels)} values, got {len(sample)}"
                            )
                        source_timestamp = float(source_timestamp)
                        if last_source_timestamp is not None:
                            gap = source_timestamp - last_source_timestamp
                            if gap <= 0:
                                self._summary.nonmonotonic_timestamp_count += 1
                                raise RuntimeError(
                                    "LSL EEG source timestamps are not strictly increasing "
                                    f"({last_source_timestamp:.9f} then {source_timestamp:.9f})"
                                )
                            if gap > self.maximum_timestamp_gap_seconds:
                                self._summary.timestamp_gap_count += 1
                                self._summary.largest_timestamp_gap_seconds = max(
                                    self._summary.largest_timestamp_gap_seconds,
                                    gap,
                                )
                                if expected_sample_rate > 0:
                                    self._summary.estimated_missing_samples += max(
                                        0,
                                        round(gap * expected_sample_rate) - 1,
                                    )
                                if self.abort_on_timestamp_gap:
                                    raise RuntimeError(
                                        "LSL EEG timestamp gap exceeded the acquisition limit: "
                                        f"{gap:.6f}s > {self.maximum_timestamp_gap_seconds:.6f}s"
                                    )
                        last_source_timestamp = source_timestamp
                        row, corrected_timestamp = _recorded_eeg_row(
                            sample,
                            source_timestamp=source_timestamp,
                            received_time=float(received_time),
                            time_correction=time_correction,
                            source_preserving=source_preserving,
                        )
                        writer.writerow(row)
                        self._summary.sample_count += 1
                        if self._summary.first_lsl_timestamp is None:
                            self._summary.first_lsl_timestamp = corrected_timestamp
                        self._summary.last_lsl_timestamp = corrected_timestamp
                    handle.flush()
            self._summary.status = "stopped"
        except Exception as exc:
            self._summary.status = "failed"
            self._summary.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            if inlet is not None:
                try:
                    inlet.close_stream()
                except Exception as exc:
                    self._summary.notes.append(f"LSL inlet cleanup failed: {type(exc).__name__}: {exc}")
            self._write_metadata()

    def _check_disk_space(self) -> None:
        usage = shutil.disk_usage(self.raw_file.parent)
        if (
            self._summary.minimum_free_bytes_observed is None
            or usage.free < self._summary.minimum_free_bytes_observed
        ):
            self._summary.minimum_free_bytes_observed = int(usage.free)
        if usage.free < self.minimum_free_bytes:
            raise OSError(
                "recording storage fell below the live safety reserve: "
                f"{usage.free} free bytes < {self.minimum_free_bytes} required bytes"
            )

    def _write_metadata(self) -> None:
        payload = self._summary.as_dict()
        payload["started_at_monotonic"] = self._started_at
        payload["finished_at_monotonic"] = self._finished_at
        payload["eeg_config"] = self.eeg_config
        payload["raw_sample_contract"] = {
            "amplitude_samples_modified": False,
            "amplitude_transformations": [],
            "channel_value_order_modified": False,
            "recording_timestamp_mode": (
                "source_preserving" if _source_preserving_recording(self.eeg_config) else "legacy_processed"
            ),
            "source_timestamp_retained": _source_preserving_recording(self.eeg_config),
            "initial_time_correction_available": (
                (self._summary.stream or {}).get("initial_time_correction_seconds") is not None
            ),
            "corrected_timestamp_formula": "source_lsl_timestamp + lsl_time_correction_seconds",
            "filtering": "none",
            "resampling": "none",
            "rereferencing": "none",
            "artifact_rejection": "none",
        }
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.metadata_file.with_name(
            f".{self.metadata_file.name}.{os.getpid()}.{monotonic_ns()}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            last_error: OSError | None = None
            for delay in (0.0, 0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8):
                if delay:
                    sleep(delay)
                try:
                    temporary.replace(self.metadata_file)
                    return
                except OSError as exc:
                    if not isinstance(exc, PermissionError) and getattr(exc, "winerror", None) not in {5, 32, 33}:
                        raise
                    last_error = exc
            if last_error is not None:
                raise last_error
        finally:
            temporary.unlink(missing_ok=True)


def _local_received_times_for_chunk(timestamps: list[float], received_at: float) -> list[float]:
    if not timestamps:
        return []
    try:
        last_timestamp = float(timestamps[-1])
    except Exception:
        return [float(received_at)] * len(timestamps)
    received = []
    previous = float("-inf")
    for timestamp in timestamps:
        try:
            estimate = float(received_at) - max(0.0, last_timestamp - float(timestamp))
        except Exception:
            estimate = float(received_at)
        if estimate <= previous:
            estimate = previous
        received.append(estimate)
        previous = estimate
    return received


def _recorded_eeg_row(
    sample: list[float],
    *,
    source_timestamp: float,
    received_time: float,
    time_correction: float | None,
    source_preserving: bool,
) -> tuple[list[Any], float]:
    corrected_timestamp = float(source_timestamp)
    if source_preserving and time_correction is not None:
        corrected_timestamp += float(time_correction)
    if source_preserving:
        return (
            [
                f"{corrected_timestamp:.9f}",
                f"{received_time:.9f}",
                f"{float(source_timestamp):.9f}",
                "nan" if time_correction is None else f"{float(time_correction):.9f}",
                *sample,
            ],
            corrected_timestamp,
        )
    return [f"{corrected_timestamp:.9f}", f"{received_time:.9f}", *sample], corrected_timestamp


def probe_eeg_stream(eeg_config: dict[str, Any], seconds: float = 2.0, timeout: float = 5.0) -> dict[str, Any]:
    """Connect to a matching EEG stream and count samples for a short period."""
    try:
        import pylsl
    except Exception as exc:
        return {"status": "failed", "error": f"pylsl import failed: {type(exc).__name__}: {exc}"}

    info, stream = _select_lsl_info(pylsl, eeg_config, timeout)
    if info is None:
        return {"status": "missing", "error": "no matching LSL EEG stream found"}

    inlet: Any | None = None
    try:
        inlet = pylsl.StreamInlet(
            info,
            max_buflen=10,
            max_chunklen=32,
            recover=True,
            processing_flags=_eeg_inlet_processing_flags(pylsl, eeg_config),
        )
        inlet.open_stream(timeout=timeout)
        time_correction = inlet_time_correction(inlet)
        deadline = monotonic() + seconds
        sample_count = 0
        first_ts = None
        last_ts = None
        captured_samples: list[list[float]] = []
        captured_timestamps: list[float] = []
        while monotonic() < deadline:
            samples, timestamps = inlet.pull_chunk(timeout=0.2, max_samples=64)
            sample_count += len(samples)
            captured_samples.extend(samples)
            captured_timestamps.extend(float(value) for value in timestamps)
            if timestamps:
                first_ts = timestamps[0] if first_ts is None else first_ts
                last_ts = timestamps[-1]
        raw_channel_names = _channel_labels(info) or _default_channel_labels(info.channel_count())
        channel_names, mapping_source = mapped_channel_names(raw_channel_names, eeg_config)
        quality = _eeg_probe_quality(
            captured_samples,
            captured_timestamps,
            channel_names,
            float(info.nominal_srate() or eeg_config.get("expected_sample_rate_hz", 0) or 0),
            eeg_config,
        )
        return {
            "status": "ok" if sample_count > 0 else "warn",
            "stream": stream,
            "sample_count": sample_count,
            "first_lsl_timestamp": first_ts,
            "last_lsl_timestamp": last_ts,
            "probe_seconds": seconds,
            "original_channel_names": raw_channel_names,
            "mapped_channel_names": channel_names,
            "channel_mapping_source": mapping_source,
            "initial_time_correction_seconds": time_correction,
            "recording_timestamp_mode": (
                "source_preserving" if _source_preserving_recording(eeg_config) else "legacy_processed"
            ),
            "quality": quality,
        }
    except Exception as exc:
        return {"status": "failed", "stream": stream, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if inlet is not None:
            try:
                inlet.close_stream()
            except Exception:
                pass


def _eeg_probe_quality(
    samples: list[list[float]],
    timestamps: list[float],
    channel_names: list[str],
    sample_rate_hz: float,
    eeg_config: dict[str, Any],
) -> dict[str, Any]:
    quality_config = dict(eeg_config.get("quality_check", {}) or {})
    minimum_std = float(quality_config.get("minimum_channel_std", 1e-12))
    maximum_abs_value = quality_config.get("maximum_absolute_value")
    maximum_abs = None if maximum_abs_value is None else float(maximum_abs_value)
    signal_units = str(eeg_config.get("lsl_signal_units") or "native_lsl_units")
    mains_hz = float(quality_config.get("line_noise_hz", 60.0))
    line_ratio_warning = float(quality_config.get("line_noise_ratio_warning", 0.25))
    valid_rows = [row for row in samples if len(row) >= len(channel_names)]
    values = np.asarray([row[: len(channel_names)] for row in valid_rows], dtype=float) if valid_rows else np.empty((0, len(channel_names)))
    timestamp_values = np.asarray(timestamps, dtype=float)
    timestamp_differences = np.diff(timestamp_values) if timestamp_values.size > 1 else np.asarray([], dtype=float)
    channel_results = []
    for index, name in enumerate(channel_names):
        column = values[:, index] if values.size else np.asarray([], dtype=float)
        finite = np.isfinite(column)
        finite_values = column[finite]
        finite_fraction = float(np.mean(finite)) if column.size else 0.0
        std = float(np.std(finite_values)) if finite_values.size else None
        peak_to_peak = float(np.ptp(finite_values)) if finite_values.size else None
        max_abs = float(np.max(np.abs(finite_values))) if finite_values.size else None
        line_ratio = _line_noise_ratio(finite_values, sample_rate_hz, mains_hz)
        flat = std is not None and std < minimum_std
        extreme = maximum_abs is not None and max_abs is not None and max_abs > maximum_abs
        warnings = []
        if finite_fraction < 1.0:
            warnings.append("non_finite_samples")
        if flat:
            warnings.append("flat_channel")
        if extreme:
            warnings.append("extreme_amplitude")
        if line_ratio is not None and line_ratio > line_ratio_warning:
            warnings.append("line_noise")
        channel_results.append(
            {
                "channel_index": index + 1,
                "channel_name": name,
                "finite_sample_fraction": finite_fraction,
                "standard_deviation_native_units": std,
                "peak_to_peak_native_units": peak_to_peak,
                "maximum_absolute_native_units": max_abs,
                "line_noise_ratio": line_ratio,
                "flatline": flat,
                "extreme_amplitude": extreme,
                "status": "warning" if warnings else "good",
                "warnings": warnings,
            }
        )
    return {
        "sample_count": int(values.shape[0]),
        "channel_count": len(channel_names),
        "sample_rate_hz": sample_rate_hz,
        "signal_units": signal_units,
        "timestamps_finite": bool(timestamp_values.size and np.all(np.isfinite(timestamp_values))),
        "timestamps_strictly_increasing": bool(timestamp_differences.size and np.all(timestamp_differences > 0)),
        "median_timestamp_step_seconds": float(np.median(timestamp_differences)) if timestamp_differences.size else None,
        "maximum_timestamp_gap_seconds": float(np.max(timestamp_differences)) if timestamp_differences.size else None,
        "channels": channel_results,
        "warning_channels": [row["channel_name"] for row in channel_results if row["status"] != "good"],
    }


def _line_noise_ratio(values: np.ndarray, sample_rate_hz: float, mains_hz: float) -> float | None:
    if values.size < 16 or sample_rate_hz <= 0 or mains_hz <= 0 or mains_hz >= sample_rate_hz / 2:
        return None
    centered = values - float(np.mean(values))
    power = np.abs(np.fft.rfft(centered)) ** 2
    frequencies = np.fft.rfftfreq(centered.size, d=1.0 / sample_rate_hz)
    total_mask = (frequencies >= 1.0) & (frequencies <= min(100.0, sample_rate_hz / 2))
    line_mask = np.abs(frequencies - mains_hz) <= 1.0
    total = float(np.sum(power[total_mask]))
    return None if total <= 0 else float(np.sum(power[line_mask])) / total


def _source_preserving_recording(eeg_config: dict[str, Any]) -> bool:
    return str(eeg_config.get("recording_lsl_processing", "legacy_processed")) == "source_preserving"


def _eeg_inlet_processing_flags(pylsl: Any, eeg_config: dict[str, Any]) -> int:
    if _source_preserving_recording(eeg_config):
        return int(getattr(pylsl, "proc_none", 0))
    return lsl_processing_flags(pylsl, dejitter=True)


def _select_lsl_info(pylsl: Any, eeg_config: dict[str, Any], timeout: float) -> tuple[Any | None, dict[str, Any] | None]:
    infos = pylsl.resolve_streams(wait_time=timeout)
    stream_infos = [_stream_dict(info) for info in infos]
    matches = matching_eeg_streams(stream_infos, eeg_config)
    if len(matches) == 1:
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
        "hostname": getattr(info, "hostname", lambda: "")(),
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
