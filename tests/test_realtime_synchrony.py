from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from reproduce.devices.lsl_eeg import _select_lsl_info
from reproduce.feedback_manager import FeedbackManager, WorkerHandle, _pipeline_validity_failures
from reproduce.hardware.enobio import mapped_channel_names
from reproduce.realtime.alpha import AlphaPowerEstimator
from reproduce.realtime.buffer import RingBuffer
from reproduce.session import create_session
from reproduce.tasks.go_nogo import _mark
from reproduce.workers.realtime_processor import _advance_deadline, _reference_for_artifact_gate, _try_open_marker_inlet


class _FakeInfo:
    def __init__(self, name: str, stream_type: str, source_id: str, channel_count: int = 8, nominal_srate: float = 500.0) -> None:
        self._name = name
        self._type = stream_type
        self._source_id = source_id
        self._channel_count = channel_count
        self._nominal_srate = nominal_srate

    def name(self) -> str:
        return self._name

    def type(self) -> str:
        return self._type

    def source_id(self) -> str:
        return self._source_id

    def channel_count(self) -> int:
        return self._channel_count

    def nominal_srate(self) -> float:
        return self._nominal_srate


class _FakeInlet:
    def __init__(self, info: _FakeInfo, **kwargs: object) -> None:
        self.info = info
        self.kwargs = kwargs
        self.opened = False

    def open_stream(self, timeout: float) -> None:
        self.opened = True

    def time_correction(self, timeout: float) -> float:
        return 0.125


class _FakePylsl:
    proc_clocksync = 1
    proc_dejitter = 2
    proc_monotonize = 4

    def __init__(self, infos: list[_FakeInfo]) -> None:
        self.infos = infos
        self.inlets: list[_FakeInlet] = []

    def resolve_streams(self, wait_time: float) -> list[_FakeInfo]:
        return self.infos

    def StreamInlet(self, info: _FakeInfo, **kwargs: object) -> _FakeInlet:
        inlet = _FakeInlet(info, **kwargs)
        self.inlets.append(inlet)
        return inlet


class _FakeLogger:
    def mark(self, label: str, *, event_type: str, timestamp: float, trial: int | None, value: str | None, **metadata: object) -> object:
        return SimpleNamespace(label=label, timestamp=timestamp, metadata=metadata)


class _FakeOutlet:
    def __init__(self) -> None:
        self.markers: list[tuple[str, float | None]] = []

    def push(self, label: str, timestamp: float | None = None) -> None:
        self.markers.append((label, timestamp))


