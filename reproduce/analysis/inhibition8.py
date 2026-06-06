"""Causal replay validation and exploratory behavior analysis for inhibition8."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from reproduce.realtime.event_features import STAGE_DEFINITIONS, RealtimeEventEngine, read_engine_capture


VOLATILE_PACKET_FIELDS = {
    "packet_index",
    "feature_computed_monotonic",
    "processing_latency_ms",
    "publication_latency_ms",
}


def replay_realtime_session(session_dir: str | Path, tolerance: float = 1e-9) -> dict[str, Any]:
    """Replay exact captured chunks and compare staged packets with online output."""
    root = Path(session_dir).expanduser().resolve()
    outdir = root / "reports" / "realtime_features"
    outdir.mkdir(parents=True, exist_ok=True)
    replay_path = outdir / "replay_features.jsonl"
    comparison_path = outdir / "online_vs_replay.csv"
    summary_path = outdir / "replay_summary.json"
    capture_path = root / "realtime" / "engine_input.bin"
    online_path = root / "realtime" / "event_features.jsonl"
    if not capture_path.exists():
        return _write_json(summary_path, {"status": "missing", "reason": "engine_capture_missing", "capture_file": str(capture_path)})

    header, records = read_engine_capture(capture_path)
    engine = RealtimeEventEngine(
        dict(header.get("event_features_config") or {}),
        float(header["sample_rate_hz"]),
        [str(value) for value in header["channel_names"]],
    )
    replay_packets: list[dict[str, Any]] = []
    for kind, payload in records:
        if kind == "eeg":
            timestamps, data = payload
            replay_packets.extend(engine.process_chunk(timestamps, data))
        else:
            replay_packets.extend(engine.add_marker(payload))
    _write_jsonl(replay_path, replay_packets)
    online_packets = _load_jsonl(online_path)
    acceptance_online = _main_task_packets(online_packets)
    acceptance_replay = _main_task_packets(replay_packets)
    practice_online = _practice_packets(online_packets)
    practice_replay = _practice_packets(replay_packets)
    rows, material_differences = _compare_packets(acceptance_online, acceptance_replay, tolerance)
    online_keys = [(str(row.get("event_id")), str(row.get("stage"))) for row in acceptance_online]
    replay_keys = [(str(row.get("event_id")), str(row.get("stage"))) for row in acceptance_replay]
    duplicate_online = len(online_keys) - len(set(online_keys))
    duplicate_replay = len(replay_keys) - len(set(replay_keys))
    material_differences += duplicate_online + duplicate_replay
    online_replay_differences = material_differences
    manifest = _load_json(root / "events" / "stimulus_manifest.json") or {}
    expected_trials = {
        int(row["trial"])
        for row in manifest.get("trials", [])
        if isinstance(row, dict) and str(row.get("trial", "")).isdigit()
    }
    expected_stages = {stage.name for stage in STAGE_DEFINITIONS}
    expected_trial_stages = {(trial, stage) for trial in expected_trials for stage in expected_stages}
    online_trial_stages = _trial_stage_pairs(acceptance_online)
    replay_trial_stages = _trial_stage_pairs(acceptance_replay)
    missing_online_expected = sorted(expected_trial_stages - online_trial_stages)
    missing_replay_expected = sorted(expected_trial_stages - replay_trial_stages)
    unexpected_online = sorted(
        pair for pair in online_trial_stages if pair[1] not in expected_stages or (expected_trials and pair[0] not in expected_trials)
    )
    unexpected_replay = sorted(
        pair for pair in replay_trial_stages if pair[1] not in expected_stages or (expected_trials and pair[0] not in expected_trials)
    )
    material_differences += len(missing_online_expected) + len(missing_replay_expected) + len(unexpected_online) + len(unexpected_replay)
    for trial, stage in missing_online_expected:
        rows.append(
            {
                "event_id": f"trial:{trial}",
                "stage": stage,
                "status": "missing_expected_online",
                "max_feature_abs_difference": "",
                "details": "task_manifest_stage_missing_online",
            }
        )
    for trial, stage in missing_replay_expected:
        rows.append(
            {
                "event_id": f"trial:{trial}",
                "stage": stage,
                "status": "missing_expected_replay",
                "max_feature_abs_difference": "",
                "details": "task_manifest_stage_missing_replay",
            }
        )
    _write_csv(
        comparison_path,
        rows,
        ["event_id", "stage", "status", "max_feature_abs_difference", "details"],
    )
    adaptive_records = _adaptive_records(root / "realtime" / "decisions.jsonl")
    feedback_records = _count_nonempty_lines(root / "realtime" / "feedback.jsonl")
    status = "pass"
    reasons: list[str] = []
    if not acceptance_online:
        status = "analytically_invalid"
        reasons.append("online_feature_packets_missing")
    if online_replay_differences:
        status = "analytically_invalid"
        reasons.append("online_replay_material_divergence")
    if missing_online_expected or missing_replay_expected:
        status = "analytically_invalid"
        reasons.append("task_manifest_stage_coverage_incomplete")
    if unexpected_online or unexpected_replay:
        status = "analytically_invalid"
        reasons.append("unexpected_feature_stage_records")
    if adaptive_records:
        status = "analytically_invalid"
        reasons.append("adaptive_decision_record_detected")
    if feedback_records:
        status = "analytically_invalid"
        reasons.append("feedback_payload_detected")
    stage_counts: dict[str, int] = {}
    valid_stage_counts: dict[str, int] = {}
    reference_contamination: list[dict[str, Any]] = []
    warmup_invalid: list[dict[str, Any]] = []
    for packet in acceptance_online:
        stage = str(packet.get("stage", "unknown"))
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        if bool(packet.get("valid", False)):
            valid_stage_counts[stage] = valid_stage_counts.get(stage, 0) + 1
        if not bool(packet.get("reference_valid", False)):
            reference_contamination.append(
                {
                    "event_id": packet.get("event_id"),
                    "trial": packet.get("trial"),
                    "stage": stage,
                    "stage_deadline_lsl": packet.get("stage_deadline_lsl"),
                    "invalid_reference_channels": packet.get("invalid_reference_channels", []),
                }
            )
        if not bool(packet.get("filter_warmup_valid", False)):
            warmup_invalid.append({"event_id": packet.get("event_id"), "trial": packet.get("trial"), "stage": stage})
    quality_status = "degraded" if reference_contamination or warmup_invalid or sum(valid_stage_counts.values()) < len(acceptance_online) else "ok"
    return _write_json(
        summary_path,
        {
            "schema_version": 1,
            "status": status,
            "reasons": reasons,
            "session_dir": str(root),
            "capture_file": str(capture_path),
            "online_file": str(online_path),
            "replay_file": str(replay_path),
            "comparison_file": str(comparison_path),
            "online_packet_count": len(online_packets),
            "replay_packet_count": len(replay_packets),
            "acceptance_scope": "main_task_trials_trial_number_greater_than_or_equal_to_1",
            "acceptance_online_packet_count": len(acceptance_online),
            "acceptance_replay_packet_count": len(acceptance_replay),
            "excluded_practice_online_packet_count": len(practice_online),
            "excluded_practice_replay_packet_count": len(practice_replay),
            "excluded_practice_online_stages": _packet_stage_counts(practice_online),
            "excluded_practice_replay_stages": _packet_stage_counts(practice_replay),
            "material_difference_count": material_differences,
            "online_replay_difference_count": online_replay_differences,
            "duplicate_online_stage_count": duplicate_online,
            "duplicate_replay_stage_count": duplicate_replay,
            "expected_task_trial_count": len(expected_trials),
            "expected_task_stage_count": len(expected_trial_stages),
            "missing_expected_online_stages": [{"trial": trial, "stage": stage} for trial, stage in missing_online_expected],
            "missing_expected_replay_stages": [{"trial": trial, "stage": stage} for trial, stage in missing_replay_expected],
            "unexpected_online_stages": [{"trial": trial, "stage": stage} for trial, stage in unexpected_online],
            "unexpected_replay_stages": [{"trial": trial, "stage": stage} for trial, stage in unexpected_replay],
            "adaptive_record_count": len(adaptive_records),
            "feedback_record_count": feedback_records,
            "feature_tolerance": tolerance,
            "engine_metadata": engine.metadata_payload(),
            "stage_packet_counts": stage_counts,
            "valid_stage_packet_counts": valid_stage_counts,
            "quality_status": quality_status,
            "reference_contamination_packets": reference_contamination,
            "filter_warmup_invalid_packets": warmup_invalid,
        },
    )


def _trial_stage_pairs(packets: list[dict[str, Any]]) -> set[tuple[int, str]]:
    pairs: set[tuple[int, str]] = set()
    for packet in packets:
        try:
            trial = int(packet.get("trial"))
        except (TypeError, ValueError):
            continue
        pairs.add((trial, str(packet.get("stage", "unknown"))))
    return pairs


def _packet_trial(packet: dict[str, Any]) -> int | None:
    try:
        return int(packet.get("trial"))
    except (TypeError, ValueError):
        return None


def _main_task_packets(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [packet for packet in packets if (_packet_trial(packet) or 0) >= 1]


def _practice_packets(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [packet for packet in packets if (trial := _packet_trial(packet)) is not None and trial < 1]


def _packet_stage_counts(packets: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for packet in packets:
        stage = str(packet.get("stage", "unknown"))
        counts[stage] = counts.get(stage, 0) + 1
    return counts


def run_feature_behavior_analysis(session_dir: str | Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run explicitly exploratory classical analyses after replay acceptance."""
    root = Path(session_dir).expanduser().resolve()
    outdir = root / "reports" / "realtime_features"
    outdir.mkdir(parents=True, exist_ok=True)
    summary_path = outdir / "behavior_feature_summary.json"
    table_path = outdir / "trial_features.csv"
    replay = _load_json(outdir / "replay_summary.json") or {}
    if replay.get("status") != "pass":
        return _write_json(
            summary_path,
            {
                "status": "skipped",
                "reason": "causal_replay_not_accepted",
                "replay_status": replay.get("status", "missing"),
                "claim": "exploratory_feature_behavior_analysis_not_inhibition_decoding",
            },
        )
    packets = _load_jsonl(root / "realtime" / "event_features.jsonl")
    manifest = _load_json(root / "events" / "stimulus_manifest.json") or {}
    rows = _join_trial_features(packets, list(manifest.get("trials", [])))
    _write_trial_table(table_path, rows)
    cfg = dict(config or {})
    permutations = int(cfg.get("permutations", 100))
    analyses = {
        "trial_type_readiness_negative_control": _fit_exploratory_model(
            rows, ["readiness_alpha_power"], "is_no_go", "classification", permutations
        ),
        "trial_type_poststim_discrimination": _fit_exploratory_model(
            rows, ["early_theta_power", "n2_mean_uv", "p3_mean_uv", "alpha_erd_percent"], "is_no_go", "classification", permutations
        ),
        "correct_go_reaction_time_readiness": _fit_exploratory_model(
            [row for row in rows if not row["is_no_go"] and row["correct_press"] and row["reaction_time_seconds"] is not None],
            ["readiness_alpha_power"],
            "reaction_time_seconds",
            "regression",
            permutations,
        ),
        "correct_go_reaction_time_poststim_association": _fit_exploratory_model(
            [row for row in rows if not row["is_no_go"] and row["correct_press"] and row["reaction_time_seconds"] is not None],
            ["early_theta_power", "n2_mean_uv", "p3_mean_uv", "alpha_erd_percent"],
            "reaction_time_seconds",
            "regression",
            permutations,
        ),
        "slow_correct_go": _fit_exploratory_model(
            [row for row in rows if not row["is_no_go"] and row["correct_press"] and row["reaction_time_seconds"] is not None],
            ["readiness_alpha_power"],
            "slow_trial",
            "classification",
            permutations,
        ),
        "commission_error_no_go": _commission_model(rows, permutations),
        "lapse_score_readiness": _fit_exploratory_model(
            rows, ["readiness_alpha_power"], "lapse_score", "regression", permutations
        ),
    }
    return _write_json(
        summary_path,
        {
            "schema_version": 1,
            "status": "ok",
            "claim": "exploratory_feature_behavior_analysis_not_inhibition_decoding",
            "trial_count": len(rows),
            "complete_valid_trial_count": sum(bool(row.get("feature_packet_valid")) for row in rows),
            "slow_trial_definition": "upper_quartile_of_correct_go_reaction_times",
            "lapse_score_definition": "trailing_10_trial_mean_of_go_rt_percentile_omission_and_commission_indicators",
            "trial_table": str(table_path),
            "analyses": analyses,
        },
    )


