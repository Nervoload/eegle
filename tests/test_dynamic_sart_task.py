from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eegle.factory import make_task_component
from eegle.realtime.epoching import EpochingConfig, MarkerEvent, load_stimulus_manifest_markers, parse_marker_label, should_epoch_marker
from eegle.realtime.policy import TaskAction
from eegle.session import create_session
from eegle.tasks.dynamic_sart import (
    DynamicSartArtifactStore,
    DynamicSartTask,
    _capture_countdown_flip,
    _capture_flip_event,
    _completion_text,
    _log_captured_flip_event,
    _log_captured_countdown_flip,
    _practice_status_text,
    _run_psychopy_countdown,
    _show_bounded_break,
    audit_dynamic_sart_action,
    practice_criteria,
    score_dynamic_sart_trial,
)
from eegle.analysis.dynamic_sart_labels import reconstruct_dynamic_sart_timing
from eegle.tasks.dynamic_sart_schema import DynamicSartConfig
from eegle.tasks.dynamic_sart_sequence import build_dynamic_sart_plan, validate_dynamic_sart_plan
from eegle.tasks.registry import get_task_spec, list_task_specs


def _config(*, seed: int = 42, abort_after: int | None = None) -> dict:
    dynamic_sart = {
        "master_seed": seed,
        "blocks": [
            {"name": "support", "phase": "support", "trials": 12},
            {"name": "query", "phase": "query", "trials": 12},
        ],
        "practice": {
            "enabled": True,
            "trials_per_round": 9,
            "no_go_trials": 1,
            "max_rounds": 2,
            "go_accuracy": 0.8,
            "no_go_accuracy": 0.6666666667,
            "max_anticipatory_response_rate": 0.15,
        },
    }
    if abort_after is not None:
        dynamic_sart["dry_run"] = {"abort_after_trial": abort_after}
    return {
        "runtime": {"session_root": "data"},
        "experiment": {"experiment_id": "test-dsart", "participant_id": "unit", "task": "dynamic_sart"},
        "hardware": {"markers": {"required_for_realtime": False}},
        "tasks": {"dynamic_sart": dynamic_sart},
        "realtime": {"feedback": {"client": {"enabled": False, "backend": "disabled"}}},
    }


def _score(config: DynamicSartConfig, *, no_go: bool, events: list[dict]) -> dict:
    planned = {
        "global_trial_index": 1,
        "block_index": 1,
        "block_trial_index": 1,
        "block_name": "support",
        "phase": "support",
        "regime": "standard",
        "is_practice": False,
        "sequence_id": "sequence",
        "master_seed": 42,
        "block_seed": 43,
        "digit": config.no_go_digit if no_go else 1,
        "condition": "no_go" if no_go else "go",
        "is_no_go": no_go,
        "expected_action": "withhold" if no_go else "press",
        "planned_stimulus_seconds": config.stimulus_seconds,
        "planned_response_window_seconds": config.response_window_seconds,
        "planned_jitter_seconds": 0.1,
        "planned_onset_offset_seconds": 0.0,
    }
    return score_dynamic_sart_trial(
        planned,
        events,
        config,
        session_id="session",
        participant_id="unit",
        task_start_monotonic=0.0,
        stimulus_onset_monotonic=10.0,
        stimulus_onset_lsl=20.0,
        stimulus_offset_monotonic=10.25,
        stimulus_offset_lsl=20.25,
        scheduled_response_window_close_monotonic=11.15,
        scheduled_response_window_close_lsl=21.15,
        response_window_close_monotonic=11.17,
        response_window_close_lsl=21.17,
        scheduled_next_trial_onset_monotonic=11.25,
    )


def _key(event_id: str, key: str, timestamp: float, *, response: bool = True, premature: bool = False) -> dict:
    return {
        "event_id": event_id,
        "key": key,
        "timestamp_monotonic": timestamp,
        "timestamp_lsl_if_available": timestamp + 10.0,
        "is_response_key": response,
        "is_escape_key": False,
        "is_premature": premature,
        "is_late": False,
    }


