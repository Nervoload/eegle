"""Captured packets exposed through the same source boundary as live inputs."""

from __future__ import annotations

from enum import Enum
from typing import Iterable

from eegle.streams.channels import StreamSpec
from eegle.streams.packets import Packet
from eegle.streams.synthetic import PacketSequenceSource


class ReplayMode(str, Enum):
    ORIGINAL_AVAILABILITY = "original_availability"
    ACCELERATED_CAUSAL = "accelerated_causal"
    COUNTERFACTUAL = "counterfactual"
    RETROSPECTIVE = "retrospective"
    ORACLE = "oracle"


class ReplaySource(PacketSequenceSource):
    """A finite capture source; pacing is independent from virtual causality.

    The Phase 3 engine does not sleep in either original or accelerated mode.
    Both retain original availability timestamps and ordering; an outer live
    adapter may pace original availability against wall time without changing
    semantic execution.
    """

    def __init__(
        self,
        stream_spec: StreamSpec,
        packets: Iterable[Packet],
        *,
        mode: ReplayMode = ReplayMode.ACCELERATED_CAUSAL,
    ) -> None:
        self.mode = ReplayMode(mode)
        super().__init__(stream_spec, packets)
