"""Realtime processor worker process."""

from __future__ import annotations

import argparse
import json
import threading
from collections import deque
from dataclasses import asdict, dataclass
from time import monotonic, sleep
from typing import Any

import numpy as np

from eegle.config import load_config
from eegle.devices.lsl_eeg import _select_lsl_info
from eegle.hardware.profiles import mapped_channel_names
from eegle.lsl import inlet_time_correction, lsl_processing_flags
from eegle.ml.contracts import normalize_input_contract, validate_supported_resampling
from eegle.ml.registry import get_model_spec, resolve_model_kind
from eegle.realtime.alpha import AlphaPowerEstimator, load_alpha_config
from eegle.realtime.buffer import RingBuffer
from eegle.realtime.classification import (
    assess_epoch_quality,
    load_model_bundle,
    model_prediction_row,
    model_rejection_row,
    model_skip_row,
    sanitize_model_metadata,
    snapshot_model_bundle,
)
from eegle.realtime.epoching import EpochingConfig, MarkerEvent, RealtimeEpocher, expected_sample_count
from eegle.realtime.event_features import EngineInputCaptureWriter, RealtimeEventEngine
from eegle.realtime.models import PreparedEpochCache
from eegle.realtime.performance import (
    RealtimePerformanceConfig,
    RealtimePerformanceStats,
    buffer_utilization,
    elapsed_ms,
    performance_config_from,
)
from eegle.realtime.registry import make_feedback_emitter, make_model, make_policy, make_stream_preprocessor
from eegle.session import paths_for_existing_session
from eegle.telemetry import Telemetry, telemetry_config_from
from eegle.workers.common import JsonlWriter, QueuedJsonlWriter, StatusWriter, install_stop_signal_handlers


@dataclass
class InferenceWorkItem:
    epoch_payload: dict[str, Any]
    epoch: Any
    model_metadata: dict[str, Any]
    quality: dict[str, Any] | None
    queued_at_monotonic: float


