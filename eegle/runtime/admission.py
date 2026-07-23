"""Source admission, monotonic availability, and watermark frontiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from eegle.runtime.plan_runtime import PlanRuntime, RuntimeNode
from eegle.streams.clocks import TimePoint
from eegle.streams.packets import DenseSampleBatch, MetadataEvent, Packet, SparseEventBatch
from eegle.streams.synthetic import packet_available_time


RequireExecutionTime = Callable[[TimePoint], None]


@dataclass(slots=True)
class SourceAdmissionState:
    last_available_seconds: dict[str, float] = field(default_factory=dict)
    incomplete_sources: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class AdmittedSourcePacket:
    packet: Packet
    available_time: TimePoint
    output_port: str


def poll_source(
    node: RuntimeNode,
    state: SourceAdmissionState,
    require_execution_time: RequireExecutionTime,
) -> AdmittedSourcePacket | None:
    """Read one packet and enforce the source's monotonic availability contract."""

    source = node.component
    if bool(getattr(source, "exhausted", False)):
        state.incomplete_sources.discard(node.component_id)
        return None
    declared_watermark = getattr(source, "watermark", None)
    if declared_watermark is not None:
        if not isinstance(declared_watermark, TimePoint):
            raise TypeError(f"source {node.component_id} exposed an invalid watermark")
        require_execution_time(declared_watermark)
    packet = source.read()
    if packet is None:
        if bool(getattr(source, "exhausted", False)):
            state.incomplete_sources.discard(node.component_id)
        else:
            state.incomplete_sources.add(node.component_id)
        return None
    if not isinstance(packet, (DenseSampleBatch, SparseEventBatch, MetadataEvent)):
        raise TypeError(f"source {node.component_id} returned a non-packet value")
    available = packet_available_time(packet)
    require_execution_time(available)
    if declared_watermark is not None and available.seconds < declared_watermark.seconds:
        raise ValueError(
            f"source {node.component_id} emitted availability before its "
            "declared watermark"
        )
    prior = state.last_available_seconds.get(node.component_id)
    if prior is not None and available.seconds < prior:
        raise ValueError(f"source {node.component_id} availability moved backwards")
    state.last_available_seconds[node.component_id] = available.seconds
    output_ports = node.descriptor.output_ports
    if len(output_ports) != 1:
        raise TypeError("specialized source requires exactly one output port")
    state.incomplete_sources.discard(node.component_id)
    return AdmittedSourcePacket(packet, available, output_ports[0].name)


def watermark_blockers(
    runtime: PlanRuntime,
    event_time: TimePoint,
    state: SourceAdmissionState,
    require_execution_time: RequireExecutionTime,
) -> tuple[str, ...]:
    """Return active sources whose frontier cannot yet release an event."""

    blockers: list[str] = []
    for component_id in sorted(state.incomplete_sources):
        source = runtime.node(component_id).component
        if bool(getattr(source, "exhausted", False)):
            continue
        watermark = getattr(source, "watermark", None)
        if watermark is None:
            blockers.append(component_id)
            continue
        if not isinstance(watermark, TimePoint):
            raise TypeError(f"source {component_id} exposed an invalid watermark")
        require_execution_time(watermark)
        if watermark.seconds < event_time.seconds:
            blockers.append(component_id)
    return tuple(blockers)
