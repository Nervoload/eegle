"""Leakage-safe behavioral derivations for Dynamic-State SART."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from eegle.tasks.dynamic_sart_schema import LABEL_CONTRACT_SCHEMA, LABELS_SCHEMA, SUPPORT_REFERENCE_SCHEMA


def load_dynamic_sart_trials(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return []
    if target.suffix.lower() == ".jsonl":
        rows = []
        with target.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
        return reconstruct_dynamic_sart_timing(rows)
    with target.open("r", encoding="utf-8", newline="") as handle:
        return reconstruct_dynamic_sart_timing([_coerce_csv_row(row) for row in csv.DictReader(handle)])


def reconstruct_dynamic_sart_timing(trials: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive actual inter-onset timing only from consecutive measured stimulus flips."""

    rows = [dict(row) for row in trials]
    for index, row in enumerate(rows):
        semantics_version = int(row.get("timing_semantics_version") or 1)
        if semantics_version < 2:
            # Version 1 wrote logical deadlines into fields whose names implied an
            # observation. Preserve those values only as schedules.
            legacy_close = _optional_float(row.get("response_window_close_monotonic"))
            legacy_close_lsl = _optional_float(row.get("response_window_close_lsl"))
            legacy_next = _optional_float(row.get("next_trial_onset_monotonic"))
            row.setdefault("scheduled_response_window_close_monotonic", legacy_close)
            row.setdefault("scheduled_response_window_close_lsl", legacy_close_lsl)
            row.setdefault("scheduled_next_trial_onset_monotonic", legacy_next)
            row["legacy_response_window_close_monotonic"] = legacy_close
            row["legacy_response_window_close_lsl"] = legacy_close_lsl
            row["response_window_close_monotonic"] = None
            row["response_window_close_lsl"] = None
            row["actual_response_window_seconds"] = None
            row["response_window_close_overshoot_seconds"] = None
        next_row = rows[index + 1] if index + 1 < len(rows) else None
        onset = _optional_float(row.get("stimulus_onset_monotonic"))
        onset_lsl = _optional_float(row.get("stimulus_onset_lsl"))
        next_onset = _optional_float(next_row.get("stimulus_onset_monotonic")) if next_row else None
        next_onset_lsl = _optional_float(next_row.get("stimulus_onset_lsl")) if next_row else None
        monotonic_pair_valid = onset is not None and next_onset is not None and next_onset > onset
        lsl_pair_valid = onset_lsl is not None and next_onset_lsl is not None and next_onset_lsl > onset_lsl
        if not monotonic_pair_valid:
            next_onset = None
        if not lsl_pair_valid:
            next_onset_lsl = None
        row["actual_next_trial_onset_monotonic"] = next_onset
        row["actual_next_trial_onset_lsl"] = next_onset_lsl
        row["next_trial_onset_monotonic"] = next_onset
        row["actual_trial_duration_seconds"] = (
            None if onset is None or next_onset is None else next_onset - onset
        )
        row["actual_trial_duration_lsl_seconds"] = (
            None if onset_lsl is None or next_onset_lsl is None else next_onset_lsl - onset_lsl
        )
        row["timing_finalization_status"] = (
            "measured_from_next_stimulus_flip"
            if next_onset is not None
            else "terminal_no_following_stimulus_flip"
            if next_row is None
            else "invalid_or_missing_following_stimulus_flip"
        )
    return rows


