"""Modality-neutral stream records and source contracts.

LSL helpers remain available from their explicit legacy modules during the
migration; importing :mod:`eegle.streams` does not import LSL.
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
]
