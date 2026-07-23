"""Executable descriptors for the dependency-light first-party components."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from eegle._domain import ComponentKind, Determinism, EquivalenceLevel, ExecutionMode
from eegle.actions.policies import ObserveOnlyPolicy
from eegle.models.builtins import MeanThresholdModel
from eegle.plugins.registry import (
    PluginCapabilities,
    PluginDescriptor,
    PortSpec,
    StateBehavior,
)
from eegle.processing.quality import FiniteQualityGate
from eegle.processing.transforms import CausalSosFilter, IdentityTransform, RetrospectiveSosFilter
from eegle.processing.windows import ContinuousWindowBuilder
from eegle.streams.channels import ContentKind, StreamSpec
from eegle.streams.packets import DenseSampleBatch
from eegle.streams.synthetic import PacketSequenceSource


_DENSE_PACKET = "eegle.dense_sample_batch.v1"
_QUALITY_DECISION = "eegle.quality_decision.v1"
_DENSE_WINDOW = "eegle.dense_window.v1"
_PREDICTION = "eegle.prediction.v1"
_SCHEMA_BASE = "https://json-schema.org/draft/2020-12/schema"


def builtin_plugin_descriptors() -> tuple[PluginDescriptor, ...]:
    """Return new immutable descriptors for the base processing set."""

    return (
        PluginDescriptor(
            plugin_id="eegle.sources.packet_sequence_dense",
            version="0.1.0",
            kind=ComponentKind.SOURCE,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {
                    "stream_spec": {"type": "object"},
                    "packets": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["stream_spec"],
                "additionalProperties": False,
            },
            input_ports=(),
            output_ports=(PortSpec("samples", _DENSE_PACKET, multiple=True),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.SNAPSHOT_RESTORE,
            ),
            factory=_packet_sequence_dense_factory,
            implementation="eegle.streams.synthetic:PacketSequenceSource",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.processing.identity",
            version="0.1.0",
            kind=ComponentKind.TRANSFORM,
            config_schema=_output_schema(),
            input_ports=(PortSpec("samples", _DENSE_PACKET),),
            output_ports=(PortSpec("samples", _DENSE_PACKET),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.STATELESS,
            ),
            factory=lambda config: IdentityTransform(**_output_config(config)),
            implementation="eegle.processing.transforms:IdentityTransform",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.processing.causal_sos",
            version="0.1.0",
            kind=ComponentKind.TRANSFORM,
            config_schema=_sos_schema(channel_count=True),
            input_ports=(PortSpec("samples", _DENSE_PACKET),),
            output_ports=(PortSpec("samples", _DENSE_PACKET),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(
                    {ExecutionMode.CAUSAL, ExecutionMode.RETROSPECTIVE}
                ),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.NUMERIC,
                state_behavior=StateBehavior.SNAPSHOT_RESTORE,
            ),
            factory=_causal_sos_factory,
            implementation="eegle.processing.transforms:CausalSosFilter",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.processing.retrospective_sos",
            version="0.1.0",
            kind=ComponentKind.TRANSFORM,
            config_schema=_sos_schema(channel_count=False),
            input_ports=(PortSpec("samples", _DENSE_PACKET),),
            output_ports=(PortSpec("samples", _DENSE_PACKET),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(
                    {ExecutionMode.RETROSPECTIVE, ExecutionMode.ORACLE}
                ),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.NUMERIC,
                state_behavior=StateBehavior.STATELESS,
                requires_future=True,
            ),
            factory=_retrospective_sos_factory,
            implementation="eegle.processing.transforms:RetrospectiveSosFilter",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.processing.finite_quality",
            version="0.1.0",
            kind=ComponentKind.QUALITY,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {
                    "gate_id": {"type": "string", "minLength": 1},
                    "minimum_valid_fraction": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                },
                "additionalProperties": False,
            },
            input_ports=(PortSpec("item", _DENSE_PACKET),),
            output_ports=(PortSpec("decision", _QUALITY_DECISION),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.SEMANTIC,
                state_behavior=StateBehavior.STATELESS,
            ),
            factory=lambda config: FiniteQualityGate(
                gate_id=str(config.get("gate_id", "eegle.finite")),
                minimum_valid_fraction=float(config.get("minimum_valid_fraction", 1.0)),
            ),
            implementation="eegle.processing.quality:FiniteQualityGate",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.processing.continuous_window",
            version="0.1.0",
            kind=ComponentKind.WINDOW,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {
                    "window_samples": {"type": "integer", "minimum": 1},
                    "step_samples": {"type": "integer", "minimum": 1},
                },
                "required": ["window_samples", "step_samples"],
                "additionalProperties": False,
            },
            input_ports=(PortSpec("samples", _DENSE_PACKET),),
            output_ports=(PortSpec("windows", _DENSE_WINDOW, multiple=True),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.SNAPSHOT_RESTORE,
            ),
            factory=lambda config: ContinuousWindowBuilder(
                window_samples=int(config["window_samples"]),
                step_samples=int(config["step_samples"]),
            ),
            implementation="eegle.processing.windows:ContinuousWindowBuilder",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.models.mean_threshold",
            version="0.1.0",
            kind=ComponentKind.MODEL,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {
                    "model_id": {"type": "string", "minLength": 1},
                    "role": {"type": "string", "minLength": 1},
                    "threshold": {"type": "number"},
                    "negative_label": {"type": "string", "minLength": 1},
                    "positive_label": {"type": "string", "minLength": 1},
                    "latency_seconds": {"type": "number", "minimum": 0.0},
                },
                "required": ["model_id", "role"],
                "additionalProperties": False,
            },
            input_ports=(PortSpec("window", _DENSE_WINDOW),),
            output_ports=(PortSpec("prediction", _PREDICTION),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.NUMERIC,
                state_behavior=StateBehavior.STATELESS,
            ),
            factory=lambda config: MeanThresholdModel(**dict(config)),
            implementation="eegle.models.builtins:MeanThresholdModel",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.actions.observe_only",
            version="0.1.0",
            kind=ComponentKind.POLICY,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            input_ports=(PortSpec("prediction", _PREDICTION),),
            output_ports=(),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.SEMANTIC,
                state_behavior=StateBehavior.STATELESS,
            ),
            factory=lambda config: ObserveOnlyPolicy(),
            implementation="eegle.actions.policies:ObserveOnlyPolicy",
            distribution="eegle",
        ),
    )


def _output_properties() -> dict[str, Any]:
    return {
        "output_stream_id": {"type": "string", "minLength": 1},
        "output_stream_revision": {"type": "integer", "minimum": 1},
    }


def _output_schema() -> dict[str, Any]:
    return {
        "$schema": _SCHEMA_BASE,
        "type": "object",
        "properties": _output_properties(),
        "dependentRequired": {"output_stream_id": ["output_stream_revision"]},
        "additionalProperties": False,
    }


def _sos_schema(*, channel_count: bool) -> dict[str, Any]:
    properties = _output_properties()
    properties["sos"] = {
        "type": "array",
        "minItems": 1,
        "items": {
            "type": "array",
            "minItems": 6,
            "maxItems": 6,
            "items": {"type": "number"},
        },
    }
    required = ["sos"]
    if channel_count:
        properties["channel_count"] = {"type": "integer", "minimum": 1}
        required.append("channel_count")
    return {
        "$schema": _SCHEMA_BASE,
        "type": "object",
        "properties": properties,
        "required": required,
        "dependentRequired": {"output_stream_id": ["output_stream_revision"]},
        "additionalProperties": False,
    }


def _output_config(config: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "output_stream_id": config.get("output_stream_id"),
        "output_stream_revision": config.get("output_stream_revision"),
    }


def _causal_sos_factory(config: Mapping[str, Any]) -> CausalSosFilter:
    return CausalSosFilter(
        np.asarray(config["sos"], dtype=float),
        channel_count=int(config["channel_count"]),
        **_output_config(config),
    )


def _retrospective_sos_factory(config: Mapping[str, Any]) -> RetrospectiveSosFilter:
    return RetrospectiveSosFilter(
        np.asarray(config["sos"], dtype=float),
        **_output_config(config),
    )


def _packet_sequence_dense_factory(config: Mapping[str, Any]) -> PacketSequenceSource:
    stream_spec = StreamSpec.from_payload(config["stream_spec"])
    if stream_spec.content_kind != ContentKind.DENSE_SAMPLES:
        raise ValueError("packet_sequence_dense requires a dense sample stream")
    packets = tuple(DenseSampleBatch.from_payload(value) for value in config.get("packets", ()))
    return PacketSequenceSource(stream_spec, packets)
