"""Executable descriptors for the dependency-light first-party components."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from eegle._domain import ComponentKind, Determinism, EquivalenceLevel, ExecutionMode
from eegle.actions.policies import (
    LabelActionPolicy,
    ObserveOnlyPolicy,
    StructuredActionPolicy,
)
from eegle.actions.providers import SimulationAuthorizationProvider
from eegle.actions.simulated import SimulatedActuator
from eegle.models.predictions import PREDICTION_RECORD_SCHEMA
from eegle.plugins.registry import (
    ContractTransformSpec,
    PluginCapabilities,
    PluginDescriptor,
    PortSpec,
    StateBehavior,
)
from eegle.processing.quality import FiniteQualityGate
from eegle.processing.transforms import CausalSosFilter, IdentityTransform, RetrospectiveSosFilter
from eegle.processing.windows import ContinuousWindowBuilder, EventWindowBuilder
from eegle.recording.sinks import InMemoryRecordSink
from eegle.recording.producers import PredictionArtifactProducer
from eegle.runtime.builtins import (
    AdaptiveCounter,
    SparseEventOutcomeResolver,
    TriggerRecordSink,
)
from eegle.streams.channels import ContentKind, StreamSpec
from eegle.streams.packets import DenseSampleBatch, SparseEventBatch
from eegle.streams.synthetic import PacketSequenceSource


_DENSE_PACKET = "eegle.dense_sample_batch.v1"
_SPARSE_PACKET = "eegle.sparse_event_batch.v1"
_QUALITY_DECISION = "eegle.quality_decision.v1"
_DENSE_WINDOW = "eegle.dense_window.v1"
_PREDICTION = PREDICTION_RECORD_SCHEMA
_OUTCOME = "eegle.outcome.v2"
_STATE_TRANSITION = "eegle.state_transition.v1"
_ACTION_REQUEST = "eegle.action_request.v1"
_ACTION_RECEIPT = "eegle.action_receipt.v1"
_ARTIFACT_PUBLICATION = "eegle.artifact_publication.v1"
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
            plugin_id="eegle.sources.packet_sequence_sparse",
            version="0.1.0",
            kind=ComponentKind.SOURCE,
            config_schema=_packet_sequence_schema(),
            input_ports=(),
            output_ports=(PortSpec("events", _SPARSE_PACKET, multiple=True),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.SNAPSHOT_RESTORE,
            ),
            factory=_packet_sequence_sparse_factory,
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
            contract_transform=ContractTransformSpec("samples", "samples"),
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
            contract_transform=ContractTransformSpec("samples", "samples"),
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
            contract_transform=ContractTransformSpec("samples", "samples"),
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
            plugin_id="eegle.processing.event_window",
            version="0.1.0",
            kind=ComponentKind.WINDOW,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {
                    "start_offset_seconds": {"type": "number"},
                    "end_offset_seconds": {"type": "number"},
                    "event_kinds": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "max_buffer_samples": {"type": "integer", "minimum": 1},
                },
                "required": ["start_offset_seconds", "end_offset_seconds"],
                "additionalProperties": False,
            },
            input_ports=(
                PortSpec("samples", _DENSE_PACKET),
                PortSpec("events", _SPARSE_PACKET),
            ),
            output_ports=(PortSpec("windows", _DENSE_WINDOW, multiple=True),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.SNAPSHOT_RESTORE,
            ),
            factory=lambda config: EventWindowBuilder(
                start_offset_seconds=float(config["start_offset_seconds"]),
                end_offset_seconds=float(config["end_offset_seconds"]),
                event_kinds=tuple(str(value) for value in config.get("event_kinds", ())),
                max_buffer_samples=int(config.get("max_buffer_samples", 100_000)),
            ),
            implementation="eegle.processing.windows:EventWindowBuilder",
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
        PluginDescriptor(
            plugin_id="eegle.outcomes.sparse_event",
            version="0.1.0",
            kind=ComponentKind.OUTCOME,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {
                    "event_kind": {"type": "string", "minLength": 1},
                    "permitted_uses": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {
                            "enum": ["metrics", "calibration", "adaptation", "policy"]
                        },
                    },
                    "default_subject_id": {"type": "string", "minLength": 1},
                },
                "required": ["event_kind", "permitted_uses"],
                "additionalProperties": False,
            },
            input_ports=(PortSpec("events", _SPARSE_PACKET),),
            output_ports=(PortSpec("outcomes", _OUTCOME, multiple=True),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.STATELESS,
            ),
            factory=lambda config: SparseEventOutcomeResolver(
                event_kind=str(config["event_kind"]),
                permitted_uses=tuple(str(value) for value in config["permitted_uses"]),
                default_subject_id=str(config.get("default_subject_id", "subject.unknown")),
            ),
            implementation="eegle.runtime.builtins:SparseEventOutcomeResolver",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.adapters.adaptive_counter",
            version="0.1.0",
            kind=ComponentKind.ADAPTER,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {
                    "required_use": {
                        "enum": ["metrics", "calibration", "adaptation", "policy"]
                    }
                },
                "additionalProperties": False,
            },
            input_ports=(PortSpec("outcome", _OUTCOME),),
            output_ports=(PortSpec("transition", _STATE_TRANSITION),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.STATELESS,
            ),
            factory=lambda config: AdaptiveCounter(
                required_use=str(config.get("required_use", "adaptation"))
            ),
            implementation="eegle.runtime.builtins:AdaptiveCounter",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.actions.label_action",
            version="0.1.0",
            kind=ComponentKind.POLICY,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {
                    "capability": {"type": "string", "minLength": 1},
                    "matching_label": {"type": "string", "minLength": 1},
                    "output_key": {"type": "string", "minLength": 1},
                    "parameters": {"type": "object"},
                    "intended_delivery_delay_seconds": {
                        "type": ["number", "null"],
                        "minimum": 0,
                    },
                    "expires_after_seconds": {
                        "type": ["number", "null"],
                        "minimum": 0,
                    },
                },
                "required": ["capability", "matching_label"],
                "additionalProperties": False,
            },
            input_ports=(PortSpec("prediction", _PREDICTION),),
            output_ports=(PortSpec("request", _ACTION_REQUEST),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.STATELESS,
            ),
            factory=lambda config: LabelActionPolicy(**dict(config)),
            implementation="eegle.actions.policies:LabelActionPolicy",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.actions.structured_action",
            version="0.1.0",
            kind=ComponentKind.POLICY,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {
                    "capability": {"type": "string", "minLength": 1},
                    "output_parameters": {
                        "type": "object",
                        "minProperties": 1,
                        "additionalProperties": {"type": "string", "minLength": 1},
                    },
                    "constant_parameters": {"type": "object"},
                    "missing_output": {"enum": ["suppress", "fail"]},
                    "intended_delivery_delay_seconds": {
                        "type": ["number", "null"],
                        "minimum": 0,
                    },
                    "expires_after_seconds": {
                        "type": ["number", "null"],
                        "minimum": 0,
                    },
                },
                "required": ["capability", "output_parameters"],
                "additionalProperties": False,
            },
            input_ports=(PortSpec("prediction", _PREDICTION),),
            output_ports=(PortSpec("request", _ACTION_REQUEST),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.STATELESS,
            ),
            factory=lambda config: StructuredActionPolicy(**dict(config)),
            implementation="eegle.actions.policies:StructuredActionPolicy",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.authorization.simulation",
            version="0.1.0",
            kind=ComponentKind.AUTHORIZATION,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {
                    "decision": {
                        "type": "string",
                        "enum": [
                            "authorized",
                            "denied",
                            "observe_only",
                            "interlocked",
                            "failed",
                        ],
                    },
                    "decision_delay_seconds": {"type": "number", "minimum": 0},
                    "valid_for_seconds": {
                        "type": ["number", "null"],
                        "minimum": 0,
                    },
                },
                "additionalProperties": False,
            },
            input_ports=(),
            output_ports=(),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.STATELESS,
                simulation_only=True,
            ),
            factory=lambda config: SimulationAuthorizationProvider(**dict(config)),
            implementation="eegle.actions.providers:SimulationAuthorizationProvider",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.actions.simulated_actuator",
            version="0.1.0",
            kind=ComponentKind.ACTUATOR,
            config_schema=_empty_schema(),
            input_ports=(PortSpec("request", _ACTION_REQUEST),),
            output_ports=(PortSpec("receipt", _ACTION_RECEIPT),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.STATELESS,
                simulation_only=True,
            ),
            factory=lambda config: SimulatedActuator(),
            implementation="eegle.actions.simulated:SimulatedActuator",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.runtime.trigger_record_sink",
            version="0.1.0",
            kind=ComponentKind.SINK,
            config_schema=_empty_schema(),
            input_ports=(),
            output_ports=(),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.SNAPSHOT_RESTORE,
                supports_triggers=True,
            ),
            factory=lambda config: TriggerRecordSink(),
            implementation="eegle.runtime.builtins:TriggerRecordSink",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.artifacts.prediction_summary",
            version="0.1.0",
            kind=ComponentKind.ARTIFACT,
            config_schema={
                "$schema": _SCHEMA_BASE,
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 1},
                    "role": {"type": "string", "minLength": 1},
                    "sensitivity": {
                        "enum": ["public", "internal", "pseudonymized", "restricted"]
                    },
                },
                "required": ["artifact_id", "role"],
                "additionalProperties": False,
            },
            input_ports=(PortSpec("prediction", _PREDICTION),),
            output_ports=(PortSpec("artifact", _ARTIFACT_PUBLICATION),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.STATELESS,
            ),
            factory=lambda config: PredictionArtifactProducer(**dict(config)),
            implementation="eegle.recording.producers:PredictionArtifactProducer",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.recording.dense_sink",
            version="0.1.0",
            kind=ComponentKind.SINK,
            config_schema=_empty_schema(),
            input_ports=(PortSpec("records", _DENSE_PACKET, multiple=True),),
            output_ports=(),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.RECORD_ONLY,
            ),
            factory=lambda config: InMemoryRecordSink(),
            implementation="eegle.recording.sinks:InMemoryRecordSink",
            distribution="eegle",
        ),
        PluginDescriptor(
            plugin_id="eegle.recording.sparse_sink",
            version="0.1.0",
            kind=ComponentKind.SINK,
            config_schema=_empty_schema(),
            input_ports=(PortSpec("records", _SPARSE_PACKET, multiple=True),),
            output_ports=(),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.BITWISE,
                state_behavior=StateBehavior.RECORD_ONLY,
            ),
            factory=lambda config: InMemoryRecordSink(),
            implementation="eegle.recording.sinks:InMemoryRecordSink",
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


def _empty_schema() -> dict[str, Any]:
    return {
        "$schema": _SCHEMA_BASE,
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


def _packet_sequence_schema() -> dict[str, Any]:
    return {
        "$schema": _SCHEMA_BASE,
        "type": "object",
        "properties": {
            "stream_spec": {"type": "object"},
            "packets": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["stream_spec"],
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


def _packet_sequence_sparse_factory(config: Mapping[str, Any]) -> PacketSequenceSource:
    stream_spec = StreamSpec.from_payload(config["stream_spec"])
    if stream_spec.content_kind != ContentKind.SPARSE_EVENTS:
        raise ValueError("packet_sequence_sparse requires a sparse event stream")
    packets = tuple(SparseEventBatch.from_payload(value) for value in config.get("packets", ()))
    return PacketSequenceSource(stream_spec, packets)
