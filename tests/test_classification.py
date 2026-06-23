from __future__ import annotations

import json
import importlib.util
import random
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from eegle.analysis.classification import evaluate_classifier_session, replay_classifier_session
from eegle.config import load_config
from eegle.hardware.system import CheckResult
from eegle.pipelines.classify8 import _validate_online_model_bundles, build_parser, train as classify8_train
from eegle.realtime.classification import (
    assess_epoch_quality,
    baseline_correct,
    extract_erp_roi_features,
    load_model_bundle,
    prepare_classifier_epoch,
    sanitize_model_metadata,
    write_model_bundle,
)
from eegle.realtime.demo_classifier import DEMO_DISCLOSURE, demo_config_from, demo_prediction_from_marker
from eegle.realtime.epoching import EpochingConfig, MarkerEvent, RealtimeEpocher
from eegle.realtime.event_features import EngineInputCaptureWriter
from eegle.realtime.models import PreparedEpochCache, prepare_artifact_epoch, train_epoch_model
from eegle.session import create_session
from eegle.telemetry import Telemetry
from eegle.workers.dashboard import DASHBOARD_HTML, DemoPredictionBridge, dashboard_snapshot
from eegle.workers.realtime_processor import _load_classifier_models


class ClassificationTests(unittest.TestCase):
    def test_classifier_config_is_observe_only_and_excludes_event_features(self) -> None:
        config = load_config("configs/forward_go_nogo_classifier8.json")
        self.assertTrue(config["realtime"]["classifier"]["enabled"])
        self.assertFalse(config["realtime"]["event_features"]["enabled"])
        self.assertFalse(config["realtime"]["feedback"]["allow_task_adaptation"])
        self.assertFalse(config["processes"]["feedback"]["enabled"])

    def test_baseline_and_roi_features_are_deterministic(self) -> None:
        times = np.arange(101, dtype=float) / 100.0 - 0.2
        channels = ["Fz", "Cz", "Pz", "P3", "P4", "Oz"]
        epoch = np.tile(np.linspace(2.0, 3.0, times.size), (len(channels), 1))
        epoch[0:2, (times >= 0.2) & (times <= 0.35)] -= 4.0
        epoch[1:5, (times >= 0.3) & (times <= 0.6)] += 6.0

        corrected = baseline_correct(epoch, times)
        first = extract_erp_roi_features(corrected, times, channels)
        second = extract_erp_roi_features(corrected, times, channels)

        self.assertEqual(first, second)
        self.assertAlmostEqual(float(np.mean(corrected[:, times <= 0.0])), 0.0, places=8)
        self.assertLess(first["n2_min_uv"], 0.0)
        self.assertGreater(first["p3_max_uv"], 0.0)

    def test_model_metadata_is_label_blind(self) -> None:
        sanitized = sanitize_model_metadata(
            {
                "relative_times": [-0.2, 0.0, 0.8],
                "epoch_window_seconds": [-0.2, 0.8],
                "trial": 4,
                "condition": "no_go",
                "training_label": 1,
                "stimulus": {"is_no_go": True},
                "response": {"correct": True},
            }
        )

        self.assertEqual(set(sanitized), {"relative_times", "epoch_window_seconds"})

    def test_prepare_classifier_epoch_reorders_channels_and_rejects_incompatible_rate(self) -> None:
        times = np.arange(101, dtype=float) / 100.0 - 0.2
        samples_channels = np.column_stack([np.ones(times.size), np.full(times.size, 3.0), np.full(times.size, 2.0)])
        contract = {
            "channel_names": ["Fz", "Cz"],
            "sample_rate_hz": 100.0,
            "epoch_window_seconds": [-0.2, 0.8],
            "baseline_seconds": [-0.2, 0.0],
            "input_layout": "samples_x_channels",
        }

        corrected, selected, _ = prepare_classifier_epoch(
            samples_channels,
            100.0,
            ["Oz", "Cz", "Fz"],
            {"relative_times": times.tolist(), "epoch_window_seconds": [-0.2, 0.8]},
            contract,
        )

        self.assertEqual(selected, ["Fz", "Cz"])
        self.assertEqual(corrected.shape, (2, 101))
        with self.assertRaisesRegex(ValueError, "sample rate"):
            prepare_classifier_epoch(samples_channels, 250.0, ["Oz", "Cz", "Fz"], {}, contract)

    def test_prepared_epoch_cache_reuses_compatible_contract_preparation(self) -> None:
        times = np.arange(101, dtype=float) / 100.0 - 0.2
        epoch = np.column_stack([np.ones(times.size), np.full(times.size, 2.0)])
        contract = {
            "channel_names": ["Fz", "Cz"],
            "sample_rate_hz": 100.0,
            "epoch_window_seconds": [-0.2, 0.8],
            "baseline_seconds": [-0.2, 0.0],
        }
        prepared = PreparedEpochCache(
            epoch,
            100.0,
            ["Fz", "Cz"],
            {"relative_times": times.tolist(), "epoch_window_seconds": [-0.2, 0.8]},
        )
        expected = (np.zeros((2, times.size)), ["Fz", "Cz"], times)

        with patch("eegle.realtime.models.prepare_classifier_epoch", return_value=expected) as prepare:
            first = prepared.classifier(contract, "samples_x_channels")
            second = prepared.classifier(dict(contract), "samples_x_channels")
            raw_first = prepared.raw_channels_samples("samples_x_channels")
            raw_second = prepared.raw_channels_samples("samples_x_channels")

        self.assertIs(first, second)
        self.assertIs(raw_first, raw_second)
        prepare.assert_called_once()

    def test_bare_artifacts_preserve_legacy_uncontracted_input(self) -> None:
        epoch = np.asarray([[3.0, 4.0], [5.0, 6.0]])
        prepared = prepare_artifact_epoch(
            epoch,
            100.0,
            ["Fz", "Cz"],
            {"relative_times": [-0.2, 0.0]},
            {},
            "samples_x_channels",
        )
        np.testing.assert_array_equal(prepared, epoch.T)

    def test_epoch_quality_rejects_nonfinite_flatline_and_extreme_epochs(self) -> None:
        good = np.random.default_rng(42).normal(0.0, 2.0, size=(100, 3))
        self.assertTrue(assess_epoch_quality(good).valid)
        bad = good.copy()
        bad[:, 0] = 0.0
        bad[0, 1] = np.nan
        bad[1, 2] = 1000.0
        quality = assess_epoch_quality(bad)
        self.assertFalse(quality.valid)
        self.assertIn("non_finite", quality.reasons)
        self.assertIn("flatline", quality.reasons)
        self.assertIn("max_abs_exceeded", quality.reasons)

    def test_model_bundle_round_trip_and_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "source.joblib"
            artifact.write_bytes(b"fixture-model")
            manifest = write_model_bundle(
                root / "bundle",
                kind="erp_roi_logreg",
                artifact_path=artifact,
                artifact_format="joblib",
                contract={"channel_names": ["Fz", "Cz"], "sample_rate_hz": 100.0},
                metrics={"balanced_accuracy": 0.7},
                training_source={"epochs_npz_sha256": "fixture"},
            )

            loaded = load_model_bundle(root / "bundle")

            self.assertEqual(loaded["bundle_hash"], manifest["bundle_hash"])
            self.assertEqual(loaded["label_mapping"], {"go": 0, "no_go": 1})
            (root / "bundle" / "source.joblib").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_model_bundle(root / "bundle")

    def test_online_model_dir_is_validated_before_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(ValueError, "--model-dir must be the parent directory"):
                _validate_online_model_bundles(root, ["erp_roi_logreg"])

    def test_demo_prediction_is_delayed_marker_truth_with_configured_errors(self) -> None:
        config = {
            "prediction_delay_seconds": 1.4,
            "error_rate": 0.0,
            "seed": 8,
            "marker_prefix": "go_nogo_stimulus_onset",
            "shapes": ["circle", "square"],
            "colors": ["blue", "green"],
            "no_go": {"shape": "x", "color": "white"},
        }
        correct = demo_prediction_from_marker(
            "go_nogo_stimulus_onset_4_go_circle_blue",
            12.5,
            config,
            random.Random(3),
        )
        wrong = demo_prediction_from_marker(
            "go_nogo_stimulus_onset_5_nogo_x_white",
            13.5,
            {**config, "error_rate": 1.0},
            random.Random(3),
        )

        self.assertEqual(correct["predicted_condition"], "go")
        self.assertTrue(correct["is_correct"])
        self.assertEqual(correct["guessed_stimulus"]["shape"], "circle")
        self.assertEqual(correct["prediction_delay_seconds"], 1.4)
        self.assertTrue(correct["erp_window"]["not_measured_from_eeg"])
        self.assertEqual(wrong["actual_condition"], "no_go")
        self.assertEqual(wrong["predicted_condition"], "go")
        self.assertFalse(wrong["is_correct"])

    def test_demo_dashboard_snapshot_and_ui_are_explicitly_simulated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "realtime").mkdir()
            (root / "logs" / "processes").mkdir(parents=True)
            config = {
                "realtime": {"dashboard": {"demo": {"enabled": True, "prediction_delay_seconds": 1.1, "error_rate": 0.1}}},
                "tasks": {"go_nogo": {"no_go": {"shape": "x", "color": "white"}}},
            }
            (root / "parameters.json").write_text(json.dumps(config), encoding="utf-8")
            row = demo_prediction_from_marker(
                "go_nogo_stimulus_onset_1_go_square_blue",
                10.0,
                demo_config_from(config),
                random.Random(4),
            )
            (root / "realtime" / "demo_predictions.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")

            snapshot = dashboard_snapshot(root, {"marker_status": "connected", "received_marker_count": 1})

            self.assertEqual(snapshot["mode"], "demo")
            self.assertEqual(snapshot["marker_status"], "connected")
            self.assertEqual(snapshot["prediction_count"], 1)
            self.assertEqual(snapshot["disclosure"], DEMO_DISCLOSURE)
            self.assertNotIn("Demo disclosure", DASHBOARD_HTML)
            self.assertIn("not measured from EEG", DASHBOARD_HTML)
            self.assertNotIn("Thinking", DASHBOARD_HTML)
            self.assertNotIn("animation:", DASHBOARD_HTML)
            self.assertIn('"Avenir Next"', DASHBOARD_HTML)
            self.assertIn("Each guess will remain here until the next one arrives.", DASHBOARD_HTML)

    def test_demo_bridge_writes_a_prediction_after_the_configured_delay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "runtime": {"session_root": tmp},
                "experiment": {"experiment_id": "test", "participant_id": "p1", "task": "go_nogo"},
                "realtime": {
                    "dashboard": {"demo": {"enabled": True, "prediction_delay_seconds": 0.0, "error_rate": 0.0}},
                    "epoching": {"marker_prefix": "go_nogo_stimulus_onset"},
                },
                "tasks": {"go_nogo": {"no_go": {"shape": "x", "color": "white"}}},
            }
            paths = create_session(config, root=Path(tmp))
            telemetry = Telemetry.from_config(config, paths, component="test.demo")
            bridge = DemoPredictionBridge(config, paths.realtime / "demo_predictions.jsonl", telemetry, threading.Event())

            bridge._schedule_marker("go_nogo_stimulus_onset_1_go_circle_blue", 5.0)
            bridge._emit_due_predictions()

            row = json.loads((paths.realtime / "demo_predictions.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(row["trial"], 1)
            self.assertEqual(row["source"], "psychopy_lsl_marker_demo")
            self.assertEqual(bridge.snapshot()["pending"], [])

    def test_demo_cli_defaults_to_marker_only_without_eeg_recording(self) -> None:
        args = build_parser().parse_args(["demo"])

        self.assertFalse(args.record_eeg)
        self.assertEqual(args.error_rate, 0.1)
        self.assertEqual(args.prediction_delay_seconds, 1.2)

    def test_primary_and_shadow_models_are_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "runtime": {"session_root": tmp},
                "experiment": {"experiment_id": "test", "participant_id": "p1", "task": "go_nogo"},
            }
            paths = create_session(config, task="go_nogo", root=tmp)
            models = _load_classifier_models(
                "erp_peak_baseline",
                {"input_layout": "samples_x_channels"},
                [{"id": "shadow-peak", "kind": "erp_peak_baseline", "input_layout": "samples_x_channels"}],
                paths,
            )

            self.assertEqual([(row["id"], row["role"]) for row in models], [("primary", "primary"), ("shadow-peak", "shadow")])

    def test_dashboard_snapshot_and_evaluation_join_canonical_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "realtime").mkdir()
            (root / "events").mkdir()
            (root / "logs" / "processes").mkdir(parents=True)
            manifest = {
                "trials": [
                    {"trial": 1, "stimulus": {"is_no_go": False}},
                    {"trial": 2, "stimulus": {"is_no_go": True}},
                    {"trial": 3, "stimulus": {"is_no_go": True}},
                ]
            }
            (root / "events" / "stimulus_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            rows = [
                _prediction(1, "go", 0.1),
                _prediction(2, "no_go", 0.9),
                {**_prediction(1, "go", 0.2), "model_id": "shadow-roi", "model_role": "shadow"},
                {**_prediction(2, "go", 0.4), "model_id": "shadow-roi", "model_role": "shadow"},
                {"schema": "eegle.model_prediction.v1", "status": "rejected", "trial": 3, "reason": "flatline"},
            ]
            (root / "realtime" / "model_predictions.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )

            live = dashboard_snapshot(root)
            evaluated = evaluate_classifier_session(root)

            self.assertEqual(live["rejected_epoch_count"], 1)
            self.assertEqual(live["metrics"]["primary"]["balanced_accuracy"], 1.0)
            self.assertEqual(live["primary_shadow_agreement"]["rate"], 0.5)
            self.assertEqual(evaluated["metrics"]["primary"]["no_go_recall"], 1.0)
            self.assertAlmostEqual(evaluated["metrics"]["primary"]["coverage"], 2.0 / 3.0)
            self.assertTrue((root / "reports" / "classification" / "predictions.csv").exists())

    def test_classifier_capture_replay_matches_online_predictions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "realtime").mkdir()
            (root / "reports" / "classification").mkdir(parents=True)
            times = np.arange(201, dtype=float) / 100.0
            data = np.random.default_rng(7).normal(0.0, 0.5, size=(times.size, 2))
            data[np.argmin(np.abs(times - 1.32)), 1] += 8.0
            header = {
                "mode": "classifier",
                "sample_rate_hz": 100.0,
                "channel_names": ["Fz", "Pz"],
                "epoching_config": {
                    "enabled": True,
                    "marker_prefix": "go_nogo_stimulus_onset",
                    "tmin_seconds": -0.2,
                    "tmax_seconds": 0.8,
                    "include_practice_trials": False,
                },
                "quality_gate": {"minimum_channel_std_uv": 0.0},
                "models": [
                    {
                        "id": "primary",
                        "role": "primary",
                        "kind": "erp_peak_baseline",
                        "config": {
                            "input_layout": "samples_x_channels",
                            "roi_channels": ["Pz"],
                            "amplitude_threshold_uv": 3.0,
                        },
                    }
                ],
            }
            writer = EngineInputCaptureWriter(root / "realtime" / "engine_input.bin", header)
            writer.write_eeg(times, data)
            writer.write_marker(MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 1.0))
            writer.write_marker(MarkerEvent("go_nogo_stimulus_onset_2_no_go_x_white", 1.9))
            writer.close()

            first = replay_classifier_session(root)
            replay_path = root / "reports" / "classification" / "replay_predictions.jsonl"
            (root / "realtime" / "model_predictions.jsonl").write_text(replay_path.read_text(encoding="utf-8"), encoding="utf-8")
            second = replay_classifier_session(root)

            self.assertEqual(first["status"], "analytically_invalid")
            self.assertEqual(second["status"], "pass")
            self.assertEqual(second["difference_count"], 0)
            self.assertEqual(second["online_prediction_count"], 2)

    def test_pending_eligible_markers_become_explicit_shutdown_rejections(self) -> None:
        epocher = RealtimeEpocher(
            EpochingConfig.from_dict(
                {
                    "enabled": True,
                    "marker_prefix": "go_nogo_stimulus_onset",
                    "include_practice_trials": False,
                }
            )
        )
        self.assertFalse(epocher.add_marker(MarkerEvent("go_nogo_stimulus_onset_-1_go_circle_red", 1.0)))
        self.assertTrue(epocher.add_marker(MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 2.0)))

        rejected = epocher.reject_pending("worker_stopped_before_epoch_completed")

        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].reason, "worker_stopped_before_epoch_completed")
        self.assertEqual(epocher.pending_count, 0)

    def test_missing_optional_training_dependencies_fail_cleanly(self) -> None:
        missing = {
            "erp_roi_logreg": "sklearn",
            "pyriemann_erp_cov": "pyriemann",
            "torch_eegnet": "torch",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            times = np.arange(101, dtype=float) / 100.0 - 0.2
            rng = np.random.default_rng(11)
            epochs = rng.normal(0.0, 1.0, size=(6, 2, times.size))
            labels = np.asarray([0, 1, 0, 1, 0, 1], dtype=int)
            np.savez(
                root / "epochs.npz",
                X=epochs,
                y=labels,
                trials=np.arange(1, 7),
                times=times,
                channel_names=np.asarray(["Fz", "Cz"], dtype=object),
                sample_rate_hz=np.asarray([100.0]),
            )
            checked = 0
            for kind, package in missing.items():
                if importlib.util.find_spec(package) is not None:
                    continue
                checked += 1
                with self.assertRaisesRegex(RuntimeError, "requires"):
                    train_epoch_model(kind, root / "epochs.npz", root / kind)
            self.assertGreaterEqual(checked, 0)

    def test_classify8_train_check_ready_does_not_fit_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = build_parser().parse_args(
                ["train", "--session-dir", tmp, "--kind", "torch_eegnet", "--check-ready"]
            )
            readiness = CheckResult(
                "training_ready",
                "fail",
                "missing training packages: torch",
                {"missing": ["torch"], "missing_by_kind": {"torch_eegnet": ["torch"]}},
            )
            with patch("eegle.pipelines.classify8.check_training_ready", return_value=readiness), patch(
                "eegle.pipelines.classify8.train_epoch_model"
            ) as train_model:
                result = classify8_train(args)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["training_ready"]["status"], "fail")
        train_model.assert_not_called()

    def test_classify8_train_skips_model_with_structured_missing_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = build_parser().parse_args(["train", "--session-dir", tmp, "--kind", "torch_eegnet"])
            readiness = CheckResult("training_ready", "fail", "missing training packages: torch")
            with patch("eegle.pipelines.classify8.check_training_ready", return_value=readiness), patch(
                "eegle.pipelines.classify8.missing_training_packages",
                return_value=["torch"],
            ), patch("eegle.pipelines.classify8.train_epoch_model") as train_model:
                result = classify8_train(args)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["models"]["torch_eegnet"]["reason"], "missing_training_dependencies")
        self.assertEqual(result["models"]["torch_eegnet"]["missing_packages"], ["torch"])
        train_model.assert_not_called()

    def test_training_excludes_practice_trials_before_model_fit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            times = np.arange(101, dtype=float) / 100.0 - 0.2
            np.savez(
                root / "epochs.npz",
                X=np.random.default_rng(4).normal(size=(2, 2, times.size)),
                y=np.asarray([0, 1]),
                trials=np.asarray([-2, -1]),
                times=times,
                channel_names=np.asarray(["Fz", "Cz"], dtype=object),
                sample_rate_hz=np.asarray([100.0]),
            )
            with self.assertRaisesRegex(ValueError, "eligible non-practice training labels"):
                train_epoch_model("erp_roi_logreg", root / "epochs.npz", root / "model")


def _prediction(trial: int, condition: str, probability: float) -> dict[str, object]:
    return {
        "schema": "eegle.model_prediction.v1",
        "status": "predicted",
        "trial": trial,
        "epoch_index": trial,
        "model_id": "primary",
        "model_role": "primary",
        "model_kind": "erp_roi_logreg",
        "predicted_condition": condition,
        "probability_no_go": probability,
        "processing_latency_ms": 2.0,
    }


if __name__ == "__main__":
    unittest.main()