def _compare_packets(
    online_packets: list[dict[str, Any]],
    replay_packets: list[dict[str, Any]],
    tolerance: float,
) -> tuple[list[dict[str, Any]], int]:
    online = {(str(row.get("event_id")), str(row.get("stage"))): row for row in online_packets}
    replay = {(str(row.get("event_id")), str(row.get("stage"))): row for row in replay_packets}
    rows: list[dict[str, Any]] = []
    material = 0
    for key in sorted(set(online) | set(replay)):
        left = online.get(key)
        right = replay.get(key)
        if left is None or right is None:
            status = "missing_online" if left is None else "missing_replay"
            rows.append({"event_id": key[0], "stage": key[1], "status": status, "max_feature_abs_difference": "", "details": status})
            material += 1
            continue
        feature_difference = _max_feature_difference(left.get("features", {}), right.get("features", {}))
        facts_match = all(
            left.get(field) == right.get(field)
            for field in ("roi_resolution", "feature_validity", "reference_valid", "filter_warmup_valid", "decision_eligibility")
        )
        status = "match" if facts_match and feature_difference <= tolerance else "different"
        if status != "match":
            material += 1
        rows.append(
            {
                "event_id": key[0],
                "stage": key[1],
                "status": status,
                "max_feature_abs_difference": feature_difference,
                "details": "facts_and_features_match" if status == "match" else "feature_or_validity_difference",
            }
        )
    return rows, material


