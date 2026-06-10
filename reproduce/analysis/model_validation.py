"""Session-blocked classifier validation and threshold calibration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from reproduce.realtime.classification import assess_epoch_quality, prepare_classifier_epoch, sanitize_model_metadata
from reproduce.realtime.models import (
    binary_classification_metrics,
    make_model_adapter,
    npz_value,
    train_erp_roi_logreg,
    train_pyriemann_erp_cov,
    train_torch_eegnet,
    training_contract,
    training_probabilities,
)


PREPROCESSING_VARIANTS = {
    "no_reference_0p5_30": {"reference": "none", "filter_low_hz": 0.5, "filter_high_hz": 30.0},
    "robust_reference_0p5_30": {"reference": "robust", "filter_low_hz": 0.5, "filter_high_hz": 30.0},
    "average_reference_0p5_30": {"reference": "average", "filter_low_hz": 0.5, "filter_high_hz": 30.0},
}


def merge_epoch_files(epoch_files: list[str | Path], output_path: str | Path) -> Path:
    datasets = [_load_epoch_file(path) for path in epoch_files]
    _validate_compatible(datasets)
    target = Path(output_path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        target,
        X=np.concatenate([row["X"] for row in datasets], axis=0),
        y=np.concatenate([row["y"] for row in datasets], axis=0),
        trials=np.concatenate([row["trials"] for row in datasets], axis=0),
        times=datasets[0]["times"],
        channel_names=np.asarray(datasets[0]["channel_names"], dtype=object),
        sample_rate_hz=np.asarray([datasets[0]["sample_rate_hz"]]),
        source_session_index=np.concatenate([
            np.full(row["y"].shape, index, dtype=int) for index, row in enumerate(datasets)
        ]),
    )
    return target


def evaluate_preprocessing_loso(
    epoch_files: list[str | Path],
    kinds: list[str],
    model_config: dict[str, Any],
    output_path: str | Path,
) -> dict[str, Any]:
    datasets = [_load_epoch_file(path) for path in epoch_files]
    _validate_compatible(datasets)
    if len(datasets) < 2:
        return _write_json(output_path, {"status": "not_run", "reason": "requires_at_least_two_sessions"})
    variants: dict[str, Any] = {}
    for variant_name, overrides in PREPROCESSING_VARIANTS.items():
        cfg = {**model_config, **overrides}
        prepared = [_prepare_dataset(row, cfg) for row in datasets]
        kind_results = {}
        for kind in kinds:
            fold_rows = []
            probabilities = []
            truth = []
            for held_out in range(len(prepared)):
                training_x = np.concatenate([row["X"] for index, row in enumerate(prepared) if index != held_out], axis=0)
                training_y = np.concatenate([row["y"] for index, row in enumerate(prepared) if index != held_out], axis=0)
                validation_x = prepared[held_out]["X"]
                validation_y = prepared[held_out]["y"]
                if len(set(training_y.tolist())) < 2 or len(set(validation_y.tolist())) < 2:
                    fold_rows.append({"held_out_session": held_out, "status": "not_run", "reason": "fold_missing_class"})
                    continue
                try:
                    artifact = _train_kind(kind, training_x, training_y, datasets[0]["npz"], cfg, prepared[0]["contract"])
                    fold_probabilities = training_probabilities(
                        kind, artifact, validation_x, datasets[0]["npz"], cfg, prepared[0]["contract"]
                    )
                    metrics = binary_classification_metrics(validation_y, fold_probabilities)
                    fold_rows.append({"held_out_session": held_out, "status": "ok", "metrics": metrics})
                    probabilities.extend(fold_probabilities.tolist())
                    truth.extend(validation_y.tolist())
                except Exception as exc:
                    fold_rows.append({"held_out_session": held_out, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            kind_results[kind] = {
                "status": "ok" if probabilities else "not_run",
                "folds": fold_rows,
                "metrics": binary_classification_metrics(np.asarray(truth), np.asarray(probabilities)) if probabilities else None,
            }
        variants[variant_name] = {"config": overrides, "models": kind_results}
    return _write_json(output_path, {"status": "ok", "session_count": len(datasets), "variants": variants})


def calibrate_model_thresholds(
    model_configs: list[dict[str, Any]],
    epoch_files: list[str | Path],
    output_path: str | Path,
) -> dict[str, Any]:
    datasets = [_load_epoch_file(path) for path in epoch_files]
    _validate_compatible(datasets)
    results = {}
    for entry in model_configs:
        adapter_config = {**dict(entry["config"]), "input_layout": "channels_x_samples"}
        adapter = make_model_adapter(str(entry["kind"]), adapter_config)
        probabilities: list[float] = []
        truth: list[int] = []
        for data in datasets:
            metadata = {
                "relative_times": data["times"].tolist(),
                "epoch_window_seconds": [float(data["times"][0]), float(data["times"][-1])],
            }
            for epoch, label, trial in zip(data["X"], data["y"], data["trials"]):
                if int(label) < 0 or int(trial) < 1:
                    continue
                prediction = adapter.predict_epoch(
                    epoch,
                    data["sample_rate_hz"],
                    data["channel_names"],
                    sanitize_model_metadata(metadata),
                )
                if prediction.probability is not None:
                    probabilities.append(float(prediction.probability))
                    truth.append(int(label))
        threshold, metrics = optimal_balanced_threshold(np.asarray(truth, dtype=int), np.asarray(probabilities, dtype=float))
        results[str(entry["id"])] = {"threshold": threshold, "metrics": metrics, "sample_count": len(truth)}
    return _write_json(output_path, {"status": "ok", "models": results, "session_count": len(datasets)})


def optimal_balanced_threshold(y_true: np.ndarray, probabilities: np.ndarray) -> tuple[float, dict[str, Any]]:
    finite = np.isfinite(probabilities)
    y_true = np.asarray(y_true, dtype=int)[finite]
    probabilities = np.asarray(probabilities, dtype=float)[finite]
    if y_true.size == 0 or len(set(y_true.tolist())) < 2:
        raise ValueError("threshold calibration requires both GO and NO-GO labels")
    unique = np.unique(np.clip(probabilities, 0.0, 1.0))
    candidates = np.unique(np.concatenate(([0.0], unique, (unique[:-1] + unique[1:]) / 2.0, [1.0])))
    scored = [(float(_metrics_at_threshold(y_true, probabilities, value)["balanced_accuracy"]), -abs(float(value) - 0.5), float(value)) for value in candidates]
    threshold = max(scored)[2]
    return threshold, _metrics_at_threshold(y_true, probabilities, threshold)


def _metrics_at_threshold(y_true: np.ndarray, probabilities: np.ndarray, threshold: float) -> dict[str, Any]:
    metrics = binary_classification_metrics(y_true, probabilities, threshold=threshold)
    metrics["decision_probability"] = float(threshold)
    return metrics


def _prepare_dataset(data: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    contract = training_contract(data["npz"], config)
    metadata = {"relative_times": data["times"].tolist(), "epoch_window_seconds": contract["epoch_window_seconds"]}
    prepared, labels = [], []
    for epoch, label, trial in zip(data["X"], data["y"], data["trials"]):
        if int(label) < 0 or int(trial) < 1:
            continue
        values = prepare_classifier_epoch(epoch, data["sample_rate_hz"], data["channel_names"], metadata, contract)[0]
        if assess_epoch_quality(values.T, config.get("quality_gate", {})).valid:
            prepared.append(values)
            labels.append(int(label))
    if not prepared:
        raise ValueError("quality gate rejected every eligible epoch in a session")
    return {"X": np.stack(prepared), "y": np.asarray(labels, dtype=int), "contract": contract}


def _train_kind(kind: str, x: np.ndarray, y: np.ndarray, data: Any, config: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    if kind == "erp_roi_logreg":
        return train_erp_roi_logreg(x, y, data, config, contract)
    if kind == "pyriemann_erp_cov":
        return train_pyriemann_erp_cov(x, y, data, config, contract)
    if kind == "torch_eegnet":
        return train_torch_eegnet(x, y, data, config, contract)
    raise NotImplementedError(kind)


def _load_epoch_file(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    data = np.load(target, allow_pickle=True)
    return {
        "path": str(target),
        "npz": data,
        "X": np.asarray(data["X"], dtype=float),
        "y": np.asarray(data["y"], dtype=int),
        "trials": np.asarray(npz_value(data, "trials", np.arange(len(data["y"])) + 1), dtype=int),
        "times": np.asarray(data["times"], dtype=float),
        "channel_names": [str(value) for value in data["channel_names"]],
        "sample_rate_hz": float(np.asarray(data["sample_rate_hz"]).ravel()[0]),
    }


def _validate_compatible(datasets: list[dict[str, Any]]) -> None:
    if not datasets:
        raise ValueError("at least one epoch session is required")
    first = datasets[0]
    for row in datasets[1:]:
        if row["channel_names"] != first["channel_names"] or row["X"].shape[1:] != first["X"].shape[1:]:
            raise ValueError("epoch sessions must have identical channels and epoch shapes")
        if abs(row["sample_rate_hz"] - first["sample_rate_hz"]) > 0.01:
            raise ValueError("epoch sessions must have identical sample rates")


def _write_json(path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload["report_file"] = str(target)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload
