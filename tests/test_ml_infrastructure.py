from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from eegle.analysis.classification import evaluate_classifier_session
from eegle.ml.calibration import select_binary_threshold
from eegle.ml.contracts import normalize_input_contract, select_contract_channels
from eegle.ml.registry import get_model_spec, list_model_kinds, resolve_model_kind
from eegle.ml.targets import build_training_target
from eegle.realtime.classification import model_prediction_row, prepare_classifier_epoch
from eegle.realtime.models import ModelPrediction, load_epoch_dataset, make_model_adapter, train_epoch_model
from eegle.realtime.policy import make_decision_policy


class ModularMlInfrastructureTests(unittest.TestCase):
    def test_registry_exposes_model_families_and_aliases(self) -> None:
        self.assertEqual(resolve_model_kind("cnn_eegnet"), "torch_eegnet")
        self.assertIn("foundation_bendr", list_model_kinds())
        eegnet = get_model_spec("cnn_eegnet")
        labram = get_model_spec("foundation_labram")

        self.assertEqual(eegnet.family, "cnn")
        self.assertTrue(eegnet.trainable)
        self.assertEqual(labram.family, "eeg_foundation")
        self.assertFalse(labram.primary_realtime_allowed)
        self.assertTrue(labram.external_checkpoint)

    def test_channel_contract_selects_required_and_optional_channels(self) -> None:
        contract = normalize_input_contract(
            {
                "input_contract": {
                    "channel_order": ["Pz", "Cz", "Oz"],
                    "required_channels": ["Pz", "Cz"],
                    "optional_channels": ["Oz"],
                    "missing_channel_policy": "error",
                    "channel_groups": {"posterior": ["Pz", "Oz"]},
                }
            },
            fallback_channel_names=["Fz", "Cz", "Pz", "Oz"],
        )

        indices, selected = select_contract_channels(["Fz", "Cz", "Pz", "Oz"], contract)

        self.assertEqual(indices, [2, 1, 3])
        self.assertEqual(selected, ["Pz", "Cz", "Oz"])
        self.assertEqual(contract["channel_groups"]["posterior"], ["Pz", "Oz"])

    def test_attention_lapse_target_can_be_derived_from_behavior_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "session"
            (session / "events").mkdir(parents=True)
            manifest = {
                "trials": [
                    {"trial": 1, "stimulus": {"is_no_go": False}, "response": {"correct_press": True, "reaction_time_seconds": 0.25}},
                    {"trial": 2, "stimulus": {"is_no_go": False}, "response": {"correct_press": True, "reaction_time_seconds": 0.90}},
                    {"trial": 3, "stimulus": {"is_no_go": True}, "response": {"button_press_count": 1}},
                    {"trial": 4, "stimulus": {"is_no_go": False}, "response": {"button_press_count": 0}},
                ]
            }
            (session / "events" / "stimulus_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            data = _npz_like(
                {
                    "X": np.zeros((4, 2, 5)),
                    "y": np.asarray([0, 0, 1, 0]),
                    "trials": np.arange(1, 5),
                }
            )

            target = build_training_target(
                data,
                {"target": "attention_lapse_binary", "session_dirs": [str(session)], "attention_lapse_threshold": 0.5},
            )

        self.assertEqual(target.label_mapping, {"attentive": 0, "attention_lapse": 1})
        self.assertEqual(target.metadata["source"], "session_behavior")
        self.assertTrue(target.eligible.all())
        self.assertIn(1, target.y.tolist())

    def test_multi_session_attention_lapse_join_uses_source_session_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = [root / "s1", root / "s2"]
            for session in sessions:
                (session / "events").mkdir(parents=True)
            (sessions[0] / "events" / "stimulus_manifest.json").write_text(
                json.dumps(
                    {
                        "trials": [
                            {"trial": 1, "stimulus": {"is_no_go": False}, "response": {"correct_press": True, "reaction_time_seconds": 0.2, "button_press_count": 1}},
                            {"trial": 2, "stimulus": {"is_no_go": False}, "response": {"correct_press": True, "reaction_time_seconds": 0.3, "button_press_count": 1}},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (sessions[1] / "events" / "stimulus_manifest.json").write_text(
                json.dumps(
                    {
                        "trials": [
                            {"trial": 1, "stimulus": {"is_no_go": True}, "response": {"button_press_count": 1}},
                            {"trial": 2, "stimulus": {"is_no_go": False}, "response": {"button_press_count": 0}},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            times = np.arange(5, dtype=float) / 100.0 - 0.02
            channels = np.asarray(["Cz", "Pz"], dtype=object)
            for index, session in enumerate(sessions):
                np.savez(
                    root / f"epochs{index}.npz",
                    X=np.zeros((2, len(channels), times.size)),
                    y=np.asarray([0, 1]),
                    trials=np.asarray([1, 2]),
                    times=times,
                    channel_names=channels,
                    sample_rate_hz=np.asarray([100.0]),
                )

            data, _paths = load_epoch_dataset([root / "epochs0.npz", root / "epochs1.npz"])
            target = build_training_target(
                data,
                {
                    "target": "attention_lapse_binary",
                    "session_dirs": [str(session) for session in sessions],
                    "attention_lapse_threshold": 0.5,
                },
            )

        self.assertEqual(np.asarray(data["source_session_index"], dtype=int).tolist(), [0, 0, 1, 1])
        self.assertEqual(target.y.tolist(), [0, 0, 1, 1])
        self.assertEqual(target.metadata["score_join"], "session_index_and_trial")

    def test_multi_session_attention_lapse_refuses_unprovenanced_trial_join(self) -> None:
        data = _npz_like(
            {
                "X": np.zeros((4, 2, 5)),
                "y": np.asarray([0, 1, 0, 1]),
                "trials": np.asarray([1, 2, 1, 2]),
            }
        )

        with self.assertRaisesRegex(ValueError, "source_session_index"):
            build_training_target(
                data,
                {
                    "target": "attention_lapse_binary",
                    "session_dirs": ["/tmp/session-a", "/tmp/session-b"],
                },
            )

    def test_multi_session_epoch_merge_refuses_partial_per_epoch_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            times = np.arange(5, dtype=float) / 100.0 - 0.02
            channels = np.asarray(["Cz", "Pz"], dtype=object)
            base = {
                "X": np.zeros((2, len(channels), times.size)),
                "y": np.asarray([0, 1]),
                "trials": np.asarray([1, 2]),
                "times": times,
                "channel_names": channels,
                "sample_rate_hz": np.asarray([100.0]),
            }
            np.savez(root / "a.npz", **base, attention_lapse_score=np.asarray([0.1, 0.9]))
            np.savez(root / "b.npz", **base)

            with self.assertRaisesRegex(ValueError, "partial per-epoch field attention_lapse_score"):
                load_epoch_dataset([root / "a.npz", root / "b.npz"])

    def test_epoch_contract_rejects_unsupported_resampling_and_sample_count_mismatch(self) -> None:
        epoch = np.zeros((5, 2), dtype=float)
        contract = {
            "channel_names": ["Cz", "Pz"],
            "sample_rate_hz": 100.0,
            "sample_count": 6,
            "resampling": "none",
        }

        with self.assertRaisesRegex(ValueError, "sample count"):
            prepare_classifier_epoch(epoch, 100.0, ["Cz", "Pz"], {}, contract)
        with self.assertRaisesRegex(ValueError, "resampling mode"):
            prepare_classifier_epoch(epoch, 100.0, ["Cz", "Pz"], {}, {**contract, "sample_count": 5, "resampling": "linear"})

    def test_threshold_calibration_selects_non_default_threshold_when_better(self) -> None:
        y = np.asarray([0, 0, 1, 1])
        probabilities = np.asarray([0.1, 0.4, 0.45, 0.7])

        calibration = select_binary_threshold(y, probabilities)

        self.assertEqual(calibration["status"], "ok")
        self.assertLess(float(calibration["selected_threshold"]), 0.5)
        self.assertGreaterEqual(float(calibration["selected_metrics"]["balanced_accuracy"]), 0.75)

    def test_prediction_row_uses_attention_lapse_fields_without_condition_truth(self) -> None:
        prediction = ModelPrediction(
            "attention_lapse",
            0.8,
            probability=0.8,
            model_kind="foundation_bendr",
            metadata={
                "target": "attention_lapse_binary",
                "model_family": "eeg_foundation",
                "calibrated_threshold": 0.7,
                "calibration_id": "cal-1",
            },
        )

        row = model_prediction_row({"trial": 3, "epoch_index": 2}, prediction.to_payload(), model_id="primary", role="primary", latency_ms=4.0, quality={"valid": True})

        self.assertIsNone(row["predicted_condition"])
        self.assertEqual(row["prediction_label"], "attention_lapse")
        self.assertEqual(row["probability_attention_lapse"], 0.8)
        self.assertIsNone(row["probability_no_go"])
        self.assertEqual(row["calibrated_threshold"], 0.7)

    def test_foundation_adapter_requires_user_supplied_checkpoint(self) -> None:
        adapter = make_model_adapter("foundation_bendr", {})

        with self.assertRaisesRegex(RuntimeError, "artifact_path"):
            adapter.predict_epoch(np.zeros((10, 2)), 100.0, ["Cz", "Pz"], {})

    def test_attention_lapse_stimulation_policy_is_explicitly_gated(self) -> None:
        prediction = ModelPrediction("attention_lapse", 0.9, probability=0.9, model_kind="fixture")
        disabled = make_decision_policy("attention_lapse_stimulation", {"allow_stimulation": False, "research_safety_ack": True})
        enabled = make_decision_policy(
            "attention_lapse_stimulation",
            {
                "allow_stimulation": True,
                "research_safety_ack": True,
                "probability_threshold": 0.7,
                "refractory_seconds": 10.0,
            },
        )

        self.assertEqual(disabled.decide(prediction, {"trial": 1, "quality": {"valid": True}})[0].action, "observe_only")
        first = enabled.decide(prediction, {"trial": 1, "epoch_index": 1, "quality": {"valid": True}})[0]
        second = enabled.decide(prediction, {"trial": 2, "epoch_index": 2, "quality": {"valid": True}})[0]

        self.assertEqual(first.action, "stimulation_candidate")
        self.assertTrue(first.parameters["requires_external_stimulator_confirmation"])
        self.assertEqual(second.action, "observe_only")

    def test_attention_lapse_evaluation_scores_behavior_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "events").mkdir()
            (root / "realtime").mkdir()
            manifest = {
                "trials": [
                    {"trial": 1, "stimulus": {"is_no_go": False}, "response": {"correct_press": True, "reaction_time_seconds": 0.2}},
                    {"trial": 2, "stimulus": {"is_no_go": False}, "response": {"correct_press": True, "reaction_time_seconds": 0.9}},
                    {"trial": 3, "stimulus": {"is_no_go": True}, "response": {"button_press_count": 1}},
                ]
            }
            (root / "events" / "stimulus_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            rows = [
                {
                    "schema": "eegle.model_prediction.v1",
                    "status": "predicted",
                    "trial": 1,
                    "model_id": "primary",
                    "model_role": "primary",
                    "model_kind": "erp_roi_logreg",
                    "target": "attention_lapse_binary",
                    "prediction_label": "attentive",
                    "probability_attention_lapse": 0.1,
                },
                {
                    "schema": "eegle.model_prediction.v1",
                    "status": "predicted",
                    "trial": 3,
                    "model_id": "primary",
                    "model_role": "primary",
                    "model_kind": "erp_roi_logreg",
                    "target": "attention_lapse_binary",
                    "prediction_label": "attention_lapse",
                    "probability_attention_lapse": 0.9,
                },
            ]
            (root / "realtime" / "model_predictions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            summary = evaluate_classifier_session(root)

        self.assertEqual(summary["metrics"]["primary"]["target"], "attention_lapse_binary")
        self.assertEqual(summary["metrics"]["primary"]["status"], "ok")

    @unittest.skipIf(importlib.util.find_spec("sklearn") is None or importlib.util.find_spec("joblib") is None, "sklearn/joblib not installed")
    def test_train_model_accepts_attention_lapse_score_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            times = np.arange(101, dtype=float) / 100.0 - 0.2
            rng = np.random.default_rng(9)
            channels = np.asarray(["Fz", "Cz", "Pz", "P3", "P4", "Oz"], dtype=object)
            x = rng.normal(0.0, 1.0, size=(8, len(channels), times.size))
            x[4:, 2:5, (times >= 0.3) & (times <= 0.6)] += 3.0
            np.savez(
                root / "epochs.npz",
                X=x,
                y=np.asarray([0, 1, 0, 1, 0, 1, 0, 1]),
                trials=np.arange(1, 9),
                times=times,
                channel_names=channels,
                sample_rate_hz=np.asarray([100.0]),
                attention_lapse_score=np.asarray([0.1, 0.2, 0.1, 0.2, 0.8, 0.9, 0.8, 0.9]),
            )

            result = train_epoch_model(
                "erp_roi_logreg",
                root / "epochs.npz",
                root / "model",
                {"target": "attention_lapse_score", "permutations": 1},
            )
            manifest = json.loads((Path(result["bundle_path"]) / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["target"], "attention_lapse_score")
        self.assertEqual(result["label_mapping"], {"attentive": 0, "attention_lapse": 1})
        self.assertEqual(result["calibration"]["status"], "ok")
        self.assertEqual(result["metrics"]["target_spec"]["score_target_mode"], "thresholded_behavior_score")
        self.assertFalse(result["metrics"]["target_spec"]["score_regression_supported"])
        self.assertTrue(manifest["training_source"]["path_values_redacted"])
        self.assertNotIn("epochs_npz", manifest["training_source"])


class _npz_like:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values
        self.files = tuple(values)

    def __getitem__(self, key: str) -> object:
        return self._values[key]


if __name__ == "__main__":
    unittest.main()