@dataclass
class InferenceProcessResult:
    prediction_count: int = 0
    primary_prediction_count: int = 0
    skipped_shadow_count: int = 0


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
    performance_config = performance_config_from(config, channel_count)
    performance_stats = RealtimePerformanceStats()
    stream_info = _stream_dict(info)
    channel_names, mapping_source = _channel_names(stream_info, channel_count, eeg_config)
    stream_info["channel_names"] = channel_names
    stream_info["channel_mapping_source"] = mapping_source
    stream_info["large_cap_detected"] = performance_config.large_cap_detected
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
    inference_enabled = bool(realtime_config.get("inference", {}).get("enabled", True)) and not event_features_enabled
    preprocessor_kind = args.preprocessor or preprocessing_config.get("kind", "causal_bandpass_notch")
    preprocessor = None if event_features_enabled else make_stream_preprocessor(preprocessor_kind, sample_rate, channel_count, preprocessing_config)
    alpha_config = load_alpha_config(config, paths.root)
    alpha_enabled = bool(alpha_config.get("enabled", False))
    model_config = realtime_config.get("model", {})
    model_input_contract = normalize_input_contract(model_config, fallback_channel_names=channel_names)
    required_channels = [str(name) for name in model_input_contract.get("required_channels", [])]
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
    classifier_mode = bool(
        realtime_config.get("classifier", {}).get(
            "enabled",
            model_kind in {"erp_roi_logreg", "pyriemann_erp_cov", "torch_eegnet"} or bool(realtime_config.get("shadow_models")),
        )
    ) and not event_features_enabled
    model_entries = [] if event_features_enabled or not inference_enabled else _load_classifier_models(
        model_kind,
        model_config,
        list(realtime_config.get("shadow_models", [])),
        paths,
    )
    _validate_model_entries(model_entries, channel_names, sample_rate, epoching_config)
    model = None if not model_entries else model_entries[0]["adapter"]
    policy_config = dict(realtime_config.get("decision_policy", {}))
    policy_config.setdefault("allow_task_adaptation", bool(feedback_config.get("allow_task_adaptation", True)))
    policy_config.setdefault("allow_stimulation", bool(feedback_config.get("allow_stimulation", False)))
    policy_config.setdefault("research_safety_ack", bool(feedback_config.get("research_safety_ack", False)))
    policy_kind = "observe_only" if event_features_enabled else str(policy_config.get("kind", "conservative_p300"))
    policy = None if event_features_enabled or not inference_enabled else make_policy(policy_kind, policy_config)
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
    raw_timestamp_scratch = np.empty(raw_buffer_samples, dtype=float) if raw_buffer is not None else None
    raw_data_scratch = np.empty((raw_buffer_samples, channel_count), dtype=float) if raw_buffer is not None else None
    inference_queue: deque[InferenceWorkItem] = deque()
    epocher = RealtimeEpocher(epoching_config) if epoching_enabled else None
    event_engine = RealtimeEventEngine(event_features_config, sample_rate, channel_names) if event_features_enabled else None
    quality_config = dict(realtime_config.get("quality_gate", {}))
    classifier_capture_enabled = (
        classifier_mode
        and bool(realtime_config.get("capture", {}).get("enabled", epoching_enabled))
    )

    marker_inlet = None
    marker_stream = None
    marker_status = "pending"
    marker_count = 0
    eligible_marker_count = 0
    sample_count = 0
    processed_count = 0
    window_count = 0
    epoch_count = 0
    rejected_epoch_count = 0
    classifier_prediction_count = 0
    classifier_predicted_epoch_count = 0
    classifier_rejected_epoch_count = 0
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
    queued_writers: list[QueuedJsonlWriter] = []
    marker_writer = QueuedJsonlWriter(
        paths.realtime_markers_jsonl,
        flush_every=performance_config.writer_flush_every,
        flush_interval_seconds=performance_config.writer_flush_interval_seconds,
    )
    epoch_writer = QueuedJsonlWriter(
        paths.realtime_epochs_jsonl,
        flush_every=performance_config.writer_flush_every,
        flush_interval_seconds=performance_config.writer_flush_interval_seconds,
    )
    window_writer = QueuedJsonlWriter(
        paths.realtime_windows_jsonl,
        flush_every=performance_config.writer_flush_every,
        flush_interval_seconds=performance_config.writer_flush_interval_seconds,
    )
    decision_writer = QueuedJsonlWriter(
        paths.realtime_decisions_jsonl,
        flush_every=performance_config.writer_flush_every,
        flush_interval_seconds=performance_config.writer_flush_interval_seconds,
    )
    queued_writers.extend([marker_writer, epoch_writer, window_writer, decision_writer])
    model_prediction_writer = (
        QueuedJsonlWriter(
            paths.realtime_model_predictions_jsonl,
            flush_every=performance_config.writer_flush_every,
            flush_interval_seconds=performance_config.writer_flush_interval_seconds,
        )
        if epoching_enabled and classifier_mode
        else None
    )
    if model_prediction_writer is not None:
        queued_writers.append(model_prediction_writer)
    capture_writer = (
        EngineInputCaptureWriter(
            paths.realtime_engine_capture,
            _capture_header(
                sample_rate,
                channel_names,
                event_features_config,
                epoching_config,
                quality_config,
                model_entries,
                event_features_enabled,
            ),
        )
        if event_features_enabled or classifier_capture_enabled
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
            model_ids=[entry["id"] for entry in model_entries],
            inference_enabled=inference_enabled,
            classifier_mode=classifier_mode,
            decision_policy=policy_kind,
            feedback_backend=feedback_backend,
            alpha_enabled=alpha_enabled,
            alpha_config=alpha_config if alpha_enabled else None,
            event_features_enabled=event_features_enabled,
            event_feature_schema=None if event_engine is None else event_engine.metadata_payload().get("feature_schema_version"),
            performance=performance_stats.snapshot(
                writer_backlog=_writer_backlog(queued_writers),
                inference_queue_depth=len(inference_queue),
                buffer_utilization=buffer_utilization(buffer),
                raw_buffer_utilization=buffer_utilization(raw_buffer),
            ),
            performance_config=performance_config.payload(),
        )
        while not stop_event.is_set():
            loop_started = monotonic()
            pull_started = monotonic()
            samples, timestamps = inlet.pull_chunk(timeout=pull_timeout_seconds, max_samples=int(realtime_config.get("max_pull_samples", 128)))
            performance_stats.eeg_pull_time_ms = elapsed_ms(pull_started)
            if samples:
                preprocessing_started = monotonic()
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
                performance_stats.preprocessing_time_ms = elapsed_ms(preprocessing_started)

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
                    marker_events = _pull_markers(marker_inlet, marker_writer, telemetry)
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
                        if epocher.add_marker(marker):
                            eligible_marker_count += 1

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
                epoch_extraction_started = monotonic()
                latest_timestamp = raw_buffer.latest_timestamp
                oldest_marker = epocher.oldest_pending_timestamp
                required_seconds = epoching_config.duration_seconds
                if latest_timestamp is not None and oldest_marker is not None:
                    required_seconds = max(
                        required_seconds,
                        latest_timestamp - (oldest_marker + epoching_config.tmin_seconds) + epoching_config.sample_tolerance_seconds,
                    )
                required_samples = max(raw_epoch_samples, int(np.ceil(required_seconds * sample_rate)) + 2)
                assert raw_timestamp_scratch is not None and raw_data_scratch is not None
                raw_timestamps, raw_data = raw_buffer.window_into(required_samples, raw_timestamp_scratch, raw_data_scratch)
                ready_epochs, rejected_epochs = epocher.extract_ready(
                    raw_timestamps,
                    raw_data,
                    sample_rate,
                    channel_names,
                )
                performance_stats.epoch_extraction_time_ms = elapsed_ms(epoch_extraction_started)
                for attempt in rejected_epochs:
                    rejected_epoch_count += 1
                    rejected_payload = attempt.payload(epoching_config)
                    epoch_writer.write(rejected_payload)
                    if inference_enabled and classifier_mode:
                        assert model_prediction_writer is not None
                        model_prediction_writer.write(
                            model_rejection_row(rejected_payload, attempt.reason)
                        )
                        classifier_rejected_epoch_count += 1
                    telemetry.emit(
                        "realtime.epoch_rejected",
                        level="realtime",
                        message="Realtime epoch rejected",
                        metadata=rejected_payload,
                    )
                for epoch in ready_epochs:
                    epoch_count += 1
                    epoch_payload = epoch.metadata_payload()
                    epoch_writer.write(epoch_payload)
                    if not inference_enabled:
                        continue
                    quality = assess_epoch_quality(epoch.data, quality_config) if classifier_mode else None
                    if quality is not None and not quality.valid:
                        assert model_prediction_writer is not None
                        rejection = model_rejection_row(epoch_payload, ",".join(quality.reasons), quality.payload())
                        model_prediction_writer.write(rejection)
                        classifier_rejected_epoch_count += 1
                        telemetry.emit("realtime.epoch_rejected", level="realtime", message="Classifier epoch rejected", metadata=rejection)
                        continue
                    full_model_metadata = {**epoch_payload, "relative_times": epoch.relative_times.astype(float).tolist()}
                    model_metadata = sanitize_model_metadata(full_model_metadata) if classifier_mode else full_model_metadata
                    if len(inference_queue) >= performance_config.inference_queue_max_epochs:
                        if classifier_mode:
                            assert model_prediction_writer is not None
                            rejection = model_rejection_row(
                                epoch_payload,
                                "inference_queue_full",
                                None if quality is None else quality.payload(),
                            )
                            model_prediction_writer.write(rejection)
                            classifier_rejected_epoch_count += 1
                        telemetry.emit(
                            "realtime.inference_queue_full",
                            level="default",
                            message="Realtime inference queue is full; rejecting epoch",
                            metadata={
                                "epoch": epoch_payload,
                                "queue_depth": len(inference_queue),
                                "queue_limit": performance_config.inference_queue_max_epochs,
                            },
                        )
                        continue
                    inference_queue.append(
                        InferenceWorkItem(
                            epoch_payload=epoch_payload,
                            epoch=epoch,
                            model_metadata=model_metadata,
                            quality=None if quality is None else quality.payload(),
                            queued_at_monotonic=monotonic(),
                        )
                    )

            if inference_queue:
                result = _process_inference_item(
                    inference_queue.popleft(),
                    queue_depth=len(inference_queue),
                    model_entries=model_entries,
                    sample_rate=sample_rate,
                    channel_names=channel_names,
                    classifier_mode=classifier_mode,
                    model_prediction_writer=model_prediction_writer,
                    decision_writer=decision_writer,
                    emitter=emitter,
                    policy=policy,
                    sample_count=sample_count,
                    processed_count=processed_count,
                    marker_count=marker_count,
                    epoch_count=epoch_count,
                    performance_config=performance_config,
                    performance_stats=performance_stats,
                    telemetry=telemetry,
                )
                classifier_prediction_count += result.prediction_count
                classifier_predicted_epoch_count += result.primary_prediction_count

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
                window_writer.write(payload)
                decision_writer.write(payload)
                emitter.emit(payload)
                telemetry.emit(
                    "model.prediction",
                    level="realtime",
                    message=f"Realtime window prediction: {prediction.label} ({prediction.score:.3f})",
                    metadata=payload,
                )
                next_process_at = _advance_deadline(next_process_at, step_seconds, monotonic())

            _drain_writers(queued_writers)
            if monotonic() >= next_status_at:
                if capture_writer is not None:
                    capture_writer.flush()
                performance_payload = performance_stats.snapshot(
                    writer_backlog=_writer_backlog(queued_writers),
                    inference_queue_depth=len(inference_queue),
                    buffer_utilization=buffer_utilization(buffer),
                    raw_buffer_utilization=buffer_utilization(raw_buffer),
                )
                status.update(
                    "running",
                    eeg_stream=stream_info,
                    marker_status=marker_status,
                    marker_stream=marker_stream,
                    sample_count=sample_count,
                    processed_sample_count=processed_count,
                    marker_count=marker_count,
                    eligible_marker_count=eligible_marker_count,
                    window_count=window_count,
                    epoch_count=epoch_count,
                    rejected_epoch_count=rejected_epoch_count,
                    classifier_prediction_count=classifier_prediction_count,
                    classifier_predicted_epoch_count=classifier_predicted_epoch_count,
                    classifier_rejected_epoch_count=classifier_rejected_epoch_count,
                    alpha_estimate_count=alpha_estimate_count,
                    alpha_enabled=alpha_enabled,
                    event_features_enabled=event_features_enabled,
                    event_feature_packet_count=event_feature_packet_count,
                    pending_epoch_count=0 if epocher is None else epocher.pending_count,
                    buffer_samples=0 if buffer is None else len(buffer),
                    raw_buffer_samples=0 if raw_buffer is None else len(raw_buffer),
                    performance=performance_payload,
                    performance_config=performance_config.payload(),
                )
                next_status_at = _advance_deadline(next_status_at, 1.0, monotonic())
            if monotonic() >= next_health_event_at:
                performance_payload = performance_stats.snapshot(
                    writer_backlog=_writer_backlog(queued_writers),
                    inference_queue_depth=len(inference_queue),
                    buffer_utilization=buffer_utilization(buffer),
                    raw_buffer_utilization=buffer_utilization(raw_buffer),
                )
                telemetry.emit(
                    "eeg.sample_heartbeat",
                    level="realtime",
                    message="Realtime EEG heartbeat",
                    metadata={
                        "sample_count": sample_count,
                        "processed_sample_count": processed_count,
                        "marker_count": marker_count,
                        "eligible_marker_count": eligible_marker_count,
                        "window_count": window_count,
                        "epoch_count": epoch_count,
                        "rejected_epoch_count": rejected_epoch_count,
                        "classifier_prediction_count": classifier_prediction_count,
                        "classifier_predicted_epoch_count": classifier_predicted_epoch_count,
                        "classifier_rejected_epoch_count": classifier_rejected_epoch_count,
                        "alpha_estimate_count": alpha_estimate_count,
                        "alpha_enabled": alpha_enabled,
                        "event_features_enabled": event_features_enabled,
                        "event_feature_packet_count": event_feature_packet_count,
                        "buffer_samples": 0 if buffer is None else len(buffer),
                        "raw_buffer_samples": 0 if raw_buffer is None else len(raw_buffer),
                        "performance": performance_payload,
                    },
                )
                next_health_event_at = _advance_deadline(next_health_event_at, heartbeat_seconds, monotonic())
            performance_stats.record_loop(loop_started)
            _drain_writers(queued_writers)
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
        if inference_enabled and classifier_mode and epocher is not None and model_prediction_writer is not None:
            while inference_queue:
                pending = inference_queue.popleft()
                classifier_rejected_epoch_count += 1
                model_prediction_writer.write(
                    model_rejection_row(
                        pending.epoch_payload,
                        "worker_stopped_before_inference_completed",
                        pending.quality,
                    )
                )
            for attempt in epocher.reject_pending("worker_stopped_before_epoch_completed"):
                rejected_epoch_count += 1
                classifier_rejected_epoch_count += 1
                rejected_payload = attempt.payload(epoching_config)
                epoch_writer.write(rejected_payload)
                model_prediction_writer.write(model_rejection_row(rejected_payload, attempt.reason))
        for writer in queued_writers:
            writer.close()
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
        eligible_marker_count=eligible_marker_count,
        window_count=window_count,
        epoch_count=epoch_count,
        rejected_epoch_count=rejected_epoch_count,
        classifier_prediction_count=classifier_prediction_count,
        classifier_predicted_epoch_count=classifier_predicted_epoch_count,
        classifier_rejected_epoch_count=classifier_rejected_epoch_count,
        alpha_estimate_count=alpha_estimate_count,
        alpha_enabled=alpha_enabled,
        event_features_enabled=event_features_enabled,
        event_feature_packet_count=event_feature_packet_count,
        pending_epoch_count=0 if epocher is None else epocher.pending_count,
        performance=performance_stats.snapshot(
            writer_backlog=_writer_backlog(queued_writers),
            inference_queue_depth=len(inference_queue),
            buffer_utilization=buffer_utilization(buffer),
            raw_buffer_utilization=buffer_utilization(raw_buffer),
        ),
        performance_config=performance_config.payload(),
    )
    return 0


