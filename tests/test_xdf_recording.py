from __future__ import annotations

import csv
import importlib.metadata
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from time import monotonic
from unittest.mock import patch

import numpy as np

from eegle.devices.labrecorder_xdf import (
    LabRecorderXdfRecorder,
    build_labrecorder_config,
    labrecorder_environment,
    required_labrecorder_streams,
)
from eegle.devices.xdf_integrity import validate_xdf_recording
from eegle.feedback_manager import normalize_processes
from eegle.hardware.neuracle import NEURACLE_W64_LSL_CHANNELS
from eegle.session import paths_for_existing_session


CHANNELS = list(NEURACLE_W64_LSL_CHANNELS)


class _Mirror:
    def __init__(self) -> None:
        self.status = "initialized"
        self.sample_count = 0

    def start(self) -> None:
        self.status = "recording"
        self.sample_count = 20

    def wait_until_ready(self, timeout: float) -> bool:
        return True

    def snapshot(self) -> dict[str, object]:
        return {
            "status": self.status,
            "sample_count": self.sample_count,
            "first_lsl_timestamp": 1.0,
            "last_lsl_timestamp": 2.0,
            "stream": {
                "name": "Neuracle EEG",
                "type": "EEG",
                "source_id": "neuracle-test",
                "hostname": "ACQ-PC",
                "channel_count": 65,
                "nominal_srate": 1000.0,
            },
        }

    def stop(self) -> dict[str, object]:
        if self.status != "failed":
            self.status = "stopped"
        return self.snapshot()


class _Process:
    def __init__(self) -> None:
        self.pid = 1234
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float) -> int:
        return int(self.returncode or 0)


class _Socket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def sendall(self, value: bytes) -> None:
        self.sent.append(value.decode("utf-8").strip())

    def close(self) -> None:
        return None


class _EmptyMirror(_Mirror):
    def start(self) -> None:
        self.status = "recording"
        self.sample_count = 0


