from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eegle.config import load_config
from eegle.hardware.profiles import analysis_channel_indices, configured_channel_types, mapped_channel_names
from eegle.pipelines.study1 import (
    SIMULATED_NEURACLE64_CHANNELS,
    Study1Options,
    _apply_visit_baseline,
    _configure_simulated_eeg_rehearsal,
    _validate_options,
    run_study1_visit,
)
from eegle.protocols.study1 import configure_study1_segment, validate_study1_config
from eegle.realtime.epoching import load_eeg_csv_for_epoching
from eegle.tasks.dynamic_sart_schema import DynamicSartConfig
from eegle.tasks.dynamic_sart_sequence import build_dynamic_sart_plan
from scripts.prepare_neuracle64_windows_config import build_configs


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "study1_neuracle64.json"


class Study1Tests(unittest.TestCase):
    def test_windows_config_generator_keeps_display_run_hardware_free(self) -> None:
        base = load_config(CONFIG)
        original = copy.deepcopy(base)

        display, live, live_task = build_configs(base)

        self.assertEqual(base, original)
        self.assertIsNone(live)
        self.assertIsNone(live_task)
        self.assertFalse(display["hardware"]["eeg"]["required_for_run"])
        self.assertFalse(display["processes"]["recorder"]["enabled"])
        self.assertEqual(display["experiment"]["components"]["eeg_recorder"], "disabled")
        self.assertFalse(display["tasks"]["dynamic_sart"]["practice"]["enabled"])

    def test_windows_live_config_requires_and_records_cap_confirmation(self) -> None:
        base = load_config(CONFIG)
        with self.assertRaisesRegex(ValueError, "labrecorder_executable"):
            build_configs(base, confirm_cap_contract=True)

        display, live, live_task = build_configs(
            base,
            labrecorder_executable=r"C:\\LabRecorder\\LabRecorder.exe",
            extra_lsl_name_patterns=["Neuracle-Collect-Test"],
            confirm_cap_contract=True,
        )

        self.assertIsNotNone(display)
        self.assertIsNotNone(live)
        self.assertIsNotNone(live_task)
        assert live is not None
        assert live_task is not None
        self.assertEqual(live["hardware"]["eeg"]["expected_channel_names"], list(SIMULATED_NEURACLE64_CHANNELS))
        self.assertEqual(len(live["hardware"]["eeg"]["expected_channel_names"]), 65)
        self.assertEqual(live["hardware"]["eeg"]["expected_channel_names"][-1], "TRIGGER_STATUS")
        self.assertEqual(len(live["hardware"]["eeg"]["electrode_channel_names"]), 64)
        self.assertEqual(live["processes"]["recorder"]["backend"], "labrecorder_xdf")
        self.assertTrue(live["processes"]["recorder"]["csv_mirror"])
        self.assertTrue(live["operator_confirmation"]["confirmed_for_this_generated_config"])
        self.assertIn("neuracle-collect-test", live["hardware"]["eeg"]["lsl_name_patterns"])
        self.assertFalse(live_task["tasks"]["dynamic_sart"]["practice"]["enabled"])

    def test_live_contract_preserves_65_values_but_selects_only_59_scalp_channels(self) -> None:
        _display, live, _live_task = build_configs(
            load_config(CONFIG),
            labrecorder_executable=r"C:\\LabRecorder\\LabRecorder.exe",
            confirm_cap_contract=True,
        )
        assert live is not None
        eeg = live["hardware"]["eeg"]
        names = list(eeg["expected_channel_names"])
        selected = analysis_channel_indices(names, eeg)

        self.assertEqual(len(names), 65)
        self.assertEqual(len(selected), 59)
        self.assertEqual([names[index] for index in selected][-3:], ["Oz", "O1", "O2"])
        self.assertEqual(configured_channel_types(names, eeg)[-6:], ["ecg", "eog", "eog", "eog", "eog", "stim"])

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "pandas is required for CSV epoch loading")
    def test_offline_epoch_loading_excludes_auxiliary_and_trigger_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "eeg.csv"
            parameters = root / "parameters.json"
            names = list(SIMULATED_NEURACLE64_CHANNELS)
            raw.write_text(
                "lsl_timestamp,local_received_time," + ",".join(names) + "\n"
                + "1.0,1.0," + ",".join("0" for _ in names) + "\n",
                encoding="utf-8",
            )
            _display, live, _live_task = build_configs(
                load_config(CONFIG),
                labrecorder_executable=r"C:\\LabRecorder\\LabRecorder.exe",
                confirm_cap_contract=True,
            )
            assert live is not None
            parameters.write_text(json.dumps(live), encoding="utf-8")

            bundle = load_eeg_csv_for_epoching(raw, parameters_path=parameters)

        self.assertEqual(bundle.data.shape, (1, 59))
        self.assertEqual(bundle.channel_names[-3:], ["Oz", "O1", "O2"])

    def test_preflight_only_runs_physical_gate_without_creating_a_visit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = load_config(CONFIG)
            config["hardware"]["eeg"].update(
                {
                    "expected_channel_names": list(SIMULATED_NEURACLE64_CHANNELS),
                    "expected_channel_types": ["EEG"] * 59 + ["ECG"] + ["EOG"] * 4 + ["stim"],
                    "electrode_channel_names": list(SIMULATED_NEURACLE64_CHANNELS[:-1]),
                    "reference": "operator-confirmed: CPz",
                    "ground": "operator-confirmed: AFz",
                    "eog_allocation": "operator-confirmed: ECG, HEOR, HEOL, VEOU, VEOL",
                }
            )
            config_path = root / "live.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            report = {
                "status": "pass",
                "report_file": str(root / "preflight.json"),
                "electrode_quality_file": str(root / "electrodes.json"),
                "channel_contract": {"status": "ok"},
                "failures": [],
                "warnings": [],
            }
            with patch(
                "eegle.pipelines.study1.run_recording_preflight",
                return_value=report,
            ) as preflight:
                result = run_study1_visit(
                    Study1Options(
                        config_path=config_path,
                        participant_id="physical-check",
                        visit_number=1,
                        visit_id="preflight-001",
                        preflight_only=True,
                        electrodes_confirmed=True,
                        output_root=root,
                    )
                )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["mode"], "preflight_only")
            self.assertFalse((root / "study1").exists())
            call = preflight.call_args.kwargs
            self.assertTrue(call["require_eeg"])
            self.assertTrue(call["record_eeg"])
            self.assertEqual(call["phase"], "preflight_only")

    def test_preflight_only_rejects_software_and_simulated_eeg_modes(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires EEG"):
            _validate_options(
                Study1Options(
                    config_path=CONFIG,
                    participant_id="unit",
                    visit_number=1,
                    record_eeg=False,
                    require_eeg=False,
                    preflight_only=True,
                )
            )
        with self.assertRaisesRegex(ValueError, "physical EEG system"):
            _validate_options(
                Study1Options(
                    config_path=CONFIG,
                    participant_id="unit",
                    visit_number=1,
                    task_mode="dry-run",
                    simulate_eeg=True,
                    preflight_only=True,
                )
            )

    def test_simulated_xdf_rehearsal_uses_observed_65_value_transport_contract(self) -> None:
        config = load_config(CONFIG)

        _configure_simulated_eeg_rehearsal(config)

        eeg = config["hardware"]["eeg"]
        self.assertEqual(eeg["expected_channel_names"], list(SIMULATED_NEURACLE64_CHANNELS))
        self.assertEqual(len(eeg["expected_channel_names"]), 65)
        self.assertEqual(eeg["expected_channel_names"][-1], "TRIGGER_STATUS")
        self.assertEqual(len(eeg["electrode_channel_names"]), 64)
        self.assertTrue(eeg["simulated"])
        self.assertEqual(eeg["data_classification"], "synthetic_rehearsal_not_participant_data")
        self.assertTrue(config["recording_rehearsal"]["not_participant_data"])
        self.assertTrue(config["recording_rehearsal"]["auxiliary_order_is_assumption"])

    def test_simulated_eeg_rejects_skip_eeg_and_visual_task_modes(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires --task-mode dry-run"):
            _validate_options(
                Study1Options(
                    config_path=CONFIG,
                    participant_id="unit",
                    visit_number=1,
                    task_mode="psychopy",
                    simulate_eeg=True,
                )
            )
        with self.assertRaisesRegex(ValueError, "cannot be combined with --skip-eeg"):
            _validate_options(
                Study1Options(
                    config_path=CONFIG,
                    participant_id="unit",
                    visit_number=1,
                    task_mode="dry-run",
                    record_eeg=False,
                    require_eeg=False,
                    simulate_eeg=True,
                )
            )

    def test_candidate_config_builds_all_proposal_segments(self) -> None:
        config = load_config(CONFIG)
        self.assertEqual(
            validate_study1_config(config),
            [
                {
                    "status": "warn",
                    "detail": "Neuracle W64 65-value LSL names/order remain unlocked; live preflight will fail until confirmed",
                },
                {
                    "status": "warn",
                    "detail": "cue assignments and markers are implemented, but physical auditory delivery remains gated",
                },
            ],
        )
        expectations = {
            "session1_main": (600, 3, 0),
            "session2_main": (600, 3, 0),
            "session2_cue_extension": (400, 2, 20),
        }
        for index, (segment, (trial_count, block_count, cue_count)) in enumerate(expectations.items(), start=1):
            child = configure_study1_segment(config, segment, no_go_digit=6, seed=100 + index)
            task = DynamicSartConfig.from_mapping(child["tasks"]["dynamic_sart"])
            plan = build_dynamic_sart_plan(task)
            self.assertEqual(len(plan["planned_trials"]), trial_count)
            self.assertEqual(len(plan["planned_blocks"]), block_count)
            self.assertEqual([row["planned_no_go_count"] for row in plan["planned_blocks"]], [30] * block_count)
            self.assertEqual(len(plan["cue_schedule"]["opportunities"]), cue_count)
            self.assertTrue(all(1.75 <= row["planned_soi_seconds"] <= 2.15 for row in plan["planned_trials"]))
            self.assertTrue(all(row["digit"] in range(10) for row in plan["planned_trials"]))
            self.assertTrue(all(row["digit"] == 6 for row in plan["planned_trials"] if row["is_no_go"]))

    def test_short_visit_contract_is_one_minute_each_practice_and_30_trials(self) -> None:
        config = load_config(CONFIG)
        options = Study1Options(
            config_path=CONFIG,
            participant_id="system-test",
            visit_number=1,
            smoke=True,
            include_practice=True,
            baseline_seconds=60.0,
        )

        _apply_visit_baseline(config, options)
        child = configure_study1_segment(
            config,
            "session1_main",
            no_go_digit=3,
            seed=42,
            smoke=True,
            include_practice=True,
        )

        self.assertEqual(
            config["recording_suite"]["baseline"],
            {"eyes_open_seconds": 60.0, "eyes_closed_seconds": 60.0},
        )
        self.assertTrue(child["tasks"]["dynamic_sart"]["practice"]["enabled"])
        self.assertEqual(
            [block["trials"] for block in child["tasks"]["dynamic_sart"]["blocks"]],
            [10, 10, 10],
        )

    def test_protocol_rejects_non_1000_hz_acquisition_contract(self) -> None:
        config = load_config(CONFIG)
        config["hardware"]["eeg"]["expected_sample_rate_hz"] = 500

        issues = validate_study1_config(config)

        self.assertTrue(
            any(issue["status"] == "fail" and "1000 Hz" in issue["detail"] for issue in issues)
        )

    def test_cue_assignments_are_two_of_four_and_deterministic(self) -> None:
        config = load_config(CONFIG)
        first_config = configure_study1_segment(
            config,
            "session2_cue_extension",
            no_go_digit=4,
            seed=31415,
        )
        second_config = configure_study1_segment(
            config,
            "session2_cue_extension",
            no_go_digit=4,
            seed=31415,
        )
        first = build_dynamic_sart_plan(DynamicSartConfig.from_mapping(first_config["tasks"]["dynamic_sart"]))
        second = build_dynamic_sart_plan(DynamicSartConfig.from_mapping(second_config["tasks"]["dynamic_sart"]))
        self.assertEqual(first["sequence_id"], second["sequence_id"])
        opportunities = first["cue_schedule"]["opportunities"]
        self.assertEqual([row["trial_index"] for row in opportunities], list(range(20, 401, 20)))
        for start in range(0, len(opportunities), 4):
            assignments = [row["assignment"] for row in opportunities[start : start + 4]]
            self.assertEqual(assignments.count("cue"), 2)
            self.assertEqual(assignments.count("no_cue"), 2)

    def test_neuracle_generic_labels_use_locked_config_order(self) -> None:
        expected = [f"E{index}" for index in range(1, 66)]
        observed = [f"Ch{index}" for index in range(1, 66)]
        mapped, source = mapped_channel_names(
            observed,
            {
                "family": "Neuracle",
                "profile": "neuracle64",
                "expected_channel_names": expected,
            },
        )
        self.assertEqual(mapped, expected)
        self.assertEqual(source, "config:expected_channel_names")

    def test_two_visit_smoke_reuses_participant_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            visit_one = run_study1_visit(
                Study1Options(
                    config_path=CONFIG,
                    participant_id="unit-study1",
                    visit_number=1,
                    visit_id="unit-visit-1",
                    task_mode="dry-run",
                    master_seed=72,
                    no_go_digit=9,
                    smoke=True,
                    baseline_seconds=0.0,
                    record_eeg=False,
                    require_eeg=False,
                    output_root=tmp,
                )
            )
            self.assertEqual(visit_one["status"], "completed")
            baseline_parameters = load_config(
                Path(visit_one["session_directories"]["baseline"]) / "parameters.json"
            )
            main_parameters = load_config(
                Path(visit_one["session_directories"]["session1_main"]) / "parameters.json"
            )
            self.assertEqual(
                baseline_parameters["processes"]["recorder"]["backend"],
                "labrecorder_xdf",
            )
            self.assertEqual(
                main_parameters["processes"]["recorder"]["backend"],
                "labrecorder_xdf",
            )
            visit_two = run_study1_visit(
                Study1Options(
                    config_path=CONFIG,
                    participant_id="unit-study1",
                    visit_number=2,
                    visit_id="unit-visit-2",
                    task_mode="dry-run",
                    master_seed=72,
                    smoke=True,
                    baseline_seconds=0.0,
                    record_eeg=False,
                    require_eeg=False,
                    output_root=tmp,
                    allow_visit_interval_override=True,
                )
            )
            self.assertEqual(visit_two["status"], "completed")
            self.assertEqual(visit_one["no_go_digit"], 9)
            self.assertEqual(visit_two["no_go_digit"], 9)
            self.assertEqual(set(visit_two["sequence_hashes"]), {"session2_main", "session2_cue_extension"})
            resumed = run_study1_visit(
                Study1Options(
                    config_path=CONFIG,
                    participant_id="unit-study1",
                    visit_number=2,
                    visit_id="unit-visit-2",
                    task_mode="dry-run",
                    master_seed=72,
                    smoke=True,
                    baseline_seconds=0.0,
                    record_eeg=False,
                    require_eeg=False,
                    resume=True,
                    output_root=tmp,
                    allow_visit_interval_override=True,
                )
            )
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["sequence_hashes"], visit_two["sequence_hashes"])

    def test_live_participant_requires_explicit_counterbalance_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "--no-go-digit"):
                run_study1_visit(
                    Study1Options(
                        config_path=CONFIG,
                        participant_id="unit-live",
                        visit_number=1,
                        task_mode="dry-run",
                        record_eeg=True,
                        require_eeg=True,
                        output_root=tmp,
                    )
                )


if __name__ == "__main__":
    unittest.main()
