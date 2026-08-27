from __future__ import annotations

import copy
import io
import importlib.util
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from eegle.config import load_config
from eegle.hardware.profiles import analysis_channel_indices, configured_channel_types, mapped_channel_names
from eegle.pipelines.study1 import (
    SIMULATED_NEURACLE64_CHANNELS,
    VISIT_SCHEMA,
    Study1Options,
    _apply_visit_baseline,
    _configure_simulated_eeg_rehearsal,
    _find_resume_manifest,
    _options_from_args,
    _persisted_electrode_confirmation,
    _resolve_visit_id,
    _resume_options_with_persisted_task_shape,
    _study1_phase_result_failures,
    _validate_options,
    _validate_visit_slot,
    build_parser,
    main,
    run_study1_visit,
)
from eegle.protocols.study1 import (
    STUDY1_FULL_1000_ACQUISITION_PROFILE,
    apply_study1_full_1000_profile,
    configure_study1_segment,
    study1_protocol,
    study1_protocol_hash,
    validate_study1_config,
)
from eegle.realtime.epoching import load_eeg_csv_for_epoching
from eegle.session import create_session
from eegle.tasks.dynamic_sart import DynamicSartTask
from eegle.tasks.dynamic_sart_schema import DynamicSartConfig
from eegle.tasks.dynamic_sart_sequence import build_dynamic_sart_plan, validate_dynamic_sart_plan
from scripts.prepare_neuracle64_windows_config import build_configs, refresh_confirmed_configs


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
        self.assertTrue(display["hardware"]["display"]["resizable"])
        self.assertTrue(display["hardware"]["display"]["wait_blanking"])
        self.assertTrue(display["hardware"]["display"]["require_refresh_rate_match"])
        self.assertEqual(display["tasks"]["dynamic_sart"]["response_window_seconds"], 1.6)

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
        self.assertFalse(live["processes"]["recorder"]["csv_mirror"])
        self.assertTrue(live["processes"]["recorder"]["lsl_sample_heartbeat"])
        self.assertTrue(live["operator_confirmation"]["confirmed_for_this_generated_config"])
        self.assertIn("neuracle-collect-test", live["hardware"]["eeg"]["lsl_name_patterns"])
        self.assertFalse(live_task["tasks"]["dynamic_sart"]["practice"]["enabled"])

    def test_windows_live_config_refresh_replaces_stale_protocol_values(self) -> None:
        base = load_config(CONFIG)
        _display, old_live, _old_live_task = build_configs(
            base,
            labrecorder_executable=r"C:\\LabRecorder\\LabRecorder.exe",
            extra_lsl_name_patterns=["M_73393543_EEG"],
            confirm_cap_contract=True,
        )
        assert old_live is not None
        old_live["tasks"]["dynamic_sart"].update(
            {
                "response_window_seconds": 1.15,
                "inter_trial_jitter_min_seconds": 0.05,
                "inter_trial_jitter_max_seconds": 0.15,
                "soi_min_seconds": 1.20,
                "soi_max_seconds": 1.30,
            }
        )
        old_live["hardware"]["display"].update(
            {
                "wait_blanking": False,
                "check_refresh_rate": False,
                "require_refresh_rate_match": False,
                "refresh_rate_tolerance_hz": 1000.0,
                "refresh_rate_stability_threshold_ms": 1000.0,
                "refresh_rate_measurement_attempts": 1,
            }
        )

        _display, refreshed, refreshed_task = refresh_confirmed_configs(base, old_live)

        self.assertFalse(
            [issue for issue in validate_study1_config(refreshed) if issue["status"] == "fail"]
        )
        task = refreshed["tasks"]["dynamic_sart"]
        self.assertEqual(task["stimulus_seconds"], 0.25)
        self.assertEqual(task["response_window_seconds"], 1.6)
        self.assertEqual(task["inter_trial_jitter_min_seconds"], 0.0)
        self.assertEqual(task["inter_trial_jitter_max_seconds"], 0.0)
        self.assertEqual(task["soi_min_seconds"], 1.6)
        self.assertEqual(task["soi_max_seconds"], 1.6)
        self.assertTrue(refreshed["hardware"]["display"]["wait_blanking"])
        self.assertTrue(refreshed["hardware"]["display"]["check_refresh_rate"])
        self.assertTrue(refreshed["hardware"]["display"]["require_refresh_rate_match"])
        self.assertEqual(refreshed["hardware"]["display"]["refresh_rate_tolerance_hz"], 2.0)
        self.assertEqual(
            refreshed["hardware"]["display"]["refresh_rate_stability_threshold_ms"],
            1.0,
        )
        self.assertEqual(refreshed["hardware"]["display"]["refresh_rate_measurement_attempts"], 3)
        self.assertEqual(refreshed["hardware"]["display"]["refresh_rate_sample_frames"], 90)
        self.assertEqual(
            refreshed["hardware"]["display"]["refresh_rate_window_settle_seconds"],
            2.0,
        )
        self.assertIn("m_73393543_eeg", refreshed["hardware"]["eeg"]["lsl_name_patterns"])
        self.assertEqual(
            refreshed["operator_confirmation"],
            old_live["operator_confirmation"],
        )
        self.assertFalse(refreshed_task["tasks"]["dynamic_sart"]["practice"]["enabled"])

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
            self.assertTrue(all(row["planned_soi_seconds"] == 1.6 for row in plan["planned_trials"]))
            self.assertTrue(all(row["planned_jitter_seconds"] == 0.0 for row in plan["planned_trials"]))
            self.assertTrue(
                all(row["planned_post_digit_fixation_seconds"] == 1.35 for row in plan["planned_trials"])
            )
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

    def test_full_1000_profile_is_four_sections_with_temporal_support_query_split(self) -> None:
        base = load_config(CONFIG)
        original = copy.deepcopy(base)

        full = apply_study1_full_1000_profile(base)
        failures = [
            issue["detail"] for issue in validate_study1_config(full) if issue["status"] == "fail"
        ]
        child = configure_study1_segment(
            full,
            "session1_main",
            no_go_digit=3,
            seed=2026,
            include_practice=True,
        )
        parsed = DynamicSartConfig.from_mapping(child["tasks"]["dynamic_sart"])
        plan = build_dynamic_sart_plan(parsed)
        blocks = plan["planned_blocks"]
        trials = plan["planned_trials"]

        self.assertEqual(base, original)
        self.assertEqual(failures, [])
        self.assertEqual(
            full["study1"]["acquisition_profile"],
            STUDY1_FULL_1000_ACQUISITION_PROFILE,
        )
        self.assertEqual([block["trials"] for block in blocks], [250, 250, 250, 250])
        self.assertEqual(
            [block["phase"] for block in blocks],
            ["support", "support", "query", "query"],
        )
        self.assertEqual(
            [block["planned_no_go_count"] for block in blocks],
            [38, 37, 38, 37],
        )
        self.assertEqual(
            [block["break_after"] for block in blocks],
            [True, True, True, False],
        )
        self.assertEqual(len(trials), 1000)
        self.assertTrue(all(row["phase"] == "support" for row in trials[:500]))
        self.assertTrue(all(row["phase"] == "query" for row in trials[500:]))
        self.assertEqual(sum(row["is_no_go"] for row in trials), 150)
        self.assertEqual(parsed.minimum_go_trials_between_no_go, 0)
        self.assertEqual(parsed.minimum_leading_go_trials, 4)
        self.assertEqual(parsed.minimum_trailing_go_trials, 4)
        self.assertEqual(
            parsed.no_go_randomization,
            {
                "mode": "stratified_weighted",
                "stratum_trials": 50,
                "maximum_consecutive_no_go": 2,
                "adjacent_no_go_weight": 0.10,
                "one_go_gap_weight": 0.35,
            },
        )
        condition_sequences = []
        observed_gaps = []
        for block_index, expected_count in enumerate((38, 37, 38, 37), start=1):
            section = [row for row in trials if row["block_index"] == block_index]
            positions = [row["block_trial_index"] for row in section if row["is_no_go"]]
            gaps = [right - left - 1 for left, right in zip(positions, positions[1:])]
            strata = [
                sum(row["is_no_go"] for row in section[start : start + 50])
                for start in range(0, 250, 50)
            ]
            self.assertEqual(sum(strata), expected_count)
            self.assertLessEqual(max(strata) - min(strata), 1)
            self.assertGreater(positions[0], 4)
            self.assertLessEqual(positions[-1], 246)
            self.assertFalse(any(
                section[index]["is_no_go"]
                and section[index + 1]["is_no_go"]
                and section[index + 2]["is_no_go"]
                for index in range(len(section) - 2)
            ))
            condition_sequences.append(tuple(row["is_no_go"] for row in section))
            observed_gaps.extend(gaps)
        self.assertNotEqual(condition_sequences[0], condition_sequences[2])
        self.assertNotEqual(condition_sequences[1], condition_sequences[3])
        self.assertIn(0, observed_gaps)
        self.assertIn(1, observed_gaps)
        self.assertGreater(len(set(observed_gaps)), 8)
        self.assertTrue(parsed.practice_enabled)
        self.assertTrue(parsed.practice_require_ready_confirmation)
        self.assertEqual(
            study1_protocol(STUDY1_FULL_1000_ACQUISITION_PROFILE).targets[0].metadata,
            {"support_trials": 500, "query_trials": 500},
        )

    def test_full_1000_options_allow_operator_run_length_overrides(self) -> None:
        base = dict(
            config_path=CONFIG,
            participant_id="unit",
            visit_number=1,
            full_1000=True,
            include_practice=True,
            trials=40,
            practice_trials=12,
            practice_no_go_trials=2,
            practice_max_rounds=1,
            baseline_seconds=60,
        )
        _validate_options(Study1Options(**base))
        _validate_options(
            Study1Options(**{**base, "include_practice": False, "skip_practice": True})
        )
        _validate_options(
            Study1Options(
                **{**base, "baseline_seconds": None, "skip_baseline": True}
            )
        )
        with self.assertRaisesRegex(ValueError, "cannot be combined with --smoke"):
            _validate_options(Study1Options(**base, smoke=True))
        with self.assertRaisesRegex(ValueError, "--trials must be at least 10"):
            _validate_options(Study1Options(**{**base, "trials": 9}))
        with self.assertRaisesRegex(ValueError, "must be less than --practice-trials"):
            _validate_options(
                Study1Options(
                    **{**base, "practice_trials": 10, "practice_no_go_trials": 10}
                )
            )
        with self.assertRaisesRegex(ValueError, "either --include-practice or --skip-practice"):
            _validate_options(Study1Options(**{**base, "skip_practice": True}))
        with self.assertRaisesRegex(ValueError, "either --baseline-seconds or --skip-baseline"):
            _validate_options(Study1Options(**{**base, "skip_baseline": True}))

    def test_full_windows_launcher_argument_vectors_match_study1_cli(self) -> None:
        launcher = (
            ROOT / "scripts" / "windows" / "neuracle64" / "07-Run-Full.ps1"
        ).read_text(encoding="utf-8")
        emitted_flags = set(re.findall(r'"(--[a-z0-9-]+)"', launcher))
        common = [
            "--config",
            str(CONFIG),
            "--visit",
            "1",
            "--task-mode",
            "psychopy",
            "--full-1000",
            "--trials",
            "1000",
            "--screen-index",
            "0",
            "--window-size",
            "1000",
            "700",
            "--session-root",
            "/tmp/eegle-study1",
            "--result-file",
            "/tmp/eegle-study1/outcome.json",
            "--lsl-wait",
            "10",
            "--include-practice",
            "--practice-trials",
            "30",
            "--practice-no-go-trials",
            "4",
            "--practice-max-rounds",
            "3",
            "--baseline-seconds",
            "120",
            "--fullscreen",
        ]
        parser = build_parser()
        self.assertEqual(emitted_flags - set(parser._option_string_actions), set())

        new_run = _options_from_args(
            parser.parse_args(
                common
                + [
                    "--participant",
                    "participant-a",
                    "--no-go-digit",
                    "0",
                    "--operator",
                    "operator-a",
                    "--confirm-electrodes",
                    "--retry-incomplete",
                ]
            )
        )
        resume = _options_from_args(
            parser.parse_args(common + ["--participant", "participant-a", "--resume"])
        )
        _validate_options(new_run)
        _validate_options(resume)

        self.assertTrue(new_run.full_1000)
        self.assertEqual(new_run.trials, 1000)
        self.assertEqual(new_run.no_go_digit, 0)
        self.assertTrue(new_run.electrodes_confirmed)
        self.assertTrue(new_run.retry_incomplete)
        self.assertFalse(new_run.resume)
        self.assertTrue(resume.full_1000)
        self.assertTrue(resume.resume)
        self.assertIsNone(resume.no_go_digit)
        self.assertIsNone(resume.operator)
        self.assertFalse(resume.electrodes_confirmed)

    def test_resume_restores_electrode_confirmation_from_current_and_legacy_manifests(self) -> None:
        options = Study1Options(
            config_path=CONFIG,
            participant_id="participant-a",
            visit_number=1,
            resume=True,
        )
        task_overrides = {
            "experimental_trials": 1000,
            "skip_practice": False,
            "practice_trials_per_round": 30,
            "practice_no_go_trials": 4,
            "practice_max_rounds": 3,
        }
        current = {
            "operator": "operator-a",
            "include_practice": True,
            "electrodes_confirmed": True,
            "task_overrides": task_overrides,
        }
        legacy = {
            "task_overrides": task_overrides,
            "phases": {
                "preflight": {
                    "attempts": [
                        {
                            "result": {
                                "checks": [
                                    {
                                        "name": "electrode_quality",
                                        "data": {"operator_confirmed": True},
                                    }
                                ]
                            }
                        }
                    ]
                }
            },
        }

        restored = _resume_options_with_persisted_task_shape(options, current)

        self.assertTrue(restored.electrodes_confirmed)
        self.assertEqual(restored.operator, "operator-a")
        self.assertTrue(restored.include_practice)
        self.assertEqual(restored.trials, 1000)
        self.assertTrue(
            _persisted_electrode_confirmation(legacy, fallback=False)
        )

    def test_protocol_hash_ignores_baseline_display_and_recorder_operations(self) -> None:
        original = load_config(CONFIG)
        changed = copy.deepcopy(original)
        changed["study1"]["visits"]["1"]["baseline"]["eyes_open_seconds"] = 7.0
        changed["hardware"]["display"]["refresh_rate_measurement_attempts"] = 9
        changed["processes"]["recorder"]["startup_timeout_seconds"] = 99.0

        self.assertEqual(study1_protocol_hash(original), study1_protocol_hash(changed))

        changed["tasks"]["dynamic_sart"]["stimulus_seconds"] = 0.30
        self.assertNotEqual(study1_protocol_hash(original), study1_protocol_hash(changed))

    def test_standard_study1_uses_weighted_strata_and_allows_rare_close_no_go_trials(self) -> None:
        base = load_config(CONFIG)
        child = configure_study1_segment(
            base,
            "session1_main",
            no_go_digit=3,
            seed=42,
        )
        parsed = DynamicSartConfig.from_mapping(child["tasks"]["dynamic_sart"])
        plan = build_dynamic_sart_plan(parsed)
        validate_dynamic_sart_plan(plan, parsed)

        self.assertEqual(parsed.minimum_go_trials_between_no_go, 0)
        self.assertEqual(parsed.minimum_trailing_go_trials, 4)
        self.assertEqual(parsed.no_go_randomization["mode"], "stratified_weighted")
        for block_index in range(1, 4):
            section = [
                row for row in plan["planned_trials"] if row["block_index"] == block_index
            ]
            strata = [
                sum(row["is_no_go"] for row in section[start : start + 50])
                for start in range(0, 200, 50)
            ]
            self.assertLessEqual(max(strata) - min(strata), 1)
            self.assertFalse(any(
                section[index]["is_no_go"]
                and section[index + 1]["is_no_go"]
                and section[index + 2]["is_no_go"]
                for index in range(len(section) - 2)
            ))

    def test_study1_smoke_removes_formal_fifty_trial_strata(self) -> None:
        child = configure_study1_segment(
            load_config(CONFIG),
            "session1_main",
            no_go_digit=3,
            seed=42,
            smoke=True,
        )
        parsed = DynamicSartConfig.from_mapping(child["tasks"]["dynamic_sart"])

        self.assertEqual(parsed.no_go_randomization, {})
        validate_dynamic_sart_plan(build_dynamic_sart_plan(parsed), parsed)

    def test_full_1000_validator_rejects_interleaved_support(self) -> None:
        full = apply_study1_full_1000_profile(load_config(CONFIG))
        blocks = full["study1"]["segments"]["session1_main"]["blocks"]
        blocks[2]["phase"] = "support"

        issues = validate_study1_config(full)

        self.assertTrue(
            any(
                issue["status"] == "fail"
                and (
                    "support blocks must all precede query blocks" in issue["detail"]
                    or "500 leading support trials" in issue["detail"]
                )
                for issue in issues
            )
        )

    def test_full_1000_validator_rejects_randomization_contract_drift(self) -> None:
        full = apply_study1_full_1000_profile(load_config(CONFIG))
        full["tasks"]["dynamic_sart"]["no_go_randomization"]["adjacent_no_go_weight"] = 1.0

        issues = validate_study1_config(full)

        self.assertTrue(any(
            issue["status"] == "fail"
            and "weighted-stratified no-go randomization" in issue["detail"]
            for issue in issues
        ))

    def test_full_1000_task_completes_software_dry_execution(self) -> None:
        full = apply_study1_full_1000_profile(load_config(CONFIG))
        child = configure_study1_segment(
            full,
            "session1_main",
            no_go_digit=3,
            seed=2026,
            include_practice=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_session(
                child,
                task="dynamic_sart",
                participant_id="full-profile-dry",
                root=Path(tmp),
            )
            result = DynamicSartTask(
                child,
                mode="dry-run",
                participant_id="full-profile-dry",
            ).run(paths)
            reference = json.loads(
                (paths.events / "dynamic_sart_support_reference.json").read_text(
                    encoding="utf-8"
                )
            )
            events = [
                json.loads(line)["label"].split("__", 1)[0]
                for line in paths.events_jsonl.read_text(encoding="utf-8").splitlines()
            ]

        self.assertFalse(result.summary["aborted"])
        self.assertEqual(result.summary["experimental_trials"], 1000)
        self.assertEqual(reference["support_trial_budget"], 500)
        self.assertEqual(reference["support_go_trial_budget"], 425)
        self.assertEqual(events.count("dynamic_sart_support_complete"), 1)
        self.assertEqual(events.count("dynamic_sart_break_start"), 3)

    def test_protocol_rejects_non_1000_hz_acquisition_contract(self) -> None:
        config = load_config(CONFIG)
        config["hardware"]["eeg"]["expected_sample_rate_hz"] = 500

        issues = validate_study1_config(config)

        self.assertTrue(
            any(issue["status"] == "fail" and "1000 Hz" in issue["detail"] for issue in issues)
        )

    def test_protocol_requires_csv_mirror_off_and_nonwriting_heartbeat_on(self) -> None:
        config = load_config(CONFIG)
        config["processes"]["recorder"]["csv_mirror"] = True
        config["processes"]["recorder"]["lsl_sample_heartbeat"] = False

        issues = validate_study1_config(config)

        self.assertTrue(any(issue["status"] == "fail" and "CSV mirror" in issue["detail"] for issue in issues))
        self.assertTrue(any(issue["status"] == "fail" and "sample heartbeat" in issue["detail"] for issue in issues))

    def test_protocol_rejects_permissive_refresh_measurement_contract(self) -> None:
        config = load_config(CONFIG)
        display = config["hardware"]["display"]
        display["refresh_rate_tolerance_hz"] = 1000.0
        display["refresh_rate_stability_threshold_ms"] = 1000.0
        display["refresh_rate_measurement_attempts"] = 1

        issues = validate_study1_config(config)
        failures = [issue["detail"] for issue in issues if issue["status"] == "fail"]

        self.assertTrue(any("tolerance" in detail for detail in failures))
        self.assertTrue(any("stability threshold" in detail for detail in failures))
        self.assertTrue(any("at least three attempts" in detail for detail in failures))

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

    def test_neuracle_mixed_labels_use_only_positionally_compatible_mapping(self) -> None:
        expected = list(SIMULATED_NEURACLE64_CHANNELS)
        observed = list(expected)
        observed[0] = "ch_001"
        observed[-1] = "Trigger"
        config = {
            "family": "Neuracle",
            "profile": "neuracle64",
            "expected_channel_names": expected,
        }

        mapped, source = mapped_channel_names(observed, config)
        wrong = list(observed)
        wrong[10] = "Cz"
        rejected, rejected_source = mapped_channel_names(wrong, config)

        self.assertEqual(mapped, expected)
        self.assertEqual(source, "config:expected_channel_names:mixed_positional")
        self.assertEqual(rejected, wrong)
        self.assertEqual(rejected_source, "lsl_metadata")

    def test_failed_visit_can_retry_same_participant_without_manual_visit_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            failed_report = {
                "status": "fail",
                "failures": ["synthetic preflight failure"],
                "warnings": [],
                "channel_contract": {"status": "fail"},
                "report_file": None,
                "electrode_quality_file": None,
            }
            with patch(
                "eegle.pipelines.study1.run_recording_preflight",
                return_value=failed_report,
            ):
                failed = run_study1_visit(
                    Study1Options(
                        config_path=CONFIG,
                        participant_id="retry-study1",
                        visit_number=1,
                        visit_id="retry-visit-1",
                        task_mode="dry-run",
                        no_go_digit=3,
                        smoke=True,
                        baseline_seconds=0.0,
                        record_eeg=False,
                        require_eeg=False,
                        output_root=tmp,
                    )
                )

            retried = run_study1_visit(
                Study1Options(
                    config_path=CONFIG,
                    participant_id="retry-study1",
                    visit_number=1,
                    task_mode="dry-run",
                    no_go_digit=3,
                    smoke=True,
                    baseline_seconds=0.0,
                    record_eeg=False,
                    require_eeg=False,
                    retry_incomplete=True,
                    output_root=tmp,
                )
            )

            manifest = json.loads(Path(retried["manifest_file"]).read_text(encoding="utf-8"))

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["failed_phase"], "preflight")
        self.assertIn("synthetic preflight failure", failed["failure_detail"])
        self.assertIn("rerun the same Windows operator command", failed["next_action"])
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(retried["visit_id"], "retry-visit-1")
        self.assertEqual(retried["failures"], [])
        self.assertEqual(len(retried["resolved_failures"]), 1)
        self.assertEqual(retried["resolved_failures"][0]["phase"], "preflight")
        self.assertEqual(
            retried["resolved_failures"][0]["resolution"],
            "visit_completed_after_retry",
        )
        self.assertEqual(manifest["failures"], [])
        self.assertEqual(len(manifest["resolved_failures"]), 1)
        self.assertEqual(len(manifest["phases"]["preflight"]["attempts"]), 2)

    def test_skip_baseline_creates_no_baseline_session_and_honors_task_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_study1_visit(
                Study1Options(
                    config_path=CONFIG,
                    participant_id="skip-baseline-study1",
                    visit_number=1,
                    visit_id="skip-baseline-visit",
                    task_mode="dry-run",
                    no_go_digit=3,
                    smoke=True,
                    include_practice=True,
                    trials=20,
                    practice_trials=12,
                    practice_no_go_trials=1,
                    practice_max_rounds=1,
                    skip_baseline=True,
                    record_eeg=False,
                    require_eeg=False,
                    output_root=tmp,
                )
            )
            manifest = json.loads(Path(result["manifest_file"]).read_text(encoding="utf-8"))
            baseline_attempt = manifest["phases"]["baseline"]["attempts"][0]

        self.assertEqual(result["status"], "completed")
        self.assertNotIn("baseline", result["session_directories"])
        self.assertEqual(baseline_attempt["status"], "completed")
        self.assertEqual(baseline_attempt["result"]["status"], "skipped")
        self.assertIsNone(baseline_attempt["result"]["session_dir"])
        self.assertEqual(manifest["task_overrides"]["experimental_trials"], 20)
        self.assertEqual(manifest["task_overrides"]["practice_trials_per_round"], 12)
        self.assertEqual(
            manifest["phases"]["session1_main"]["attempts"][0]["result"]["task_summary"][
                "experimental_trials"
            ],
            20,
        )

    def test_resume_after_completed_baseline_does_not_run_baseline_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            partial_dir = Path(tmp) / "retained-partial-task"
            partial_dir.mkdir()
            failed_task = {
                "status": "partial",
                "session_dir": str(partial_dir),
                "error": "synthetic task interruption",
            }
            initial_options = Study1Options(
                config_path=CONFIG,
                participant_id="resume-after-baseline",
                visit_number=1,
                visit_id="resume-after-baseline-visit",
                task_mode="dry-run",
                no_go_digit=3,
                smoke=True,
                trials=20,
                baseline_seconds=0.0,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )
            with patch(
                "eegle.pipelines.study1.run_dsart_child_session",
                return_value=failed_task,
            ):
                first = run_study1_visit(initial_options)

            with patch(
                "eegle.pipelines.study1.run_resting_baseline",
                side_effect=AssertionError("completed baseline must not run on resume"),
            ):
                resumed = run_study1_visit(
                    Study1Options(
                        **{
                            **initial_options.__dict__,
                            "visit_id": None,
                            "trials": 30,
                            "baseline_seconds": 12.0,
                            "retry_incomplete": True,
                        }
                    )
                )
            manifest = json.loads(Path(resumed["manifest_file"]).read_text(encoding="utf-8"))

        self.assertEqual(first["status"], "partial")
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(len(manifest["phases"]["baseline"]["attempts"]), 1)
        self.assertEqual(len(manifest["phases"]["session1_main"]["attempts"]), 2)
        self.assertEqual(
            manifest["phases"]["session1_main"]["attempts"][1]["result"]["task_summary"][
                "experimental_trials"
            ],
            20,
        )
        self.assertEqual(
            manifest["session_directories"]["baseline"],
            first["session_directories"]["baseline"],
        )

    def test_resume_reaccepts_completed_baseline_without_reacquisition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            initial_options = Study1Options(
                config_path=CONFIG,
                participant_id="resume-baseline-acceptance",
                visit_number=1,
                visit_id="resume-baseline-acceptance-visit",
                task_mode="dry-run",
                no_go_digit=3,
                smoke=True,
                trials=20,
                baseline_seconds=0.0,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )

            def reject_baseline(result, _options, phase):
                if phase == "Study 1 baseline":
                    result["warnings"] = ["synthetic retained baseline warning"]
                    result["operator_warning_acceptance"] = {"accepted": False}
                    raise RuntimeError(
                        "Study 1 baseline warnings were not accepted; raw recording was retained"
                    )

            with patch(
                "eegle.pipelines.study1._accept_post_recording_warnings",
                side_effect=reject_baseline,
            ):
                first = run_study1_visit(initial_options)

            with patch(
                "eegle.pipelines.study1.run_resting_baseline",
                side_effect=AssertionError("completed baseline must not be reacquired"),
            ):
                resumed = run_study1_visit(
                    Study1Options(
                        **{
                            **initial_options.__dict__,
                            "visit_id": None,
                            "retry_incomplete": True,
                        }
                    )
                )
            manifest = json.loads(Path(resumed["manifest_file"]).read_text(encoding="utf-8"))

        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["failed_phase"], "baseline")
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(len(manifest["phases"]["baseline"]["attempts"]), 1)
        self.assertTrue(
            manifest["phases"]["baseline"]["attempts"][0][
                "acceptance_recovered_without_reacquisition"
            ]
        )
        self.assertEqual(
            manifest["resumed_phases"][-1]["action"],
            "post_recording_warning_acceptance_recovered_without_reacquisition",
        )

    def test_resume_reaccepts_completed_task_without_reacquisition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            initial_options = Study1Options(
                config_path=CONFIG,
                participant_id="resume-task-acceptance",
                visit_number=1,
                visit_id="resume-task-acceptance-visit",
                task_mode="dry-run",
                no_go_digit=3,
                smoke=True,
                trials=20,
                baseline_seconds=0.0,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )

            def reject_task(result, _options, phase):
                if phase == "session1_main":
                    result["warnings"] = ["synthetic retained task warning"]
                    result["operator_warning_acceptance"] = {"accepted": False}
                    raise RuntimeError(
                        "session1_main warnings were not accepted; raw recording was retained"
                    )

            with patch(
                "eegle.pipelines.study1._accept_post_recording_warnings",
                side_effect=reject_task,
            ):
                first = run_study1_visit(initial_options)
            retained = first["retained_session_directory"]

            with patch(
                "eegle.pipelines.study1.run_dsart_child_session",
                side_effect=AssertionError("completed task must not be reacquired"),
            ):
                resumed = run_study1_visit(
                    Study1Options(
                        **{
                            **initial_options.__dict__,
                            "visit_id": None,
                            "retry_incomplete": True,
                        }
                    )
                )
            manifest = json.loads(Path(resumed["manifest_file"]).read_text(encoding="utf-8"))

        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["failed_phase"], "session1_main")
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["session_directories"]["session1_main"], retained)
        self.assertEqual(len(manifest["phases"]["session1_main"]["attempts"]), 1)
        self.assertTrue(
            manifest["phases"]["session1_main"]["attempts"][0][
                "acceptance_recovered_without_reacquisition"
            ]
        )

    def test_resume_target_finds_visit_id_run_name_and_exact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            visit_dir = root / "study1" / "participant-a" / "visits" / "visit-1" / "visit-alpha"
            session_dir = root / "participants" / "participant-a" / "sessions" / "date" / "task" / "run-alpha"
            visit_dir.mkdir(parents=True)
            session_dir.mkdir(parents=True)
            manifest_path = visit_dir / "visit_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": VISIT_SCHEMA,
                        "status": "partial",
                        "participant_id": "participant-a",
                        "visit_number": 1,
                        "visit_id": "visit-alpha",
                        "session_directories": {"session1_main": str(session_dir)},
                    }
                ),
                encoding="utf-8",
            )

            selections = [
                _find_resume_manifest("visit-alpha", config_path=CONFIG, output_root=root),
                _find_resume_manifest("run-alpha", config_path=CONFIG, output_root=root),
                _find_resume_manifest(str(visit_dir), config_path=CONFIG, output_root=None),
                _find_resume_manifest(str(manifest_path), config_path=CONFIG, output_root=None),
                _find_resume_manifest(str(session_dir), config_path=CONFIG, output_root=None),
            ]
            parsed = build_parser().parse_args(
                ["--resume", "--resume-target", str(session_dir)]
            )
            resolved_options = _options_from_args(parsed)

        self.assertTrue(
            all(Path(row["manifest_path"]).resolve() == manifest_path.resolve() for row in selections)
        )
        self.assertTrue(all(row["manifest"]["participant_id"] == "participant-a" for row in selections))
        self.assertTrue(all(Path(row["output_root"]).resolve() == root.resolve() for row in selections))
        self.assertEqual(resolved_options.participant_id, "participant-a")
        self.assertEqual(resolved_options.visit_number, 1)
        self.assertEqual(resolved_options.visit_id, "visit-alpha")
        self.assertEqual(Path(resolved_options.output_root).resolve(), root.resolve())

    def test_participant_resume_selects_most_recent_incomplete_visit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            participant_dir = Path(tmp) / "study1" / "participant-a"
            older = participant_dir / "visits" / "visit-1" / "older" / "visit_manifest.json"
            newer = participant_dir / "visits" / "visit-1" / "newer" / "visit_manifest.json"
            for path, visit_id in ((older, "older"), (newer, "newer")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "schema": VISIT_SCHEMA,
                            "status": "partial",
                            "participant_id": "participant-a",
                            "visit_number": 1,
                            "visit_id": visit_id,
                        }
                    ),
                    encoding="utf-8",
                )
            os.utime(older, (100.0, 100.0))
            os.utime(newer, (200.0, 200.0))
            selected = _resolve_visit_id(
                participant_dir,
                {"visits": {"1": {"status": "partial", "visit_id": "newer"}}},
                Study1Options(
                    config_path=CONFIG,
                    participant_id="participant-a",
                    visit_number=1,
                    resume=True,
                ),
            )

        self.assertEqual(selected, "newer")

    def test_main_reports_and_returns_process_exit_code_from_final_status(self) -> None:
        completed = {"status": "completed", "participant_id": "unit"}
        failed = {"status": "failed", "participant_id": "unit"}

        with tempfile.TemporaryDirectory() as tmp:
            for result, expected in ((completed, 0), (failed, 1)):
                outcome_file = Path(tmp) / f"{result['status']}.json"
                argv = [
                    "--participant",
                    "unit",
                    "--visit",
                    "1",
                    "--result-file",
                    str(outcome_file),
                ]
                output = io.StringIO()
                with self.subTest(status=result["status"]), patch(
                    "eegle.pipelines.study1.run_study1_visit",
                    return_value=copy.deepcopy(result),
                ), redirect_stdout(output):
                    exit_code = main(argv)
                printed = json.loads(output.getvalue())
                persisted = json.loads(outcome_file.read_text(encoding="utf-8"))
                self.assertEqual(exit_code, expected)
                self.assertEqual(printed["process_exit_code"], expected)
                self.assertEqual(persisted, printed)
                self.assertEqual(persisted["status"], result["status"])
                self.assertEqual(persisted["outcome_file"], str(outcome_file.resolve()))
                if expected:
                    self.assertTrue(persisted["failure_detail"])
                    self.assertEqual(persisted["failed_phase"], "startup_or_orchestration")

    def test_main_outcome_write_failure_does_not_change_scientific_status(self) -> None:
        output = io.StringIO()
        with patch(
            "eegle.pipelines.study1.run_study1_visit",
            return_value={"status": "completed", "participant_id": "unit"},
        ), patch(
            "eegle.pipelines.study1._write_json_atomic",
            side_effect=PermissionError("synthetic outcome write failure"),
        ), redirect_stdout(output):
            exit_code = main(
                [
                    "--participant",
                    "unit",
                    "--visit",
                    "1",
                    "--result-file",
                    "unwritable-outcome.json",
                ]
            )

        printed = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(printed["status"], "completed")
        self.assertEqual(printed["process_exit_code"], 0)
        self.assertIn("PermissionError", printed["outcome_file_error"])

    def test_study1_phase_contract_rejects_contradictory_success_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            manifest = {
                "task_mode": "psychopy",
                "include_practice": False,
                "prepared_sequence_hashes": {"session1_main": "prepared-sequence"},
            }
            valid = {
                "status": "completed",
                "session_dir": str(session_dir),
                "validation": {"status": "pass", "failures": [], "warnings": []},
                "forward": {"status": "complete"},
                "sequence_hash": "prepared-sequence",
                "phase_worker": {"return_code": 0},
            }
            self.assertEqual(
                _study1_phase_result_failures("session1_main", valid, manifest),
                [],
            )

            practice_manifest = copy.deepcopy(manifest)
            practice_manifest["include_practice"] = True
            proceeded = copy.deepcopy(valid)
            proceeded["practice_status"] = "proceeded_without_passing"
            self.assertEqual(
                _study1_phase_result_failures("session1_main", proceeded, practice_manifest),
                [],
            )

            not_passed = copy.deepcopy(valid)
            not_passed["practice_status"] = "not_passed"
            self.assertTrue(
                _study1_phase_result_failures("session1_main", not_passed, practice_manifest)
            )

            contradictions = {
                "recorder failure": {"forward": {"status": "failed"}},
                "sequence mismatch": {"sequence_hash": "different-sequence"},
                "worker crash": {"phase_worker": {"return_code": 1}},
                "missing validation": {"validation": {}},
            }
            for label, replacement in contradictions.items():
                with self.subTest(label=label):
                    result = copy.deepcopy(valid)
                    result.update(replacement)
                    self.assertTrue(
                        _study1_phase_result_failures("session1_main", result, manifest)
                    )

    def test_retry_incomplete_does_not_overwrite_completed_visit(self) -> None:
        participant = {
            "visits": {"1": {"status": "completed", "visit_id": "completed-v1"}}
        }
        with self.assertRaisesRegex(FileExistsError, "already completed"):
            _validate_visit_slot(
                participant,
                Study1Options(
                    config_path=CONFIG,
                    participant_id="unit",
                    visit_number=1,
                    retry_incomplete=True,
                ),
            )

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
            visit_one_manifest = json.loads(
                Path(visit_one["manifest_file"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                visit_one_manifest["prepared_sequence_hashes"]["session1_main"],
                visit_one["sequence_hashes"]["session1_main"],
            )
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
