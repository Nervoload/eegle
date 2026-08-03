"""Dependency-lazy MNE bridges with explicit EEGle timing sidecars."""

from __future__ import annotations

import importlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from eegle._validation import require_identifier, thaw_json
from eegle.processing import DenseWindow
from eegle.streams import (
    ChannelSpec,
    ContentKind,
    DenseSampleBatch,
    MetadataEvent,
    MissingDataPolicy,
    RateModel,
    SparseEvent,
    SparseEventBatch,
    StreamSpec,
    TimePoint,
)
from eegle.streams.packets import Packet


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
    sample_times_seconds: tuple[float, ...]
    timing_representation: str


@dataclass(frozen=True, slots=True)
class MneAnnotationsExport:
    """MNE annotations plus exact sparse-event identity and availability."""

    annotations: Any
    origin_time: TimePoint
    event_ids: tuple[str, ...]
    event_values: tuple[Any, ...]
    event_times_seconds: tuple[float, ...]
    available_times: tuple[TimePoint, ...]
    descriptions: tuple[str, ...]
    durations_seconds: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class MneEpochsExport:
    """MNE epochs plus the admitted windows and causality MNE cannot encode."""

    epochs: Any
    stream_id: str
    stream_revision: int
    window_ids: tuple[str, ...]
    sequence_spans: tuple[tuple[int, int], ...]
    start_times_seconds: tuple[float, ...]
    end_times_seconds: tuple[float, ...]
    sample_clock_id: str
    available_times: tuple[TimePoint, ...]
    input_ids: tuple[tuple[str, ...], ...]
    tmin_seconds: float
    timing_projection: str = "regular_grid_from_window_bounds"


@dataclass(frozen=True, slots=True)
class MneReplayInputs:
    """Typed streams and packets ready for the normal EEGle replay boundary."""

    streams: tuple[StreamSpec, ...]
    packets: tuple[Packet, ...]

    @property
    def dense_packet(self) -> DenseSampleBatch:
        return next(value for value in self.packets if isinstance(value, DenseSampleBatch))

    @property
    def marker_packet(self) -> SparseEventBatch | None:
        return next(
            (value for value in self.packets if isinstance(value, SparseEventBatch)),
            None,
        )


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
    data = _scaled_channel_data(batch, stream)
    if copy:
        data = data.copy()
    mne = _mne_module()
    info = mne.create_info(
        ch_names=list(channel_ids),
        sfreq=float(stream.sample_rate_hz),
        ch_types=["eeg" if stream.modality.lower() == "eeg" else "misc"] * len(channel_ids),
    )
    raw = mne.io.RawArray(data, info, verbose="ERROR")
    if batch.first_sample_time is not None:
        first_sample = batch.first_sample_time
        sample_times = tuple(
            first_sample.seconds + index * float(batch.sample_period_seconds)
            for index in range(batch.sample_count)
        )
        timing_representation = "regular"
    else:
        first_sample = batch.sample_times[0]
        sample_times = tuple(value.seconds for value in batch.sample_times)
        timing_representation = "explicit"
    return MneRawExport(
        raw,
        batch.stream_id,
        batch.stream_revision,
        first_sample.seconds,
        first_sample.clock_id,
        batch.available_time.seconds,
        batch.available_time.clock_id,
        sample_times,
        timing_representation,
    )


def sparse_events_to_mne_annotations(
    values: Any,
    *,
    origin_time: TimePoint,
    default_duration_seconds: float = 0.0,
) -> MneAnnotationsExport:
    """Project sparse marker events onto MNE's raw-relative annotation axis."""

    if not isinstance(origin_time, TimePoint):
        raise TypeError("MNE annotation origin_time must be a TimePoint")
    duration_default = float(default_duration_seconds)
    if not math.isfinite(duration_default) or duration_default < 0:
        raise ValueError("default annotation duration must be finite and nonnegative")
    events = _sparse_events(values)
    if not events:
        raise ValueError("MNE annotation export requires at least one sparse event")
    onsets: list[float] = []
    durations: list[float] = []
    descriptions: list[str] = []
    for event in events:
        if event.event_time.clock_id != origin_time.clock_id:
            raise ValueError("marker event and MNE annotation origin must share a clock")
        onset = event.event_time.seconds - origin_time.seconds
        if onset < 0:
            raise ValueError("marker event precedes the MNE annotation origin")
        duration = _annotation_duration(event, duration_default)
        onsets.append(onset)
        durations.append(duration)
        descriptions.append(_annotation_description(event))
    mne = _mne_module()
    annotations = mne.Annotations(
        onset=onsets,
        duration=durations,
        description=descriptions,
        orig_time=None,
    )
    return MneAnnotationsExport(
        annotations=annotations,
        origin_time=origin_time,
        event_ids=tuple(value.event_id for value in events),
        event_values=tuple(value.value for value in events),
        event_times_seconds=tuple(value.event_time.seconds for value in events),
        available_times=tuple(value.available_time for value in events),
        descriptions=tuple(descriptions),
        durations_seconds=tuple(durations),
    )


