"""Task-aware post-session analysis for Dynamic-State SART."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any

from eegle.analysis.dynamic_sart_labels import (
    compute_support_reference,
    load_dynamic_sart_trials,
    write_dynamic_sart_label_artifacts,
)
from eegle.tasks.dynamic_sart_schema import SUMMARY_SCHEMA


def analyze_dynamic_sart_session(session_dir: str | Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
    task_config = dict((config or {}).get("tasks", {}).get("dynamic_sart", {}))
    trials_path = root / "events" / "dynamic_sart_trials.jsonl"
    reference_path = root / "events" / "dynamic_sart_support_reference.json"
    results_path = root / "events" / "dynamic_sart_results.json"
    report_path = root / "reports" / "dynamic_sart_summary.json"
    timing_path = root / "reports" / "dynamic_sart_timing.csv"
    rows = load_dynamic_sart_trials(trials_path)
    if not rows:
        result = {
            "schema": SUMMARY_SCHEMA,
            "status": "missing",
            "reason": "dynamic_sart trial artifact not found or empty",
            "trials_file": str(trials_path),
        }
        _write_json_atomic(report_path, result)
        return result

    minimum_rt = float(task_config.get("minimum_valid_rt_seconds", 0.10))
    response_window = float(task_config.get("response_window_seconds", 1.15))
    saved_reference = _load_json(reference_path) or {}
    event_counts = _event_counts(root / "events" / "events.jsonl")
    support_complete_count = event_counts.get("dynamic_sart_support_complete", 0)
    boundary = saved_reference.get("created_at_support_boundary")
    regenerated = compute_support_reference(
        rows,
        minimum_valid_rt_seconds=minimum_rt,
        response_window_seconds=response_window,
        created_at_support_boundary=boundary,
    )
    reference_matches = bool(saved_reference) and saved_reference.get("reference_hash") == regenerated.get("reference_hash")
    if not saved_reference:
        saved_reference = regenerated
        reference_matches = support_complete_count == 1
        if reference_matches:
            _write_json_atomic(reference_path, regenerated)

    raw_hash_before = _sha256(trials_path)
    labels = write_dynamic_sart_label_artifacts(root, rows, saved_reference)
    raw_hash_after = _sha256(trials_path)
    experimental = [row for row in rows if not bool(row.get("is_practice")) and row.get("phase") in {"support", "query"}]
    practice = [row for row in rows if bool(row.get("is_practice")) or row.get("phase") == "practice"]
    outcomes = Counter(str(row.get("primary_outcome", "invalid")) for row in experimental)
    flags = {
        field: sum(int(bool(row.get(field))) for row in experimental)
        for field in (
            "commission_error",
            "omission_error",
            "premature_response",
            "too_fast_response",
            "multiple_response",
            "wrong_key_response",
            "late_response",
            "aborted",
            "invalid",
        )
    }
    go_rows = [row for row in experimental if row.get("condition") == "go"]
    no_go_rows = [row for row in experimental if row.get("condition") == "no_go"]
    valid_go_rts = [
        float(row["reaction_time_seconds"])
        for row in go_rows
        if row.get("reaction_time_seconds") is not None
        and not bool(row.get("too_fast_response"))
        and not bool(row.get("aborted"))
        and not bool(row.get("invalid"))
    ]
    measured_inter_onset_rows = [
        row for row in experimental if row.get("actual_trial_duration_seconds") is not None
    ]
    onset_schedule_drift = [
        float(row["actual_next_trial_onset_monotonic"])
        - float(row["scheduled_next_trial_onset_monotonic"])
        for row in measured_inter_onset_rows
        if row.get("actual_next_trial_onset_monotonic") is not None
        and row.get("scheduled_next_trial_onset_monotonic") is not None
        and not bool(row.get("planned_break_after_trial"))
        and not bool(row.get("probe_after"))
    ]
    response_close_overshoot = [
        float(row["response_window_close_overshoot_seconds"])
        for row in rows
        if row.get("response_window_close_overshoot_seconds") is not None
    ]
    legacy_timing_rows = sum(int(int(row.get("timing_semantics_version") or 1) < 2) for row in rows)
    _write_timing_rows(timing_path, rows)
    onset_count = event_counts.get("dynamic_sart_stimulus_onset", 0)
    presented_count = sum(int(bool(row.get("presented", True))) for row in rows)
    phase_counts = Counter(str(row.get("phase")) for row in experimental)
    block_counts = Counter(str(row.get("block_name")) for row in experimental)
    task_results = _load_json(results_path) or {}
    run_aborted = bool(task_results.get("aborted"))
    result = {
        "schema": SUMMARY_SCHEMA,
        "status": "ok" if reference_matches and raw_hash_before == raw_hash_after else "warn",
        "session_dir": str(root),
        "trials_file": str(trials_path),
        "timing_file": str(timing_path),
        "experimental_trial_count": len(experimental),
        "practice_trial_count": len(practice),
        "phase_counts": dict(sorted(phase_counts.items())),
        "block_counts": dict(sorted(block_counts.items())),
        "go_trial_count": len(go_rows),
        "no_go_trial_count": len(no_go_rows),
        "go_accuracy": _accuracy(go_rows),
        "no_go_accuracy": _accuracy(no_go_rows),
        "mean_valid_go_rt_seconds": sum(valid_go_rts) / len(valid_go_rts) if valid_go_rts else None,
        "median_valid_go_rt_seconds": median(valid_go_rts) if valid_go_rts else None,
        "timing_measurement": {
            "contract": "actual inter-onset intervals are reconstructed from consecutive flip-captured stimulus onsets",
            "measured_inter_onset_count": len(measured_inter_onset_rows),
            "terminal_or_missing_following_flip_count": len(experimental) - len(measured_inter_onset_rows),
            "legacy_scheduled_response_close_row_count": legacy_timing_rows,
            "mean_next_onset_schedule_drift_seconds": (
                sum(onset_schedule_drift) / len(onset_schedule_drift) if onset_schedule_drift else None
            ),
            "maximum_absolute_next_onset_schedule_drift_seconds": (
                max(abs(value) for value in onset_schedule_drift) if onset_schedule_drift else None
            ),
            "mean_response_window_close_overshoot_seconds": (
                sum(response_close_overshoot) / len(response_close_overshoot)
                if response_close_overshoot
                else None
            ),
            "maximum_response_window_close_overshoot_seconds": (
                max(response_close_overshoot) if response_close_overshoot else None
            ),
        },
        "primary_outcome_counts": dict(sorted(outcomes.items())),
        "flag_counts": flags,
        "valid_reaction_time_count": int(saved_reference.get("valid_support_go_count", 0)) + sum(
            int(row.get("phase") == "query" and row.get("primary_outcome") == "correct_go" and row.get("reaction_time_seconds") is not None and not row.get("too_fast_response"))
            for row in experimental
        ),
        "support_reference": saved_reference,
        "support_reference_regenerated_hash": regenerated.get("reference_hash"),
        "support_reference_stable": reference_matches,
        "raw_trials_unchanged_by_labels": raw_hash_before == raw_hash_after,
        "marker_trial_parity": {
            "stimulus_onset_event_count": onset_count,
            "presented_trial_count": presented_count,
            "matches": onset_count == presented_count,
        },
        "support_complete_event_count": support_complete_count,
        "task_end_event_count": event_counts.get("dynamic_sart_task_end", 0),
        "partial_run": run_aborted or event_counts.get("dynamic_sart_task_end", 0) == 0,
        "labels": labels,
        "warnings": [
            *list(saved_reference.get("warnings") or []),
            *([] if support_complete_count == 1 else ["support_phase_incomplete_reference_is_provisional"]),
            *([] if reference_matches else ["saved_support_reference_does_not_match_regeneration"]),
            *([] if raw_hash_before == raw_hash_after else ["raw_trial_artifact_changed_during_label_generation"]),
            *([] if onset_count == presented_count else ["stimulus_onset_and_presented_trial_counts_differ"]),
            *(
                []
                if legacy_timing_rows == 0
                else ["legacy_trial_rows_have_scheduled_not_observed_response_window_close_timing"]
            ),
        ],
    }
    _write_json_atomic(report_path, result)
    return result


def _accuracy(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    return sum(int(bool(row.get("correct"))) for row in rows) / len(rows)


def _event_counts(path: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    if not path.exists():
        return counts
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                label = str(json.loads(line).get("label", ""))
            except json.JSONDecodeError:
                continue
            family = label.split("__", 1)[0]
            counts[family] += 1
    return counts


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _write_timing_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = (
        "global_trial_index",
        "phase",
        "is_practice",
        "stimulus_onset_monotonic",
        "stimulus_onset_lsl",
        "scheduled_response_window_close_monotonic",
        "scheduled_response_window_close_lsl",
        "response_window_close_monotonic",
        "response_window_close_lsl",
        "response_window_close_overshoot_seconds",
        "scheduled_next_trial_onset_monotonic",
        "actual_next_trial_onset_monotonic",
        "actual_next_trial_onset_lsl",
        "actual_trial_duration_seconds",
        "actual_trial_duration_lsl_seconds",
        "next_onset_schedule_drift_seconds",
        "planned_break_after_trial",
        "probe_after",
        "timing_semantics_version",
        "timing_finalization_status",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            actual_next = row.get("actual_next_trial_onset_monotonic")
            scheduled_next = row.get("scheduled_next_trial_onset_monotonic")
            drift = (
                None
                if actual_next is None
                or scheduled_next is None
                or bool(row.get("planned_break_after_trial"))
                or bool(row.get("probe_after"))
                else float(actual_next) - float(scheduled_next)
            )
            payload = {field: row.get(field) for field in fields}
            payload["next_onset_schedule_drift_seconds"] = drift
            writer.writerow(
                {
                    field: "" if value is None else int(value) if isinstance(value, bool) else value
                    for field, value in payload.items()
                }
            )
    temporary.replace(path)
