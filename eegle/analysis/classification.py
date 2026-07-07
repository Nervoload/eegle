"""Evaluation and deterministic replay for Go/No-go EEG condition decoding."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from eegle.realtime.classification import (
    assess_epoch_quality,
    model_prediction_row,
    model_rejection_row,
    sanitize_model_metadata,
)
from eegle.realtime.epoching import EpochingConfig, extract_epoch_from_arrays, should_epoch_marker
from eegle.realtime.event_features import read_engine_capture
from eegle.realtime.models import (
    PreparedEpochCache,
    binary_classification_metrics,
    make_model_adapter,
    performance_warnings,
    positive_label_for_target,
    prediction_permutation_p_value,
)
from eegle.realtime.online_adaptation import (
    adaptation_update_row,
    apply_delayed_adaptation_update,
    snapshot_adapter_state,
)
from eegle.realtime.online_labels import OnlineAttentionLapseLabeler, trial_complete_from_event_record


@dataclass(frozen=True)
class ReplayResult:
    """Typed view of a deterministic classifier replay summary."""

    status: str
    difference_count: int = 0
    differences: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ReplayResult":
        return cls(
            status=str(payload.get("status", "unknown")),
            difference_count=int(payload.get("difference_count", 0) or 0),
            differences=list(payload.get("differences", [])),
            metadata=dict(payload),
        )

    def payload(self) -> dict[str, Any]:
        return {
            **self.metadata,
            "status": self.status,
            "difference_count": self.difference_count,
            "differences": self.differences,
        }


def evaluate_classifier_session(session_dir: str | Path) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
    outdir = root / "reports" / "classification"
    outdir.mkdir(parents=True, exist_ok=True)
    online = _load_jsonl(root / "realtime" / "model_predictions.jsonl")
    truth = _manifest_truth(root / "events" / "stimulus_manifest.json")
    lapse_truth = _manifest_lapse_truth(root / "events" / "stimulus_manifest.json")
    joined = []
    for row in online:
        trial = _optional_int(row.get("trial"))
        canonical = truth.get(trial) if trial is not None else None
        lapse = lapse_truth.get(trial) if trial is not None else None
        joined.append(
            {
                **row,
                "canonical_condition": None if canonical is None else canonical["condition"],
                "canonical_label": None if canonical is None else canonical["label"],
                "canonical_attention_lapse_label": None if lapse is None else lapse["label"],
                "canonical_attention_lapse_score": None if lapse is None else lapse["score"],
                "canonical_slow_go_rt": None if lapse is None else lapse.get("slow_go_rt"),
                "canonical_omission_error": None if lapse is None else lapse.get("omission_error"),
                "canonical_commission_error": None if lapse is None else lapse.get("commission_error"),
                "canonical_composite_lapse": None if lapse is None else lapse.get("composite_lapse"),
            }
        )
    _write_predictions_csv(outdir / "predictions.csv", joined)
    _write_probability_csv(outdir / "probability_by_trial.csv", joined)
    metrics: dict[str, Any] = {}
    for model_id in sorted({str(row.get("model_id")) for row in joined if row.get("model_id")}):
        model_rows = [row for row in joined if row.get("model_id") == model_id and row.get("status") == "predicted"]
        target = str((model_rows[0].get("target") if model_rows else "condition") or "condition")
        label_field = "canonical_attention_lapse_label" if target.startswith("attention_lapse") else "canonical_label"
        probability_field = "probability_attention_lapse" if target.startswith("attention_lapse") else "probability_no_go"
        rows = [
            row for row in model_rows
            if row.get(label_field) is not None
            and row.get(probability_field) is not None
        ]
        if not rows:
            metrics[model_id] = {"status": "missing_predictions"}
            _write_json(outdir / f"metrics_{_safe_name(model_id)}.json", metrics[model_id])
            continue
        truth_values = np.asarray([int(row[label_field]) for row in rows], dtype=int)
        probabilities = np.asarray([float(row[probability_field]) for row in rows], dtype=float)
        threshold_info = _operating_threshold_from_rows(rows)
        positive_label = positive_label_for_target(target)
        values = binary_classification_metrics(
            truth_values,
            probabilities,
            threshold=threshold_info["threshold"],
            positive_label=positive_label,
        )
        values["default_threshold_metrics"] = binary_classification_metrics(
            truth_values,
            probabilities,
            threshold=0.5,
            positive_label=positive_label,
        )
        values["permutation_p_value"] = prediction_permutation_p_value(
            truth_values,
            probabilities,
            threshold=threshold_info["threshold"],
            positive_label=positive_label,
        )
        truth_source = lapse_truth if target.startswith("attention_lapse") else truth
        predicted_trials = {int(row["trial"]) for row in rows if _optional_int(row.get("trial")) in truth_source}
        values["coverage"] = len(predicted_trials) / max(1, len(truth_source))
        values["false_alarms_per_minute"] = _false_alarms_per_minute(rows, truth_values, probabilities, threshold_info["threshold"])
        values["target"] = target
        values["evaluation_level"] = "full_inference"
        values["threshold_source"] = threshold_info["source"]
        values["threshold_values_seen"] = threshold_info["values_seen"]
        values["calibration_id"] = threshold_info["calibration_id"]
        values["warnings"] = performance_warnings(values)
        values["status"] = "ok"
        metrics[model_id] = values
        _write_json(outdir / f"metrics_{_safe_name(model_id)}.json", values)
        _write_confusion_matrix(outdir / f"confusion_matrix_{_safe_name(model_id)}.png", values["confusion_matrix"], model_id)
        if abs(float(values["operating_threshold"]) - 0.5) > 1e-12:
            _write_confusion_matrix(
                outdir / f"confusion_matrix_default_threshold_{_safe_name(model_id)}.png",
                values["default_threshold_metrics"]["confusion_matrix"],
                f"{model_id} @ 0.5",
            )
    adaptation_summary = _adaptation_evaluation_summary(root, joined)
    summary = {
        "schema_version": 1,
        "status": "ok" if metrics else "missing",
        "session_dir": str(root),
        "prediction_count": len(online),
        "canonical_trial_count": len(truth),
        "metrics": metrics,
        "adaptation": adaptation_summary,
        "files": {
            "predictions_csv": str(outdir / "predictions.csv"),
            "probability_by_trial_csv": str(outdir / "probability_by_trial.csv"),
            "metrics_json": str(outdir / "metrics.json"),
            "adaptation_updates_jsonl": str(root / "realtime" / "adaptation_updates.jsonl"),
        },
    }
    _write_json(outdir / "metrics.json", summary)
    return summary


def replay_classifier_session(session_dir: str | Path) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
    outdir = root / "reports" / "classification"
    outdir.mkdir(parents=True, exist_ok=True)
    capture_path = root / "realtime" / "engine_input.bin"
    summary_path = outdir / "replay_summary.json"
    if not capture_path.exists():
        return _write_json(summary_path, {"status": "missing", "reason": "classifier_capture_missing"})
    try:
        header, records = read_engine_capture(capture_path)
    except ValueError as exc:
        if "realtime engine capture" not in str(exc):
            raise
        return _write_json(
            summary_path,
            {
                "schema_version": 1,
                "status": "analytically_invalid",
                "reason": "classifier_capture_unreadable",
                "error": str(exc),
                "capture_file": str(capture_path),
            },
        )
    if str(header.get("mode")) != "classifier":
        return _write_json(summary_path, {"status": "missing", "reason": "capture_is_not_classifier_mode"})
    sample_rate = float(header["sample_rate_hz"])
    channels = [str(value) for value in header["channel_names"]]
    epoch_cfg = EpochingConfig.from_dict(header.get("epoching_config", {}))
    quality_cfg = dict(header.get("quality_gate", {}))
    models = [
        {
            **entry,
            "adapter": make_model_adapter(str(entry["kind"]), _replay_model_config(dict(entry["config"]), root)),
        }
        for entry in header.get("models", [])
    ]
    params = _load_json(root / "parameters.json") or {}
    realtime_cfg = dict(params.get("realtime", {}) or {})
    model_cfg = dict(realtime_cfg.get("model", {}) or {})
    adaptation_config = dict(header.get("adaptation_config") or realtime_cfg.get("adaptation", {}) or {})
    adaptation_enabled = bool(adaptation_config.get("enabled", False))
    trial_complete_events = _trial_complete_events(root / "events" / "events.jsonl") if adaptation_enabled else {}
    adaptation_labeler = (
        OnlineAttentionLapseLabeler(
            {
                **model_cfg,
                **adaptation_config,
                "label_mode": adaptation_config.get("label_mode", model_cfg.get("attention_lapse_label", "slow_go_rt")),
                "slow_rt_quantile": adaptation_config.get("slow_rt_quantile", model_cfg.get("slow_rt_quantile", 0.8)),
            }
        )
        if adaptation_enabled
        else None
    )
    replay_adaptation_rows: list[dict[str, Any]] = []
    replay_adaptation_state_dir = outdir / "replay_adaptation_state"
    if adaptation_enabled:
        replay_adaptation_state_dir.mkdir(parents=True, exist_ok=True)
    timestamps: list[np.ndarray] = []
    samples: list[np.ndarray] = []
    markers = []
    for kind, payload in records:
        if kind == "eeg":
            ts, values = payload
            timestamps.append(ts)
            samples.append(values)
        elif should_epoch_marker(payload, epoch_cfg):
            markers.append(payload)
    all_timestamps = np.concatenate(timestamps) if timestamps else np.empty((0,), dtype=float)
    all_samples = np.concatenate(samples, axis=0) if samples else np.empty((0, len(channels)), dtype=float)
    if epoch_cfg.data_source in {"processed", "causal_preprocessed"}:
        from eegle.realtime.preprocessing import CausalBandpassNotchPreprocessor

        preprocessor = CausalBandpassNotchPreprocessor(
            sample_rate,
            len(channels),
            dict(header.get("preprocessing_config") or {}),
        )
        all_timestamps, all_samples = preprocessor.process_chunk(all_timestamps, all_samples)
        sample_rate = preprocessor.output_sample_rate_hz
    replay_rows: list[dict[str, Any]] = []
    for index, marker in enumerate(markers, start=1):
        attempt = extract_epoch_from_arrays(all_timestamps, all_samples, marker, sample_rate, channels, epoch_cfg, index)
        if attempt.status != "ready" or attempt.epoch is None:
            rejection = model_rejection_row(attempt.payload(epoch_cfg), attempt.reason)
            replay_rows.append(rejection)
            if adaptation_enabled and adaptation_labeler is not None:
                replay_adaptation_rows.extend(
                    _replay_skipped_adaptation_rows(
                        trial=_optional_int(rejection.get("trial")),
                        models=models,
                        prediction_row=rejection,
                        events=trial_complete_events,
                        labeler=adaptation_labeler,
                        adaptation_config=adaptation_config,
                        state_dir=replay_adaptation_state_dir,
                        reason=f"epoch_{attempt.reason}",
                    )
                )
            continue
        epoch = attempt.epoch
        epoch_payload = epoch.metadata_payload()
        quality = assess_epoch_quality(epoch.data, quality_cfg)
        if not quality.valid:
            rejection = model_rejection_row(epoch_payload, ",".join(quality.reasons), quality.payload())
            replay_rows.append(rejection)
            if adaptation_enabled and adaptation_labeler is not None:
                replay_adaptation_rows.extend(
                    _replay_skipped_adaptation_rows(
                        trial=_optional_int(rejection.get("trial")),
                        models=models,
                        prediction_row=rejection,
                        events=trial_complete_events,
                        labeler=adaptation_labeler,
                        adaptation_config=adaptation_config,
                        state_dir=replay_adaptation_state_dir,
                        reason="quality_rejected_epoch",
                    )
                )
            continue
        model_metadata = sanitize_model_metadata(
            {**epoch_payload, "relative_times": epoch.relative_times.astype(float).tolist()}
        )
        prepared = PreparedEpochCache(epoch.data, sample_rate, channels, model_metadata)
        model_records: list[dict[str, Any]] = []
        for model_entry in models:
            adapter = model_entry["adapter"]
            if hasattr(adapter, "predict_prepared_epoch"):
                prediction = adapter.predict_prepared_epoch(prepared)
            else:
                prediction = adapter.predict_epoch(epoch.data, sample_rate, channels, model_metadata)
            row = model_prediction_row(
                epoch_payload,
                prediction.to_payload(),
                model_id=str(model_entry["id"]),
                role=str(model_entry["role"]),
                latency_ms=None,
                quality=quality.payload(),
                prediction_source="replay",
            )
            replay_rows.append(row)
            model_records.append(
                {
                    "adapter": adapter,
                    "prepared": prepared,
                    "prediction_row": row,
                    "model_id": str(model_entry["id"]),
                    "model_role": str(model_entry["role"]),
                    "model_kind": str(model_entry["kind"]),
                    "adaptation_config": dict(adaptation_config),
                }
            )
        if adaptation_enabled and adaptation_labeler is not None:
            replay_adaptation_rows.extend(
                _replay_adaptation_rows_for_trial(
                    _optional_int(epoch_payload.get("trial")),
                    model_records,
                    trial_complete_events,
                    adaptation_labeler,
                    adaptation_config,
                    replay_adaptation_state_dir,
                )
            )
    replay_path = outdir / "replay_predictions.jsonl"
    _write_jsonl(replay_path, replay_rows)
    replay_adaptation_path = outdir / "replay_adaptation_updates.jsonl"
    if adaptation_enabled:
        _write_jsonl(replay_adaptation_path, replay_adaptation_rows)
    online_rows = _load_jsonl(root / "realtime" / "model_predictions.jsonl")
    comparable_online_rows = _replay_comparable_rows(online_rows)
    differences = _prediction_differences(comparable_online_rows, replay_rows)
    adaptation_summary = _replay_adaptation_summary(root, adaptation_enabled, trial_complete_events, replay_adaptation_rows)
    status = "pass" if not differences and comparable_online_rows else "analytically_invalid"
    return _write_json(
        summary_path,
        {
            "schema_version": 1,
            "status": status,
            "online_prediction_count": len(comparable_online_rows),
            "online_skipped_count": len(online_rows) - len(comparable_online_rows),
            "replay_prediction_count": len(replay_rows),
            "difference_count": len(differences),
            "differences": differences[:100],
            "adaptation_replay": adaptation_summary,
            "capture_file": str(capture_path),
            "replay_file": str(replay_path),
            "replay_adaptation_file": str(replay_adaptation_path),
            "online_file": str(root / "realtime" / "model_predictions.jsonl"),
        },
    )


def _replay_comparable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("status") != "skipped"]


def _trial_complete_events(path: Path) -> dict[int, dict[str, Any]]:
    events: dict[int, dict[str, Any]] = {}
    for row in _load_jsonl(path):
        complete = trial_complete_from_event_record(row)
        if complete is None:
            continue
        trial = _optional_int(complete.get("trial"))
        if trial is not None:
            events[trial] = complete
    return events


def _replay_adaptation_rows_for_trial(
    trial: int | None,
    records: list[dict[str, Any]],
    events: dict[int, dict[str, Any]],
    labeler: OnlineAttentionLapseLabeler,
    adaptation_config: dict[str, Any],
    state_dir: Path,
) -> list[dict[str, Any]]:
    if trial is None:
        label = _missing_online_label(None, labeler, "missing_trial")
    elif trial not in events:
        label = _missing_online_label(trial, labeler, "missing_trial_complete_event")
    else:
        label = labeler.label_trial_complete(events[trial]).payload()
    rows = []
    snapshot_every = max(1, int(adaptation_config.get("snapshot_every_updates", 10)))
    for record in records:
        update_result = apply_delayed_adaptation_update(
            record["adapter"],
            record,
            label,
            events.get(trial, {"trial": trial}) if trial is not None else {},
            adaptation_config,
        )
        row = adaptation_update_row(
            trial=trial,
            model_id=str(record["model_id"]),
            model_role=str(record["model_role"]),
            model_kind=str(record["model_kind"]),
            prediction_row=dict(record.get("prediction_row") or {}),
            online_label=label,
            update_result=update_result,
            observe_only=True,
        )
        rows.append(row)
        if row.get("update_status") == "updated" and row.get("support_size_after") is not None:
            if int(row["support_size_after"]) % snapshot_every == 0:
                snapshot_adapter_state(record["adapter"], state_dir, str(record["model_id"]))
    for record in records:
        snapshot_adapter_state(record["adapter"], state_dir, str(record["model_id"]))
    return rows


def _replay_skipped_adaptation_rows(
    *,
    trial: int | None,
    models: list[dict[str, Any]],
    prediction_row: dict[str, Any],
    events: dict[int, dict[str, Any]],
    labeler: OnlineAttentionLapseLabeler,
    adaptation_config: dict[str, Any],
    state_dir: Path,
    reason: str,
) -> list[dict[str, Any]]:
    records = [
        {
            "adapter": model_entry["adapter"],
            "prediction_row": {
                **prediction_row,
                "model_id": str(model_entry["id"]),
                "model_role": str(model_entry["role"]),
                "model_kind": str(model_entry["kind"]),
            },
            "model_id": str(model_entry["id"]),
            "model_role": str(model_entry["role"]),
            "model_kind": str(model_entry["kind"]),
            "quality_rejected": True,
            "skip_reason": reason,
        }
        for model_entry in models
    ]
    return _replay_adaptation_rows_for_trial(trial, records, events, labeler, adaptation_config, state_dir)


def _missing_online_label(trial: int | None, labeler: OnlineAttentionLapseLabeler, reason: str) -> dict[str, Any]:
    return {
        "schema": "eegle.online_label.v1",
        "status": "skipped",
        "trial": trial,
        "label": None,
        "label_name": "attention_lapse",
        "label_mode": labeler.label_mode,
        "threshold_value": None,
        "reason": reason,
        "used_for_adaptation": False,
        "metadata": {},
    }


def _replay_adaptation_summary(
    root: Path,
    enabled: bool,
    events: dict[int, dict[str, Any]],
    replay_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not enabled:
        return {"status": "disabled"}
    if not events:
        return {"status": "missing_behavior_events", "replay_update_count": len(replay_rows)}
    live_rows = _load_jsonl(root / "realtime" / "adaptation_updates.jsonl")
    differences = _adaptation_update_differences(live_rows, replay_rows) if live_rows else []
    if live_rows:
        status = "pass" if not differences else "mismatch"
    else:
        status = "replay_only"
    return {
        "status": status,
        "trial_complete_event_count": len(events),
        "live_update_count": len(live_rows),
        "replay_update_count": len(replay_rows),
        "accepted_replay_update_count": sum(1 for row in replay_rows if row.get("update_status") == "updated"),
        "difference_count": len(differences),
        "differences": differences[:100],
        "final_state_hashes": {
            str(row.get("model_id")): row.get("calibration_state_hash_after")
            for row in replay_rows
            if row.get("model_id") and row.get("calibration_state_hash_after")
        },
    }


def _adaptation_update_differences(online: list[dict[str, Any]], replay: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(row: dict[str, Any]) -> tuple[Any, Any, Any]:
        return row.get("trial"), row.get("model_id"), row.get("model_role")

    online_map = {key(row): row for row in online}
    replay_map = {key(row): row for row in replay}
    differences: list[dict[str, Any]] = []
    fields = [
        "label_status",
        "true_online_label",
        "update_status",
        "skip_reason",
        "support_size_after",
        "class_counts_after",
        "calibration_state_hash_after",
    ]
    for item_key in sorted(set(online_map) | set(replay_map), key=str):
        left = online_map.get(item_key)
        right = replay_map.get(item_key)
        if left is None or right is None:
            differences.append({"key": list(item_key), "reason": "missing_adaptation_row"})
            continue
        for field in fields:
            if left.get(field) != right.get(field):
                differences.append({"key": list(item_key), "reason": "field_difference", "field": field})
                break
        else:
            left_threshold = _optional_float(left.get("calibration_threshold_after"))
            right_threshold = _optional_float(right.get("calibration_threshold_after"))
            if left_threshold is not None and right_threshold is not None and abs(left_threshold - right_threshold) > 1e-9:
                differences.append({"key": list(item_key), "reason": "threshold_difference"})
    return differences


def _prediction_differences(online: list[dict[str, Any]], replay: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(row: dict[str, Any]) -> tuple[Any, Any, Any, Any]:
        return row.get("status"), row.get("trial"), row.get("model_id"), row.get("epoch_index")

    online_map = {key(row): row for row in online}
    replay_map = {key(row): row for row in replay}
    differences: list[dict[str, Any]] = []
    for item_key in sorted(set(online_map) | set(replay_map), key=str):
        left = online_map.get(item_key)
        right = replay_map.get(item_key)
        if left is None or right is None:
            differences.append({"key": list(item_key), "reason": "missing_row"})
            continue
        left_label = left.get("predicted_condition") if left.get("predicted_condition") is not None else left.get("prediction_label")
        right_label = right.get("predicted_condition") if right.get("predicted_condition") is not None else right.get("prediction_label")
        if left_label != right_label:
            differences.append({"key": list(item_key), "reason": "label_difference"})
            continue
        left_probability = left.get("probability_no_go")
        right_probability = right.get("probability_no_go")
        if left_probability is None and right_probability is None:
            left_probability = left.get("probability_attention_lapse")
            right_probability = right.get("probability_attention_lapse")
        if left_probability is not None and right_probability is not None:
            tolerance = 1e-6 if left.get("model_kind") == "torch_eegnet" else 1e-9
            difference = abs(float(left_probability) - float(right_probability))
            if difference > tolerance:
                differences.append({"key": list(item_key), "reason": "probability_difference", "difference": difference})
    return differences


def _operating_threshold_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = []
    calibration_ids = []
    for row in rows:
        threshold = _optional_float(row.get("calibrated_threshold"))
        if threshold is not None:
            values.append(float(threshold))
        if row.get("calibration_id"):
            calibration_ids.append(str(row.get("calibration_id")))
    if not values:
        return {
            "threshold": 0.5,
            "source": "default_0.5",
            "values_seen": [],
            "calibration_id": None,
        }
    unique = sorted({round(value, 12) for value in values})
    return {
        "threshold": float(values[-1]),
        "source": "prediction_row_calibrated_threshold",
        "values_seen": unique[:10],
        "calibration_id": calibration_ids[-1] if calibration_ids else None,
    }


def _adaptation_evaluation_summary(root: Path, prediction_rows: list[dict[str, Any]]) -> dict[str, Any]:
    params = _load_json(root / "parameters.json") or {}
    adaptation_cfg = dict(dict(params.get("realtime", {}) or {}).get("adaptation", {}) or {})
    enabled = bool(adaptation_cfg.get("enabled", False))
    updates = _load_jsonl(root / "realtime" / "adaptation_updates.jsonl")
    if not enabled and not updates:
        return {"status": "disabled"}
    if not updates:
        return {"status": "missing_updates", "enabled": enabled}

    warmup = max(0, int(adaptation_cfg.get("warmup_trials", 20)))
    summary: dict[str, Any] = {
        "status": "ok",
        "enabled": enabled,
        "update_count": len(updates),
        "accepted_update_count": sum(1 for row in updates if row.get("update_status") == "updated"),
        "skipped_update_count": sum(1 for row in updates if row.get("update_status") != "updated"),
        "skipped_reasons": _count_values(row.get("skip_reason") for row in updates if row.get("skip_reason")),
        "warmup_trials": warmup,
        "models": {},
    }
    for model_id in sorted({str(row.get("model_id")) for row in updates if row.get("model_id")}):
        model_updates = [row for row in updates if str(row.get("model_id")) == model_id]
        model_predictions = [
            row
            for row in prediction_rows
            if row.get("model_id") == model_id
            and row.get("status") == "predicted"
            and str(row.get("target", "")).startswith("attention_lapse")
            and row.get("canonical_attention_lapse_label") is not None
            and row.get("probability_attention_lapse") is not None
        ]
        threshold_values = [
            float(value)
            for row in model_updates
            if (value := _optional_float(row.get("calibration_threshold_after"))) is not None
        ]
        model_summary: dict[str, Any] = {
            "model_kind": model_updates[-1].get("model_kind") if model_updates else None,
            "model_role": model_updates[-1].get("model_role") if model_updates else None,
            "update_count": len(model_updates),
            "accepted_update_count": sum(1 for row in model_updates if row.get("update_status") == "updated"),
            "skipped_update_count": sum(1 for row in model_updates if row.get("update_status") != "updated"),
            "skipped_reasons": _count_values(row.get("skip_reason") for row in model_updates if row.get("skip_reason")),
            "final_class_counts": model_updates[-1].get("class_counts_after") if model_updates else None,
            "final_calibration_threshold": threshold_values[-1] if threshold_values else None,
            "threshold_values_seen": sorted({round(value, 12) for value in threshold_values})[:20],
            "final_adaptation_state_hash": model_updates[-1].get("calibration_state_hash_after") if model_updates else None,
            "update_acceptance_rate": (
                sum(1 for row in model_updates if row.get("update_status") == "updated") / max(1, len(model_updates))
            ),
            "performance_by_trial_block": _attention_lapse_metrics_by_block(model_predictions),
            "frozen_initial_performance": _attention_lapse_subset_metrics(
                [row for row in model_predictions if (_optional_int(row.get("trial")) or 0) <= warmup]
            ),
            "adapted_after_warmup_performance": _attention_lapse_subset_metrics(
                [row for row in model_predictions if (_optional_int(row.get("trial")) or 0) > warmup]
            ),
        }
        summary["models"][model_id] = model_summary
    return summary


def _attention_lapse_metrics_by_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    blocks = {
        "1-20": lambda trial: 1 <= trial <= 20,
        "21-50": lambda trial: 21 <= trial <= 50,
        "51-100": lambda trial: 51 <= trial <= 100,
        "100+": lambda trial: trial > 100,
    }
    result = {}
    for name, predicate in blocks.items():
        subset = [row for row in rows if (trial := _optional_int(row.get("trial"))) is not None and predicate(trial)]
        result[name] = _attention_lapse_subset_metrics(subset)
    return result


def _attention_lapse_subset_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"status": "missing", "prediction_count": 0}
    truth_values = np.asarray([int(row["canonical_attention_lapse_label"]) for row in rows], dtype=int)
    probabilities = np.asarray([float(row["probability_attention_lapse"]) for row in rows], dtype=float)
    if len(set(truth_values.tolist())) < 2:
        return {
            "status": "single_class",
            "prediction_count": len(rows),
            "class_counts": _count_values(int(value) for value in truth_values.tolist()),
        }
    threshold_info = _operating_threshold_from_rows(rows)
    values = binary_classification_metrics(
        truth_values,
        probabilities,
        threshold=threshold_info["threshold"],
        positive_label="attention_lapse",
    )
    values["status"] = "ok"
    values["prediction_count"] = len(rows)
    values["threshold_source"] = threshold_info["source"]
    return values


def _count_values(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _manifest_truth(path: Path) -> dict[int, dict[str, Any]]:
    manifest = _load_json(path) or {}
    truth = {}
    for row in manifest.get("trials", []):
        trial = _optional_int(row.get("trial"))
        if trial is None or trial < 1:
            continue
        is_no_go = bool(dict(row.get("stimulus") or {}).get("is_no_go"))
        truth[trial] = {"condition": "no_go" if is_no_go else "go", "label": int(is_no_go)}
    return truth


def _manifest_lapse_truth(path: Path) -> dict[int, dict[str, Any]]:
    manifest = _load_json(path) or {}
    params = _load_json(path.parent.parent / "parameters.json") or {}
    model_cfg = dict(dict(params.get("realtime", {}) or {}).get("model", {}) or {})
    slow_rt_quantile = float(model_cfg.get("slow_rt_quantile", model_cfg.get("attention_lapse_slow_rt_quantile", 0.75)))
    primary_label = str(model_cfg.get("attention_lapse_label", model_cfg.get("attention_lapse_mode", "composite_lapse_score")))
    threshold_value = float(model_cfg.get("attention_lapse_threshold", 0.5))
    trials = [row for row in manifest.get("trials", []) if _optional_int(row.get("trial")) is not None]
    correct_go_rts = [
        float(dict(row.get("response") or {}).get("reaction_time_seconds"))
        for row in trials
        if not bool(dict(row.get("stimulus") or {}).get("is_no_go"))
        and bool(dict(row.get("response") or {}).get("correct_press"))
        and dict(row.get("response") or {}).get("reaction_time_seconds") is not None
    ]
    threshold = float(np.quantile(correct_go_rts, slow_rt_quantile)) if correct_go_rts else float("inf")
    rows = []
    for row in trials:
        trial = int(row.get("trial"))
        stimulus = dict(row.get("stimulus") or {})
        response = dict(row.get("response") or {})
        is_no_go = bool(stimulus.get("is_no_go"))
        rt = _optional_float(response.get("reaction_time_seconds"))
        correct = bool(response.get("correct_press"))
        rows.append(
            {
                "trial": trial,
                "is_no_go": int(is_no_go),
                "reaction_time_seconds": rt,
                "commission_error": int(is_no_go and int(response.get("button_press_count", 0) or 0) > 0),
                "omission_error": int(not is_no_go and int(response.get("button_press_count", 0) or 0) == 0),
                "slow_trial": int(not is_no_go and correct and rt is not None and rt >= threshold),
                "slow_go_rt": int(not is_no_go and correct and rt is not None and rt >= threshold),
            }
        )
    go_rts = [row["reaction_time_seconds"] for row in rows if not row["is_no_go"] and row["reaction_time_seconds"] is not None]
    ordered = sorted(float(value) for value in go_rts)
    result = {}
    for index, row in enumerate(rows):
        window = rows[max(0, index - 9) : index + 1]
        components: list[float] = []
        for item in window:
            rt = item.get("reaction_time_seconds")
            if not item["is_no_go"] and rt is not None and ordered:
                components.append(sum(value <= float(rt) for value in ordered) / len(ordered))
            components.append(float(item["omission_error"]))
            components.append(float(item["commission_error"]))
        score = float(np.mean(components)) if components else 0.0
        composite = int(bool(row.get("slow_go_rt")) or bool(row.get("omission_error")) or bool(row.get("commission_error")))
        primary_score = _lapse_primary_score({**row, "score": score, "composite_lapse": composite}, primary_label)
        result[int(row["trial"])] = {
            "score": score,
            "label": int(primary_score >= threshold_value),
            "primary_label": primary_label,
            "primary_score": float(primary_score),
            "slow_go_rt": int(row.get("slow_go_rt", 0)),
            "omission_error": int(row.get("omission_error", 0)),
            "commission_error": int(row.get("commission_error", 0)),
            "composite_lapse": composite,
            "slow_rt_quantile": slow_rt_quantile,
        }
    return result


def _lapse_primary_score(row: dict[str, Any], primary_label: str) -> float:
    label = str(primary_label)
    if label in {"slow_go_rt", "slow_trial", "attention_lapse_slow_rt"}:
        return float(row.get("slow_go_rt", row.get("slow_trial", 0.0)))
    if label in {"omission", "omission_error"}:
        return float(row.get("omission_error", 0.0))
    if label in {"commission", "commission_error"}:
        return float(row.get("commission_error", 0.0))
    if label in {"composite", "composite_lapse"}:
        return float(row.get("composite_lapse", 0.0))
    return float(row.get("score", 0.0))


def _replay_model_config(config: dict[str, Any], root: Path) -> dict[str, Any]:
    bundle_path = config.get("bundle_path")
    if not bundle_path:
        return config
    candidate = Path(str(bundle_path))
    snapshot = root / "realtime" / "models" / candidate.name
    if snapshot.exists():
        return {**config, "bundle_path": str(snapshot)}
    return config


def _write_predictions_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "trial", "epoch_index", "model_id", "model_role", "model_kind", "target", "status",
        "prediction_label", "predicted_condition", "probability_no_go", "probability_attention_lapse",
        "canonical_condition", "canonical_label", "canonical_attention_lapse_label",
        "canonical_slow_go_rt", "canonical_omission_error", "canonical_commission_error", "canonical_composite_lapse",
        "prediction_window_seconds", "prediction_horizon", "prediction_source", "support_size", "calibration_id",
        "calibration_state_hash", "processing_latency_ms", "preprocessing_latency_ms", "quality_status", "reason",
    ]
    _write_csv(path, rows, fields)


def _write_probability_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "trial", "model_id", "model_role", "model_kind", "target",
        "probability_no_go", "probability_attention_lapse", "canonical_label", "canonical_attention_lapse_label",
        "canonical_slow_go_rt", "canonical_omission_error", "canonical_commission_error", "canonical_composite_lapse",
    ]
    _write_csv(path, [row for row in rows if row.get("status") == "predicted"], fields)


def _write_confusion_matrix(path: Path, matrix: list[list[int]], title: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    figure, axis = plt.subplots(figsize=(4, 4))
    image = axis.imshow(np.asarray(matrix), cmap="Blues")
    for row in range(2):
        for column in range(2):
            axis.text(column, row, str(matrix[row][column]), ha="center", va="center")
    axis.set_xticks([0, 1], ["GO", "NO-GO"])
    axis.set_yticks([0, 1], ["GO", "NO-GO"])
    axis.set_xlabel("Predicted")
    axis.set_ylabel("Actual")
    axis.set_title(title)
    figure.colorbar(image, ax=axis)
    figure.tight_layout()
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _false_alarms_per_minute(
    rows: list[dict[str, Any]],
    truth_values: np.ndarray,
    probabilities: np.ndarray,
    threshold: float,
) -> float:
    predicted = (np.asarray(probabilities, dtype=float) >= float(threshold)).astype(int)
    false_alarms = int(np.sum((np.asarray(truth_values, dtype=int) == 0) & (predicted == 1)))
    timestamps = [
        value
        for row in rows
        if (value := _optional_float(row.get("marker_timestamp_lsl"))) is not None
    ]
    if len(timestamps) >= 2:
        minutes = max((max(timestamps) - min(timestamps)) / 60.0, 1e-9)
    else:
        minutes = max(len(rows) / 60.0, 1e-9)
    return float(false_alarms / minutes)


def _safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in value)
