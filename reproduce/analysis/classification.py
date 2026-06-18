"""Evaluation and deterministic replay for Go/No-go EEG condition decoding."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import numpy as np

from reproduce.realtime.classification import (
    assess_epoch_quality,
    model_prediction_row,
    model_rejection_row,
    sanitize_model_metadata,
)
from reproduce.realtime.epoching import EpochingConfig, extract_epoch_from_arrays, should_epoch_marker
from reproduce.realtime.event_features import read_engine_capture
from reproduce.realtime.models import (
    binary_classification_metrics,
    make_model_adapter,
    performance_warnings,
    prediction_permutation_p_value,
)


def evaluate_classifier_session(session_dir: str | Path) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
    outdir = root / "reports" / "classification"
    outdir.mkdir(parents=True, exist_ok=True)
    online = _load_jsonl(root / "realtime" / "model_predictions.jsonl")
    truth = _manifest_truth(root / "events" / "stimulus_manifest.json")
    joined = []
    for row in online:
        trial = _optional_int(row.get("trial"))
        canonical = truth.get(trial) if trial is not None else None
        joined.append(
            {
                **row,
                "canonical_condition": None if canonical is None else canonical["condition"],
                "canonical_label": None if canonical is None else canonical["label"],
            }
        )
    _write_predictions_csv(outdir / "predictions.csv", joined)
    _write_probability_csv(outdir / "probability_by_trial.csv", joined)
    metrics: dict[str, Any] = {}
    for model_id in sorted({str(row.get("model_id")) for row in joined if row.get("model_id")}):
        rows = [
            row for row in joined
            if row.get("model_id") == model_id
            and row.get("status") == "predicted"
            and row.get("canonical_label") is not None
            and row.get("probability_no_go") is not None
        ]
        if not rows:
            metrics[model_id] = {"status": "missing_predictions"}
            _write_json(outdir / f"metrics_{_safe_name(model_id)}.json", metrics[model_id])
            continue
        values = binary_classification_metrics(
            np.asarray([int(row["canonical_label"]) for row in rows], dtype=int),
            np.asarray([float(row["probability_no_go"]) for row in rows], dtype=float),
        )
        values["permutation_p_value"] = prediction_permutation_p_value(
            np.asarray([int(row["canonical_label"]) for row in rows], dtype=int),
            np.asarray([float(row["probability_no_go"]) for row in rows], dtype=float),
        )
        predicted_trials = {int(row["trial"]) for row in rows if _optional_int(row.get("trial")) in truth}
        values["coverage"] = len(predicted_trials) / max(1, len(truth))
        values["warnings"] = performance_warnings(values)
        values["status"] = "ok"
        metrics[model_id] = values
        _write_json(outdir / f"metrics_{_safe_name(model_id)}.json", values)
        _write_confusion_matrix(outdir / f"confusion_matrix_{_safe_name(model_id)}.png", values["confusion_matrix"], model_id)
    summary = {
        "schema_version": 1,
        "status": "ok" if metrics else "missing",
        "session_dir": str(root),
        "prediction_count": len(online),
        "canonical_trial_count": len(truth),
        "metrics": metrics,
        "files": {
            "predictions_csv": str(outdir / "predictions.csv"),
            "probability_by_trial_csv": str(outdir / "probability_by_trial.csv"),
            "metrics_json": str(outdir / "metrics.json"),
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
    header, records = read_engine_capture(capture_path)
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
    replay_rows: list[dict[str, Any]] = []
    for index, marker in enumerate(markers, start=1):
        attempt = extract_epoch_from_arrays(all_timestamps, all_samples, marker, sample_rate, channels, epoch_cfg, index)
        if attempt.status != "ready" or attempt.epoch is None:
            replay_rows.append(model_rejection_row(attempt.payload(epoch_cfg), attempt.reason))
            continue
        epoch = attempt.epoch
        epoch_payload = epoch.metadata_payload()
        quality = assess_epoch_quality(epoch.data, quality_cfg)
        if not quality.valid:
            replay_rows.append(model_rejection_row(epoch_payload, ",".join(quality.reasons), quality.payload()))
            continue
        model_metadata = sanitize_model_metadata(
            {**epoch_payload, "relative_times": epoch.relative_times.astype(float).tolist()}
        )
        for model_entry in models:
            prediction = model_entry["adapter"].predict_epoch(epoch.data, sample_rate, channels, model_metadata)
            replay_rows.append(
                model_prediction_row(
                    epoch_payload,
                    prediction.to_payload(),
                    model_id=str(model_entry["id"]),
                    role=str(model_entry["role"]),
                    latency_ms=None,
                    quality=quality.payload(),
                )
            )
    replay_path = outdir / "replay_predictions.jsonl"
    _write_jsonl(replay_path, replay_rows)
    online_rows = _load_jsonl(root / "realtime" / "model_predictions.jsonl")
    comparable_online_rows = _replay_comparable_rows(online_rows)
    differences = _prediction_differences(comparable_online_rows, replay_rows)
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
            "capture_file": str(capture_path),
            "replay_file": str(replay_path),
            "online_file": str(root / "realtime" / "model_predictions.jsonl"),
        },
    )


def _replay_comparable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("status") != "skipped"]


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
        if left.get("predicted_condition") != right.get("predicted_condition"):
            differences.append({"key": list(item_key), "reason": "label_difference"})
            continue
        left_probability = left.get("probability_no_go")
        right_probability = right.get("probability_no_go")
        if left_probability is not None and right_probability is not None:
            tolerance = 1e-6 if left.get("model_kind") == "torch_eegnet" else 1e-9
            difference = abs(float(left_probability) - float(right_probability))
            if difference > tolerance:
                differences.append({"key": list(item_key), "reason": "probability_difference", "difference": difference})
    return differences


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
        "trial", "epoch_index", "model_id", "model_role", "model_kind", "status",
        "predicted_condition", "probability_no_go", "canonical_condition", "canonical_label",
        "processing_latency_ms", "reason",
    ]
    _write_csv(path, rows, fields)


def _write_probability_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = ["trial", "model_id", "model_role", "model_kind", "probability_no_go", "canonical_label"]
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


def _safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in value)