def attach_annotations_to_mne_raw(
    raw_export: MneRawExport,
    annotation_export: MneAnnotationsExport,
    *,
    copy: bool = True,
) -> MneRawExport:
    """Attach compatible annotations while retaining both EEGle sidecars."""

    if not isinstance(raw_export, MneRawExport):
        raise TypeError("raw_export must be an MneRawExport")
    if not isinstance(annotation_export, MneAnnotationsExport):
        raise TypeError("annotation_export must be an MneAnnotationsExport")
    origin = annotation_export.origin_time
    if (
        origin.clock_id != raw_export.source_clock_id
        or origin.seconds != raw_export.first_sample_seconds
    ):
        raise ValueError("annotation origin does not match the exported raw sample origin")
    raw = raw_export.raw.copy() if copy else raw_export.raw
    raw.set_annotations(annotation_export.annotations)
    return MneRawExport(
        raw=raw,
        stream_id=raw_export.stream_id,
        stream_revision=raw_export.stream_revision,
        first_sample_seconds=raw_export.first_sample_seconds,
        source_clock_id=raw_export.source_clock_id,
        available_seconds=raw_export.available_seconds,
        availability_clock_id=raw_export.availability_clock_id,
        sample_times_seconds=raw_export.sample_times_seconds,
        timing_representation=raw_export.timing_representation,
    )


def dense_windows_to_mne_epochs(
    windows: Any,
    stream: StreamSpec,
    *,
    tmin_seconds: float = 0.0,
    copy: bool = True,
) -> MneEpochsExport:
    """Convert admitted dense windows into an ``EpochsArray`` plus lineage."""

    values = tuple(windows)
    if not values:
        raise ValueError("MNE epoch export requires at least one admitted window")
    if not isinstance(stream, StreamSpec):
        raise TypeError("MNE epoch export requires a StreamSpec")
    if stream.sample_rate_hz is None:
        raise ValueError("MNE epoch export requires a regular sample rate")
    if any(not isinstance(value, DenseWindow) for value in values):
        raise TypeError("MNE epoch export accepts only DenseWindow values")
    first = values[0]
    sample_count = first.sample_count
    for window in values:
        if (
            window.stream_id != stream.stream_id
            or window.stream_revision != stream.revision
        ):
            raise ValueError("window and stream identities do not match")
        if window.channel_ids != first.channel_ids or window.sample_count != sample_count:
            raise ValueError("MNE epochs require equal channel order and sample count")
        if window.start_time.clock_id != first.start_time.clock_id:
            raise ValueError("MNE epoch windows must share one sample clock")
    data = np.stack([_scaled_channel_data(value, stream) for value in values])
    if copy:
        data = data.copy()
    tmin = float(tmin_seconds)
    if not math.isfinite(tmin):
        raise ValueError("MNE epoch tmin must be finite")
    mne = _mne_module()
    info = _mne_info(mne, stream)
    events = np.column_stack(
        (
            np.arange(len(values), dtype=int) * sample_count,
            np.zeros(len(values), dtype=int),
            np.ones(len(values), dtype=int),
        )
    )
    epochs = mne.EpochsArray(
        data,
        info,
        events=events,
        event_id={"eegle_window": 1},
        tmin=tmin,
        baseline=None,
        verbose="ERROR",
    )
    return MneEpochsExport(
        epochs=epochs,
        stream_id=stream.stream_id,
        stream_revision=stream.revision,
        window_ids=tuple(value.window_id for value in values),
        sequence_spans=tuple(
            (value.sequence_start, value.sequence_end) for value in values
        ),
        start_times_seconds=tuple(value.start_time.seconds for value in values),
        end_times_seconds=tuple(value.end_time.seconds for value in values),
        sample_clock_id=first.start_time.clock_id,
        available_times=tuple(value.available_time for value in values),
        input_ids=tuple(value.input_ids for value in values),
        tmin_seconds=tmin,
    )