def _max_feature_difference(left: dict[str, Any], right: dict[str, Any]) -> float:
    maximum = 0.0
    for key in set(left) | set(right):
        a, b = left.get(key), right.get(key)
        if a is None and b is None:
            continue
        try:
            difference = abs(float(a) - float(b))
        except (TypeError, ValueError):
            return math.inf
        maximum = max(maximum, difference)
    return maximum


def _join_trial_features(packets: list[dict[str, Any]], trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_trial: dict[int, dict[str, Any]] = {}
    for packet in packets:
        try:
            trial = int(packet.get("trial"))
        except (TypeError, ValueError):
            continue
        previous = by_trial.get(trial)
        if previous is None or int(packet.get("packet_index", 0)) >= int(previous.get("packet_index", 0)):
            by_trial[trial] = packet
    rows: list[dict[str, Any]] = []
    correct_go_rts = [
        float(dict(trial.get("response") or {}).get("reaction_time_seconds"))
        for trial in trials
        if not bool(dict(trial.get("stimulus") or {}).get("is_no_go"))
        and bool(dict(trial.get("response") or {}).get("correct_press"))
        and dict(trial.get("response") or {}).get("reaction_time_seconds") is not None
    ]
    slow_threshold = float(np.quantile(correct_go_rts, 0.75)) if correct_go_rts else math.inf
    for trial in trials:
        trial_index = int(trial.get("trial", -1))
        packet = by_trial.get(trial_index, {})
        response = dict(trial.get("response") or {})
        stimulus = dict(trial.get("stimulus") or {})
        features = dict(packet.get("features") or {})
        is_no_go = bool(stimulus.get("is_no_go"))
        rt = _optional_float(response.get("reaction_time_seconds"))
        correct = bool(response.get("correct_press"))
        row = {
            "trial": trial_index,
            "is_no_go": int(is_no_go),
            "reaction_time_seconds": rt,
            "correct_press": int(correct),
            "commission_error": int(is_no_go and int(response.get("button_press_count", 0) or 0) > 0),
            "omission_error": int(not is_no_go and int(response.get("button_press_count", 0) or 0) == 0),
            "slow_trial": int(not is_no_go and correct and rt is not None and rt >= slow_threshold),
            "feature_packet_valid": bool(packet.get("valid", False)),
            **{key: features.get(key) for key in (
                "readiness_alpha_power",
                "early_theta_power",
                "n2_mean_uv",
                "p3_mean_uv",
                "alpha_erd_percent",
            )},
        }
        rows.append(row)
    _add_lapse_scores(rows)
    return rows


def _add_lapse_scores(rows: list[dict[str, Any]]) -> None:
    go_rts = [row["reaction_time_seconds"] for row in rows if not row["is_no_go"] and row["reaction_time_seconds"] is not None]
    ordered = sorted(float(value) for value in go_rts)
    for index, row in enumerate(rows):
        start = max(0, index - 9)
        window = rows[start : index + 1]
        components: list[float] = []
        for item in window:
            rt = item.get("reaction_time_seconds")
            if not item["is_no_go"] and rt is not None and ordered:
                components.append(sum(value <= float(rt) for value in ordered) / len(ordered))
            components.append(float(item["omission_error"]))
            components.append(float(item["commission_error"]))
        row["lapse_score"] = float(np.mean(components)) if components else 0.0


def _commission_model(rows: list[dict[str, Any]], permutations: int) -> dict[str, Any]:
    no_go = [row for row in rows if row["is_no_go"]]
    errors = sum(int(row["commission_error"]) for row in no_go)
    correct = len(no_go) - errors
    if errors < 5 or correct < 5:
        return {"status": "skipped", "reason": "requires_at_least_5_error_and_5_non_error_no_go_trials", "errors": errors, "non_errors": correct}
    return _fit_exploratory_model(no_go, ["readiness_alpha_power"], "commission_error", "classification", permutations)


def _fit_exploratory_model(
    rows: list[dict[str, Any]],
    feature_names: list[str],
    target: str,
    kind: str,
    permutations: int,
) -> dict[str, Any]:
    complete = [
        row for row in rows
        if row.get("feature_packet_valid")
        and row.get(target) is not None
        and all(row.get(feature) is not None and np.isfinite(float(row[feature])) for feature in feature_names)
    ]
    minimum = max(10, len(feature_names) * 3)
    if len(complete) < minimum:
        return {"status": "skipped", "reason": "insufficient_complete_valid_trials", "n": len(complete), "minimum": minimum}
    x = np.asarray([[float(row[feature]) for feature in feature_names] for row in complete], dtype=float)
    y = np.asarray([float(row[target]) for row in complete], dtype=float)
    if kind == "classification" and np.unique(y).size < 2:
        return {"status": "skipped", "reason": "target_has_one_class", "n": len(complete)}
    blocks = np.array_split(np.arange(len(y)), min(5, len(y)))

    def blocked_predictions(target_values: np.ndarray) -> np.ndarray:
        predictions = np.full(target_values.shape, np.nan, dtype=float)
        for test_indices in blocks:
            train_indices = np.setdiff1d(np.arange(len(target_values)), test_indices)
            if train_indices.size < 2 or (kind == "classification" and np.unique(target_values[train_indices]).size < 2):
                continue
            predictions[test_indices], _coefficients = _fit_standardized_linear_model(
                x[train_indices],
                target_values[train_indices],
                x[test_indices],
                kind,
            )
        return predictions

    predictions = blocked_predictions(y)
    mask = np.isfinite(predictions)
    if int(np.sum(mask)) < minimum:
        return {"status": "skipped", "reason": "insufficient_valid_blocked_cv_folds", "n": len(complete)}
    score = _model_score(y[mask], predictions[mask], kind)
    rng = np.random.default_rng(97)
    null_scores: list[float] = []
    for _ in range(max(0, permutations)):
        shuffled = rng.permutation(y)
        shuffled_predictions = blocked_predictions(shuffled)
        shuffled_mask = np.isfinite(shuffled_predictions)
        if int(np.sum(shuffled_mask)) < minimum:
            continue
        null_scores.append(_model_score(shuffled[shuffled_mask], shuffled_predictions[shuffled_mask], kind))
    _full_predictions, coefficients = _fit_standardized_linear_model(x, y, x, kind)
    bootstrap_coefficients: list[np.ndarray] = []
    for _ in range(min(100, max(20, permutations))):
        indices = rng.integers(0, len(y), size=len(y))
        if kind == "classification" and np.unique(y[indices]).size < 2:
            continue
        _bootstrap_predictions, bootstrap_values = _fit_standardized_linear_model(x[indices], y[indices], x[indices], kind)
        bootstrap_coefficients.append(bootstrap_values)
    coefficient_intervals: dict[str, list[float]] = {}
    if bootstrap_coefficients:
        bootstrap_array = np.stack(bootstrap_coefficients, axis=0)
        for index, name in enumerate(feature_names):
            coefficient_intervals[name] = [
                float(np.quantile(bootstrap_array[:, index], 0.025)),
                float(np.quantile(bootstrap_array[:, index], 0.975)),
            ]
    p_value = None if not null_scores else float((1 + sum(value >= score for value in null_scores)) / (len(null_scores) + 1))
    return {
        "status": "ok",
        "interpretation": "exploratory_not_inhibition_decoding",
        "kind": kind,
        "target": target,
        "features": feature_names,
        "n": len(complete),
        "blocked_cv_folds": len(blocks),
        "score_name": "balanced_accuracy" if kind == "classification" else "r2",
        "score": score,
        "permutation_count": len(null_scores),
        "permutation_p_value": p_value,
        "standardized_coefficients": {name: float(value) for name, value in zip(feature_names, coefficients)},
        "standardized_coefficient_95pct_bootstrap_intervals": coefficient_intervals,
    }


def _fit_standardized_linear_model(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_test: np.ndarray,
    kind: str,
    *,
    regularization: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    mean = np.mean(x_train, axis=0)
    scale = np.std(x_train, axis=0)
    scale[scale < 1e-12] = 1.0
    train = (x_train - mean) / scale
    test = (x_test - mean) / scale
    if kind == "regression":
        target_mean = float(np.mean(y_train))
        centered_target = y_train - target_mean
        coefficients = np.linalg.solve(
            train.T @ train + regularization * np.eye(train.shape[1]),
            train.T @ centered_target,
        )
        return target_mean + test @ coefficients, np.asarray(coefficients, dtype=float)

    from scipy.optimize import minimize
    from scipy.special import expit

    design = np.column_stack((np.ones(train.shape[0]), train))
    test_design = np.column_stack((np.ones(test.shape[0]), test))
    classes, counts = np.unique(y_train, return_counts=True)
    class_weights = {float(label): len(y_train) / (len(classes) * int(count)) for label, count in zip(classes, counts)}
    weights = np.asarray([class_weights[float(value)] for value in y_train], dtype=float)

    def objective(parameters: np.ndarray) -> tuple[float, np.ndarray]:
        logits = design @ parameters
        loss = float(np.sum(weights * (np.logaddexp(0.0, logits) - y_train * logits)))
        loss += float(0.5 * regularization * np.sum(np.square(parameters[1:])))
        gradient = design.T @ (weights * (expit(logits) - y_train))
        gradient[1:] += regularization * parameters[1:]
        return loss, gradient

    result = minimize(
        lambda parameters: objective(parameters)[0],
        np.zeros(design.shape[1], dtype=float),
        jac=lambda parameters: objective(parameters)[1],
        method="L-BFGS-B",
    )
    parameters = np.asarray(result.x, dtype=float)
    predictions = (expit(test_design @ parameters) >= 0.5).astype(float)
    return predictions, parameters[1:]


def _model_score(expected: np.ndarray, predicted: np.ndarray, kind: str) -> float:
    if kind == "classification":
        recalls = [
            float(np.mean(predicted[expected == label] == label))
            for label in np.unique(expected)
            if np.any(expected == label)
        ]
        return float(np.mean(recalls)) if recalls else math.nan
    residual = float(np.sum(np.square(expected - predicted)))
    total = float(np.sum(np.square(expected - np.mean(expected))))
    return math.nan if total <= 1e-12 else float(1.0 - residual / total)


def _adaptive_records(path: Path) -> list[dict[str, Any]]:
    return [
        row
        for row in _load_jsonl(path)
        if any(str(action.get("action")) != "observe_only" for action in row.get("actions", []) if isinstance(action, dict))
    ]


def _write_trial_table(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["trial"]
    _write_csv(path, rows, fields)


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return payload


def _count_nonempty_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def _optional_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None
