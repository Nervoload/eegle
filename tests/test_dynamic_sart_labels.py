from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from eegle.analysis.dynamic_sart import analyze_dynamic_sart_session
from eegle.analysis.dynamic_sart_labels import (
    compute_support_reference,
    dynamic_sart_label_contract,
    generate_dynamic_sart_labels,
    write_dynamic_sart_label_artifacts,
)


def _trial(
    index: int,
    *,
    phase: str,
    condition: str = "go",
    rt: float | None = 0.4,
    outcome: str | None = None,
    practice: bool = False,
) -> dict:
    if outcome is None:
        outcome = "correct_no_go" if condition == "no_go" else "correct_go"
    return {
        "global_trial_index": index,
        "phase": phase,
        "condition": condition,
        "is_no_go": condition == "no_go",
        "is_practice": practice,
        "primary_outcome": outcome,
        "reaction_time_seconds": rt,
        "stimulus_onset_monotonic": 100.0 + index * 1.25,
        "first_response_timestamp_monotonic": None if rt is None else 100.0 + index * 1.25 + rt,
        "time_on_task_seconds": index * 1.25,
        "correct": outcome in {"correct_go", "correct_no_go"},
        "commission_error": outcome == "commission_error",
        "omission_error": outcome == "omission_error",
        "premature_response": False,
        "too_fast_response": False,
        "multiple_response": False,
        "wrong_key_response": False,
        "late_response": False,
        "aborted": False,
        "invalid": False,
    }


def _rows() -> list[dict]:
    return [
        _trial(-1, phase="practice", rt=0.2, practice=True),
        _trial(1, phase="support", rt=0.30),
        _trial(2, phase="support", rt=0.40),
        _trial(3, phase="support", condition="no_go", rt=None),
        _trial(4, phase="support", rt=0.50),
        _trial(5, phase="query", condition="no_go", rt=0.15, outcome="commission_error"),
        _trial(6, phase="query", rt=None, outcome="omission_error"),
        _trial(7, phase="query", rt=0.60),
        _trial(8, phase="query", rt=0.55),
        _trial(9, phase="query", rt=0.52),
        _trial(10, phase="query", rt=0.51),
        _trial(11, phase="query", rt=0.49),
    ]


