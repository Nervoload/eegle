"""Development-only simulated EEG LSL outlet."""

from __future__ import annotations

import math
from time import monotonic, sleep

import numpy as np


def run_simulated_eeg(
    name: str = "EnobioMock",
    stream_type: str = "EEG",
    channel_count: int = 32,
    sample_rate_hz: float = 500.0,
    duration_seconds: float = 30.0,
) -> None:
    import pylsl

    info = pylsl.StreamInfo(name, stream_type, channel_count, sample_rate_hz, "float32", f"{name}-source")
    channels = info.desc().append_child("channels")
    for idx in range(channel_count):
        channel = channels.append_child("channel")
        channel.append_child_value("label", f"ch_{idx + 1:03d}")
        channel.append_child_value("unit", "microvolts")
        channel.append_child_value("type", "EEG")
    outlet = pylsl.StreamOutlet(info)

    interval = 1.0 / sample_rate_hz
    next_sample = monotonic()
    end_time = monotonic() + duration_seconds
    sample_idx = 0
    while monotonic() < end_time:
        t = sample_idx / sample_rate_hz
        alpha = math.sin(2.0 * math.pi * 10.0 * t)
        theta = math.sin(2.0 * math.pi * 6.0 * t)
        noise = np.random.normal(0.0, 0.1, channel_count)
        sample = (5.0 * alpha + 2.0 * theta + noise).astype(float).tolist()
        outlet.push_sample(sample)
        sample_idx += 1
        next_sample += interval
        sleep(max(0.0, next_sample - monotonic()))
