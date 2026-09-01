from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eegle.io.events import EventLogger
from eegle.pipelines.dsart_recording import (
    _electrode_report,
    _normalize_electrode_quality_status,
    _partition_xdf_failures,
)
from eegle.pipelines.study1 import _study1_participant_directory
from eegle.pipelines.study1_validation import (
    ValidationOptions,
    main,
    validate_study1_data,
)
from eegle.quality import (
    collect_recording_quality_issues,
    electrode_quality_issues,
    publish_recording_quality_report,
)
from eegle.session import create_session, participant_storage_component


class Study1ValidationTests(unittest.TestCase):
    def _visit_fixture(self, root: Path, participant: str = "Jane Doe") -> tuple[Path, Path]:
        visit = root / "study1" / "Jane-Doe" / "visits" / "visit-1" / "visit-a"
        session = (
            root
            / "participants"
            / participant
            / "sessions"
            / "2026-08-27"
            / "study1"
            / "dynamic_sart"
            / "run-a"
        )
        (visit / "reports").mkdir(parents=True)
        (visit / "preflight").mkdir()
        (session / "raw").mkdir(parents=True)
        (session / "events").mkdir(parents=True)
        (session / "reports").mkdir(parents=True)
        (session / "logs" / "processes").mkdir(parents=True)
        (visit.parents[2] / "participant_manifest.json").write_text(
            json.dumps({"participant_id": participant}),
            encoding="utf-8",
        )
        (session / "manifest.json").write_text(
            json.dumps({"participant_id": participant, "task": "dynamic_sart"}),
            encoding="utf-8",
        )
        parameters = {
            "hardware": {
                "markers": {"source_id": "markers-a"},
                "eeg": {"expected_sample_rate_hz": 1000.0},
            },
            "tasks": {
                "dynamic_sart": {
                    "digits": list(range(10)),
                    "no_go_digit": 3,
                    "response_keys": ["space"],
                }
            },
        }
        (session / "parameters.json").write_text(json.dumps(parameters), encoding="utf-8")
        (session / "raw" / "recording.xdf").write_bytes(b"fixture-xdf")
        (session / "raw" / "xdf_metadata.json").write_text(
            json.dumps({"status": "stopped"}),
            encoding="utf-8",
        )
        (session / "logs" / "processes" / "recorder.status.json").write_text(
            json.dumps({"status": "stopped", "summary": {"status": "stopped"}}),
            encoding="utf-8",
        )
        with (session / "raw" / "lsl_markers_received.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["marker_label", "lsl_timestamp"])
            writer.writeheader()
            writer.writerow({"marker_label": "dynamic_sart_stimulus_onset", "lsl_timestamp": "1.0"})
        key = {
            "event_id": "key-00000001",
            "key": "space",
            "timestamp_monotonic": 1.3,
            "timestamp_lsl_if_available": 101.3,
            "is_response_key": True,
            "is_escape_key": False,
        }
        trial = {
            "schema": "eegle.dynamic_sart_trial.v3",
            "session_id": "run-a",
            "participant_id": participant,
            "global_trial_index": 1,
            "sequence_id": "sequence-a",
            "digit": 1,
            "condition": "go",
            "is_no_go": False,
            "expected_action": "press",
            "stimulus_onset_monotonic": 1.0,
            "stimulus_onset_lsl": 101.0,
            "stimulus_offset_monotonic": 1.25,
            "stimulus_offset_lsl": 101.25,
            "scheduled_response_window_close_monotonic": 2.6,
            "scheduled_response_window_close_lsl": 102.6,
            "response_window_close_monotonic": 2.6,
            "response_window_close_lsl": 102.6,
            "scheduled_next_trial_onset_monotonic": None,
            "time_on_task_seconds": 0.0,
            "all_key_event_ids": [key["event_id"]],
            "primary_outcome": "correct_go",
            "correct": True,
            "commission_error": False,
            "omission_error": False,
            "too_fast_response": False,
            "multiple_response": False,
            "wrong_key_response": False,
            "response_key_count": 1,
            "reaction_time_seconds": 0.3,
            "aborted": False,
            "invalid": False,
        }
        (session / "events" / "dynamic_sart_trials.jsonl").write_text(json.dumps(trial) + "\n")
        (session / "events" / "dynamic_sart_key_events.jsonl").write_text(json.dumps(key) + "\n")
        (session / "events" / "events.jsonl").write_text(
            json.dumps({"label": "dynamic_sart_stimulus_onset", "trial": 1}) + "\n"
        )
        stimulus = {
            "sequence_id": "sequence-a",
            "trials": [{"global_trial_index": 1, "presented": True}],
            "display_timing": {},
        }
        (session / "events" / "stimulus_manifest.json").write_text(json.dumps(stimulus))
        (session / "events" / "dynamic_sart_results.json").write_text(
            json.dumps({"summary": {"experimental_trials": 1}})
        )
        (session / "events" / "dynamic_sart_trials.csv").write_text("global_trial_index\n1\n")
        (session / "events" / "behavior.csv").write_text("trial,label\n1,onset\n")
        (session / "triggers.txt").write_text("onset EVENT 1.0\n")
        preflight_report = visit / "preflight" / "visit_1_preflight.json"
        electrode_report = visit / "preflight" / "visit_1_preflight_electrode_quality.json"
        preflight_report.write_text(
            json.dumps(
                {
                    "schema": "eegle.dsart_recording_preflight.v1",
                    "status": "pass",
                    "warnings": [],
                    "failures": [],
                    "quality_issues": [],
                }
            ),
            encoding="utf-8",
        )
        electrode_report.write_text(
            json.dumps(
                {
                    "schema": "eegle.dsart_electrode_quality.v1",
                    "channels": [{"channel_name": "Fz", "quality_status": "good"}],
                    "quality_issues": [],
                }
            ),
            encoding="utf-8",
        )
        manifest = {
            "schema": "eegle.study1.visit.v1",
            "status": "completed",
            "participant_id": participant,
            "visit_number": 1,
            "visit_id": "visit-a",
            "record_eeg": True,
            "task_mode": "psychopy",
            "phase_order": ["preflight", "session1_main"],
            "phases": {
                "preflight": {
                    "status": "completed",
                    "attempts": [
                        {
                            "status": "completed",
                            "result": {
                                "report_file": str(preflight_report),
                                "electrode_quality_file": str(electrode_report),
                            },
                        }
                    ],
                },
                "session1_main": {
                    "status": "completed",
                    "attempts": [
                        {
                            "status": "completed",
                            "result": {
                                "session_dir": str(session),
                                "sequence_hash": "sequence-a",
                            },
                        }
                    ],
                }
            },
            "session_directories": {"session1_main": str(session)},
        }
        manifest_path = visit / "visit_manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path, session

    def test_quick_validation_selects_participant_by_manifest_identity_and_saves_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, _session = self._visit_fixture(root)
            config = root / "config.json"
            config.write_text(json.dumps({"runtime": {"session_root": str(root)}}))
            with patch(
                "eegle.pipelines.study1_validation._resolve_xdf_headers",
                return_value=[{"type": "EEG"}, {"type": "Markers"}],
            ):
                report = validate_study1_data(
                    ValidationOptions(config_path=config, participant_id="Jane Doe", mode="quick")
                )
            report_path = Path(report["report_file"])
            self.assertEqual(report["status"], "ok", report)
            self.assertTrue(report_path.is_file())
            self.assertTrue(report_path.is_relative_to(manifest.parent / "reports"))
            self.assertEqual(report["canonical_inventory"], [])
            self.assertEqual(json.loads(report_path.read_text())["report_file"], str(report_path))

    def test_exact_child_target_resolves_visit_and_missing_core_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _manifest, session = self._visit_fixture(root)
            (session / "raw" / "recording.xdf").unlink()
            exit_code = main(["--target", str(session), "--mode", "quick"])
            self.assertEqual(exit_code, 2)

    def test_comprehensive_inventory_and_optional_backup_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            manifest, session = self._visit_fixture(root)
            manifest_before = manifest.read_bytes()
            xdf_before = (session / "raw" / "recording.xdf").read_bytes()
            xdf_validation = {
                "status": "warning",
                "failures": [],
                "warnings": ["momentary sampling gap"],
                "eeg": {
                    "sampling_gap_warning_count": 1,
                    "timestamp_issues": [
                        {
                            "code": "sampling_gap",
                            "sample_index": 10,
                            "previous_timestamp": 1.0,
                            "timestamp": 1.01,
                            "gap_seconds": 0.01,
                        }
                    ],
                    "estimated_missing_samples": 9,
                    "largest_timestamp_gap_seconds": 0.01,
                    "signal_quality": {"channels": []},
                },
            }
            marker = {
                "failures": [],
                "warnings": [],
                "stimulus_duration_warning_trials": [],
                "soi_warning_trials": [],
            }
            countdown = {"failures": [], "warnings": []}
            with patch(
                "eegle.pipelines.study1_validation._resolve_xdf_headers",
                return_value=[{"type": "EEG"}, {"type": "Markers"}],
            ), patch(
                "eegle.pipelines.study1_validation.validate_xdf_recording",
                return_value=xdf_validation,
            ), patch(
                "eegle.pipelines.study1_validation._task_marker_integrity",
                return_value=marker,
            ), patch(
                "eegle.pipelines.study1_validation._countdown_event_integrity",
                return_value=countdown,
            ):
                first = validate_study1_data(
                    ValidationOptions(config_path="unused", target=session, mode="comprehensive")
                )
                backup = Path(tmp) / "backup"
                for artifact in first["canonical_inventory"]:
                    source = Path(artifact["absolute_path"])
                    destination = backup / artifact["relative_path"]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                second = validate_study1_data(
                    ValidationOptions(
                        config_path="unused",
                        target=session,
                        mode="comprehensive",
                        backup_root=backup,
                    )
                )
            self.assertTrue(first["canonical_inventory"])
            self.assertTrue(all(row["sha256"] for row in first["canonical_inventory"]))
            roles = {row["role"] for row in first["canonical_inventory"]}
            self.assertIn("preflight_report", roles)
            self.assertIn("electrode_quality_report", roles)
            self.assertIn("recorder_process_status", roles)
            self.assertEqual(second["backup_verification"]["status"], "match")
            self.assertTrue(any(row["code"] == "timestamp.sampling_gap" for row in first["quality_issues"]))
            self.assertNotEqual(first["report_file"], second["report_file"])
            self.assertEqual(manifest.read_bytes(), manifest_before)
            self.assertEqual((session / "raw" / "recording.xdf").read_bytes(), xdf_before)
            self.assertFalse((session / "raw" / "eeg.csv").exists())

    def test_participant_storage_mapping_preserves_names_and_contains_dangerous_ids(self) -> None:
        self.assertEqual(participant_storage_component("Jane Doe"), "Jane Doe")
        self.assertEqual(participant_storage_component("José O'Neil"), "José O'Neil")
        for value in ("../escape", r"..\escape", r"C:\escape", r"\\server\share", "CON", "trailing."):
            with self.subTest(value=value):
                component = participant_storage_component(value)
                self.assertRegex(component, r"^participant-[0-9a-f]{20}$")
        with tempfile.TemporaryDirectory() as tmp:
            paths = create_session(
                {"experiment": {"experiment_id": "test", "task": "dynamic_sart"}},
                participant_id="../escape",
                root=tmp,
            )
            self.assertTrue(paths.root.is_relative_to(Path(tmp) / "participants"))
            manifest = json.loads(paths.manifest.read_text())
            self.assertEqual(manifest["participant_id"], "../escape")
            self.assertRegex(manifest["participant_storage_component"], r"^participant-")

    def test_study1_participant_directory_preserves_new_names_and_reuses_legacy_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                _study1_participant_directory(root, "Jane Doe"),
                root / "study1" / "Jane Doe",
            )
            dangerous = _study1_participant_directory(root, r"..\escape")
            self.assertTrue(dangerous.is_relative_to(root / "study1"))
            self.assertRegex(dangerous.name, r"^participant-[0-9a-f]{20}$")
            legacy = root / "study1" / "Jane-Doe"
            legacy.mkdir(parents=True)
            (legacy / "participant_manifest.json").write_text(
                json.dumps({"participant_id": "Jane Doe"}),
                encoding="utf-8",
            )
            self.assertEqual(_study1_participant_directory(root, "Jane Doe"), legacy)

    def test_fsync_permission_error_is_recorded_without_interrupting_event_logging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with EventLogger(root / "behavior.csv", root / "events.jsonl", root / "triggers.txt") as logger:
                logger.mark("test")
                with patch("eegle.io.events.os.fsync", side_effect=PermissionError("policy")):
                    logger.flush(durable=True)
                self.assertEqual(len(logger.durability_warnings), 3)

    def test_event_writes_do_not_fsync_until_a_durable_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("eegle.io.events.os.fsync") as fsync:
                logger = EventLogger(
                    root / "behavior.csv",
                    root / "events.jsonl",
                    root / "triggers.txt",
                    flush_each_event=False,
                )
                logger.mark("test")
                self.assertEqual(fsync.call_count, 0)
                logger.flush(durable=True)
                self.assertEqual(fsync.call_count, 3)
                logger.close()
                self.assertEqual(fsync.call_count, 6)

    def test_malformed_canonical_ledger_is_core_unreadable_but_visit_can_be_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, session = self._visit_fixture(root)
            payload = json.loads(manifest.read_text())
            payload["status"] = "incomplete"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            (session / "events" / "dynamic_sart_trials.jsonl").write_text("{truncated\n")
            with patch(
                "eegle.pipelines.study1_validation._resolve_xdf_headers",
                return_value=[{"type": "EEG"}, {"type": "Markers"}],
            ):
                report = validate_study1_data(
                    ValidationOptions(config_path="unused", target=session, mode="quick")
                )
            self.assertEqual(report["visit_status"], "incomplete")
            self.assertEqual(report["status"], "unreadable")
            self.assertTrue(any("trial_ledger is unreadable" in row for row in report["core_errors"]))

    def test_electrode_problem_and_unknown_statuses_are_warnings(self) -> None:
        self.assertEqual(_normalize_electrode_quality_status("failed")[:2], ("warning", "reported_problem"))
        self.assertEqual(_normalize_electrode_quality_status("vendor-special"), ("warning", "unrecognized_status", False))
        with tempfile.TemporaryDirectory() as tmp:
            supplied = Path(tmp) / "electrodes.json"
            supplied.write_text(
                json.dumps({"channels": {"Fz": {"status": "vendor-special", "value": 42}}}),
                encoding="utf-8",
            )
            report = _electrode_report(
                ["Fz"],
                {
                    "quality": {
                        "signal_units": "uV",
                        "channels": [
                            {
                                "channel_name": "Fz",
                                "status": "good",
                                "finite_sample_fraction": 1.0,
                                "standard_deviation_native_units": 2.5,
                            }
                        ],
                    }
                },
                recipe="study1",
                quality_file=supplied,
                operator_note="reviewed",
                operator_confirmed=True,
            )
            issue = electrode_quality_issues(report, phase="preflight")[0]
            self.assertEqual(issue["code"], "electrode.unrecognized_status")
            self.assertEqual(issue["channels"], ["Fz"])
            self.assertFalse(issue["observed"]["recognized"])
            self.assertEqual(issue["observed"]["raw_supplied_status"], "vendor-special")
            self.assertEqual(issue["observed"]["standard_deviation_native_units"], 2.5)

    def test_quality_sidecar_records_exact_channels_trials_and_timestamp_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "reports").mkdir()
            (session / "events").mkdir()
            event_ledger = session / "events" / "events.jsonl"
            event_ledger.write_text('{"label":"task_end"}\n', encoding="utf-8")
            (session / "events" / "dynamic_sart_trials.jsonl").write_text(
                json.dumps(
                    {
                        "global_trial_index": 12,
                        "stimulus_onset_lsl": 1.8,
                        "scheduled_response_window_close_lsl": 2.1,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            event_ledger_before = event_ledger.read_bytes()
            result = {
                "session_dir": str(session),
                "validation": {
                    "raw_integrity": {
                        "xdf_integrity": {
                            "eeg": {
                                "nonmonotonic_timestamp_count": 1,
                                "timestamp_issues_truncated": True,
                                "timestamp_issues": [
                                    {
                                        "code": "nonmonotonic_timestamp",
                                        "sample_index": 22,
                                        "previous_timestamp": 2.0,
                                        "timestamp": 1.9,
                                    }
                                ],
                                "signal_quality": {
                                    "channels": [
                                        {
                                            "channel_name": "Fz",
                                            "status": "warning",
                                            "warnings": ["flatline_run"],
                                        }
                                    ]
                                },
                            }
                        }
                    },
                    "marker_integrity": {
                        "stimulus_duration_warning_trials": [4, 8],
                        "soi_warning_trials": [],
                    },
                },
            }
            issues = collect_recording_quality_issues(result, phase="session1_main")
            path = publish_recording_quality_report(result, phase="session1_main")
            self.assertIsNotNone(path)
            self.assertTrue(Path(path).is_file())
            by_code = {row["code"]: row for row in issues}
            self.assertEqual(by_code["channel.flatline_run"]["channels"], ["Fz"])
            self.assertEqual(by_code["trial.stimulus_duration_deviation"]["trial_indices"], [4, 8])
            self.assertEqual(by_code["timestamp.nonmonotonic"]["details"][0]["sample_index"], 22)
            self.assertTrue(by_code["timestamp.nonmonotonic"]["details_truncated"])
            published_by_code = {row["code"]: row for row in result["quality_issues"]}
            self.assertEqual(published_by_code["timestamp.nonmonotonic"]["trial_indices"], [12])
            self.assertEqual(published_by_code["channel.flatline_run"]["trial_indices"], [])
            self.assertEqual(event_ledger.read_bytes(), event_ledger_before)

    def test_xdf_quality_conditions_remain_precise_warning_only_statuses(self) -> None:
        result = {
            "validation": {
                "raw_integrity": {
                    "xdf_integrity": {
                        "eeg": {
                            "nonfinite_timestamp_count": 2,
                            "sampling_gap_warning_count": 1,
                            "estimated_missing_samples": 7,
                            "largest_timestamp_gap_seconds": 0.008,
                            "timestamp_issues": [
                                {"code": "nonfinite_timestamp", "sample_index": 3},
                                {"code": "nonfinite_timestamp", "sample_index": 4},
                                {"code": "sampling_gap", "sample_index": 9, "gap_seconds": 0.008},
                            ],
                            "finite_timestamp_count": 90,
                            "expected_sample_count": 100,
                            "sample_fraction_of_expected": 0.9,
                            "minimum_sample_fraction_warning": 0.98,
                            "effective_sample_rate_hz": 960.0,
                            "expected_sample_rate_hz": 1000.0,
                            "effective_rate_warning_tolerance_fraction": 0.02,
                            "signal_quality": {
                                "channels": [
                                    {
                                        "channel_name": "Cz",
                                        "status": "warning",
                                        "warnings": ["non_finite_samples", "possible_clipping"],
                                        "finite_sample_count": 98,
                                        "nonfinite_sample_count": 2,
                                        "finite_sample_fraction": 0.98,
                                        "extreme_repeat_count": 12,
                                        "extreme_repeat_fraction": 0.12,
                                    }
                                ]
                            },
                        }
                    }
                }
            }
        }
        issues = collect_recording_quality_issues(result, phase="session1_main")
        by_code = {row["code"]: row for row in issues}
        self.assertEqual(by_code["timestamp.nonfinite"]["count"], 2)
        self.assertEqual(by_code["timestamp.sampling_gap"]["observed"]["largest_gap_seconds"], 0.008)
        self.assertEqual(by_code["eeg.sample_retention_low"]["observed"]["sample_fraction"], 0.9)
        self.assertEqual(by_code["eeg.effective_sample_rate_out_of_range"]["observed"]["effective_sample_rate_hz"], 960.0)
        self.assertEqual(by_code["channel.non_finite_samples"]["channels"], ["Cz"])
        self.assertEqual(by_code["channel.non_finite_samples"]["count"], 2)
        self.assertEqual(by_code["channel.possible_clipping"]["count"], 12)
        self.assertTrue(all(row["severity"] == "warning" for row in issues))

    def test_xdf_quality_failures_are_review_warnings_but_unreadable_data_stays_blocking(self) -> None:
        blocking, review = _partition_xdf_failures(
            [
                "XDF EEG channel labels/order do not map to the configured physical device order",
                "XDF marker labels/order do not exactly match the independent LSL marker receipt",
                "authoritative XDF recording is missing",
                "XDF sample chunks could not be read: corrupt",
            ]
        )
        self.assertEqual(len(review), 2)
        self.assertEqual(len(blocking), 2)
        self.assertTrue(any("recording is missing" in row for row in blocking))


if __name__ == "__main__":
    unittest.main()
