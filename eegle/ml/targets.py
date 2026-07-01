"""Training target generation for EEGle epoch datasets."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np


SUPPORTED_TARGETS = ("condition", "attention_lapse_binary", "attention_lapse_score")


@dataclass(frozen=True)
class TrainingTarget:
    name: str
    y: np.ndarray
    eligible: np.ndarray
    label_mapping: dict[str, int]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class AttentionScores:
    values: np.ndarray
    source: str
    score_name: str
    metadata: dict[str, Any]


def build_training_target(data: Any, config: dict[str, Any] | None = None) -> TrainingTarget:
    cfg = dict(config or {})
    target = str(cfg.get("target", "condition"))
    if target not in SUPPORTED_TARGETS:
        raise ValueError(f"unsupported training target '{target}'")
    y = np.asarray(data["y"], dtype=int)
    trials = np.asarray(npz_value(data, "trials", np.arange(y.size) + 1), dtype=int)
    if target == "condition":
        eligible = (y >= 0) & (trials >= 1)
        return TrainingTarget(
            name=target,
            y=y,
            eligible=eligible,
            label_mapping={"go": 0, "no_go": 1},
            metadata={"target": target, "source": "epochs_npz_y"},
        )

    score_result = _attention_scores_from_npz(data)
    if score_result is None:
        session_dirs = [Path(value).expanduser().resolve() for value in cfg.get("session_dirs", [])]
        score_result = _attention_scores_from_sessions(
            session_dirs,
            trials,
            _optional_int_array(data, "source_session_index"),
        )
    if score_result is None:
        raise ValueError(
            f"{target} requires lapse scores in epochs.npz or session_dirs with events/stimulus_manifest.json"
        )
    scores = score_result.values
    if scores.shape != trials.shape:
        raise ValueError(
            f"{target} score shape {scores.shape} does not match trial shape {trials.shape}"
        )
    threshold = cfg.get("attention_lapse_threshold")
    finite_scores = scores[np.isfinite(scores)]
    if threshold is None:
        threshold = 0.5 if finite_scores.size == 0 else float(np.median(finite_scores))
    labels = (scores >= float(threshold)).astype(int)
    eligible = np.isfinite(scores) & (trials >= 1)
    positive_count = int(np.sum(labels[eligible] == 1))
    negative_count = int(np.sum(labels[eligible] == 0))
    return TrainingTarget(
        name=target,
        y=labels,
        eligible=eligible,
        label_mapping={"attentive": 0, "attention_lapse": 1},
        metadata={
            "target": target,
            "source": score_result.source,
            "attention_lapse_threshold": float(threshold),
            "score_name": score_result.score_name,
            "score_count": int(np.sum(np.isfinite(scores))),
            "eligible_score_count": int(np.sum(eligible)),
            "positive_label_count": positive_count,
            "negative_label_count": negative_count,
            "learning_problem": "binary_classification",
            "score_target_mode": "thresholded_behavior_score",
            "score_regression_supported": False,
            "label_rule": f"{score_result.score_name} >= {float(threshold):.6g}",
            **score_result.metadata,
        },
    )


def _attention_scores_from_npz(data: Any) -> AttentionScores | None:
    for key in ("attention_lapse_score", "lapse_score"):
        if key in data.files:
            return AttentionScores(
                values=np.asarray(data[key], dtype=float),
                source="epochs_npz",
                score_name=key,
                metadata={"score_join": "per_epoch_npz_array"},
            )
    if "attention_lapse_binary" in data.files:
        return AttentionScores(
            values=np.asarray(data["attention_lapse_binary"], dtype=float),
            source="epochs_npz",
            score_name="attention_lapse_binary",
            metadata={"score_join": "per_epoch_npz_array", "binary_score_source": True},
        )
    return None


def _attention_scores_from_sessions(
    session_dirs: list[Path],
    trials: np.ndarray,
    source_session_index: np.ndarray | None,
) -> AttentionScores | None:
    if not session_dirs:
        return None
    if source_session_index is None and len(session_dirs) > 1:
        raise ValueError(
            "multi-session attention-lapse targets require per-epoch source_session_index provenance; "
            "regenerate combined training data with load_epoch_dataset or provide per-epoch lapse scores"
        )
    if source_session_index is not None and source_session_index.shape != trials.shape:
        raise ValueError("source_session_index shape does not match trials for attention-lapse target join")
    by_key: dict[tuple[int, int], float] = {}
    trial_counts: dict[int, int] = {}
    for session_index, session in enumerate(session_dirs):
        manifest = _load_json(session / "events" / "stimulus_manifest.json") or {}
        rows = _trial_lapse_rows(list(manifest.get("trials", [])))
        trial_counts[session_index] = len(rows)
        for row in rows:
            by_key[(session_index, int(row["trial"]))] = float(row["lapse_score"])
    scores = np.full(trials.shape, np.nan, dtype=float)
    for index, trial in enumerate(trials):
        session_index = 0 if source_session_index is None else int(source_session_index[index])
        if session_index < 0 or session_index >= len(session_dirs):
            raise ValueError(
                f"source_session_index {session_index} has no matching session_dir for attention-lapse target join"
            )
        key = (session_index, int(trial))
        if key in by_key:
            scores[index] = by_key[key]
    return AttentionScores(
        values=scores,
        source="session_behavior",
        score_name="lapse_score",
        metadata={
            "score_join": "session_index_and_trial",
            "session_count": len(session_dirs),
            "source_session_index_present": source_session_index is not None,
            "session_trial_counts": {str(key): value for key, value in trial_counts.items()},
        },
    )


def _trial_lapse_rows(trials: list[dict[str, Any]]) -> list[dict[str, Any]]:
    correct_go_rts = [
        float(dict(trial.get("response") or {}).get("reaction_time_seconds"))
        for trial in trials
        if not bool(dict(trial.get("stimulus") or {}).get("is_no_go"))
        and bool(dict(trial.get("response") or {}).get("correct_press"))
        and dict(trial.get("response") or {}).get("reaction_time_seconds") is not None
    ]
    slow_threshold = float(np.quantile(correct_go_rts, 0.75)) if correct_go_rts else math.inf
    rows = []
    for trial in trials:
        trial_index = int(trial.get("trial", -1))
        response = dict(trial.get("response") or {})
        stimulus = dict(trial.get("stimulus") or {})
        is_no_go = bool(stimulus.get("is_no_go"))
        rt = _optional_float(response.get("reaction_time_seconds"))
        correct = bool(response.get("correct_press"))
        rows.append(
            {
                "trial": trial_index,
                "is_no_go": int(is_no_go),
                "reaction_time_seconds": rt,
                "commission_error": int(is_no_go and int(response.get("button_press_count", 0) or 0) > 0),
                "omission_error": int(not is_no_go and int(response.get("button_press_count", 0) or 0) == 0),
                "slow_trial": int(not is_no_go and correct and rt is not None and rt >= slow_threshold),
            }
        )
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


def npz_value(data: Any, key: str, default: Any) -> Any:
    return data[key] if key in data.files else default


def _optional_int_array(data: Any, key: str) -> np.ndarray | None:
    if key not in data.files:
        return None
    return np.asarray(data[key], dtype=int)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
