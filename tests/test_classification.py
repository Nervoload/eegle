from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from eegle.analysis.classification import replay_classifier_session
from eegle.models.bundles import load_model_bundle, write_model_bundle
from eegle.realtime.classification import (
    assess_epoch_quality,
    baseline_correct,
    extract_erp_roi_features,
    prepare_classifier_epoch,
    sanitize_model_metadata,
)
from eegle.realtime.epoching import EpochingConfig, MarkerEvent, RealtimeEpocher
from eegle.realtime.event_features import EngineInputCaptureWriter
from eegle.realtime.models import PreparedEpochCache, prepare_artifact_epoch, train_epoch_model


class ClassificationContractTests(unittest.TestCase):
    """Phase 6/8 source evidence independent of legacy recipes and workers."""

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

    def test_prepare_classifier_epoch_enforces_input_contract(self) -> None:
        times = np.arange(101, dtype=float) / 100.0 - 0.2
        epoch = np.column_stack(
            [np.ones(times.size), np.full(times.size, 3.0), np.full(times.size, 2.0)]
        )
        contract = {
            "channel_names": ["Fz", "Cz"],
            "sample_rate_hz": 100.0,
            "epoch_window_seconds": [-0.2, 0.8],
            "baseline_seconds": [-0.2, 0.0],
            "input_layout": "samples_x_channels",
        }

        corrected, selected, _ = prepare_classifier_epoch(
            epoch,
            100.0,
            ["Oz", "Cz", "Fz"],
            {"relative_times": times.tolist(), "epoch_window_seconds": [-0.2, 0.8]},
            contract,
        )

        self.assertEqual(selected, ["Fz", "Cz"])
        self.assertEqual(corrected.shape, (2, 101))
        with self.assertRaisesRegex(ValueError, "sample rate"):
            prepare_classifier_epoch(epoch, 250.0, ["Oz", "Cz", "Fz"], {}, contract)

    def test_prepared_epoch_cache_reuses_compatible_preparation(self) -> None:
        times = np.arange(101, dtype=float) / 100.0 - 0.2
        prepared = PreparedEpochCache(
            np.column_stack([np.ones(times.size), np.full(times.size, 2.0)]),
            100.0,
            ["Fz", "Cz"],
            {"relative_times": times.tolist(), "epoch_window_seconds": [-0.2, 0.8]},
        )
        contract = {
            "channel_names": ["Fz", "Cz"],
            "sample_rate_hz": 100.0,
            "epoch_window_seconds": [-0.2, 0.8],
            "baseline_seconds": [-0.2, 0.0],
        }
        expected = (np.zeros((2, times.size)), ["Fz", "Cz"], times)

        with patch("eegle.realtime.models.prepare_classifier_epoch", return_value=expected) as prepare:
            first = prepared.classifier(contract, "samples_x_channels")
            second = prepared.classifier(dict(contract), "samples_x_channels")

        self.assertIs(first, second)
        prepare.assert_called_once()

    def test_uncontracted_artifact_and_epoch_quality_behaviour_are_explicit(self) -> None:
        epoch = np.asarray([[3.0, 4.0], [5.0, 6.0]])
        artifact = prepare_artifact_epoch(
            epoch,
            100.0,
            ["Fz", "Cz"],
            {"relative_times": [-0.2, 0.0]},
            {},
            "samples_x_channels",
        )
        np.testing.assert_array_equal(artifact, epoch.T)

        samples = np.random.default_rng(42).normal(0.0, 2.0, size=(100, 3))
        self.assertTrue(assess_epoch_quality(samples).valid)
        samples[:, 0] = 0.0
        samples[0, 1] = np.nan
        samples[1, 2] = 1000.0
        quality = assess_epoch_quality(samples)
        self.assertFalse(quality.valid)
        self.assertTrue({"non_finite", "flatline", "max_abs_exceeded"}.issubset(quality.reasons))

    def test_model_bundle_round_trip_detects_tampering(self) -> None:
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
            (root / "bundle" / "source.joblib").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_model_bundle(root / "bundle")

    def test_capture_replay_accounts_for_all_predictions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "realtime").mkdir()
            times = np.arange(201, dtype=float) / 100.0
            data = np.random.default_rng(7).normal(0.0, 0.5, size=(times.size, 2))
            data[np.argmin(np.abs(times - 1.32)), 1] += 8.0
            writer = EngineInputCaptureWriter(
                root / "realtime" / "engine_input.bin",
                {
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
                            "config": {"input_layout": "samples_x_channels", "roi_channels": ["Pz"]},
                        }
                    ],
                },
            )
            writer.write_eeg(times, data)
            writer.write_marker(MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 1.0))
            writer.write_marker(MarkerEvent("go_nogo_stimulus_onset_2_no_go_x_white", 1.9))
            writer.close()

            first = replay_classifier_session(root)
            replay = root / "reports" / "classification" / "replay_predictions.jsonl"
            (root / "realtime" / "model_predictions.jsonl").write_text(
                replay.read_text(encoding="utf-8"), encoding="utf-8"
            )
            second = replay_classifier_session(root)

        self.assertEqual(first["status"], "analytically_invalid")
        self.assertEqual(second["status"], "pass")
        self.assertEqual(second["difference_count"], 0)
        self.assertEqual(second["online_prediction_count"], 2)

    def test_pending_eligible_markers_become_explicit_rejections(self) -> None:
        epocher = RealtimeEpocher(
            EpochingConfig.from_dict(
                {"enabled": True, "marker_prefix": "go_nogo_stimulus_onset", "include_practice_trials": False}
            )
        )
        self.assertFalse(epocher.add_marker(MarkerEvent("go_nogo_stimulus_onset_-1_go_circle_red", 1.0)))
        self.assertTrue(epocher.add_marker(MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 2.0)))

        rejected = epocher.reject_pending("execution_stopped_before_epoch_completed")

        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].reason, "execution_stopped_before_epoch_completed")
        self.assertEqual(epocher.pending_count, 0)

    def test_missing_optional_training_dependencies_fail_cleanly(self) -> None:
        missing = {"erp_roi_logreg": "sklearn", "pyriemann_erp_cov": "pyriemann", "torch_eegnet": "torch"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            times = np.arange(101, dtype=float) / 100.0 - 0.2
            np.savez(
                root / "epochs.npz",
                X=np.random.default_rng(11).normal(0.0, 1.0, size=(6, 2, times.size)),
                y=np.asarray([0, 1, 0, 1, 0, 1], dtype=int),
                trials=np.arange(1, 7),
                times=times,
                channel_names=np.asarray(["Fz", "Cz"], dtype=object),
                sample_rate_hz=np.asarray([100.0]),
            )
            for kind, package in missing.items():
                if importlib.util.find_spec(package) is None:
                    with self.assertRaisesRegex(RuntimeError, "requires"):
                        train_epoch_model(kind, root / "epochs.npz", root / kind)

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


if __name__ == "__main__":
    unittest.main()
