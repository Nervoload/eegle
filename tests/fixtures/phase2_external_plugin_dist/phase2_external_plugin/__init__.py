"""Independent packet-transform plugin used by EEGle's wheel boundary test."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from eegle.plugins import (
    ComponentKind,
    Determinism,
    EquivalenceLevel,
    ExecutionContext,
    ExecutionMode,
    PluginCapabilities,
    PluginDescriptor,
    PortSpec,
    StateBehavior,
)
from eegle.streams import DenseSampleBatch, Lineage


class ScaleTransform:
    def __init__(self, scale: float) -> None:
        self.scale = float(scale)

    def update(
        self,
        packet: DenseSampleBatch,
        context: ExecutionContext,
    ) -> DenseSampleBatch:
        if context.current_time.clock_id != packet.available_time.clock_id:
            raise ValueError("plugin and input must use the execution clock")
        if context.current_time.seconds < packet.available_time.seconds:
            raise ValueError("plugin cannot consume unavailable input")
        clock_mappings = dict(
            {} if packet.lineage is None else packet.lineage.clock_mapping_revisions
        )
        for mapping_id, revision in context.clock_mapping_revisions.items():
            previous = clock_mappings.get(mapping_id)
            if previous is not None and previous != revision:
                raise ValueError("clock mapping revision conflict")
            clock_mappings[mapping_id] = revision
        stream_revisions = dict(
            {} if packet.lineage is None else packet.lineage.stream_revisions
        )
        stream_revisions[packet.stream_id] = packet.stream_revision
        return DenseSampleBatch(
            batch_id=context.next_id("batch"),
            stream_id=packet.stream_id,
            stream_revision=packet.stream_revision,
            sequence_start=packet.sequence_start,
            channel_ids=packet.channel_ids,
            values=np.asarray(packet.values, dtype=float) * self.scale,
            received_time=packet.received_time,
            available_time=context.current_time,
            first_sample_time=packet.first_sample_time,
            sample_period_seconds=packet.sample_period_seconds,
            sample_times=packet.sample_times,
            validity_mask=packet.validity_mask,
            lineage=Lineage(
                component_id=context.component_id,
                input_ids=(packet.batch_id,),
                component_version=context.component_version,
                latest_input_available_time=packet.available_time,
                clock_mapping_revisions=clock_mappings,
                stream_revisions=stream_revisions,
            ),
        )


def plugin() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="fixture.external.scale",
        version="1.2.0",
        kind=ComponentKind.TRANSFORM,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"scale": {"type": "number"}},
            "required": ["scale"],
            "additionalProperties": False,
        },
        input_ports=(PortSpec("samples", "eegle.dense_sample_batch.v1"),),
        output_ports=(PortSpec("scaled", "eegle.dense_sample_batch.v1"),),
        capabilities=PluginCapabilities(
            supported_modes=frozenset(
                {ExecutionMode.CAUSAL, ExecutionMode.RETROSPECTIVE}
            ),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.NUMERIC,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=lambda config: ScaleTransform(float(config["scale"])),
        implementation="phase2_external_plugin:ScaleTransform",
    )
