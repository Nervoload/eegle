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
from eegle.session import paths_for_existing_session


CHANNELS = [f"E{index:02d}" for index in range(1, 65)]


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
                "channel_count": 64,
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

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
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
                }
            },
        }

    def test_config_normalization_retains_managed_xdf_contract(self) -> None:
        normalized = normalize_processes(self._config("C:/LSL/LabRecorder.exe"))

        self.assertEqual(normalized["recorder"]["backend"], "labrecorder_xdf")
        self.assertTrue(normalized["recorder"]["csv_mirror"])
        self.assertEqual(normalized["recorder"]["executable"], "C:/LSL/LabRecorder.exe")
        self.assertEqual(normalized["recorder"]["rcs_port"], 22345)
        self.assertEqual(normalized["recorder"]["shutdown_timeout_seconds"], 15.0)

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
            metadata = json.loads(paths.xdf_metadata.read_text(encoding="utf-8"))
            self.assertIn("RequiredStreams=", metadata["labrecorder_config_contents"])
            self.assertEqual(metadata["labrecorder_launch_command"], popen.call_args.args[0])

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

    def test_snapshot_fails_when_xdf_growth_stalls(self) -> None:
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

            self.assertEqual(status["status"], "failed")
            self.assertIn("has not advanced", str(status["error"]))

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

    def test_snapshot_fails_when_csv_mirror_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = paths_for_existing_session(tmp)
            paths.eeg_xdf.write_bytes(b"XDF:test")
            recorder = LabRecorderXdfRecorder(self._config("LabRecorder.exe"), paths)
            recorder._mirror = _Mirror()  # type: ignore[assignment]
            recorder._mirror.status = "failed"
            recorder._process = _Process()  # type: ignore[assignment]
            recorder._status = "recording"

            status = recorder.snapshot()

            self.assertEqual(status["status"], "failed")
            self.assertIn("CSV mirror status", str(status["error"]))


class XdfIntegrityTests(unittest.TestCase):
    def _session(
        self,
        root: Path,
        *,
        rate: float = 1000.0,
        channel_count: int = 64,
        labels: list[str] | None = None,
        eeg_stamps: list[float] | None = None,
    ) -> tuple[object, Path]:
        paths = paths_for_existing_session(root)
        paths.eeg_xdf.write_bytes(b"XDF:test-data")
        parameters = {
            "hardware": {
                "eeg": {
                    "family": "Neuracle",
                    "profile": "neuracle64",
                    "expected_channel_counts": [64],
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
        )

    def _fake_pyxdf(
        self,
        *,
        rate: float,
        channel_count: int = 64,
        labels: list[str] | None = None,
        eeg_stamps: list[float] | None = None,
    ) -> object:
        labels = list(labels or CHANNELS[:channel_count])
        eeg_stamps = list(eeg_stamps or [1.0, 1.001, 1.002])
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
            eeg_values, returned_eeg_stamps, _ = on_chunk(  # type: ignore[operator]
                np.zeros((len(eeg_stamps), channel_count), dtype=np.float32),
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
                {"info": eeg_info, "time_series": eeg_values, "time_stamps": returned_eeg_stamps},
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
            self.assertTrue(result["markers"]["sequence_matches_receipt"])
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

    def test_xdf_validation_rejects_wrong_sample_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), rate=500.0)
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

            self.assertEqual(result["status"], "fail")
            self.assertTrue(any("sample rate" in failure for failure in result["failures"]))

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

    def test_xdf_validation_rejects_timestamp_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), eeg_stamps=[1.0, 1.001, 1.5])
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("timestamp gaps" in failure for failure in result["failures"]))

    def test_xdf_validation_rejects_nonmonotonic_source_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths, pyxdf = self._session(Path(tmp), eeg_stamps=[1.0, 1.002, 1.001])
            with patch.dict(sys.modules, {"pyxdf": pyxdf}):
                result = validate_xdf_recording(paths.root, required=True)

        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("nonmonotonic" in failure for failure in result["failures"]))

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
            eeg_info = pylsl.StreamInfo("Neuracle Integration", "EEG", 64, 1000, "float32", "eeg-test")
            channels = eeg_info.desc().append_child("channels")
            for name in CHANNELS:
                channels.append_child("channel").append_child_value("label", name)
            eeg_outlet = pylsl.StreamOutlet(eeg_info)
            stop = threading.Event()

            def publish() -> None:
                sample = [0.0] * 64
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
                        "expected_channel_counts": [64],
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
