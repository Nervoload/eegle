"""Modality-neutral stream records and source contracts.

LSL helpers remain available from their explicit legacy modules during the
migration; importing :mod:`eegle.streams` does not import LSL.
"""

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


__all__ = [
    "ChannelSpec",
    "ClockIdentity",
    "ClockKind",
    "ClockMapping",
    "ContentKind",
    "DenseSampleBatch",
    "MetadataEvent",
    "MissingDataPolicy",
    "RateModel",
    "Source",
    "SparseEvent",
    "SparseEventBatch",
    "StreamSpec",
    "TimePoint",
]
