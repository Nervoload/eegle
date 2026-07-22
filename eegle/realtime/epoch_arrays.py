"""Legacy epoch layout helpers isolated to keep classifier/model imports acyclic."""

from __future__ import annotations

from typing import Any

import numpy as np


def epoch_to_channels_samples(
    epoch: np.ndarray,
    channel_names: list[str],
    input_layout: str = "auto",
) -> np.ndarray:
    data = np.asarray(epoch, dtype=float)
    if data.ndim != 2:
        raise ValueError("epoch must be a 2D array")
    layout = input_layout.lower()
    channel_count = len(channel_names)
    if layout == "channels_x_samples":
        return data
    if layout == "samples_x_channels":
        return data.T
    if channel_count and data.shape[0] == channel_count:
        return data
    if channel_count and data.shape[1] == channel_count:
        return data.T
    return data.T if data.shape[0] > data.shape[1] else data


def epoch_to_samples_channels(
    epoch: np.ndarray,
    channel_names: list[str],
    input_layout: str = "auto",
) -> np.ndarray:
    return epoch_to_channels_samples(epoch, channel_names, input_layout).T


def relative_times(
    metadata: dict[str, Any],
    sample_count: int,
    sample_rate_hz: float,
) -> np.ndarray:
    if "relative_times" in metadata:
        values = np.asarray(metadata["relative_times"], dtype=float)
        if values.size == sample_count:
            return values
    window = metadata.get("epoch_window_seconds") or metadata.get("epoch_window") or [-0.2, 0.8]
    tmin = float(window[0])
    return np.arange(sample_count, dtype=float) / sample_rate_hz + tmin
