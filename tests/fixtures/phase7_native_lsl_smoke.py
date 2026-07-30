"""Installed-artifact smoke test against native pylsl's local network path."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pylsl

from eegle.integrations.lsl import LSL_DENSE_SOURCE_PLUGIN_ID, LslSource, detect_lsl


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
    print(f"native LSL smoke: pylsl={pylsl.__version__} channels=C3,C4 samples=2")


if __name__ == "__main__":
    main()