def mne_raw_to_replay_inputs(
    raw: Any,
    *,
    stream_id: str,
    first_sample_time: TimePoint,
    received_time: TimePoint,
    available_time: TimePoint,
    stream_revision: int = 1,
    batch_id: str | None = None,
    channel_ids: tuple[str, ...] | None = None,
    marker_stream_id: str | None = None,
    marker_stream_revision: int = 1,
) -> MneReplayInputs:
    """Create explicit EEGle replay streams without inferring clock authority.

    MNE annotations become a separate sparse stream. They are never embedded in
    dense packet metadata, which keeps model inference inputs label-blind.
    """

    if not isinstance(first_sample_time, TimePoint) or not isinstance(
        available_time, TimePoint
    ):
        raise TypeError("MNE replay conversion requires explicit TimePoint authority")
    received = received_time
    if not isinstance(received, TimePoint):
        raise TypeError("received_time must be a TimePoint")
    get_data = getattr(raw, "get_data", None)
    if not callable(get_data):
        raise TypeError("MNE replay conversion requires a Raw-like get_data()")
    data = np.asarray(get_data(), dtype=float)
    if data.ndim != 2 or not data.shape[0] or not data.shape[1]:
        raise ValueError("MNE Raw data must be a non-empty channels x samples array")
    info = getattr(raw, "info", None)
    if info is None:
        raise TypeError("MNE Raw data must expose info")
    sample_rate = float(info["sfreq"])
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("MNE Raw sampling frequency must be finite and positive")
    raw_names = tuple(str(value) for value in getattr(raw, "ch_names", ()))
    channels = raw_names if channel_ids is None else tuple(channel_ids)
    if len(channels) != data.shape[0]:
        raise ValueError("channel_ids must match the MNE Raw channel count")
    channels = tuple(require_identifier(value, "channel_id") for value in channels)
    get_types = getattr(raw, "get_channel_types", None)
    channel_types = (
        tuple(str(value) for value in get_types())
        if callable(get_types)
        else ("eeg",) * len(channels)
    )
    if len(channel_types) != len(channels) or any(
        value != "eeg" for value in channel_types
    ):
        raise ValueError("the v1 MNE replay bridge accepts EEG channels only")
    invalid = ~np.isfinite(data.T)
    mask = None if not bool(np.any(invalid)) else ~invalid
    revision = int(stream_revision)
    dense_stream_id = require_identifier(stream_id, "stream_id")
    stream = StreamSpec(
        stream_id=dense_stream_id,
        revision=revision,
        modality="eeg",
        content_kind=ContentKind.DENSE_SAMPLES,
        rate_model=RateModel.REGULAR,
        clock_id=first_sample_time.clock_id,
        channels=tuple(ChannelSpec(value, "eeg", "V") for value in channels),
        sample_rate_hz=sample_rate,
        sample_dtype=data.dtype.name,
        missing_data_policy=(
            MissingDataPolicy.FORBID
            if mask is None
            else MissingDataPolicy.VALIDITY_MASK
        ),
        metadata={
            "source": "mne_raw",
            "mne_channel_types": list(channel_types),
            "annotations_separate": True,
        },
    )
    dense = DenseSampleBatch(
        batch_id=batch_id or f"batch.{dense_stream_id}.mne",
        stream_id=dense_stream_id,
        stream_revision=revision,
        sequence_start=0,
        channel_ids=channels,
        values=data.T,
        validity_mask=mask,
        received_time=received,
        available_time=available_time,
        first_sample_time=first_sample_time,
        sample_period_seconds=1.0 / sample_rate,
    )
    streams: list[StreamSpec] = [stream]
    packets: list[Packet] = [dense]
    annotation_values = getattr(raw, "annotations", None)
    if annotation_values is not None and len(annotation_values):
        if getattr(annotation_values, "orig_time", None) is not None:
            raise ValueError(
                "MNE replay annotations must be normalized to raw-relative time"
            )
        marker_id = require_identifier(
            marker_stream_id or f"{dense_stream_id}.markers",
            "marker_stream_id",
        )
        marker_revision = int(marker_stream_revision)
        marker_events = tuple(
            _mne_annotation_event(
                index,
                marker_id,
                first_sample_time,
                received,
                available_time,
                onset,
                duration,
                description,
            )
            for index, (onset, duration, description) in enumerate(
                zip(
                    annotation_values.onset,
                    annotation_values.duration,
                    annotation_values.description,
                ),
                start=1,
            )
        )
        marker_stream = StreamSpec(
            marker_id,
            marker_revision,
            "markers",
            ContentKind.SPARSE_EVENTS,
            RateModel.EVENT,
            first_sample_time.clock_id,
            metadata={
                "source": "mne_annotations",
                "separate_from_dense_input": True,
            },
        )
        marker_packet = SparseEventBatch(
            batch_id=f"batch.{marker_id}.mne",
            stream_id=marker_id,
            stream_revision=marker_revision,
            sequence_start=0,
            events=marker_events,
        )
        streams.append(marker_stream)
        packets.append(marker_packet)
    return MneReplayInputs(tuple(streams), tuple(packets))


