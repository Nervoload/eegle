from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from reproduce.analysis.inhibition8 import replay_realtime_session, run_feature_behavior_analysis
from reproduce.analysis.alpha import run_alpha_validation
from reproduce.analysis.html_summary import _build_segments
from reproduce.analysis.reports import _behavior_summary
from reproduce.hardware.enobio import expected_profile, mapped_channel_names
from reproduce.config import load_config
from reproduce.pipelines.inhibition8 import _config_contract_issues
from reproduce.realtime.epoching import MarkerEvent
from reproduce.realtime.event_features import (
    ERP_BASELINE_WINDOW,
    FEATURE_DEFINITIONS,
    EngineInputCaptureWriter,
    FeatureRegistry,
    RealtimeEventEngine,
)


CHANNELS = ["Fz", "Cz", "Pz", "C3", "C4", "P3", "P4", "Oz"]


class Inhibition8Tests(unittest.TestCase):
    def test_shipped_config_enforces_observe_only_contract(self) -> None:
        config = load_config("configs/forward_go_nogo_inhibition8.json")

        self.assertEqual(_config_contract_issues(config), [])

    def test_montage_is_exact_and_does_not_claim_missing_midline_channels(self) -> None:
        profile = expected_profile("enobio8_inhibition")
        mapped, source = mapped_channel_names([f"ch_{index:03d}" for index in range(1, 9)], {"profile": profile.name})

        self.assertEqual(list(profile.channel_names), CHANNELS)
        self.assertEqual(mapped, CHANNELS)
        self.assertEqual(source, "profile:enobio8_inhibition")
        self.assertNotIn("FCz", mapped)
        self.assertNotIn("CPz", mapped)

    def test_feature_registry_declares_windows_and_degrades_without_silent_substitution(self) -> None:
        registry = FeatureRegistry(["Fz", "Cz", "Pz"])

        self.assertEqual(FEATURE_DEFINITIONS["readiness_alpha"].window, (-1.0, -0.2))
        self.assertEqual(ERP_BASELINE_WINDOW, (-0.2, 0.0))
        self.assertEqual(FEATURE_DEFINITIONS["n2"].window, (0.2, 0.35))
        self.assertEqual(FEATURE_DEFINITIONS["p3"].window, (0.3, 0.6))
        self.assertEqual(FEATURE_DEFINITIONS["alpha_erd"].window, (0.2, 0.8))
        self.assertEqual(registry.resolution["n2"], ["Fz", "Cz"])
        self.assertEqual(registry.resolution["p3"], ["Pz", "Cz"])
        self.assertNotIn("FCz", registry.resolution["n2"])
        self.assertNotIn("CPz", registry.resolution["p3"])
        self.assertTrue(registry.valid["n2"])
        self.assertTrue(registry.valid["p3"])
        self.assertFalse(registry.valid["readiness_alpha"])

    def test_event_engine_emits_staged_observe_only_packets_with_delay_metadata(self) -> None:
        sample_rate = 100.0
        timestamps, data = _synthetic_eeg(sample_rate)
        engine = RealtimeEventEngine(_engine_config(), sample_rate, CHANNELS)

        engine.process_chunk(timestamps[:250], data[:250])
        self.assertEqual(engine.add_marker(MarkerEvent("go_nogo_stimulus_onset_1_no_go_x_white", 2.5)), [])
        packets = engine.process_chunk(timestamps[250:], data[250:])

        self.assertEqual([packet["stage"] for packet in packets], ["prestim_state", "n2_theta", "p3", "alpha_erd"])
        for packet in packets:
            self.assertFalse(packet["decision_eligibility"]["can_adapt"])
            self.assertTrue(packet["decision_eligibility"]["observe_only"])
            self.assertIn("erp_0p5_30", packet["filter_effective_delay_ms"])
            self.assertIn("coefficient_sha256", packet["filter_profiles"]["erp_0p5_30"])
            self.assertEqual(packet["erp_baseline_window_seconds"], [-0.2, 0.0])
            self.assertFalse(packet["disabled_features"]["alpha_rebound"]["enabled"])
            self.assertEqual(packet["roi_resolution"]["n2_theta"], ["Fz", "Cz"])
            self.assertNotIn("FCz", json.dumps(packet))
            self.assertNotIn("CPz", json.dumps(packet))
        self.assertIn("readiness_alpha_power", packets[-1]["features"])
        self.assertIn("n2_mean_uv", packets[-1]["features"])
        self.assertIn("p3_mean_uv", packets[-1]["features"])
        self.assertIn("alpha_erd_percent", packets[-1]["features"])

    def test_scheduler_emits_each_stage_once_only_after_its_eeg_horizon(self) -> None:
        sample_rate = 100.0
        timestamps, data = _synthetic_eeg(sample_rate)
        engine = RealtimeEventEngine(_engine_config(), sample_rate, CHANNELS)
        self.assertEqual(engine.process_chunk(timestamps[:250], data[:250]), [])
        self.assertEqual(engine.add_marker(MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 2.5)), [])

        first = engine.process_chunk(timestamps[250:286], data[250:286])
        second = engine.process_chunk(timestamps[286:311], data[286:311])
        third = engine.process_chunk(timestamps[311:], data[311:])
        repeated = engine.emit_ready()

        self.assertEqual([packet["stage"] for packet in first], ["prestim_state", "n2_theta"])
        self.assertEqual([packet["stage"] for packet in second], ["p3"])
        self.assertEqual([packet["stage"] for packet in third], ["alpha_erd"])
        self.assertEqual(repeated, [])

    def test_event_engine_excludes_practice_trials_by_default(self) -> None:
        sample_rate = 100.0
        timestamps, data = _synthetic_eeg(sample_rate)
        engine = RealtimeEventEngine(_engine_config(), sample_rate, CHANNELS)
        engine.process_chunk(timestamps[:250], data[:250])

        packets = engine.add_marker(MarkerEvent("go_nogo_stimulus_onset_-1_go_circle_red", 2.5))
        packets.extend(engine.process_chunk(timestamps[250:], data[250:]))

        self.assertEqual(packets, [])

    def test_reference_contamination_invalidates_features_without_changing_reference(self) -> None:
        sample_rate = 100.0
        timestamps, data = _synthetic_eeg(sample_rate)
        data[(timestamps >= 2.6) & (timestamps <= 2.8), 0] = 5000.0
        engine = RealtimeEventEngine(_engine_config(), sample_rate, CHANNELS)
        engine.process_chunk(timestamps[:250], data[:250])
        engine.add_marker(MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 2.5))
        packets = engine.process_chunk(timestamps[250:], data[250:])
        n2 = next(packet for packet in packets if packet["stage"] == "n2_theta")

        self.assertFalse(n2["reference_valid"])
        self.assertFalse(n2["valid"])
        self.assertIn("Fz", n2["invalid_reference_channels"])
        self.assertEqual(n2["fixed_reference_channels"], CHANNELS)

    def test_explicit_empty_calibration_reference_is_not_silently_replaced(self) -> None:
        sample_rate = 100.0
        timestamps, data = _synthetic_eeg(sample_rate)
        config = _engine_config()
        config["fixed_reference_channels"] = []
        engine = RealtimeEventEngine(config, sample_rate, CHANNELS)
        engine.process_chunk(timestamps[:250], data[:250])
        engine.add_marker(MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 2.5))
        packets = engine.process_chunk(timestamps[250:], data[250:])

        self.assertTrue(packets)
        self.assertFalse(packets[0]["reference_valid"])
        self.assertEqual(packets[0]["fixed_reference_channels"], [])

    def test_filter_warmup_covers_the_beginning_of_each_feature_window(self) -> None:
        sample_rate = 100.0
        timestamps, data = _synthetic_eeg(sample_rate)
        config = _engine_config()
        config["filter_warmup_seconds"] = 2.0
        engine = RealtimeEventEngine(config, sample_rate, CHANNELS)
        engine.process_chunk(timestamps[:250], data[:250])
        engine.add_marker(MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 2.5))
        packets = engine.process_chunk(timestamps[250:], data[250:])

        readiness = next(packet for packet in packets if packet["stage"] == "prestim_state")
        n2 = next(packet for packet in packets if packet["stage"] == "n2_theta")
        self.assertFalse(readiness["filter_warmup_valid"])
        self.assertTrue(n2["filter_warmup_valid"])

    def test_filter_provenance_includes_the_shared_notch_in_branch_delay(self) -> None:
        engine = RealtimeEventEngine(_engine_config(), 500.0, CHANNELS)

        self.assertIn("shared_notch_60", engine.filters.provenance)
        self.assertEqual(engine.filters.provenance["erp_0p5_30"]["components"], ["shared_notch_60", "erp_0p5_30"])
        self.assertIn("coefficient_sha256", engine.filters.provenance["shared_notch_60"])

    def test_nonfinite_reference_input_invalidates_quality_without_poisoning_filter_state(self) -> None:
        sample_rate = 100.0
        timestamps, data = _synthetic_eeg(sample_rate)
        data[(timestamps >= 2.6) & (timestamps <= 2.8), 0] = np.nan
        engine = RealtimeEventEngine(_engine_config(), sample_rate, CHANNELS)
        engine.process_chunk(timestamps[:250], data[:250])
        engine.add_marker(MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 2.5))
        packets = engine.process_chunk(timestamps[250:], data[250:])
        n2 = next(packet for packet in packets if packet["stage"] == "n2_theta")
        _filtered_timestamps, filtered = engine.filters.buffers["erp_0p5_30"].window(50)

        self.assertFalse(n2["reference_valid"])
        self.assertTrue(np.isfinite(filtered).all())

    def test_exact_capture_replay_passes(self) -> None:
        sample_rate = 100.0
        timestamps, data = _synthetic_eeg(sample_rate)
        config = _engine_config()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "realtime").mkdir()
            writer = EngineInputCaptureWriter(
                root / "realtime" / "engine_input.bin",
                {"sample_rate_hz": sample_rate, "channel_names": CHANNELS, "event_features_config": config},
            )
            engine = RealtimeEventEngine(config, sample_rate, CHANNELS)
            online = []
            writer.write_eeg(timestamps[:250], data[:250])
            online.extend(engine.process_chunk(timestamps[:250], data[:250]))
            marker = MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 2.5)
            writer.write_marker(marker)
            online.extend(engine.add_marker(marker))
            writer.write_eeg(timestamps[250:], data[250:])
            online.extend(engine.process_chunk(timestamps[250:], data[250:]))
            writer.close()
            with (root / "realtime" / "event_features.jsonl").open("w", encoding="utf-8") as handle:
                for packet in online:
                    handle.write(json.dumps(packet) + "\n")

            summary = replay_realtime_session(root)

            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["online_packet_count"], 4)
            self.assertEqual(summary["material_difference_count"], 0)

    def test_replay_rejects_matching_online_and_replay_streams_that_missed_task_trials(self) -> None:
        sample_rate = 100.0
        timestamps, data = _synthetic_eeg(sample_rate)
        config = _engine_config()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "realtime").mkdir()
            (root / "events").mkdir()
            (root / "events" / "stimulus_manifest.json").write_text(
                json.dumps({"trials": [{"trial": 1}, {"trial": 2}]}),
                encoding="utf-8",
            )
            writer = EngineInputCaptureWriter(
                root / "realtime" / "engine_input.bin",
                {"sample_rate_hz": sample_rate, "channel_names": CHANNELS, "event_features_config": config},
            )
            engine = RealtimeEventEngine(config, sample_rate, CHANNELS)
            writer.write_eeg(timestamps[:250], data[:250])
            online = engine.process_chunk(timestamps[:250], data[:250])
            marker = MarkerEvent("go_nogo_stimulus_onset_1_go_circle_red", 2.5)
            writer.write_marker(marker)
            online.extend(engine.add_marker(marker))
            writer.write_eeg(timestamps[250:], data[250:])
            online.extend(engine.process_chunk(timestamps[250:], data[250:]))
            writer.close()
            with (root / "realtime" / "event_features.jsonl").open("w", encoding="utf-8") as handle:
                for packet in online:
                    handle.write(json.dumps(packet) + "\n")

            summary = replay_realtime_session(root)

            self.assertEqual(summary["status"], "analytically_invalid")
            self.assertIn("task_manifest_stage_coverage_incomplete", summary["reasons"])
            self.assertEqual(len(summary["missing_expected_online_stages"]), 4)

    def test_replay_audits_but_does_not_invalidate_negative_practice_trials(self) -> None:
        sample_rate = 100.0
        timestamps, data = _synthetic_eeg(sample_rate)
        config = _engine_config()
        config["include_practice_trials"] = True
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "realtime").mkdir()
            (root / "events").mkdir()
            (root / "events" / "stimulus_manifest.json").write_text(
                json.dumps({"trials": [{"trial": 1}]}),
                encoding="utf-8",
            )
            writer = EngineInputCaptureWriter(
                root / "realtime" / "engine_input.bin",
                {"sample_rate_hz": sample_rate, "channel_names": CHANNELS, "event_features_config": config},
            )
            engine = RealtimeEventEngine(config, sample_rate, CHANNELS)
            writer.write_eeg(timestamps[:250], data[:250])
            online = engine.process_chunk(timestamps[:250], data[:250])
            for label in ("go_nogo_stimulus_onset_-1_go_circle_red", "go_nogo_stimulus_onset_1_go_circle_red"):
                marker = MarkerEvent(label, 2.5)
                writer.write_marker(marker)
                online.extend(engine.add_marker(marker))
            writer.write_eeg(timestamps[250:], data[250:])
            online.extend(engine.process_chunk(timestamps[250:], data[250:]))
            writer.close()
            with (root / "realtime" / "event_features.jsonl").open("w", encoding="utf-8") as handle:
                for packet in online:
                    handle.write(json.dumps(packet) + "\n")

            summary = replay_realtime_session(root)

            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["acceptance_online_packet_count"], 4)
            self.assertEqual(summary["excluded_practice_online_packet_count"], 4)
            self.assertEqual(summary["material_difference_count"], 0)

    def test_behavior_analysis_is_gated_and_commission_model_handles_sparse_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "reports" / "realtime_features").mkdir(parents=True)
            (root / "events").mkdir()
            (root / "realtime").mkdir()
            (root / "reports" / "realtime_features" / "replay_summary.json").write_text(
                json.dumps({"status": "pass"}),
                encoding="utf-8",
            )
            trials = []
            packets = []
            for index in range(1, 31):
                is_no_go = index % 4 == 0
                error = is_no_go and index == 8
                rt = None if is_no_go else 0.2 + index * 0.01
                trials.append(
                    {
                        "trial": index,
                        "stimulus": {"is_no_go": is_no_go},
                        "response": {
                            "reaction_time_seconds": rt,
                            "button_press_count": 1 if (not is_no_go or error) else 0,
                            "correct_press": 0 if error else 1,
                        },
                    }
                )
                packets.append(
                    {
                        "packet_index": index,
                        "trial": index,
                        "stage": "alpha_erd",
                        "valid": True,
                        "features": {
                            "readiness_alpha_power": 1.0 + index * 0.01,
                            "early_theta_power": 2.0 + index * 0.01,
                            "n2_mean_uv": -2.0 - index * 0.01,
                            "p3_mean_uv": 3.0 + index * 0.01,
                            "alpha_erd_percent": -10.0 + index * 0.1,
                        },
                    }
                )
            (root / "events" / "stimulus_manifest.json").write_text(json.dumps({"trials": trials}), encoding="utf-8")
            with (root / "realtime" / "event_features.jsonl").open("w", encoding="utf-8") as handle:
                for packet in packets:
                    handle.write(json.dumps(packet) + "\n")

            summary = run_feature_behavior_analysis(root, {"permutations": 5})

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["analyses"]["trial_type_readiness_negative_control"]["status"], "ok")
            self.assertEqual(summary["analyses"]["correct_go_reaction_time_readiness"]["status"], "ok")
            self.assertEqual(summary["analyses"]["commission_error_no_go"]["status"], "skipped")
            table = (root / "reports" / "realtime_features" / "trial_features.csv").read_text(encoding="utf-8")
            self.assertIn("lapse_score", table)
            self.assertIn("slow_trial", table)

    def test_alpha_validation_recognizes_staged_inhibition8_alpha_without_legacy_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "realtime").mkdir()
            (root / "events").mkdir()
            packets = [
                {"trial": 1, "stage": "prestim_state", "valid": True, "features": {"readiness_alpha_power": 2.0}},
                {"trial": 1, "stage": "alpha_erd", "valid": True, "features": {"readiness_alpha_power": 2.0, "alpha_erd_percent": -10.0}},
            ]
            with (root / "realtime" / "event_features.jsonl").open("w", encoding="utf-8") as handle:
                for packet in packets:
                    handle.write(json.dumps(packet) + "\n")

            summary = run_alpha_validation(root, {})

            self.assertEqual(summary["status"], "ok_staged_event_features")
            self.assertEqual(summary["live_alpha_quality"]["status"], "usable")
            self.assertEqual(summary["live_alpha_quality"]["estimate_count"], 2)

    def test_html_segments_keep_task_practice_separate_from_main_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "events").mkdir()
            events = [
                {"trial": -1, "label": "go_nogo_stimulus_onset_-1_go_circle_red", "timestamp": 201.0, "metadata": {"lsl_timestamp": 101.0}},
                {"trial": -1, "label": "go_nogo_stimulus_offset_-1_go_circle_red", "timestamp": 202.0, "metadata": {"lsl_timestamp": 102.0}},
            ]
            with (root / "events" / "events.jsonl").open("w", encoding="utf-8") as handle:
                for row in events:
                    handle.write(json.dumps(row) + "\n")
            (root / "events" / "stimulus_manifest.json").write_text(
                json.dumps({"trials": [{"trial": 1, "onset_lsl_timestamp": 110.0, "offset_lsl_timestamp": 111.0}]}),
                encoding="utf-8",
            )

            segments = _build_segments(root, {"duration_seconds": 20.0}, 100.0, 200.0, "lsl")
            by_key = {row["key"]: row for row in segments}

            self.assertIn("task_practice", by_key)
            self.assertIn("task", by_key)
            self.assertLess(by_key["task_practice"]["end"], by_key["task"]["start"])

    def test_behavior_summary_counts_main_task_button_press_rts_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "behavior.csv"
            path.write_text(
                "trial,label,event_type,timestamp,value,metadata\n"
                '-1,button_press,EVENT,1,,\"{\"\"rt_seconds\"\": 0.9}\"\n'
                '1,button_press,EVENT,2,,\"{\"\"rt_seconds\"\": 0.3}\"\n'
                '2,go_nogo_stimulus_onset_2_nogo_x_white,EVENT,3,,\"{}\"\n',
                encoding="utf-8",
            )

            summary = _behavior_summary(path)

            self.assertEqual(summary["trials"], 2)
            self.assertEqual(summary["responses"], 1)
            self.assertAlmostEqual(summary["mean_rt_seconds"], 0.3)


