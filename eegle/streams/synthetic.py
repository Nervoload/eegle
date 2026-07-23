"""Deterministic finite sources for simulation and engine contract tests."""

from __future__ import annotations

from typing import Iterable

from eegle.streams.channels import StreamSpec
from eegle.streams.clocks import TimePoint
from eegle.streams.packets import DenseSampleBatch, MetadataEvent, Packet, SparseEventBatch


def packet_available_time(packet: Packet) -> TimePoint:
    if isinstance(packet, (DenseSampleBatch, MetadataEvent)):
        return packet.available_time
    if isinstance(packet, SparseEventBatch):
        point = packet.events[0].available_time
        for event in packet.events[1:]:
            if event.available_time.clock_id != point.clock_id:
                raise ValueError("sparse event batch uses multiple availability clocks")
            if event.available_time.seconds > point.seconds:
                point = event.available_time
        return point
    raise TypeError(f"unsupported packet type: {type(packet).__name__}")


class PacketSequenceSource:
    """A finite source with a monotonic scheduling watermark."""

    def __init__(self, stream_spec: StreamSpec, packets: Iterable[Packet]) -> None:
        self._stream_spec = stream_spec
        self._packets = tuple(packets)
        self._index = 0
        self._closed = False
        self._watermark: TimePoint | None = None
        for packet in self._packets:
            if packet.stream_id != stream_spec.stream_id:
                raise ValueError("source packet stream_id does not match stream_spec")
            if packet.stream_revision != stream_spec.revision:
                raise ValueError("source packet revision does not match stream_spec")

    @property
    def stream_spec(self) -> StreamSpec:
        return self._stream_spec

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self._packets)

    @property
    def watermark(self) -> TimePoint | None:
        return self._watermark

    def read(self) -> Packet | None:
        if self._closed or self.exhausted:
            return None
        packet = self._packets[self._index]
        self._index += 1
        available = packet_available_time(packet)
        if self._watermark is not None:
            if available.clock_id != self._watermark.clock_id:
                raise ValueError("source availability clock changed")
            if available.seconds < self._watermark.seconds:
                # The engine owns the lateness policy; the source still reports
                # its honest latest monotonic watermark.
                return packet
        self._watermark = available
        return packet

    def close(self) -> None:
        self._closed = True