def _mne_module() -> Any:
    try:
        return importlib.import_module("mne")
    except (ImportError, ModuleNotFoundError) as exc:
        raise ModuleNotFoundError(
            "MNE export requires the optional 'eegle[analysis]' dependency"
        ) from exc


def _mne_info(mne: Any, stream: StreamSpec) -> Any:
    return mne.create_info(
        ch_names=[value.channel_id for value in stream.channels],
        sfreq=float(stream.sample_rate_hz),
        ch_types=["eeg" if stream.modality.lower() == "eeg" else "misc"]
        * len(stream.channels),
    )


def _scaled_channel_data(
    value: DenseSampleBatch | DenseWindow,
    stream: StreamSpec,
) -> np.ndarray:
    channel_ids = tuple(item.channel_id for item in stream.channels)
    if value.channel_ids != channel_ids:
        raise ValueError("dense value channel order does not match the StreamSpec")
    scales = np.asarray(
        [_unit_scale(item.unit) for item in stream.channels],
        dtype=float,
    )
    values = np.asarray(value.values, dtype=float)
    if value.validity_mask is not None:
        values = np.where(value.validity_mask, values, np.nan)
    return (values * scales[np.newaxis, :]).T


def _sparse_events(values: Any) -> tuple[SparseEvent, ...]:
    events: list[SparseEvent] = []
    candidates = (
        (values,)
        if isinstance(values, (SparseEventBatch, SparseEvent, MetadataEvent))
        else values
    )
    for value in candidates:
        if isinstance(value, SparseEventBatch):
            events.extend(value.events)
        elif isinstance(value, SparseEvent):
            events.append(value)
        elif isinstance(value, MetadataEvent):
            events.append(
                SparseEvent(
                    value.event_id,
                    value.kind,
                    value.event_time,
                    value.received_time,
                    value.available_time,
                    thaw_json(value.metadata),
                )
            )
        else:
            raise TypeError(
                "MNE annotations require SparseEventBatch, SparseEvent, or MetadataEvent values"
            )
    return tuple(events)


def _annotation_duration(event: SparseEvent, default: float) -> float:
    value = event.value
    candidate = value.get("duration_seconds") if isinstance(value, Mapping) else None
    duration = default if candidate is None else float(candidate)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError(f"marker {event.event_id} has an invalid annotation duration")
    return duration


def _annotation_description(event: SparseEvent) -> str:
    value = event.value
    if isinstance(value, Mapping) and value.get("description") is not None:
        return str(value["description"])
    if isinstance(value, str):
        return value
    if value is None:
        return event.kind
    encoded = json.dumps(
        thaw_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"{event.kind}:{encoded}"


def _mne_annotation_event(
    index: int,
    stream_id: str,
    first_sample_time: TimePoint,
    received_time: TimePoint,
    available_time: TimePoint,
    onset: Any,
    duration: Any,
    description: Any,
) -> SparseEvent:
    onset_seconds = float(onset)
    duration_seconds = float(duration)
    if not math.isfinite(onset_seconds) or onset_seconds < 0:
        raise ValueError("MNE replay annotation onset must be finite and nonnegative")
    if not math.isfinite(duration_seconds) or duration_seconds < 0:
        raise ValueError("MNE replay annotation duration must be finite and nonnegative")
    event_time = TimePoint(
        first_sample_time.seconds + onset_seconds,
        first_sample_time.clock_id,
    )
    return SparseEvent(
        event_id=f"event.{stream_id}.{index}",
        kind="mne_annotation",
        event_time=event_time,
        source_time=event_time,
        received_time=received_time,
        available_time=available_time,
        value={
            "description": str(description),
            "duration_seconds": duration_seconds,
        },
    )


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


__all__ = [
    "MneAnnotationsExport",
    "MneEpochsExport",
    "MneRawExport",
    "MneReplayInputs",
    "attach_annotations_to_mne_raw",
    "dense_batch_to_mne_raw",
    "dense_windows_to_mne_epochs",
    "mne_raw_to_replay_inputs",
    "sparse_events_to_mne_annotations",
]
