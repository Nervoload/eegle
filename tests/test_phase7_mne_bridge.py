from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import patch

import numpy as np

from eegle._domain import Lineage
from eegle.integrations import (
    MneRawExport,
    attach_annotations_to_mne_raw,
    dense_batch_to_mne_raw,
    dense_windows_to_mne_epochs,
    mne_raw_to_replay_inputs,
    sparse_events_to_mne_annotations,
)
from eegle.processing import DenseWindow
from eegle.replay import ReplaySource
from eegle.streams import (
    ChannelSpec,
    ContentKind,
    DenseSampleBatch,
    RateModel,
    SparseEvent,
    SparseEventBatch,
    StreamSpec,
    TimePoint,
)


class _RawArray:
    def __init__(self, data, info, *, verbose):
        self.data = data
        self.info = info
        self.verbose = verbose
        self.ch_names = list(info["ch_names"])
        self.annotations = _Annotations([], [], [], orig_time=None)

    def copy(self):
        return deepcopy(self)

    def get_data(self):
        return self.data

    def get_channel_types(self):
        return list(self.info["ch_types"])

    def set_annotations(self, annotations):
        self.annotations = annotations
        return self


class _Annotations:
    def __init__(self, onset, duration, description, orig_time=None):
        self.onset = np.asarray(onset, dtype=float)
        self.duration = np.asarray(duration, dtype=float)
        self.description = np.asarray(description, dtype=str)
        self.orig_time = orig_time

    def __len__(self):
        return len(self.onset)


class _EpochsArray:
    def __init__(
        self,
        data,
        info,
        *,
        events,
        event_id,
        tmin,
        baseline,
        verbose,
    ):
        self.data = data
        self.info = info
        self.events = events
        self.event_id = event_id
        self.tmin = tmin
        self.baseline = baseline
        self.verbose = verbose


class _Mne:
    Annotations = _Annotations
    EpochsArray = _EpochsArray

    class io:
        RawArray = _RawArray

    @staticmethod
    def create_info(*, ch_names, sfreq, ch_types):
        return {"ch_names": ch_names, "sfreq": sfreq, "ch_types": ch_types}