class ManagedXdfTests(unittest.TestCase):
    def _config(self, executable: str) -> dict[str, object]:
        return {
            "hardware": {
                "eeg": {"stream_timeout_seconds": 0.1},
                "markers": {
                    "lsl_stream_name": "EEGleMarkers",
                    "lsl_stream_type": "Markers",
                    "source_id": "marker-test",
                },
            },
            "processes": {
                "recorder": {
                    "enabled": True,
                    "backend": "labrecorder_xdf",
                    "csv_mirror": True,
                    "executable": executable,
                    "rcs_port": 22345,
                    "startup_timeout_seconds": 20.0,
                    "shutdown_timeout_seconds": 15.0,
                    "xdf_stall_timeout_seconds": 15.0,
                    "tail_guard_seconds": 0.0,
                }
            },
        }

    def test_config_normalization_retains_managed_xdf_contract(self) -> None:
        normalized = normalize_processes(self._config("C:/LSL/LabRecorder.exe"))

        self.assertEqual(normalized["recorder"]["backend"], "labrecorder_xdf")
        self.assertTrue(normalized["recorder"]["csv_mirror"])
        self.assertEqual(normalized["recorder"]["executable"], "C:/LSL/LabRecorder.exe")
        self.assertEqual(normalized["recorder"]["rcs_port"], 22345)
        self.assertNotIn("shutdown_timeout_seconds", normalized["recorder"])
        self.assertEqual(normalized["recorder"]["finalization_status_interval_seconds"], 5.0)
        self.assertEqual(normalized["recorder"]["tail_guard_seconds"], 0.0)

    def test_labrecorder_config_preserves_spaces_and_required_streams(self) -> None:
        with tempfile.TemporaryDirectory(prefix="xdf path ") as tmp:
            rendered = build_labrecorder_config(
                Path(tmp),
                ["Neuracle EEG (ACQ-PC)", "EEGleMarkers"],
                rcs_port=22345,
            )

        self.assertIn("StudyRoot=", rendered)
        self.assertIn("xdf path ", rendered)
        self.assertIn('RequiredStreams="Neuracle EEG (ACQ-PC)","EEGleMarkers"', rendered)
        self.assertIn("RCSEnabled=1", rendered)
        self.assertIn("RCSPort=22345", rendered)

    def test_required_stream_contract_uses_exact_eeg_and_marker_hosts(self) -> None:
        required = required_labrecorder_streams(
            {"name": "Neuracle EEG", "hostname": "ACQ-PC"},
            {"name": "EEGleMarkers", "hostname": "TASK-PC"},
        )
        self.assertEqual(required, ["Neuracle EEG (ACQ-PC)", "EEGleMarkers (TASK-PC)"])

    def test_managed_lifecycle_sends_remote_commands_and_finalizes_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = paths_for_existing_session(tmp)
            executable = Path(tmp) / "LabRecorder.exe"
            executable.write_bytes(b"test executable")
            recorder = LabRecorderXdfRecorder(self._config(str(executable)), paths)
            recorder._mirror = _Mirror()  # type: ignore[assignment]
            process = _Process()
            remote = _Socket()

            def create_xdf() -> None:
                paths.eeg_xdf.write_bytes(b"XDF:test-data")
                recorder._last_xdf_size = paths.eeg_xdf.stat().st_size
                recorder._last_xdf_growth_at = monotonic()

            with patch(
                "eegle.devices.labrecorder_xdf.require_loopback_port_available"
            ), patch(
                "eegle.devices.labrecorder_xdf.resolve_labrecorder_marker_stream",
                return_value={
                    "name": "EEGleMarkers",
                    "type": "Markers",
                    "source_id": "marker-test",
                    "hostname": "TASK-PC",
                    "uid": "marker-uid",
                },
            ), patch(
                "eegle.devices.labrecorder_xdf.subprocess.Popen",
                return_value=process,
            ) as popen, patch.object(
                recorder,
                "_connect_rcs",
                return_value=remote,
            ), patch.object(
                recorder,
                "_wait_for_xdf_growth",
                side_effect=create_xdf,
            ), patch.object(
                recorder,
                "_wait_for_xdf_settle",
            ):
                started = recorder.start()
                stopped = recorder.stop(reason="unit_test")

            self.assertEqual(started["status"], "recording")
            self.assertEqual(stopped["status"], "stopped")
            self.assertEqual(remote.sent[0], "update")
            self.assertTrue(remote.sent[1].startswith("filename "))
            self.assertEqual(remote.sent[2:], ["start", "stop"])
            self.assertEqual(stopped["sample_count"], 20)
            self.assertEqual(
                popen.call_args.args[0],
                [str(executable.resolve()), "--config", str(paths.process_logs / "labrecorder.cfg")],
            )
            self.assertTrue(paths.xdf_metadata.exists())
            self.assertTrue(paths.eeg_xdf.exists())
            self.assertFalse(process.killed)
            metadata = json.loads(paths.xdf_metadata.read_text(encoding="utf-8"))
            self.assertIn("RequiredStreams=", metadata["labrecorder_config_contents"])
            self.assertEqual(metadata["labrecorder_launch_command"], popen.call_args.args[0])

    def test_recorder_holds_tail_guard_before_sending_labrecorder_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = paths_for_existing_session(tmp)
            config = self._config("LabRecorder.exe")
            config["processes"]["recorder"]["tail_guard_seconds"] = 1.0  # type: ignore[index]
            recorder = LabRecorderXdfRecorder(config, paths)
            recorder._mirror = _Mirror()  # type: ignore[assignment]
            recorder._mirror.start()
            recorder._process = _Process()  # type: ignore[assignment]
            recorder._rcs_socket = _Socket()  # type: ignore[assignment]
            recorder._status = "recording"
            order = []
            with patch(
                "eegle.devices.labrecorder_xdf.sleep",
                side_effect=lambda seconds: order.append(("tail", seconds)),
            ), patch.object(
                recorder,
                "_send",
                side_effect=lambda command: order.append(("command", command)),
            ), patch.object(recorder, "_wait_for_xdf_settle"):
                summary = recorder.stop(reason="unit_test")

        self.assertEqual(order[:2], [("tail", 1.0), ("command", "stop")])
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["tail_guard_seconds"], 1.0)

    def test_startup_requires_csv_samples_before_launching_labrecorder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = paths_for_existing_session(tmp)
            recorder = LabRecorderXdfRecorder(self._config("LabRecorder.exe"), paths)
            recorder._mirror = _EmptyMirror()  # type: ignore[assignment]
            with patch(
                "eegle.devices.labrecorder_xdf.monotonic",
                side_effect=[0.0, 3.0],
            ):
                with self.assertRaisesRegex(TimeoutError, "did not receive any EEG samples"):
                    recorder._start_mirror()

    def test_environment_preflight_records_executable_hash_and_pyxdf_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "LabRecorder.exe"
            executable.write_bytes(b"labrecorder")
            with patch(
                "eegle.devices.labrecorder_xdf.importlib.metadata.version",
                return_value="1.17.5",
            ), patch("eegle.devices.labrecorder_xdf.require_loopback_port_available"):
                environment = labrecorder_environment(self._config(str(executable)))

        self.assertEqual(environment["executable"], str(executable.resolve()))
        self.assertEqual(environment["pyxdf_version"], "1.17.5")
        self.assertEqual(len(environment["executable_sha256"]), 64)

    def test_environment_preflight_reports_missing_pyxdf_actionably(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "LabRecorder.exe"
            executable.write_bytes(b"labrecorder")
            with patch(
                "eegle.devices.labrecorder_xdf.importlib.metadata.version",
                side_effect=importlib.metadata.PackageNotFoundError("pyxdf"),
            ):
                with self.assertRaisesRegex(RuntimeError, "pyxdf is not installed"):
                    labrecorder_environment(self._config(str(executable)))

    def test_existing_xdf_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = paths_for_existing_session(tmp)
            paths.eeg_xdf.write_bytes(b"XDF:existing")
            recorder = LabRecorderXdfRecorder(self._config("LabRecorder.exe"), paths)
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                recorder.start()

    def test_xdf_metadata_report_failure_does_not_stop_primary_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = paths_for_existing_session(tmp)
            recorder = LabRecorderXdfRecorder(self._config("LabRecorder.exe"), paths)
            recorder._status = "recording"
            recorder._process = _Process()  # type: ignore[assignment]
            with patch(
                "eegle.devices.labrecorder_xdf._write_json_atomic",
                side_effect=PermissionError("metadata policy lock"),
            ):
                recorder._write_metadata()

            status = recorder.snapshot()

        self.assertEqual(status["status"], "recording")
        self.assertIn("primary XDF acquisition continues", status["xdf_metadata_warning"])

    def test_snapshot_warns_when_xdf_growth_stalls_but_csv_mirror_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = paths_for_existing_session(tmp)
            paths.eeg_xdf.write_bytes(b"XDF:test")
            recorder = LabRecorderXdfRecorder(self._config("LabRecorder.exe"), paths)
            recorder._mirror = _Mirror()  # type: ignore[assignment]
            recorder._mirror.start()
            recorder._process = _Process()  # type: ignore[assignment]
            recorder._status = "recording"
            recorder._last_xdf_size = paths.eeg_xdf.stat().st_size
            recorder._last_xdf_growth_at = monotonic() - 20.0

            status = recorder.snapshot()

            self.assertEqual(status["status"], "recording")
            self.assertEqual(status["xdf_growth_status"], "buffering_warning")
            self.assertEqual(status["xdf_growth_warning_count"], 1)
            self.assertIsNone(status["error"])
            self.assertTrue(any("buffering" in note for note in status["notes"]))

    def test_snapshot_fails_when_labrecorder_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = paths_for_existing_session(tmp)
            paths.eeg_xdf.write_bytes(b"XDF:test")
            recorder = LabRecorderXdfRecorder(self._config("LabRecorder.exe"), paths)
            recorder._mirror = _Mirror()  # type: ignore[assignment]
            recorder._mirror.start()
            process = _Process()
            process.returncode = 3
            recorder._process = process  # type: ignore[assignment]
            recorder._status = "recording"

            status = recorder.snapshot()

            self.assertEqual(status["status"], "failed")
            self.assertIn("return code 3", str(status["error"]))

    def test_snapshot_warns_when_csv_mirror_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = paths_for_existing_session(tmp)
            paths.eeg_xdf.write_bytes(b"XDF:test")
            recorder = LabRecorderXdfRecorder(self._config("LabRecorder.exe"), paths)
            recorder._mirror = _Mirror()  # type: ignore[assignment]
            recorder._mirror.status = "failed"
            recorder._process = _Process()  # type: ignore[assignment]
            recorder._status = "recording"

            status = recorder.snapshot()

            self.assertEqual(status["status"], "recording")
            self.assertTrue(status["csv_mirror_degraded"])
            self.assertIn("CSV mirror status", str(status["csv_mirror_warning"]))
            self.assertIsNone(status["error"])


class XdfIntegrityTests(unittest.TestCase):
    def _session(
        self,
        root: Path,
        *,
        rate: float = 1000.0,
        channel_count: int = 65,
        labels: list[str] | None = None,
        eeg_stamps: list[float] | None = None,
        eeg_values: np.ndarray | None = None,
    ) -> tuple[object, Path]:
        paths = paths_for_existing_session(root)
        paths.eeg_xdf.write_bytes(b"XDF:test-data")
        parameters = {
            "hardware": {
                "eeg": {
                    "family": "Neuracle",
                    "profile": "neuracle64",
                    "expected_channel_counts": [65],
                    "expected_channel_names": CHANNELS,
                    "expected_sample_rate_hz": 1000.0,
                    "maximum_timestamp_gap_seconds": 0.1,
                },
                "markers": {"source_id": "marker-test"},
            },
            "processes": {"recorder": {"backend": "labrecorder_xdf", "csv_mirror": True}},
        }
        paths.parameters.write_text(json.dumps(parameters), encoding="utf-8")
        paths.xdf_metadata.write_text(
            json.dumps({"selected_eeg_stream": {"name": "Neuracle EEG", "source_id": "eeg-test"}}),
            encoding="utf-8",
        )
        with (paths.raw / "lsl_markers_received.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["marker_label", "lsl_timestamp", "local_received_lsl_timestamp"])
            writer.writerow(["task_start", "1.0005", "1.0005"])
            writer.writerow(["task_end", "1.0015", "1.0015"])
        return paths, self._fake_pyxdf(
            rate=rate,
            channel_count=channel_count,
            labels=labels,
            eeg_stamps=eeg_stamps,
            eeg_values=eeg_values,
        )

    def _fake_pyxdf(
        self,
        *,
        rate: float,
        channel_count: int = 65,
        labels: list[str] | None = None,
        eeg_stamps: list[float] | None = None,
        eeg_values: np.ndarray | None = None,
    ) -> object:
        labels = list(labels or CHANNELS[:channel_count])
        eeg_stamps = list(eeg_stamps or [1.0, 1.001, 1.002])
        if eeg_values is None:
            eeg_matrix = (
                np.arange(len(eeg_stamps), dtype=np.float32)[:, None]
                + np.arange(channel_count, dtype=np.float32)[None, :] / 100.0
            )
        else:
            eeg_matrix = np.asarray(eeg_values, dtype=np.float32)
        eeg_header = {
            "stream_id": 1,
            "name": "Neuracle EEG",
            "type": "EEG",
            "source_id": "eeg-test",
            "channel_count": channel_count,
            "channel_format": "float32",
            "nominal_srate": rate,
        }
        marker_header = {
            "stream_id": 2,
            "name": "EEGleMarkers",
            "type": "Markers",
            "source_id": "marker-test",
            "channel_count": 1,
            "channel_format": "string",
            "nominal_srate": 0.0,
        }
        module = types.SimpleNamespace()
        module.resolve_streams = lambda filename: [eeg_header, marker_header]

        def load_xdf(filename: str, *, on_chunk: object, **kwargs: object) -> tuple[list[dict[str, object]], dict[str, object]]:
            channel_rows = [{"label": [name]} for name in labels]
            eeg_info = {"stream_id": 1, "desc": [{"channels": [{"channel": channel_rows}]}]}
            marker_info = {"stream_id": 2, "desc": [{}]}
            returned_eeg_values, returned_eeg_stamps, _ = on_chunk(  # type: ignore[operator]
                eeg_matrix,
                np.asarray(eeg_stamps),
                {"info": eeg_info},
                1,
            )
            marker_values, returned_marker_stamps, _ = on_chunk(  # type: ignore[operator]
                [["task_start"], ["task_end"]],
                np.asarray([1.0005, 1.0015]),
                {"info": marker_info},
                2,
            )
            return [
                {
                    "info": eeg_info,
                    "time_series": returned_eeg_values,
                    "time_stamps": returned_eeg_stamps,
                },
                {"info": marker_info, "time_series": marker_values, "time_stamps": returned_marker_stamps},
            ], {}

        module.load_xdf = load_xdf
        return module

    def test_bounded_xdf_validation_checks_streams_timestamps_and_marker_parity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp))
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["eeg"]["sample_count"], 3)
            self.assertEqual(
                result["eeg"]["mapped_channel_types"][-6:],
                ["ecg", "eog", "eog", "eog", "eog", "stim"],
            )
            self.assertTrue(result["markers"]["sequence_matches_receipt"])
            self.assertEqual(
                result["recording_coverage"]["clock_normalization"]["schema"],
                "eegle.xdf_clock_normalization.v1",
            )
            self.assertAlmostEqual(result["eeg"]["normalized_first_timestamp"], -0.0005)
            self.assertEqual(result["markers"]["normalized_first_timestamp"], 0.0)
            metadata = json.loads(paths.xdf_metadata.read_text(encoding="utf-8"))
            self.assertEqual(metadata["validation"]["status"], "pass")

    def test_optional_missing_xdf_is_reported_as_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = paths_for_existing_session(tmp)

            result = validate_xdf_recording(paths.root, required=False)

        self.assertEqual(result["status"], "skipped")
        self.assertTrue(result["skipped"])
        self.assertIn("not requested", result["skip_reason"])
        self.assertEqual(result["failures"], [])

    def test_xdf_validation_report_write_failure_does_not_invalidate_raw_xdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp))
            with patch.dict(sys.modules, {"pyxdf": pyxdf}), patch(
                "eegle.devices.xdf_integrity._write_json_atomic",
                side_effect=PermissionError("report policy lock"),
            ):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["failures"], [])
        self.assertTrue(any("raw XDF was retained" in row for row in result["warnings"]))

    def test_xdf_validation_recovers_unique_stream_identity_without_metadata_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp))
            paths.xdf_metadata.unlink()
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["failures"], [])
        self.assertTrue(any("recovered from the XDF header" in row for row in result["warnings"]))

    def test_xdf_validation_warns_on_wrong_sample_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), rate=500.0)
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

            self.assertEqual(result["status"], "warning")
            self.assertEqual(result["failures"], [])
            self.assertTrue(any("rate" in warning for warning in result["warnings"]))

    def test_xdf_validation_rejects_marker_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp))
            receipt = paths.raw / "lsl_markers_received.csv"
            receipt.write_text(
                "marker_label,lsl_timestamp,local_received_lsl_timestamp\nwrong,1.0,1.0\n",
                encoding="utf-8",
            )
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

            self.assertEqual(result["status"], "fail")
            self.assertTrue(any("marker labels/order" in failure for failure in result["failures"]))

    def test_xdf_validation_rejects_wrong_channel_count_and_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), channel_count=63)
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                count_result = validate_xdf_recording(paths.root, required=True)

        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), labels=list(reversed(CHANNELS)))
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                order_result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(count_result["status"], "fail")
        self.assertTrue(any("channel count" in failure for failure in count_result["failures"]))
        self.assertEqual(order_result["status"], "fail")
        self.assertTrue(any("labels/order" in failure for failure in order_result["failures"]))
        self.assertTrue(any("value 1:" in failure for failure in order_result["failures"]))

    def test_xdf_validation_accepts_safe_mixed_generic_and_semantic_labels(self) -> None:
        mixed = list(CHANNELS)
        mixed[0] = "ch_001"
        mixed[-1] = "Trigger"
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), labels=mixed)
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["eeg"]["mapped_channel_names"], CHANNELS)
        self.assertEqual(
            result["eeg"]["channel_mapping_source"],
            "config:expected_channel_names:mixed_positional",
        )

    def test_operator_confirmed_position_and_csv_identity_resolve_xdf_descriptor_difference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), labels=list(reversed(CHANNELS)))
            parameters = json.loads(paths.parameters.read_text(encoding="utf-8"))
            parameters["hardware"]["eeg"]["mapping_source"] = (
                "operator_confirmed_neuracle_w64_65_value_lsl_order"
            )
            paths.parameters.write_text(json.dumps(parameters), encoding="utf-8")
            (paths.raw / "eeg_metadata.json").write_text(
                json.dumps(
                    {
                        "status": "stopped",
                        "stream": {
                            "name": "Neuracle EEG",
                            "type": "EEG",
                            "source_id": "eeg-test",
                            "channel_count": 65,
                            "channel_names": CHANNELS,
                            "channel_value_order_changed": False,
                        },
                        "raw_sample_contract": {"channel_value_order_modified": False},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["eeg"]["mapped_channel_names"], CHANNELS)
        self.assertEqual(
            result["eeg"]["channel_mapping_source"],
            "operator_confirmed_position+csv_mirror_identity",
        )
        self.assertEqual(
            result["eeg"]["confirmed_positional_mapping_evidence"]["status"],
            "pass",
        )

    def test_xdf_validation_warns_on_timestamp_gap_and_estimated_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), eeg_stamps=[1.0, 1.001, 1.5])
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["failures"], [])
        self.assertGreater(result["eeg"]["estimated_missing_samples"], 0)
        self.assertTrue(any("timestamp gap" in warning for warning in result["warnings"]))

    def test_xdf_validation_warns_for_nonfinite_flatline_and_clipping(self) -> None:
        stamps = [1.0 + index / 1000.0 for index in range(100)]
        values = (
            np.arange(100, dtype=np.float32)[:, None]
            + np.arange(65, dtype=np.float32)[None, :] / 100.0
        )
        values[10, 0] = np.nan
        values[11, 0] = np.inf
        values[:, 1] = 0.0
        values[:10, 2] = -100.0
        values[-10:, 2] = 100.0
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(
                Path(tmp),
                eeg_stamps=stamps,
                eeg_values=values,
            )
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["eeg"]["signal_quality"]["nonfinite_value_count"], 2)
        by_name = {
            row["channel_name"]: row
            for row in result["eeg"]["signal_quality"]["channels"]
        }
        self.assertIn("non_finite_samples", by_name[CHANNELS[0]]["warnings"])
        self.assertIn("flat_channel", by_name[CHANNELS[1]]["warnings"])
        self.assertIn("possible_clipping", by_name[CHANNELS[2]]["warnings"])

    def test_short_xdf_finalization_tail_is_warning_when_csv_proves_full_coverage(self) -> None:
        stamps = [index * 0.05 for index in range(10)]
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), eeg_stamps=stamps)
            streams = list(pyxdf.resolve_streams(str(paths.eeg_xdf)))  # type: ignore[attr-defined]
            streams[0]["hostname"] = "ACQ-PC"
            streams[1]["hostname"] = "ACQ-PC"
            pyxdf.resolve_streams = lambda filename: streams  # type: ignore[attr-defined]
            (paths.raw / "eeg_metadata.json").write_text(
                json.dumps(
                    {
                        "status": "stopped",
                        "sample_count": 2000,
                        "last_lsl_timestamp": 1.1,
                        "first_source_lsl_timestamp": 0.0,
                        "last_source_lsl_timestamp": 1.1,
                        "first_local_received_lsl_timestamp": 0.0,
                        "last_local_received_lsl_timestamp": 1.1,
                        "timestamp_gap_count": 0,
                        "nonmonotonic_timestamp_count": 0,
                        "stream": {
                            "name": "Neuracle EEG",
                            "type": "EEG",
                            "source_id": "eeg-test",
                            "hostname": "ACQ-PC",
                            "channel_count": 65,
                        },
                        "raw_sample_contract": {
                            "source_timestamp_retained": True,
                            "amplitude_samples_modified": False,
                            "channel_value_order_modified": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (paths.raw / "lsl_markers_received_metadata.json").write_text(
                json.dumps(
                    {
                        "status": "stopped",
                        "received_count": 2,
                        "last_lsl_timestamp": 1.0015,
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["failures"], [])
        self.assertTrue(any("CSV mirror" in warning for warning in result["warnings"]))
        self.assertEqual(result["recording_coverage"]["status"], "warning")
        self.assertEqual(
            result["recording_coverage"]["csv_tail_evidence"]["status"],
            "warning",
        )

    def test_large_xdf_tail_remains_fatal_even_with_complete_csv_evidence(self) -> None:
        stamps = [index * 0.05 for index in range(10)]
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), eeg_stamps=stamps)
            streams = list(pyxdf.resolve_streams(str(paths.eeg_xdf)))  # type: ignore[attr-defined]
            streams[0]["hostname"] = "ACQ-PC"
            streams[1]["hostname"] = "ACQ-PC"
            pyxdf.resolve_streams = lambda filename: streams  # type: ignore[attr-defined]
            receipt = paths.raw / "lsl_markers_received.csv"
            receipt.write_text(
                "marker_label,lsl_timestamp,local_received_lsl_timestamp\n"
                "task_start,1.0005,1.0005\n"
                "task_end,3.0015,3.0015\n",
                encoding="utf-8",
            )
            (paths.raw / "eeg_metadata.json").write_text(
                json.dumps(
                    {
                        "status": "stopped",
                        "sample_count": 4000,
                        "last_lsl_timestamp": 3.1,
                        "first_source_lsl_timestamp": 0.0,
                        "last_source_lsl_timestamp": 3.1,
                        "first_local_received_lsl_timestamp": 0.0,
                        "last_local_received_lsl_timestamp": 3.1,
                        "timestamp_gap_count": 0,
                        "nonmonotonic_timestamp_count": 0,
                        "stream": {
                            "name": "Neuracle EEG",
                            "type": "EEG",
                            "source_id": "eeg-test",
                            "hostname": "ACQ-PC",
                            "channel_count": 65,
                        },
                        "raw_sample_contract": {
                            "source_timestamp_retained": True,
                            "amplitude_samples_modified": False,
                            "channel_value_order_modified": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            (paths.raw / "lsl_markers_received_metadata.json").write_text(
                json.dumps(
                    {
                        "status": "stopped",
                        "received_count": 2,
                        "last_lsl_timestamp": 3.0015,
                    }
                ),
                encoding="utf-8",
            )
            original_load = pyxdf.load_xdf  # type: ignore[attr-defined]

            def load_with_late_marker(*args: object, **kwargs: object) -> object:
                loaded, header = original_load(*args, **kwargs)
                loaded[1]["time_stamps"][-1] = 3.0015
                return loaded, header

            pyxdf.load_xdf = load_with_late_marker  # type: ignore[attr-defined]
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "fail")
        self.assertEqual(
            result["recording_coverage"]["csv_tail_evidence"]["status"],
            "fail",
        )
        self.assertTrue(any("warning limit 2.000" in row for row in result["failures"]))

    def test_xdf_tail_without_complete_csv_evidence_remains_fatal(self) -> None:
        stamps = [index * 0.05 for index in range(10)]
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), eeg_stamps=stamps)
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("clock-bridge evidence" in failure for failure in result["failures"]))

    def test_large_constant_eeg_marker_clock_offset_is_normalized_when_bridge_proves_coverage(self) -> None:
        eeg_stamps = [100.0 + index / 1000.0 for index in range(301)]
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), eeg_stamps=eeg_stamps)
            original_load = pyxdf.load_xdf  # type: ignore[attr-defined]

            def load_with_device_clock_offset(*args: object, **kwargs: object) -> object:
                loaded, header = original_load(*args, **kwargs)
                loaded[1]["time_stamps"] = np.asarray([871645.05, 871645.25])
                return loaded, header

            pyxdf.load_xdf = load_with_device_clock_offset  # type: ignore[attr-defined]
            receipt = paths.raw / "lsl_markers_received.csv"
            receipt.write_text(
                "marker_label,lsl_timestamp,local_received_lsl_timestamp\n"
                "task_start,871645.05,871645.05\n"
                "task_end,871645.25,871645.25\n",
                encoding="utf-8",
            )
            (paths.raw / "eeg_metadata.json").write_text(
                json.dumps(
                    {
                        "status": "stopped",
                        "sample_count": 400,
                        "first_source_lsl_timestamp": 99.9,
                        "last_source_lsl_timestamp": 100.4,
                        "first_local_received_lsl_timestamp": 871644.9,
                        "last_local_received_lsl_timestamp": 871645.4,
                        "timestamp_gap_count": 0,
                        "nonmonotonic_timestamp_count": 0,
                        "stream": {
                            "name": "Neuracle EEG",
                            "type": "EEG",
                            "source_id": "eeg-test",
                            "channel_count": 65,
                        },
                        "raw_sample_contract": {
                            "source_timestamp_retained": True,
                            "amplitude_samples_modified": False,
                            "channel_value_order_modified": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(result["failures"], [])
        self.assertEqual(
            result["recording_coverage"]["clock_alignment_mode"],
            "recording_origin_normalized",
        )
        self.assertEqual(
            result["recording_coverage"]["csv_clock_bridge_evidence"]["status"],
            "pass",
        )
        normalization = result["recording_coverage"]["clock_normalization"]
        self.assertEqual(
            normalization["origin_definition"],
            "first_required_marker_pyxdf_synchronized_timestamp",
        )
        self.assertAlmostEqual(normalization["normalized_eeg_start_seconds"], -0.05)
        self.assertAlmostEqual(normalization["normalized_eeg_end_seconds"], 0.25)
        self.assertAlmostEqual(normalization["normalized_marker_start_seconds"], 0.0)
        self.assertAlmostEqual(normalization["normalized_marker_end_seconds"], 0.2)
        self.assertFalse(normalization["raw_xdf_timestamps_modified"])
        self.assertEqual(result["warnings"], [])

    def test_nonwriting_heartbeat_normalizes_device_clock_without_csv_mirror(self) -> None:
        eeg_stamps = [100.0 + index / 1000.0 for index in range(301)]
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), eeg_stamps=eeg_stamps)
            original_load = pyxdf.load_xdf  # type: ignore[attr-defined]

            def load_with_device_clock_offset(*args: object, **kwargs: object) -> object:
                loaded, header = original_load(*args, **kwargs)
                loaded[1]["time_stamps"] = np.asarray([871645.05, 871645.25])
                return loaded, header

            pyxdf.load_xdf = load_with_device_clock_offset  # type: ignore[attr-defined]
            (paths.raw / "lsl_markers_received.csv").write_text(
                "marker_label,lsl_timestamp,local_received_lsl_timestamp\n"
                "task_start,871645.05,871645.06\n"
                "task_end,871645.25,871645.26\n",
                encoding="utf-8",
            )
            metadata = json.loads(paths.xdf_metadata.read_text(encoding="utf-8"))
            metadata["lsl_sample_heartbeat"] = {
                "status": "stopped",
                "sample_count": 400,
                "first_source_lsl_timestamp": 99.9,
                "last_source_lsl_timestamp": 100.4,
                "first_local_received_lsl_timestamp": 871644.9,
                "last_local_received_lsl_timestamp": 871645.4,
                "timestamp_gap_count": 0,
                "nonmonotonic_timestamp_count": 0,
                "stream": {
                    "name": "Neuracle EEG",
                    "type": "EEG",
                    "source_id": "eeg-test",
                    "channel_count": 65,
                },
            }
            paths.xdf_metadata.write_text(json.dumps(metadata), encoding="utf-8")
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "pass", result)
        bridge = result["recording_coverage"]["clock_bridge_evidence"]
        self.assertEqual(bridge["evidence_source"], "lsl_sample_heartbeat")
        self.assertEqual(bridge["status"], "pass")
        self.assertEqual(
            result["recording_coverage"]["clock_alignment_mode"],
            "recording_origin_normalized",
        )

    def test_clock_bridge_still_rejects_xdf_that_really_starts_after_marker(self) -> None:
        eeg_stamps = [100.8, 100.85, 100.9, 100.95, 101.0]
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), eeg_stamps=eeg_stamps)
            original_load = pyxdf.load_xdf  # type: ignore[attr-defined]

            def load_with_device_clock_offset(*args: object, **kwargs: object) -> object:
                loaded, header = original_load(*args, **kwargs)
                loaded[1]["time_stamps"] = np.asarray([871645.05, 871645.25])
                return loaded, header

            pyxdf.load_xdf = load_with_device_clock_offset  # type: ignore[attr-defined]
            (paths.raw / "lsl_markers_received.csv").write_text(
                "marker_label,lsl_timestamp,local_received_lsl_timestamp\n"
                "task_start,871645.05,871645.05\n"
                "task_end,871645.25,871645.25\n",
                encoding="utf-8",
            )
            (paths.raw / "eeg_metadata.json").write_text(
                json.dumps(
                    {
                        "status": "stopped",
                        "sample_count": 400,
                        "first_source_lsl_timestamp": 99.9,
                        "last_source_lsl_timestamp": 101.1,
                        "first_local_received_lsl_timestamp": 871644.9,
                        "last_local_received_lsl_timestamp": 871646.1,
                        "timestamp_gap_count": 0,
                        "nonmonotonic_timestamp_count": 0,
                        "stream": {
                            "name": "Neuracle EEG",
                            "type": "EEG",
                            "source_id": "eeg-test",
                            "channel_count": 65,
                        },
                        "raw_sample_contract": {
                            "source_timestamp_retained": True,
                            "amplitude_samples_modified": False,
                            "channel_value_order_modified": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(
            any("starts after the first required marker" in row for row in result["failures"])
        )

    def test_xdf_validation_warns_on_nonmonotonic_source_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), eeg_stamps=[1.0, 1.002, 1.001])
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["failures"], [])
        self.assertTrue(any("nonmonotonic" in warning for warning in result["warnings"]))

    def test_xdf_validation_rejects_duplicate_required_eeg_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp))
            original_resolve = pyxdf.resolve_streams  # type: ignore[attr-defined]

            def duplicated(filename: str) -> list[dict[str, object]]:
                streams = list(original_resolve(filename))
                duplicate = dict(streams[0])
                duplicate["stream_id"] = 3
                streams.append(duplicate)
                return streams

            pyxdf.resolve_streams = duplicated  # type: ignore[attr-defined]
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("selected EEG identity" in failure for failure in result["failures"]))

    def test_xdf_validation_rejects_missing_required_marker_stream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp))
            original_resolve = pyxdf.resolve_streams  # type: ignore[attr-defined]
            pyxdf.resolve_streams = lambda filename: list(original_resolve(filename))[:1]  # type: ignore[attr-defined]
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("marker source ID" in failure for failure in result["failures"]))

    def test_xdf_validation_rejects_structurally_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp))

            def corrupt(filename: str) -> list[dict[str, object]]:
                raise OSError("invalid chunk")

            pyxdf.resolve_streams = corrupt  # type: ignore[attr-defined]
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("structure could not be read" in failure for failure in result["failures"]))


