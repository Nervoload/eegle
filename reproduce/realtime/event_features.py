"""Observe-only staged EEG features shared by live processing and causal replay."""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Iterator

import numpy as np

from reproduce.realtime.buffer import RingBuffer
from reproduce.realtime.epoching import MarkerEvent, parse_marker_label


FEATURE_SCHEMA_VERSION = "inhibition8.features.v1"
DEFAULT_CHANNELS = ("Fz", "Cz", "Pz", "C3", "C4", "P3", "P4", "Oz")
ERP_BASELINE_WINDOW = (-0.2, 0.0)
CAPTURE_MAGIC = b"CLRE1\n"


@dataclass(frozen=True)
class FeatureDefinition:
    name: str
    stage: str
    window: tuple[float, float]
    preferred_channels: tuple[str, ...]
    minimum_channels: int
    filter_profile: str
    optional_channels: tuple[str, ...] = ()
    fallback_channels: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageDefinition:
    name: str
    available_after_seconds: float
    feature_names: tuple[str, ...]


FEATURE_DEFINITIONS: dict[str, FeatureDefinition] = {
    "readiness_alpha": FeatureDefinition(
        "readiness_alpha", "prestim_state", (-1.0, -0.2), ("Pz", "P3", "P4", "Oz"), 2, "alpha_individual"
    ),
    "early_theta": FeatureDefinition(
        "early_theta", "n2_theta", (0.10, 0.35), ("Fz", "Cz"), 2, "theta_4_8", ("C3", "C4")
    ),
    "n2": FeatureDefinition("n2", "n2_theta", (0.20, 0.35), ("Fz", "Cz"), 2, "erp_0p5_30"),
    "p3": FeatureDefinition("p3", "p3", (0.30, 0.60), ("Pz", "P3", "P4"), 2, "erp_0p5_30", fallback_channels=("Cz",)),
    "alpha_erd": FeatureDefinition("alpha_erd", "alpha_erd", (0.20, 0.80), ("Pz", "P3", "P4", "Oz"), 2, "alpha_individual"),
}

STAGE_DEFINITIONS = (
    StageDefinition("prestim_state", 0.0, ("readiness_alpha",)),
    StageDefinition("n2_theta", 0.35, ("early_theta", "n2")),
    StageDefinition("p3", 0.60, ("p3",)),
    StageDefinition("alpha_erd", 0.80, ("alpha_erd",)),
)


@dataclass
class EventState:
    event_id: str
    marker: MarkerEvent
    metadata: dict[str, Any]
    completed_stages: set[str] = field(default_factory=set)
    cumulative_features: dict[str, float | None] = field(default_factory=dict)
    cumulative_validity: dict[str, bool] = field(default_factory=dict)


class FeatureRegistry:
    """Resolve declared feature ROIs without inventing missing electrodes."""

    def __init__(self, channel_names: list[str]) -> None:
        self.channel_names = list(channel_names)
        self.resolution: dict[str, list[str]] = {}
        self.missing: dict[str, list[str]] = {}
        self.valid: dict[str, bool] = {}
        for name, definition in FEATURE_DEFINITIONS.items():
            preferred = [channel for channel in definition.preferred_channels if channel in self.channel_names]
            resolved = list(preferred)
            if len(resolved) < definition.minimum_channels:
                for channel in definition.fallback_channels:
                    if channel in self.channel_names and channel not in resolved:
                        resolved.append(channel)
                    if len(resolved) >= definition.minimum_channels:
                        break
            declared = [*definition.preferred_channels, *definition.fallback_channels]
            self.resolution[name] = resolved
            self.missing[name] = [channel for channel in declared if channel not in self.channel_names]
            self.valid[name] = len(resolved) >= definition.minimum_channels

    def indices(self, feature_name: str) -> list[int]:
        return [self.channel_names.index(channel) for channel in self.resolution[feature_name]]

    def payload(self) -> dict[str, list[str]]:
        return {
            "readiness_alpha": self.resolution["readiness_alpha"],
            "n2_theta": self.resolution["n2"],
            "early_theta_optional": [
                channel for channel in FEATURE_DEFINITIONS["early_theta"].optional_channels if channel in self.channel_names
            ],
            "p3": self.resolution["p3"],
            "alpha_erd": self.resolution["alpha_erd"],
        }

    def declarations_payload(self) -> dict[str, Any]:
        return {
            name: {
                "stage": definition.stage,
                "window_seconds": list(definition.window),
                "preferred_channels": list(definition.preferred_channels),
                "fallback_channels": list(definition.fallback_channels),
                "optional_channels": list(definition.optional_channels),
                "minimum_channels": definition.minimum_channels,
                "filter_profile": definition.filter_profile,
            }
            for name, definition in FEATURE_DEFINITIONS.items()
        }