def _try_open_marker_inlet(pylsl: Any, config: dict[str, Any]) -> tuple[Any | None, dict[str, Any] | None]:
    marker_config = config.get("hardware", {}).get("markers", {})
    name = marker_config.get("lsl_stream_name", "EEGleMarkers")
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


def _pull_markers(marker_inlet: Any, writer: QueuedJsonlWriter, telemetry: Telemetry | None = None) -> list[MarkerEvent]:
    samples, timestamps = marker_inlet.pull_chunk(timeout=0.0, max_samples=32)
    markers: list[MarkerEvent] = []
    for sample, timestamp in zip(samples, timestamps):
        label = sample[0] if isinstance(sample, list) else sample
        payload = {"lsl_timestamp": float(timestamp), "label": str(label)}
        writer.write(payload)
        if telemetry is not None:
            telemetry.emit(
                "realtime.marker_received",
                level="realtime",
                message=f"Marker received: {label}",
                metadata=payload,
            )
        markers.append(MarkerEvent(label=str(label), timestamp=float(timestamp), timebase="lsl", source="lsl"))
    return markers


def _process_inference_item(
    item: InferenceWorkItem,
    *,
    queue_depth: int,
    model_entries: list[dict[str, Any]],
    sample_rate: float,
    channel_names: list[str],
    classifier_mode: bool,
    model_prediction_writer: QueuedJsonlWriter | None,
    decision_writer: QueuedJsonlWriter,
    emitter: Any,
    policy: Any,
    sample_count: int,
    processed_count: int,
    marker_count: int,
    epoch_count: int,
    performance_config: RealtimePerformanceConfig,
    performance_stats: RealtimePerformanceStats,
    telemetry: Telemetry,
) -> InferenceProcessResult:
    result = InferenceProcessResult()
    prepared = PreparedEpochCache(item.epoch.data, sample_rate, channel_names, item.model_metadata)
    primary_latency_ms: float | None = None
    max_shadow_latency_ms = 0.0
    for model_entry in _ordered_model_entries(model_entries):
        role = str(model_entry.get("role", "shadow"))
        model_id = str(model_entry.get("id", role))
        model_kind = str(model_entry.get("kind", "unknown"))
        if role != "primary" and _should_skip_shadow(queue_depth, primary_latency_ms, performance_config):
            if classifier_mode and model_prediction_writer is not None:
                model_prediction_writer.write(
                    model_skip_row(
                        item.epoch_payload,
                        model_id=model_id,
                        role=role,
                        model_kind=model_kind,
                        reason=_shadow_skip_reason(queue_depth, primary_latency_ms, performance_config),
                        quality=item.quality,
                        queue_depth=queue_depth,
                        primary_latency_ms=primary_latency_ms,
                    )
                )
            result.skipped_shadow_count += 1
            performance_stats.skipped_shadow_count += 1
            telemetry.emit(
                "model.shadow_skipped",
                level="realtime",
                message=f"Skipped shadow model {model_id}",
                metadata={
                    "model_id": model_id,
                    "model_kind": model_kind,
                    "queue_depth": queue_depth,
                    "primary_latency_ms": primary_latency_ms,
                },
            )
            continue

        processing_started = monotonic()
        adapter = model_entry["adapter"]
        if hasattr(adapter, "predict_prepared_epoch"):
            prediction = adapter.predict_prepared_epoch(prepared)
        else:
            prediction = adapter.predict_epoch(item.epoch.data, sample_rate, channel_names, item.model_metadata)
        model_latency_ms = elapsed_ms(processing_started)
        prediction.latency_ms = model_latency_ms
        row = None
        if classifier_mode:
            assert item.quality is not None and model_prediction_writer is not None
            row = model_prediction_row(
                item.epoch_payload,
                prediction.to_payload(),
                model_id=model_id,
                role=role,
                latency_ms=model_latency_ms,
                quality=item.quality,
            )
            model_prediction_writer.write(row)
            result.prediction_count += 1
            if role == "primary":
                result.primary_prediction_count += 1
        if role != "primary":
            max_shadow_latency_ms = max(max_shadow_latency_ms, model_latency_ms)
            telemetry.emit(
                "model.shadow_prediction",
                level="realtime",
                message=f"Shadow prediction {model_id}: {prediction.label} ({prediction.score:.3f})",
                metadata=row or prediction.to_payload(),
            )
            continue

        primary_latency_ms = elapsed_ms(item.queued_at_monotonic)
        performance_stats.primary_latency_ms = primary_latency_ms
        assert policy is not None
        actions = policy.decide(prediction, {**item.epoch_payload, "quality": item.quality})
        payload = {
            "schema_version": 1,
            "decision_source": "event_epoch",
            "window_index": None,
            "epoch_index": item.epoch.epoch_index,
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
            "processing_latency_ms": model_latency_ms,
            "primary_latency_ms": primary_latency_ms,
            "inference_queue_depth": queue_depth,
            "epoch": item.epoch_payload,
        }
        decision_writer.write(payload)
        emitter.emit(payload)
        telemetry.emit(
            "model.prediction",
            level="realtime",
            message=f"Realtime epoch prediction: {prediction.label} ({prediction.score:.3f})",
            metadata=payload,
        )
    performance_stats.shadow_latency_ms = max_shadow_latency_ms
    return result