class RealtimeSynchronyTests(unittest.TestCase):
    def test_marker_inlet_requires_exact_session_stream_and_applies_clock_sync(self) -> None:
        pylsl = _FakePylsl(
            [
                _FakeInfo("EEGleMarkers", "Markers", "stale-session"),
                _FakeInfo("EEGleMarkers", "Markers", "run-123"),
                _FakeInfo("OtherMarkers", "Markers", "run-123"),
            ]
        )
        config = {
            "hardware": {
                "markers": {
                    "lsl_stream_name": "EEGleMarkers",
                    "lsl_stream_type": "Markers",
                    "source_id": "run-123",
                }
            }
        }

        inlet, metadata = _try_open_marker_inlet(pylsl, config)

        self.assertIsNotNone(inlet)
        self.assertEqual(metadata["source_id"], "run-123")
        self.assertEqual(pylsl.inlets[0].kwargs["processing_flags"], 5)
        self.assertEqual(metadata["initial_time_correction_seconds"], 0.125)

    def test_duplicate_exact_marker_streams_are_rejected(self) -> None:
        pylsl = _FakePylsl(
            [
                _FakeInfo("EEGleMarkers", "Markers", "run-123"),
                _FakeInfo("EEGleMarkers", "Markers", "run-123"),
            ]
        )
        config = {"hardware": {"markers": {"source_id": "run-123"}}}

        inlet, metadata = _try_open_marker_inlet(pylsl, config)

        self.assertIsNone(inlet)
        self.assertIsNone(metadata)

    def test_eeg_selection_does_not_silently_accept_unmatched_eeg_stream(self) -> None:
        unrelated = _FakeInfo("OtherEEG", "EEG", "other-device")
        pylsl = _FakePylsl([unrelated])
        config = {
            "lsl_stream_type": "EEG",
            "lsl_name_patterns": ["enobio", "nic"],
            "expected_channel_counts": [8],
            "expected_sample_rate_hz": 500,
        }

        info, stream = _select_lsl_info(pylsl, config, 0.1)

        self.assertIsNone(info)
        self.assertIsNone(stream)

    def test_flip_marker_uses_modeled_visual_timestamp_before_logging(self) -> None:
        logger = _FakeLogger()
        outlet = _FakeOutlet()
        with patch("reproduce.tasks.go_nogo.lsl_local_clock", return_value=100.0):
            record = _mark(
                logger,
                outlet,
                "stimulus_onset",
                _scheduled_on_flip=True,
                _display_timing={
                    "fixed_display_latency_ms": 12.0,
                    "expected_refresh_rate_hz": 60.0,
                    "measured_refresh_rate_hz": 60.0,
                    "expected_frame_interval_ms": 16.6667,
                    "expected_visual_onset_uncertainty_ms": 8.3333,
                },
            )

        self.assertAlmostEqual(outlet.markers[0][1] or 0.0, 100.012)
        self.assertAlmostEqual(record.timestamp - record.metadata["flip_monotonic"], 0.012)
        self.assertTrue(record.metadata["scheduled_on_flip"])

    def test_profile_maps_generic_nic_labels_to_posterior_montage(self) -> None:
        mapped, source = mapped_channel_names(
            [f"ch_{index:03d}" for index in range(1, 9)],
            {"profile": "enobio8_alpha_posterior"},
        )

        self.assertEqual(mapped, ["P3", "P4", "PO3", "PO4", "Pz", "O1", "O2", "Oz"])
        self.assertEqual(source, "profile:enobio8_alpha_posterior")

    def test_alpha_estimator_rejects_unknown_posterior_montage(self) -> None:
        with self.assertRaisesRegex(ValueError, "none of the configured posterior"):
            AlphaPowerEstimator(
                250.0,
                ["ch_001", "ch_002"],
                {"posterior_channels": ["P3", "P4"], "band": {"low_hz": 8.0, "high_hz": 12.0}},
            )

    def test_artifact_gate_reference_is_applied_before_filters(self) -> None:
        raw = np.asarray([[1000.0, 1002.0], [1004.0, 1006.0]])

        referenced = _reference_for_artifact_gate(raw, {"reference": "average"})

        np.testing.assert_array_equal(referenced, np.zeros((2, 2)))

    def test_ring_buffer_vectorized_append_preserves_latest_order(self) -> None:
        buffer = RingBuffer(5, 2)
        buffer.append_chunk(np.arange(4, dtype=float), np.column_stack([np.arange(4), np.arange(4) + 10]))
        buffer.append_chunk(np.arange(4, 8, dtype=float), np.column_stack([np.arange(4, 8), np.arange(4, 8) + 10]))

        timestamps, data = buffer.window(5)

        np.testing.assert_array_equal(timestamps, np.arange(3, 8, dtype=float))
        np.testing.assert_array_equal(data[:, 0], np.arange(3, 8, dtype=float))

    def test_periodic_deadline_does_not_accumulate_runtime_drift(self) -> None:
        self.assertAlmostEqual(_advance_deadline(10.0, 0.1, 10.06), 10.1)
        self.assertAlmostEqual(_advance_deadline(10.0, 0.1, 10.36), 10.4)

    def test_session_parameters_include_run_unique_marker_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "runtime": {"session_root": tmp},
                "experiment": {"experiment_id": "test", "participant_id": "p1", "task": "go_nogo"},
                "hardware": {"markers": {"lsl_stream_name": "EEGleMarkers", "lsl_stream_type": "Markers"}},
                "realtime": {"enabled": True},
            }
            paths = create_session(config, root=Path(tmp))
            parameters = json.loads(paths.parameters.read_text(encoding="utf-8"))

            source_id = parameters["hardware"]["markers"]["source_id"]
            self.assertIn(paths.root.name, source_id)
            self.assertTrue(parameters["hardware"]["markers"]["required_for_realtime"])

    def test_worker_exit_is_reported_immediately_with_stderr_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "runtime": {"session_root": tmp},
                "experiment": {"experiment_id": "test", "participant_id": "p1", "task": "go_nogo"},
            }
            paths = create_session(config, root=Path(tmp))
            manager = FeedbackManager(config, paths, record_eeg=False)
            stderr_file = paths.process_logs / "realtime_processor.stderr.log"
            stderr_file.write_text("Traceback omitted\nFileNotFoundError: missing model bundle\n", encoding="utf-8")
            worker = WorkerHandle(
                name="realtime_processor",
                backend="lsl",
                module="test.worker",
                command=[],
                status_file=paths.process_logs / "realtime_processor.status.json",
                stdout_file=paths.process_logs / "realtime_processor.stdout.log",
                stderr_file=stderr_file,
                process=SimpleNamespace(poll=lambda: 1),
            )

            status = manager._wait_for_status(worker, {"running", "failed"}, timeout_seconds=1.0)

            self.assertEqual(status["status"], "failed")
            self.assertEqual(status["error"], "FileNotFoundError: missing model bundle")

    def test_worker_command_uses_current_python_module_launch_without_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "runtime": {"session_root": tmp},
                "experiment": {"experiment_id": "test", "participant_id": "p1", "task": "go_nogo"},
            }
            paths = create_session(config, root=Path(tmp))
            worker = FeedbackManager(config, paths, record_eeg=False)._make_worker(
                "realtime_processor",
                "reproduce.workers.realtime_processor",
                ["--backend", "lsl"],
            )

        self.assertIsInstance(worker.command, list)
        self.assertEqual(worker.command[0], sys.executable)
        self.assertEqual(worker.command[1:3], ["-m", "reproduce.workers.realtime_processor"])

    def test_enabled_realtime_without_markers_or_alpha_is_invalid(self) -> None:
        failures = _pipeline_validity_failures(
            {
                "recorder": {"status": "stopped", "sample_count": 100},
                "realtime_processor": {"status": "stopped", "sample_count": 100, "marker_count": 0, "alpha_estimate_count": 0},
            },
            {
                "recorder": {"enabled": True},
                "realtime_processor": {"enabled": True},
            },
            {
                "realtime": {
                    "alpha": {"enabled": True},
                    "epoching": {"enabled": True},
                }
            },
        )

        self.assertIn("realtime_processor", failures)

    def test_recorder_validity_reads_nested_worker_summary(self) -> None:
        failures = _pipeline_validity_failures(
            {
                "recorder": {"status": "stopped", "summary": {"sample_count": 100}},
                "realtime_processor": {"status": "disabled"},
            },
            {
                "recorder": {"enabled": True},
                "realtime_processor": {"enabled": False},
            },
            {"realtime": {}},
        )

        self.assertNotIn("recorder", failures)


if __name__ == "__main__":
    unittest.main()
