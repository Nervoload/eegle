"""Captured packets exposed through the same source boundary as live inputs."""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Mapping

from eegle._domain import ComponentKind
from eegle.compiler.plan import ExecutionPlan
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

    The semantic engine does not sleep in either original or accelerated mode.
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
        # Captured source-contract violations must be replayed through admission
        # so their rejection evidence remains reproducible.
        super().__init__(stream_spec, packets, validate_packets=False)


def build_replay_source_overrides(
    plan: ExecutionPlan,
    streams: Iterable[StreamSpec],
    packets: Iterable[Packet],
    *,
    mode: ReplayMode = ReplayMode.ACCELERATED_CAUSAL,
) -> Mapping[str, Any]:
    """Bind an exact capture to every locked source component in a plan."""

    stream_values = tuple(streams)
    stream_by_id = {value.stream_id: value for value in stream_values}
    if len(stream_by_id) != len(stream_values):
        raise ValueError("replay stream specifications must have unique stream IDs")
    packets_by_stream: dict[str, list[Packet]] = {}
    for packet in packets:
        packets_by_stream.setdefault(packet.stream_id, []).append(packet)
    plugin_kind = {
        (value.plugin_id, value.version): value.kind for value in plan.plugins
    }
    source_components = tuple(
        value
        for value in plan.components
        if plugin_kind[(value.plugin_id, value.plugin_version)] == ComponentKind.SOURCE
    )
    stream_ids = [value.stream_id for value in source_components]
    if any(value is None for value in stream_ids):
        missing = [
            value.component_id for value in source_components if value.stream_id is None
        ]
        raise ValueError(f"replay sources do not declare stream IDs: {missing}")
    if len(stream_ids) != len(set(stream_ids)):
        raise ValueError("each replay source must own a distinct stream")
    unknown_packet_streams = set(packets_by_stream) - set(stream_ids)
    if unknown_packet_streams:
        raise ValueError(
            "capture contains streams without locked source components: "
            f"{sorted(unknown_packet_streams)}"
        )
    overrides: dict[str, ReplaySource] = {}
    for component in source_components:
        assert component.stream_id is not None
        try:
            stream = stream_by_id[component.stream_id]
        except KeyError as exc:
            raise ValueError(
                f"replay is missing StreamSpec for {component.stream_id}"
            ) from exc
        overrides[component.component_id] = ReplaySource(
            stream,
            packets_by_stream.get(component.stream_id, ()),
            mode=mode,
        )
    return overrides
