from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from eegle.analysis.alpha import run_alpha_validation
from eegle.analysis.html_summary import generate_experiment_html_report
from eegle.calibration.posterior_alpha import PosteriorAlphaCalibrationSuite
from eegle.pipelines.alpha8 import Alpha8PipelineOptions, run_full_pipeline
from eegle.realtime.alpha import (
    AlphaPowerEstimator,
    ArtifactGate,
    bounded_alpha_band,
    fallback_alpha_band,
    spectral_peak_candidates,
)
from eegle.realtime.models import ModelPrediction, make_model_adapter
from eegle.realtime.policy import ConservativeDecisionPolicy, TaskAction
from eegle.realtime.registry import make_model
from eegle.realtime.task_feedback import TaskFeedbackClient
from eegle.session import create_session
from eegle.tasks.go_nogo import GoNoGoTask


class ClosedLoopModelSystemTests(unittest.TestCase):
    def test_erp_peak_baseline_detects_synthetic_p300(self) -> None:
        sample_rate = 100.0
        times = np.arange(101, dtype=float) / sample_rate - 0.2
        epoch = np.zeros((times.size, 3), dtype=float)
        epoch[np.argmin(np.abs(times - 0.32)), 1] = 8.0
        model = make_model_adapter(
            "erp_peak_baseline",
            {
                "input_layout": "samples_x_channels",
                "roi_channels": ["Pz"],
                "amplitude_threshold_uv": 3.0,
                "amplitude_slope_uv": 1.0,
            },
        )

        prediction = model.predict_epoch(
            epoch,
            sample_rate,
            ["Fz", "Pz", "Oz"],
            {"relative_times": times.tolist(), "epoch_window_seconds": [-0.2, 0.8]},
        )

        self.assertEqual(prediction.label, "p300_present")
        self.assertGreater(prediction.probability or 0.0, 0.9)
        self.assertAlmostEqual(prediction.features["p300_latency_ms"], 320.0, delta=10.0)

    def test_registry_creates_lazy_adapters_and_errors_clearly(self) -> None:
        for kind in ("erp_peak_baseline", "sklearn_xdawn_lda", "pyriemann_erp_cov", "torch_eegnet", "torch_shallowconvnet", "onnx_p300"):
            adapter = make_model(kind, {"artifact_path": "/tmp/closedloop-missing-model"})
            self.assertEqual(adapter.kind, kind)

        adapter = make_model("onnx_p300", {"artifact_path": "/tmp/closedloop-missing-model"})
        with self.assertRaisesRegex(RuntimeError, "artifact does not exist"):
            adapter.predict_epoch(np.zeros((10, 2)), 100.0, ["Cz", "Pz"], {})

    def test_decision_policy_clamps_and_respects_cooldown(self) -> None:
        policy = ConservativeDecisionPolicy(
            {
                "actions": ["increase_no_go_probability", "observe_only"],
                "probability_threshold": 0.6,
                "cooldowns": {"trials": 2},
                "bounds": {"no_go_probability": [0.2, 0.4]},
                "no_go_probability_step": 0.5,
            }
        )
        prediction = ModelPrediction("p300_present", 8.0, probability=0.95, model_kind="erp_peak_baseline")

        first = policy.decide(prediction, {"trial": 1, "epoch_index": 1})[0]
        second = policy.decide(prediction, {"trial": 2, "epoch_index": 2})[0]

        self.assertEqual(first.action, "increase_no_go_probability")
        self.assertEqual(first.parameters["max"], 0.4)
        self.assertLessEqual(first.value, 0.2)
        self.assertEqual(second.action, "observe_only")

    def test_task_feedback_client_matches_boundaries_and_trials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feedback.jsonl"
            action = TaskAction(
                action="increase_no_go_probability",
                boundary="between_trials",
                reason="test",
                action_id="a1",
                parameters={"delta": 0.1, "min": 0.2, "max": 0.5},
                target_trial_index=3,
            )
            path.write_text("not json\n" + json.dumps({"actions": [action.to_payload()]}) + "\n", encoding="utf-8")
            client = TaskFeedbackClient({"enabled": True, "backend": "jsonl", "jsonl_path": str(path)})

            self.assertEqual(client.poll("between_trials", 2), [])
            accepted = client.poll("between_trials", 3)

            self.assertEqual(len(accepted), 1)
            self.assertEqual(accepted[0].action_id, "a1")
            self.assertTrue(any(record.status == "rejected" for record in client.audit_records) or accepted)

    def test_dry_run_go_nogo_applies_jsonl_feedback_at_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "runtime": {"session_root": tmp},
                "experiment": {"experiment_id": "test", "participant_id": "p1", "task": "go_nogo"},
                "telemetry": {"console_level": "disabled", "file_level": "disabled"},
                "tasks": {
                    "go_nogo": {
                        "trials": 2,
                        "no_go_probability": 0.3,
                        "stimulus_seconds": 0.01,
                        "isi_seconds": 0.01,
                        "response_keys": ["space"],
                        "escape_keys": ["escape"],
                        "no_go": {"shape": "x", "color": "white", "randomize": False},
                    }
                },
                "realtime": {
                    "decision_policy": {"bounds": {"no_go_probability": [0.1, 0.5], "isi_seconds": [0.0, 1.0], "visual_alpha": [0.5, 1.0]}},
                    "feedback": {"client": {"enabled": True, "backend": "jsonl"}},
                },
            }
            paths = create_session(config, task="go_nogo", root=tmp)
            action = TaskAction(
                action="increase_no_go_probability",
                boundary="between_trials",
                reason="fixture",
                action_id="fixture-1",
                parameters={"delta": 0.1, "min": 0.1, "max": 0.5},
                target_trial_index=1,
            )
            paths.realtime_feedback_jsonl.write_text(json.dumps({"actions": [action.to_payload()]}) + "\n", encoding="utf-8")

            result = GoNoGoTask(config, mode="dry-run").run(paths)
            manifest = json.loads((paths.events / "stimulus_manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(result.summary["trials"], 2)
            self.assertEqual(manifest["trials"][0]["applied_actions"][0]["action"], "increase_no_go_probability")
            self.assertAlmostEqual(manifest["trials"][0]["adaptive_state"]["no_go_probability"], 0.4)

    def test_alpha_band_is_bounded_from_specparam_peak(self) -> None:
        band = bounded_alpha_band(10.4, 5.0)

        self.assertAlmostEqual(band.low_hz, 7.9)
        self.assertAlmostEqual(band.high_hz, 12.9)
        self.assertEqual(band.confidence, "accepted")

        wide = bounded_alpha_band(12.0, 20.0)
        self.assertLessEqual(wide.width_hz, 6.0)
        fallback = fallback_alpha_band("fixture")
        self.assertEqual((fallback.low_hz, fallback.high_hz), (8.0, 12.0))
        self.assertEqual(fallback.confidence, "low_confidence_fallback")

    def test_spectral_peak_candidate_parsing_filters_alpha_range(self) -> None:
        model = {
            "peak_params": [
                [5.5, 0.5, 2.0],
                [10.2, 0.8, 3.0],
                [11.0, 0.4, 2.5],
                [18.0, 0.9, 4.0],
            ],
            "r_squared": 0.93,
        }

        candidates = spectral_peak_candidates(model)

        self.assertEqual(len(candidates), 2)
        self.assertAlmostEqual(candidates[0].center_hz, 10.2)
        self.assertAlmostEqual(candidates[0].fit_r_squared or 0.0, 0.93)

    def test_alpha_artifact_gate_rejects_high_amplitude_chunks(self) -> None:
        gate = ArtifactGate({"max_abs_uv": 100.0, "max_peak_to_peak_uv": 150.0})

        ok = gate.check(np.zeros((10, 2)))
        rejected = gate.check(np.asarray([[0.0, 200.0], [0.0, -10.0]]))

        self.assertTrue(ok["ok"])
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["reason"], "max_abs_exceeded")

    def test_alpha_power_estimator_emits_payload_with_latency_metadata(self) -> None:
        sample_rate = 250.0
        times = np.arange(0, 2.0, 1.0 / sample_rate)
        signal = 5.0 * np.sin(2.0 * np.pi * 10.0 * times)
        data = np.column_stack([signal, signal, signal])
        estimator = AlphaPowerEstimator(
            sample_rate,
            ["P3", "P4", "Oz"],
            {
                "band": {"low_hz": 8.0, "high_hz": 12.0},
                "posterior_channels": ["P3", "P4", "Oz"],
                "smoothing_seconds": 0.2,
            },
        )

        estimator.process_chunk(times, data)
        payload = estimator.snapshot()

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertGreater(payload["alpha_power"], 0.0)
        self.assertEqual(payload["method"], "causal_bandpass_hilbert_envelope")
        self.assertGreater(payload["latency_estimate_ms"], 0.0)
        self.assertEqual(payload["band"]["low_hz"], 8.0)

    def test_alpha_power_estimator_averages_power_not_channel_waveforms(self) -> None:
        sample_rate = 250.0
        times = np.arange(0, 2.0, 1.0 / sample_rate)
        signal = 5.0 * np.sin(2.0 * np.pi * 10.0 * times)
        data = np.column_stack([signal, -signal])
        estimator = AlphaPowerEstimator(
            sample_rate,
            ["P3", "P4"],
            {
                "band": {"low_hz": 8.0, "high_hz": 12.0},
                "posterior_channels": ["P3", "P4"],
                "smoothing_seconds": 0.2,
            },
        )

        estimator.process_chunk(times, data)
        payload = estimator.snapshot()

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertGreater(payload["alpha_power"], 1.0)
        self.assertEqual(set(payload["channel_alpha_power"]), {"P3", "P4"})

    def test_posterior_alpha_calibration_dry_run_writes_fallback_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "runtime": {"session_root": tmp},
                "experiment": {"experiment_id": "test", "participant_id": "p1", "task": "go_nogo"},
                "telemetry": {"console_level": "disabled", "file_level": "disabled"},
                "hardware": {
                    "eeg": {"profile": "enobio8_alpha_posterior", "expected_sample_rate_hz": 100},
                    "markers": {"lsl_stream_name": "EEGleMarkers", "lsl_stream_type": "Markers"},
                },
                "tasks": {
                    "go_nogo": {
                        "trials": 2,
                        "no_go_probability": 0.3,
                        "stimulus_seconds": 0.01,
                        "isi_seconds": 0.01,
                        "response_keys": ["space"],
                        "escape_keys": ["escape"],
                        "no_go": {"shape": "x", "color": "white", "randomize": False},
                    }
                },
                "calibration": {
                    "posterior_alpha": {
                        "eyes_open_seconds": 2.0,
                        "eyes_closed_seconds": 2.0,
                        "go_nogo_practice_seconds": 2.0,
                        "go_nogo_practice_trials": 2,
                        "synthetic_phase_seconds": 2.0,
                        "welch_window_seconds": 1.0,
                        "write_plot": False,
                        "posterior_channels": ["P3", "P4", "PO3", "PO4", "Pz", "O1", "O2", "Oz"],
                    }
                },
            }
            paths = create_session(config, task="go_nogo", root=tmp)
            suite = PosteriorAlphaCalibrationSuite(config, paths, mode="dry-run", record_eeg=True)

            result = suite.run()
            updated = suite.apply_result_to_config(result)

            self.assertTrue(paths.calibration_eeg_csv.exists())
            self.assertTrue(paths.calibration_result.exists())
            self.assertEqual(result["status"], "low_confidence_fallback")
            self.assertTrue(updated["realtime"]["alpha"]["enabled"])
            self.assertEqual(updated["realtime"]["decision_policy"]["kind"], "observe_only")

    def test_alpha_validation_joins_estimates_to_go_nogo_trials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "events").mkdir()
            (root / "raw").mkdir()
            (root / "realtime").mkdir()
            manifest = {
                "task": "go_nogo",
                "trials": [
                    {
                        "trial": 1,
                        "stimulus": {"is_no_go": False},
                        "onset_lsl_timestamp": 10.0,
                        "offset_lsl_timestamp": 10.8,
                        "response": {"reaction_time_seconds": 0.3, "correct_press": 1, "button_press_count": 1},
                    },
                    {
                        "trial": 2,
                        "stimulus": {"is_no_go": True},
                        "onset_lsl_timestamp": 12.0,
                        "offset_lsl_timestamp": 12.8,
                        "response": {"reaction_time_seconds": None, "correct_press": 1, "button_press_count": 0},
                    },
                ],
            }
            (root / "events" / "stimulus_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with (root / "raw" / "eeg.csv").open("w", encoding="utf-8") as handle:
                handle.write("lsl_timestamp,local_received_time,P3,P4,Oz\n")
                for index in range(400):
                    timestamp = 9.0 + index / 100.0
                    signal = 4.0 * np.sin(2.0 * np.pi * 10.0 * index / 100.0)
                    handle.write(f"{timestamp:.6f},{timestamp:.6f},{signal:.6f},{signal:.6f},{signal:.6f}\n")
            estimates = [
                {"window_start_lsl_timestamp": 9.4, "window_end_lsl_timestamp": 9.8, "alpha_power_z": 0.2, "alpha_power": 5.0},
                {"window_start_lsl_timestamp": 10.1, "window_end_lsl_timestamp": 10.4, "alpha_power_z": 0.4, "alpha_power": 6.0},
                {"window_start_lsl_timestamp": 11.4, "window_end_lsl_timestamp": 11.8, "alpha_power_z": -0.1, "alpha_power": 4.0},
            ]
            with (root / "realtime" / "alpha_power.jsonl").open("w", encoding="utf-8") as handle:
                for row in estimates:
                    handle.write(json.dumps(row) + "\n")

            summary = run_alpha_validation(root, {})

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["trial_count"], 2)
            self.assertTrue((root / "reports" / "alpha" / "trial_alpha.csv").exists())
            self.assertEqual(summary["offline_alpha"]["status"], "ok")
            self.assertTrue((root / "reports" / "alpha" / "offline_alpha_timeseries.csv").exists())

    def test_alpha_validation_uses_monotonic_when_lsl_clocks_do_not_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "events").mkdir()
            (root / "raw").mkdir()
            (root / "realtime").mkdir()
            manifest = {
                "task": "go_nogo",
                "trials": [
                    {
                        "trial": 1,
                        "stimulus": {"is_no_go": False},
                        "onset_lsl_timestamp": 10000.0,
                        "offset_lsl_timestamp": 10000.8,
                        "onset_monotonic": 20.0,
                        "offset_monotonic": 20.8,
                        "response": {"reaction_time_seconds": 0.3, "correct_press": 1, "button_press_count": 1},
                    }
                ],
            }
            (root / "events" / "stimulus_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with (root / "raw" / "eeg.csv").open("w", encoding="utf-8") as handle:
                handle.write("lsl_timestamp,local_received_time,P3,P4,Oz\n")
                for index in range(400):
                    lsl_timestamp = 10.0 + index / 100.0
                    monotonic_timestamp = 19.0 + index / 100.0
                    signal = 4.0 * np.sin(2.0 * np.pi * 10.0 * index / 100.0)
                    handle.write(f"{lsl_timestamp:.6f},{monotonic_timestamp:.6f},{signal:.6f},{signal:.6f},{signal:.6f}\n")
            with (root / "realtime" / "alpha_power.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "window_start_lsl_timestamp": 10.4,
                            "window_end_lsl_timestamp": 10.8,
                            "created_at_monotonic": 19.8,
                            "alpha_power": 5.0,
                        }
                    )
                    + "\n"
            )

            summary = run_alpha_validation(root, {})
            with (root / "reports" / "alpha" / "trial_alpha.csv").open() as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(summary["timebase"], "monotonic")
            self.assertEqual(rows[0]["pre_alpha"], "5.0")

    def test_alpha_validation_rejects_numerical_noise_live_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "events").mkdir()
            (root / "raw").mkdir()
            (root / "realtime").mkdir()
            manifest = {
                "task": "go_nogo",
                "trials": [
                    {
                        "trial": 1,
                        "stimulus": {"is_no_go": False},
                        "onset_lsl_timestamp": 10.0,
                        "offset_lsl_timestamp": 10.8,
                        "response": {"reaction_time_seconds": 0.3, "correct_press": 1, "button_press_count": 1},
                    }
                ],
            }
            (root / "events" / "stimulus_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with (root / "raw" / "eeg.csv").open("w", encoding="utf-8") as handle:
                handle.write("lsl_timestamp,local_received_time,P3,P4,Oz\n")
                for index in range(400):
                    timestamp = 9.0 + index / 100.0
                    signal = 4.0 * np.sin(2.0 * np.pi * 10.0 * index / 100.0)
                    handle.write(f"{timestamp:.6f},{timestamp:.6f},{signal:.6f},{signal:.6f},{signal:.6f}\n")
            with (root / "realtime" / "alpha_power.jsonl").open("w", encoding="utf-8") as handle:
                for index in range(20):
                    handle.write(
                        json.dumps(
                            {
                                "window_start_lsl_timestamp": 9.0 + index * 0.1,
                                "window_end_lsl_timestamp": 9.2 + index * 0.1,
                                "alpha_power": 1e-23 * (index + 1),
                                "alpha_power_z": None,
                                "valid": True,
                            }
                        )
                        + "\n"
                    )

            summary = run_alpha_validation(root, {})
            with (root / "reports" / "alpha" / "trial_alpha.csv").open() as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(summary["status"], "invalid_live_alpha")
            self.assertFalse(summary["live_alpha_quality"]["usable"])
            self.assertIn("alpha_power_at_numerical_noise_scale", summary["live_alpha_quality"]["reasons"])
            self.assertEqual(summary["behavior_prediction"]["status"], "not_run_invalid_live_alpha")
            self.assertEqual(rows[0]["pre_alpha"], "")

    def test_html_report_generation_embeds_replay_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "events").mkdir()
            (root / "raw").mkdir()
            (root / "realtime").mkdir()
            (root / "calibration").mkdir()
            manifest = {
                "task": "go_nogo",
                "trials": [
                    {
                        "trial": 1,
                        "stimulus": {"is_no_go": False},
                        "onset_lsl_timestamp": 10.0,
                        "offset_lsl_timestamp": 10.8,
                        "response": {
                            "reaction_time_seconds": 0.25,
                            "correct_press": 1,
                            "button_press_count": 1,
                            "presses": [{"key": "space", "rt_seconds": 0.25}],
                        },
                    },
                    {
                        "trial": 2,
                        "stimulus": {"is_no_go": True},
                        "onset_lsl_timestamp": 12.0,
                        "offset_lsl_timestamp": 12.8,
                        "response": {"presses": []},
                    },
                ],
            }
            (root / "events" / "stimulus_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with (root / "events" / "events.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({"label": "task_start", "timestamp": 10.0, "metadata": {"lsl_timestamp": 10.0}}) + "\n")
            with (root / "calibration" / "events.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "event": "recording_start",
                            "phase": "eyes_open_fixation",
                            "lsl_timestamp": 9.1,
                            "timestamp_monotonic": 9.1,
                        }
                    )
                    + "\n"
                )
                handle.write(
                    json.dumps(
                        {
                            "event": "recording_end",
                            "phase": "eyes_open_fixation",
                            "lsl_timestamp": 9.7,
                            "timestamp_monotonic": 9.7,
                        }
                    )
                    + "\n"
                )
                handle.write(
                    json.dumps(
                        {
                            "event": "recording_start",
                            "phase": "eyes_closed_rest",
                            "lsl_timestamp": 11.0,
                            "timestamp_monotonic": 11.0,
                        }
                    )
                    + "\n"
                )
                handle.write(
                    json.dumps(
                        {
                            "event": "recording_end",
                            "phase": "eyes_closed_rest",
                            "lsl_timestamp": 11.7,
                            "timestamp_monotonic": 11.7,
                        }
                    )
                    + "\n"
                )
            with (root / "raw" / "eeg.csv").open("w", encoding="utf-8") as handle:
                handle.write("lsl_timestamp,local_received_time,P3,P4,Oz\n")
                for index in range(500):
                    timestamp = 9.0 + index / 100.0
                    signal = 3.0 * np.sin(2.0 * np.pi * 10.0 * index / 100.0)
                    handle.write(f"{timestamp:.6f},{timestamp:.6f},{signal:.6f},{signal:.6f},{signal:.6f}\n")
            (root / "calibration" / "alpha_calibration.json").write_text(
                json.dumps(
                    {
                        "status": "accepted",
                        "online_band": {"low_hz": 8.0, "high_hz": 12.0, "center_hz": 10.0, "bandwidth_hz": 4.0},
                        "posterior_channels": ["P3", "P4", "Oz"],
                    }
                ),
                encoding="utf-8",
            )
            with (root / "realtime" / "alpha_power.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({"window_start_lsl_timestamp": 9.4, "window_end_lsl_timestamp": 9.8, "alpha_power": 5.0, "valid": True}) + "\n")

            summary = generate_experiment_html_report(root, {})
            html_path = Path(summary["html_file"])
            html = html_path.read_text(encoding="utf-8")

            self.assertEqual(summary["status"], "ok")
            self.assertTrue(html_path.exists())
            self.assertIn("replayCanvas", html)
            self.assertIn("featureCanvas", html)
            self.assertIn("Feature Analysis", html)
            self.assertIn("showFeatureMarkers", html)
            self.assertNotIn('id="alphaCanvas"', html)
            self.assertIn("rawStackPanel", html)
            self.assertIn("visibleChannelValues", html)
            self.assertIn("centerUv", html)
            self.assertIn("markerLegend", html)
            self.assertNotIn("showAlphaGraphMarkers", html)
            self.assertIn("Number(band.low_hz).toFixed(4)", html)
            self.assertIn("Full Task", html)
            payload = html.split('<script id="report-data" type="application/json">', 1)[1].split("</script>", 1)[0]
            data = json.loads(payload)
            self.assertEqual([segment["key"] for segment in data["segments"]], ["full", "eyes_open_fixation", "eyes_closed_rest", "task"])
            self.assertIn("no_go", data["marker_categories"])
            self.assertTrue(any(marker["category"] == "no_go" and marker["trial"] == 2 for marker in data["markers"]))
            task_segment = next(segment for segment in data["segments"] if segment["key"] == "task")
            self.assertLess(task_segment["end"] - task_segment["start"], data["raw"]["duration_seconds"])
            self.assertTrue((root / "reports" / "alpha" / "offline_alpha_timeseries.csv").exists())

    def test_html_report_uses_monotonic_display_timebase_when_lsl_markers_do_not_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "events").mkdir()
            (root / "raw").mkdir()
            (root / "realtime").mkdir()
            (root / "calibration").mkdir()
            manifest = {
                "task": "go_nogo",
                "trials": [
                    {
                        "trial": 1,
                        "stimulus": {"is_no_go": False},
                        "onset_lsl_timestamp": 10000.0,
                        "offset_lsl_timestamp": 10000.8,
                        "onset_monotonic": 20.0,
                        "offset_monotonic": 20.8,
                        "response": {"presses": [{"key": "space", "rt_seconds": 0.2}]},
                    }
                ],
            }
            (root / "events" / "stimulus_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with (root / "events" / "events.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({"label": "task_start", "timestamp": 19.0, "metadata": {"lsl_timestamp": 9999.0}}) + "\n")
            with (root / "raw" / "eeg.csv").open("w", encoding="utf-8") as handle:
                handle.write("lsl_timestamp,local_received_time,P3,P4,Oz\n")
                for index in range(500):
                    lsl_timestamp = 10.0 + index / 100.0
                    monotonic_timestamp = 19.0 + index / 100.0
                    signal = 3.0 * np.sin(2.0 * np.pi * 10.0 * index / 100.0)
                    handle.write(f"{lsl_timestamp:.6f},{monotonic_timestamp:.6f},{signal:.6f},{signal:.6f},{signal:.6f}\n")
            (root / "calibration" / "alpha_calibration.json").write_text(
                json.dumps({"status": "accepted", "online_band": {"low_hz": 8.0, "high_hz": 12.0}}),
                encoding="utf-8",
            )
            with (root / "realtime" / "alpha_power.jsonl").open("w", encoding="utf-8") as handle:
                handle.write(json.dumps({"created_at_monotonic": 20.2, "alpha_power": 5.0, "valid": True}) + "\n")

            summary = generate_experiment_html_report(root, {})
            html = Path(summary["html_file"]).read_text(encoding="utf-8")
            payload = html.split('<script id="report-data" type="application/json">', 1)[1].split("</script>", 1)[0]
            data = json.loads(payload)

            self.assertEqual(summary["display_timebase"], "monotonic")
            self.assertEqual(data["display_timebase"], "monotonic")
            self.assertLess(max(marker["time"] for marker in data["markers"]), data["raw"]["duration_seconds"] + 1.0)

    def test_alpha8_full_pipeline_dry_run_runs_in_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "runtime": {"session_root": str(root / "data"), "runtime_cache_dir": str(root / ".runtime")},
                "experiment": {
                    "experiment_id": "alpha8_test",
                    "participant_id": "p1",
                    "task": "go_nogo",
                    "components": {
                        "task": "go_nogo",
                        "eeg_recorder": "disabled",
                        "realtime_processor": "disabled",
                        "feedback": "disabled",
                        "analysis": "minimal",
                    },
                },
                "telemetry": {"console_level": "disabled", "file_level": "disabled"},
                "hardware": {
                    "eeg": {
                        "profile": "enobio8_alpha_posterior",
                        "expected_channel_counts": [8],
                        "expected_sample_rate_hz": 100,
                        "lsl_stream_type": "EEG",
                        "lsl_name_patterns": ["enobio", "nic"],
                        "stream_timeout_seconds": 0.1,
                        "required_for_run": False,
                    },
                    "markers": {"lsl_stream_name": "EEGleMarkers", "lsl_stream_type": "Markers"},
                },
                "processes": {
                    "recorder": {"enabled": False, "backend": "disabled"},
                    "realtime_processor": {"enabled": False, "backend": "disabled"},
                    "feedback": {"enabled": False, "backend": "disabled"},
                    "offline_analyzer": {"enabled": True, "backend": "minimal", "timeout_seconds": 30.0},
                },
                "tasks": {
                    "go_nogo": {
                        "trials": 2,
                        "no_go_probability": 0.3,
                        "stimulus_seconds": 0.01,
                        "isi_seconds": 0.01,
                        "response_keys": ["space"],
                        "escape_keys": ["escape"],
                        "no_go": {"shape": "x", "color": "white", "randomize": False},
                    }
                },
                "calibration": {
                    "posterior_alpha": {
                        "eyes_open_seconds": 2.0,
                        "eyes_closed_seconds": 2.0,
                        "go_nogo_practice_seconds": 2.0,
                        "go_nogo_practice_trials": 2,
                        "synthetic_phase_seconds": 2.0,
                        "welch_window_seconds": 1.0,
                        "write_plot": False,
                        "posterior_channels": ["P3", "P4", "PO3", "PO4", "Pz", "O1", "O2", "Oz"],
                    }
                },
                "realtime": {
                    "enabled": False,
                    "epoching": {"enabled": True, "marker_prefix": "go_nogo_stimulus_onset"},
                    "alpha": {"enabled": False},
                    "decision_policy": {},
                    "feedback": {"client": {"enabled": False}},
                },
                "analysis": {
                    "erp": {"enabled": True},
                    "alpha": {"enabled": True},
                    "html": {"enabled": True, "max_raw_points": 1000, "max_alpha_points": 1000},
                },
            }
            config_path = root / "alpha8_config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")

            summary = run_full_pipeline(
                Alpha8PipelineOptions(
                    config_path=config_path,
                    task_mode="dry-run",
                    trials=2,
                    record_eeg=False,
                    require_eeg=False,
                    lsl_wait_seconds=0.1,
                    quiet=True,
                    max_raw_points=1000,
                    max_alpha_points=1000,
                )
            )

            self.assertEqual(summary["status"], "complete")
            self.assertEqual([step["step"] for step in summary["steps"]], ["preflight", "calibration_and_experiment", "analysis"])
            session = Path(summary["session_dir"])
            self.assertTrue((session / "reports" / "alpha8_full_summary.json").exists())
            self.assertTrue((session / "reports" / "summary.json").exists())
            self.assertTrue((session / "reports" / "experiment_summary.html").exists())
            self.assertEqual(summary["task"]["summary"]["trials"], 2)


if __name__ == "__main__":
    unittest.main()