class Phase7MneBridgeTests(unittest.TestCase):
    @staticmethod
    def _stream() -> StreamSpec:
        return StreamSpec(
            "stream.lsl.eeg",
            1,
            "eeg",
            ContentKind.DENSE_SAMPLES,
            RateModel.REGULAR,
            "device.clock",
            (ChannelSpec("Cz", "eeg", "uV"),),
            100.0,
            "float64",
        )

    def test_dense_packet_export_scales_units_and_preserves_timing_sidecar(self) -> None:
        stream = StreamSpec(
            "stream.eeg",
            1,
            "eeg",
            ContentKind.DENSE_SAMPLES,
            RateModel.REGULAR,
            "device.clock",
            (
                ChannelSpec("C3", "eeg", "uV"),
                ChannelSpec("C4", "eeg", "mV"),
            ),
            250.0,
            "float64",
        )
        batch = DenseSampleBatch(
            "batch.eeg.1",
            stream.stream_id,
            stream.revision,
            0,
            ("C3", "C4"),
            np.asarray([[1.0, 2.0], [3.0, 4.0]]),
            TimePoint(10.1, "boundary.clock"),
            TimePoint(10.2, "boundary.clock"),
            TimePoint(9.0, "device.clock"),
            1 / 250.0,
        )
        with patch("eegle.integrations.mne._mne_module", return_value=_Mne):
            exported = dense_batch_to_mne_raw(batch, stream)

        self.assertIsInstance(exported, MneRawExport)
        np.testing.assert_allclose(
            exported.raw.data,
            np.asarray([[1e-6, 3e-6], [2e-3, 4e-3]]),
        )
        self.assertEqual(exported.raw.info["ch_names"], ["C3", "C4"])
        self.assertEqual(exported.first_sample_seconds, 9.0)
        self.assertEqual(exported.source_clock_id, "device.clock")
        self.assertEqual(exported.availability_clock_id, "boundary.clock")
        self.assertEqual(exported.sample_times_seconds, (9.0, 9.004))
        self.assertEqual(exported.timing_representation, "regular")

    def test_lsl_shaped_explicit_timestamps_export_without_attribute_error(self) -> None:
        stream = self._stream()
        batch = DenseSampleBatch(
            "batch.lsl.eeg.1",
            stream.stream_id,
            stream.revision,
            0,
            ("Cz",),
            np.asarray([[1.0], [2.0], [3.0]]),
            TimePoint(10.1, "boundary.clock"),
            TimePoint(10.2, "boundary.clock"),
            sample_times=(
                TimePoint(9.0, "device.clock"),
                TimePoint(9.011, "device.clock"),
                TimePoint(9.021, "device.clock"),
            ),
        )

        with patch("eegle.integrations.mne._mne_module", return_value=_Mne):
            exported = dense_batch_to_mne_raw(batch, stream)

        self.assertEqual(exported.first_sample_seconds, 9.0)
        self.assertEqual(exported.source_clock_id, "device.clock")
        self.assertEqual(exported.sample_times_seconds, (9.0, 9.011, 9.021))
        self.assertEqual(exported.timing_representation, "explicit")

    def test_sparse_markers_attach_as_annotations_with_exact_timing_sidecar(self) -> None:
        stream = self._stream()
        batch = DenseSampleBatch(
            "batch.lsl.eeg.markers",
            stream.stream_id,
            stream.revision,
            0,
            ("Cz",),
            np.asarray([[1.0], [2.0], [3.0]]),
            TimePoint(10.1, "boundary.clock"),
            TimePoint(10.2, "boundary.clock"),
            sample_times=tuple(
                TimePoint(value, "device.clock") for value in (9.0, 9.01, 9.02)
            ),
        )
        marker = SparseEvent(
            "event.target.1",
            "stimulus",
            TimePoint(9.01, "device.clock"),
            TimePoint(10.11, "boundary.clock"),
            TimePoint(10.12, "boundary.clock"),
            {"description": "target", "duration_seconds": 0.05},
        )
        markers = SparseEventBatch(
            "batch.markers.1",
            "stream.markers",
            1,
            0,
            (marker,),
        )

        with patch("eegle.integrations.mne._mne_module", return_value=_Mne):
            raw = dense_batch_to_mne_raw(batch, stream)
            annotations = sparse_events_to_mne_annotations(
                markers,
                origin_time=TimePoint(9.0, "device.clock"),
            )
            attached = attach_annotations_to_mne_raw(raw, annotations)

        np.testing.assert_allclose(attached.raw.annotations.onset, [0.01])
        np.testing.assert_allclose(attached.raw.annotations.duration, [0.05])
        self.assertEqual(attached.raw.annotations.description.tolist(), ["target"])
        self.assertEqual(annotations.event_ids, ("event.target.1",))
        self.assertEqual(annotations.event_values[0]["description"], "target")
        self.assertEqual(
            annotations.available_times,
            (TimePoint(10.12, "boundary.clock"),),
        )
        self.assertEqual(len(raw.raw.annotations), 0)

    def test_admitted_dense_windows_export_as_epochs_with_lineage_sidecar(self) -> None:
        stream = self._stream()
        windows = tuple(
            DenseWindow(
                window_id=f"window.{index}",
                stream_id=stream.stream_id,
                stream_revision=stream.revision,
                sequence_start=index * 2,
                channel_ids=("Cz",),
                values=np.asarray([[1.0 + index], [2.0 + index]]),
                start_time=TimePoint(index * 0.02, "device.clock"),
                end_time=TimePoint((index + 1) * 0.02, "device.clock"),
                available_time=TimePoint(1.0 + index, "boundary.clock"),
                input_ids=(f"batch.input.{index}",),
                lineage=Lineage(
                    "window.event",
                    (f"batch.input.{index}",),
                    component_version="0.1.0",
                    latest_input_available_time=TimePoint(
                        1.0 + index, "boundary.clock"
                    ),
                    stream_revisions={stream.stream_id: stream.revision},
                ),
            )
            for index in range(2)
        )

        with patch("eegle.integrations.mne._mne_module", return_value=_Mne):
            exported = dense_windows_to_mne_epochs(
                windows,
                stream,
                tmin_seconds=-0.01,
            )

        self.assertEqual(exported.epochs.data.shape, (2, 1, 2))
        np.testing.assert_allclose(
            exported.epochs.data,
            np.asarray([[[1e-6, 2e-6]], [[2e-6, 3e-6]]]),
        )
        self.assertEqual(exported.window_ids, ("window.0", "window.1"))
        self.assertEqual(
            exported.input_ids,
            (("batch.input.0",), ("batch.input.1",)),
        )
        self.assertEqual(exported.sequence_spans, ((0, 1), (2, 3)))
        self.assertEqual(exported.epochs.event_id, {"eegle_window": 1})

    def test_mne_raw_and_annotations_become_separate_replay_streams(self) -> None:
        info = _Mne.create_info(
            ch_names=["C3", "C4"],
            sfreq=100.0,
            ch_types=["eeg", "eeg"],
        )
        raw = _RawArray(
            np.asarray([[1e-6, 2e-6], [3e-6, 4e-6]]),
            info,
            verbose="ERROR",
        )
        raw.set_annotations(_Annotations([0.01], [0.0], ["target"], orig_time=None))

        inputs = mne_raw_to_replay_inputs(
            raw,
            stream_id="stream.replay.eeg",
            first_sample_time=TimePoint(5.0, "recording.clock"),
            received_time=TimePoint(8.0, "boundary.clock"),
            available_time=TimePoint(8.1, "boundary.clock"),
        )

        self.assertEqual(len(inputs.streams), 2)
        self.assertEqual(len(inputs.packets), 2)
        np.testing.assert_allclose(
            inputs.dense_packet.values,
            np.asarray([[1e-6, 3e-6], [2e-6, 4e-6]]),
        )
        self.assertNotIn("annotations", inputs.dense_packet.to_payload())
        self.assertTrue(inputs.streams[0].metadata["annotations_separate"])
        assert inputs.marker_packet is not None
        self.assertEqual(inputs.marker_packet.events[0].event_time.seconds, 5.01)
        self.assertEqual(
            inputs.marker_packet.events[0].value["description"],
            "target",
        )
        replay = ReplaySource(inputs.streams[0], (inputs.dense_packet,))
        self.assertEqual(replay.read(), inputs.dense_packet)

    def test_unsupported_units_fail_without_importing_mne(self) -> None:
        stream = StreamSpec(
            "stream.signal",
            1,
            "signal",
            ContentKind.DENSE_SAMPLES,
            RateModel.REGULAR,
            "device.clock",
            (ChannelSpec("X", "signal", "tesla"),),
            1.0,
            "float64",
        )
        batch = DenseSampleBatch(
            "batch.signal.1",
            stream.stream_id,
            1,
            0,
            ("X",),
            np.asarray([[1.0]]),
            TimePoint(1.0, "boundary.clock"),
            TimePoint(1.0, "boundary.clock"),
            TimePoint(0.0, "device.clock"),
            1.0,
        )
        with (
            patch("eegle.integrations.mne._mne_module") as importer,
            self.assertRaisesRegex(ValueError, "unsupported MNE voltage unit"),
        ):
            dense_batch_to_mne_raw(batch, stream)
        importer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
