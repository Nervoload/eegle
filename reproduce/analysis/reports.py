"""Post-session report generation."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from statistics import mean
from typing import Any

from reproduce.analysis.alpha import run_alpha_validation
from reproduce.analysis.erp import run_erp_analysis
from reproduce.analysis.html_summary import generate_experiment_html_report
from reproduce.analysis.inhibition8 import run_feature_behavior_analysis


def analyze_session(session_dir: str | Path) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
    behavior = root / "events" / "behavior.csv"
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    parameters = _load_json(root / "parameters.json") or {}
    erp_config = parameters.get("analysis", {}).get("erp", {})
    alpha_config = parameters.get("analysis", {}).get("alpha", {})
    event_features_enabled = bool(parameters.get("realtime", {}).get("event_features", {}).get("enabled", False))
    erp = run_erp_analysis(root, erp_config)
    alpha = run_alpha_validation(root, alpha_config)
    replay = _load_json(root / "reports" / "realtime_features" / "replay_summary.json") or {"status": "missing"}
    inhibition8_behavior = run_feature_behavior_analysis(
        root,
        parameters.get("analysis", {}).get("inhibition8", {}),
    ) if event_features_enabled else {"status": "disabled"}
    epochs = _epoch_summary(root / "realtime" / "epochs" / "manifest.json")
    if event_features_enabled and epochs.get("status") == "missing":
        epochs = {
            **epochs,
            "status": "replaced_by_staged_event_features",
            "reason": "legacy marker-locked epoch manifest is disabled for inhibition8",
        }
    html_report = _html_report_summary(root, parameters)
    summary = {
        "session_dir": str(root),
        "behavior_file": str(behavior),
        "behavior": _behavior_summary(behavior),
        "raw_eeg": _raw_eeg_summary(root / "raw" / "eeg.csv", root / "raw" / "eeg_metadata.json"),
        "realtime": _realtime_summary(root / "realtime" / "decisions.jsonl", root / "realtime" / "markers.jsonl"),
        "epochs": epochs,
        "processes": _process_summary(root / "logs" / "processes"),
        "erp": erp,
        "alpha": alpha,
        "realtime_feature_replay": replay,
        "exploratory_feature_behavior": inhibition8_behavior,
        "html_report": html_report,
        "analysis_status": {
            "power_bands": "pending_raw_eeg",
            "scalp_maps": "pending_raw_eeg",
            "classification": "pending_model_outputs",
            "bids_export": "pending_raw_eeg",
            "erp": erp.get("status"),
            "alpha": alpha.get("status"),
            "html_report": html_report.get("status"),
            "epochs": epochs.get("status"),
            "realtime_feature_replay": replay.get("status"),
            "realtime_feature_quality": replay.get("quality_status"),
            "exploratory_feature_behavior": inhibition8_behavior.get("status"),
        },
    }
    target = reports / "summary.json"
    with target.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")
    _refresh_session_summary(root / "session_summary.json", summary)
    return summary


def _refresh_session_summary(path: Path, analysis: dict[str, Any]) -> None:
    session = _load_json(path)
    if not session:
        return
    session["analysis"] = analysis
    with path.open("w", encoding="utf-8") as handle:
        json.dump(session, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _html_report_summary(root: Path, parameters: dict[str, Any]) -> dict[str, Any]:
    html_cfg = dict(parameters.get("analysis", {}).get("html", {}))
    if html_cfg.get("enabled", True) is False:
        return {"status": "disabled"}
    try:
        return generate_experiment_html_report(
            root,
            parameters,
            max_raw_points=int(html_cfg.get("max_raw_points", 120000)),
            max_alpha_points=int(html_cfg.get("max_alpha_points", 12000)),
        )
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _behavior_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "trials": 0}

    responses: list[float] = []
    misses = 0
    premature = 0
    too_fast = 0
    trials = set()
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            trial_index = None
            if row.get("trial"):
                try:
                    trial_index = int(row["trial"])
                    if trial_index >= 1:
                        trials.add(row["trial"])
                except ValueError:
                    trials.add(row["trial"])
            if row.get("label") == "response" and row.get("value"):
                try:
                    responses.append(float(row["value"]))
                except ValueError:
                    pass
            elif row.get("label") == "button_press" and (trial_index is None or trial_index >= 1):
                try:
                    metadata = json.loads(row.get("metadata") or "{}")
                    rt = metadata.get("rt_seconds")
                    if rt is not None:
                        responses.append(float(rt))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            if row.get("label") == "miss":
                misses += 1
            if row.get("label") == "premature_response":
                premature += 1
            if row.get("label") == "too_fast_response":
                too_fast += 1

    return {
        "status": "ok",
        "trials": len(trials),
        "responses": len(responses),
        "misses": misses,
        "premature_responses": premature,
        "too_fast_responses": too_fast,
        "mean_rt_seconds": mean(responses) if responses else None,
        "median_rt_seconds": _median(responses),
    }


def _raw_eeg_summary(raw_path: Path, metadata_path: Path) -> dict[str, Any]:
    if not raw_path.exists():
        metadata = _load_json(metadata_path)
        return {"status": "missing", "raw_file": str(raw_path), "metadata": metadata}

    rows = 0
    header = None
    first_timestamp = None
    last_timestamp = None
    with raw_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        for row in reader:
            rows += 1
            if row:
                try:
                    timestamp = float(row[0])
                    first_timestamp = timestamp if first_timestamp is None else first_timestamp
                    last_timestamp = timestamp
                except ValueError:
                    pass

    duration = None
    if first_timestamp is not None and last_timestamp is not None:
        duration = last_timestamp - first_timestamp

    return {
        "status": "ok" if rows > 0 else "empty",
        "raw_file": str(raw_path),
        "metadata_file": str(metadata_path),
        "sample_count": rows,
        "channel_count": max(0, len(header or []) - 2),
        "first_lsl_timestamp": first_timestamp,
        "last_lsl_timestamp": last_timestamp,
        "duration_seconds": duration,
        "metadata": _load_json(metadata_path),
    }


def _realtime_summary(decisions_path: Path, markers_path: Path) -> dict[str, Any]:
    decisions = _count_jsonl(decisions_path)
    markers = _count_jsonl(markers_path)
    latest = _last_jsonl(decisions_path)
    return {
        "status": "ok" if decisions else "missing",
        "decisions_file": str(decisions_path),
        "markers_file": str(markers_path),
        "decision_count": decisions,
        "marker_count": markers,
        "latest_decision": latest,
    }


def _epoch_summary(manifest_path: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    if not manifest:
        return {"status": "missing", "manifest_file": str(manifest_path)}
    return {
        "status": manifest.get("status", "unknown"),
        "manifest_file": str(manifest_path),
        "data_file": manifest.get("data_file"),
        "metadata_file": manifest.get("metadata_file"),
        "epoch_count": manifest.get("epoch_count"),
        "rejected_count": manifest.get("rejected_count"),
        "epoch_shape": manifest.get("epoch_shape"),
        "data_layout": manifest.get("data_layout"),
        "raw_file_unchanged": manifest.get("raw_file_unchanged"),
        "raw_sha256": manifest.get("raw_sha256"),
        "source": manifest.get("source"),
    }


def _process_summary(process_dir: Path) -> dict[str, Any]:
    statuses: dict[str, Any] = {}
    if not process_dir.exists():
        return {"status": "missing", "processes": statuses}
    for path in sorted(process_dir.glob("*.status.json")):
        statuses[path.name.removesuffix(".status.json")] = _compact_process_status(_load_json(path))
    failed = [name for name, status in statuses.items() if status and status.get("status") in {"failed", "killed", "unsupported"}]
    return {
        "status": "failed" if failed else "ok",
        "processes": statuses,
        "failed_processes": failed,
    }


def _compact_process_status(status: dict[str, Any] | None) -> dict[str, Any] | None:
    if status is None:
        return None
    compact = dict(status)
    compact.pop("summary", None)
    return compact


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def _last_jsonl(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    last = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                last = line
    if last is None:
        return None
    return json.loads(last)


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2
