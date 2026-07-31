"""Modality-neutral stream records and source contracts.

Transport integrations such as LSL are optional plugins rather than stream
kernel facades.
"""

from eegle._domain import Lineage
from eegle.streams.channels import (
    ChannelSpec,
    ContentKind,
    MissingDataPolicy,
    RateModel,
    StreamSpec,
)
from eegle.streams.clocks import ClockIdentity, ClockKind, ClockMapping, TimePoint
from eegle.streams.packets import DenseSampleBatch, MetadataEvent, SparseEvent, SparseEventBatch
from eegle.streams.sources import Source
from eegle.streams.synthetic import PacketSequenceSource
from eegle.streams.validation import validate_packet_against_stream_spec


__all__ = [
    "ChannelSpec",
    "ClockIdentity",
    "ClockKind",
    "ClockMapping",
    "ContentKind",
    "DenseSampleBatch",
    "Lineage",
    "MetadataEvent",
    "MissingDataPolicy",
    "PacketSequenceSource",
    "RateModel",
    "Source",
    "SparseEvent",
    "SparseEventBatch",
    "StreamSpec",
    "TimePoint",
    "validate_packet_against_stream_spec",
]