class EventQualityGate:
    """Assess raw pre-reference EEG and invalidate contaminated fixed references."""

    def __init__(self, channel_names: list[str], fixed_reference_channels: list[str], config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.channel_names = list(channel_names)
        self.fixed_reference_channels = [channel for channel in fixed_reference_channels if channel in self.channel_names]
        self.reference_indices = [self.channel_names.index(channel) for channel in self.fixed_reference_channels]
        self.minimum_reference_channels = int(cfg.get("minimum_reference_channels", 4))
        self.max_abs_uv = float(cfg.get("max_abs_uv", 250.0))
        self.max_peak_to_peak_uv = float(cfg.get("max_peak_to_peak_uv", 400.0))
        self.min_std_uv = float(cfg.get("min_std_uv", 0.01))
        self.max_drift_uv = float(cfg.get("max_drift_uv", 150.0))
        self.max_broadband_diff_std_uv = float(cfg.get("max_broadband_diff_std_uv", 100.0))
        self.max_nan_fraction = float(cfg.get("max_nan_fraction", 0.0))

    def assess(self, data: np.ndarray) -> dict[str, Any]:
        values = np.asarray(data, dtype=float)
        flags: dict[str, list[str]] = {}
        if values.ndim != 2 or values.shape[0] < 2:
            return {
                "valid": False,
                "reference_valid": False,
                "quality_flags": {"global": ["insufficient_samples"]},
                "invalid_reference_channels": list(self.fixed_reference_channels),
            }
        for index, channel in enumerate(self.channel_names):
            series = values[:, index]
            finite = series[np.isfinite(series)]
            reasons: list[str] = []
            nan_fraction = float(np.mean(~np.isfinite(series)))
            if finite.size < 2 or nan_fraction > self.max_nan_fraction:
                reasons.append("dropout_or_nonfinite")
            else:
                centered = finite - float(np.median(finite))
                if float(np.std(centered)) < self.min_std_uv:
                    reasons.append("flatline")
                if float(np.max(np.abs(centered))) > self.max_abs_uv:
                    reasons.append("extreme_amplitude")
                if float(np.ptp(centered)) > self.max_peak_to_peak_uv:
                    reasons.append("peak_to_peak_exceeded")
                if centered.size > 2 and float(np.std(np.diff(centered))) > self.max_broadband_diff_std_uv:
                    reasons.append("broadband_artifact")
                split = max(1, centered.size // 4)
                drift = abs(float(np.median(centered[-split:]) - np.median(centered[:split])))
                if drift > self.max_drift_uv:
                    reasons.append("drift")
            if reasons:
                flags[channel] = reasons
        invalid_reference = [channel for channel in self.fixed_reference_channels if channel in flags]
        reference_valid = len(self.fixed_reference_channels) >= self.minimum_reference_channels and not invalid_reference
        return {
            "valid": reference_valid,
            "reference_valid": reference_valid,
            "quality_flags": flags,
            "invalid_reference_channels": invalid_reference,
            "fixed_reference_channels": list(self.fixed_reference_channels),
        }


class CausalFilterBank:
    """Stateful fixed-reference causal filters with deterministic provenance."""

    def __init__(
        self,
        sample_rate_hz: float,
        channel_names: list[str],
        fixed_reference_channels: list[str],
        alpha_band: tuple[float, float],
        *,
        buffer_seconds: float = 30.0,
        notch_hz: float = 60.0,
        warmup_seconds: float = 2.0,
    ) -> None:
        from scipy import signal

        self.sample_rate_hz = float(sample_rate_hz)
        self.channel_names = list(channel_names)
        self.reference_indices = [self.channel_names.index(name) for name in fixed_reference_channels if name in self.channel_names]
        self.fixed_reference_channels = [self.channel_names[index] for index in self.reference_indices]
        self.warmup_seconds = float(warmup_seconds)
        max_samples = max(1, int(round(buffer_seconds * self.sample_rate_hz)))
        self.raw = RingBuffer(max_samples, len(channel_names))
        self.buffers = {
            "erp_0p5_30": RingBuffer(max_samples, len(channel_names)),
            "theta_4_8": RingBuffer(max_samples, len(channel_names)),
            "alpha_individual": RingBuffer(max_samples, len(channel_names)),
        }
        self._started_timestamp: float | None = None
        self._last_finite_raw = np.zeros(len(channel_names), dtype=float)
        self._notch = signal.iirnotch(float(notch_hz), Q=30.0, fs=self.sample_rate_hz) if 0 < notch_hz < self.sample_rate_hz / 2 else None
        self._notch_zi: np.ndarray | None = None
        notch_sos = None if self._notch is None else signal.tf2sos(*self._notch)
        self._sos = {
            "erp_0p5_30": signal.butter(4, [0.5, 30.0], btype="bandpass", fs=self.sample_rate_hz, output="sos"),
            "theta_4_8": signal.butter(4, [4.0, 8.0], btype="bandpass", fs=self.sample_rate_hz, output="sos"),
            "alpha_individual": signal.butter(4, list(alpha_band), btype="bandpass", fs=self.sample_rate_hz, output="sos"),
        }
        self._zi: dict[str, np.ndarray | None] = {name: None for name in self._sos}
        self.provenance = {
            name: _filter_provenance(
                name,
                sos if notch_sos is None else np.vstack((notch_sos, sos)),
                self.sample_rate_hz,
                _representative_frequencies(name, alpha_band),
                components=[name] if notch_sos is None else ["shared_notch_60", name],
            )
            for name, sos in self._sos.items()
        }
        if notch_sos is not None:
            self.provenance["shared_notch_60"] = _filter_provenance(
                "shared_notch_60",
                notch_sos,
                self.sample_rate_hz,
                [55.0, 57.5, 62.5, 65.0],
                components=["shared_notch_60"],
            )

    def process_chunk(self, timestamps: np.ndarray, data: np.ndarray) -> None:
        from scipy import signal

        ts = np.asarray(timestamps, dtype=float)
        values = np.asarray(data, dtype=float)
        if ts.size == 0:
            return
        if self._started_timestamp is None:
            self._started_timestamp = float(ts[0])
        self.raw.append_chunk(ts, values)
        sanitized = values.copy()
        for channel_index in range(sanitized.shape[1]):
            finite = np.isfinite(sanitized[:, channel_index])
            if finite.any():
                first_finite = int(np.flatnonzero(finite)[0])
                sanitized[:first_finite, channel_index] = self._last_finite_raw[channel_index]
                for sample_index in range(first_finite + 1, sanitized.shape[0]):
                    if not np.isfinite(sanitized[sample_index, channel_index]):
                        sanitized[sample_index, channel_index] = sanitized[sample_index - 1, channel_index]
                self._last_finite_raw[channel_index] = float(sanitized[np.flatnonzero(finite)[-1], channel_index])
            else:
                sanitized[:, channel_index] = self._last_finite_raw[channel_index]
        if self.reference_indices:
            referenced = sanitized - np.mean(sanitized[:, self.reference_indices], axis=1, keepdims=True)
        else:
            referenced = sanitized
        if self._notch is not None:
            b, a = self._notch
            if self._notch_zi is None:
                zi = signal.lfilter_zi(b, a)
                self._notch_zi = zi[:, np.newaxis] * referenced[0][np.newaxis, :]
            referenced, self._notch_zi = signal.lfilter(b, a, referenced, axis=0, zi=self._notch_zi)
        for name, sos in self._sos.items():
            if self._zi[name] is None:
                zi = signal.sosfilt_zi(sos)
                self._zi[name] = zi[:, :, np.newaxis] * referenced[0][np.newaxis, np.newaxis, :]
            filtered, self._zi[name] = signal.sosfilt(sos, referenced, axis=0, zi=self._zi[name])
            self.buffers[name].append_chunk(ts, filtered)

    @property
    def latest_timestamp(self) -> float | None:
        return self.raw.latest_timestamp

    def warmup_valid_at(self, timestamp: float) -> bool:
        return self._started_timestamp is not None and float(timestamp) - self._started_timestamp >= self.warmup_seconds


class RealtimeEventEngine:
    """Own stream buffers and scheduling; emit facts without policy decisions."""

    def __init__(self, config: dict[str, Any], sample_rate_hz: float, channel_names: list[str]) -> None:
        self.config = dict(config or {})
        self.sample_rate_hz = float(sample_rate_hz)
        self.channel_names = list(channel_names)
        self.montage_profile = str(self.config.get("montage_profile", "enobio8_inhibition"))
        configured_reference = self.config.get("fixed_reference_channels")
        reference = list(self.channel_names if configured_reference is None else configured_reference)
        alpha_cfg = dict(self.config.get("alpha_band") or {})
        alpha_band = (float(alpha_cfg.get("low_hz", 8.0)), float(alpha_cfg.get("high_hz", 12.0)))
        self.registry = FeatureRegistry(self.channel_names)
        self.quality_gate = EventQualityGate(self.channel_names, reference, self.config.get("quality_gate"))
        self.filters = CausalFilterBank(
            self.sample_rate_hz,
            self.channel_names,
            self.quality_gate.fixed_reference_channels,
            alpha_band,
            buffer_seconds=float(self.config.get("buffer_seconds", 30.0)),
            notch_hz=float(self.config.get("notch_hz", 60.0)),
            warmup_seconds=float(self.config.get("filter_warmup_seconds", 2.0)),
        )
        self.marker_prefix = str(self.config.get("marker_prefix", "go_nogo_stimulus_onset"))
        self.include_practice_trials = bool(self.config.get("include_practice_trials", False))
        self.calibration_id = self.config.get("calibration_id")
        alpha_rebound = dict(self.config.get("alpha_rebound") or {"enabled": False, "window_seconds": [0.8, 1.5]})
        if bool(alpha_rebound.get("enabled", False)):
            raise ValueError("alpha rebound is configured but must remain disabled for inhibition8 v1")
        self.disabled_features = {"alpha_rebound": alpha_rebound}
        self.display_timing = {
            "display_latency_model": "fixed_offset",
            "display_latency_ms": float(self.config.get("display_latency_ms", 0.0)),
            "display_latency_validated_by_photodiode": bool(self.config.get("display_latency_validated_by_photodiode", False)),
        }
        self.events: list[EventState] = []
        self.packet_index = 0

    def process_chunk(self, timestamps: np.ndarray, data: np.ndarray) -> list[dict[str, Any]]:
        self.filters.process_chunk(timestamps, data)
        return self.emit_ready()

    def add_marker(self, marker: MarkerEvent) -> list[dict[str, Any]]:
        if not (marker.label == self.marker_prefix or marker.label.startswith(f"{self.marker_prefix}_")):
            return []
        metadata = marker.parsed_metadata(self.marker_prefix)
        trial = metadata.get("trial", "unknown")
        if not self.include_practice_trials:
            try:
                if int(trial) < 1:
                    return []
            except (TypeError, ValueError):
                pass
        self.events.append(EventState(f"go_nogo:{trial}:{marker.timestamp:.9f}", marker, metadata))
        return self.emit_ready()

    def emit_ready(self) -> list[dict[str, Any]]:
        latest = self.filters.latest_timestamp
        if latest is None:
            return []
        packets: list[dict[str, Any]] = []
        remaining: list[EventState] = []
        for event in self.events:
            for stage in STAGE_DEFINITIONS:
                if stage.name in event.completed_stages or latest + 1e-9 < event.marker.timestamp + stage.available_after_seconds:
                    continue
                packet = self._compute_stage(event, stage, latest)
                packets.append(packet)
                event.completed_stages.add(stage.name)
            if len(event.completed_stages) < len(STAGE_DEFINITIONS):
                remaining.append(event)
        self.events = remaining
        return packets

    def _compute_stage(self, event: EventState, stage: StageDefinition, latest: float) -> dict[str, Any]:
        started = monotonic()
        feature_values: dict[str, float | None] = {}
        feature_validity: dict[str, bool] = {}
        quality_flags: dict[str, Any] = {}
        stage_start = min(FEATURE_DEFINITIONS[name].window[0] for name in stage.feature_names)
        stage_end = max(FEATURE_DEFINITIONS[name].window[1] for name in stage.feature_names)
        if any(FEATURE_DEFINITIONS[name].filter_profile == "erp_0p5_30" for name in stage.feature_names):
            stage_start = min(stage_start, ERP_BASELINE_WINDOW[0])
            stage_end = max(stage_end, ERP_BASELINE_WINDOW[1])
        _raw_ts, raw_data = self.filters.raw.range(event.marker.timestamp + stage_start, event.marker.timestamp + stage_end)
        quality = self.quality_gate.assess(raw_data)
        warmup_valid = self.filters.warmup_valid_at(event.marker.timestamp + stage_start)
        for feature_name in stage.feature_names:
            values, valid, flags = self._compute_feature(
                event,
                feature_name,
                quality["reference_valid"],
                warmup_valid,
                quality.get("quality_flags", {}),
            )
            feature_values.update(values)
            feature_validity[feature_name] = valid
            if flags:
                quality_flags[feature_name] = flags
        event.cumulative_features.update(feature_values)
        event.cumulative_validity.update(feature_validity)
        self.packet_index += 1
        now = monotonic()
        packet = {
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "packet_index": self.packet_index,
            "event_id": event.event_id,
            "stage": stage.name,
            "trial": event.metadata.get("trial"),
            "condition": event.metadata.get("condition"),
            "marker_label": event.marker.label,
            "marker_timestamp_lsl": float(event.marker.timestamp),
            "stage_deadline_lsl": float(event.marker.timestamp + stage.available_after_seconds),
            "eeg_horizon_lsl": float(latest),
            "feature_computed_monotonic": now,
            "processing_latency_ms": float((now - started) * 1000.0),
            "publication_latency_ms": float(max(0.0, (latest - event.marker.timestamp) * 1000.0)),
            "new_features": feature_values,
            "features": dict(event.cumulative_features),
            "feature_validity": dict(event.cumulative_validity),
            "valid": bool(quality["reference_valid"] and warmup_valid and all(feature_validity.values())),
            "quality_flags": {**quality.get("quality_flags", {}), **quality_flags},
            "reference_valid": bool(quality["reference_valid"]),
            "invalid_reference_channels": quality.get("invalid_reference_channels", []),
            "filter_warmup_valid": warmup_valid,
            "roi_resolution": self.registry.payload(),
            "feature_declarations": self.registry.declarations_payload(),
            "include_practice_trials": self.include_practice_trials,
            "erp_baseline_window_seconds": list(ERP_BASELINE_WINDOW),
            "disabled_features": self.disabled_features,
            "missing_declared_channels": self.registry.missing,
            "decision_eligibility": {"observe_only": True, "can_adapt": False, "reason": "v1_observe_only"},
            "montage_profile": self.montage_profile,
            "channel_order": list(self.channel_names),
            "fixed_reference_channels": list(self.filters.fixed_reference_channels),
            "sample_rate_hz": self.sample_rate_hz,
            "calibration_id": self.calibration_id,
            "filter_profiles": self.filters.provenance,
            "filter_state_nonfinite_handling": "causal_last_finite_hold_raw_quality_invalidates_features",
            "filter_effective_delay_ms": {
                name: values["effective_delay_ms"] for name, values in self.filters.provenance.items()
            },
            "filter_delay_correction_ms": {
                name: values["effective_delay_ms"] for name, values in self.filters.provenance.items()
            },
            "feature_window_delay_correction_applied": False,
            "peak_latency_interpretation": "exploratory_not_for_physiological_latency_claims",
            **self.display_timing,
        }
        return packet

    def _compute_feature(
        self,
        event: EventState,
        feature_name: str,
        reference_valid: bool,
        warmup_valid: bool,
        channel_quality_flags: dict[str, Any],
    ) -> tuple[dict[str, float | None], bool, list[str]]:
        definition = FEATURE_DEFINITIONS[feature_name]
        channels = self.registry.resolution[feature_name]
        flags: list[str] = []
        if len(channels) < definition.minimum_channels:
            flags.append("required_channels_missing")
        if not reference_valid:
            flags.append("reference_contaminated_or_insufficient")
        if not warmup_valid:
            flags.append("filter_warmup_incomplete")
        contaminated = [channel for channel in channels if channel in channel_quality_flags]
        if contaminated:
            flags.append("feature_channels_contaminated:" + ",".join(contaminated))
        valid = not flags
        indices = self.registry.indices(feature_name)
        start, end = definition.window
        timestamps, data = self.filters.buffers[definition.filter_profile].range(
            event.marker.timestamp + start, event.marker.timestamp + end
        )
        if data.shape[0] < 2 or not indices:
            flags.append("feature_window_incomplete")
            valid = False
        if not valid:
            return _empty_feature_values(feature_name), False, flags
        roi = data[:, indices].mean(axis=1)
        relative = timestamps - event.marker.timestamp
        if feature_name == "readiness_alpha":
            power = float(np.mean(np.square(data[:, indices])))
            return {"readiness_alpha_power": power}, True, flags
        if feature_name == "early_theta":
            values: dict[str, float | None] = {"early_theta_power": float(np.mean(np.square(data[:, indices])))}
            for channel in definition.optional_channels:
                if channel in self.channel_names:
                    channel_index = self.channel_names.index(channel)
                    values[f"early_theta_power_{channel}"] = float(np.mean(np.square(data[:, channel_index])))
            return values, True, flags
        if feature_name in {"n2", "p3"}:
            baseline_ts, baseline_data = self.filters.buffers["erp_0p5_30"].range(
                event.marker.timestamp + ERP_BASELINE_WINDOW[0],
                event.marker.timestamp + ERP_BASELINE_WINDOW[1],
            )
            if baseline_data.shape[0] < 2:
                return _empty_feature_values(feature_name), False, [*flags, "erp_baseline_incomplete"]
            baseline = float(np.mean(baseline_data[:, indices]))
            corrected = roi - baseline
            peak_index = int(np.argmin(corrected) if feature_name == "n2" else np.argmax(corrected))
            delay = float(self.filters.provenance["erp_0p5_30"]["effective_delay_ms"])
            return {
                f"{feature_name}_mean_uv": float(np.mean(corrected)),
                f"{feature_name}_peak_uv_exploratory": float(corrected[peak_index]),
                f"{feature_name}_peak_latency_ms_observed_exploratory": float(relative[peak_index] * 1000.0),
                f"{feature_name}_peak_latency_ms_delay_corrected_exploratory": float(relative[peak_index] * 1000.0 - delay),
            }, True, flags
        if feature_name == "alpha_erd":
            readiness = event.cumulative_features.get("readiness_alpha_power")
            post = float(np.mean(np.square(data[:, indices])))
            if readiness is None or float(readiness) <= 1e-12:
                return {"poststim_alpha_power": post, "alpha_erd_percent": None, "alpha_erd_log_ratio": None}, False, [
                    *flags,
                    "readiness_alpha_unavailable",
                ]
            return {
                "poststim_alpha_power": post,
                "alpha_erd_percent": float(100.0 * (post - float(readiness)) / float(readiness)),
                "alpha_erd_log_ratio": float(np.log(max(post, 1e-12) / float(readiness))),
            }, True, flags
        return {}, False, ["unknown_feature"]

    def metadata_payload(self) -> dict[str, Any]:
        return {
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "montage_profile": self.montage_profile,
            "channel_order": list(self.channel_names),
            "sample_rate_hz": self.sample_rate_hz,
            "fixed_reference_channels": list(self.filters.fixed_reference_channels),
            "roi_resolution": self.registry.payload(),
            "feature_declarations": self.registry.declarations_payload(),
            "erp_baseline_window_seconds": list(ERP_BASELINE_WINDOW),
            "disabled_features": self.disabled_features,
            "missing_declared_channels": self.registry.missing,
            "filter_profiles": self.filters.provenance,
            "filter_state_nonfinite_handling": "causal_last_finite_hold_raw_quality_invalidates_features",
            "decision_eligibility": {"observe_only": True, "can_adapt": False, "reason": "v1_observe_only"},
            **self.display_timing,
        }


class EngineInputCaptureWriter:
    """Append exact online realtime inputs as framed binary records."""

    def __init__(self, path: str | Path, header: dict[str, Any]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("wb")
        encoded = json.dumps(header, sort_keys=True).encode("utf-8")
        self._handle.write(CAPTURE_MAGIC)
        self._handle.write(struct.pack("<I", len(encoded)))
        self._handle.write(encoded)

    def write_eeg(self, timestamps: np.ndarray, data: np.ndarray) -> None:
        ts = np.asarray(timestamps, dtype="<f8")
        values = np.asarray(data, dtype="<f8")
        self._handle.write(b"E")
        self._handle.write(struct.pack("<II", ts.size, values.shape[1]))
        self._handle.write(ts.tobytes(order="C"))
        self._handle.write(values.tobytes(order="C"))

    def write_marker(self, marker: MarkerEvent) -> None:
        payload = json.dumps(
            {"label": marker.label, "timestamp": marker.timestamp, "timebase": marker.timebase, "source": marker.source},
            sort_keys=True,
        ).encode("utf-8")
        self._handle.write(b"M")
        self._handle.write(struct.pack("<I", len(payload)))
        self._handle.write(payload)

    def flush(self) -> None:
        self._handle.flush()

    def close(self) -> None:
        self.flush()
        self._handle.close()


def read_engine_capture(path: str | Path) -> tuple[dict[str, Any], Iterator[tuple[str, Any]]]:
    target = Path(path)
    handle = target.open("rb")
    if handle.read(len(CAPTURE_MAGIC)) != CAPTURE_MAGIC:
        handle.close()
        raise ValueError("invalid realtime engine capture magic")
    header_size = struct.unpack("<I", handle.read(4))[0]
    header = json.loads(handle.read(header_size).decode("utf-8"))

    def records() -> Iterator[tuple[str, Any]]:
        try:
            while True:
                kind = handle.read(1)
                if not kind:
                    break
                if kind == b"E":
                    sample_count, channel_count = struct.unpack("<II", handle.read(8))
                    timestamps = np.frombuffer(handle.read(sample_count * 8), dtype="<f8").copy()
                    data = np.frombuffer(handle.read(sample_count * channel_count * 8), dtype="<f8").copy()
                    yield "eeg", (timestamps, data.reshape(sample_count, channel_count))
                elif kind == b"M":
                    size = struct.unpack("<I", handle.read(4))[0]
                    row = json.loads(handle.read(size).decode("utf-8"))
                    yield "marker", MarkerEvent(
                        label=str(row["label"]),
                        timestamp=float(row["timestamp"]),
                        timebase=str(row.get("timebase", "lsl")),
                        source=str(row.get("source", "capture")),
                    )
                else:
                    raise ValueError(f"unknown realtime capture frame {kind!r}")
        finally:
            handle.close()

    return header, records()


# Neutral names for classifier capture/replay; legacy names remain compatible.
RealtimeInputCaptureWriter = EngineInputCaptureWriter
read_realtime_capture = read_engine_capture


def _filter_provenance(
    name: str,
    sos: np.ndarray,
    sample_rate_hz: float,
    frequencies: list[float],
    *,
    components: list[str] | None = None,
) -> dict[str, Any]:
    from scipy import signal

    requested = np.asarray([frequency for frequency in frequencies if 0 < frequency < sample_rate_hz / 2], dtype=float)
    if requested.size == 0:
        requested = np.asarray([min(10.0, sample_rate_hz / 4.0)], dtype=float)
    dense_frequencies, response = signal.sosfreqz(sos, worN=16384, fs=sample_rate_hz)
    phase = np.unwrap(np.angle(response))
    angular_frequency = 2.0 * np.pi * dense_frequencies / sample_rate_hz
    dense_delays = -np.gradient(phase, angular_frequency)
    delays = np.interp(requested, dense_frequencies, dense_delays)
    finite = delays[np.isfinite(delays)]
    delay_ms = finite / sample_rate_hz * 1000.0 if finite.size else np.asarray([0.0])
    coefficient_hash = hashlib.sha256(np.asarray(sos, dtype="<f8").tobytes()).hexdigest()
    return {
        "profile_id": name,
        "components": list(components or [name]),
        "coefficient_sha256": coefficient_hash,
        "characterization_method": "scipy.signal.sosfreqz_phase_derivative_at_representative_frequencies",
        "representative_frequencies_hz": requested.astype(float).tolist(),
        "frequency_delay_ms": delay_ms.astype(float).tolist(),
        "effective_delay_ms": float(np.median(delay_ms)),
        "delay_range_ms": [float(np.min(delay_ms)), float(np.max(delay_ms))],
        "delay_correction_applied": False,
    }


def _representative_frequencies(name: str, alpha_band: tuple[float, float]) -> list[float]:
    if name == "erp_0p5_30":
        return [1.0, 5.0, 10.0, 20.0, 30.0]
    if name == "theta_4_8":
        return [4.0, 6.0, 8.0]
    return [float(alpha_band[0]), float(sum(alpha_band) / 2.0), float(alpha_band[1])]


def _empty_feature_values(feature_name: str) -> dict[str, float | None]:
    if feature_name == "readiness_alpha":
        return {"readiness_alpha_power": None}
    if feature_name == "early_theta":
        return {"early_theta_power": None}
    if feature_name in {"n2", "p3"}:
        return {
            f"{feature_name}_mean_uv": None,
            f"{feature_name}_peak_uv_exploratory": None,
            f"{feature_name}_peak_latency_ms_observed_exploratory": None,
            f"{feature_name}_peak_latency_ms_delay_corrected_exploratory": None,
        }
    if feature_name == "alpha_erd":
        return {"poststim_alpha_power": None, "alpha_erd_percent": None, "alpha_erd_log_ratio": None}
    return {}
