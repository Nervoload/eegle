"""Dependency-lazy export of dense EEGle packets to MNE ``RawArray`` values."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from typing import Any

import numpy as np

from eegle.streams import DenseSampleBatch, StreamSpec


@dataclass(frozen=True, slots=True)
class MneRawExport:
    """An MNE object plus the timing identity that MNE cannot represent exactly."""

    raw: Any
    stream_id: str
    stream_revision: int
    first_sample_seconds: float
    source_clock_id: str
    available_seconds: float
    availability_clock_id: str


def dense_batch_to_mne_raw(
    batch: DenseSampleBatch,
    stream: StreamSpec,
    *,
    copy: bool = True,
) -> MneRawExport:
    """Convert one regular dense packet without discarding EEGle timing metadata.

    MNE stores EEG values in volts and samples by channel, so supported channel
    units are scaled explicitly and the EEGle samples-by-channel matrix is
    transposed. Clock identities and boundary availability remain in the
    returned sidecar rather than being presented as an MNE timing guarantee.
    """

    if not isinstance(batch, DenseSampleBatch) or not isinstance(stream, StreamSpec):
        raise TypeError("MNE export requires a DenseSampleBatch and StreamSpec")
    if batch.stream_id != stream.stream_id or batch.stream_revision != stream.revision:
        raise ValueError("packet and stream identities do not match")
    if stream.sample_rate_hz is None:
        raise ValueError("MNE RawArray export requires a regular sample rate")
    channel_ids = tuple(value.channel_id for value in stream.channels)
    if batch.channel_ids != channel_ids:
        raise ValueError("packet channel order does not match the StreamSpec")
    scales = np.asarray([_unit_scale(value.unit) for value in stream.channels], dtype=float)
    values = np.asarray(batch.values, dtype=float)
    if batch.validity_mask is not None:
        values = np.where(batch.validity_mask, values, np.nan)
    data = (values * scales[np.newaxis, :]).T
    if copy:
        data = data.copy()
    mne = _mne_module()
    info = mne.create_info(
        ch_names=list(channel_ids),
        sfreq=float(stream.sample_rate_hz),
        ch_types=["eeg" if stream.modality.lower() == "eeg" else "misc"] * len(channel_ids),
    )
    raw = mne.io.RawArray(data, info, verbose="ERROR")
    return MneRawExport(
        raw,
        batch.stream_id,
        batch.stream_revision,
        batch.first_sample_time.seconds,
        batch.first_sample_time.clock_id,
        batch.available_time.seconds,
        batch.available_time.clock_id,
    )


def _mne_module() -> Any:
    try:
        return importlib.import_module("mne")
    except (ImportError, ModuleNotFoundError) as exc:
        raise ModuleNotFoundError(
            "MNE export requires the optional 'eegle[analysis]' dependency"
        ) from exc


def _unit_scale(unit: str) -> float:
    normalized = unit.strip().replace("µ", "u").lower()
    scales = {
        "v": 1.0,
        "mv": 1e-3,
        "uv": 1e-6,
        "nv": 1e-9,
    }
    try:
        return scales[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported MNE voltage unit: {unit}") from exc


__all__ = ["MneRawExport", "dense_batch_to_mne_raw"]
