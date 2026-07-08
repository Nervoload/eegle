from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from eegle.analysis.classification import replay_classifier_session
from eegle.config import load_config
from eegle.pipelines import attention8
from eegle.realtime.epoching import MarkerEvent
from eegle.realtime.event_features import EngineInputCaptureWriter
from eegle.realtime.models import PreparedEpochCache, make_model_adapter, train_epoch_model
from eegle.realtime.online_adaptation import AdaptationUpdateResult
from eegle.realtime.online_labels import OnlineAttentionLapseLabeler, trial_complete_from_event_record
from eegle.session import create_session
from eegle.tasks.go_nogo import GoNoGoTask
from eegle.workers.realtime_processor import _process_ready_adaptation_updates


class Attention8OnlineAdaptationTests(unittest.TestCase):
    def test_attention8_config_defaults_are_safe_and_observe_only(self) -> None:
        config = load_config("configs/forward_attention_lapse_go_nogo8.json")
        adaptation = config["realtime"]["adaptation"]

        self.assertFalse(adaptation["enabled"])
        self.assertTrue(adaptation["update_primary"])
        self.assertFalse(adaptation["update_shadows"])
        self.assertEqual(adaptation["label_mode"], "slow_go_rt")
        self.assertTrue(config["realtime"]["decision_policy"]["enabled"])
        self.assertFalse(config["realtime"]["decision_policy"]["allow_task_adaptation"])
        self.assertFalse(config["realtime"]["decision_policy"]["allow_stimulation"])
        self.assertFalse(config["realtime"]["feedback"]["allow_task_adaptation"])
        self.assertFalse(config["realtime"]["feedback"]["allow_stimulation"])

    def test_attention8_online_cli_exposes_explicit_adaptation_opt_in(self) -> None:
        args = attention8.build_parser().parse_args(
            [
                "online",
                "--model-dir",
                "/tmp/models",
                "--enable-adaptation",
                "--adaptation-label-mode",
                "composite_lapse",
                "--min-correct-go-rts-for-threshold",
                "5",
            ]
        )

        self.assertTrue(args.enable_adaptation)
        self.assertEqual(args.adaptation_label_mode, "composite_lapse")
        self.assertEqual(args.min_correct_go_rts_for_threshold, 5)

    def test_slow_go_rt_labeler_uses_only_past_correct_go_rts(self) -> None:
        labeler = OnlineAttentionLapseLabeler(
            {
                "label_mode": "slow_go_rt",
                "slow_rt_quantile": 0.5,
                "min_correct_go_rts_for_threshold": 2,
            }
        )

        first = labeler.label_trial_complete(_go_event(1, 0.2))
        second = labeler.label_trial_complete(_go_event(2, 0.4))
        third = labeler.label_trial_complete(_go_event(3, 0.5))

        self.assertEqual(first.status, "skipped")
        self.assertEqual(first.reason, "insufficient_past_correct_go_rts")
        self.assertEqual(second.status, "skipped")
        self.assertEqual(second.metadata["past_correct_go_rt_count"], 1)
        self.assertEqual(third.status, "labeled")
        self.assertEqual(third.label, 1)
        self.assertAlmostEqual(third.threshold_value or 0.0, 0.3)
        self.assertEqual(third.metadata["past_correct_go_rt_count"], 2)

    def test_go_nogo_dry_run_writes_delayed_trial_complete_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config("configs/forward_attention_lapse_go_nogo8.json")
            config["runtime"]["session_root"] = tmp
            config["telemetry"]["console_level"] = "quiet"
            paths = create_session(config, task="go_nogo", participant_id="unit", root=tmp)

            GoNoGoTask(config, mode="dry-run", trials=2, participant_id="unit").run(paths)

            rows = [json.loads(line) for line in paths.events_jsonl.read_text(encoding="utf-8").splitlines()]
            complete = [trial_complete_from_event_record(row) for row in rows]
            complete = [row for row in complete if row is not None]

        self.assertEqual(len(complete), 2)
        self.assertEqual(complete[0]["event_type_name"], "go_nogo_trial_complete")
        self.assertIn(complete[0]["condition"], {"go", "no_go"})
        self.assertIn("correct", complete[0])
        self.assertIn("omission_error", complete[0])
        self.assertIn("commission_error", complete[0])

    def test_attention_challenge_cues_are_recorded_in_dry_run_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config("configs/forward_attention_lapse_go_nogo8.json")
            config["runtime"]["session_root"] = tmp
            config["telemetry"]["console_level"] = "quiet"
            config["tasks"]["go_nogo"]["attention_challenge"] = {
                "enabled": True,
                "cue_trials": [2],
                "window_trials": 2,
                "cue_duration_seconds": 0.0,
                "expected_state_label": "deliberate_inattention",
                "cue_text": "Let attention drift for this test window.",
            }
            paths = create_session(config, task="go_nogo", participant_id="unit", root=tmp)

            GoNoGoTask(config, mode="dry-run", trials=3, participant_id="unit").run(paths)

            manifest = json.loads((paths.events / "stimulus_manifest.json").read_text(encoding="utf-8"))
            events = [json.loads(line) for line in paths.events_jsonl.read_text(encoding="utf-8").splitlines()]

        trials = manifest["trials"]
        self.assertEqual(trials[0]["attention_challenge"]["expected_state"], "normal")
        self.assertTrue(trials[1]["attention_challenge"]["is_cue_trial"])
        self.assertEqual(trials[1]["attention_challenge"]["expected_state"], "deliberate_inattention")
        self.assertEqual(trials[2]["attention_challenge"]["cue_start_trial"], 2)
        cues = [row for row in events if row["label"] == "attention_challenge_cue"]
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["trial"], 2)
        complete = [row for row in events if row["label"] == "go_nogo_trial_complete" and row["trial"] == 2]
        self.assertEqual(complete[0]["metadata"]["attention_challenge"]["expected_state"], "deliberate_inattention")

    def test_trial_complete_does_not_update_until_prediction_exists(self) -> None:
        writer = _MemoryWriter()
        telemetry = _SilentTelemetry()
        labeler = OnlineAttentionLapseLabeler(
            {
                "label_mode": "slow_go_rt",
                "slow_rt_quantile": 0.5,
                "min_correct_go_rts_for_threshold": 1,
                "initial_correct_go_rts": [0.3],
            }
        )
        outcomes = {1: _go_event(1, 0.5)}
        pending: dict[int, list[dict[str, object]]] = {}

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            _process_ready_adaptation_updates(
                pending,
                outcomes,
                labeler,
                _adaptation_config(),
                writer,
                state_dir,
                telemetry,
            )
            self.assertEqual(writer.rows, [])
            self.assertIn(1, outcomes)

            adapter = _FakeAdaptiveAdapter()
            pending[1] = [
                {
                    "adapter": adapter,
                    "prepared": object(),
                    "prediction_row": {
                        "trial": 1,
                        "prediction_label": "attentive",
                        "probability_attention_lapse": 0.8,
                    },
                    "model_id": "primary",
                    "model_role": "primary",
                    "model_kind": "foundation_prototype",
                }
            ]
            _process_ready_adaptation_updates(
                pending,
                outcomes,
                labeler,
                _adaptation_config(),
                writer,
                state_dir,
                telemetry,
            )

        self.assertEqual(len(writer.rows), 1)
        self.assertEqual(writer.rows[0]["update_status"], "updated")
        self.assertEqual(writer.rows[0]["true_online_label"], 1)
        self.assertNotIn(1, outcomes)
        self.assertEqual(adapter.labels, [1])

    @unittest.skipIf(importlib.util.find_spec("joblib") is None, "joblib not installed")
    def test_foundation_prototype_online_update_changes_lapse_prototype_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = _write_attention_epochs(root)
            result = train_epoch_model(
                "foundation_prototype",
                root / "epochs.npz",
                root / "prototype",
                _training_config(),
            )
            adapter = make_model_adapter(
                "foundation_prototype",
                {"bundle_path": result["bundle_path"], "input_layout": "samples_x_channels"},
            )
            prepared = PreparedEpochCache(
                data["x"][0].T,
                100.0,
                data["channels"],
                {"relative_times": data["times"].tolist(), "epoch_window_seconds": [-2.0, 0.0]},
            )
            before = adapter.snapshot_adaptation_state()

            update = adapter.update_after_trial(
                prepared,
                1,
                {
                    "trial": 9,
                    "label_mode": "slow_go_rt",
                    "model_id": "primary",
                    "model_role": "primary",
                    "pre_update_probability": 0.8,
                    "adaptation_config": _adaptation_config(),
                },
            )
            after = adapter.snapshot_adaptation_state()

        self.assertEqual(update.status, "updated")
        self.assertIsNotNone(before)
        self.assertIsNotNone(after)
        assert before is not None and after is not None
        self.assertEqual(after["class_counts"]["1"], before["class_counts"]["1"] + 1)
        self.assertNotEqual(after["calibration_state_hash"], before["calibration_state_hash"])
        self.assertEqual(after["update_policy"]["encoder_update_policy"], "frozen")

    @unittest.skipIf(
        importlib.util.find_spec("sklearn") is None or importlib.util.find_spec("joblib") is None,
        "sklearn/joblib not installed",
    )
    def test_causal_bandpower_online_update_recalibrates_threshold_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = _write_attention_epochs(root)
            result = train_epoch_model(
                "causal_bandpower_logreg",
                root / "epochs.npz",
                root / "bandpower",
                _training_config(),
            )
            adapter = make_model_adapter(
                "causal_bandpower_logreg",
                {"bundle_path": result["bundle_path"], "input_layout": "samples_x_channels"},
            )
            prepared = PreparedEpochCache(
                data["x"][0].T,
                100.0,
                data["channels"],
                {"relative_times": data["times"].tolist(), "epoch_window_seconds": [-2.0, 0.0]},
            )

            first = adapter.update_after_trial(
                prepared,
                0,
                {
                    "trial": 9,
                    "label_mode": "slow_go_rt",
                    "model_id": "primary",
                    "model_role": "primary",
                    "pre_update_probability": 0.2,
                    "adaptation_config": {**_adaptation_config(), "min_examples_for_threshold_update": 2},
                },
            )
            second = adapter.update_after_trial(
                prepared,
                1,
                {
                    "trial": 10,
                    "label_mode": "slow_go_rt",
                    "model_id": "primary",
                    "model_role": "primary",
                    "pre_update_probability": 0.8,
                    "adaptation_config": {**_adaptation_config(), "min_examples_for_threshold_update": 2},
                },
            )
            state = adapter.snapshot_adaptation_state()

        self.assertEqual(first.status, "updated")
        self.assertEqual(second.status, "updated")
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["support_size"], 2)
        self.assertEqual(state["class_counts"], {"0": 1, "1": 1})
        self.assertIsNotNone(state["selected_threshold"])

    @unittest.skipIf(importlib.util.find_spec("joblib") is None, "joblib not installed")
    def test_replay_reproduces_adaptation_update_rows_and_state_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "realtime").mkdir()
            (root / "events").mkdir()
            data = _write_attention_epochs(root)
            result = train_epoch_model(
                "foundation_prototype",
                root / "epochs.npz",
                root / "prototype",
                _training_config(),
            )
            header = {
                "mode": "classifier",
                "sample_rate_hz": 100.0,
                "channel_names": data["channels"],
                "epoching_config": {
                    "enabled": True,
                    "marker_prefix": "go_nogo_stimulus_onset",
                    "tmin_seconds": -2.0,
                    "tmax_seconds": 0.0,
                    "include_practice_trials": False,
                    "data_source": "raw",
                },
                "quality_gate": {"minimum_channel_std_uv": 0.0},
                "models": [
                    {
                        "id": "primary",
                        "role": "primary",
                        "kind": "foundation_prototype",
                        "config": {
                            "bundle_path": result["bundle_path"],
                            "input_layout": "samples_x_channels",
                        },
                    }
                ],
                "adaptation_config": {
                    **_adaptation_config(),
                    "label_mode": "slow_go_rt",
                    "slow_rt_quantile": 0.5,
                    "min_correct_go_rts_for_threshold": 1,
                    "initial_correct_go_rts": [0.3],
                    "allowed_model_kinds": ["foundation_prototype"],
                },
            }
            capture = EngineInputCaptureWriter(root / "realtime" / "engine_input.bin", header)
            eeg_times = np.arange(0.0, 5.01, 0.01)
            eeg = np.random.default_rng(77).normal(0.0, 1.0, size=(eeg_times.size, len(data["channels"])))
            capture.write_eeg(eeg_times, eeg)
            capture.write_marker(MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 2.1))
            capture.write_marker(MarkerEvent("go_nogo_stimulus_onset_2_go_square_blue", 4.5))
            capture.close()
            event_rows = [
                _event_jsonl_row(_go_event(1, 0.4)),
                _event_jsonl_row(_go_event(2, 0.2)),
            ]
            (root / "events" / "events.jsonl").write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in event_rows),
                encoding="utf-8",
            )

            first = replay_classifier_session(root)
            replay_dir = root / "reports" / "classification"
            (root / "realtime" / "model_predictions.jsonl").write_text(
                (replay_dir / "replay_predictions.jsonl").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / "realtime" / "adaptation_updates.jsonl").write_text(
                (replay_dir / "replay_adaptation_updates.jsonl").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            second = replay_classifier_session(root)

        self.assertEqual(first["adaptation_replay"]["status"], "replay_only")
        self.assertEqual(second["status"], "pass")
        self.assertEqual(second["adaptation_replay"]["status"], "pass")
        self.assertEqual(second["adaptation_replay"]["difference_count"], 0)
        self.assertTrue(second["adaptation_replay"]["final_state_hashes"]["primary"])


def _go_event(trial: int, rt: float) -> dict[str, object]:
    return {
        "trial": trial,
        "practice": False,
        "is_no_go": False,
        "condition": "go",
        "reaction_time_seconds": rt,
        "correct": True,
        "correct_press": 1,
        "button_press_count": 1,
        "omission_error": 0,
        "commission_error": 0,
    }


def _event_jsonl_row(event: dict[str, object]) -> dict[str, object]:
    return {
        "label": "go_nogo_trial_complete",
        "timestamp": float(event["trial"]),
        "trial": event["trial"],
        "event_type": "EVENT",
        "value": None,
        "metadata": {
            **event,
            "schema_version": 1,
            "event_type_name": "go_nogo_trial_complete",
            "source": "test",
        },
    }


def _adaptation_config() -> dict[str, object]:
    return {
        "enabled": True,
        "update_primary": True,
        "update_shadows": False,
        "allowed_model_kinds": ["foundation_prototype", "causal_bandpower_logreg"],
        "snapshot_every_updates": 1,
        "threshold_metric": "balanced_accuracy",
        "strict_no_leakage": True,
    }


def _training_config() -> dict[str, object]:
    return {
        "target": "attention_lapse_binary",
        "attention_lapse_label": "slow_go_rt",
        "epoch_data_source": "causal_preprocessed",
        "prediction_window_seconds": [-2.0, 0.0],
        "baseline_seconds": [-2.0, 0.0],
        "support_trials": 8,
        "permutations": 1,
        "decision_probability": 0.5,
    }


def _write_attention_epochs(root: Path) -> dict[str, object]:
    rng = np.random.default_rng(123)
    times = np.linspace(-2.0, 0.0, 201)
    channels = ["Fz", "Cz", "Pz"]
    x = rng.normal(0.0, 1.0, size=(8, len(channels), times.size))
    labels = np.asarray([0, 0, 0, 0, 1, 1, 1, 1], dtype=int)
    x[labels == 1, :, -40:] += 1.5
    np.savez(
        root / "epochs.npz",
        X=x,
        y=labels,
        trials=np.arange(1, labels.size + 1),
        times=times,
        channel_names=np.asarray(channels, dtype=object),
        sample_rate_hz=np.asarray([100.0]),
        slow_go_rt=labels,
    )
    return {"x": x, "times": times, "channels": channels}


class _MemoryWriter:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def write(self, row: dict[str, object]) -> None:
        self.rows.append(row)


class _SilentTelemetry:
    def emit(self, *args: object, **kwargs: object) -> None:
        return None


class _FakeAdaptiveAdapter:
    supports_online_update = True

    def __init__(self) -> None:
        self.labels: list[int] = []

    def snapshot_adaptation_state(self) -> dict[str, object]:
        return {
            "schema": "eegle.online_adaptation_state.v1",
            "model_kind": "foundation_prototype",
            "target": "attention_lapse_binary",
            "label_mode": "slow_go_rt",
            "model_id": "primary",
            "model_role": "primary",
            "support_size": len(self.labels),
            "class_counts": {
                "0": sum(label == 0 for label in self.labels),
                "1": sum(label == 1 for label in self.labels),
            },
            "calibration_state_hash": f"hash-{len(self.labels)}",
        }

    def update_after_trial(
        self,
        prepared: object,
        label: int,
        metadata: dict[str, object],
    ) -> AdaptationUpdateResult:
        before = self.snapshot_adaptation_state()
        self.labels.append(int(label))
        after = self.snapshot_adaptation_state()
        return AdaptationUpdateResult(status="updated", state_before=before, state_after=after, update_time_ms=0.1)


if __name__ == "__main__":
    unittest.main()