def compute_support_reference(
    trials: Iterable[dict[str, Any]],
    *,
    minimum_valid_rt_seconds: float,
    response_window_seconds: float,
    created_at_support_boundary: float | None = None,
) -> dict[str, Any]:
    rows = [dict(row) for row in trials]
    included = [
        row
        for row in rows
        if _valid_go_rt(row, minimum_valid_rt_seconds, response_window_seconds)
        and row.get("phase") == "support"
        and not bool(row.get("is_practice", False))
    ]
    reaction_times = [float(row["reaction_time_seconds"]) for row in included]
    log_rts = [math.log(value) for value in reaction_times]
    support_trial_budget = sum(
        int(row.get("phase") == "support" and not bool(row.get("is_practice", False)))
        for row in rows
    )
    support_go_trial_budget = sum(
        int(
            row.get("phase") == "support"
            and row.get("condition") == "go"
            and not bool(row.get("is_practice", False))
        )
        for row in rows
    )
    q95_preferred_attainable = support_go_trial_budget >= 200
    log_median = median(log_rts) if log_rts else None
    raw_median = median(reaction_times) if reaction_times else None
    raw_mad = _median_absolute_deviation(log_rts)
    robust_scale, scale_method = _robust_scale(log_rts, raw_mad)
    warnings = []
    if len(log_rts) < 50:
        warnings.append("valid_support_go_count_below_50_for_robust_reference")
    if len(log_rts) < 100:
        warnings.append("valid_support_go_count_below_100_for_q80")
    if len(log_rts) < 200:
        warnings.append("valid_support_go_count_below_200_for_q95")
    if not q95_preferred_attainable:
        warnings.append("q95_preferred_confidence_unattainable_under_support_go_trial_budget")
    if robust_scale is None:
        warnings.append("support_log_rt_scale_unavailable")
    source_payload = [
        {
            "global_trial_index": int(row["global_trial_index"]),
            "reaction_time_seconds": float(row["reaction_time_seconds"]),
        }
        for row in included
    ]
    source_trial_hash = _hash_payload(source_payload)
    reference = {
        "schema": SUPPORT_REFERENCE_SCHEMA,
        "support_trial_budget": support_trial_budget,
        "support_go_trial_budget": support_go_trial_budget,
        "valid_support_go_count": len(log_rts),
        "support_rt_median_seconds": raw_median,
        "support_log_rt_median": log_median,
        "support_log_rt_mad": raw_mad,
        "support_log_rt_robust_scale": robust_scale,
        "support_log_rt_scale_method": scale_method,
        "support_log_rt_q80": _quantile(log_rts, 0.80),
        "support_log_rt_q95": _quantile(log_rts, 0.95),
        "principal_support_relative_threshold": "q80",
        "support_q80_role": "primary_exploratory",
        "support_q95_role": "secondary_exploratory",
        "support_q80_confidence": "preferred" if len(log_rts) >= 100 else "low",
        "support_q95_confidence": (
            "preferred"
            if len(log_rts) >= 200
            else "low_unattainable_by_design"
            if not q95_preferred_attainable
            else "low"
        ),
        "support_q80_preferred_min_valid_go_trials": 100,
        "support_q95_preferred_min_valid_go_trials": 200,
        "support_q95_preferred_attainable": q95_preferred_attainable,
        "created_at_support_boundary": created_at_support_boundary,
        "last_included_support_trial": max((int(row["global_trial_index"]) for row in included), default=None),
        "source_trial_hash": source_trial_hash,
        "warnings": warnings,
    }
    reference["reference_hash"] = _hash_payload({key: value for key, value in reference.items() if key != "reference_hash"})
    return reference


