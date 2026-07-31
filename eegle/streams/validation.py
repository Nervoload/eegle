"""Exact packet validation against an admitted stream specification."""

from __future__ import annotations

import math

import numpy as np

from eegle.streams.channels import (
    ContentKind,
    MissingDataPolicy,
    RateModel,
    StreamSpec,
)
from eegle.streams.packets import (
    DenseSampleBatch,
    MetadataEvent,
    Packet,
    SparseEventBatch,
)


def validate_packet_against_stream_spec(packet: Packet, stream_spec: StreamSpec) -> None:
    """Fail closed when a source packet differs from its exact stream lock."""

    if packet.stream_id != stream_spec.stream_id:
        raise ValueError("source packet stream_id does not match stream_spec")
    if packet.stream_revision != stream_spec.revision:
        raise ValueError("source packet revision does not match stream_spec")

    if isinstance(packet, DenseSampleBatch):
        _validate_dense_packet(packet, stream_spec)
        return
    if isinstance(packet, SparseEventBatch):
        if stream_spec.content_kind != ContentKind.SPARSE_EVENTS:
            raise ValueError("sparse source packet does not match stream content_kind")
        if stream_spec.rate_model != RateModel.EVENT:
            raise ValueError("sparse source packet requires an event-rate stream")
        for event in packet.events:
            if event.event_time.clock_id != stream_spec.clock_id:
                raise ValueError("sparse event time does not use the stream clock")
            if (
                event.source_time is not None
                and event.source_time.clock_id != stream_spec.clock_id
            ):
                raise ValueError("sparse event source_time does not use the stream clock")
        return
    if isinstance(packet, MetadataEvent):
        if stream_spec.content_kind != ContentKind.METADATA:
            raise ValueError("metadata source packet does not match stream content_kind")
        if stream_spec.rate_model != RateModel.EVENT:
            raise ValueError("metadata source packet requires an event-rate stream")
        if packet.event_time.clock_id != stream_spec.clock_id:
            raise ValueError("metadata event time does not use the stream clock")
        return
    raise TypeError(f"unsupported source packet type: {type(packet).__name__}")


def _validate_dense_packet(packet: DenseSampleBatch, stream_spec: StreamSpec) -> None:
    if stream_spec.content_kind != ContentKind.DENSE_SAMPLES:
        raise ValueError("dense source packet does not match stream content_kind")
    expected_channels = tuple(value.channel_id for value in stream_spec.channels)
    if packet.channel_ids != expected_channels:
        raise ValueError("dense source packet channel_ids do not match stream_spec")
    expected_dtype = np.dtype(stream_spec.sample_dtype)
    if packet.values.dtype != expected_dtype:
        raise ValueError("dense source packet dtype does not match stream_spec")
    if (
        stream_spec.missing_data_policy == MissingDataPolicy.FORBID
        and packet.validity_mask is not None
        and not bool(np.all(packet.validity_mask))
    ):
        raise ValueError("dense source packet contains missing data forbidden by stream_spec")

    if packet.first_sample_time is not None:
        sample_clock = packet.first_sample_time.clock_id
    else:
        sample_clock = packet.sample_times[0].clock_id
    if sample_clock != stream_spec.clock_id:
        raise ValueError("dense source packet timing does not use the stream clock")

    if stream_spec.rate_model == RateModel.REGULAR:
        assert stream_spec.sample_rate_hz is not None
        expected_period = 1.0 / stream_spec.sample_rate_hz
        if packet.sample_period_seconds is not None:
            if not math.isclose(
                packet.sample_period_seconds,
                expected_period,
                rel_tol=1e-12,
                abs_tol=0.0,
            ):
                raise ValueError("dense source packet rate does not match stream_spec")
        # Explicit timestamps are observed timing evidence. A regular stream's
        # nominal rate remains locked by StreamSpec; jitter and measured gaps
        # must not be rewritten merely to make the observations look regular.
        return

    if packet.sample_times == ():
        raise ValueError("non-regular dense streams require explicit sample_times")