def _ordered_model_entries(model_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(model_entries, key=lambda entry: 0 if str(entry.get("role")) == "primary" else 1)


def _should_skip_shadow(
    queue_depth: int,
    primary_latency_ms: float | None,
    performance_config: RealtimePerformanceConfig,
) -> bool:
    if int(queue_depth) >= performance_config.skip_shadows_when_queue_depth_gte:
        return True
    if primary_latency_ms is not None and primary_latency_ms >= performance_config.primary_latency_budget_ms:
        return True
    return False


def _shadow_skip_reason(
    queue_depth: int,
    primary_latency_ms: float | None,
    performance_config: RealtimePerformanceConfig,
) -> str:
    if int(queue_depth) >= performance_config.skip_shadows_when_queue_depth_gte:
        return "shadow_skipped_inference_queue_backlog"
    if primary_latency_ms is not None and primary_latency_ms >= performance_config.primary_latency_budget_ms:
        return "shadow_skipped_primary_latency_budget"
    return "shadow_skipped"


def _drain_writers(writers: list[QueuedJsonlWriter]) -> None:
    for writer in writers:
        writer.drain()


def _writer_backlog(writers: list[QueuedJsonlWriter]) -> int:
    return sum(writer.backlog for writer in writers)


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


def _load_classifier_models(
    primary_kind: str,
    primary_config: dict[str, Any],
    shadow_configs: list[Any],
    paths: Any,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    configured = [{**dict(primary_config), "id": "primary", "role": "primary", "kind": primary_kind}]
    for index, value in enumerate(shadow_configs, start=1):
        shadow = {"kind": str(value)} if isinstance(value, str) else dict(value)
        shadow.setdefault("id", f"shadow-{index}")
        shadow["role"] = "shadow"
        configured.append(shadow)
    for value in configured:
        raw_kind = str(value.get("kind", "erp_roi_logreg"))
        kind = raw_kind if raw_kind in {"erp_peak_baseline", "band_power_threshold"} else resolve_model_kind(raw_kind)
        role = str(value.get("role", "shadow"))
        model_id = str(value.get("id", f"{role}-{kind}"))
        if role == "primary":
            _validate_primary_model_allowed(kind)
        adapter_config = {key: item for key, item in value.items() if key not in {"id", "role", "kind"}}
        if adapter_config.get("bundle_path"):
            snapshot = snapshot_model_bundle(adapter_config["bundle_path"], paths.realtime_model_snapshots, model_id)
            adapter_config["bundle_path"] = snapshot["bundle_dir"]
        entries.append(
            {
                "id": model_id,
                "role": role,
                "kind": kind,
                "config": adapter_config,
                "adapter": make_model(kind, adapter_config),
            }
        )
    return entries


def _capture_header(
    sample_rate: float,
    channel_names: list[str],
    event_features_config: dict[str, Any],
    epoching_config: EpochingConfig,
    quality_config: dict[str, Any],
    model_entries: list[dict[str, Any]],
    event_features_enabled: bool,
) -> dict[str, Any]:
    if event_features_enabled:
        return {
            "schema_version": 1,
            "mode": "event_features",
            "sample_rate_hz": sample_rate,
            "channel_names": channel_names,
            "event_features_config": event_features_config,
        }
    return {
        "schema_version": 1,
        "mode": "classifier",
        "sample_rate_hz": sample_rate,
        "channel_names": channel_names,
        "epoching_config": asdict(epoching_config),
        "quality_gate": quality_config,
        "models": [
            {
                "id": entry["id"],
                "role": entry["role"],
                "kind": entry["kind"],
                "config": entry["config"],
            }
            for entry in model_entries
        ],
    }


def _validate_model_entries(
    entries: list[dict[str, Any]],
    channel_names: list[str],
    sample_rate_hz: float,
    epoching_config: EpochingConfig,
) -> None:
    runtime_window = [epoching_config.tmin_seconds, epoching_config.tmax_seconds]
    for entry in entries:
        bundle_path = entry["config"].get("bundle_path")
        if not bundle_path:
            continue
        bundle = load_model_bundle(bundle_path)
        contract = dict(bundle.get("contract") or {})
        normalized_contract = normalize_input_contract(contract, fallback_channel_names=channel_names)
        validate_supported_resampling(normalized_contract)
        missing = [name for name in normalized_contract.get("required_channels", []) if name not in channel_names]
        if missing:
            raise ValueError(f"{entry['id']} model-required channels missing from stream: {', '.join(missing)}")
        expected_rate = float(normalized_contract.get("sample_rate_hz", sample_rate_hz))
        if abs(expected_rate - sample_rate_hz) > float(normalized_contract.get("sample_rate_tolerance_hz", 0.01)):
            raise ValueError(f"{entry['id']} model sample rate {expected_rate:g} does not match stream {sample_rate_hz:g}")
        expected_window = normalized_contract.get("epoch_window_seconds")
        if expected_window and any(abs(float(a) - float(b)) > 0.01 for a, b in zip(expected_window, runtime_window)):
            raise ValueError(f"{entry['id']} model epoch window {expected_window} does not match runtime {runtime_window}")
        expected_samples = normalized_contract.get("sample_count")
        if expected_samples is not None:
            actual_samples = expected_sample_count(sample_rate_hz, epoching_config)
            if int(expected_samples) != int(actual_samples):
                raise ValueError(
                    f"{entry['id']} model sample count {int(expected_samples)} does not match runtime {actual_samples}"
                )
        if str(normalized_contract.get("input_units", "microvolts")).lower() not in {"microvolts", "uv", "µv", "v", "volt", "volts"}:
            raise ValueError(f"{entry['id']} model declares unsupported input units {normalized_contract.get('input_units')}")


def _validate_primary_model_allowed(kind: str) -> None:
    try:
        spec = get_model_spec(kind)
    except NotImplementedError:
        return
    if not spec.primary_realtime_allowed:
        raise ValueError(f"model '{kind}' is not allowed as the primary realtime model")


if __name__ == "__main__":
    raise SystemExit(main())
