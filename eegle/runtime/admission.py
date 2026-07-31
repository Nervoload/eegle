"""Source admission, monotonic availability, and watermark frontiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

from eegle.runtime.plan_runtime import PlanRuntime, RuntimeNode
from eegle.streams.channels import StreamSpec
from eegle.streams.clocks import TimePoint
from eegle.streams.packets import (
    DenseSampleBatch,
    MetadataEvent,
    Packet,
    SparseEventBatch,
)
from eegle.streams.synthetic import packet_available_time
from eegle.streams.validation import validate_packet_against_stream_spec

RequireExecutionTime = Callable[[TimePoint], None]


@dataclass(slots=True)
class SourceAdmissionState:
    last_available_seconds: dict[str, float] = field(default_factory=dict)
    next_sequences: dict[str, int] = field(default_factory=dict)
    incomplete_sources: set[str] = field(default_factory=set)
    packet_rejections: dict[tuple[str, str], str] = field(default_factory=dict)
    packet_rejection_codes: dict[tuple[str, str], str] = field(default_factory=dict)
    sequence_gaps: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)
    late_packets: dict[tuple[str, str], tuple[float, str]] = field(default_factory=dict)
    watermark_lateness: dict[tuple[str, str], float] = field(default_factory=dict)


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
    prior = state.last_available_seconds.get(node.component_id)
    state.last_available_seconds[node.component_id] = (
        available.seconds if prior is None else max(prior, available.seconds)
    )
    output_ports = node.descriptor.output_ports
    if len(output_ports) != 1:
        raise TypeError("specialized source requires exactly one output port")
    packet_id = _packet_id(packet)
    if declared_watermark is not None and available.seconds < declared_watermark.seconds:
        state.watermark_lateness[(node.component_id, packet_id)] = (
            declared_watermark.seconds - available.seconds
        )
    try:
        stream_spec = getattr(source, "stream_spec", None)
        if not isinstance(stream_spec, StreamSpec):
            raise TypeError(f"source {node.component_id} exposed an invalid stream_spec")
        if node.planned.stream_id != stream_spec.stream_id:
            raise ValueError(
                f"source {node.component_id} stream_spec differs from its planned stream_id"
            )
        locked_payload = node.planned.config.get("stream_spec")
        if locked_payload is not None:
            if not isinstance(locked_payload, Mapping):
                raise TypeError(
                    f"source {node.component_id} has an invalid locked stream_spec"
                )
            if StreamSpec.from_payload(locked_payload) != stream_spec:
                raise ValueError(
                    f"source {node.component_id} stream_spec differs from its locked configuration"
                )
        validate_packet_against_stream_spec(packet, stream_spec)
    except (TypeError, ValueError) as exc:
        key = (node.component_id, packet_id)
        prior_rejection = state.packet_rejections.get(key)
        contract_rejection = f"{type(exc).__name__}: {exc}"
        state.packet_rejections[key] = (
            contract_rejection
            if prior_rejection is None
            else f"{prior_rejection}; {contract_rejection}"
        )
        state.packet_rejection_codes.setdefault(key, "source_contract_violation")

    sequence_start, sequence_end = _sequence_range(packet)
    expected = state.next_sequences.get(node.component_id)
    if expected is not None and sequence_start != expected:
        state.sequence_gaps[(node.component_id, packet_id)] = (expected, sequence_start)
    state.next_sequences[node.component_id] = sequence_end + 1
    state.incomplete_sources.discard(node.component_id)
    return AdmittedSourcePacket(packet, available, output_ports[0].name)


def _packet_id(packet: Packet) -> str:
    if isinstance(packet, (DenseSampleBatch, SparseEventBatch)):
        return packet.batch_id
    return packet.event_id


def _sequence_range(packet: Packet) -> tuple[int, int]:
    if isinstance(packet, (DenseSampleBatch, SparseEventBatch)):
        return packet.sequence_start, packet.sequence_end
    return packet.sequence, packet.sequence


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