@unittest.skipUnless(
    os.environ.get("EEGLE_RUN_LABRECORDER_INTEGRATION") == "1",
    "set EEGLE_RUN_LABRECORDER_INTEGRATION=1 on a LabRecorder workstation",
)
class RealLabRecorderIntegrationTests(unittest.TestCase):
    def test_prestarted_marker_stream_is_retained_in_managed_xdf(self) -> None:
        import pylsl

        executable = os.environ["EEGLE_LABRECORDER_EXECUTABLE"]
        with tempfile.TemporaryDirectory() as tmp:
            paths = paths_for_existing_session(tmp)
            eeg_info = pylsl.StreamInfo("Neuracle Integration", "EEG", 65, 1000, "float32", "eeg-test")
            channels = eeg_info.desc().append_child("channels")
            for name in CHANNELS:
                channels.append_child("channel").append_child_value("label", name)
            eeg_outlet = pylsl.StreamOutlet(eeg_info)
            stop = threading.Event()

            def publish() -> None:
                sample = [0.0] * 65
                while not stop.is_set():
                    eeg_outlet.push_sample(sample)
                    time.sleep(0.001)

            publisher = threading.Thread(target=publish, daemon=True)
            publisher.start()
            marker_info = pylsl.StreamInfo("EEGleMarkers", "Markers", 1, 0, "string", "marker-test")
            marker_outlet = pylsl.StreamOutlet(marker_info)
            config = {
                "hardware": {
                    "eeg": {
                        "family": "Neuracle",
                        "profile": "neuracle64",
                        "expected_channel_counts": [65],
                        "expected_channel_names": CHANNELS,
                        "expected_sample_rate_hz": 1000,
                        "lsl_stream_type": "EEG",
                        "lsl_name_patterns": ["neuracle"],
                        "stream_timeout_seconds": 5,
                        "maximum_timestamp_gap_seconds": 0.1,
                    },
                    "markers": {
                        "lsl_stream_name": "EEGleMarkers",
                        "lsl_stream_type": "Markers",
                        "source_id": "marker-test",
                    },
                },
                "processes": {
                    "recorder": {
                        "backend": "labrecorder_xdf",
                        "csv_mirror": True,
                        "executable": executable,
                        "rcs_port": int(os.environ.get("EEGLE_LABRECORDER_RCS_PORT", "22345")),
                    }
                },
            }
            paths.parameters.write_text(json.dumps(config), encoding="utf-8")
            recorder = LabRecorderXdfRecorder(config, paths)
            recorder.start()
            time.sleep(2.0)
            marker_outlet.push_sample(["integration_marker"])
            time.sleep(1.0)
            recorder.stop(reason="integration_test")
            stop.set()
            publisher.join(timeout=2.0)
            (paths.raw / "lsl_markers_received.csv").write_text(
                "marker_label,lsl_timestamp,local_received_lsl_timestamp\n"
                "integration_marker,0,0\n",
                encoding="utf-8",
            )

            result = validate_xdf_recording(paths.root, required=True)

            self.assertEqual(result["status"], "pass", result)


if __name__ == "__main__":
    unittest.main()