class DynamicSartTaskTests(unittest.TestCase):
    def test_artifact_store_close_attempts_all_handles_before_reporting_failure(self) -> None:
        closed = []

        class Handle:
            closed = False

            def __init__(self, name: str, fail: bool = False) -> None:
                self.name = name
                self.fail = fail

            def close(self) -> None:
                closed.append(self.name)
                self.closed = True
                if self.fail:
                    raise OSError("simulated artifact close failure")

        store = object.__new__(DynamicSartArtifactStore)
        store._trial_jsonl = Handle("trials-jsonl", fail=True)
        store._key_jsonl = Handle("keys-jsonl")
        store._probes_jsonl = Handle("probes-jsonl")
        store._trial_csv = Handle("trials-csv")
        store._blocks_csv = Handle("blocks-csv")

        with self.assertRaisesRegex(RuntimeError, "simulated artifact close failure"):
            store.close()
        self.assertEqual(
            closed,
            ["trials-jsonl", "keys-jsonl", "probes-jsonl", "trials-csv", "blocks-csv"],
        )

    def test_flip_callback_emits_marker_before_any_event_file_logging(self) -> None:
        class MarkerOutlet:
            name = "DSARTMarkers"
            stream_type = "Markers"
            source_id = "session-specific-source"

            def __init__(self) -> None:
                self.pushed: list[tuple[str, float | None]] = []

            def push(self, label: str, timestamp: float | None = None) -> None:
                self.pushed.append((label, timestamp))

        class Logger:
            def __init__(self) -> None:
                self.marked: list[tuple[tuple, dict]] = []

            def mark(self, *args: object, **kwargs: object) -> None:
                self.marked.append((args, kwargs))

        planned = {
            "global_trial_index": 7,
            "phase": "support",
            "is_practice": False,
            "condition": "go",
            "digit": 4,
            "block_index": 1,
            "sequence_id": "sequence",
        }
        holder: dict = {}
        outlet = MarkerOutlet()
        logger = Logger()
        label = "dynamic_sart_stimulus_onset__v=1__trial=7"

        with (
            patch("eegle.tasks.dynamic_sart.monotonic", return_value=42.0),
            patch("eegle.tasks.dynamic_sart.lsl_local_clock", return_value=100.0),
        ):
            _capture_flip_event(
                holder,
                outlet,
                label,
                planned,
                {"fixed_display_latency_ms": 8.0},
                "onset",
            )

        self.assertEqual(outlet.pushed, [(label, 100.008)])
        self.assertEqual(logger.marked, [])
        self.assertEqual(holder["onset_monotonic"], 42.008)
        self.assertEqual(holder["onset_event"]["marker_stream_source_id"], "session-specific-source")

        _log_captured_flip_event(holder, logger, "onset")
        self.assertEqual(len(logger.marked), 1)
        self.assertEqual(logger.marked[0][0], (label,))
        self.assertEqual(logger.marked[0][1]["lsl_timestamp"], 100.008)

    def test_countdown_flip_marker_precedes_event_file_logging(self) -> None:
        class MarkerOutlet:
            name = "DSARTMarkers"
            stream_type = "Markers"
            source_id = "session-specific-source"

            def __init__(self) -> None:
                self.pushed = []

            def push(self, label: str, timestamp: float | None = None) -> None:
                self.pushed.append((label, timestamp))

        class Logger:
            def __init__(self) -> None:
                self.marked = []

            def mark(self, *args: object, **kwargs: object) -> None:
                self.marked.append((args, kwargs))

        holder = {}
        outlet = MarkerOutlet()
        logger = Logger()
        label = "dynamic_sart_countdown_step__step=1__value=5"
        with patch("eegle.tasks.dynamic_sart.monotonic", return_value=42.0), patch(
            "eegle.tasks.dynamic_sart.lsl_local_clock", return_value=100.0
        ):
            _capture_countdown_flip(holder, outlet, label, "5", 1, {"fixed_display_latency_ms": 8.0})

        self.assertEqual(outlet.pushed, [(label, 100.008)])
        self.assertEqual(logger.marked, [])
        _log_captured_countdown_flip(holder, logger)
        self.assertEqual(logger.marked[0][0], (label,))
        self.assertEqual(logger.marked[0][1]["value"], "5")
        self.assertTrue(logger.marked[0][1]["scheduled_on_flip"])

    def test_psychopy_countdown_renders_required_sequence(self) -> None:
        class Stimulus:
            def __init__(self, _win, *, text: str, **_kwargs) -> None:
                self.text = text

            def draw(self) -> None:
                rendered.append(self.text)

        class Window:
            def __init__(self) -> None:
                self.callbacks = []

            def callOnFlip(self, callback, *args) -> None:
                self.callbacks.append((callback, args))

            def flip(self) -> None:
                callbacks, self.callbacks = self.callbacks, []
                for callback, args in callbacks:
                    callback(*args)

        class MarkerOutlet:
            name = "markers"
            stream_type = "Markers"
            source_id = "source"

            def __init__(self) -> None:
                self.labels = []

            def push(self, label: str, timestamp: float | None = None) -> None:
                self.labels.append(label)

        class Logger:
            def __init__(self) -> None:
                self.rows = []

            def mark(self, label: str, **metadata) -> None:
                self.rows.append({"label": label, **metadata})

        rendered = []
        outlet = MarkerOutlet()
        logger = Logger()
        keyboard = SimpleNamespace(poll=lambda **_kwargs: [])
        monitor = SimpleNamespace(check=lambda: SimpleNamespace(ok=True))
        ok, reason = _run_psychopy_countdown(
            Window(),
            SimpleNamespace(TextStim=Stimulus),
            keyboard,
            logger,
            outlet,
            {"fixed_display_latency_ms": 0.0},
            monitor,
            0.0,
        )
        self.assertTrue(ok)
        self.assertIsNone(reason)
        self.assertEqual(rendered, ["5", "4", "3", "2", "1", "GO!"])
        step_rows = [row for row in logger.rows if row["label"].startswith("dynamic_sart_countdown_step__")]
        self.assertEqual([row["value"] for row in step_rows], rendered)
        self.assertTrue(all(row["scheduled_on_flip"] for row in step_rows))

    def test_registration_and_factory(self) -> None:
        names = {spec.name for spec in list_task_specs()}
        self.assertIn("dynamic_sart", names)
        self.assertEqual(get_task_spec("dynamic_sart").name, "dynamic_sart")
        self.assertIsInstance(make_task_component("dynamic_sart", _config(), "dry-run", 24, "unit"), DynamicSartTask)
        self.assertIn("go_nogo", names)
        self.assertIn("pvt", names)

    def test_plan_is_deterministic_balanced_and_spaced(self) -> None:
        config = DynamicSartConfig.from_mapping(_config()["tasks"]["dynamic_sart"])
        first = build_dynamic_sart_plan(config)
        second = build_dynamic_sart_plan(config)
        different = build_dynamic_sart_plan(DynamicSartConfig.from_mapping(_config(seed=99)["tasks"]["dynamic_sart"]))
        self.assertEqual(first, second)
        self.assertNotEqual(first["sequence_id"], different["sequence_id"])
        validate_dynamic_sart_plan(first, config)
        self.assertEqual({row["phase"] for row in first["planned_trials"]}, {"support", "query"})
        for block_index in (1, 2):
            rows = [row for row in first["planned_trials"] if row["block_index"] == block_index]
            positions = [row["block_trial_index"] for row in rows if row["is_no_go"]]
            self.assertTrue(positions)
            self.assertGreater(positions[0], config.minimum_leading_go_trials)
            self.assertTrue(all(right - left - 1 >= 2 for left, right in zip(positions, positions[1:])))
            counts = {}
            for row in rows:
                if not row["is_no_go"]:
                    counts[row["digit"]] = counts.get(row["digit"], 0) + 1
            self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

    def test_smoke_override_preserves_support_query_and_manifest(self) -> None:
        config = DynamicSartConfig.from_mapping({})
        plan = build_dynamic_sart_plan(config, trial_override=24)
        self.assertTrue(plan["smoke_test_override"])
        self.assertEqual(plan["requested_trial_override"], 24)
        self.assertEqual(sum(row["phase"] == "support" for row in plan["planned_trials"]), 12)
        self.assertEqual(sum(row["phase"] == "query" for row in plan["planned_trials"]), 12)
        self.assertGreaterEqual(sum(row["phase"] == "support" and row["is_no_go"] for row in plan["planned_trials"]), 1)
        self.assertGreaterEqual(sum(row["phase"] == "query" and row["is_no_go"] for row in plan["planned_trials"]), 1)

    def test_scoring_keeps_primary_outcome_and_orthogonal_flags_separate(self) -> None:
        config = DynamicSartConfig.from_mapping(_config()["tasks"]["dynamic_sart"])
        correct_go = _score(config, no_go=False, events=[_key("1", "space", 10.35)])
        correct_no_go = _score(config, no_go=True, events=[])
        omission = _score(config, no_go=False, events=[])
        commission = _score(config, no_go=True, events=[_key("1", "space", 10.05), _key("2", "space", 10.30)])
        wrong_then_valid = _score(
            config,
            no_go=False,
            events=[_key("1", "x", 10.20, response=False), _key("2", "space", 10.40), _key("3", "space", 10.60)],
        )
        self.assertEqual(correct_go["primary_outcome"], "correct_go")
        self.assertAlmostEqual(correct_go["reaction_time_seconds"], 0.35)
        self.assertEqual(correct_no_go["primary_outcome"], "correct_no_go")
        self.assertEqual(omission["primary_outcome"], "omission_error")
        self.assertEqual(commission["primary_outcome"], "commission_error")
        self.assertTrue(commission["too_fast_response"])
        self.assertTrue(commission["multiple_response"])
        self.assertEqual(wrong_then_valid["first_response_key"], "x")
        self.assertEqual(wrong_then_valid["first_valid_response_key"], "space")
        self.assertTrue(wrong_then_valid["wrong_key_response"])
        self.assertTrue(wrong_then_valid["multiple_response"])

    def test_timing_fields_distinguish_schedules_observations_and_next_flip_measurements(self) -> None:
        config = DynamicSartConfig.from_mapping(_config()["tasks"]["dynamic_sart"])
        record = _score(config, no_go=False, events=[_key("1", "space", 10.35)])
        self.assertEqual(record["scheduled_response_window_close_monotonic"], 11.15)
        self.assertEqual(record["response_window_close_monotonic"], 11.17)
        self.assertAlmostEqual(record["response_window_close_overshoot_seconds"], 0.02)
        self.assertEqual(record["scheduled_next_trial_onset_monotonic"], 11.25)
        self.assertIsNone(record["actual_next_trial_onset_monotonic"])
        self.assertIsNone(record["actual_trial_duration_seconds"])

        next_record = dict(record)
        next_record["global_trial_index"] = 2
        next_record["stimulus_onset_monotonic"] = 11.31
        next_record["stimulus_onset_lsl"] = 21.31
        reconstructed = reconstruct_dynamic_sart_timing([record, next_record])
        self.assertEqual(reconstructed[0]["actual_next_trial_onset_monotonic"], 11.31)
        self.assertAlmostEqual(reconstructed[0]["actual_trial_duration_seconds"], 1.31)
        self.assertEqual(reconstructed[0]["timing_finalization_status"], "measured_from_next_stimulus_flip")
        self.assertEqual(reconstructed[1]["timing_finalization_status"], "terminal_no_following_stimulus_flip")

    def test_response_scoring_uses_logical_deadline_not_observed_loop_exit(self) -> None:
        config = DynamicSartConfig.from_mapping(_config()["tasks"]["dynamic_sart"])
        record = _score(config, no_go=False, events=[_key("late", "space", 11.16)])
        self.assertEqual(record["primary_outcome"], "omission_error")

    def test_legacy_timing_is_reclassified_as_scheduled_without_fabricating_observations(self) -> None:
        legacy = [
            {
                "global_trial_index": 1,
                "stimulus_onset_monotonic": 10.0,
                "response_window_close_monotonic": 11.15,
                "next_trial_onset_monotonic": 11.25,
                "actual_response_window_seconds": 1.15,
            },
            {"global_trial_index": 2, "stimulus_onset_monotonic": 11.31},
        ]
        reconstructed = reconstruct_dynamic_sart_timing(legacy)
        self.assertEqual(reconstructed[0]["scheduled_response_window_close_monotonic"], 11.15)
        self.assertEqual(reconstructed[0]["scheduled_next_trial_onset_monotonic"], 11.25)
        self.assertIsNone(reconstructed[0]["response_window_close_monotonic"])
        self.assertIsNone(reconstructed[0]["actual_response_window_seconds"])
        self.assertEqual(reconstructed[0]["actual_next_trial_onset_monotonic"], 11.31)

    def test_within_task_break_ignores_continue_until_minimum(self) -> None:
        class Stimulus:
            def __init__(self, _win, **_kwargs) -> None:
                pass

            def draw(self) -> None:
                pass

        class Window:
            def flip(self) -> None:
                pass

        responses = iter(
            [
                [{"is_escape_key": False, "is_response_key": True}],
                [{"is_escape_key": False, "is_response_key": True}],
            ]
        )
        keyboard = SimpleNamespace(poll=lambda **_kwargs: next(responses))
        with patch("eegle.tasks.dynamic_sart.monotonic", side_effect=[1.0, 1.0, 31.0, 31.0, 31.0]), patch(
            "eegle.tasks.dynamic_sart.sleep"
        ):
            continued, result = _show_bounded_break(
                Window(),
                SimpleNamespace(TextStim=Stimulus),
                keyboard,
                30.0,
                60.0,
                started_at=0.0,
            )
        self.assertTrue(continued)
        self.assertEqual(result["ignored_early_continue_presses"], 1)
        self.assertEqual(result["break_end_reason"], "participant_continue_after_minimum")
        self.assertGreaterEqual(result["actual_break_seconds"], 30.0)

    def test_practice_criteria_separate_go_and_no_go_accuracy(self) -> None:
        config = DynamicSartConfig.from_mapping(_config()["tasks"]["dynamic_sart"])
        records = [
            *[{"condition": "go", "correct": True, "premature_response": False} for _ in range(8)],
            *[{"condition": "go", "correct": False, "premature_response": False} for _ in range(1)],
            {"condition": "no_go", "correct": False, "premature_response": False},
        ]
        result = practice_criteria(records, config, comprehension_confirmed=True)
        self.assertGreaterEqual(result["go_accuracy"], 0.8)
        self.assertLess(result["no_go_accuracy"], 0.67)
        self.assertFalse(result["passed"])

    def test_practice_two_of_three_no_go_trials_meets_two_thirds_requirement(self) -> None:
        mapping = _config()["tasks"]["dynamic_sart"]
        mapping["practice"].update({"no_go_trials": 3, "trials_per_round": 27})
        config = DynamicSartConfig.from_mapping(mapping)
        records = [
            *[{"condition": "go", "correct": True, "premature_response": False} for _ in range(24)],
            *[{"condition": "no_go", "correct": True, "premature_response": False} for _ in range(2)],
            {"condition": "no_go", "correct": False, "premature_response": False},
        ]
        result = practice_criteria(records, config, comprehension_confirmed=True)
        self.assertEqual(result["no_go_correct_count"], 2)
        self.assertEqual(result["no_go_required_correct_count"], 2)
        self.assertTrue(result["passed"])

    def test_practice_feedback_distinguishes_repeat_from_final_failure(self) -> None:
        mapping = _config()["tasks"]["dynamic_sart"]
        mapping["practice"].update({"no_go_trials": 3, "trials_per_round": 27})
        config = DynamicSartConfig.from_mapping(mapping)
        criteria = {
            "go_correct_count": 22,
            "go_trial_count": 24,
            "go_required_correct_count": 20,
            "no_go_correct_count": 1,
            "no_go_trial_count": 3,
            "no_go_required_correct_count": 2,
            "anticipatory_response_rate": 0.0,
            "maximum_anticipatory_response_rate": 0.15,
        }
        repeat = _practice_status_text(criteria, config, will_repeat=True)
        final = _practice_status_text(criteria, config, will_repeat=False)
        self.assertIn("Practice will repeat", repeat)
        self.assertIn("1/3", repeat)
        self.assertIn("need 2", repeat)
        self.assertNotIn("will repeat", final)
        self.assertIn("experimental session will not start", final)

    def test_completion_text_reports_aborted_task_truthfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config()
            paths = create_session(config, task="dynamic_sart", participant_id="unit", root=Path(tmp))
            text = _completion_text(
                paths,
                {
                    "aborted": True,
                    "abort_reason": "practice_criteria_not_met",
                    "experimental_trials": 0,
                    "planned_experimental_trials": 10,
                },
            )
        self.assertIn("did not complete", text)
        self.assertIn("Practice criteria were not met", text)
        self.assertIn("Recorded 0 of 10", text)

    def test_dry_run_flushes_artifacts_and_orders_support_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config()
            paths = create_session(config, task="dynamic_sart", participant_id="unit", root=Path(tmp))
            result = DynamicSartTask(config, mode="dry-run", participant_id="unit").run(paths)
            rows = [json.loads(line) for line in (paths.events / "dynamic_sart_trials.jsonl").read_text().splitlines()]
            manifest = json.loads((paths.events / "stimulus_manifest.json").read_text())
            event_rows = [json.loads(line) for line in paths.events_jsonl.read_text().splitlines()]
            events = [row["label"].split("__", 1)[0] for row in event_rows]
            countdown = [row for row in event_rows if row["label"].startswith("dynamic_sart_countdown_step__")]
            self.assertEqual(result.summary["experimental_trials"], 24)
            self.assertEqual(sum(not row["is_practice"] for row in rows), 24)
            self.assertTrue(all("planned_jitter_seconds" in row for row in rows))
            self.assertEqual(events.count("dynamic_sart_support_complete"), 1)
            last_support = max(index for index, value in enumerate(events) if value == "dynamic_sart_trial_complete" and index < events.index("dynamic_sart_support_complete"))
            first_query = next(index for index, value in enumerate(events) if value == "dynamic_sart_stimulus_onset" and index > events.index("dynamic_sart_support_complete"))
            self.assertLess(last_support, events.index("dynamic_sart_support_complete"))
            self.assertLess(events.index("dynamic_sart_support_complete"), first_query)
            self.assertEqual([row["value"] for row in countdown], ["5", "4", "3", "2", "1", "GO!"])
            self.assertLess(events.index("dynamic_sart_countdown_end"), events.index("dynamic_sart_block_start"))
            self.assertEqual(len(manifest["planned_trials"]), 42)
            self.assertEqual(manifest["presented_trial_count"], len(rows))
            self.assertFalse((paths.events / "dynamic_sart_labels.csv").exists())
            self.assertFalse((paths.events / "dynamic_sart_label_contract.json").exists())

    def test_abort_preserves_completed_rows_and_full_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config(abort_after=5)
            paths = create_session(config, task="dynamic_sart", participant_id="unit", root=Path(tmp))
            result = DynamicSartTask(config, mode="dry-run", participant_id="unit").run(paths)
            rows = (paths.events / "dynamic_sart_trials.jsonl").read_text().splitlines()
            manifest = json.loads((paths.events / "stimulus_manifest.json").read_text())
            self.assertTrue(result.summary["aborted"])
            self.assertEqual(sum(not json.loads(row)["is_practice"] for row in rows), 5)
            self.assertEqual(len([row for row in manifest["planned_trials"] if not row["is_practice"]]), 24)
            self.assertTrue(manifest["aborted"])

    def test_versioned_markers_and_legacy_markers_remain_compatible(self) -> None:
        prefix = "dynamic_sart_stimulus_onset"
        label = f"{prefix}__v=1__trial=42__condition=go__digit=7__block=3__phase=query__practice=0"
        parsed = parse_marker_label(label, prefix)
        self.assertEqual(parsed["trial"], 42)
        self.assertEqual(parsed["digit"], 7)
        self.assertFalse(parsed["practice"])
        legacy = parse_marker_label("go_nogo_stimulus_onset_4_no_go_x_white", "go_nogo_stimulus_onset")
        self.assertEqual(legacy["trial"], 4)
        self.assertEqual(legacy["condition"], "no_go")
        cfg = EpochingConfig(marker_prefix=prefix, include_practice_trials=False)
        practice = MarkerEvent(label.replace("trial=42", "trial=1").replace("phase=query", "phase=practice").replace("practice=0", "practice=1"), 1.0)
        self.assertFalse(should_epoch_marker(practice, cfg))

    def test_manifest_loader_prefers_persisted_dsart_label(self) -> None:
        prefix = "dynamic_sart_stimulus_onset"
        label = f"{prefix}__v=1__trial=2__condition=go__digit=7__block=1__phase=support__practice=0"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stimulus_manifest.json"
            path.write_text(json.dumps({"task_name": "dynamic_sart", "trials": [{
                "global_trial_index": 2,
                "block_index": 1,
                "phase": "support",
                "is_practice": False,
                "condition": "go",
                "digit": 7,
                "stimulus_marker_label": label,
                "stimulus_onset_lsl": 123.5,
            }]}))
            markers = load_stimulus_manifest_markers(path, EpochingConfig(marker_prefix=prefix), timebase="lsl")
        self.assertEqual(len(markers), 1)
        self.assertEqual(markers[0].label, label)
        self.assertEqual(markers[0].metadata["trial"], 2)
        self.assertEqual(markers[0].metadata["digit"], 7)

    def test_feedback_actions_are_audited_without_state_change(self) -> None:
        observe = TaskAction("observe_only", "between_trials", "test", "one")
        adapt = TaskAction("adjust_isi", "between_trials", "test", "two")
        self.assertEqual(audit_dynamic_sart_action(observe)["status"], "accepted")
        rejected = audit_dynamic_sart_action(adapt)
        self.assertEqual(rejected["status"], "rejected")
        self.assertFalse(rejected["applied"])

    def test_optional_probe_persists_question_choices_timing_and_exclusion_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _config()
            config["tasks"]["dynamic_sart"]["thought_probes"] = {
                "enabled": True,
                "question": "Where was your attention?",
                "response_choices": ["on_task", "off_task"],
                "after_trials": [5],
                "max_seconds": 3.0,
                "exclusion_window_trials": 2,
            }
            paths = create_session(config, task="dynamic_sart", participant_id="unit", root=Path(tmp))
            DynamicSartTask(config, mode="dry-run", participant_id="unit").run(paths)
            probes = [json.loads(line) for line in (paths.events / "dynamic_sart_probes.jsonl").read_text().splitlines()]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["question"], "Where was your attention?")
        self.assertEqual(probes[0]["response_choices"], ["on_task", "off_task"])
        self.assertEqual(probes[0]["after_trial_index"], 5)
        self.assertGreaterEqual(probes[0]["offset_monotonic"], probes[0]["onset_monotonic"])
        self.assertEqual(probes[0]["exclusion_window_trials"], 2)

    def test_invalid_config_rejects_support_after_query_and_enabled_probe_without_schedule(self) -> None:
        with self.assertRaisesRegex(ValueError, "support blocks must all precede query"):
            DynamicSartConfig.from_mapping({"blocks": [
                {"name": "query", "phase": "query", "trials": 12},
                {"name": "support", "phase": "support", "trials": 12},
            ]})
        with self.assertRaisesRegex(ValueError, "after_trials"):
            DynamicSartConfig.from_mapping({"thought_probes": {"enabled": True}})
        with self.assertRaisesRegex(ValueError, "countdown_step_seconds"):
            DynamicSartConfig.from_mapping({"countdown_step_seconds": 0})


if __name__ == "__main__":
    unittest.main()
