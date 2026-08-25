from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

from eegle.config import load_config
from eegle.cli import cmd_run_forward, cmd_run_task
from eegle.devices.lsl_eeg import (
    LslEegRecorder,
    LslSampleHeartbeat,
    _eeg_inlet_processing_flags,
    _eeg_probe_quality,
    _recorded_eeg_row,
)
from eegle.devices.lsl_markers import LslMarkerReceiptRecorder
from eegle.eeg_csv import eeg_channel_columns
from eegle.hardware.profiles import mapped_channel_names
from eegle.hardware.system import CheckResult
from eegle.io.events import EventLogger
from eegle.experiment import ForwardExperimentResult
from eegle.lsl import LslMarkerOutlet
from eegle.pipelines.dsart_recording import (
    DSART8_CHANNELS,
    DsartRecordingOptions,
    _acquisition_config_sha256,
    _accept_post_recording_warnings,
    _accept_recording_preflight,
    _baseline_phase_aborted,
    _baseline_phase_result,
    _baseline_recording_validation,
    _child_session_validation,
    _close_resources,
    compare_preflights,
    _configure_practice_policy,
    _countdown_event_integrity,
    _marker_receipt_integrity,
    _run_dsart_child_session_inline,
    _run_dsart_child_session_isolated,
    _run_xdf_preflight_probe,
    _runtime_cache_root,
    _storage_check,
    _task_marker_integrity,
    _trial_soi_duration,
    _trial_stimulus_duration,
    _options_from_args,
    _psychopy_baseline_phase,
    _raw_eeg_integrity,
    _require_preflight_acquisition_config,
    _run_baseline_dry,
    _run_baseline_psychopy,
    _write_json_atomic,
    assess_sample_probe,
    build_parser,
    run_resting_baseline,
    run_recording_suite,
    run_dsart_child_session,
    run_inter_session_break,
    run_recording_preflight,
    validate_dynamic_sart_forward_result,
    validate_recording_config,
    write_dsart8_overlap_manifest,
)
from eegle.recording_health import RecorderHealthMonitor
from eegle.session import create_session
from eegle.storage_permissions import probe_recording_storage
from eegle.tasks.base import TaskRunResult
from eegle.tasks.dynamic_sart_schema import DynamicSartConfig
from eegle.tasks.dynamic_sart_sequence import build_dynamic_sart_plan, validate_dynamic_sart_plan
from eegle.workers.recorder import _manager_process_disappeared


ROOT = Path(__file__).resolve().parents[1]
CONFIG_8 = ROOT / "configs" / "record_dsart8.json"
CONFIG_32 = ROOT / "configs" / "record_dsart32.json"


