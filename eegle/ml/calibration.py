"""Threshold calibration helpers for model training and reports."""

from __future__ import annotations

from typing import Any

import numpy as np


def binary_metrics_at_threshold(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    threshold: float,
    *,
    positive_label: str = "positive",
) -> dict[str, Any]:
    truth = np.asarray(y_true, dtype=int)
    probs = np.clip(np.asarray(probabilities, dtype=float), 0.0, 1.0)
    predicted = (probs >= float(threshold)).astype(int)
    tn = int(np.sum((truth == 0) & (predicted == 0)))
    fp = int(np.sum((truth == 0) & (predicted == 1)))
    fn = int(np.sum((truth == 1) & (predicted == 0)))
    tp = int(np.sum((truth == 1) & (predicted == 1)))
    recall_negative = tn / max(1, tn + fp)
    recall_positive = tp / max(1, tp + fn)
    precision_positive = tp / max(1, tp + fp)
    f1 = 2.0 * precision_positive * recall_positive / max(1e-12, precision_positive + recall_positive)
    positives = probs[truth == 1]
    negatives = probs[truth == 0]
    auc = None
    if positives.size and negatives.size:
        auc = float(np.mean([(p > n) + 0.5 * (p == n) for p in positives for n in negatives]))
    result = {
        "threshold": float(threshold),
        "operating_threshold": float(threshold),
        "accuracy": float(np.mean(predicted == truth)) if truth.size else 0.0,
        "balanced_accuracy": float((recall_negative + recall_positive) / 2.0),
        "roc_auc": auc,
        "positive_precision": float(precision_positive),
        "positive_recall": float(recall_positive),
        "positive_f1": float(f1),
        "negative_recall": float(recall_negative),
        "brier_score": float(np.mean((probs - truth) ** 2)) if truth.size else 0.0,
        "confusion_matrix": [[tn, fp], [fn, tp]],
        "sample_count": int(truth.size),
    }
    safe_label = _metric_label(positive_label)
    if safe_label:
        result[f"{safe_label}_precision"] = float(precision_positive)
        result[f"{safe_label}_recall"] = float(recall_positive)
        result[f"{safe_label}_f1"] = float(f1)
    return result


def select_binary_threshold(
    y_true: np.ndarray,
    probabilities: np.ndarray,
    *,
    metric: str = "balanced_accuracy",
    max_candidates: int = 512,
    positive_label: str = "positive",
) -> dict[str, Any]:
    truth = np.asarray(y_true, dtype=int)
    probs = np.clip(np.asarray(probabilities, dtype=float), 0.0, 1.0)
    if truth.size == 0 or len(set(truth.tolist())) < 2:
        return {"status": "not_run", "reason": "requires_both_classes", "selected_threshold": 0.5}
    candidates = threshold_candidates(probs, max_candidates=max_candidates)
    scored = [
        binary_metrics_at_threshold(truth, probs, threshold, positive_label=positive_label)
        for threshold in candidates
    ]
    best = max(scored, key=lambda row: (float(row.get(metric, 0.0)), -abs(float(row["threshold"]) - 0.5)))
    return {
        "status": "ok",
        "metric": metric,
        "selected_threshold": float(best["threshold"]),
        "selected_metrics": best,
        "candidate_count": len(candidates),
        "max_candidates": int(max_candidates),
    }


def threshold_candidates(probabilities: np.ndarray, *, max_candidates: int = 512) -> list[float]:
    """Return deterministic candidate thresholds without scaling with every trial forever."""
    probs = np.clip(np.asarray(probabilities, dtype=float), 0.0, 1.0)
    finite = probs[np.isfinite(probs)]
    if finite.size == 0:
        return [0.5]
    candidate_limit = max(2, int(max_candidates))
    unique = sorted({0.5, *[float(value) for value in finite.tolist()]})
    if len(unique) <= candidate_limit:
        return unique
    quantiles = np.linspace(0.0, 1.0, candidate_limit)
    reduced = {0.5, *[float(value) for value in np.quantile(finite, quantiles).tolist()]}
    return sorted(reduced)


def _metric_label(value: str) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in str(value).strip().lower())
    return "_".join(part for part in cleaned.split("_") if part)
