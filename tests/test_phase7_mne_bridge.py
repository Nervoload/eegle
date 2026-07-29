from __future__ import annotations

from unittest.mock import patch
import unittest

import numpy as np

from eegle.integrations import MneRawExport, dense_batch_to_mne_raw
from eegle.streams import (
    ChannelSpec,
    ContentKind,
    DenseSampleBatch,
    RateModel,
    StreamSpec,
    TimePoint,
)


class _RawArray:
    def __init__(self, data, info, *, verbose):
        self.data = data
        self.info = info
        self.verbose = verbose


class _Mne:
    class io:
        RawArray = _RawArray

    @staticmethod
    def create_info(*, ch_names, sfreq, ch_types):
        return {"ch_names": ch_names, "sfreq": sfreq, "ch_types": ch_types}


class Phase7MneBridgeTests(unittest.TestCase):
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
        with patch("eegle.integrations.mne._mne_module") as importer:
            with self.assertRaisesRegex(ValueError, "unsupported MNE voltage unit"):
                dense_batch_to_mne_raw(batch, stream)
        importer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
