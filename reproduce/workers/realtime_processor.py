"""Realtime processor worker process."""

from __future__ import annotations

import argparse
import json
import threading
from time import monotonic, sleep
from typing import Any

import numpy as np

from reproduce.config import load_config
from reproduce.devices.lsl_eeg import _select_lsl_info
from reproduce.hardware.enobio import mapped_channel_names
from reproduce.lsl import inlet_time_correction, lsl_processing_flags
from reproduce.realtime.alpha import AlphaPowerEstimator, load_alpha_config
from reproduce.realtime.buffer import RingBuffer
from reproduce.realtime.epoching import EpochingConfig, MarkerEvent, RealtimeEpocher, expected_sample_count
from reproduce.realtime.event_features import EngineInputCaptureWriter, RealtimeEventEngine
from reproduce.realtime.registry import make_feedback_emitter, make_model, make_policy, make_stream_preprocessor
from reproduce.session import paths_for_existing_session
from reproduce.telemetry import Telemetry, telemetry_config_from
from reproduce.workers.common import JsonlWriter, StatusWriter, append_jsonl, install_stop_signal_handlers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Managed realtime processor worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--backend", default="lsl")
    parser.add_argument("--preprocessor", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--feedback-backend", default="disabled")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    paths = paths_for_existing_session(args.session_dir)
    telemetry = Telemetry.from_config(config, paths, component="realtime_processor")
    telemetry_config = telemetry_config_from(config)
    status = StatusWriter(paths.process_logs / "realtime_processor.status.json", "realtime_processor", args.backend, telemetry)
    stop_event = threading.Event()
    install_stop_signal_handlers(stop_event)

    if args.backend in {"disabled", "none"}:
        status.update("disabled", reason="realtime processor disabled")
        return 0
    if args.backend != "lsl":
        status.update("failed", error=f"realtime processor backend '{args.backend}' is not implemented")
        return 2

    try:
        import pylsl
    except Exception as exc:
        telemetry.emit(
            "process.failed",
            level="default",
            message="pylsl import failed",
            metadata={"exception_type": type(exc).__name__, "exception": str(exc)},
        )
        status.update("failed", error=f"pylsl import failed: {type(exc).__name__}: {exc}")
        return 1

    eeg_config = config.get("hardware", {}).get("eeg", {})
    realtime_config = config.get("realtime", {})
    feedback_config = realtime_config.get("feedback", {})
    timeout = float(eeg_config.get("stream_timeout_seconds", 5.0))
    telemetry.emit(
        "lsl.discovery.start",
        level="default",
        message="Resolving realtime EEG LSL stream",
        metadata={
            "lsl_stream_type": eeg_config.get("lsl_stream_type", "EEG"),
            "lsl_name_patterns": eeg_config.get("lsl_name_patterns", []),
            "timeout_seconds": timeout,
        },
    )
    status.update("starting", message="resolving EEG LSL stream")
    info, stream = _select_lsl_info(pylsl, eeg_config, timeout)
    if info is None:
        telemetry.emit("lsl.discovery.failed", level="default", message="No realtime EEG LSL stream found")
        status.update("failed", error="no matching LSL EEG stream found")
        return 1

    sample_rate = float(info.nominal_srate() or eeg_config.get("expected_sample_rate_hz", 500.0))
    channel_count = int(info.channel_count())
    stream_info = _stream_dict(info)
    channel_names, mapping_source = _channel_names(stream_info, channel_count, eeg_config)
    stream_info["channel_names"] = channel_names
    stream_info["channel_mapping_source"] = mapping_source
    stream_info["lsl_processing"] = ["clocksync", "dejitter", "monotonize"]
    inlet = pylsl.StreamInlet(
        info,
        max_buflen=60,
        max_chunklen=int(realtime_config.get("max_chunk_samples", 32)),
        recover=True,
        processing_flags=lsl_processing_flags(pylsl, dejitter=True),
    )
    inlet.open_stream(timeout=timeout)
    stream_info["initial_time_correction_seconds"] = inlet_time_correction(inlet)
    telemetry.emit(
        "lsl.discovery.complete",
        level="default",
        message="Realtime EEG LSL stream connected",
        metadata=stream_info,
    )

    window_seconds = float(realtime_config.get("window_seconds", 2.0))
    step_seconds = float(realtime_config.get("step_seconds", 0.25))
    preprocessing_config = realtime_config.get("preprocessing", {})
    event_features_config = dict(realtime_config.get("event_features", {}))
    display_config = dict(config.get("hardware", {}).get("display", {}))
    event_features_config.setdefault("display_latency_ms", float(display_config.get("fixed_display_latency_ms", 0.0)))
    event_features_config.setdefault(
        "display_latency_validated_by_photodiode",
        bool(display_config.get("photodiode_latency_validated", False)),
    )
    event_features_enabled = bool(event_features_config.get("enabled", False))
    epoching_config = EpochingConfig.from_dict(realtime_config.get("epoching", {}))
    epoching_enabled = bool(epoching_config.enabled) and not event_features_enabled
    preprocessor_kind = args.preprocessor or preprocessing_config.get("kind", "causal_bandpass_notch")
    preprocessor = None if event_features_enabled else make_stream_preprocessor(preprocessor_kind, sample_rate, channel_count, preprocessing_config)
    alpha_config = load_alpha_config(config, paths.root)
    alpha_enabled = bool(alpha_config.get("enabled", False))
    model_config = realtime_config.get("model", {})
    required_channels = [str(name) for name in model_config.get("required_channels", [])]
    missing_required_channels = [name for name in required_channels if name not in channel_names]
    if epoching_enabled and missing_required_channels:
        error = "realtime EEG montage is missing model-required channels: " + ", ".join(missing_required_channels)
        telemetry.emit("process.failed", level="default", message=error)
        status.update("failed", error=error, eeg_stream=stream_info)
        inlet.close_stream()
        return 1
    posterior_channels = [str(name) for name in alpha_config.get("posterior_channels", [])]
    if alpha_enabled and posterior_channels and not any(name in channel_names for name in posterior_channels):
        error = (
            "realtime EEG montage has none of the configured posterior alpha channels: "
            + ", ".join(posterior_channels)
        )
        telemetry.emit("process.failed", level="default", message=error)
        status.update("failed", error=error, eeg_stream=stream_info)
        inlet.close_stream()
        return 1
    model_kind = "none_observe_only" if event_features_enabled else (args.model or model_config.get("kind", "erp_peak_baseline"))
    model = None if event_features_enabled else make_model(model_kind, model_config)
    policy_config = dict(realtime_config.get("decision_policy", {}))
    policy_config.setdefault("allow_task_adaptation", bool(feedback_config.get("allow_task_adaptation", True)))
    policy_kind = "observe_only" if event_features_enabled else str(policy_config.get("kind", "conservative_p300"))
    policy = None if event_features_enabled else make_policy(policy_kind, policy_config)
    feedback_backend = "disabled" if event_features_enabled else args.feedback_backend
    emitter = make_feedback_emitter(feedback_backend, feedback_config, paths.realtime_feedback_jsonl)
    processed_sample_rate = sample_rate if preprocessor is None else preprocessor.output_sample_rate_hz
    alpha_estimator = AlphaPowerEstimator(processed_sample_rate, channel_names, alpha_config) if alpha_enabled else None
    alpha_step_seconds = float(alpha_config.get("step_seconds", realtime_config.get("step_seconds", 0.25)))
    window_samples = max(1, int(round(window_seconds * processed_sample_rate)))
    buffer_samples = max(window_samples * 5, int(round(processed_sample_rate * 30)))
    buffer = None if event_features_enabled else RingBuffer(buffer_samples, channel_count)
    raw_epoch_samples = expected_sample_count(sample_rate, epoching_config)
    raw_buffer_samples = max(raw_epoch_samples * 5, int(round(sample_rate * 30)))
    raw_buffer = None if event_features_enabled else RingBuffer(raw_buffer_samples, channel_count)
    epocher = RealtimeEpocher(epoching_config) if epoching_enabled else None
    event_engine = RealtimeEventEngine(event_features_config, sample_rate, channel_names) if event_features_enabled else None

    marker_inlet = None
    marker_stream = None
    marker_status = "pending"
    marker_count = 0
    sample_count = 0
    processed_count = 0
    window_count = 0
    epoch_count = 0
    rejected_epoch_count = 0
    alpha_estimate_count = 0
    event_feature_packet_count = 0
    latest_raw_eeg_timestamp: float | None = None
    next_process_at = monotonic() + step_seconds
    next_alpha_at = monotonic() + alpha_step_seconds
    next_status_at = monotonic()
    next_marker_resolve_at = monotonic()
    clock_error_threshold_seconds = float(realtime_config.get("clock_error_threshold_seconds", 60.0))
    pull_timeout_seconds = float(realtime_config.get("pull_timeout_seconds", 0.01))
    alpha_telemetry_every = max(1, int(alpha_config.get("telemetry_every_estimates", 10)))
    heartbeat_seconds = float(telemetry_config.get("heartbeat_seconds", 5.0))
    next_health_event_at = monotonic() + heartbeat_seconds
    last_marker_status = marker_status

    alpha_writer = (
        JsonlWriter(paths.realtime_alpha_jsonl, flush_every=max(1, int(round(1.0 / max(alpha_step_seconds, 1e-6)))))
        if alpha_enabled
        else None
    )
    event_feature_writer = JsonlWriter(paths.realtime_event_features_jsonl, flush_every=1) if event_features_enabled else None
    capture_writer = (
        EngineInputCaptureWriter(
            paths.realtime_engine_capture,
            {
                "schema_version": 1,
                "sample_rate_hz": sample_rate,
                "channel_names": channel_names,
                "event_features_config": event_features_config,
            },
        )
        if event_features_enabled
        else None
    )
    if event_engine is not None:
        paths.realtime_engine_metadata.write_text(
            json.dumps(event_engine.metadata_payload(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    try:
        status.update(
            "running",
            eeg_stream=stream_info,
            marker_status=marker_status,
            sample_rate_hz=sample_rate,
            processed_sample_rate_hz=processed_sample_rate,
            channel_count=channel_count,
            window_samples=window_samples,
            epoching_enabled=epoching_enabled,
            epoch_marker_prefix=epoching_config.marker_prefix,
            epoch_window_seconds=[epoching_config.tmin_seconds, epoching_config.tmax_seconds],
            raw_epoch_samples=raw_epoch_samples,
            model_kind=model_kind,
            decision_policy=policy_kind,
            feedback_backend=feedback_backend,
            alpha_enabled=alpha_enabled,
            alpha_config=alpha_config if alpha_enabled else None,
            event_features_enabled=event_features_enabled,
            event_feature_schema=None if event_engine is None else event_engine.metadata_payload().get("feature_schema_version"),
        )
        while not stop_event.is_set():
            samples, timestamps = inlet.pull_chunk(timeout=pull_timeout_seconds, max_samples=int(realtime_config.get("max_pull_samples", 128)))
            if samples:
                raw = np.asarray(samples, dtype=float)
                ts = np.asarray(timestamps, dtype=float)
                sample_count += raw.shape[0]
                latest_raw_eeg_timestamp = float(ts[-1])
                if raw_buffer is not None:
                    raw_buffer.append_chunk(ts, raw)
                if capture_writer is not None:
                    capture_writer.write_eeg(ts, raw)
                if event_engine is not None:
                    packets = event_engine.process_chunk(ts, raw)
                    assert event_feature_writer is not None
                    for packet in packets:
                        event_feature_writer.write(packet)
                        event_feature_packet_count += 1
                alpha_artifact = None
                if alpha_estimator is not None:
                    alpha_artifact = alpha_estimator.check_artifact(_reference_for_artifact_gate(raw, preprocessing_config))
                if preprocessor is not None and buffer is not None:
                    processed_ts, processed = preprocessor.process_chunk(ts, raw)
                    buffer.append_chunk(processed_ts, processed)
                    processed_count += processed.shape[0]
                    if alpha_estimator is not None and processed.shape[0] > 0:
                        alpha_estimator.process_chunk(processed_ts, processed, artifact_result=alpha_artifact)
                else:
                    processed_count += raw.shape[0]

            if marker_inlet is None and monotonic() >= next_marker_resolve_at:
                marker_inlet, marker_stream = _try_open_marker_inlet(pylsl, config)
                marker_status = "connected" if marker_inlet is not None else "missing"
                if marker_status != last_marker_status:
                    telemetry.emit(
                        "lsl.marker_status",
                        level="default" if marker_status == "connected" else "realtime",
                        message=f"Marker stream {marker_status}",
                        metadata={"marker_status": marker_status, "marker_stream": marker_stream},
                    )
                    last_marker_status = marker_status
                next_marker_resolve_at = monotonic() + 1.0
            if marker_inlet is not None:
                try:
                    marker_events = _pull_markers(marker_inlet, paths.realtime_markers_jsonl, telemetry)
                except Exception as exc:
                    telemetry.emit(
                        "lsl.marker_disconnected",
                        level="default",
                        message="Marker inlet failed; resolving the session marker stream again",
                        metadata={"exception_type": type(exc).__name__, "exception": str(exc), "marker_stream": marker_stream},
                    )
                    _close_inlet(marker_inlet)
                    marker_inlet = None
                    marker_stream = None
                    marker_status = "reconnecting"
                    next_marker_resolve_at = monotonic()
                    marker_events = []
                marker_count += len(marker_events)
                if marker_events:
                    if latest_raw_eeg_timestamp is not None:
                        for marker in marker_events:
                            delta = float(marker.timestamp - latest_raw_eeg_timestamp)
                            if abs(delta) > clock_error_threshold_seconds:
                                raise RuntimeError(
                                    f"marker/EEG LSL clock-domain mismatch: marker is {delta:.3f}s from latest EEG sample"
                                )
                    if capture_writer is not None:
                        for marker in marker_events:
                            capture_writer.write_marker(marker)
                    if event_engine is not None:
                        assert event_feature_writer is not None
                        for marker in marker_events:
                            for packet in event_engine.add_marker(marker):
                                event_feature_writer.write(packet)
                                event_feature_packet_count += 1
                if epocher is not None:
                    for marker in marker_events:
                        epocher.add_marker(marker)

            if alpha_estimator is not None and monotonic() >= next_alpha_at:
                alpha_payload = alpha_estimator.snapshot()
                if alpha_payload is not None:
                    alpha_estimate_count += 1
                    alpha_payload.update(
                        {
                            "estimate_index": alpha_estimate_count,
                            "sample_count": sample_count,
                            "processed_sample_count": processed_count,
                        }
                    )
                    assert alpha_writer is not None
                    alpha_writer.write(alpha_payload)
                    if alpha_estimate_count == 1 or alpha_estimate_count % alpha_telemetry_every == 0:
                        telemetry.emit(
                            "alpha.estimate",
                            level="realtime",
                            message=f"Realtime alpha estimate: {alpha_payload['alpha_power']:.3f}",
                            metadata=alpha_payload,
                        )
                next_alpha_at = _advance_deadline(next_alpha_at, alpha_step_seconds, monotonic())

            if epocher is not None and raw_buffer is not None and epocher.pending_count > 0 and len(raw_buffer) > 0:
                latest_timestamp = float(raw_buffer.window(1)[0][-1])
                oldest_marker = epocher.oldest_pending_timestamp
                required_seconds = epoching_config.duration_seconds
                if oldest_marker is not None:
                    required_seconds = max(
                        required_seconds,
                        latest_timestamp - (oldest_marker + epoching_config.tmin_seconds) + epoching_config.sample_tolerance_seconds,
                    )
                required_samples = max(raw_epoch_samples, int(np.ceil(required_seconds * sample_rate)) + 2)
                raw_timestamps, raw_data = raw_buffer.window(required_samples)
                ready_epochs, rejected_epochs = epocher.extract_ready(
                    raw_timestamps,
                    raw_data,
                    sample_rate,
                    channel_names,
                )
                for attempt in rejected_epochs:
                    rejected_epoch_count += 1
                    rejected_payload = attempt.payload(epoching_config)
                    append_jsonl(paths.realtime_epochs_jsonl, rejected_payload)
                    telemetry.emit(
                        "realtime.epoch_rejected",
                        level="realtime",
                        message="Realtime epoch rejected",
                        metadata=rejected_payload,
                    )
                for epoch in ready_epochs:
                    epoch_count += 1
                    processing_started = monotonic()
                    epoch_payload = epoch.metadata_payload()
                    model_metadata = {**epoch_payload, "relative_times": epoch.relative_times.astype(float).tolist()}
                    assert model is not None and policy is not None
                    prediction = model.predict_epoch(epoch.data, sample_rate, channel_names, model_metadata)
                    latency_ms = (monotonic() - processing_started) * 1000.0
                    prediction.latency_ms = latency_ms
                    actions = policy.decide(prediction, epoch_payload)
                    append_jsonl(paths.realtime_epochs_jsonl, epoch_payload)
                    payload = {
                        "schema_version": 1,
                        "decision_source": "event_epoch",
                        "window_index": None,
                        "epoch_index": epoch.epoch_index,
                        "created_at_monotonic": monotonic(),
                        "sample_count": sample_count,
                        "processed_sample_count": processed_count,
                        "marker_count": marker_count,
                        "epoch_count": epoch_count,
                        "prediction_label": prediction.label,
                        "prediction_score": prediction.score,
                        "prediction_probability": prediction.probability,
                        "features": prediction.features,
                        "prediction": prediction.to_payload(),
                        "actions": [action.to_payload() for action in actions],
                        "feedback": actions[0].to_payload() if actions else None,
                        "processing_latency_ms": latency_ms,
                        "epoch": epoch_payload,
                    }
                    append_jsonl(paths.realtime_decisions_jsonl, payload)
                    emitter.emit(payload)
                    telemetry.emit(
                        "model.prediction",
                        level="realtime",
                        message=f"Realtime epoch prediction: {prediction.label} ({prediction.score:.3f})",
                        metadata=payload,
                    )

            if not epoching_enabled and not event_features_enabled and buffer is not None and len(buffer) >= window_samples and monotonic() >= next_process_at:
                window_count += 1
                processing_started = monotonic()
                window_timestamps, window_data = buffer.window(window_samples)
                assert model is not None and policy is not None
                prediction = model.predict_window(window_data, processed_sample_rate)
                latency_ms = (monotonic() - processing_started) * 1000.0
                prediction.latency_ms = latency_ms
                window_metadata = {
                    "window_index": window_count,
                    "window_start_lsl_timestamp": float(window_timestamps[0]),
                    "window_end_lsl_timestamp": float(window_timestamps[-1]),
                }
                actions = policy.decide(prediction, window_metadata)
                payload = {
                    "schema_version": 1,
                    "decision_source": "rolling_window",
                    "window_index": window_count,
                    "created_at_monotonic": monotonic(),
                    "window_start_lsl_timestamp": float(window_timestamps[0]),
                    "window_end_lsl_timestamp": float(window_timestamps[-1]),
                    "sample_count": sample_count,
                    "processed_sample_count": processed_count,
                    "marker_count": marker_count,
                    "prediction_label": prediction.label,
                    "prediction_score": prediction.score,
                    "prediction_probability": prediction.probability,
                    "features": prediction.features,
                    "prediction": prediction.to_payload(),
                    "actions": [action.to_payload() for action in actions],
                    "feedback": actions[0].to_payload() if actions else None,
                    "processing_latency_ms": latency_ms,
                }
                append_jsonl(paths.realtime_windows_jsonl, payload)
                append_jsonl(paths.realtime_decisions_jsonl, payload)
                emitter.emit(payload)
                telemetry.emit(
                    "model.prediction",
                    level="realtime",
                    message=f"Realtime window prediction: {prediction.label} ({prediction.score:.3f})",
                    metadata=payload,
                )
                next_process_at = _advance_deadline(next_process_at, step_seconds, monotonic())

            if monotonic() >= next_status_at:
                if capture_writer is not None:
                    capture_writer.flush()
                status.update(
                    "running",
                    eeg_stream=stream_info,
                    marker_status=marker_status,
                    marker_stream=marker_stream,
                    sample_count=sample_count,
                    processed_sample_count=processed_count,
                    marker_count=marker_count,
                    window_count=window_count,
                    epoch_count=epoch_count,
                    rejected_epoch_count=rejected_epoch_count,
                    alpha_estimate_count=alpha_estimate_count,
                    alpha_enabled=alpha_enabled,
                    event_features_enabled=event_features_enabled,
                    event_feature_packet_count=event_feature_packet_count,
                    pending_epoch_count=0 if epocher is None else epocher.pending_count,
                    buffer_samples=0 if buffer is None else len(buffer),
                    raw_buffer_samples=0 if raw_buffer is None else len(raw_buffer),
                )
                next_status_at = _advance_deadline(next_status_at, 1.0, monotonic())
            if monotonic() >= next_health_event_at:
                telemetry.emit(
                    "eeg.sample_heartbeat",
                    level="realtime",
                    message="Realtime EEG heartbeat",
                    metadata={
                        "sample_count": sample_count,
                        "processed_sample_count": processed_count,
                        "marker_count": marker_count,
                        "window_count": window_count,
                        "epoch_count": epoch_count,
                        "rejected_epoch_count": rejected_epoch_count,
                        "alpha_estimate_count": alpha_estimate_count,
                        "alpha_enabled": alpha_enabled,
                        "event_features_enabled": event_features_enabled,
                        "event_feature_packet_count": event_feature_packet_count,
                        "buffer_samples": 0 if buffer is None else len(buffer),
                        "raw_buffer_samples": 0 if raw_buffer is None else len(raw_buffer),
                    },
                )
                next_health_event_at = _advance_deadline(next_health_event_at, heartbeat_seconds, monotonic())
            sleep(0.001)
    except Exception as exc:
        telemetry.emit(
            "process.failed",
            level="default",
            message="Realtime processor failed",
            metadata={"exception_type": type(exc).__name__, "exception": str(exc)},
        )
        status.update("failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if alpha_writer is not None:
            alpha_writer.close()
        if event_feature_writer is not None:
            event_feature_writer.close()
        if capture_writer is not None:
            capture_writer.close()
        if marker_inlet is not None:
            _close_inlet(marker_inlet)
        emitter.close()
        _close_inlet(inlet)

    status.update(
        "stopped",
        eeg_stream=stream_info,
        marker_status=marker_status,
        marker_stream=marker_stream,
        sample_count=sample_count,
        processed_sample_count=processed_count,
        marker_count=marker_count,
        window_count=window_count,
        epoch_count=epoch_count,
        rejected_epoch_count=rejected_epoch_count,
        alpha_estimate_count=alpha_estimate_count,
        alpha_enabled=alpha_enabled,
        event_features_enabled=event_features_enabled,
        event_feature_packet_count=event_feature_packet_count,
        pending_epoch_count=0 if epocher is None else epocher.pending_count,
    )
    return 0


def _try_open_marker_inlet(pylsl: Any, config: dict[str, Any]) -> tuple[Any | None, dict[str, Any] | None]:
    marker_config = config.get("hardware", {}).get("markers", {})
    name = marker_config.get("lsl_stream_name", "ClosedLoopMarkers")
    stream_type = marker_config.get("lsl_stream_type", "Markers")
    source_id = marker_config.get("source_id")
    matches = [
        info
        for info in pylsl.resolve_streams(wait_time=0.05)
        if info.name() == name and info.type() == stream_type and (not source_id or info.source_id() == source_id)
    ]
    if len(matches) != 1:
        return None, None
    info = matches[0]
    inlet = pylsl.StreamInlet(
        info,
        max_buflen=60,
        max_chunklen=16,
        recover=True,
        processing_flags=lsl_processing_flags(pylsl, dejitter=False),
    )
    inlet.open_stream(timeout=0.2)
    return inlet, {
        "name": info.name(),
        "type": info.type(),
        "source_id": info.source_id(),
        "lsl_processing": ["clocksync", "monotonize"],
        "initial_time_correction_seconds": inlet_time_correction(inlet, timeout=0.2),
    }


def _pull_markers(marker_inlet: Any, path: str, telemetry: Telemetry | None = None) -> list[MarkerEvent]:
    samples, timestamps = marker_inlet.pull_chunk(timeout=0.0, max_samples=32)
    markers: list[MarkerEvent] = []
    for sample, timestamp in zip(samples, timestamps):
        label = sample[0] if isinstance(sample, list) else sample
        payload = {"lsl_timestamp": float(timestamp), "label": str(label)}
        append_jsonl(path, payload)
        if telemetry is not None:
            telemetry.emit(
                "realtime.marker_received",
                level="realtime",
                message=f"Marker received: {label}",
                metadata=payload,
            )
        markers.append(MarkerEvent(label=str(label), timestamp=float(timestamp), timebase="lsl", source="lsl"))
    return markers


def _stream_dict(info: Any) -> dict[str, Any]:
    return {
        "name": info.name(),
        "type": info.type(),
        "channel_count": info.channel_count(),
        "nominal_srate": info.nominal_srate(),
        "source_id": info.source_id(),
        "channel_names": _extract_channel_names(info),
    }


def _channel_names(stream_info: dict[str, Any], channel_count: int, eeg_config: dict[str, Any]) -> tuple[list[str], str]:
    names = stream_info.get("channel_names")
    if isinstance(names, list) and len(names) == channel_count:
        return mapped_channel_names([str(name) for name in names], eeg_config)
    return mapped_channel_names([f"ch_{idx + 1:03d}" for idx in range(channel_count)], eeg_config)


def _extract_channel_names(info: Any) -> list[str]:
    names: list[str] = []
    try:
        channel = info.desc().child("channels").child("channel")
        for _ in range(info.channel_count()):
            label = channel.child_value("label")
            names.append(label or f"ch_{len(names) + 1:03d}")
            channel = channel.next_sibling()
    except Exception:
        return []
    return names


def _advance_deadline(deadline: float, interval_seconds: float, now: float) -> float:
    """Advance a periodic deadline without accumulating loop-runtime drift."""
    interval = max(float(interval_seconds), 1e-6)
    next_deadline = float(deadline) + interval
    if next_deadline <= now:
        missed = int((now - next_deadline) // interval) + 1
        next_deadline += missed * interval
    return next_deadline


def _close_inlet(inlet: Any) -> None:
    try:
        inlet.close_stream()
    except Exception:
        pass


def _reference_for_artifact_gate(data: np.ndarray, preprocessing_config: dict[str, Any]) -> np.ndarray:
    values = np.asarray(data, dtype=float)
    if preprocessing_config.get("reference", "average") == "average" and values.ndim == 2 and values.shape[1] > 1:
        values = values - values.mean(axis=1, keepdims=True)
    if values.ndim == 2 and values.shape[0] > 0:
        # Enobio channels can carry large stable electrode offsets. Gate the
        # transient amplitude, not the DC offset, before causal filtering.
        values = values - np.nanmedian(values, axis=0, keepdims=True)
    return values


if __name__ == "__main__":
    raise SystemExit(main())