class DynamicSartLabelTests(unittest.TestCase):
    def test_support_reference_excludes_practice_and_query(self) -> None:
        rows = _rows()
        first = compute_support_reference(rows, minimum_valid_rt_seconds=0.1, response_window_seconds=1.15, created_at_support_boundary=1.0)
        changed = [dict(row) for row in rows]
        for row in changed:
            if row["phase"] == "query":
                row["reaction_time_seconds"] = 1.0
        second = compute_support_reference(changed, minimum_valid_rt_seconds=0.1, response_window_seconds=1.15, created_at_support_boundary=1.0)
        self.assertEqual(first["valid_support_go_count"], 3)
        self.assertEqual(first["last_included_support_trial"], 4)
        self.assertEqual(first["reference_hash"], second["reference_hash"])

    def test_missing_support_data_warns_without_fabricating_statistics(self) -> None:
        reference = compute_support_reference(
            [_trial(1, phase="support", condition="no_go", rt=None)],
            minimum_valid_rt_seconds=0.1,
            response_window_seconds=1.15,
        )
        self.assertEqual(reference["valid_support_go_count"], 0)
        self.assertIsNone(reference["support_log_rt_median"])
        self.assertIn("support_log_rt_scale_unavailable", reference["warnings"])

    def test_q80_is_primary_and_q95_is_explicitly_unattainable_for_pilot_support_budget(self) -> None:
        rows = [_trial(index, phase="support", rt=0.3 + index / 10000) for index in range(1, 179)]
        rows.extend(
            _trial(index, phase="support", condition="no_go", rt=None)
            for index in range(179, 201)
        )
        reference = compute_support_reference(rows, minimum_valid_rt_seconds=0.1, response_window_seconds=1.15)
        self.assertEqual(reference["support_go_trial_budget"], 178)
        self.assertEqual(reference["principal_support_relative_threshold"], "q80")
        self.assertEqual(reference["support_q80_confidence"], "preferred")
        self.assertFalse(reference["support_q95_preferred_attainable"])
        self.assertEqual(reference["support_q95_confidence"], "low_unattainable_by_design")

    def test_causal_features_exclude_current_and_later_trials(self) -> None:
        rows = _rows()
        reference = compute_support_reference(rows, minimum_valid_rt_seconds=0.1, response_window_seconds=1.15)
        original = {row["global_trial_index"]: row for row in generate_dynamic_sart_labels(rows, reference)}
        changed_current = [dict(row) for row in rows]
        next(row for row in changed_current if row["global_trial_index"] == 7)["reaction_time_seconds"] = 0.9
        current_changed = {row["global_trial_index"]: row for row in generate_dynamic_sart_labels(changed_current, reference)}
        causal_fields = [
            "previous_valid_go_log_rt",
            "rolling_log_rt_median_previous_5",
            "recent_commission_count",
            "trials_since_error",
            "trials_since_no_go",
            "time_since_previous_response",
        ]
        for field in causal_fields:
            self.assertEqual(original[7][field], current_changed[7][field])

        changed_later = [dict(row) for row in rows]
        next(row for row in changed_later if row["global_trial_index"] == 11)["reaction_time_seconds"] = 1.0
        later_changed = {row["global_trial_index"]: row for row in generate_dynamic_sart_labels(changed_later, reference)}
        for field in causal_fields:
            self.assertEqual(original[7][field], later_changed[7][field])

    def test_next_valid_go_skips_physical_no_go_and_omission_trials(self) -> None:
        rows = _rows()
        reference = compute_support_reference(rows, minimum_valid_rt_seconds=0.1, response_window_seconds=1.15)
        labels = {row["global_trial_index"]: row for row in generate_dynamic_sart_labels(rows, reference)}
        self.assertEqual(labels[4]["next_valid_go_trial_index"], 7)
        self.assertEqual(labels[4]["next_valid_go_trial_distance"], 3)
        self.assertTrue(labels[4]["future_commission_any_next_5_experimental_trials"])
        self.assertIsNone(labels[11]["next_valid_go_rt_seconds"])
        self.assertEqual(labels[11]["next_valid_go_status"], "unavailable_no_future_valid_go")

    def test_future_valid_go_and_physical_trial_horizons_are_distinct(self) -> None:
        rows = _rows()
        reference = compute_support_reference(rows, minimum_valid_rt_seconds=0.1, response_window_seconds=1.15)
        labels = {row["global_trial_index"]: row for row in generate_dynamic_sart_labels(rows, reference)}
        self.assertIsNotNone(labels[4]["future_commission_any_next_5_experimental_trials"])
        self.assertIsNotNone(labels[4]["future_slow_any_next_3_valid_go_trials"])
        self.assertIsNone(labels[9]["future_slow_any_next_3_valid_go_trials"])

    def test_future_targets_persist_maturity_and_support_boundary_availability(self) -> None:
        rows = _rows()
        reference = compute_support_reference(rows, minimum_valid_rt_seconds=0.1, response_window_seconds=1.15)
        labels = {row["global_trial_index"]: row for row in generate_dynamic_sart_labels(rows, reference)}
        crossing = labels[4]["future_target_maturity"]
        self.assertEqual(crossing["next_valid_go_log_rt"]["target_maturity_trial"], 7)
        self.assertEqual(crossing["next_valid_go_log_rt"]["target_maturity_phase"], "query")
        self.assertFalse(crossing["next_valid_go_log_rt"]["available_at_support_complete"])
        self.assertFalse(labels[4]["available_at_support_complete"])
        self.assertNotIn("next_valid_go_log_rt", labels[4]["support_calibration_eligible_targets"])

        early = labels[1]["future_target_maturity"]["next_valid_go_log_rt"]
        self.assertEqual(early["target_maturity_trial"], 2)
        self.assertTrue(early["available_at_support_complete"])
        self.assertIn("next_valid_go_log_rt", labels[1]["support_calibration_eligible_targets"])

    def test_label_generation_is_idempotent_and_does_not_modify_raw_trials(self) -> None:
        rows = _rows()
        reference = compute_support_reference(rows, minimum_valid_rt_seconds=0.1, response_window_seconds=1.15)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = root / "events"
            events.mkdir()
            raw_path = events / "dynamic_sart_trials.jsonl"
            raw_path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
            before = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            first = write_dynamic_sart_label_artifacts(root, rows, reference)
            first_labels = (events / "dynamic_sart_labels.csv").read_bytes()
            second = write_dynamic_sart_label_artifacts(root, rows, reference)
            second_labels = (events / "dynamic_sart_labels.csv").read_bytes()
            after = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        self.assertEqual(first["label_count"], 11)
        self.assertEqual(first, second)
        self.assertEqual(first_labels, second_labels)
        self.assertEqual(before, after)

    def test_contract_documents_every_generated_field(self) -> None:
        rows = _rows()
        reference = compute_support_reference(rows, minimum_valid_rt_seconds=0.1, response_window_seconds=1.15)
        generated = generate_dynamic_sart_labels(rows, reference)
        self.assertEqual(set(generated[0]), set(dynamic_sart_label_contract()["fields"]))

    def test_partial_analysis_handles_missing_error_classes(self) -> None:
        rows = [_trial(1, phase="support", rt=0.3), _trial(2, phase="query", rt=0.4)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "events").mkdir()
            (root / "reports").mkdir()
            (root / "events" / "dynamic_sart_trials.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
            )
            result = analyze_dynamic_sart_session(root, {"tasks": {"dynamic_sart": {}}})
            timing_rows = (root / "reports" / "dynamic_sart_timing.csv").read_text().splitlines()
        self.assertIn(result["status"], {"ok", "warn"})
        self.assertEqual(result["flag_counts"]["commission_error"], 0)
        self.assertEqual(result["flag_counts"]["omission_error"], 0)
        self.assertTrue(result["partial_run"])
        self.assertEqual(result["timing_measurement"]["measured_inter_onset_count"], 1)
        self.assertEqual(len(timing_rows), 3)
        self.assertIn("actual_trial_duration_seconds", timing_rows[0])


if __name__ == "__main__":
    unittest.main()
