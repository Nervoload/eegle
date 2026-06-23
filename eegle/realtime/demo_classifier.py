"""Transparent marker-driven classifier simulation for public demonstrations."""

from __future__ import annotations

import math
import random
from datetime import datetime
from typing import Any

from eegle.realtime.epoching import DEFAULT_MARKER_PREFIX, parse_marker_label


DEMO_PREDICTION_SCHEMA = "eegle.demo_prediction.v1"
DEMO_DISCLOSURE = (
    "Predicting type of stimulus signal--GO or NO-GO--from ERP in realtime."
)


def demo_prediction_from_marker(
    label: str,
    marker_timestamp_lsl: float,
    config: dict[str, Any],
    rng: random.Random,
) -> dict[str, Any] | None:
    """Create one intentionally imperfect, clearly labeled demo prediction."""
    marker_prefix = str(config.get("marker_prefix", DEFAULT_MARKER_PREFIX))
    parsed = parse_marker_label(label, marker_prefix)
    trial = parsed.get("trial")
    condition = _condition(parsed.get("condition"))
    if not isinstance(trial, int) or trial < 1 or condition is None:
        return None

    actual_stimulus = {
        "condition": condition,
        "shape": str(parsed.get("shape") or ("x" if condition == "no_go" else "circle")),
        "color": str(parsed.get("color") or ("white" if condition == "no_go" else "blue")),
    }
    error_rate = min(1.0, max(0.0, float(config.get("error_rate", 0.1))))
    intentionally_wrong = rng.random() < error_rate
    predicted_condition = _opposite(condition) if intentionally_wrong else condition
    guessed_stimulus = (
        _wrong_stimulus(predicted_condition, config, rng)
        if intentionally_wrong
        else dict(actual_stimulus)
    )
    probability_no_go = (
        rng.uniform(0.72, 0.96)
        if predicted_condition == "no_go"
        else rng.uniform(0.04, 0.28)
    )
    confidence = probability_no_go if predicted_condition == "no_go" else 1.0 - probability_no_go
    return {
        "schema": DEMO_PREDICTION_SCHEMA,
        "status": "predicted",
        "source": "psychopy_lsl_marker_demo",
        "simulated": True,
        "disclosure": DEMO_DISCLOSURE,
        "trial": trial,
        "marker_label": label,
        "marker_timestamp_lsl": float(marker_timestamp_lsl),
        "predicted_at": datetime.now().isoformat(timespec="milliseconds"),
        "prediction_delay_seconds": float(config.get("prediction_delay_seconds", 1.2)),
        "configured_error_rate": error_rate,
        "intentionally_wrong": intentionally_wrong,
        "actual_condition": condition,
        "predicted_condition": predicted_condition,
        "is_correct": predicted_condition == condition,
        "actual_stimulus": actual_stimulus,
        "guessed_stimulus": guessed_stimulus,
        "probability_no_go": probability_no_go,
        "confidence": confidence,
        "erp_window": _illustrative_erp_window(condition, trial, int(config.get("seed", 42))),
    }


def demo_config_from(config: dict[str, Any]) -> dict[str, Any]:
    """Return normalized dashboard demo settings from a session config."""
    dashboard = dict(config.get("realtime", {}).get("dashboard", {}))
    demo = dict(dashboard.get("demo", {}))
    task = dict(config.get("tasks", {}).get("go_nogo", {}))
    epoching = dict(config.get("realtime", {}).get("epoching", {}))
    return {
        "enabled": bool(demo.get("enabled", False)),
        "prediction_delay_seconds": max(0.0, float(demo.get("prediction_delay_seconds", 1.2))),
        "error_rate": min(1.0, max(0.0, float(demo.get("error_rate", 0.1)))),
        "seed": int(demo.get("seed", 42)),
        "marker_prefix": str(demo.get("marker_prefix", epoching.get("marker_prefix", DEFAULT_MARKER_PREFIX))),
        "shapes": list(task.get("shapes", ["circle", "square", "triangle", "star", "hexagon"])),
        "colors": list(task.get("colors", ["red", "blue", "green", "yellow", "magenta"])),
        "no_go": dict(task.get("no_go", {"shape": "x", "color": "white"})),
    }


def _illustrative_erp_window(condition: str, trial: int, seed: int) -> dict[str, Any]:
    rng = random.Random(seed + trial * 7919)
    times_ms = list(range(-200, 801, 20))
    amplitudes = []
    n2_scale = -4.6 if condition == "no_go" else -2.2
    p3_scale = 7.2 if condition == "no_go" else 4.4
    for time_ms in times_ms:
        drift = 0.45 * math.sin((time_ms + trial * 17) / 115.0)
        n2 = n2_scale * math.exp(-0.5 * ((time_ms - 265.0) / 62.0) ** 2)
        p3 = p3_scale * math.exp(-0.5 * ((time_ms - 455.0) / 105.0) ** 2)
        amplitudes.append(round(drift + n2 + p3 + rng.gauss(0.0, 0.42), 3))
    return {
        "kind": "illustrative_toy_erp",
        "label": "Illustrative ERP window",
        "times_ms": times_ms,
        "amplitude_uv": amplitudes,
        "window_seconds": [-0.2, 0.8],
        "not_measured_from_eeg": True,
    }


def _wrong_stimulus(condition: str, config: dict[str, Any], rng: random.Random) -> dict[str, str]:
    if condition == "no_go":
        no_go = dict(config.get("no_go", {}))
        return {
            "condition": "no_go",
            "shape": str(no_go.get("shape", "x")),
            "color": str(no_go.get("color", "white")),
        }
    shapes = [str(value) for value in config.get("shapes", [])] or ["circle", "square", "triangle"]
    colors = [str(value) for value in config.get("colors", [])] or ["red", "blue", "green"]
    no_go = dict(config.get("no_go", {}))
    combinations = [
        (shape, color)
        for shape in shapes
        for color in colors
        if not (shape == str(no_go.get("shape", "x")) and color == str(no_go.get("color", "white")))
    ]
    shape, color = rng.choice(combinations)
    return {"condition": "go", "shape": shape, "color": color}


def _condition(value: Any) -> str | None:
    normalized = str(value or "").lower()
    if normalized in {"no_go", "nogo"}:
        return "no_go"
    if normalized == "go":
        return "go"
    return None


def _opposite(condition: str) -> str:
    return "go" if condition == "no_go" else "no_go"