class DsartRecordingTests(unittest.TestCase):
    @staticmethod
    def _passing_preflight(*_args, **kwargs):
        return {
            "schema": "test",
            "phase": kwargs["phase"],
            "status": "pass",
            "checks": [],
            "channel_contract": {"status": "ok"},
            "warnings": [],
        }

    def test_baseline_phase_uses_one_abort_field_and_reads_legacy_artifacts(self) -> None:
        phase = _baseline_phase_result("eyes_open", 2.0, 10.0, 12.0, 20.0, 22.0, "completed", False)
        self.assertIn("aborted", phase)
        self.assertNotIn("abort_status", phase)
        self.assertFalse(_baseline_phase_aborted(phase))
        self.assertTrue(_baseline_phase_aborted({"abort_status": True}))

    def test_task_timing_qc_prefers_flip_lsl_over_coarse_monotonic_durations(self) -> None:
        row = {
            "stimulus_onset_lsl": 20.0,
            "stimulus_offset_lsl": 20.25,
            "actual_stimulus_seconds": 0.265,
            "actual_trial_duration_lsl_seconds": 1.6,
            "actual_trial_duration_seconds": 1.625,
        }

        self.assertEqual(_trial_stimulus_duration(row), (0.25, "lsl_flip"))
        self.assertEqual(_trial_soi_duration(row), (1.6, "lsl_flip"))

    def test_recording_enabled_dry_baseline_emits_lsl_boundaries(self) -> None:
        outlet = MagicMock()
        outlet.name = "EEGleMarkers"
        outlet.stream_type = "Markers"
        outlet.source_id = "baseline-marker-source"
        receipt = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(CONFIG_8)
            config["recording_rehearsal"] = {
                "marker_discovery_settle_seconds": 0.0,
                "marker_receipt_drain_seconds": 0.0,
            }
            paths = create_session(config, task="dsart_baseline", participant_id="unit", root=Path(tmp))
            with patch(
                "eegle.pipelines.dsart_recording._make_marker_outlet",
                return_value=outlet,
            ), patch(
                "eegle.pipelines.dsart_recording._start_marker_receipt_recorder",
                return_value=receipt,
            ), patch(
                "eegle.pipelines.dsart_recording.lsl_local_clock",
                side_effect=[10.0, 10.1, 10.2, 10.3],
            ):
                result = _run_baseline_dry(
                    config,
                    paths,
                    MagicMock(),
                    0.0,
                    0.0,
                    record_eeg=True,
                )

        self.assertEqual(
            [(row["start_lsl_timestamp"], row["end_lsl_timestamp"]) for row in result["phases"]],
            [(10.0, 10.1), (10.2, 10.3)],
        )
        self.assertEqual(outlet.push.call_count, 4)
        receipt.close.assert_called_once()
        outlet.close.assert_called_once()

    def test_recording_dry_baseline_reuses_prestarted_marker_outlet(self) -> None:
        outlet = MagicMock()
        outlet.name = "EEGleMarkers"
        outlet.stream_type = "Markers"
        outlet.source_id = "prestarted-baseline-marker"
        receipt = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(CONFIG_8)
            config["recording_rehearsal"] = {
                "marker_discovery_settle_seconds": 0.0,
                "marker_receipt_drain_seconds": 0.0,
            }
            paths = create_session(config, task="dsart_baseline", participant_id="unit", root=Path(tmp))
            with patch("eegle.pipelines.dsart_recording._make_marker_outlet") as make_outlet, patch(
                "eegle.pipelines.dsart_recording._start_marker_receipt_recorder",
                return_value=receipt,
            ):
                _run_baseline_dry(
                    config,
                    paths,
                    MagicMock(),
                    0.0,
                    0.0,
                    record_eeg=True,
                    marker_outlet=outlet,
                )

        make_outlet.assert_not_called()
        receipt.close.assert_called_once()
        outlet.close.assert_not_called()

    def test_successful_psychopy_baseline_accepts_completed_phase_results(self) -> None:
        closed = []

        class Window:
            waitBlanking = True
            timestamp = 100.0

            def flip(self) -> float:
                self.timestamp += 1.0 / 60.0
                return self.timestamp

            def close(self) -> None:
                closed.append("window")

        psychopy = ModuleType("psychopy")
        psychopy.event = SimpleNamespace()
        psychopy.visual = SimpleNamespace(Window=lambda **_kwargs: Window())
        outlet = MagicMock()
        outlet.close.side_effect = lambda: closed.append("outlet")
        phases = [
            _baseline_phase_result("eyes_open", 2.0, 10.0, 12.0, None, None, "completed", False),
            _baseline_phase_result("eyes_closed", 2.0, 12.0, 14.0, None, None, "completed", False),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(CONFIG_8)
            config["runtime"]["session_root"] = tmp
            paths = create_session(config, task="dsart_baseline", participant_id="unit")
            with patch.dict(sys.modules, {"psychopy": psychopy}), patch(
                "eegle.pipelines.dsart_recording.prepare_psychopy_runtime"
            ), patch(
                "eegle.pipelines.dsart_recording.clear_psychopy_keys"
            ), patch(
                "eegle.pipelines.dsart_recording._make_marker_outlet", return_value=outlet
            ), patch(
                "eegle.pipelines.dsart_recording._baseline_instruction", return_value=True
            ), patch(
                "eegle.pipelines.dsart_recording._psychopy_baseline_phase", side_effect=phases
            ), patch(
                "eegle.pipelines.dsart_recording._play_baseline_end_signal"
            ):
                result = _run_baseline_psychopy(config, paths, MagicMock(), 2.0, 2.0)

        self.assertEqual(result["status"], "completed")
        self.assertEqual([row["phase"] for row in result["phases"]], ["eyes_open", "eyes_closed"])
        self.assertEqual(closed, ["outlet", "window"])

    def test_resource_cleanup_attempts_every_resource_and_reports_failures(self) -> None:
        closed = []

        class Resource:
            def __init__(self, name: str, fail: bool = False) -> None:
                self.name = name
                self.fail = fail

            def close(self) -> None:
                closed.append(self.name)
                if self.fail:
                    raise OSError("simulated close failure")

        warnings = _close_resources(
            ("first", Resource("first", fail=True)),
            ("second", Resource("second")),
        )
        self.assertEqual(closed, ["first", "second"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("simulated close failure", warnings[0])

    def test_baseline_window_initialization_failure_is_structured_without_cleanup_crash(self) -> None:
        psychopy = ModuleType("psychopy")
        psychopy.event = SimpleNamespace()
        psychopy.visual = SimpleNamespace(Window=MagicMock(side_effect=AttributeError("native window failure")))
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(CONFIG_8)
            config["runtime"]["session_root"] = tmp
            paths = create_session(config, task="dsart_baseline", participant_id="unit-window-failure")
            with patch.dict(sys.modules, {"psychopy": psychopy}), patch(
                "eegle.pipelines.dsart_recording.prepare_psychopy_runtime"
            ):
                result = _run_baseline_psychopy(config, paths, MagicMock(), 2.0, 2.0)
        self.assertEqual(result["status"], "failed")
        self.assertIn("native window failure", result["error"])
        self.assertNotIn("cleanup", result.get("error", "").lower())

    def test_static_baseline_fixation_is_flipped_once_while_keys_are_polled(self) -> None:
        callbacks = []

        class Window:
            def __init__(self) -> None:
                self.flip_count = 0

            def callOnFlip(self, callback, *args) -> None:
                callbacks.append((callback, args))

            def flip(self) -> None:
                self.flip_count += 1
                while callbacks:
                    callback, args = callbacks.pop(0)
                    callback(*args)

        window = Window()
        visual = SimpleNamespace(TextStim=lambda *_args, **_kwargs: SimpleNamespace(draw=lambda: None))
        logger = MagicMock()
        with patch("eegle.pipelines.dsart_recording.monotonic", side_effect=[10.0, 10.1, 10.1, 10.3, 10.3]), patch(
            "eegle.pipelines.dsart_recording.lsl_local_clock", return_value=20.0
        ), patch("eegle.pipelines.dsart_recording.poll_psychopy_keys", return_value=[]), patch(
            "eegle.pipelines.dsart_recording.sleep"
        ):
            result = _psychopy_baseline_phase(
                window,
                visual,
                SimpleNamespace(),
                logger,
                MagicMock(),
                name="eyes_open",
                duration=0.2,
                draw_fixation=True,
            )
        self.assertEqual(window.flip_count, 1)
        self.assertEqual(result["completion_status"], "completed")
        self.assertTrue(logger.mark.call_args_list[0].kwargs["scheduled_on_flip"])
        self.assertEqual(logger.mark.call_args_list[0].kwargs["lsl_timestamp"], 20.0)

    def test_baseline_health_failure_preserves_the_actual_recorder_reason(self) -> None:
        callbacks = []

        class Window:
            def callOnFlip(self, callback, *args) -> None:
                callbacks.append((callback, args))

            def flip(self) -> None:
                while callbacks:
                    callback, args = callbacks.pop(0)
                    callback(*args)

        monitor = SimpleNamespace(
            check=lambda: SimpleNamespace(
                ok=False,
                warning=False,
                reason="LabRecorder process exited with code 3",
                status={"status": "failed"},
            )
        )
        with patch(
            "eegle.pipelines.dsart_recording.monotonic",
            side_effect=[10.0, 10.1, 10.1, 10.15],
        ), patch(
            "eegle.pipelines.dsart_recording.lsl_local_clock",
            return_value=20.0,
        ), patch("eegle.pipelines.dsart_recording.sleep"):
            result = _psychopy_baseline_phase(
                Window(),
                SimpleNamespace(TextStim=lambda *_args, **_kwargs: SimpleNamespace(draw=lambda: None)),
                SimpleNamespace(),
                MagicMock(),
                MagicMock(),
                name="eyes_open",
                duration=0.2,
                draw_fixation=True,
                recorder_monitor=monitor,
            )

        self.assertTrue(result["aborted"])
        self.assertEqual(
            result["abort_reason"],
            "recorder_health_failure: LabRecorder process exited with code 3",
        )

    def test_inter_session_break_closes_marker_outlet_on_operator_interrupt(self) -> None:
        class Outlet:
            closed = False

            def __init__(self, *_args, **_kwargs) -> None:
                self.source_id = "unit-break-source"

            def push(self, *_args, **_kwargs) -> None:
                return None

            def close(self) -> None:
                Outlet.closed = True

        with tempfile.TemporaryDirectory() as tmp:
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit-break",
                task_mode="psychopy",
                record_eeg=True,
                require_eeg=True,
            )
            with patch("eegle.pipelines.dsart_recording.LslMarkerOutlet", Outlet), patch(
                "eegle.pipelines.dsart_recording.lsl_local_clock", return_value=20.0
            ), patch("eegle.pipelines.dsart_recording.sleep", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    run_inter_session_break(
                        load_config(CONFIG_8),
                        options,
                        visit_id="visit-unit-break",
                        visit_dir=Path(tmp),
                        break_seconds=10.0,
                    )
            rows = [json.loads(line) for line in (Path(tmp) / "recording_suite_events.jsonl").read_text().splitlines()]
        self.assertTrue(Outlet.closed)
        self.assertEqual(rows[-1]["label"], "dsart_inter_session_break_aborted")

    def test_optional_telemetry_failure_does_not_undo_flushed_behavior_event(self) -> None:
        class BrokenTelemetry:
            def emit(self, *_args, **_kwargs) -> None:
                raise OSError("simulated telemetry failure")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with EventLogger(
                root / "behavior.csv",
                root / "events.jsonl",
                root / "triggers.txt",
                BrokenTelemetry(),
                "test",
            ) as logger:
                logger.mark("retained_event", event_type="SYSTEM", timestamp=1.0)
                self.assertIn("simulated telemetry failure", logger.telemetry_error or "")
            event_rows = (root / "events.jsonl").read_text(encoding="utf-8")
            trigger_rows = (root / "triggers.txt").read_text(encoding="utf-8")

        self.assertIn("retained_event", event_rows)
        self.assertIn("retained_event", trigger_rows)

    def test_suite_structures_baseline_implementation_exception_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit",
                visit_id="visit-baseline-exception",
                task_mode="dry-run",
                trials_per_session=10,
                baseline_seconds=2.0,
                break_seconds=0.0,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )
            with patch(
                "eegle.pipelines.dsart_recording.run_recording_preflight",
                side_effect=self._passing_preflight,
            ), patch(
                "eegle.pipelines.dsart_recording.run_resting_baseline",
                side_effect=KeyError("aborted"),
            ):
                result = run_recording_suite(options)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["aborts"][0]["phase"], "baseline")
        self.assertEqual(result["aborts"][0]["failure_kind"], "phase_failure")
        self.assertIn("KeyError", result["aborts"][0]["error"])

    def test_suite_structures_terminal_interrupt_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit",
                visit_id="visit-interrupt",
                task_mode="dry-run",
                trials_per_session=10,
                baseline_seconds=2.0,
                break_seconds=0.0,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )
            with patch(
                "eegle.pipelines.dsart_recording.run_recording_preflight",
                side_effect=KeyboardInterrupt,
            ):
                result = run_recording_suite(options)

        self.assertEqual(result["status"], "aborted")
        self.assertEqual(result["aborts"][0]["failure_kind"], "operator_interrupt")

    def test_suite_rejects_unknown_preflight_status_instead_of_advancing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit",
                visit_id="visit-unknown-preflight",
                task_mode="dry-run",
                trials_per_session=10,
                baseline_seconds=0.0,
                break_seconds=0.0,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )
            with patch(
                "eegle.pipelines.dsart_recording.run_recording_preflight",
                return_value={
                    "status": "unknown",
                    "phase": "initial_preflight",
                    "checks": [],
                    "failures": [],
                    "warnings": [],
                },
            ):
                result = run_recording_suite(options)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["aborts"][0]["phase"], "initial_preflight")

    def test_baseline_runtime_exception_retains_its_session_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(CONFIG_8)
            config["runtime"]["session_root"] = tmp
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit",
                visit_id="visit-structured-baseline",
                task_mode="dry-run",
                trials_per_session=10,
                baseline_seconds=2.0,
                break_seconds=0.0,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )
            manager = MagicMock()
            manager.eeg_summary = None
            manager.summary.return_value = {"status": "complete", "processes": {}, "notes": []}
            with patch("eegle.pipelines.dsart_recording.FeedbackManager", return_value=manager), patch(
                "eegle.pipelines.dsart_recording._run_baseline_protocol",
                side_effect=KeyError("simulated protocol defect"),
            ):
                result = run_resting_baseline(config, options, visit_id=options.visit_id or "visit", preflight={})

        self.assertEqual(result["status"], "failed")
        self.assertIn("simulated protocol defect", result["error"])
        self.assertTrue(result["session_dir"])
        manager.stop_after_task.assert_called_once()

    def test_smoke_cli_overrides_trials_baseline_and_break(self) -> None:
        args = build_parser().parse_args(
            [
                "--recipe", "dsart8",
                "--participant", "unit",
                "--task-mode", "psychopy",
                "--trials", "10",
                "--baseline-seconds", "2",
                "--break-seconds", "0",
                "--window-size", "900", "600",
                "--skip-eeg",
            ]
        )
        options = _options_from_args(args)
        self.assertEqual(options.trials_per_session, 10)
        self.assertEqual(options.baseline_seconds, 2.0)
        self.assertEqual(options.break_seconds, 0.0)
        self.assertEqual(options.window_size, (900, 600))
        self.assertFalse(options.include_practice)
        self.assertFalse(options.record_eeg)
        self.assertFalse(options.require_eeg)

    def test_suite_rejects_smoke_runs_shorter_than_task_sequence_contract(self) -> None:
        options = DsartRecordingOptions(
            recipe="dsart32",
            config_path=CONFIG_32,
            participant_id="unit-too-short",
            trials_per_session=9,
            record_eeg=False,
            require_eeg=False,
        )
        with self.assertRaisesRegex(ValueError, "at least 10"):
            run_recording_suite(options)

    def test_software_only_preflight_skips_all_acquisition_dependent_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "eegle.pipelines.dsart_recording.run_preflight",
            return_value=[CheckResult("display_ready", "ok", "ready")],
        ) as preflight, patch(
            "eegle.pipelines.dsart_recording.probe_psychopy_display_and_keyboard",
            return_value={"status": "measured", "window_opened": True, "keyboard_backend": "ptb"},
        ), patch(
            "eegle.pipelines.dsart_recording.prepare_psychopy_runtime",
        ), patch(
            "eegle.pipelines.dsart_recording._electrode_report"
        ) as electrode_report:
            report = run_recording_preflight(
                load_config(CONFIG_32),
                recipe="dsart32",
                participant_id="unit-no-eeg",
                visit_id="visit-no-eeg",
                phase="initial_preflight",
                output_dir=Path(tmp),
                require_eeg=False,
                record_eeg=False,
                lsl_wait_seconds=30.0,
                electrode_quality_file=None,
                electrode_note=None,
                electrodes_confirmed=False,
                initial_preflight=None,
                require_display=True,
            )

        preflight.assert_called_once_with(
            unittest.mock.ANY,
            lsl_wait=30.0,
            require_eeg=False,
            check_eeg=False,
        )
        electrode_report.assert_not_called()
        checks = {row["name"]: row for row in report["checks"]}
        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["channel_contract"]["status"], "skip")
        self.assertEqual(checks["dsart_sample_contract"]["status"], "skip")
        self.assertEqual(checks["electrode_quality"]["status"], "skip")
        self.assertEqual(checks["marker_loopback"]["status"], "skip")
        self.assertIsNone(report["electrode_quality_file"])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["acquisition_config_contract"], "hardware_and_recorder_v1")
        self.assertEqual(len(report["acquisition_config_sha256"]), 64)

    def test_preflight_acquisition_contract_detects_phase_config_drift(self) -> None:
        config = load_config(CONFIG_32)
        preflight = {"acquisition_config_sha256": _acquisition_config_sha256(config)}
        changed = copy.deepcopy(config)
        changed["hardware"]["display"]["screen_index"] = 2

        _require_preflight_acquisition_config(config, preflight, phase="baseline")
        with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
            _require_preflight_acquisition_config(changed, preflight, phase="task")

    def test_preflight_acquisition_contract_allows_additive_display_checker_upgrade(self) -> None:
        current = load_config(CONFIG_32)
        display = current["hardware"]["display"]
        display.update(
            {
                "refresh_rate_window_settle_seconds": 2.0,
                "refresh_rate_retry_settle_seconds": 1.0,
                "refresh_rate_warmup_frames": 20,
                "refresh_rate_sample_frames": 90,
            }
        )
        previous = copy.deepcopy(current)
        for name in (
            "refresh_rate_window_settle_seconds",
            "refresh_rate_retry_settle_seconds",
            "refresh_rate_warmup_frames",
            "refresh_rate_sample_frames",
        ):
            previous["hardware"]["display"].pop(name)
        preflight = {"acquisition_config_sha256": _acquisition_config_sha256(previous)}

        _require_preflight_acquisition_config(current, preflight, phase="task")

        changed = copy.deepcopy(current)
        changed["hardware"]["eeg"]["expected_sample_rate_hz"] = 123.0
        with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
            _require_preflight_acquisition_config(changed, preflight, phase="task")

    def test_preflight_acquisition_contract_allows_windowed_to_fullscreen_safety_upgrade(self) -> None:
        current = load_config(CONFIG_32)
        current["hardware"]["display"]["full_screen"] = True
        previous = copy.deepcopy(current)
        previous["hardware"]["display"]["full_screen"] = False
        preflight = {"acquisition_config_sha256": _acquisition_config_sha256(previous)}

        _require_preflight_acquisition_config(current, preflight, phase="task")

    def test_xdf_preflight_probe_records_and_surfaces_quality_warnings(self) -> None:
        class NativeOutlet:
            def push_sample(self, sample, timestamp=None) -> None:
                return None

        class Receipt:
            def wait_for_count(self, expected_count: int, *, timeout: float) -> bool:
                return True

            def stop(self) -> dict[str, object]:
                return {"status": "stopped", "received_count": 2}

        class Recorder:
            def start(self) -> dict[str, object]:
                return {"status": "recording"}

            def stop(self, *, reason: str) -> dict[str, object]:
                return {
                    "status": "stopped",
                    "csv_mirror_warning": "CSV mirror failed; XDF acquisition continued",
                }

        outlet = object.__new__(LslMarkerOutlet)
        outlet._outlet = NativeOutlet()
        outlet.name = "EEGleMarkers"
        outlet.stream_type = "Markers"
        outlet.source_id = "xdf-probe-markers"
        outlet.pushed_count = 0
        validation = {
            "status": "warning",
            "failures": [],
            "warnings": ["XDF measured EEG rate is 970.0 Hz; expected about 1000 Hz"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(CONFIG_32)
            config["processes"]["recorder"].update(
                {
                    "backend": "labrecorder_xdf",
                    "preflight_xdf_probe_seconds": 1.0,
                }
            )
            with patch(
                "eegle.pipelines.dsart_recording._make_marker_outlet",
                return_value=outlet,
            ), patch(
                "eegle.pipelines.dsart_recording._start_marker_receipt_recorder",
                return_value=Receipt(),
            ), patch(
                "eegle.pipelines.dsart_recording.LabRecorderXdfRecorder",
                return_value=Recorder(),
            ), patch(
                "eegle.pipelines.dsart_recording.validate_xdf_recording",
                return_value=validation,
            ), patch(
                "eegle.pipelines.dsart_recording.lsl_local_clock",
                side_effect=[1.0, 2.0],
            ), patch("eegle.pipelines.dsart_recording.sleep"):
                result = _run_xdf_preflight_probe(
                    config,
                    participant_id="unit-xdf-probe",
                    phase="initial_preflight",
                    output_dir=Path(tmp),
                )

        self.assertEqual(result.status, "warn")
        self.assertIn("measured EEG rate", result.detail)
        self.assertTrue(any("CSV mirror failed" in row for row in result.data["warnings"]))
        probe_session = Path(result.data["session_dir"])
        self.assertTrue(probe_session.name.startswith("run-"))
        self.assertIn("xdfp", probe_session.parts)
        self.assertNotIn("xdf_recording_probes", probe_session.parts)
        self.assertNotIn("xdf_preflight_initial_preflight", probe_session.parts)

    def test_preflight_comparison_returns_its_quality_result(self) -> None:
        initial = {
            "status": "pass",
            "eeg_probe": {
                "stream": {"source_id": "eeg-1", "nominal_srate": 1000.0},
                "quality": {"channels": []},
            },
            "channel_contract": {"expected_channel_order": ["E1"]},
        }
        comparison = compare_preflights(
            initial,
            {
                "stream": {"source_id": "eeg-1", "nominal_srate": 1000.0},
                "quality": {"channels": []},
            },
            {"status": "pass", "expected_channel_order": ["E1"]},
        )

        self.assertEqual(comparison["status"], "pass")
        self.assertEqual(comparison["sample_rate_difference_hz"], 0.0)

    def test_preflight_comparison_treats_sample_rate_change_as_warning(self) -> None:
        initial = {
            "eeg_probe": {
                "stream": {"source_id": "eeg-1", "nominal_srate": 1000.0},
                "quality": {"channels": []},
            },
            "channel_contract": {"expected_channel_order": ["E1"]},
        }
        comparison = compare_preflights(
            initial,
            {
                "stream": {"source_id": "eeg-1", "nominal_srate": 900.0},
                "quality": {"channels": []},
            },
            {"status": "pass", "expected_channel_order": ["E1"]},
        )

        self.assertEqual(comparison["status"], "warning")
        self.assertEqual(comparison["failures"], [])
        self.assertTrue(any("sample rate changed" in row for row in comparison["warnings"]))

    def test_session_root_cli_alias_maps_to_the_suite_output_root(self) -> None:
        args = build_parser().parse_args(
            ["--recipe", "dsart8", "--participant", "unit", "--session-root", "approved-data"]
        )
        self.assertEqual(_options_from_args(args).output_root, "approved-data")

    def test_suite_honors_eegle_session_root_for_parent_and_child_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_root = Path(tmp) / "approved-data"
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit-env-root",
                visit_id="visit-env-root",
                task_mode="dry-run",
                trials_per_session=10,
                baseline_seconds=2.0,
                break_seconds=0.0,
                record_eeg=False,
                require_eeg=False,
            )
            with patch.dict(os.environ, {"EEGLE_SESSION_ROOT": str(session_root)}, clear=False), patch(
                "eegle.pipelines.dsart_recording.run_recording_preflight",
                side_effect=self._passing_preflight,
            ):
                result = run_recording_suite(options)

            manifest = json.loads(Path(result["manifest_file"]).read_text(encoding="utf-8"))
            child_parameters = json.loads(
                (Path(result["dsart_session_1_directory"]) / "parameters.json").read_text(encoding="utf-8")
            )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(Path(result["manifest_file"]).is_relative_to(session_root))
        self.assertTrue(Path(result["baseline_session_directory"]).is_relative_to(session_root))
        self.assertTrue(Path(result["dsart_session_1_directory"]).is_relative_to(session_root))
        self.assertTrue(Path(result["dsart_session_2_directory"]).is_relative_to(session_root))
        self.assertEqual(os.path.realpath(manifest["session_root"]), os.path.realpath(session_root))
        self.assertEqual(
            os.path.realpath(child_parameters["runtime"]["runtime_cache_dir"]),
            os.path.realpath(session_root / ".runtime"),
        )

    def test_suite_reports_locked_session_root_before_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocked_root = Path(tmp) / "not-a-directory"
            blocked_root.write_text("blocked\n", encoding="utf-8")
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit-blocked-root",
                output_root=blocked_root,
                record_eeg=False,
                require_eeg=False,
            )
            with self.assertRaisesRegex(OSError, r"--session-root.*do not redirect"):
                run_recording_suite(options)

    def test_recording_storage_probe_exercises_fresh_child_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = probe_recording_storage(Path(tmp) / "approved-data")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["parent_operations"]["status"], "ok")
        self.assertEqual(result["child_process"]["status"], "ok")
        self.assertEqual(result["child_process"]["return_code"], 0)

    def test_storage_preflight_fails_when_task_worker_cannot_write(self) -> None:
        probe = {
            "status": "fail",
            "output_root": "C:\\EEGleData",
            "failures": ["fresh child Python process was denied"],
            "child_process": {"status": "fail", "return_code": 1},
        }
        with patch(
            "eegle.pipelines.dsart_recording.probe_recording_storage",
            return_value=probe,
        ):
            result = _storage_check(Path("C:/EEGleData"), record_eeg=True)

        self.assertEqual(result.status, "fail")
        self.assertIn("fresh child Python process was denied", result.detail)

    def test_recording_storage_probe_reports_child_policy_denial(self) -> None:
        denied = SimpleNamespace(
            returncode=5,
            stdout="",
            stderr="PermissionError: enterprise child-process policy denied access",
        )
        with tempfile.TemporaryDirectory() as tmp, patch(
            "eegle.storage_permissions.subprocess.run",
            return_value=denied,
        ):
            result = probe_recording_storage(Path(tmp) / "approved-data")

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["parent_operations"]["status"], "ok")
        self.assertEqual(result["child_process"]["status"], "fail")
        self.assertIn("enterprise child-process policy", result["child_process"]["error"])

    def test_atomic_json_write_retries_transient_windows_access_denial(self) -> None:
        original_replace = Path.replace
        attempts = 0

        def transient_replace(source: Path, target: Path) -> Path:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("simulated Windows access denial")
            return original_replace(source, target)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "second_preflight.json"
            target.write_text('{"old": true}\n', encoding="utf-8")
            with patch.object(Path, "replace", new=transient_replace), patch(
                "eegle.pipelines.dsart_recording.sleep"
            ):
                _write_json_atomic(target, {"updated": True})
            payload = json.loads(target.read_text(encoding="utf-8"))

        self.assertEqual(attempts, 2)
        self.assertEqual(payload, {"updated": True})

    def test_practice_policy_skips_short_smoke_unless_explicitly_requested(self) -> None:
        smoke = DsartRecordingOptions(
            recipe="dsart8",
            config_path=CONFIG_8,
            participant_id="unit",
            trials_per_session=10,
            record_eeg=False,
            require_eeg=False,
        )
        config = load_config(CONFIG_8)
        policy = _configure_practice_policy(config, smoke)
        self.assertTrue(policy["shortened_run"])
        self.assertFalse(policy["session_1_enabled"])
        self.assertFalse(config["tasks"]["dynamic_sart"]["practice"]["enabled"])

        explicit = copy.copy(smoke)
        object.__setattr__(explicit, "include_practice", True)
        explicit_config = load_config(CONFIG_8)
        explicit_policy = _configure_practice_policy(explicit_config, explicit)
        self.assertTrue(explicit_policy["session_1_enabled"])

        formal = copy.copy(smoke)
        object.__setattr__(formal, "trials_per_session", 600)
        formal_config = load_config(CONFIG_8)
        formal_policy = _configure_practice_policy(formal_config, formal)
        self.assertFalse(formal_policy["shortened_run"])
        self.assertTrue(formal_policy["session_1_enabled"])

    def test_short_suite_records_exactly_requested_trials_without_practice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit-short-suite",
                visit_id="visit-short-suite",
                task_mode="dry-run",
                trials_per_session=10,
                baseline_seconds=2.0,
                break_seconds=0.0,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )
            with patch(
                "eegle.pipelines.dsart_recording.run_recording_preflight",
                side_effect=self._passing_preflight,
            ):
                result = run_recording_suite(options)

            summaries = [
                json.loads((Path(result[f"dsart_session_{index}_directory"]) / "session_summary.json").read_text())
                for index in (1, 2)
            ]

        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["include_practice"])
        self.assertFalse(result["practice_policy"]["session_1_enabled"])
        for summary in summaries:
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["task"]["summary"]["experimental_trials"], 10)
            self.assertEqual(summary["task"]["summary"]["practice_trials"], 0)
            self.assertFalse(summary["task"]["summary"]["aborted"])

    def test_include_practice_cli_flag_is_explicit(self) -> None:
        args = build_parser().parse_args(
            ["--recipe", "dsart8", "--participant", "unit", "--trials", "10", "--include-practice"]
        )
        self.assertTrue(_options_from_args(args).include_practice)

    def test_forward_result_marks_aborted_task_as_failed(self) -> None:
        root = Path("/tmp/dsart-aborted-test")
        result = ForwardExperimentResult(
            session_dir=root,
            preflight=[],
            task=TaskRunResult(
                task="dynamic_sart",
                session_dir=root,
                mode="psychopy",
                summary={"aborted": True, "abort_reason": "practice_criteria_not_met"},
            ),
            eeg=None,
            analysis=None,
            processes={"status": "complete"},
            summary_file=root / "session_summary.json",
        )
        self.assertEqual(result.as_dict()["status"], "failed")

    def test_forward_result_requires_completed_managed_process_lifecycle(self) -> None:
        root = Path("/tmp/dsart-process-status-test")
        for process_status in (None, "running", "partial", "failed"):
            with self.subTest(process_status=process_status):
                result = ForwardExperimentResult(
                    session_dir=root,
                    preflight=[],
                    task=TaskRunResult(
                        task="dynamic_sart",
                        session_dir=root,
                        mode="psychopy",
                        summary={"aborted": False},
                    ),
                    eeg=None,
                    analysis=None,
                    processes={} if process_status is None else {"status": process_status},
                    summary_file=root / "session_summary.json",
                )
                self.assertEqual(result.as_dict()["status"], "failed")

    def test_run_task_cli_returns_failure_when_dsart_aborts(self) -> None:
        args = SimpleNamespace(
            task="dynamic_sart",
            mode="psychopy",
            participant="unit",
            trials=10,
            window_size=None,
            full_screen=None,
        )
        task_result = TaskRunResult(
            task="dynamic_sart",
            session_dir=Path("/retained/display-smoke"),
            mode="psychopy",
            summary={"aborted": True, "abort_reason": "keyboard_disconnect"},
        )
        component = MagicMock()
        component.run.return_value = task_result
        output = io.StringIO()
        with patch("eegle.cli.make_task_component", return_value=component), redirect_stdout(output):
            exit_code = cmd_run_task(args, load_config(CONFIG_8))

        printed = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(printed["status"], "failed")
        self.assertEqual(printed["failure_detail"], "keyboard_disconnect")

    def test_run_forward_cli_does_not_mask_managed_process_failure(self) -> None:
        args = SimpleNamespace(
            task="pvt",
            task_mode="dry-run",
            participant="unit",
            trials=2,
            skip_eeg=False,
            require_eeg=False,
            allow_missing_eeg=False,
            calibration_suite=None,
            window_size=None,
            full_screen=None,
        )
        forward = MagicMock()
        forward.preflight = []
        forward.as_dict.return_value = {
            "status": "failed",
            "session_dir": "/retained/session",
            "summary_file": "/retained/session/session_summary.json",
            "task": {"task": "pvt", "mode": "dry-run", "summary": {"aborted": False}},
            "eeg": None,
            "analysis": None,
            "processes": {"status": "failed", "notes": ["LabRecorder exited unexpectedly"], "processes": {}},
        }
        output = io.StringIO()
        with patch("eegle.cli.ForwardExperimentRunner") as runner, redirect_stdout(output):
            runner.return_value.run.return_value = forward
            exit_code = cmd_run_forward(args, load_config(CONFIG_8))

        printed = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(printed["status"], "failed")
        self.assertEqual(printed["process_exit_code"], 1)
        self.assertIn("LabRecorder", printed["failure_detail"])

    def test_run_forward_cli_does_not_mask_dsart_validation_failure(self) -> None:
        args = SimpleNamespace(
            task="dynamic_sart",
            task_mode="dry-run",
            participant="unit",
            trials=10,
            skip_eeg=True,
            require_eeg=False,
            allow_missing_eeg=True,
            calibration_suite=None,
            window_size=None,
            full_screen=None,
        )
        forward = MagicMock()
        forward.preflight = []
        forward.as_dict.return_value = {
            "status": "complete",
            "session_dir": "/retained/session",
            "summary_file": "/retained/session/session_summary.json",
            "task": {"task": "dynamic_sart", "mode": "dry-run", "summary": {"aborted": False}},
            "eeg": None,
            "analysis": None,
            "processes": {"status": "complete", "notes": [], "processes": {}},
        }
        strict_validation = {
            "validation": {
                "status": "fail",
                "failures": ["stimulus manifest is missing"],
                "warnings": [],
            }
        }
        output = io.StringIO()
        with patch("eegle.cli.ForwardExperimentRunner") as runner, patch(
            "eegle.pipelines.dsart_recording.validate_dynamic_sart_forward_result",
            return_value=strict_validation,
        ), redirect_stdout(output):
            runner.return_value.run.return_value = forward
            exit_code = cmd_run_forward(args, load_config(CONFIG_8))

        printed = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(printed["status"], "failed")
        self.assertEqual(printed["validation"]["status"], "fail")
        self.assertIn("stimulus manifest", printed["failure_detail"])

    def test_run_forward_cli_fails_closed_on_inconsistent_validation_status(self) -> None:
        args = SimpleNamespace(
            task="dynamic_sart",
            task_mode="dry-run",
            participant="unit",
            trials=10,
            skip_eeg=True,
            require_eeg=False,
            allow_missing_eeg=True,
            calibration_suite=None,
            window_size=None,
            full_screen=None,
        )
        forward = MagicMock()
        forward.preflight = []
        forward.as_dict.return_value = {
            "status": "complete",
            "session_dir": "/retained/session",
            "summary_file": "/retained/session/session_summary.json",
            "task": {"task": "dynamic_sart", "mode": "dry-run", "summary": {"aborted": False}},
            "eeg": None,
            "analysis": None,
            "processes": {"status": "complete", "notes": [], "processes": {}},
        }
        output = io.StringIO()
        with patch("eegle.cli.ForwardExperimentRunner") as runner, patch(
            "eegle.pipelines.dsart_recording.validate_dynamic_sart_forward_result",
            return_value={
                "validation": {"status": "fail", "failures": [], "warnings": []}
            },
        ), redirect_stdout(output):
            runner.return_value.run.return_value = forward
            exit_code = cmd_run_forward(args, load_config(CONFIG_8))

        printed = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(printed["status"], "failed")
        self.assertEqual(printed["failure_detail"], "Dynamic SART validation status=fail")

    def test_dsart_validation_exception_is_a_failure_with_raw_retained(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "events").mkdir()
            (session / "events" / "stimulus_manifest.json").write_text("{}", encoding="utf-8")
            with patch(
                "eegle.pipelines.dsart_recording.analyze_dynamic_sart_session",
                return_value={},
            ), patch(
                "eegle.pipelines.dsart_recording._child_session_validation",
                side_effect=RuntimeError("synthetic validator defect"),
            ):
                validated = validate_dynamic_sart_forward_result(
                    {
                        "status": "complete",
                        "session_dir": str(session),
                        "task": {"summary": {"aborted": False}},
                    },
                    load_config(CONFIG_8),
                    record_eeg=False,
                    task_mode="dry-run",
                )

        validation = validated["validation"]
        self.assertEqual(validation["status"], "fail")
        self.assertTrue(any("synthetic validator defect" in row for row in validation["failures"]))
        self.assertTrue(any("must not be overwritten" in row for row in validation["warnings"]))

    def test_psychopy_child_sessions_dispatch_to_fresh_process_isolation(self) -> None:
        options = DsartRecordingOptions(
            recipe="dsart8",
            config_path=CONFIG_8,
            participant_id="unit",
            task_mode="psychopy",
            record_eeg=False,
            require_eeg=False,
        )
        expected = {"status": "completed"}
        with patch(
            "eegle.pipelines.dsart_recording._run_dsart_child_session_isolated",
            return_value=expected,
        ) as isolated, patch(
            "eegle.pipelines.dsart_recording._run_dsart_child_session_inline"
        ) as inline:
            result = run_dsart_child_session(
                load_config(CONFIG_8),
                options,
                visit_id="visit",
                session_index=1,
                seed=123,
                preflight={"checks": []},
            )
        self.assertIs(result, expected)
        isolated.assert_called_once()
        inline.assert_not_called()

    def test_phase_worker_nonzero_exit_cannot_report_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(CONFIG_8)
            config["runtime"]["runtime_cache_dir"] = tmp
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit",
                task_mode="psychopy",
                trials_per_session=10,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )

            def start_worker(command):
                result_path = Path(command[command.index("--result") + 1])
                result_path.write_text(
                    json.dumps(
                        {
                            "status": "completed",
                            "session_index": 1,
                            "session_dir": "/retained/worker-session",
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(pid=1234, wait=lambda *, timeout: 7)

            with patch("eegle.pipelines.dsart_recording.subprocess.Popen", side_effect=start_worker):
                result = _run_dsart_child_session_isolated(
                    config,
                    options,
                    visit_id="visit-worker-mismatch",
                    session_index=1,
                    seed=123,
                    preflight={"checks": []},
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_kind"], "phase_worker_status_mismatch")
        self.assertEqual(result["phase_worker"]["return_code"], 7)

    def test_isolated_child_session_uses_json_request_and_result_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(CONFIG_8)
            config["runtime"]["session_root"] = tmp
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit",
                task_mode="psychopy",
                trials_per_session=10,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )
            captured = {}

            def run_worker(command):
                request_path = Path(command[command.index("--request") + 1])
                result_path = Path(command[command.index("--result") + 1])
                captured["request"] = json.loads(request_path.read_text())
                result_path.write_text(
                    json.dumps({"status": "completed", "session_index": 1, "session_dir": "/recording"}),
                    encoding="utf-8",
                )
                return SimpleNamespace(pid=1234, wait=lambda *, timeout: 0)

            with patch("eegle.pipelines.dsart_recording.subprocess.Popen", side_effect=run_worker):
                result = _run_dsart_child_session_isolated(
                    config,
                    options,
                    visit_id="visit-isolated",
                    session_index=1,
                    seed=123,
                    preflight={"checks": []},
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["phase_worker"]["mode"], "fresh_python_process")
        self.assertEqual(result["phase_worker"]["return_code"], 0)
        self.assertTrue(result["phase_worker"]["status_file"].endswith(".status.json"))
        self.assertEqual(captured["request"]["session_index"], 1)
        self.assertEqual(captured["request"]["options"]["window_size"], None)

    def test_phase_worker_executes_complete_child_session_in_fresh_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(CONFIG_8)
            config["runtime"]["session_root"] = tmp
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit-worker",
                task_mode="dry-run",
                trials_per_session=10,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )
            _configure_practice_policy(config, options)
            result = _run_dsart_child_session_isolated(
                config,
                options,
                visit_id="visit-worker",
                session_index=1,
                seed=123,
                preflight={"checks": [{"name": "software", "status": "ok", "detail": "test", "data": {}}]},
            )
            worker_status = json.loads(Path(result["phase_worker"]["status_file"]).read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["phase_worker"]["return_code"], 0)
        self.assertEqual(worker_status["status"], "complete")
        self.assertEqual(worker_status["stage"], "worker_finished")
        self.assertEqual(result["task_summary"]["experimental_trials"], 10)
        self.assertEqual(result["task_summary"]["practice_trials"], 0)

    def test_child_session_exception_persists_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = load_config(CONFIG_8)
            config["runtime"]["session_root"] = tmp
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit",
                task_mode="psychopy",
                trials_per_session=10,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )
            runner = MagicMock()
            runner.session_dir = Path(tmp) / "child-session"
            runner.run.side_effect = AttributeError("native window failure")
            with patch("eegle.pipelines.dsart_recording.ForwardExperimentRunner", return_value=runner):
                result = _run_dsart_child_session_inline(
                    config,
                    options,
                    visit_id="visit",
                    session_index=2,
                    seed=456,
                    preflight={"checks": []},
                )
            traceback_path = Path(result["traceback_file"])
            traceback_text = traceback_path.read_text(encoding="utf-8")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_kind"], "child_session_exception")
        self.assertIn("AttributeError: native window failure", traceback_text)

    def test_post_recording_metadata_failure_preserves_child_session_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "completed-raw-session"
            session.mkdir()
            (session / "events").mkdir()
            (session / "events" / "stimulus_manifest.json").write_text(
                "{}",
                encoding="utf-8",
            )
            config = load_config(CONFIG_32)
            config["runtime"]["session_root"] = tmp
            options = DsartRecordingOptions(
                recipe="dsart32",
                config_path=CONFIG_32,
                participant_id="unit",
                task_mode="psychopy",
                trials_per_session=10,
                record_eeg=True,
                require_eeg=True,
                output_root=tmp,
            )
            forward = MagicMock()
            forward.session_dir = session
            forward.as_dict.return_value = {
                "status": "complete",
                "session_dir": str(session),
                "task": {"summary": {}},
            }
            runner = MagicMock()
            runner.run.return_value = forward
            with patch(
                "eegle.pipelines.dsart_recording.ForwardExperimentRunner",
                return_value=runner,
            ), patch(
                "eegle.pipelines.dsart_recording._write_json_atomic",
                side_effect=PermissionError("policy lock"),
            ), patch(
                "eegle.pipelines.dsart_recording._child_session_validation",
                return_value={"status": "pass", "failures": [], "warnings": []},
            ):
                result = _run_dsart_child_session_inline(
                    config,
                    options,
                    visit_id="visit",
                    session_index=1,
                    seed=123,
                    preflight={"checks": []},
                )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["session_dir"], str(session))
        self.assertTrue(result["raw_recording_retained"])
        self.assertNotIn("failure_kind", result)
        self.assertTrue(any("policy lock" in warning for warning in result["warnings"]))

    def test_relative_runtime_cache_follows_the_approved_session_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "approved"
            self.assertEqual(
                _runtime_cache_root({"runtime": {"runtime_cache_dir": ".runtime"}}, root),
                (root / ".runtime").resolve(),
            )
            explicit = Path(tmp) / "explicit-cache"
            self.assertEqual(
                _runtime_cache_root({"runtime": {"runtime_cache_dir": str(explicit)}}, root),
                explicit.resolve(),
            )

    def test_recipes_share_task_and_recording_contract(self) -> None:
        dsart8 = load_config(CONFIG_8)
        dsart32 = load_config(CONFIG_32)
        self.assertEqual(validate_recording_config(dsart8, "dsart8"), [])
        issues_32 = validate_recording_config(dsart32, "dsart32")
        self.assertEqual(issues_32, [])
        self.assertEqual(dsart8["tasks"], dsart32["tasks"])
        self.assertEqual(dsart8["realtime"], dsart32["realtime"])
        self.assertFalse(dsart8["hardware"]["display"]["full_screen"])
        self.assertFalse(dsart32["hardware"]["display"]["full_screen"])
        self.assertTrue(dsart8["hardware"]["display"]["resizable"])
        self.assertTrue(dsart8["hardware"]["display"]["wait_blanking"])
        self.assertTrue(dsart8["hardware"]["display"]["require_refresh_rate_match"])
        self.assertEqual(dsart8["tasks"]["dynamic_sart"]["response_window_seconds"], 1.6)
        self.assertEqual(dsart8["tasks"]["dynamic_sart"]["inter_trial_jitter_max_seconds"], 0.0)
        suite_8 = copy.deepcopy(dsart8["recording_suite"])
        suite_32 = copy.deepcopy(dsart32["recording_suite"])
        suite_8.pop("recipe")
        suite_32.pop("recipe")
        self.assertEqual(suite_8, suite_32)

    def test_recipe_validation_rejects_all_online_acquisition_work(self) -> None:
        mutations = (
            ("enabled",),
            ("capture", "enabled"),
            ("inference", "enabled"),
            ("classifier", "enabled"),
            ("decision_policy", "enabled"),
            ("feedback", "client", "enabled"),
        )
        for path in mutations:
            with self.subTest(path=path):
                config = load_config(CONFIG_8)
                target = config["realtime"]
                for key in path[:-1]:
                    target = target.setdefault(key, {})
                target[path[-1]] = True
                failures = [
                    row for row in validate_recording_config(config, "dsart8") if row["status"] == "fail"
                ]
                self.assertTrue(failures)

    def test_recipe_validation_rejects_window_marker_and_epoching_drift(self) -> None:
        mutations = (
            ("hardware", "display", "wait_blanking", False),
            ("hardware", "markers", "required_for_realtime", False),
            ("hardware", "eeg", "raw_sample_mode", "filtered"),
            ("realtime", "epoching", "timebase", "local_received"),
        )
        for *path, value in mutations:
            with self.subTest(path=path):
                config = load_config(CONFIG_8)
                target = config
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                failures = [row for row in validate_recording_config(config, "dsart8") if row["status"] == "fail"]
                self.assertTrue(failures)

    def test_recipe_validation_allows_fullscreen_display(self) -> None:
        config = load_config(CONFIG_8)
        config["hardware"]["display"]["full_screen"] = True
        self.assertEqual(validate_recording_config(config, "dsart8"), [])

    def test_raw_integrity_rejects_channel_permutation_even_when_values_are_unmodified(self) -> None:
        config = load_config(CONFIG_8)
        expected = list(config["hardware"]["eeg"]["channel_number_map"])
        permuted = [expected[1], expected[0], *expected[2:]]
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "raw").mkdir()
            (session / "parameters.json").write_text(json.dumps(config), encoding="utf-8")
            (session / "raw" / "eeg.csv").write_text(
                ",".join([
                    "lsl_timestamp",
                    "local_received_time",
                    "source_lsl_timestamp",
                    "lsl_time_correction_seconds",
                    *permuted,
                ]) + "\n",
                encoding="utf-8",
            )
            metadata = {
                "status": "stopped",
                "sample_count": 10,
                "stream": {
                    "channel_names": permuted,
                    "channel_value_order_changed": False,
                    "amplitude_transformations": [],
                    "lsl_processing": [],
                    "initial_time_correction_seconds": 0.001,
                },
                "raw_sample_contract": {
                    "amplitude_samples_modified": False,
                    "amplitude_transformations": [],
                    "channel_value_order_modified": False,
                    "recording_timestamp_mode": "source_preserving",
                    "source_timestamp_retained": True,
                    "initial_time_correction_available": True,
                    "filtering": "none",
                    "resampling": "none",
                    "rereferencing": "none",
                    "artifact_rejection": "none",
                },
            }
            (session / "raw" / "eeg_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            integrity = _raw_eeg_integrity(session, required=True)
        self.assertEqual(integrity["status"], "fail")
        self.assertTrue(any("physical device order" in failure for failure in integrity["failures"]))

    def test_raw_integrity_requires_clean_stop_and_rejects_timestamp_gaps(self) -> None:
        config = load_config(CONFIG_8)
        expected = list(config["hardware"]["eeg"]["channel_number_map"])
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "raw").mkdir()
            (session / "parameters.json").write_text(json.dumps(config), encoding="utf-8")
            (session / "raw" / "eeg.csv").write_text(
                ",".join([
                    "lsl_timestamp", "local_received_time", "source_lsl_timestamp",
                    "lsl_time_correction_seconds", *expected,
                ]) + "\n",
                encoding="utf-8",
            )
            metadata = {
                "status": "recording",
                "sample_count": 10,
                "timestamp_gap_count": 1,
                "nonmonotonic_timestamp_count": 0,
                "stream": {
                    "channel_names": expected,
                    "channel_value_order_changed": False,
                    "amplitude_transformations": [],
                    "lsl_processing": [],
                    "initial_time_correction_seconds": 0.001,
                },
                "raw_sample_contract": {
                    "amplitude_samples_modified": False,
                    "amplitude_transformations": [],
                    "channel_value_order_modified": False,
                    "recording_timestamp_mode": "source_preserving",
                    "source_timestamp_retained": True,
                    "initial_time_correction_available": True,
                    "filtering": "none",
                    "resampling": "none",
                    "rereferencing": "none",
                    "artifact_rejection": "none",
                },
            }
            (session / "raw" / "eeg_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            integrity = _raw_eeg_integrity(session, required=True)

        self.assertTrue(any("status is not stopped" in row for row in integrity["failures"]))
        self.assertTrue(any("timestamp gaps" in row for row in integrity["failures"]))

    def test_raw_integrity_dispatches_to_xdf_for_managed_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "raw").mkdir()
            (session / "parameters.json").write_text(
                json.dumps(
                    {
                        "hardware": {"eeg": {}},
                        "processes": {"recorder": {"backend": "labrecorder_xdf"}},
                    }
                ),
                encoding="utf-8",
            )
            xdf_result = {
                "status": "fail",
                "failures": ["managed XDF validation failed"],
                "warnings": [],
            }
            with patch(
                "eegle.pipelines.dsart_recording.validate_xdf_recording",
                return_value=xdf_result,
            ) as validate:
                integrity = _raw_eeg_integrity(session, required=False)

        validate.assert_called_once_with(session, required=False)
        self.assertEqual(integrity["primary_format"], "xdf")
        self.assertEqual(integrity["xdf_integrity"], xdf_result)
        self.assertEqual(integrity["status"], "fail")
        self.assertIn("managed XDF validation failed", integrity["failures"])

    def test_raw_integrity_skips_csv_checks_when_xdf_mirror_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "raw").mkdir()
            (session / "parameters.json").write_text(
                json.dumps(
                    {
                        "hardware": {"eeg": {}},
                        "processes": {
                            "recorder": {"backend": "labrecorder_xdf", "csv_mirror": False}
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "eegle.pipelines.dsart_recording.validate_xdf_recording",
                return_value={"status": "pass", "failures": [], "warnings": []},
            ):
                integrity = _raw_eeg_integrity(session, required=True)

        self.assertEqual(integrity["failures"], [])
        self.assertEqual(integrity["warnings"], [])
        self.assertEqual(integrity["status"], "pass")
        self.assertEqual(integrity["csv_validation_status"], "not_configured")

    def test_raw_integrity_warns_when_enabled_xdf_csv_mirror_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "raw").mkdir()
            (session / "parameters.json").write_text(
                json.dumps(
                    {
                        "hardware": {"eeg": {}},
                        "processes": {
                            "recorder": {"backend": "labrecorder_xdf", "csv_mirror": True}
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "eegle.pipelines.dsart_recording.validate_xdf_recording",
                return_value={"status": "pass", "failures": [], "warnings": []},
            ):
                integrity = _raw_eeg_integrity(session, required=True)

        self.assertEqual(integrity["failures"], [])
        self.assertEqual(integrity["status"], "warning")
        self.assertEqual(integrity["csv_validation_status"], "enabled")
        self.assertTrue(any("CSV mirror warning" in warning for warning in integrity["warnings"]))

    def test_baseline_validation_does_not_require_disabled_xdf_csv_mirror(self) -> None:
        labels = [
            "dsart_baseline_eyes_open_start",
            "dsart_baseline_eyes_open_end",
            "dsart_baseline_eyes_closed_start",
            "dsart_baseline_eyes_closed_end",
        ]
        phases = [
            {
                "phase": "eyes_open",
                "completion_status": "completed",
                "aborted": False,
                "start_monotonic_timestamp": 10.0,
                "end_monotonic_timestamp": 11.0,
                "start_lsl_timestamp": 100.0,
                "end_lsl_timestamp": 101.0,
            },
            {
                "phase": "eyes_closed",
                "completion_status": "completed",
                "aborted": False,
                "start_monotonic_timestamp": 12.0,
                "end_monotonic_timestamp": 13.0,
                "start_lsl_timestamp": 102.0,
                "end_lsl_timestamp": 103.0,
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            parameters = session / "parameters.json"
            events = session / "events.jsonl"
            parameters.write_text(
                json.dumps({"hardware": {"markers": {"source_id": "marker-source"}}}),
                encoding="utf-8",
            )
            events.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "label": label,
                            "metadata": {
                                "lsl_timestamp": 100.0 + index,
                                "marker_stream_source_id": "marker-source",
                                "scheduled_on_flip": label.endswith("_start"),
                            },
                        }
                    )
                    for index, label in enumerate(labels)
                ),
                encoding="utf-8",
            )
            paths = SimpleNamespace(root=session, parameters=parameters, events_jsonl=events)
            with patch(
                "eegle.pipelines.dsart_recording._marker_receipt_integrity",
                return_value={"status": "pass", "failures": [], "warnings": []},
            ), patch(
                "eegle.pipelines.dsart_recording._raw_eeg_integrity",
                return_value={
                    "status": "pass",
                    "primary_format": "xdf",
                    "csv_validation_status": "not_configured",
                    "metadata": {},
                    "failures": [],
                    "warnings": [],
                },
            ):
                validation = _baseline_recording_validation(
                    paths,
                    {"phases": phases},
                    record_eeg=True,
                )

        self.assertEqual(validation["status"], "pass")
        self.assertEqual(validation["warnings"], [])
        self.assertEqual(
            validation["csv_receipt_span_status"],
            "not_applicable_authoritative_xdf",
        )

        with patch(
            "eegle.pipelines.dsart_recording._marker_receipt_integrity",
            return_value={"status": "pass", "failures": [], "warnings": []},
        ), patch(
            "eegle.pipelines.dsart_recording._raw_eeg_integrity",
            return_value={
                "status": "pass",
                "primary_format": "xdf",
                "csv_validation_status": "not_configured",
                "metadata": {},
                "failures": [],
                "warnings": [],
            },
        ):
            failed_process_validation = _baseline_recording_validation(
                paths,
                {
                    "phases": phases,
                    "processes": {
                        "status": "failed",
                        "notes": ["LabRecorder exited unexpectedly"],
                    },
                },
                record_eeg=True,
            )
        self.assertEqual(failed_process_validation["status"], "fail")
        self.assertTrue(
            any(
                "LabRecorder exited unexpectedly" in row
                for row in failed_process_validation["failures"]
            )
        )

    def test_independent_marker_receipt_must_match_ledger_order_and_timestamps(self) -> None:
        ledger = [
            {"label": "dynamic_sart_stimulus_onset__trial=1", "metadata": {"lsl_timestamp": 10.0}},
            {"label": "dynamic_sart_stimulus_offset__trial=1", "metadata": {"lsl_timestamp": 10.25}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "raw").mkdir()
            (session / "raw" / "lsl_markers_received_metadata.json").write_text(
                json.dumps({"status": "stopped", "received_count": 2}),
                encoding="utf-8",
            )
            receipt = session / "raw" / "lsl_markers_received.csv"
            receipt.write_text(
                "marker_label,lsl_timestamp,local_received_lsl_timestamp\n"
                "dynamic_sart_stimulus_onset__trial=1,10.000000000,10.01\n"
                "dynamic_sart_stimulus_offset__trial=1,10.250000000,10.26\n",
                encoding="utf-8",
            )
            passed = _marker_receipt_integrity(session, ledger, required=True)
            (session / "raw" / "lsl_markers_received_metadata.json").write_text(
                json.dumps({"status": "failed", "received_count": 2}),
                encoding="utf-8",
            )
            unclean_stop = _marker_receipt_integrity(session, ledger, required=True)
            (session / "raw" / "lsl_markers_received_metadata.json").write_text(
                json.dumps({"status": "stopped", "received_count": 2}),
                encoding="utf-8",
            )
            receipt.write_text(
                "marker_label,lsl_timestamp,local_received_lsl_timestamp\n"
                "dynamic_sart_stimulus_onset__trial=1,10.000000000,10.50\n"
                "dynamic_sart_stimulus_offset__trial=1,10.250000000,10.75\n",
                encoding="utf-8",
            )
            delayed = _marker_receipt_integrity(session, ledger, required=True)
            receipt.write_text(
                "marker_label,lsl_timestamp,local_received_lsl_timestamp\n"
                "dynamic_sart_stimulus_onset__trial=1,10.000000000,9.99\n"
                "dynamic_sart_stimulus_offset__trial=1,10.250000000,10.24\n",
                encoding="utf-8",
            )
            early = _marker_receipt_integrity(session, ledger, required=True)
            receipt.write_text(
                "marker_label,lsl_timestamp,local_received_lsl_timestamp\n"
                "dynamic_sart_stimulus_offset__trial=1,10.250000000,10.26\n",
                encoding="utf-8",
            )
            failed = _marker_receipt_integrity(session, ledger, required=True)

        self.assertEqual(passed["status"], "pass")
        self.assertAlmostEqual(passed["maximum_delivery_latency_seconds"], 0.01)
        self.assertEqual(unclean_stop["status"], "warning")
        self.assertEqual(unclean_stop["failures"], [])
        self.assertTrue(any("clean stop" in row for row in unclean_stop["warnings"]))
        self.assertEqual(delayed["status"], "warning")
        self.assertEqual(delayed["failures"], [])
        self.assertEqual(delayed["late_delivery_indices"], [1, 2])
        self.assertEqual(early["status"], "warning")
        self.assertEqual(early["failures"], [])
        self.assertEqual(early["negative_delivery_latency_indices"], [1, 2])
        self.assertEqual(failed["status"], "fail")
        self.assertTrue(any("order/count" in row for row in failed["failures"]))

    def test_marker_receipt_drain_records_expected_delivery_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            recorder = LslMarkerReceiptRecorder(
                "marker-source",
                root / "markers.csv",
                root / "markers.json",
            )
            recorder._summary.status = "recording"
            recorder._summary.received_count = 4

            completed = recorder.wait_for_count(4, timeout=0.0)
            snapshot = recorder.snapshot()

        self.assertTrue(completed)
        self.assertEqual(snapshot["drain_expected_count"], 4)
        self.assertTrue(snapshot["drain_completed"])

    def test_dry_run_marker_integrity_does_not_require_psychopy_flips(self) -> None:
        source_id = "dry-marker-source"
        onset = {
            "label": "dynamic_sart_stimulus_onset__trial=1",
            "trial": 1,
            "timestamp": 10.0,
            "metadata": {
                "lsl_timestamp": 20.0,
                "marker_stream_source_id": source_id,
            },
        }
        offset = {
            "label": "dynamic_sart_stimulus_offset__trial=1",
            "trial": 1,
            "timestamp": 10.25,
            "metadata": {
                "lsl_timestamp": 20.25,
                "marker_stream_source_id": source_id,
            },
        }
        trial = {
            "global_trial_index": 1,
            "stimulus_onset_monotonic": 10.0,
            "stimulus_offset_monotonic": 10.25,
            "response_window_close_monotonic": 10.5,
        }
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "events").mkdir()
            (session / "raw").mkdir()
            (session / "events" / "events.jsonl").write_text(
                json.dumps(onset) + "\n" + json.dumps(offset) + "\n",
                encoding="utf-8",
            )
            (session / "events" / "dynamic_sart_trials.jsonl").write_text(
                json.dumps(trial) + "\n",
                encoding="utf-8",
            )
            (session / "raw" / "eeg_metadata.json").write_text(
                json.dumps(
                    {
                        "first_lsl_timestamp": 19.0,
                        "last_lsl_timestamp": 21.0,
                        "first_local_received_time": 9.0,
                        "last_local_received_time": 11.0,
                    }
                ),
                encoding="utf-8",
            )
            (session / "raw" / "lsl_markers_received_metadata.json").write_text(
                json.dumps({"status": "stopped", "received_count": 2}),
                encoding="utf-8",
            )
            (session / "raw" / "lsl_markers_received.csv").write_text(
                "marker_label,lsl_timestamp,local_received_lsl_timestamp\n"
                f"{onset['label']},20.000000000,20.0\n"
                f"{offset['label']},20.250000000,20.25\n",
                encoding="utf-8",
            )
            parameters = {"hardware": {"markers": {"source_id": source_id}}}
            dry = _task_marker_integrity(
                session,
                parameters,
                require_markers=True,
                require_display_flip=False,
            )
            visual = _task_marker_integrity(
                session,
                parameters,
                require_markers=True,
                require_display_flip=True,
            )
            (session / "raw" / "eeg_metadata.json").unlink()
            xdf_parameters = copy.deepcopy(parameters)
            xdf_parameters["processes"] = {
                "recorder": {"backend": "labrecorder_xdf"}
            }
            xdf_without_csv = _task_marker_integrity(
                session,
                xdf_parameters,
                require_markers=True,
                require_display_flip=False,
            )

        self.assertEqual(dry["status"], "pass", dry)
        self.assertEqual(visual["status"], "fail")
        self.assertTrue(any("display flip" in row for row in visual["failures"]))
        self.assertEqual(xdf_without_csv["failures"], [])
        self.assertEqual(xdf_without_csv["status"], "pass")
        self.assertEqual(
            xdf_without_csv["csv_marker_overlap_status"],
            "not_applicable_authoritative_xdf",
        )

    def test_raw_recorder_aborts_on_large_source_timestamp_gap(self) -> None:
        class Info:
            def channel_count(self) -> int:
                return 1

            def desc(self):
                raise RuntimeError("no metadata")

        class Inlet:
            def __init__(self, *_args, **_kwargs) -> None:
                return None

            def open_stream(self, **_kwargs) -> None:
                return None

            def time_correction(self, **_kwargs) -> float:
                return 0.0

            def pull_chunk(self, **_kwargs):
                return [[1.0], [2.0]], [1.0, 1.2]

            def close_stream(self) -> None:
                return None

        pylsl = ModuleType("pylsl")
        pylsl.StreamInlet = Inlet
        pylsl.local_clock = lambda: 1000.0
        pylsl.proc_none = 0
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"pylsl": pylsl}), patch(
            "eegle.devices.lsl_eeg._select_lsl_info",
            return_value=(Info(), {"name": "unit-eeg", "channel_count": 1, "nominal_srate": 500.0}),
        ):
            recorder = LslEegRecorder(
                {
                    "recording_lsl_processing": "source_preserving",
                    "expected_sample_rate_hz": 500,
                    "maximum_timestamp_gap_seconds": 0.1,
                    "abort_on_timestamp_gap": True,
                    "minimum_free_bytes_during_recording": 0,
                },
                Path(tmp) / "eeg.csv",
                Path(tmp) / "eeg_metadata.json",
            )
            recorder._record()
            summary = recorder.snapshot()

        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["timestamp_gap_count"], 1)
        self.assertGreater(summary["estimated_missing_samples"], 0)
        self.assertIn("timestamp gap exceeded", summary["error"])
        self.assertEqual(summary["first_source_lsl_timestamp"], 1.0)
        self.assertAlmostEqual(summary["first_local_received_lsl_timestamp"], 999.8)

    def test_nonwriting_sample_heartbeat_warns_on_gap_without_throwing(self) -> None:
        holder: dict[str, LslSampleHeartbeat] = {}

        class Inlet:
            def __init__(self, *_args, **_kwargs) -> None:
                return None

            def open_stream(self, **_kwargs) -> None:
                return None

            def pull_chunk(self, **_kwargs):
                holder["heartbeat"]._stop.set()
                return [[1.0], [2.0]], [1.0, 1.2]

            def close_stream(self) -> None:
                return None

        pylsl = ModuleType("pylsl")
        pylsl.StreamInlet = Inlet
        pylsl.local_clock = lambda: 871645.4
        pylsl.proc_none = 0
        heartbeat = LslSampleHeartbeat(
            {
                "recording_lsl_processing": "source_preserving",
                "maximum_timestamp_gap_seconds": 0.1,
                "abort_on_timestamp_gap": True,
            }
        )
        holder["heartbeat"] = heartbeat
        with patch.dict(sys.modules, {"pylsl": pylsl}), patch(
            "eegle.devices.lsl_eeg._select_lsl_info",
            return_value=(object(), {"name": "unit-eeg", "nominal_srate": 500.0}),
        ):
            heartbeat._observe()
            summary = heartbeat.stop()

        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["timestamp_gap_count"], 1)
        self.assertAlmostEqual(summary["first_local_received_lsl_timestamp"], 871645.2)
        self.assertAlmostEqual(summary["last_local_received_lsl_timestamp"], 871645.4)
        self.assertEqual(summary["clock_origin"]["definition"], "first_observed_eeg_sample")
        self.assertAlmostEqual(summary["clock_origin"]["source_lsl_timestamp"], 1.0)
        self.assertIsNone(summary["error"])

    def test_marker_receipt_recorder_persists_delivered_lsl_sample(self) -> None:
        recorder_holder = {}

        class Inlet:
            def __init__(self, *_args, **_kwargs) -> None:
                return None

            def open_stream(self, **_kwargs) -> None:
                return None

            def pull_sample(self, **_kwargs):
                recorder_holder["recorder"]._stop.set()
                return ["dynamic_sart_stimulus_onset__trial=1"], 10.0

            def close_stream(self) -> None:
                return None

        pylsl = ModuleType("pylsl")
        pylsl.resolve_byprop = lambda *_args, **_kwargs: [object()]
        pylsl.StreamInlet = Inlet
        pylsl.local_clock = lambda: 10.01
        pylsl.proc_none = 0
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"pylsl": pylsl}):
            recorder = LslMarkerReceiptRecorder(
                "run-source",
                Path(tmp) / "markers.csv",
                Path(tmp) / "markers.json",
            )
            recorder_holder["recorder"] = recorder
            recorder._record()
            summary = recorder.snapshot()
            rows = (Path(tmp) / "markers.csv").read_text(encoding="utf-8")

        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["received_count"], 1)
        self.assertEqual(summary["first_local_received_lsl_timestamp"], 10.01)
        self.assertEqual(summary["last_local_received_lsl_timestamp"], 10.01)
        self.assertIn("dynamic_sart_stimulus_onset__trial=1", rows)

    def test_full_plan_has_exact_trial_phase_and_no_go_counts(self) -> None:
        for path in (CONFIG_8, CONFIG_32):
            config = load_config(path)
            task = DynamicSartConfig.from_mapping(config["tasks"]["dynamic_sart"])
            plan = build_dynamic_sart_plan(task)
            validate_dynamic_sart_plan(plan, task)
            experimental = plan["planned_trials"]
            self.assertEqual(len(experimental), 600)
            self.assertEqual(sum(row["phase"] == "support" for row in experimental), 200)
            self.assertEqual(sum(row["phase"] == "query" for row in experimental), 400)
            self.assertEqual(sum(bool(row["is_no_go"]) for row in experimental), 67)
            self.assertEqual(sorted(block["planned_no_go_count"] for block in plan["planned_blocks"]), [11, 11, 11, 11, 11, 12])

    def test_channel_maps_are_bijective_and_dsart32_uses_confirmed_device_order(self) -> None:
        dsart8 = load_config(CONFIG_8)["hardware"]["eeg"]
        dsart32 = load_config(CONFIG_32)["hardware"]["eeg"]
        self.assertEqual(dsart8["channel_number_map"], {
            "Fz": 1, "Cz": 2, "Pz": 3, "C3": 4, "C4": 5, "P3": 6, "P4": 7, "Oz": 8,
        })
        self.assertEqual(sorted(dsart32["channel_number_map"].values()), list(range(1, 33)))
        self.assertEqual(dsart32["channel_number_map"]["Fp1"], 17)
        self.assertEqual(dsart32["channel_number_map"]["FC5"], 18)
        self.assertEqual(dsart32["mapping_version"], 2)
        self.assertNotIn("mapping_confirmation_required", dsart32)
        self.assertNotIn("mapping_confirmation_note", dsart32)
        mapped, source = mapped_channel_names([f"EEG{index}" for index in range(1, 33)], dsart32)
        self.assertEqual(mapped[16:18], ["Fp1", "FC5"])
        self.assertEqual(source, "profile:enobio32_dsart_wet")
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "events").mkdir()
            payload = write_dsart8_overlap_manifest(session, load_config(CONFIG_32))
        self.assertEqual(payload["matching_32_channel_labels"], list(DSART8_CHANNELS))
        self.assertEqual(payload["mapping_status"], "complete")

    def test_generic_nic_eeg_labels_map_positionally_without_reordering_values(self) -> None:
        config = load_config(CONFIG_8)["hardware"]["eeg"]
        mapped, source = mapped_channel_names([f"EEG{index}" for index in range(1, 9)], config)
        self.assertEqual(mapped, list(config["channel_number_map"]))
        self.assertEqual(source, "profile:enobio8_inhibition")

    def test_dsart32_command_has_no_channel_map_confirmation_gate(self) -> None:
        self.assertNotIn("--confirm-channel-map", build_parser().format_help())
        issues = validate_recording_config(load_config(CONFIG_32), "dsart32")
        self.assertFalse(any("confirmation" in issue["detail"].lower() for issue in issues))

    def test_recording_cannot_weaken_the_required_eeg_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit",
                visit_id="visit-required-eeg",
                task_mode="psychopy",
                record_eeg=True,
                require_eeg=False,
                output_root=tmp,
            )
            with self.assertRaisesRegex(ValueError, "cannot record EEG while allowing it to be missing"):
                run_recording_suite(options)

    def test_partial_attempt_is_preserved_and_resume_retries_only_incomplete_phase(self) -> None:
        preflight = self._passing_preflight
        baseline = {"status": "completed", "session_dir": "/recordings/baseline"}
        failed_child = {"status": "failed", "session_dir": "/recordings/session-1-partial", "seed": 1, "error": "abort"}
        completed_children = [
            {"status": "completed", "session_dir": "/recordings/session-1-retry", "sequence_hash": "one"},
            {"status": "completed", "session_dir": "/recordings/session-2", "sequence_hash": "two"},
        ]
        break_result = {"status": "completed", "start_monotonic": 1.0, "end_monotonic": 2.0}
        with tempfile.TemporaryDirectory() as tmp:
            initial = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit",
                visit_id="visit-resume",
                task_mode="dry-run",
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
                break_seconds=0,
            )
            with patch("eegle.pipelines.dsart_recording.run_recording_preflight", side_effect=preflight), patch(
                "eegle.pipelines.dsart_recording.run_resting_baseline", return_value=baseline
            ), patch("eegle.pipelines.dsart_recording.run_dsart_child_session", return_value=failed_child):
                first = run_recording_suite(initial)
            self.assertEqual(first["status"], "partial")
            self.assertEqual(first["partial_recordings"][0]["session_dir"], "/recordings/session-1-partial")

            resumed = copy.copy(initial)
            object.__setattr__(resumed, "resume", True)
            with patch("eegle.pipelines.dsart_recording.run_recording_preflight", side_effect=preflight), patch(
                "eegle.pipelines.dsart_recording.run_resting_baseline"
            ) as baseline_runner, patch(
                "eegle.pipelines.dsart_recording.run_dsart_child_session", side_effect=completed_children
            ), patch("eegle.pipelines.dsart_recording.run_inter_session_break", return_value=break_result):
                second = run_recording_suite(resumed)
            self.assertFalse(baseline_runner.called)
            self.assertEqual(second["status"], "completed")
            self.assertEqual(second["dsart_session_1_directory"], "/recordings/session-1-retry")
            self.assertEqual(second["dsart_session_2_directory"], "/recordings/session-2")
            manifest = json.loads(Path(second["manifest_file"]).read_text())
            self.assertEqual(len(manifest["phases"]["dsart_session_1"]["attempts"]), 2)
            self.assertEqual(manifest["phases"]["baseline"]["status"], "completed")

    def test_resume_refuses_changed_effective_recording_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "record_dsart8.json"
            config = load_config(CONFIG_8)
            config_path.write_text(json.dumps(config), encoding="utf-8")
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=config_path,
                participant_id="unit",
                visit_id="visit-config-hash",
                task_mode="dry-run",
                trials_per_session=10,
                baseline_seconds=2.0,
                break_seconds=0.0,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )
            with patch(
                "eegle.pipelines.dsart_recording.run_recording_preflight",
                side_effect=self._passing_preflight,
            ), patch(
                "eegle.pipelines.dsart_recording.run_resting_baseline",
                side_effect=RuntimeError("stop after manifest creation"),
            ):
                first = run_recording_suite(options)
            self.assertEqual(first["status"], "failed")

            config["hardware"]["display"]["background_color"] = "grey"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            resumed = copy.copy(options)
            object.__setattr__(resumed, "resume", True)
            with self.assertRaisesRegex(ValueError, "configuration_hashes.recording_recipe"):
                run_recording_suite(resumed)

    def test_resume_refuses_task_or_eeg_mode_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            options = DsartRecordingOptions(
                recipe="dsart8",
                config_path=CONFIG_8,
                participant_id="unit",
                visit_id="visit-mode-identity",
                task_mode="dry-run",
                trials_per_session=10,
                baseline_seconds=2.0,
                break_seconds=0.0,
                record_eeg=False,
                require_eeg=False,
                output_root=tmp,
            )
            with patch(
                "eegle.pipelines.dsart_recording.run_recording_preflight",
                side_effect=self._passing_preflight,
            ), patch(
                "eegle.pipelines.dsart_recording.run_resting_baseline",
                side_effect=RuntimeError("stop after manifest creation"),
            ):
                first = run_recording_suite(options)
            self.assertEqual(first["status"], "failed")

            resumed = copy.copy(options)
            object.__setattr__(resumed, "resume", True)
            object.__setattr__(resumed, "task_mode", "psychopy")
            with self.assertRaisesRegex(ValueError, "task_mode"):
                run_recording_suite(resumed)

            object.__setattr__(resumed, "task_mode", "dry-run")
            object.__setattr__(resumed, "record_eeg", True)
            object.__setattr__(resumed, "require_eeg", True)
            with self.assertRaisesRegex(ValueError, "record_eeg"):
                run_recording_suite(resumed)

    def test_session_creation_does_not_reuse_same_second_directory(self) -> None:
        config = {"runtime": {}, "experiment": {"experiment_id": "unique", "task": "dynamic_sart"}}
        with tempfile.TemporaryDirectory() as tmp:
            first = create_session(config, participant_id="unit", root=tmp)
            second = create_session(config, participant_id="unit", root=tmp)
        self.assertNotEqual(first.root, second.root)
        self.assertTrue(second.root.name.endswith("-01"))

    def test_post_recording_analysis_failure_is_warning_not_rerecord_trigger(self) -> None:
        task_summary = {
            "experimental_trials": 600,
            "support_trials": 200,
            "query_trials": 400,
            "no_go_trial_count": 67,
            "aborted": False,
            "support_complete": True,
        }
        parameters = {
            "realtime": {"epoching": {
                "marker_prefix": "dynamic_sart_stimulus_onset",
                "tmin_seconds": -2.0,
                "tmax_seconds": -0.05,
                "timebase": "lsl",
                "include_practice_trials": False,
                "data_source": "raw",
            }}
        }
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "raw").mkdir()
            (session / "events").mkdir()
            (session / "raw" / "eeg_metadata.json").write_text(json.dumps({"status": "stopped"}))
            (session / "parameters.json").write_text(json.dumps(parameters))
            countdown_rows = [
                {"label": "dynamic_sart_countdown_start", "value": None, "timestamp": 10.0},
                *[
                    {
                        "label": f"dynamic_sart_countdown_step__step={index}",
                        "value": value,
                        "timestamp": 10.0 + index,
                    }
                    for index, value in enumerate(["5", "4", "3", "2", "1", "GO!"], start=1)
                ],
                {"label": "dynamic_sart_countdown_end", "value": None, "timestamp": 17.0},
                {
                    "label": "dynamic_sart_stimulus_onset__practice=0",
                    "value": None,
                    "timestamp": 17.0,
                    "trial": 1,
                    "metadata": {"practice": False, "phase": "support"},
                },
            ]
            (session / "events" / "events.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in countdown_rows),
                encoding="utf-8",
            )
            validation = _child_session_validation(
                session,
                task_summary,
                {"normal_recipe_trial_count": 600},
                record_eeg=False,
                dynamic_report={},
                analysis_error="RuntimeError: report failed",
            )
        self.assertEqual(validation["failures"], [])
        self.assertEqual(validation["status"], "warning")
        self.assertTrue(any("raw recording was retained" in warning for warning in validation["warnings"]))

    def test_countdown_integrity_ignores_practice_onsets_before_countdown(self) -> None:
        rows = [
            {
                "label": "dynamic_sart_stimulus_onset__trial=-1__phase=practice__practice=1",
                "timestamp": 1.0,
                "trial": -1,
                "metadata": {"practice": True, "phase": "practice"},
            },
            {
                "label": "dynamic_sart_stimulus_onset__practice=1",
                "timestamp": 1.5,
            },
            {"label": "dynamic_sart_countdown_start", "timestamp": 2.0},
            *[
                {
                    "label": f"dynamic_sart_countdown_step__step={index}",
                    "value": value,
                    "timestamp": 2.0 + index,
                }
                for index, value in enumerate(["5", "4", "3", "2", "1", "GO!"], start=1)
            ],
            {"label": "dynamic_sart_countdown_end", "timestamp": 9.0},
            {
                "label": "dynamic_sart_stimulus_onset__trial=1__phase=support__practice=0",
                "timestamp": 9.0,
                "trial": 1,
                "metadata": {"practice": False, "phase": "support"},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "events").mkdir()
            (session / "events" / "events.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            result = _countdown_event_integrity(session)

        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["practice_stimulus_onset_count"], 2)
        self.assertEqual(result["experimental_stimulus_onset_count"], 1)
        self.assertTrue(result["event_order_valid"])

    def test_countdown_integrity_rejects_real_experimental_onset_before_end(self) -> None:
        rows = [
            {"label": "dynamic_sart_countdown_start", "timestamp": 2.0},
            *[
                {
                    "label": f"dynamic_sart_countdown_step__step={index}",
                    "value": value,
                    "timestamp": 2.0 + index,
                }
                for index, value in enumerate(["5", "4", "3", "2", "1", "GO!"], start=1)
            ],
            {
                "label": "dynamic_sart_stimulus_onset__trial=1__phase=support__practice=0",
                "timestamp": 8.5,
                "trial": 1,
                "metadata": {"practice": False, "phase": "support"},
            },
            {"label": "dynamic_sart_countdown_end", "timestamp": 9.0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp)
            (session / "events").mkdir()
            (session / "events" / "events.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            result = _countdown_event_integrity(session)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(
            any("after the first experimental stimulus" in row for row in result["failures"])
        )
        self.assertFalse(result["event_order_valid"])

    def test_signal_probe_reports_native_units_and_flat_channels(self) -> None:
        samples = [[0.0, float(index)] for index in range(32)]
        timestamps = [index / 500.0 for index in range(32)]
        quality = _eeg_probe_quality(samples, timestamps, ["Fz", "Cz"], 500.0, {})
        self.assertEqual(quality["signal_units"], "native_lsl_units")
        self.assertTrue(quality["timestamps_strictly_increasing"])
        self.assertEqual(quality["channels"][0]["status"], "warning")
        self.assertIn("flat_channel", quality["channels"][0]["warnings"])
        self.assertEqual(quality["channels"][1]["status"], "good")
        self.assertAlmostEqual(quality["effective_sample_rate_hz"], 500.0)
        self.assertAlmostEqual(quality["sample_fraction_of_expected_from_timestamp_span"], 1.0)

    def test_preflight_warnings_require_explicit_live_operator_acceptance(self) -> None:
        options = DsartRecordingOptions(
            recipe="dsart32",
            config_path=CONFIG_32,
            participant_id="unit",
            task_mode="psychopy",
            trials_per_session=10,
            record_eeg=True,
            require_eeg=True,
            electrodes_confirmed=True,
        )
        report = {
            "phase": "initial_preflight",
            "status": "warning",
            "warnings": ["Measured EEG rate is 480 Hz; expected about 500 Hz"],
            "channel_contract": {"status": "ok"},
        }
        with patch("builtins.input", return_value="YES"):
            _accept_recording_preflight(report, options)

        self.assertTrue(report["operator_acceptance"]["accepted"])
        self.assertTrue(report["operator_acceptance"]["warnings_accepted"])
        self.assertEqual(report["operator_acceptance"]["warning_count"], 1)

    def test_post_recording_warnings_require_explicit_live_acceptance(self) -> None:
        options = DsartRecordingOptions(
            recipe="dsart32",
            config_path=CONFIG_32,
            participant_id="unit",
            task_mode="psychopy",
            trials_per_session=10,
            record_eeg=True,
            require_eeg=True,
        )
        result = {"warnings": ["CSV mirror failed; XDF was retained"]}
        with patch("builtins.input", return_value="YES"):
            _accept_post_recording_warnings(result, options, "DSART session 1")

        self.assertTrue(result["operator_warning_acceptance"]["accepted"])

    def test_preflight_acceptance_report_rewrite_failure_is_nonfatal(self) -> None:
        options = DsartRecordingOptions(
            recipe="dsart32",
            config_path=CONFIG_32,
            participant_id="unit",
            task_mode="psychopy",
            trials_per_session=10,
            record_eeg=True,
            require_eeg=True,
            electrodes_confirmed=True,
        )
        report = {
            "phase": "initial_preflight",
            "status": "warning",
            "warnings": ["XDF signal quality warning"],
            "channel_contract": {"status": "ok"},
            "report_file": "/blocked/preflight.json",
        }
        with patch("builtins.input", return_value="YES"), patch(
            "eegle.pipelines.dsart_recording._write_json_atomic",
            side_effect=PermissionError("acceptance policy lock"),
        ):
            _accept_recording_preflight(report, options)

        self.assertTrue(report["operator_acceptance"]["accepted"])
        self.assertTrue(report["acceptance_persistence_warnings"])

    def test_signal_probe_excludes_reserved_trigger_status_from_quality_warnings(self) -> None:
        samples = [[float(index), 0.0] for index in range(32)]
        timestamps = [index / 1000.0 for index in range(32)]
        quality = _eeg_probe_quality(
            samples,
            timestamps,
            ["O2", "TRIGGER_STATUS"],
            1000.0,
            {"quality_excluded_channel_names": ["TRIGGER_STATUS"]},
        )

        self.assertEqual(quality["channels"][1]["status"], "excluded")
        self.assertEqual(quality["channels"][1]["warnings"], [])
        self.assertNotIn("TRIGGER_STATUS", quality["warning_channels"])

    def test_sample_contract_warns_on_rate_and_timestamp_issues(self) -> None:
        probe = {
            "status": "ok",
            "sample_count": 100,
            "probe_seconds": 3.0,
            "stream": {"nominal_srate": 250.0},
            "quality": {
                "sample_rate_hz": 250.0,
                "timestamps_finite": True,
                "timestamps_strictly_increasing": False,
                "maximum_timestamp_gap_seconds": 0.02,
            },
        }
        result = assess_sample_probe(
            probe,
            {"expected_sample_rate_hz": 500, "sample_probe_seconds": 3.0},
            require_eeg=True,
        )
        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["failures"], [])
        self.assertTrue(any("rate" in warning for warning in result["warnings"]))
        self.assertTrue(any("strictly increasing" in warning for warning in result["warnings"]))

    def test_sample_contract_still_fails_when_required_stream_has_no_samples(self) -> None:
        result = assess_sample_probe(
            {"status": "missing", "sample_count": 0},
            {"expected_sample_rate_hz": 500, "sample_probe_seconds": 3.0},
            require_eeg=True,
        )

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("did not receive" in failure for failure in result["failures"]))

    def test_recorder_monitor_warns_for_stall_and_detects_process_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status_path = Path(tmp) / "recorder.status.json"
            status_path.write_text(json.dumps({"status": "recording", "summary": {"sample_count": 100}}))
            monitor = RecorderHealthMonitor(status_path, required=True, stall_timeout_seconds=1.0)
            self.assertTrue(monitor.check().ok)
            monitor._last_progress_at -= 2.0
            stalled = monitor.check()
            self.assertTrue(stalled.ok)
            self.assertTrue(stalled.warning)
            status_path.write_text(json.dumps({"status": "failed", "summary": {"sample_count": 100}}))
            failed = monitor.check()
        self.assertFalse(failed.ok)
        self.assertIn("failed", str(failed.reason))

    def test_recorder_monitor_warns_for_stale_status_while_csv_still_advances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "recorder.status.json"
            raw_path = root / "eeg.csv"
            raw_path.write_text("header\n", encoding="utf-8")
            payload = {
                "status": "recording",
                "summary": {
                    "sample_count": 100,
                    "csv_mirror": {"raw_file": str(raw_path)},
                },
            }
            status_path.write_text(json.dumps(payload), encoding="utf-8")
            monitor = RecorderHealthMonitor(
                status_path,
                required=True,
                stall_timeout_seconds=1.0,
                status_stale_seconds=1.0,
            )
            self.assertTrue(monitor.check().ok)
            raw_path.write_text("header\n1,2,3\n", encoding="utf-8")
            stale_time = status_path.stat().st_mtime - 10.0
            os.utime(status_path, (stale_time, stale_time))

            warning = monitor.check()
            repeated = monitor.check()
            monitor._last_progress_at -= 2.0
            stalled = monitor.check()

        self.assertTrue(warning.ok)
        self.assertTrue(warning.warning)
        self.assertIn("still advancing", str(warning.reason))
        self.assertTrue(repeated.ok)
        self.assertFalse(repeated.warning)
        self.assertTrue(stalled.ok)
        self.assertTrue(stalled.warning)
        self.assertIn("has not advanced", str(stalled.reason))

    def test_recorder_monitor_does_not_abort_xdf_when_csv_mirror_is_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_path = root / "recorder.status.json"
            xdf_path = root / "recording.xdf"
            xdf_path.write_bytes(b"XDF:test")
            status_path.write_text(
                json.dumps(
                    {
                        "status": "recording",
                        "summary": {
                            "primary_format": "xdf",
                            "xdf_file": str(xdf_path),
                            "sample_count": 0,
                            "csv_mirror": {"status": "failed"},
                            "csv_mirror_warning": "CSV mirror failed; XDF acquisition continues",
                        },
                    }
                ),
                encoding="utf-8",
            )
            monitor = RecorderHealthMonitor(status_path, required=True, stall_timeout_seconds=1.0)
            first = monitor.check()
            monitor._last_progress_at -= 2.0
            second = monitor.check()

        self.assertTrue(first.ok)
        self.assertFalse(first.warning)
        self.assertTrue(second.ok)
        self.assertTrue(second.warning)
        self.assertIn("not advanced", str(second.reason))

    def test_raw_row_preserves_amplitudes_and_both_lsl_timestamps(self) -> None:
        sample = [1.25, -2.5, 3.75]
        row, corrected = _recorded_eeg_row(
            sample,
            source_timestamp=100.0,
            received_time=200.0,
            time_correction=0.125,
            source_preserving=True,
        )
        self.assertEqual(corrected, 100.125)
        self.assertEqual(float(row[0]), 100.125)
        self.assertEqual(float(row[2]), 100.0)
        self.assertEqual(float(row[3]), 0.125)
        self.assertEqual(row[4:], sample)
        self.assertEqual(
            eeg_channel_columns([
                "lsl_timestamp", "local_received_time", "source_lsl_timestamp",
                "lsl_time_correction_seconds", "Fz", "Cz", "Pz",
            ]),
            ["Fz", "Cz", "Pz"],
        )

    def test_source_preserving_inlet_disables_lsl_timestamp_processing(self) -> None:
        class FakePylsl:
            proc_none = 0
            proc_clocksync = 1
            proc_dejitter = 2
            proc_monotonize = 4

        self.assertEqual(
            _eeg_inlet_processing_flags(FakePylsl, {"recording_lsl_processing": "source_preserving"}),
            0,
        )

    def test_raw_recorder_closes_lsl_inlet_after_chunk_failure(self) -> None:
        class Info:
            def channel_count(self) -> int:
                return 1

            def desc(self):
                raise RuntimeError("no metadata")

        class Inlet:
            closed = False

            def __init__(self, *_args, **_kwargs) -> None:
                return None

            def open_stream(self, **_kwargs) -> None:
                return None

            def time_correction(self, **_kwargs) -> float:
                return 0.001

            def pull_chunk(self, **_kwargs):
                raise OSError("simulated LSL read failure")

            def close_stream(self) -> None:
                Inlet.closed = True

        pylsl = ModuleType("pylsl")
        pylsl.StreamInlet = Inlet
        pylsl.proc_none = 0
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"pylsl": pylsl}), patch(
            "eegle.devices.lsl_eeg._select_lsl_info",
            return_value=(Info(), {"name": "unit-eeg", "channel_count": 1}),
        ):
            recorder = LslEegRecorder(
                {"recording_lsl_processing": "source_preserving"},
                Path(tmp) / "eeg.csv",
                Path(tmp) / "eeg_metadata.json",
            )
            recorder._record()
            summary = recorder.snapshot()
        self.assertTrue(Inlet.closed)
        self.assertEqual(summary["status"], "failed")
        self.assertIn("simulated LSL read failure", summary["error"])

    def test_recorder_parent_watchdog_detects_reparenting(self) -> None:
        with patch("eegle.workers.recorder.os.getppid", return_value=999):
            self.assertTrue(_manager_process_disappeared(123))
            self.assertFalse(_manager_process_disappeared(999))
            self.assertFalse(_manager_process_disappeared(1))


if __name__ == "__main__":
    unittest.main()
