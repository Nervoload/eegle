"""Installed-artifact smoke test against native pylsl's local network path."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pylsl

from eegle._domain import Lineage
from eegle.integrations import (
    attach_annotations_to_mne_raw,
    dense_batch_to_mne_raw,
    dense_windows_to_mne_epochs,
    mne_raw_to_replay_inputs,
    sparse_events_to_mne_annotations,
)
from eegle.integrations.lsl import LSL_DENSE_SOURCE_PLUGIN_ID, LslSource, detect_lsl
from eegle.processing import DenseWindow
from eegle.streams import SparseEvent, SparseEventBatch, TimePoint


def main() -> None:
    identity = f"eegle-native-smoke-{uuid.uuid4().hex}"
    info = pylsl.StreamInfo(identity, "EEG", 2, 100.0, "float32", identity)
    channels = info.desc().append_child("channels")
    for label in ("C3", "C4"):
        channel = channels.append_child("channel")
        channel.append_child_value("label", label)
        channel.append_child_value("unit", "uV")
    outlet = pylsl.StreamOutlet(info)

    time.sleep(0.25)
    detected = detect_lsl(wait_time=2.0)
    capability = next(
        source
        for source in detected.sources
        if source.plugin_id == LSL_DENSE_SOURCE_PLUGIN_ID
        and source.stream.metadata["lsl"]["source_id"] == identity
    )
    assert tuple(channel.name for channel in capability.stream.channels) == ("C3", "C4")
    assert tuple(channel.unit for channel in capability.stream.channels) == ("uV", "uV")

    config = dict(capability.config)
    config["pull_timeout_seconds"] = 3.0
    config["resolve_timeout_seconds"] = 2.0
    source = LslSource(config)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(source.read)
        assert outlet.wait_for_consumers(timeout=5.0), "native LSL inlet did not subscribe"
        outlet.push_chunk([[1.0, 2.0], [3.0, 4.0]], timestamp=pylsl.local_clock())
        batch = pending.result(timeout=5.0)
    source.close()

    assert batch is not None
    np.testing.assert_array_equal(batch.values, np.asarray([[1.0, 2.0], [3.0, 4.0]]))
    exported = dense_batch_to_mne_raw(batch, capability.stream)
    np.testing.assert_allclose(
        exported.raw.get_data(),
        np.asarray([[1e-6, 3e-6], [2e-6, 4e-6]]),
    )
    assert exported.raw.ch_names == ["C3", "C4"]
    assert exported.timing_representation == "explicit"
    assert exported.sample_times_seconds == tuple(
        timestamp.seconds for timestamp in batch.sample_times
    )
    marker = SparseEvent(
        "event.native.target",
        "stimulus",
        batch.sample_times[0],
        batch.received_time,
        batch.available_time,
        "target",
    )
    marker_batch = SparseEventBatch(
        "batch.native.markers",
        "stream.native.markers",
        1,
        0,
        (marker,),
    )
    annotations = sparse_events_to_mne_annotations(
        (marker_batch,),
        origin_time=batch.sample_times[0],
    )
    annotated = attach_annotations_to_mne_raw(exported, annotations)
    assert annotated.raw.annotations.description.tolist() == ["target"]

    window = DenseWindow(
        "window.native.lsl",
        batch.stream_id,
        batch.stream_revision,
        batch.sequence_start,
        batch.channel_ids,
        batch.values,
        batch.sample_times[0],
        TimePoint(
            batch.sample_times[-1].seconds + 0.01,
            batch.sample_times[-1].clock_id,
        ),
        batch.available_time,
        (batch.batch_id,),
        Lineage(
            "window.native",
            (batch.batch_id,),
            component_version="1",
            latest_input_available_time=batch.available_time,
            stream_revisions={batch.stream_id: batch.stream_revision},
        ),
    )
    epochs = dense_windows_to_mne_epochs((window,), capability.stream)
    assert epochs.epochs.get_data().shape == (1, 2, 2)

    replay_inputs = mne_raw_to_replay_inputs(
        annotated.raw,
        stream_id="stream.native.mne-replay",
        first_sample_time=batch.sample_times[0],
        received_time=batch.received_time,
        available_time=batch.available_time,
    )
    assert len(replay_inputs.streams) == 2
    assert replay_inputs.marker_packet is not None
    assert replay_inputs.marker_packet.events[0].value["description"] == "target"
    print(
        f"native LSL/MNE smoke: pylsl={pylsl.__version__} "
        "channels=C3,C4 samples=2 annotations=1 epochs=1 replay_streams=2"
    )


if __name__ == "__main__":
    main()