def generate_dynamic_sart_labels(
    trials: Iterable[dict[str, Any]],
    support_reference: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_rows = [dict(row) for row in trials]
    experimental = [
        row
        for row in raw_rows
        if not bool(row.get("is_practice", False)) and row.get("phase") in {"support", "query"}
    ]
    experimental.sort(key=lambda row: int(row.get("global_trial_index", 0)))
    support_boundary_trial = max(
        (int(row["global_trial_index"]) for row in experimental if row.get("phase") == "support"),
        default=None,
    )
    valid_go_indices = [index for index, row in enumerate(experimental) if _is_valid_go_record(row)]
    q80 = _optional_float(support_reference.get("support_log_rt_q80"))
    q95 = _optional_float(support_reference.get("support_log_rt_q95"))
    support_median = _optional_float(support_reference.get("support_log_rt_median"))
    support_scale = _optional_float(support_reference.get("support_log_rt_robust_scale"))
    labels: list[dict[str, Any]] = []
    prior_valid_go: list[dict[str, Any]] = []
    prior_rows: list[dict[str, Any]] = []
    for index, row in enumerate(experimental):
        current_rt = float(row["reaction_time_seconds"]) if _is_valid_go_record(row) else None
        current_log_rt = math.log(current_rt) if current_rt is not None else None
        support_z = _support_z(current_log_rt, support_median, support_scale)
        next_index = next((candidate for candidate in valid_go_indices if candidate > index), None)
        next_row = experimental[next_index] if next_index is not None else None
        next_rt = float(next_row["reaction_time_seconds"]) if next_row is not None else None
        next_log_rt = math.log(next_rt) if next_rt is not None else None
        previous_valid = prior_valid_go[-1] if prior_valid_go else None
        previous_response_time = next(
            (
                _optional_float(previous.get("first_response_timestamp_monotonic"))
                for previous in reversed(prior_rows)
                if _optional_float(previous.get("first_response_timestamp_monotonic")) is not None
            ),
            None,
        )
        onset = _optional_float(row.get("stimulus_onset_monotonic"))
        recent = prior_rows[-20:]
        future_valid_3 = [experimental[candidate] for candidate in valid_go_indices if candidate > index][:3]
        future_valid_5 = [experimental[candidate] for candidate in valid_go_indices if candidate > index][:5]
        future_experimental_5 = experimental[index + 1 : index + 6]
        next_valid_go_maturity = _target_maturity(next_row, support_boundary_trial)
        future_target_maturity = {
            "next_valid_go_rt_seconds": dict(next_valid_go_maturity),
            "next_valid_go_log_rt": dict(next_valid_go_maturity),
            "next_log_rt_support_robust_z": dict(next_valid_go_maturity),
            "future_slow_any_next_3_valid_go_trials": _target_maturity(
                future_valid_3[-1] if len(future_valid_3) == 3 else None,
                support_boundary_trial,
            ),
            "future_very_slow_any_next_5_valid_go_trials": _target_maturity(
                future_valid_5[-1] if len(future_valid_5) == 5 else None,
                support_boundary_trial,
            ),
            "future_commission_any_next_5_experimental_trials": _target_maturity(
                future_experimental_5[-1] if len(future_experimental_5) == 5 else None,
                support_boundary_trial,
            ),
            "future_omission_any_next_5_experimental_trials": _target_maturity(
                future_experimental_5[-1] if len(future_experimental_5) == 5 else None,
                support_boundary_trial,
            ),
            "future_behavioral_failure_any_next_5_experimental_trials": _target_maturity(
                future_experimental_5[-1] if len(future_experimental_5) == 5 else None,
                support_boundary_trial,
            ),
        }
        matured_targets = [
            metadata
            for metadata in future_target_maturity.values()
            if metadata["target_maturity_trial"] is not None
        ]
        latest_maturity = max(
            matured_targets,
            key=lambda metadata: int(metadata["target_maturity_trial"]),
            default=None,
        )
        support_calibration_eligible_targets = [
            name
            for name, metadata in future_target_maturity.items()
            if metadata["available_at_support_complete"]
        ]
        current_slow = current_log_rt is not None and q80 is not None and current_log_rt >= q80
        current_very_slow = current_log_rt is not None and q95 is not None and current_log_rt >= q95
        current_failure = bool(row.get("commission_error")) or bool(row.get("omission_error")) or current_very_slow
        last_failure_index = next(
            (
                int(previous["global_trial_index"])
                for previous in reversed(prior_rows)
                if _behavioral_failure(previous, q95)
            ),
            None,
        )
        previous_row = prior_rows[-1] if prior_rows else None
        fields = {
            "schema": LABELS_SCHEMA,
            "global_trial_index": int(row["global_trial_index"]),
            "phase": row.get("phase"),
            "condition": row.get("condition"),
            "analysis_eligible": True,
            "current_valid_go_rt_seconds": current_rt,
            "current_valid_go_log_rt": current_log_rt,
            "current_log_rt_support_robust_z": support_z,
            "support_relative_slow": current_slow if current_log_rt is not None and q80 is not None else None,
            "support_relative_slow_confidence": support_reference.get("support_q80_confidence"),
            "support_relative_slow_is_primary": True,
            "support_relative_very_slow": current_very_slow if current_log_rt is not None and q95 is not None else None,
            "support_relative_very_slow_confidence": support_reference.get("support_q95_confidence"),
            "support_relative_very_slow_is_primary": False,
            "commission_error": bool(row.get("commission_error", False)),
            "omission_error": bool(row.get("omission_error", False)),
            "premature_response": bool(row.get("premature_response", False)),
            "too_fast_response": bool(row.get("too_fast_response", False)),
            "multiple_response": bool(row.get("multiple_response", False)),
            "wrong_key_response": bool(row.get("wrong_key_response", False)),
            "late_response": bool(row.get("late_response", False)),
            "behavioral_failure_composite_v1": current_failure,
            "behavioral_failure_composite_v1_confidence": support_reference.get("support_q95_confidence"),
            "behavioral_failure_composite_v1_is_primary": False,
            "previous_valid_go_rt_seconds": _optional_float(previous_valid.get("reaction_time_seconds")) if previous_valid else None,
            "previous_valid_go_log_rt": math.log(float(previous_valid["reaction_time_seconds"])) if previous_valid else None,
            "rolling_log_rt_median_previous_5": _rolling_stat(prior_valid_go, 5, "median"),
            "rolling_log_rt_mad_previous_5": _rolling_stat(prior_valid_go, 5, "mad"),
            "rolling_log_rt_median_previous_10": _rolling_stat(prior_valid_go, 10, "median"),
            "rolling_log_rt_mad_previous_10": _rolling_stat(prior_valid_go, 10, "mad"),
            "rolling_log_rt_median_previous_20": _rolling_stat(prior_valid_go, 20, "median"),
            "rolling_log_rt_mad_previous_20": _rolling_stat(prior_valid_go, 20, "mad"),
            "previous_primary_outcome": previous_row.get("primary_outcome") if previous_row else None,
            "recent_commission_count": sum(int(bool(previous.get("commission_error"))) for previous in recent),
            "recent_omission_count": sum(int(bool(previous.get("omission_error"))) for previous in recent),
            "recent_behavioral_failure_count": sum(int(_behavioral_failure(previous, q95)) for previous in recent),
            "trials_since_error": _trials_since(prior_rows, lambda previous: bool(previous.get("commission_error")) or bool(previous.get("omission_error"))),
            "trials_since_no_go": _trials_since(prior_rows, lambda previous: bool(previous.get("is_no_go"))),
            "time_since_previous_response": None if onset is None or previous_response_time is None else onset - previous_response_time,
            "time_on_task_seconds": _optional_float(row.get("time_on_task_seconds")),
            "next_valid_go_trial_index": int(next_row["global_trial_index"]) if next_row else None,
            "next_valid_go_trial_distance": int(next_row["global_trial_index"]) - int(row["global_trial_index"]) if next_row else None,
            "next_valid_go_time_distance_seconds": _time_distance(row, next_row),
            "next_valid_go_rt_seconds": next_rt,
            "next_valid_go_log_rt": next_log_rt,
            "next_log_rt_support_robust_z": _support_z(next_log_rt, support_median, support_scale),
            "next_valid_go_status": "available" if next_row else "unavailable_no_future_valid_go",
            "future_slow_any_next_3_valid_go_trials": _future_tail(future_valid_3, q80, expected=3),
            "future_very_slow_any_next_5_valid_go_trials": _future_tail(future_valid_5, q95, expected=5),
            "future_very_slow_any_next_5_valid_go_trials_confidence": support_reference.get("support_q95_confidence"),
            "future_very_slow_any_next_5_valid_go_trials_is_primary": False,
            "future_commission_any_next_5_experimental_trials": _future_event(future_experimental_5, "commission_error", expected=5),
            "future_omission_any_next_5_experimental_trials": _future_event(future_experimental_5, "omission_error", expected=5),
            "future_behavioral_failure_any_next_5_experimental_trials": _future_failure(future_experimental_5, q95, expected=5),
            "future_behavioral_failure_any_next_5_experimental_trials_confidence": support_reference.get("support_q95_confidence"),
            "future_behavioral_failure_any_next_5_experimental_trials_is_primary": False,
            "future_target_maturity": future_target_maturity,
            "target_maturity_trial": None if latest_maturity is None else latest_maturity["target_maturity_trial"],
            "target_maturity_phase": None if latest_maturity is None else latest_maturity["target_maturity_phase"],
            "available_at_support_complete": bool(future_target_maturity) and all(
                metadata["available_at_support_complete"] for metadata in future_target_maturity.values()
            ),
            "support_calibration_eligible_targets": support_calibration_eligible_targets,
            "trials_since_behavioral_failure": None if last_failure_index is None else int(row["global_trial_index"]) - last_failure_index,
            "post_commission_trial": bool(previous_row and previous_row.get("commission_error")),
            "post_omission_trial": bool(previous_row and previous_row.get("omission_error")),
            "recovery_1_to_3_trials_after_behavioral_failure": (
                last_failure_index is not None and 1 <= int(row["global_trial_index"]) - last_failure_index <= 3
            ),
            "reference_hash": support_reference.get("reference_hash"),
        }
        labels.append(fields)
        prior_rows.append(row)
        if _is_valid_go_record(row):
            prior_valid_go.append(row)
    return labels


def write_dynamic_sart_label_artifacts(
    session_dir: str | Path,
    trials: Iterable[dict[str, Any]],
    support_reference: dict[str, Any],
) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
    events = root / "events"
    events.mkdir(parents=True, exist_ok=True)
    labels = generate_dynamic_sart_labels(trials, support_reference)
    labels_path = events / "dynamic_sart_labels.csv"
    contract_path = events / "dynamic_sart_label_contract.json"
    _write_rows_csv(labels_path, labels)
    contract = dynamic_sart_label_contract()
    _write_json_atomic(contract_path, contract)
    return {
        "status": "ok",
        "label_count": len(labels),
        "labels_file": str(labels_path),
        "label_contract_file": str(contract_path),
        "reference_hash": support_reference.get("reference_hash"),
        "warnings": list(support_reference.get("warnings") or []),
    }


def dynamic_sart_label_contract() -> dict[str, Any]:
    field_names = (
        "schema", "global_trial_index", "phase", "condition", "analysis_eligible",
        "current_valid_go_rt_seconds", "current_valid_go_log_rt", "current_log_rt_support_robust_z",
        "support_relative_slow", "support_relative_slow_confidence", "support_relative_slow_is_primary",
        "support_relative_very_slow", "support_relative_very_slow_confidence",
        "support_relative_very_slow_is_primary", "commission_error", "omission_error",
        "premature_response", "too_fast_response", "multiple_response", "wrong_key_response",
        "late_response", "behavioral_failure_composite_v1", "behavioral_failure_composite_v1_confidence",
        "behavioral_failure_composite_v1_is_primary", "previous_valid_go_rt_seconds",
        "previous_valid_go_log_rt", "rolling_log_rt_median_previous_5", "rolling_log_rt_mad_previous_5",
        "rolling_log_rt_median_previous_10", "rolling_log_rt_mad_previous_10",
        "rolling_log_rt_median_previous_20", "rolling_log_rt_mad_previous_20",
        "previous_primary_outcome", "recent_commission_count", "recent_omission_count",
        "recent_behavioral_failure_count", "trials_since_error", "trials_since_no_go",
        "time_since_previous_response", "time_on_task_seconds", "next_valid_go_trial_index",
        "next_valid_go_trial_distance", "next_valid_go_time_distance_seconds", "next_valid_go_rt_seconds",
        "next_valid_go_log_rt", "next_log_rt_support_robust_z", "next_valid_go_status",
        "future_slow_any_next_3_valid_go_trials", "future_very_slow_any_next_5_valid_go_trials",
        "future_very_slow_any_next_5_valid_go_trials_confidence",
        "future_very_slow_any_next_5_valid_go_trials_is_primary",
        "future_commission_any_next_5_experimental_trials", "future_omission_any_next_5_experimental_trials",
        "future_behavioral_failure_any_next_5_experimental_trials",
        "future_behavioral_failure_any_next_5_experimental_trials_confidence",
        "future_behavioral_failure_any_next_5_experimental_trials_is_primary", "future_target_maturity",
        "target_maturity_trial", "target_maturity_phase", "available_at_support_complete",
        "support_calibration_eligible_targets", "trials_since_behavioral_failure",
        "post_commission_trial", "post_omission_trial", "recovery_1_to_3_trials_after_behavioral_failure",
        "reference_hash",
    )
    fields = {
        name: _field(
            f"Versioned DSART derived field: {name}.",
            "before anchor onset" if name.startswith(("previous_", "rolling_", "recent_", "trials_since_", "time_since_")) else "current trial complete",
            "all experimental anchors",
            name.startswith("support_relative_") or "support_robust_z" in name or name == "reference_hash",
        )
        for name in field_names
    }
    for name in ("schema", "global_trial_index", "phase", "condition", "analysis_eligible", "time_on_task_seconds"):
        fields[name]["temporal_availability"] = "known at or before anchor onset"
    fields.update(
        {
            "current_valid_go_log_rt": _field("Log reaction time for an eligible current go trial.", "current trial complete", "current valid go", False),
            "current_log_rt_support_robust_z": _field("Current log RT relative to the fixed support reference.", "current trial complete", "current valid go", True),
            "support_relative_slow": _field("Current valid-go log RT at or above support q80.", "current trial complete", "current valid go", True),
            "support_relative_very_slow": _field("Current valid-go log RT at or above support q95.", "current trial complete", "current valid go", True),
            "previous_valid_go_log_rt": _field("Most recent earlier valid-go log RT.", "before anchor onset", "all experimental anchors", False),
            "rolling_log_rt_median_previous_5": _field("Median of up to five earlier valid-go log RTs.", "before anchor onset", "all experimental anchors", False),
            "next_valid_go_log_rt": _field("Log RT of the first later valid go trial.", "future outcome maturity", "anchors with a later valid go", False, future="next valid go"),
            "future_slow_any_next_3_valid_go_trials": _field("Any support-relative slow outcome among the next three valid-go trials.", "future outcome maturity", "anchors with three later valid go trials", True, future="next 3 valid go trials"),
            "future_commission_any_next_5_experimental_trials": _field("Any commission in the next five physical experimental trials.", "future outcome maturity", "anchors with five later experimental trials", False, future="next 5 experimental trials"),
            "behavioral_failure_composite_v1": _field("Commission, omission, or support-relative very slow current outcome.", "current trial complete", "experimental trials", True),
            "future_target_maturity": _field(
                "Per-target trial, phase, and support-boundary availability for every future behavioral target.",
                "future outcome maturity",
                "all experimental anchors",
                False,
                future="target-specific",
            ),
            "target_maturity_trial": _field(
                "Latest maturity trial among the row's available future targets.",
                "future outcome maturity",
                "all experimental anchors",
                False,
                future="latest target-specific maturity",
            ),
            "target_maturity_phase": _field(
                "Phase containing the latest available target maturity trial.",
                "future outcome maturity",
                "all experimental anchors",
                False,
                future="latest target-specific maturity",
            ),
            "available_at_support_complete": _field(
                "True only when every declared future target on the row matured within support.",
                "support boundary",
                "support anchors",
                False,
            ),
            "support_calibration_eligible_targets": _field(
                "Future target names whose outcomes matured no later than support_complete.",
                "support boundary",
                "all experimental anchors",
                False,
            ),
        }
    )
    for name in field_names:
        if name.startswith("next_") or name.startswith("future_"):
            fields[name]["temporal_availability"] = "future outcome maturity"
            fields[name]["future_horizon"] = fields[name]["future_horizon"] or "field name defines horizon"
    fields["future_target_maturity"]["data_type"] = "object"
    fields["target_maturity_phase"]["data_type"] = "string"
    fields["support_calibration_eligible_targets"]["data_type"] = "list_of_strings"
    return {
        "schema": LABEL_CONTRACT_SCHEMA,
        "fields": fields,
        "global_rules": {
            "raw_trials_are_immutable": True,
            "causal_features_use_trials_strictly_before_anchor": True,
            "future_targets_begin_at_anchor_plus_one": True,
            "support_reference_uses_support_only": True,
            "support_calibration_requires_target_maturity": True,
            "support_calibration_rule": (
                "Do not select rows by anchor phase alone. A future target may train a support calibrator only when "
                "future_target_maturity[target].available_at_support_complete is true."
            ),
            "principal_support_relative_threshold": "q80",
            "q95_role": "secondary exploratory; confidence is unattainable at the 200-valid-go rule in the 200-trial support recipe",
            "scientific_interpretation": "Operational behavioral performance variables.",
            "scientific_non_interpretation": "These fields are not direct observations of a biological attention state.",
        },
    }


def _field(description: str, availability: str, eligibility: str, support: bool, future: str | None = None) -> dict[str, Any]:
    return {
        "description": description,
        "data_type": "number_or_boolean",
        "eligibility_set": eligibility,
        "prediction_anchor": "trial t",
        "temporal_availability": availability,
        "uses_support_reference": support,
        "support_fields_used": ["support_log_rt_q80", "support_log_rt_q95", "support_log_rt_median", "support_log_rt_robust_scale"] if support else [],
        "future_horizon": future,
        "missing_value_rule": "empty CSV value when unavailable or immature",
        "confidence_rule": (
            "q80 is the primary exploratory support-relative threshold and is preferred at 100 valid support go trials; "
            "q95 remains secondary and is preferred only at 200, which the 200-trial support recipe cannot attain."
        ),
        "scientific_interpretation": "Behavioral proxy or context feature.",
        "scientific_non_interpretation": "Not a directly observed latent attention state.",
    }


def _is_valid_go_record(row: dict[str, Any]) -> bool:
    rt = _optional_float(row.get("reaction_time_seconds"))
    return (
        row.get("condition") == "go"
        and row.get("primary_outcome") == "correct_go"
        and rt is not None
        and not bool(row.get("too_fast_response", False))
        and not bool(row.get("aborted", False))
        and not bool(row.get("invalid", False))
    )


def _valid_go_rt(row: dict[str, Any], minimum: float, maximum: float) -> bool:
    rt = _optional_float(row.get("reaction_time_seconds"))
    return _is_valid_go_record(row) and rt is not None and minimum <= rt <= maximum


def _behavioral_failure(row: dict[str, Any], q95: float | None) -> bool:
    if bool(row.get("commission_error")) or bool(row.get("omission_error")):
        return True
    if q95 is None or not _is_valid_go_record(row):
        return False
    return math.log(float(row["reaction_time_seconds"])) >= q95


def _rolling_stat(rows: list[dict[str, Any]], count: int, kind: str) -> float | None:
    values = [math.log(float(row["reaction_time_seconds"])) for row in rows[-count:]]
    if not values:
        return None
    return median(values) if kind == "median" else _median_absolute_deviation(values)


def _trials_since(rows: list[dict[str, Any]], predicate: Any) -> int | None:
    for distance, row in enumerate(reversed(rows), start=1):
        if predicate(row):
            return distance
    return None


def _future_tail(rows: list[dict[str, Any]], threshold: float | None, *, expected: int) -> bool | None:
    if threshold is None or len(rows) < expected:
        return None
    return any(math.log(float(row["reaction_time_seconds"])) >= threshold for row in rows)


def _future_event(rows: list[dict[str, Any]], field: str, *, expected: int) -> bool | None:
    if len(rows) < expected:
        return None
    return any(bool(row.get(field)) for row in rows)


def _future_failure(rows: list[dict[str, Any]], q95: float | None, *, expected: int) -> bool | None:
    if len(rows) < expected:
        return None
    return any(_behavioral_failure(row, q95) for row in rows)


def _target_maturity(
    maturity_row: dict[str, Any] | None,
    support_boundary_trial: int | None,
) -> dict[str, Any]:
    maturity_trial = None if maturity_row is None else int(maturity_row["global_trial_index"])
    maturity_phase = None if maturity_row is None else maturity_row.get("phase")
    return {
        "target_maturity_trial": maturity_trial,
        "target_maturity_phase": maturity_phase,
        "available_at_support_complete": bool(
            maturity_trial is not None
            and support_boundary_trial is not None
            and maturity_phase == "support"
            and maturity_trial <= support_boundary_trial
        ),
    }


def _time_distance(anchor: dict[str, Any], future: dict[str, Any] | None) -> float | None:
    if future is None:
        return None
    left = _optional_float(anchor.get("stimulus_onset_monotonic"))
    right = _optional_float(future.get("stimulus_onset_monotonic"))
    return None if left is None or right is None else right - left


def _support_z(value: float | None, center: float | None, scale: float | None) -> float | None:
    if value is None or center is None or scale is None or scale <= 0:
        return None
    return (value - center) / scale


def _robust_scale(values: list[float], mad: float | None) -> tuple[float | None, str | None]:
    if mad is not None and mad > 1e-12:
        return 1.4826 * mad, "median_absolute_deviation"
    if len(values) >= 4:
        q25 = _quantile(values, 0.25)
        q75 = _quantile(values, 0.75)
        if q25 is not None and q75 is not None and q75 - q25 > 1e-12:
            return (q75 - q25) / 1.349, "interquartile_range"
    if len(values) >= 2:
        center = sum(values) / len(values)
        variance = sum((value - center) ** 2 for value in values) / (len(values) - 1)
        if variance > 1e-24:
            return math.sqrt(variance), "sample_standard_deviation"
    return None, None


def _median_absolute_deviation(values: list[float]) -> float | None:
    if not values:
        return None
    center = median(values)
    return median([abs(value - center) for value in values])


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _coerce_csv_row(row: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = dict(row)
    for field in ("global_trial_index", "block_index", "block_trial_index", "digit"):
        if row.get(field):
            result[field] = int(row[field])
    for field in (
        "is_practice",
        "is_no_go",
        "correct",
        "commission_error",
        "omission_error",
        "premature_response",
        "too_fast_response",
        "multiple_response",
        "wrong_key_response",
        "late_response",
        "aborted",
        "invalid",
    ):
        if field in row:
            result[field] = str(row[field]).lower() in {"1", "true", "yes"}
    for field in ("reaction_time_seconds", "response_window_close_monotonic", "stimulus_onset_monotonic", "time_on_task_seconds"):
        if row.get(field):
            result[field] = float(row[field])
    return result


def _write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0]) if rows else ["schema", "global_trial_index"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in fields})


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