def _engine_config() -> dict[str, object]:
    return {
        "montage_profile": "enobio8_inhibition",
        "fixed_reference_channels": CHANNELS,
        "alpha_band": {"low_hz": 8.0, "high_hz": 12.0},
        "filter_warmup_seconds": 0.1,
        "quality_gate": {
            "minimum_reference_channels": 4,
            "max_abs_uv": 250.0,
            "max_peak_to_peak_uv": 400.0,
            "max_broadband_diff_std_uv": 100.0,
        },
    }


def _synthetic_eeg(sample_rate: float) -> tuple[np.ndarray, np.ndarray]:
    timestamps = np.arange(0.0, 4.0, 1.0 / sample_rate)
    rng = np.random.default_rng(7)
    data = rng.normal(0.0, 0.5, size=(timestamps.size, len(CHANNELS)))
    data[:, 2] += 3.0 * np.sin(2.0 * np.pi * 10.0 * timestamps)
    data[:, 5] += 2.5 * np.sin(2.0 * np.pi * 10.0 * timestamps)
    n2 = (timestamps >= 2.70) & (timestamps <= 2.84)
    p3 = (timestamps >= 2.82) & (timestamps <= 3.08)
    data[n2, 0] -= 4.0
    data[n2, 1] -= 3.0
    data[p3, 2] += 5.0
    data[p3, 5] += 4.0
    data[p3, 6] += 4.0
    return timestamps, data


if __name__ == "__main__":
    unittest.main()
