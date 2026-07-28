"""Bounded compositional experiment authoring and deterministic lowering.

The values in this module describe researcher intent.  They are immutable,
non-executable, and never consumed by the runtime.  One lowering service turns
named declarations into the existing canonical protocol and suite types.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from eegle._domain import ComponentKind, ExecutionMode
from eegle._validation import (
    freeze_json,
    require_digest,
    require_finite,
    require_identifier,
    thaw_json,
)
from eegle.authoring.contracts import (
    AuthoringOrigin,
    CanonicalArtifact,
    ConfirmationState,
    ScientificMateriality,
    SourceKind,
    SourceLocation,
)
from eegle.authoring.drafts import (
    DeploymentRequirement,
    DeploymentRequirementKind,
    DeploymentRequirements,
    DraftIssue,
    DraftLoweringError,
    ExperimentDraft,
    LoweredExperiment,
)
from eegle.authoring.provenance import (
    AuthoringProvenance,
    CanonicalTarget,
    DraftSourceMap,
    ProvenanceEntry,
)
from eegle.compiler import canonical_hash, canonical_json_bytes, compile_suite
from eegle.plugins import PluginRegistry
from eegle.specs import (
    AcceptanceCriterion,
    AdaptationSpec,
    ArtifactSpec,
    ClaimSpec,
    ComparisonOperator,
    ComponentSpec,
    DeploymentSpec,
    LogicalStreamSpec,
    MetricSpec,
    ModelUseSpec,
    OutcomeExpectationSpec,
    PhaseSpec,
    PhaseTransition,
    ProtocolSpec,
    ResumePolicy,
    RouteSpec,
    SchedulingSpec,
    SignalContract,
    SuiteSpec,
    TransitionCondition,
    validate_payload,
)
from eegle.specs.protocol import PROTOCOL_SPEC_SCHEMA
from eegle.specs.suite import SUITE_SPEC_SCHEMA

if TYPE_CHECKING:
    from eegle.compiler import CompilationResult
    from eegle.models import ModelManifest


EXPERIMENT_DESIGN_SCHEMA_ID = "eegle.experiment_design.v1"
COMPOSED_EXPERIMENT_SCHEMA_ID = "eegle.composed_experiment.v1"
COMPOSED_PROJECT_SCHEMA_ID = "eegle.composed_authoring_project.v1"
_JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


EXPERIMENT_DESIGN_JSON_SCHEMA: Mapping[str, Any] = freeze_json(
    {
        "$schema": _JSON_SCHEMA,
        "title": "EEGle bounded compositional experiment design v1",
        "type": "object",
        "required": [
            "schema",
            "experiment_id",
            "revision",
            "study",
            "signals",
            "events",
            "processing",
            "windows",
            "quality_gates",
            "models",
            "comparisons",
            "outcomes",
            "adaptations",
            "calibrations",
            "policies",
            "actions",
            "phases",
            "initial_phase",
            "recording",
            "acceptance",
        ],
        "properties": {
            "schema": {"const": EXPERIMENT_DESIGN_SCHEMA_ID},
            "experiment_id": {"type": "string", "pattern": _IDENTIFIER},
            "revision": {"type": "integer", "minimum": 1},
            "study": {"type": "object"},
            "signals": {"type": "array", "items": {"type": "object"}},
            "events": {"type": "array", "items": {"type": "object"}},
            "processing": {"type": "array", "items": {"type": "object"}},
            "windows": {"type": "array", "items": {"type": "object"}},
            "quality_gates": {"type": "array", "items": {"type": "object"}},
            "models": {"type": "array", "items": {"type": "object"}},
            "comparisons": {"type": "array", "items": {"type": "object"}},
            "outcomes": {"type": "array", "items": {"type": "object"}},
            "adaptations": {"type": "array", "items": {"type": "object"}},
            "calibrations": {"type": "array", "items": {"type": "object"}},
            "policies": {"type": "array", "items": {"type": "object"}},
            "actions": {"type": "array", "items": {"type": "object"}},
            "phases": {"type": "array", "items": {"type": "object"}},
            "initial_phase": {
                "type": ["string", "null"],
                "pattern": _IDENTIFIER,
            },
            "recording": {"type": "object"},
            "acceptance": {"type": "array", "items": {"type": "object"}},
        },
        "additionalProperties": False,
    }
)


@dataclass(frozen=True, slots=True)
class StudyIntent:
    statement: str
    execution_mode: ExecutionMode = ExecutionMode.CAUSAL
    execution_clock_id: str = "boundary.clock"
    protocol_id: str | None = None
    suite_id: str | None = None
    annotations: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.statement).strip():
            raise ValueError("study statement cannot be empty")
        object.__setattr__(self, "execution_mode", ExecutionMode(self.execution_mode))
        object.__setattr__(
            self,
            "execution_clock_id",
            require_identifier(self.execution_clock_id, "execution_clock_id"),
        )
        for name in ("protocol_id", "suite_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_identifier(value, name))
        object.__setattr__(self, "annotations", freeze_json(self.annotations or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "statement": self.statement,
            "execution_mode": self.execution_mode.value,
            "execution_clock_id": self.execution_clock_id,
            "protocol_id": self.protocol_id,
            "suite_id": self.suite_id,
            "annotations": thaw_json(self.annotations),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StudyIntent":
        _reject_unknown(
            payload,
            {
                "statement",
                "execution_mode",
                "execution_clock_id",
                "protocol_id",
                "suite_id",
                "annotations",
            },
            "study",
        )
        return cls(
            statement=str(payload["statement"]),
            execution_mode=ExecutionMode(str(payload.get("execution_mode", "causal"))),
            execution_clock_id=str(payload.get("execution_clock_id", "boundary.clock")),
            protocol_id=_optional_string(payload.get("protocol_id")),
            suite_id=_optional_string(payload.get("suite_id")),
            annotations=dict(payload.get("annotations") or {}),
        )


@dataclass(frozen=True, slots=True)
class SignalDeclaration:
    signal_id: str
    modality: str
    channels: tuple[str, ...]
    unit: str
    nominal_rate_hz: float | None
    clock_id: str = "device.clock"
    rate_model: str = "regular"
    channel_units: Mapping[str, str] = field(default_factory=dict)
    missing_data_policy: str = "explicit_validity"
    layout: str = "samples_by_channels"

    def __post_init__(self) -> None:
        for name in ("signal_id", "modality", "clock_id", "rate_model"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))
        channels = tuple(require_identifier(value, "channel") for value in self.channels)
        if not channels or len(channels) != len(set(channels)):
            raise ValueError("dense signal channels must be non-empty and unique")
        object.__setattr__(self, "channels", channels)
        if not self.unit.strip():
            raise ValueError("signal unit cannot be empty")
        if self.nominal_rate_hz is not None:
            rate = require_finite(self.nominal_rate_hz, "nominal_rate_hz")
            if rate <= 0:
                raise ValueError("nominal_rate_hz must be positive")
            object.__setattr__(self, "nominal_rate_hz", rate)
        if self.rate_model == "regular" and self.nominal_rate_hz is None:
            raise ValueError("regular signals require nominal_rate_hz")
        units = {require_identifier(str(k), "channel unit key"): str(v) for k, v in self.channel_units.items()}
        if set(units) - set(channels):
            raise ValueError("channel_units must reference declared channels")
        if any(not value.strip() for value in units.values()):
            raise ValueError("channel units cannot be empty")
        object.__setattr__(self, "channel_units", freeze_json(units))
        for name in ("missing_data_policy", "layout"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))

    @property
    def semantic_id(self) -> str:
        return f"signal.{self.signal_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "modality": self.modality,
            "channels": list(self.channels),
            "unit": self.unit,
            "nominal_rate_hz": self.nominal_rate_hz,
            "clock_id": self.clock_id,
            "rate_model": self.rate_model,
            "channel_units": thaw_json(self.channel_units),
            "missing_data_policy": self.missing_data_policy,
            "layout": self.layout,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SignalDeclaration":
        _reject_unknown(payload, _field_names(cls), "signal")
        return cls(
            signal_id=str(payload["signal_id"]),
            modality=str(payload["modality"]),
            channels=tuple(str(v) for v in payload["channels"]),
            unit=str(payload["unit"]),
            nominal_rate_hz=None if payload.get("nominal_rate_hz") is None else float(payload["nominal_rate_hz"]),
            clock_id=str(payload.get("clock_id", "device.clock")),
            rate_model=str(payload.get("rate_model", "regular")),
            channel_units={str(k): str(v) for k, v in dict(payload.get("channel_units") or {}).items()},
            missing_data_policy=str(payload.get("missing_data_policy", "explicit_validity")),
            layout=str(payload.get("layout", "samples_by_channels")),
        )


@dataclass(frozen=True, slots=True)
class EventDeclaration:
    event_id: str
    event_kinds: tuple[str, ...]
    modality: str = "markers"
    clock_id: str = "device.clock"

    def __post_init__(self) -> None:
        for name in ("event_id", "modality", "clock_id"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))
        kinds = tuple(require_identifier(value, "event kind") for value in self.event_kinds)
        if not kinds or len(kinds) != len(set(kinds)):
            raise ValueError("event kinds must be non-empty and unique")
        object.__setattr__(self, "event_kinds", kinds)

    @property
    def semantic_id(self) -> str:
        return f"event.{self.event_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_kinds": list(self.event_kinds),
            "modality": self.modality,
            "clock_id": self.clock_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EventDeclaration":
        _reject_unknown(payload, _field_names(cls), "event")
        return cls(
            event_id=str(payload["event_id"]),
            event_kinds=tuple(str(v) for v in payload["event_kinds"]),
            modality=str(payload.get("modality", "markers")),
            clock_id=str(payload.get("clock_id", "device.clock")),
        )


@dataclass(frozen=True, slots=True)
class ProcessingStep:
    step_id: str
    plugin_id: str
    config: Mapping[str, Any] = field(default_factory=dict)
    version_spec: str = "~=0.1.0"
    input_port: str = "samples"
    output_port: str = "samples"

    def __post_init__(self) -> None:
        for name in ("step_id", "plugin_id", "input_port", "output_port"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))
        if not self.version_spec.strip():
            raise ValueError("processing version_spec cannot be empty")
        object.__setattr__(self, "config", freeze_json(self.config or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "plugin_id": self.plugin_id,
            "version_spec": self.version_spec,
            "config": thaw_json(self.config),
            "input_port": self.input_port,
            "output_port": self.output_port,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProcessingStep":
        _reject_unknown(payload, _field_names(cls), "processing step")
        return cls(
            step_id=str(payload["step_id"]),
            plugin_id=str(payload["plugin_id"]),
            version_spec=str(payload.get("version_spec", "~=0.1.0")),
            config=dict(payload.get("config") or {}),
            input_port=str(payload.get("input_port", "samples")),
            output_port=str(payload.get("output_port", "samples")),
        )


@dataclass(frozen=True, slots=True)
class ProcessingChain:
    chain_id: str
    input: str
    steps: tuple[ProcessingStep, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "chain_id", require_identifier(self.chain_id, "chain_id"))
        object.__setattr__(self, "input", _semantic_reference(self.input, "processing input"))
        steps = tuple(self.steps)
        if not steps or not all(isinstance(value, ProcessingStep) for value in steps):
            raise ValueError("processing chain requires typed steps")
        _unique((value.step_id for value in steps), "processing step")
        object.__setattr__(self, "steps", steps)

    @property
    def semantic_id(self) -> str:
        return f"processing.{self.chain_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "input": self.input,
            "steps": [value.to_payload() for value in self.steps],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProcessingChain":
        _reject_unknown(payload, _field_names(cls), "processing chain")
        return cls(
            chain_id=str(payload["chain_id"]),
            input=str(payload["input"]),
            steps=tuple(ProcessingStep.from_payload(value) for value in payload["steps"]),
        )


class WindowKind(str, Enum):
    CONTINUOUS = "continuous"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class WindowDeclaration:
    window_id: str
    kind: WindowKind
    input: str
    duration_seconds: float | None = None
    step_seconds: float | None = None
    event_stream: str | None = None
    event_kind: str | None = None
    start_offset_seconds: float | None = None
    end_offset_seconds: float | None = None
    max_buffer_samples: int = 100_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "window_id", require_identifier(self.window_id, "window_id"))
        object.__setattr__(self, "kind", WindowKind(self.kind))
        object.__setattr__(self, "input", _semantic_reference(self.input, "window input"))
        if self.kind == WindowKind.CONTINUOUS:
            if self.duration_seconds is None or self.step_seconds is None:
                raise ValueError("continuous windows require duration_seconds and step_seconds")
            for name in ("duration_seconds", "step_seconds"):
                value = require_finite(getattr(self, name), name)
                if value <= 0:
                    raise ValueError(f"{name} must be positive")
                object.__setattr__(self, name, value)
            if any(value is not None for value in (self.event_stream, self.event_kind, self.start_offset_seconds, self.end_offset_seconds)):
                raise ValueError("continuous windows cannot declare event fields")
        else:
            if self.event_stream is None or self.event_kind is None:
                raise ValueError("event windows require event_stream and event_kind")
            object.__setattr__(self, "event_stream", _semantic_reference(self.event_stream, "event_stream"))
            object.__setattr__(self, "event_kind", require_identifier(self.event_kind, "event_kind"))
            if self.start_offset_seconds is None or self.end_offset_seconds is None:
                raise ValueError("event windows require start and end offsets")
            start = require_finite(self.start_offset_seconds, "start_offset_seconds")
            end = require_finite(self.end_offset_seconds, "end_offset_seconds")
            if end <= start:
                raise ValueError("event window end must follow its start")
            object.__setattr__(self, "start_offset_seconds", start)
            object.__setattr__(self, "end_offset_seconds", end)
            if self.duration_seconds is not None or self.step_seconds is not None:
                raise ValueError("event windows cannot declare continuous duration or step")
        object.__setattr__(self, "max_buffer_samples", int(self.max_buffer_samples))
        if self.max_buffer_samples <= 0:
            raise ValueError("max_buffer_samples must be positive")

    @property
    def semantic_id(self) -> str:
        return f"window.{self.window_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "window_id": self.window_id,
                "kind": self.kind.value,
                "input": self.input,
                "duration_seconds": self.duration_seconds,
                "step_seconds": self.step_seconds,
                "event_stream": self.event_stream,
                "event_kind": self.event_kind,
                "start_offset_seconds": self.start_offset_seconds,
                "end_offset_seconds": self.end_offset_seconds,
                "max_buffer_samples": self.max_buffer_samples,
            }.items()
            if value is not None
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "WindowDeclaration":
        _reject_unknown(payload, _field_names(cls), "window")
        return cls(
            window_id=str(payload["window_id"]),
            kind=WindowKind(str(payload["kind"])),
            input=str(payload["input"]),
            duration_seconds=_optional_float(payload.get("duration_seconds")),
            step_seconds=_optional_float(payload.get("step_seconds")),
            event_stream=_optional_string(payload.get("event_stream")),
            event_kind=_optional_string(payload.get("event_kind")),
            start_offset_seconds=_optional_float(payload.get("start_offset_seconds")),
            end_offset_seconds=_optional_float(payload.get("end_offset_seconds")),
            max_buffer_samples=int(payload.get("max_buffer_samples", 100_000)),
        )


@dataclass(frozen=True, slots=True)
class QualityGateDeclaration:
    gate_id: str
    input: str
    plugin_id: str = "eegle.processing.finite_quality"
    version_spec: str = "~=0.1.0"
    config: Mapping[str, Any] = field(default_factory=dict)
    input_port: str = "item"
    output_port: str = "decision"

    def __post_init__(self) -> None:
        for name in ("gate_id", "plugin_id", "input_port", "output_port"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))
        object.__setattr__(self, "input", _semantic_reference(self.input, "quality input"))
        if not self.version_spec.strip():
            raise ValueError("quality version_spec cannot be empty")
        object.__setattr__(self, "config", freeze_json(self.config or {}))

    @property
    def semantic_id(self) -> str:
        return f"quality.{self.gate_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "input": self.input,
            "plugin_id": self.plugin_id,
            "version_spec": self.version_spec,
            "config": thaw_json(self.config),
            "input_port": self.input_port,
            "output_port": self.output_port,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "QualityGateDeclaration":
        _reject_unknown(payload, _field_names(cls), "quality gate")
        return cls(
            gate_id=str(payload["gate_id"]),
            input=str(payload["input"]),
            plugin_id=str(payload.get("plugin_id", "eegle.processing.finite_quality")),
            version_spec=str(payload.get("version_spec", "~=0.1.0")),
            config=dict(payload.get("config") or {}),
            input_port=str(payload.get("input_port", "item")),
            output_port=str(payload.get("output_port", "decision")),
        )


@dataclass(frozen=True, slots=True)
class ModelDeclaration:
    model_id: str
    plugin_id: str
    manifest_digest: str
    role: str
    inputs: Mapping[str, str]
    version_spec: str = "~=0.1.0"
    config: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("model_id", "plugin_id", "role"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))
        object.__setattr__(self, "manifest_digest", require_digest(self.manifest_digest, "manifest_digest"))
        if not self.version_spec.strip():
            raise ValueError("model version_spec cannot be empty")
        inputs = {
            require_identifier(str(port), "model input port"): _semantic_reference(ref, "model input")
            for port, ref in self.inputs.items()
        }
        if not inputs:
            raise ValueError("model declaration requires at least one input")
        object.__setattr__(self, "inputs", freeze_json(inputs))
        object.__setattr__(self, "config", freeze_json(self.config or {}))

    @property
    def semantic_id(self) -> str:
        return f"model.{self.model_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "plugin_id": self.plugin_id,
            "manifest_digest": self.manifest_digest,
            "role": self.role,
            "inputs": thaw_json(self.inputs),
            "version_spec": self.version_spec,
            "config": thaw_json(self.config),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelDeclaration":
        _reject_unknown(payload, _field_names(cls), "model")
        return cls(
            model_id=str(payload["model_id"]),
            plugin_id=str(payload["plugin_id"]),
            manifest_digest=str(payload["manifest_digest"]),
            role=str(payload["role"]),
            inputs={str(k): str(v) for k, v in dict(payload["inputs"]).items()},
            version_spec=str(payload.get("version_spec", "~=0.1.0")),
            config=dict(payload.get("config") or {}),
        )


@dataclass(frozen=True, slots=True)
class ComparisonGroup:
    comparison_id: str
    members: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "comparison_id", require_identifier(self.comparison_id, "comparison_id"))
        members = tuple(_model_reference(value) for value in self.members)
        if len(members) < 2 or len(members) != len(set(members)):
            raise ValueError("comparison groups require at least two unique models")
        object.__setattr__(self, "members", members)

    @property
    def semantic_id(self) -> str:
        return f"comparison.{self.comparison_id}"

    def to_payload(self) -> dict[str, Any]:
        return {"comparison_id": self.comparison_id, "members": list(self.members)}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ComparisonGroup":
        _reject_unknown(payload, _field_names(cls), "comparison")
        return cls(str(payload["comparison_id"]), tuple(str(v) for v in payload["members"]))


@dataclass(frozen=True, slots=True)
class OutcomeDeclaration:
    outcome_id: str
    event_stream: str
    event_kind: str
    models: tuple[str, ...]
    permitted_uses: tuple[str, ...] = ("metrics",)
    plugin_id: str = "eegle.outcomes.sparse_event"
    version_spec: str = "~=0.1.0"
    max_pending_predictions: int = 128
    prediction_ttl_seconds: float = 300.0

    def __post_init__(self) -> None:
        for name in ("outcome_id", "event_kind", "plugin_id"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))
        object.__setattr__(self, "event_stream", _event_reference(self.event_stream))
        models = tuple(_model_reference(value) for value in self.models)
        if not models or len(models) != len(set(models)):
            raise ValueError("outcomes require unique target models")
        object.__setattr__(self, "models", models)
        uses = tuple(require_identifier(value, "permitted outcome use") for value in self.permitted_uses)
        if not uses or len(uses) != len(set(uses)):
            raise ValueError("outcome permitted uses must be non-empty and unique")
        object.__setattr__(self, "permitted_uses", uses)
        if not self.version_spec.strip():
            raise ValueError("outcome version_spec cannot be empty")
        object.__setattr__(self, "max_pending_predictions", int(self.max_pending_predictions))
        if self.max_pending_predictions <= 0:
            raise ValueError("max_pending_predictions must be positive")
        ttl = require_finite(self.prediction_ttl_seconds, "prediction_ttl_seconds")
        if ttl < 0:
            raise ValueError("prediction_ttl_seconds cannot be negative")
        object.__setattr__(self, "prediction_ttl_seconds", ttl)

    @property
    def semantic_id(self) -> str:
        return f"outcome.{self.outcome_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "event_stream": self.event_stream,
            "event_kind": self.event_kind,
            "models": list(self.models),
            "permitted_uses": list(self.permitted_uses),
            "plugin_id": self.plugin_id,
            "version_spec": self.version_spec,
            "max_pending_predictions": self.max_pending_predictions,
            "prediction_ttl_seconds": self.prediction_ttl_seconds,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "OutcomeDeclaration":
        _reject_unknown(payload, _field_names(cls), "outcome")
        return cls(
            outcome_id=str(payload["outcome_id"]),
            event_stream=str(payload["event_stream"]),
            event_kind=str(payload["event_kind"]),
            models=tuple(str(v) for v in payload["models"]),
            permitted_uses=tuple(str(v) for v in payload.get("permitted_uses", ("metrics",))),
            plugin_id=str(payload.get("plugin_id", "eegle.outcomes.sparse_event")),
            version_spec=str(payload.get("version_spec", "~=0.1.0")),
            max_pending_predictions=int(payload.get("max_pending_predictions", 128)),
            prediction_ttl_seconds=float(payload.get("prediction_ttl_seconds", 300.0)),
        )


@dataclass(frozen=True, slots=True)
class AdaptationDeclaration:
    adaptation_id: str
    model: str
    outcome: str
    enabled_phases: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "adaptation_id", require_identifier(self.adaptation_id, "adaptation_id"))
        object.__setattr__(self, "model", _model_reference(self.model))
        object.__setattr__(self, "outcome", _prefixed_reference(self.outcome, "outcome", "outcome"))
        phases = tuple(_prefixed_reference(value, "phase", "phase") for value in self.enabled_phases)
        if not phases or len(phases) != len(set(phases)):
            raise ValueError("adaptation enabled phases must be non-empty and unique")
        object.__setattr__(self, "enabled_phases", phases)

    @property
    def semantic_id(self) -> str:
        return f"adaptation.{self.adaptation_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "adaptation_id": self.adaptation_id,
            "model": self.model,
            "outcome": self.outcome,
            "enabled_phases": list(self.enabled_phases),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AdaptationDeclaration":
        _reject_unknown(payload, _field_names(cls), "adaptation")
        return cls(
            adaptation_id=str(payload["adaptation_id"]),
            model=str(payload["model"]),
            outcome=str(payload["outcome"]),
            enabled_phases=tuple(str(v) for v in payload["enabled_phases"]),
        )


@dataclass(frozen=True, slots=True)
class CalibrationDeclaration:
    calibration_id: str
    producer: str
    producer_port: str
    producer_phase: str
    required_phases: tuple[str, ...]
    media_type: str = "application/json"
    role: str = "calibration_state"

    def __post_init__(self) -> None:
        for name in ("calibration_id", "producer_port", "role"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))
        object.__setattr__(self, "producer", _semantic_reference(self.producer, "calibration producer"))
        object.__setattr__(self, "producer_phase", _prefixed_reference(self.producer_phase, "phase", "phase"))
        phases = tuple(_prefixed_reference(value, "phase", "phase") for value in self.required_phases)
        if not phases or len(phases) != len(set(phases)):
            raise ValueError("calibration required phases must be non-empty and unique")
        object.__setattr__(self, "required_phases", phases)
        if not self.media_type.strip():
            raise ValueError("calibration media_type cannot be empty")

    @property
    def semantic_id(self) -> str:
        return f"calibration.{self.calibration_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "calibration_id": self.calibration_id,
            "producer": self.producer,
            "producer_port": self.producer_port,
            "producer_phase": self.producer_phase,
            "required_phases": list(self.required_phases),
            "media_type": self.media_type,
            "role": self.role,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CalibrationDeclaration":
        _reject_unknown(payload, _field_names(cls), "calibration")
        return cls(
            calibration_id=str(payload["calibration_id"]),
            producer=str(payload["producer"]),
            producer_port=str(payload["producer_port"]),
            producer_phase=str(payload["producer_phase"]),
            required_phases=tuple(str(v) for v in payload["required_phases"]),
            media_type=str(payload.get("media_type", "application/json")),
            role=str(payload.get("role", "calibration_state")),
        )


@dataclass(frozen=True, slots=True)
class PolicyDeclaration:
    policy_id: str
    plugin_id: str
    inputs: Mapping[str, str]
    config: Mapping[str, Any] = field(default_factory=dict)
    version_spec: str = "~=0.1.0"

    def __post_init__(self) -> None:
        for name in ("policy_id", "plugin_id"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))
        if not self.version_spec.strip():
            raise ValueError("policy version_spec cannot be empty")
        inputs = {
            require_identifier(str(port), "policy input port"): _semantic_reference(ref, "policy input")
            for port, ref in self.inputs.items()
        }
        if not inputs:
            raise ValueError("policy declaration requires at least one input")
        object.__setattr__(self, "inputs", freeze_json(inputs))
        object.__setattr__(self, "config", freeze_json(self.config or {}))

    @property
    def semantic_id(self) -> str:
        return f"policy.{self.policy_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "plugin_id": self.plugin_id,
            "inputs": thaw_json(self.inputs),
            "config": thaw_json(self.config),
            "version_spec": self.version_spec,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PolicyDeclaration":
        _reject_unknown(payload, _field_names(cls), "policy")
        return cls(
            policy_id=str(payload["policy_id"]),
            plugin_id=str(payload["plugin_id"]),
            inputs={str(k): str(v) for k, v in dict(payload["inputs"]).items()},
            config=dict(payload.get("config") or {}),
            version_spec=str(payload.get("version_spec", "~=0.1.0")),
        )


@dataclass(frozen=True, slots=True)
class ActionDeclaration:
    action_id: str
    capability: str
    policy: str
    plugin_id: str
    version_spec: str = "~=0.1.0"
    config: Mapping[str, Any] = field(default_factory=dict)
    policy_output_port: str = "request"
    actuator_input_port: str = "request"

    def __post_init__(self) -> None:
        for name in ("action_id", "capability", "plugin_id", "policy_output_port", "actuator_input_port"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))
        object.__setattr__(self, "policy", _prefixed_reference(self.policy, "policy", "policy"))
        if not self.version_spec.strip():
            raise ValueError("action version_spec cannot be empty")
        object.__setattr__(self, "config", freeze_json(self.config or {}))

    @property
    def semantic_id(self) -> str:
        return f"action.{self.action_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "capability": self.capability,
            "policy": self.policy,
            "plugin_id": self.plugin_id,
            "version_spec": self.version_spec,
            "config": thaw_json(self.config),
            "policy_output_port": self.policy_output_port,
            "actuator_input_port": self.actuator_input_port,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ActionDeclaration":
        _reject_unknown(payload, _field_names(cls), "action")
        return cls(
            action_id=str(payload["action_id"]),
            capability=str(payload["capability"]),
            policy=str(payload["policy"]),
            plugin_id=str(payload["plugin_id"]),
            version_spec=str(payload.get("version_spec", "~=0.1.0")),
            config=dict(payload.get("config") or {}),
            policy_output_port=str(payload.get("policy_output_port", "request")),
            actuator_input_port=str(payload.get("actuator_input_port", "request")),
        )


@dataclass(frozen=True, slots=True)
class PhaseDeclaration:
    phase_id: str
    active: tuple[str, ...]
    next_phases: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    retry_limit: int = 0
    resume_policy: ResumePolicy = ResumePolicy.CHECKPOINT
    operator_confirmation: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase_id", require_identifier(self.phase_id, "phase_id"))
        active = tuple(_semantic_reference(value, "phase active reference") for value in self.active)
        if not active or len(active) != len(set(active)):
            raise ValueError("phase active references must be non-empty and unique")
        object.__setattr__(self, "active", active)
        next_phases = tuple(_prefixed_reference(value, "phase", "phase") for value in self.next_phases)
        if len(next_phases) != len(set(next_phases)):
            raise ValueError("phase next references must be unique")
        object.__setattr__(self, "next_phases", next_phases)
        if self.timeout_seconds is not None:
            timeout = require_finite(self.timeout_seconds, "timeout_seconds")
            if timeout <= 0:
                raise ValueError("timeout_seconds must be positive")
            object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "retry_limit", int(self.retry_limit))
        if self.retry_limit < 0:
            raise ValueError("retry_limit cannot be negative")
        object.__setattr__(self, "resume_policy", ResumePolicy(self.resume_policy))

    @property
    def semantic_id(self) -> str:
        return f"phase.{self.phase_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "active": list(self.active),
            "next_phases": list(self.next_phases),
            "timeout_seconds": self.timeout_seconds,
            "retry_limit": self.retry_limit,
            "resume_policy": self.resume_policy.value,
            "operator_confirmation": self.operator_confirmation,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PhaseDeclaration":
        _reject_unknown(payload, _field_names(cls), "phase")
        return cls(
            phase_id=str(payload["phase_id"]),
            active=tuple(str(v) for v in payload["active"]),
            next_phases=tuple(str(v) for v in payload.get("next_phases", ())),
            timeout_seconds=_optional_float(payload.get("timeout_seconds")),
            retry_limit=int(payload.get("retry_limit", 0)),
            resume_policy=ResumePolicy(str(payload.get("resume_policy", "checkpoint"))),
            operator_confirmation=bool(payload.get("operator_confirmation", False)),
        )


@dataclass(frozen=True, slots=True)
class RecordingDeclaration:
    inputs: tuple[str, ...] = ()
    execution_capture: bool = True
    semantic_evidence: bool = True
    raw_recording: str = "reference"

    def __post_init__(self) -> None:
        inputs = tuple(_semantic_reference(value, "recording input") for value in self.inputs)
        if len(inputs) != len(set(inputs)):
            raise ValueError("recording inputs must be unique")
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "raw_recording", require_identifier(self.raw_recording, "raw_recording"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "inputs": list(self.inputs),
            "execution_capture": self.execution_capture,
            "semantic_evidence": self.semantic_evidence,
            "raw_recording": self.raw_recording,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RecordingDeclaration":
        _reject_unknown(payload, _field_names(cls), "recording")
        return cls(
            inputs=tuple(str(v) for v in payload.get("inputs", ())),
            execution_capture=bool(payload.get("execution_capture", True)),
            semantic_evidence=bool(payload.get("semantic_evidence", True)),
            raw_recording=str(payload.get("raw_recording", "reference")),
        )


@dataclass(frozen=True, slots=True)
class AcceptanceDeclaration:
    criterion_id: str
    metric_id: str
    measure: str
    operator: ComparisonOperator
    value: float | int | bool | str
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("criterion_id", "metric_id", "measure"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))
        object.__setattr__(self, "operator", ComparisonOperator(self.operator))
        freeze_json(self.value)
        object.__setattr__(self, "parameters", freeze_json(self.parameters or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "metric_id": self.metric_id,
            "measure": self.measure,
            "operator": self.operator.value,
            "value": self.value,
            "parameters": thaw_json(self.parameters),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AcceptanceDeclaration":
        _reject_unknown(payload, _field_names(cls), "acceptance")
        return cls(
            criterion_id=str(payload["criterion_id"]),
            metric_id=str(payload["metric_id"]),
            measure=str(payload["measure"]),
            operator=ComparisonOperator(str(payload["operator"])),
            value=payload["value"],
            parameters=dict(payload.get("parameters") or {}),
        )


@dataclass(frozen=True, slots=True)
class ExperimentDesign:
    """Immutable named scientific design above the canonical graph boundary."""

    experiment_id: str
    study: StudyIntent
    revision: int = 1
    signals: tuple[SignalDeclaration, ...] = ()
    events: tuple[EventDeclaration, ...] = ()
    processing: tuple[ProcessingChain, ...] = ()
    windows: tuple[WindowDeclaration, ...] = ()
    quality_gates: tuple[QualityGateDeclaration, ...] = ()
    models: tuple[ModelDeclaration, ...] = ()
    comparisons: tuple[ComparisonGroup, ...] = ()
    outcomes: tuple[OutcomeDeclaration, ...] = ()
    adaptations: tuple[AdaptationDeclaration, ...] = ()
    calibrations: tuple[CalibrationDeclaration, ...] = ()
    policies: tuple[PolicyDeclaration, ...] = ()
    actions: tuple[ActionDeclaration, ...] = ()
    phases: tuple[PhaseDeclaration, ...] = ()
    initial_phase: str | None = None
    recording: RecordingDeclaration = RecordingDeclaration()
    acceptance: tuple[AcceptanceDeclaration, ...] = ()
    sources: Mapping[str, SourceLocation] = field(default_factory=dict, repr=False, compare=False)
    schema: str = EXPERIMENT_DESIGN_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != EXPERIMENT_DESIGN_SCHEMA_ID:
            raise ValueError(f"unsupported experiment design schema: {self.schema}")
        object.__setattr__(self, "experiment_id", require_identifier(self.experiment_id, "experiment_id"))
        if not isinstance(self.study, StudyIntent):
            raise TypeError("study must be a StudyIntent")
        object.__setattr__(self, "revision", int(self.revision))
        if self.revision <= 0:
            raise ValueError("design revision must be positive")
        typed_collections = (
            ("signals", SignalDeclaration, "signal_id"),
            ("events", EventDeclaration, "event_id"),
            ("processing", ProcessingChain, "chain_id"),
            ("windows", WindowDeclaration, "window_id"),
            ("quality_gates", QualityGateDeclaration, "gate_id"),
            ("models", ModelDeclaration, "model_id"),
            ("comparisons", ComparisonGroup, "comparison_id"),
            ("outcomes", OutcomeDeclaration, "outcome_id"),
            ("adaptations", AdaptationDeclaration, "adaptation_id"),
            ("calibrations", CalibrationDeclaration, "calibration_id"),
            ("policies", PolicyDeclaration, "policy_id"),
            ("actions", ActionDeclaration, "action_id"),
            ("phases", PhaseDeclaration, "phase_id"),
            ("acceptance", AcceptanceDeclaration, "criterion_id"),
        )
        for name, expected, identity in typed_collections:
            values = tuple(getattr(self, name))
            if not all(isinstance(value, expected) for value in values):
                raise TypeError(f"{name} must contain {expected.__name__} values")
            values = tuple(sorted(values, key=lambda value: getattr(value, identity)))
            _unique((getattr(value, identity) for value in values), name)
            object.__setattr__(self, name, values)
        source_ids = {value.signal_id for value in self.signals}
        event_ids = {value.event_id for value in self.events}
        if source_ids & event_ids:
            overlap = ", ".join(sorted(source_ids & event_ids))
            raise ValueError(
                "dense signals and event streams cannot share generated stream IDs: "
                + overlap
            )
        if not isinstance(self.recording, RecordingDeclaration):
            raise TypeError("recording must be a RecordingDeclaration")
        if self.initial_phase is not None:
            object.__setattr__(
                self,
                "initial_phase",
                _prefixed_reference(self.initial_phase, "phase", "initial_phase"),
            )
        locations = {str(key): value for key, value in self.sources.items()}
        if not all(isinstance(value, SourceLocation) for value in locations.values()):
            raise TypeError("design sources must be SourceLocation values")
        object.__setattr__(self, "sources", MappingProxyType(locations))
        validate_payload(self.to_payload(), EXPERIMENT_DESIGN_JSON_SCHEMA)

    @classmethod
    def create(
        cls,
        experiment_id: str,
        statement: str,
        *,
        execution_mode: ExecutionMode = ExecutionMode.CAUSAL,
        execution_clock_id: str = "boundary.clock",
        annotations: Mapping[str, Any] | None = None,
    ) -> "ExperimentDesign":
        source = SourceLocation(SourceKind.PYTHON, symbol="ExperimentDesign.create")
        return cls(
            experiment_id=experiment_id,
            study=StudyIntent(
                statement,
                execution_mode,
                execution_clock_id,
                annotations=annotations or {},
            ),
            sources={"study": source},
        )

    @property
    def design_digest(self) -> str:
        return canonical_hash(self.to_payload())

    def dense_signal(
        self,
        signal_id: str,
        *,
        modality: str,
        channels: Sequence[str],
        unit: str,
        rate_hz: float | None,
        clock_id: str = "device.clock",
        rate_model: str = "regular",
        channel_units: Mapping[str, str] | None = None,
        missing_data_policy: str = "explicit_validity",
        layout: str = "samples_by_channels",
    ) -> "ExperimentDesign":
        value = SignalDeclaration(
            signal_id,
            modality,
            tuple(channels),
            unit,
            rate_hz,
            clock_id,
            rate_model,
            channel_units or {},
            missing_data_policy,
            layout,
        )
        return self._append("signals", value, value.semantic_id, "ExperimentDesign.dense_signal")

    def event_stream(
        self,
        event_id: str,
        *,
        kinds: Sequence[str],
        modality: str = "markers",
        clock_id: str = "device.clock",
    ) -> "ExperimentDesign":
        value = EventDeclaration(event_id, tuple(kinds), modality, clock_id)
        return self._append("events", value, value.semantic_id, "ExperimentDesign.event_stream")

    def processing_chain(
        self,
        chain_id: str,
        *,
        input: str,
        steps: Sequence[ProcessingStep],
    ) -> "ExperimentDesign":
        value = ProcessingChain(chain_id, input, tuple(steps))
        return self._append("processing", value, value.semantic_id, "ExperimentDesign.processing_chain")

    def continuous_window(
        self,
        window_id: str,
        *,
        input: str,
        duration_seconds: float,
        step_seconds: float,
        max_buffer_samples: int = 100_000,
    ) -> "ExperimentDesign":
        value = WindowDeclaration(
            window_id,
            WindowKind.CONTINUOUS,
            input,
            duration_seconds=duration_seconds,
            step_seconds=step_seconds,
            max_buffer_samples=max_buffer_samples,
        )
        return self._append("windows", value, value.semantic_id, "ExperimentDesign.continuous_window")

    def event_window(
        self,
        window_id: str,
        *,
        input: str,
        event_stream: str,
        event_kind: str,
        start_seconds: float,
        end_seconds: float,
        max_buffer_samples: int = 100_000,
    ) -> "ExperimentDesign":
        value = WindowDeclaration(
            window_id,
            WindowKind.EVENT,
            input,
            event_stream=event_stream,
            event_kind=event_kind,
            start_offset_seconds=start_seconds,
            end_offset_seconds=end_seconds,
            max_buffer_samples=max_buffer_samples,
        )
        return self._append("windows", value, value.semantic_id, "ExperimentDesign.event_window")

    def quality_gate(
        self,
        gate_id: str,
        *,
        input: str,
        plugin_id: str = "eegle.processing.finite_quality",
        version_spec: str = "~=0.1.0",
        config: Mapping[str, Any] | None = None,
    ) -> "ExperimentDesign":
        value = QualityGateDeclaration(gate_id, input, plugin_id, version_spec, config or {})
        return self._append("quality_gates", value, value.semantic_id, "ExperimentDesign.quality_gate")

    def model(
        self,
        model_id: str,
        *,
        plugin_id: str,
        manifest_digest: str,
        role: str,
        inputs: Mapping[str, str],
        version_spec: str = "~=0.1.0",
        config: Mapping[str, Any] | None = None,
    ) -> "ExperimentDesign":
        value = ModelDeclaration(model_id, plugin_id, manifest_digest, role, inputs, version_spec, config or {})
        return self._append("models", value, value.semantic_id, "ExperimentDesign.model")

    def comparison_group(self, comparison_id: str, *, members: Sequence[str]) -> "ExperimentDesign":
        value = ComparisonGroup(comparison_id, tuple(members))
        return self._append("comparisons", value, value.semantic_id, "ExperimentDesign.comparison_group")

    def outcome(
        self,
        outcome_id: str,
        *,
        event_stream: str,
        event_kind: str,
        models: Sequence[str],
        permitted_uses: Sequence[str] = ("metrics",),
        max_pending_predictions: int = 128,
        prediction_ttl_seconds: float = 300.0,
    ) -> "ExperimentDesign":
        value = OutcomeDeclaration(
            outcome_id,
            event_stream,
            event_kind,
            tuple(models),
            tuple(permitted_uses),
            max_pending_predictions=max_pending_predictions,
            prediction_ttl_seconds=prediction_ttl_seconds,
        )
        return self._append("outcomes", value, value.semantic_id, "ExperimentDesign.outcome")

    def adaptation(
        self,
        adaptation_id: str,
        *,
        model: str,
        outcome: str,
        enabled_phases: Sequence[str],
    ) -> "ExperimentDesign":
        value = AdaptationDeclaration(adaptation_id, model, outcome, tuple(enabled_phases))
        return self._append("adaptations", value, value.semantic_id, "ExperimentDesign.adaptation")

    def calibration(
        self,
        calibration_id: str,
        *,
        producer: str,
        producer_port: str,
        producer_phase: str,
        required_phases: Sequence[str],
        media_type: str = "application/json",
        role: str = "calibration_state",
    ) -> "ExperimentDesign":
        value = CalibrationDeclaration(
            calibration_id,
            producer,
            producer_port,
            producer_phase,
            tuple(required_phases),
            media_type,
            role,
        )
        return self._append("calibrations", value, value.semantic_id, "ExperimentDesign.calibration")

    def policy(
        self,
        policy_id: str,
        *,
        plugin_id: str,
        inputs: Mapping[str, str],
        config: Mapping[str, Any] | None = None,
        version_spec: str = "~=0.1.0",
    ) -> "ExperimentDesign":
        value = PolicyDeclaration(policy_id, plugin_id, inputs, config or {}, version_spec)
        return self._append("policies", value, value.semantic_id, "ExperimentDesign.policy")

    def action(
        self,
        action_id: str,
        *,
        capability: str,
        policy: str,
        plugin_id: str,
        version_spec: str = "~=0.1.0",
        config: Mapping[str, Any] | None = None,
    ) -> "ExperimentDesign":
        value = ActionDeclaration(action_id, capability, policy, plugin_id, version_spec, config or {})
        return self._append("actions", value, value.semantic_id, "ExperimentDesign.action")

    def phase(
        self,
        phase_id: str,
        *,
        active: Sequence[str],
        next_phases: Sequence[str] = (),
        timeout_seconds: float | None = None,
        retry_limit: int = 0,
        resume_policy: ResumePolicy = ResumePolicy.CHECKPOINT,
        operator_confirmation: bool = False,
        initial: bool = False,
    ) -> "ExperimentDesign":
        value = PhaseDeclaration(
            phase_id,
            tuple(active),
            tuple(next_phases),
            timeout_seconds,
            retry_limit,
            resume_policy,
            operator_confirmation,
        )
        result = self._append("phases", value, value.semantic_id, "ExperimentDesign.phase")
        if initial:
            if self.initial_phase is not None:
                raise ValueError("an initial phase is already declared")
            result = replace(result, initial_phase=value.semantic_id)
        return result

    def with_initial_phase(self, phase_id: str) -> "ExperimentDesign":
        """Select the explicit entry phase for a multi-phase design."""

        return replace(
            self,
            initial_phase=_prefixed_reference(phase_id, "phase", "initial_phase"),
        )

    def record(
        self,
        *inputs: str,
        execution_capture: bool = True,
        semantic_evidence: bool = True,
        raw_recording: str = "reference",
    ) -> "ExperimentDesign":
        recording = RecordingDeclaration(tuple(inputs), execution_capture, semantic_evidence, raw_recording)
        sources = dict(self.sources)
        sources["recording"] = SourceLocation(SourceKind.PYTHON, symbol="ExperimentDesign.record")
        return replace(self, recording=recording, sources=sources)

    def accept(
        self,
        criterion_id: str,
        *,
        metric_id: str,
        measure: str,
        operator: ComparisonOperator,
        value: float | int | bool | str,
        parameters: Mapping[str, Any] | None = None,
    ) -> "ExperimentDesign":
        acceptance = AcceptanceDeclaration(criterion_id, metric_id, measure, operator, value, parameters or {})
        return self._append("acceptance", acceptance, f"acceptance.{criterion_id}", "ExperimentDesign.accept")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "experiment_id": self.experiment_id,
            "revision": self.revision,
            "study": self.study.to_payload(),
            "signals": [value.to_payload() for value in self.signals],
            "events": [value.to_payload() for value in self.events],
            "processing": [value.to_payload() for value in self.processing],
            "windows": [value.to_payload() for value in self.windows],
            "quality_gates": [value.to_payload() for value in self.quality_gates],
            "models": [value.to_payload() for value in self.models],
            "comparisons": [value.to_payload() for value in self.comparisons],
            "outcomes": [value.to_payload() for value in self.outcomes],
            "adaptations": [value.to_payload() for value in self.adaptations],
            "calibrations": [value.to_payload() for value in self.calibrations],
            "policies": [value.to_payload() for value in self.policies],
            "actions": [value.to_payload() for value in self.actions],
            "phases": [value.to_payload() for value in self.phases],
            "initial_phase": self.initial_phase,
            "recording": self.recording.to_payload(),
            "acceptance": [value.to_payload() for value in self.acceptance],
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        source_map: DraftSourceMap | None = None,
    ) -> "ExperimentDesign":
        validate_payload(payload, EXPERIMENT_DESIGN_JSON_SCHEMA)
        sources = _semantic_sources(payload, source_map)
        return cls(
            schema=str(payload["schema"]),
            experiment_id=str(payload["experiment_id"]),
            revision=int(payload["revision"]),
            study=StudyIntent.from_payload(payload["study"]),
            signals=tuple(SignalDeclaration.from_payload(value) for value in payload["signals"]),
            events=tuple(EventDeclaration.from_payload(value) for value in payload["events"]),
            processing=tuple(ProcessingChain.from_payload(value) for value in payload["processing"]),
            windows=tuple(WindowDeclaration.from_payload(value) for value in payload["windows"]),
            quality_gates=tuple(QualityGateDeclaration.from_payload(value) for value in payload["quality_gates"]),
            models=tuple(ModelDeclaration.from_payload(value) for value in payload["models"]),
            comparisons=tuple(ComparisonGroup.from_payload(value) for value in payload["comparisons"]),
            outcomes=tuple(OutcomeDeclaration.from_payload(value) for value in payload["outcomes"]),
            adaptations=tuple(AdaptationDeclaration.from_payload(value) for value in payload["adaptations"]),
            calibrations=tuple(CalibrationDeclaration.from_payload(value) for value in payload["calibrations"]),
            policies=tuple(PolicyDeclaration.from_payload(value) for value in payload["policies"]),
            actions=tuple(ActionDeclaration.from_payload(value) for value in payload["actions"]),
            phases=tuple(PhaseDeclaration.from_payload(value) for value in payload["phases"]),
            initial_phase=_optional_string(payload.get("initial_phase")),
            recording=RecordingDeclaration.from_payload(payload["recording"]),
            acceptance=tuple(AcceptanceDeclaration.from_payload(value) for value in payload["acceptance"]),
            sources=sources,
        )

    def canonical_json(self) -> str:
        return canonical_json_bytes(self.to_payload()).decode("utf-8")

    def to_draft(self) -> ExperimentDraft:
        payload = self.to_payload()
        intent = {key: value for key, value in payload.items() if key not in {"schema", "experiment_id", "revision"}}
        return ExperimentDraft(
            draft_id=self.experiment_id,
            revision=self.revision,
            intent=intent,
            design_schema=self.schema,
        )

    def draft_source_map(self) -> DraftSourceMap:
        locations: dict[str, SourceLocation] = {
            "/intent/study": self.sources.get("study", _generated_source("study")),
            "/intent/recording": self.sources.get("recording", _generated_source("recording")),
        }
        categories: tuple[tuple[str, Sequence[Any], str], ...] = (
            ("signals", self.signals, "semantic_id"),
            ("events", self.events, "semantic_id"),
            ("processing", self.processing, "semantic_id"),
            ("windows", self.windows, "semantic_id"),
            ("quality_gates", self.quality_gates, "semantic_id"),
            ("models", self.models, "semantic_id"),
            ("comparisons", self.comparisons, "semantic_id"),
            ("outcomes", self.outcomes, "semantic_id"),
            ("adaptations", self.adaptations, "semantic_id"),
            ("calibrations", self.calibrations, "semantic_id"),
            ("policies", self.policies, "semantic_id"),
            ("actions", self.actions, "semantic_id"),
            ("phases", self.phases, "semantic_id"),
        )
        for category, values, identity in categories:
            for index, value in enumerate(values):
                semantic_id = getattr(value, identity)
                locations[f"/intent/{category}/{index}"] = self.sources.get(
                    semantic_id,
                    _generated_source(semantic_id),
                )
        for index, value in enumerate(self.acceptance):
            key = f"acceptance.{value.criterion_id}"
            locations[f"/intent/acceptance/{index}"] = self.sources.get(key, _generated_source(key))
        return DraftSourceMap(locations, fallback=_generated_source(self.experiment_id))

    def build(self) -> "ComposedExperiment":
        return ComposedExperiment(self, lower_experiment_design(self))

    def _append(self, field_name: str, value: Any, semantic_id: str, symbol: str) -> "ExperimentDesign":
        values = (*getattr(self, field_name), value)
        sources = dict(self.sources)
        sources[semantic_id] = SourceLocation(SourceKind.PYTHON, symbol=symbol)
        return replace(self, **{field_name: values, "sources": sources})


@dataclass(frozen=True, slots=True)
class WrittenComposedProject:
    root: Path
    files: Mapping[str, Path]
    manifest_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "files", MappingProxyType({str(k): Path(v) for k, v in self.files.items()}))
        object.__setattr__(self, "manifest_digest", require_digest(self.manifest_digest, "manifest_digest"))


@dataclass(frozen=True, slots=True)
class ComposedExperiment:
    design: ExperimentDesign
    lowered: LoweredExperiment

    def __post_init__(self) -> None:
        if not isinstance(self.design, ExperimentDesign) or not isinstance(self.lowered, LoweredExperiment):
            raise TypeError("composed experiments require a design and lowered result")

    @property
    def protocol(self) -> ProtocolSpec:
        return self.lowered.protocol

    @property
    def suite(self) -> SuiteSpec:
        return self.lowered.suite

    @property
    def requirements(self) -> DeploymentRequirements:
        return self.lowered.deployment_requirements

    @property
    def provenance(self) -> AuthoringProvenance:
        return self.lowered.provenance

    def canonical_specs(self) -> dict[str, Any]:
        return {"protocol": self.protocol.to_payload(), "suite": self.suite.to_payload()}

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": COMPOSED_EXPERIMENT_SCHEMA_ID,
            "design": self.design.to_payload(),
            "protocol": self.protocol.to_payload(),
            "suite": self.suite.to_payload(),
            "deployment_requirements": self.requirements.to_payload(),
            "authoring_provenance": self.provenance.to_payload(),
        }

    def compile(
        self,
        deployment: DeploymentSpec,
        registry: PluginRegistry,
        *,
        model_manifests: Mapping[str, "ModelManifest"] | None = None,
    ) -> "CompilationResult":
        return compile_suite(
            self.protocol,
            self.suite,
            deployment,
            registry,
            model_manifests=model_manifests,
        )

    def write_project(self, root: str | Path, *, overwrite: bool = False) -> WrittenComposedProject:
        target = Path(root)
        payloads = {
            "experiment.design.json": self.design.to_payload(),
            "protocol.json": self.protocol.to_payload(),
            "suite.json": self.suite.to_payload(),
            "deployment-requirements.json": self.requirements.to_payload(),
            "authoring-provenance.json": self.provenance.to_payload(),
        }
        manifest = {
            "schema": COMPOSED_PROJECT_SCHEMA_ID,
            "experiment_id": self.design.experiment_id,
            "revision": self.design.revision,
            "design_digest": self.design.design_digest,
            "files": {
                name: {"schema": payload.get("schema"), "canonical_digest": canonical_hash(payload)}
                for name, payload in sorted(payloads.items())
            },
        }
        all_payloads = {**payloads, "authoring-project.json": manifest}
        existing = sorted(name for name in all_payloads if (target / name).exists())
        if existing and not overwrite:
            raise FileExistsError("composed authoring project files already exist: " + ", ".join(existing))
        target.mkdir(parents=True, exist_ok=True)
        files = {name: _atomic_json(target / name, payload) for name, payload in sorted(all_payloads.items())}
        return WrittenComposedProject(target, files, canonical_hash(manifest))


def lower_experiment_design(design: ExperimentDesign) -> LoweredExperiment:
    """Lower one complete named design through the canonical authority boundary."""

    if not isinstance(design, ExperimentDesign):
        raise TypeError("design lowering requires an ExperimentDesign")
    return _DesignLowerer(design).lower()


@dataclass(frozen=True, slots=True)
class _OutputBinding:
    component_id: str
    port: str
    kind: str
    rate_hz: float | None = None


class _DesignLowerer:
    def __init__(self, design: ExperimentDesign) -> None:
        self.design = design
        self.draft = design.to_draft()
        self.source_map = design.draft_source_map()
        self.components: dict[str, ComponentSpec] = {}
        self.routes: dict[str, RouteSpec] = {}
        self.outputs: dict[str, _OutputBinding] = {}
        self.semantic_components: dict[str, tuple[str, ...]] = {}
        self.bindings: dict[tuple[CanonicalArtifact, str], str] = {}

    def lower(self) -> LoweredExperiment:
        issues = self._reference_issues()
        if issues:
            raise DraftLoweringError(tuple(issues))
        protocol = self._protocol()
        streams = self._sources()
        self._processing()
        self._windows()
        self._quality()
        model_uses = self._models()
        outcome_expectations = self._outcomes()
        self._policies()
        self._actions()
        self._recording()
        phases = self._phases()
        artifacts = self._calibrations(phases)
        adaptations = self._adaptations()
        suite = SuiteSpec(
            suite_id=self.design.study.suite_id or f"suite.{self.design.experiment_id}",
            protocol_id=protocol.protocol_id,
            streams=tuple(sorted(streams, key=lambda value: value.stream_id)),
            components=tuple(sorted(self.components.values(), key=lambda value: value.component_id)),
            routes=tuple(sorted(self.routes.values(), key=lambda value: value.route_id)),
            phases=phases,
            initial_phase=self._initial_phase(phases),
            clock_policy={
                "execution_clock_id": self.design.study.execution_clock_id,
                "ordering": "availability_watermark",
            },
            recording={
                "execution_capture": self.design.recording.execution_capture,
                "semantic_evidence": self.design.recording.semantic_evidence,
                "raw_recording": self.design.recording.raw_recording,
            },
            validation={"require_replay_equivalence": True},
            artifacts=artifacts,
            model_uses=model_uses,
            outcome_expectations=outcome_expectations,
            adaptations=adaptations,
            scheduling=SchedulingSpec(),
        )
        requirements = _design_requirements(suite)
        provenance = self._provenance(protocol, suite)
        return LoweredExperiment(protocol, suite, requirements, provenance)

    def _reference_issues(self) -> list[DraftIssue]:
        signal_refs = {value.semantic_id for value in self.design.signals}
        event_refs = {value.semantic_id for value in self.design.events}
        processing_refs = {value.semantic_id for value in self.design.processing}
        window_refs = {value.semantic_id for value in self.design.windows}
        quality_refs = {value.semantic_id for value in self.design.quality_gates}
        model_refs = {value.semantic_id for value in self.design.models}
        outcome_refs = {value.semantic_id for value in self.design.outcomes}
        policy_refs = {value.semantic_id for value in self.design.policies}
        action_refs = {value.semantic_id for value in self.design.actions}
        valid = signal_refs | event_refs | processing_refs | window_refs | quality_refs | model_refs | outcome_refs | policy_refs | action_refs
        phases = {value.semantic_id for value in self.design.phases}
        issues: list[DraftIssue] = []

        def require(ref: str, path: str, *, allowed: set[str] = valid, port: bool = True) -> None:
            if _reference_base(ref, allowed, allow_port=port) is None:
                issues.append(self._issue("authoring.missing_reference", path, f"unknown semantic reference {ref}"))

        for index, chain in enumerate(self.design.processing):
            require(
                chain.input,
                f"/intent/processing/{index}/input",
                allowed=signal_refs | processing_refs,
            )
        for index, window in enumerate(self.design.windows):
            require(
                window.input,
                f"/intent/windows/{index}/input",
                allowed=signal_refs | processing_refs,
            )
            if window.event_stream is not None:
                require(window.event_stream, f"/intent/windows/{index}/event_stream", allowed=event_refs, port=False)
        for index, gate in enumerate(self.design.quality_gates):
            require(
                gate.input,
                f"/intent/quality_gates/{index}/input",
                allowed=signal_refs | processing_refs | window_refs,
            )
        for index, model in enumerate(self.design.models):
            for port, ref in model.inputs.items():
                require(
                    str(ref),
                    f"/intent/models/{index}/inputs/{_escape_pointer(str(port))}",
                    allowed=signal_refs
                    | event_refs
                    | processing_refs
                    | window_refs
                    | quality_refs,
                )
        for index, comparison in enumerate(self.design.comparisons):
            for member in comparison.members:
                require(member, f"/intent/comparisons/{index}/members", allowed=model_refs, port=False)
        for index, outcome in enumerate(self.design.outcomes):
            require(outcome.event_stream, f"/intent/outcomes/{index}/event_stream", allowed=event_refs, port=False)
            for model in outcome.models:
                require(model, f"/intent/outcomes/{index}/models", allowed=model_refs, port=False)
        for index, adaptation in enumerate(self.design.adaptations):
            require(adaptation.model, f"/intent/adaptations/{index}/model", allowed=model_refs, port=False)
            require(adaptation.outcome, f"/intent/adaptations/{index}/outcome", allowed=outcome_refs, port=False)
            for phase in adaptation.enabled_phases:
                require(phase, f"/intent/adaptations/{index}/enabled_phases", allowed=phases, port=False)
        for index, policy in enumerate(self.design.policies):
            for port, ref in policy.inputs.items():
                require(
                    str(ref),
                    f"/intent/policies/{index}/inputs/{_escape_pointer(str(port))}",
                    allowed=model_refs | quality_refs | outcome_refs,
                )
        for index, action in enumerate(self.design.actions):
            require(action.policy, f"/intent/actions/{index}/policy", allowed=policy_refs, port=False)
        for index, phase in enumerate(self.design.phases):
            for ref in phase.active:
                require(ref, f"/intent/phases/{index}/active")
            for target in phase.next_phases:
                require(target, f"/intent/phases/{index}/next_phases", allowed=phases, port=False)
        if self.design.initial_phase is not None:
            require(
                self.design.initial_phase,
                "/intent/initial_phase",
                allowed=phases,
                port=False,
            )
        elif len(self.design.phases) > 1:
            issues.append(
                self._issue(
                    "authoring.initial_phase",
                    "/intent/initial_phase",
                    "multi-phase designs require an explicit initial_phase",
                )
            )
        for index, calibration in enumerate(self.design.calibrations):
            require(calibration.producer, f"/intent/calibrations/{index}/producer")
            require(calibration.producer_phase, f"/intent/calibrations/{index}/producer_phase", allowed=phases, port=False)
            for phase in calibration.required_phases:
                require(phase, f"/intent/calibrations/{index}/required_phases", allowed=phases, port=False)
        for index, ref in enumerate(self.design.recording.inputs):
            require(
                ref,
                f"/intent/recording/inputs/{index}",
                allowed=signal_refs | event_refs | processing_refs,
            )
        membership: dict[str, str] = {}
        for group in self.design.comparisons:
            for member in group.members:
                if member in membership:
                    issues.append(self._issue("authoring.comparison_membership", "/intent/comparisons", f"{member} belongs to both {membership[member]} and {group.semantic_id}"))
                membership[member] = group.semantic_id
        for model in self.design.models:
            if model.role in {"shadow", "candidate"} and model.semantic_id not in membership:
                issues.append(self._issue("authoring.comparison_required", "/intent/models", f"{model.semantic_id} role {model.role} requires a comparison group"))
        return issues

    def _protocol(self) -> ProtocolSpec:
        metrics = tuple(MetricSpec(value.metric_id, value.measure, value.parameters) for value in self.design.acceptance)
        criteria = tuple(AcceptanceCriterion(value.criterion_id, value.metric_id, value.operator, value.value) for value in self.design.acceptance)
        protocol = ProtocolSpec(
            protocol_id=self.design.study.protocol_id or f"protocol.{self.design.experiment_id}",
            execution_mode=self.design.study.execution_mode,
            claims=(ClaimSpec(f"claim.{self.design.experiment_id}", self.design.study.statement),),
            metrics=metrics,
            acceptance=criteria,
            annotations=self.design.study.annotations,
        )
        self.bindings[(CanonicalArtifact.PROTOCOL, "")] = "/intent/study"
        return protocol

    def _sources(self) -> list[LogicalStreamSpec]:
        streams: list[LogicalStreamSpec] = []
        for signal in self.design.signals:
            stream_id = f"stream.{signal.signal_id}"
            component_id = f"source.{signal.signal_id}"
            units = {channel: signal.channel_units.get(channel, signal.unit) for channel in signal.channels}
            contract = SignalContract(
                type_id="eegle.dense_sample_batch.v1",
                unit=signal.unit,
                channel_count=len(signal.channels),
                nominal_rate_hz=signal.nominal_rate_hz,
                content_kind="dense_samples",
                rate_model=signal.rate_model,
                channel_ids=signal.channels,
                units=units,
                missing_data_policy=signal.missing_data_policy,
                layout=signal.layout,
            )
            streams.append(LogicalStreamSpec(stream_id, contract, signal.modality, signal.clock_id))
            self._component(ComponentSpec(component_id, ComponentKind.SOURCE, stream_id=stream_id))
            self.outputs[signal.semantic_id] = _OutputBinding(component_id, "samples", "dense", signal.nominal_rate_hz)
            self.semantic_components[signal.semantic_id] = (component_id,)
        for event in self.design.events:
            stream_id = f"stream.{event.event_id}"
            component_id = f"source.{event.event_id}"
            contract = SignalContract(
                type_id="eegle.sparse_event_batch.v1",
                content_kind="sparse_events",
                rate_model="irregular",
                event_kinds=event.event_kinds,
            )
            streams.append(LogicalStreamSpec(stream_id, contract, event.modality, event.clock_id))
            self._component(ComponentSpec(component_id, ComponentKind.SOURCE, stream_id=stream_id))
            self.outputs[event.semantic_id] = _OutputBinding(component_id, "events", "sparse", None)
            self.semantic_components[event.semantic_id] = (component_id,)
        return streams

    def _processing(self) -> None:
        pending = list(self.design.processing)
        while pending:
            progress = False
            for chain in tuple(pending):
                if self._resolve(chain.input) is None:
                    continue
                previous = self._resolve_required(chain.input)
                chain_components: list[str] = []
                for step in chain.steps:
                    component_id = f"processing.{chain.chain_id}.{step.step_id}"
                    self._component(ComponentSpec(component_id, ComponentKind.TRANSFORM, step.plugin_id, step.version_spec, step.config))
                    self._route(previous, component_id, step.input_port)
                    previous = _OutputBinding(component_id, step.output_port, previous.kind, previous.rate_hz)
                    chain_components.append(component_id)
                self.outputs[chain.semantic_id] = previous
                self.semantic_components[chain.semantic_id] = tuple(chain_components)
                pending.remove(chain)
                progress = True
            if not progress:
                refs = ", ".join(value.semantic_id for value in pending)
                raise DraftLoweringError((self._issue("authoring.processing_cycle", "/intent/processing", f"processing chains contain a cycle: {refs}"),))

    def _windows(self) -> None:
        for window in self.design.windows:
            source = self._resolve_required(window.input)
            component_id = f"window.{window.window_id}"
            if window.kind == WindowKind.CONTINUOUS:
                if source.rate_hz is None:
                    raise DraftLoweringError((self._issue("authoring.window_rate", "/intent/windows", f"{window.semantic_id} needs an explicit input rate"),))
                window_samples = _seconds_to_samples(window.duration_seconds, source.rate_hz, "duration_seconds")
                step_samples = _seconds_to_samples(window.step_seconds, source.rate_hz, "step_seconds")
                component = ComponentSpec(
                    component_id,
                    ComponentKind.WINDOW,
                    "eegle.processing.continuous_window",
                    "~=0.1.0",
                    {"window_samples": window_samples, "step_samples": step_samples},
                )
                self._component(component)
                self._route(source, component_id, "samples")
            else:
                events = self._resolve_required(str(window.event_stream))
                component = ComponentSpec(
                    component_id,
                    ComponentKind.WINDOW,
                    "eegle.processing.event_window",
                    "~=0.1.0",
                    {
                        "start_offset_seconds": window.start_offset_seconds,
                        "end_offset_seconds": window.end_offset_seconds,
                        "event_kinds": [window.event_kind],
                        "max_buffer_samples": window.max_buffer_samples,
                    },
                )
                self._component(component)
                self._route(source, component_id, "samples")
                self._route(events, component_id, "events")
            self.outputs[window.semantic_id] = _OutputBinding(component_id, "windows", "window", source.rate_hz)
            self.semantic_components[window.semantic_id] = (component_id,)

    def _quality(self) -> None:
        for gate in self.design.quality_gates:
            source = self._resolve_required(gate.input)
            component_id = f"quality.{gate.gate_id}"
            config = {"gate_id": gate.gate_id, **thaw_json(gate.config)}
            self._component(ComponentSpec(component_id, ComponentKind.QUALITY, gate.plugin_id, gate.version_spec, config))
            self._route(source, component_id, gate.input_port)
            self.outputs[gate.semantic_id] = _OutputBinding(component_id, gate.output_port, "quality", None)
            self.semantic_components[gate.semantic_id] = (component_id,)

    def _models(self) -> tuple[ModelUseSpec, ...]:
        groups = {member: group.comparison_id for group in self.design.comparisons for member in group.members}
        uses: list[ModelUseSpec] = []
        for model in self.design.models:
            component_id = f"model.{model.model_id}"
            self._component(ComponentSpec(component_id, ComponentKind.MODEL, model.plugin_id, model.version_spec, model.config))
            for port, ref in sorted(model.inputs.items()):
                self._route(self._resolve_required(str(ref)), component_id, str(port))
            self.outputs[model.semantic_id] = _OutputBinding(component_id, "prediction", "prediction", None)
            self.semantic_components[model.semantic_id] = (component_id,)
            uses.append(ModelUseSpec(component_id, model.manifest_digest, model.role, groups.get(model.semantic_id)))
        return tuple(sorted(uses, key=lambda value: value.component_id))

    def _outcomes(self) -> tuple[OutcomeExpectationSpec, ...]:
        expectations: list[OutcomeExpectationSpec] = []
        for outcome in self.design.outcomes:
            component_id = f"outcome.{outcome.outcome_id}"
            self._component(
                ComponentSpec(
                    component_id,
                    ComponentKind.OUTCOME,
                    outcome.plugin_id,
                    outcome.version_spec,
                    {"event_kind": outcome.event_kind, "permitted_uses": list(outcome.permitted_uses)},
                    outcome_uses=outcome.permitted_uses,
                )
            )
            self._route(self._resolve_required(outcome.event_stream), component_id, "events")
            self.outputs[outcome.semantic_id] = _OutputBinding(component_id, "outcomes", "outcome", None)
            self.semantic_components[outcome.semantic_id] = (component_id,)
            for model_ref in outcome.models:
                model_component = self._resolve_required(model_ref).component_id
                expectations.append(
                    OutcomeExpectationSpec(
                        f"expectation.{outcome.outcome_id}.{model_component.removeprefix('model.')}",
                        model_component,
                        (component_id,),
                        outcome.permitted_uses,
                        outcome.max_pending_predictions,
                        outcome.prediction_ttl_seconds,
                    )
                )
        return tuple(sorted(expectations, key=lambda value: value.expectation_id))

    def _policies(self) -> None:
        for policy in self.design.policies:
            component_id = f"policy.{policy.policy_id}"
            self._component(ComponentSpec(component_id, ComponentKind.POLICY, policy.plugin_id, policy.version_spec, policy.config))
            for port, ref in sorted(policy.inputs.items()):
                self._route(self._resolve_required(str(ref)), component_id, str(port))
            self.outputs[policy.semantic_id] = _OutputBinding(component_id, "request", "action_request", None)
            self.semantic_components[policy.semantic_id] = (component_id,)

    def _actions(self) -> None:
        for action in self.design.actions:
            component_id = f"actuator.{action.action_id}"
            self._component(
                ComponentSpec(
                    component_id,
                    ComponentKind.ACTUATOR,
                    action.plugin_id,
                    action.version_spec,
                    action.config,
                    action_capabilities=(action.capability,),
                )
            )
            policy = self._resolve_required(action.policy)
            self._route(_OutputBinding(policy.component_id, action.policy_output_port, policy.kind), component_id, action.actuator_input_port)
            self.outputs[action.semantic_id] = _OutputBinding(component_id, "receipt", "receipt", None)
            self.semantic_components[action.semantic_id] = (component_id,)

    def _recording(self) -> None:
        components: list[str] = []
        for ref in self.design.recording.inputs:
            source = self._resolve_required(ref)
            if source.kind not in {"dense", "sparse"}:
                raise DraftLoweringError((self._issue("authoring.recording_contract", "/intent/recording/inputs", f"{ref} is not a recordable dense or sparse stream"),))
            suffix = ref.replace(":", ".").replace("/", ".")
            component_id = f"sink.{suffix}"
            plugin_id = "eegle.recording.sparse_sink" if source.kind == "sparse" else "eegle.recording.dense_sink"
            self._component(ComponentSpec(component_id, ComponentKind.SINK, plugin_id, "~=0.1.0"))
            self._route(source, component_id, "records")
            components.append(component_id)
        self.semantic_components["recording"] = tuple(components)

    def _phases(self) -> tuple[PhaseSpec, ...]:
        required: dict[str, list[str]] = {}
        for calibration in self.design.calibrations:
            for phase in calibration.required_phases:
                required.setdefault(phase, []).append(f"calibration.{calibration.calibration_id}")
        if not self.design.phases:
            return (
                PhaseSpec(
                    "phase.run",
                    tuple(sorted(self.components)),
                    resume_policy=ResumePolicy.CHECKPOINT,
                ),
            )
        phases: list[PhaseSpec] = []
        for phase in self.design.phases:
            component_ids: set[str] = set()
            for ref in phase.active:
                base = _reference_base(ref, set(self.semantic_components), allow_port=True)
                if base is None:
                    continue
                component_ids.update(self.semantic_components[base])
            transitions = tuple(
                PhaseTransition(target, TransitionCondition.COMPLETE)
                for target in phase.next_phases
            )
            phases.append(
                PhaseSpec(
                    phase.semantic_id,
                    tuple(sorted(component_ids)),
                    transitions=transitions,
                    required_artifacts=tuple(sorted(required.get(phase.semantic_id, ()))),
                    retry_limit=phase.retry_limit,
                    resume_policy=phase.resume_policy,
                    operator_confirmation=phase.operator_confirmation,
                    timeout_seconds=phase.timeout_seconds,
                )
            )
        return tuple(phases)

    def _calibrations(self, phases: tuple[PhaseSpec, ...]) -> tuple[ArtifactSpec, ...]:
        phase_ids = {value.phase_id for value in phases}
        artifacts: list[ArtifactSpec] = []
        for calibration in self.design.calibrations:
            producer = self._resolve_required(calibration.producer)
            if calibration.producer_phase not in phase_ids:
                raise DraftLoweringError((self._issue("authoring.calibration_phase", "/intent/calibrations", f"unknown producer phase {calibration.producer_phase}"),))
            artifacts.append(
                ArtifactSpec(
                    f"calibration.{calibration.calibration_id}",
                    calibration.role,
                    calibration.media_type,
                    calibration.producer_phase,
                    producer.component_id,
                    calibration.producer_port,
                )
            )
        return tuple(artifacts)

    def _initial_phase(self, phases: tuple[PhaseSpec, ...]) -> str:
        if self.design.initial_phase is not None:
            return self.design.initial_phase
        if len(phases) == 1:
            return phases[0].phase_id
        if not self.design.phases:
            return "phase.run"
        raise DraftLoweringError(
            (
                self._issue(
                    "authoring.initial_phase",
                    "/intent/initial_phase",
                    "multi-phase designs require an explicit initial_phase",
                ),
            )
        )

    def _adaptations(self) -> tuple[AdaptationSpec, ...]:
        adaptations: list[AdaptationSpec] = []
        for value in self.design.adaptations:
            model_component = self._resolve_required(value.model).component_id
            outcome_id = value.outcome.removeprefix("outcome.")
            expectation_id = f"expectation.{outcome_id}.{model_component.removeprefix('model.')}"
            adaptations.append(AdaptationSpec(value.semantic_id, expectation_id, model_component, value.enabled_phases))
        return tuple(adaptations)

    def _component(self, component: ComponentSpec) -> None:
        if component.component_id in self.components:
            raise DraftLoweringError(
                (
                    self._issue(
                        "authoring.generated_identity",
                        "/intent",
                        f"declarations collide at generated component {component.component_id}",
                    ),
                )
            )
        self.components[component.component_id] = component

    def _route(self, source: _OutputBinding, target_component: str, target_port: str) -> None:
        route_id = f"route.{target_component}.{target_port}"
        if route_id in self.routes:
            raise DraftLoweringError((self._issue("authoring.duplicate_input", "/intent", f"multiple inputs target {target_component}.{target_port}"),))
        self.routes[route_id] = RouteSpec(route_id, source.component_id, source.port, target_component, target_port)

    def _resolve(self, reference: str) -> _OutputBinding | None:
        base = _reference_base(reference, set(self.outputs), allow_port=True)
        if base is None:
            return None
        output = self.outputs.get(base)
        if output is None:
            return None
        if reference == base:
            return output
        return _OutputBinding(output.component_id, reference[len(base) + 1 :], output.kind, output.rate_hz)

    def _resolve_required(self, reference: str) -> _OutputBinding:
        value = self._resolve(reference)
        if value is None:
            raise DraftLoweringError((self._issue("authoring.unresolved_reference", "/intent", f"cannot lower unresolved reference {reference}"),))
        return value

    def _issue(self, code: str, path: str, message: str) -> DraftIssue:
        return DraftIssue(code, path, message, self.source_map.source_for(path))

    def _provenance(self, protocol: ProtocolSpec, suite: SuiteSpec) -> AuthoringProvenance:
        self._index_bindings(suite)
        entries: list[ProvenanceEntry] = []
        for artifact, payload in (
            (CanonicalArtifact.PROTOCOL, protocol.to_payload()),
            (CanonicalArtifact.SUITE, suite.to_payload()),
        ):
            for path in _leaf_paths(payload):
                draft_path = self._binding_for(artifact, path)
                explicit = draft_path is not None
                entries.append(
                    ProvenanceEntry(
                        artifact,
                        path,
                        AuthoringOrigin.USER_EXPLICIT if explicit else AuthoringOrigin.AUTHORING_DEFAULT,
                        self.source_map.source_for(draft_path or "/intent"),
                        _materiality(artifact, path),
                        ConfirmationState.NOT_REQUIRED,
                    )
                )
        return AuthoringProvenance(
            self.draft.draft_id,
            self.draft.revision,
            self.draft.draft_digest,
            {
                CanonicalArtifact.PROTOCOL: CanonicalTarget(PROTOCOL_SPEC_SCHEMA, protocol.spec_hash),
                CanonicalArtifact.SUITE: CanonicalTarget(SUITE_SPEC_SCHEMA, suite.spec_hash),
            },
            tuple(entries),
        )

    def _index_bindings(self, suite: SuiteSpec) -> None:
        stream_paths = {
            **{
                f"stream.{value.signal_id}": f"/intent/signals/{index}"
                for index, value in enumerate(self.design.signals)
            },
            **{
                f"stream.{value.event_id}": f"/intent/events/{index}"
                for index, value in enumerate(self.design.events)
            },
        }
        for index, stream in enumerate(suite.streams):
            path = stream_paths.get(stream.stream_id)
            if path is not None:
                self.bindings[(CanonicalArtifact.SUITE, f"/streams/{index}")] = path
        for index, component in enumerate(suite.components):
            semantic = self._semantic_for_component(component.component_id)
            path = _semantic_draft_path(self.design, semantic)
            if path is not None:
                self.bindings[(CanonicalArtifact.SUITE, f"/components/{index}")] = path
        for index, route in enumerate(suite.routes):
            semantic = self._semantic_for_component(route.target_component)
            path = _semantic_draft_path(self.design, semantic)
            if path is not None:
                self.bindings[(CanonicalArtifact.SUITE, f"/routes/{index}")] = path
        for index, model in enumerate(suite.model_uses):
            semantic = self._semantic_for_component(model.component_id)
            path = _semantic_draft_path(self.design, semantic)
            if path is not None:
                self.bindings[(CanonicalArtifact.SUITE, f"/model_uses/{index}")] = path
        for index, outcome in enumerate(suite.outcome_expectations):
            semantic = self._semantic_for_component(outcome.outcome_component_ids[0])
            path = _semantic_draft_path(self.design, semantic)
            if path is not None:
                self.bindings[(CanonicalArtifact.SUITE, f"/outcome_expectations/{index}")] = path
        for index, adaptation in enumerate(suite.adaptations):
            path = _semantic_draft_path(self.design, adaptation.adaptation_id)
            if path is not None:
                self.bindings[(CanonicalArtifact.SUITE, f"/adaptations/{index}")] = path
        phase_paths = {
            value.semantic_id: f"/intent/phases/{index}"
            for index, value in enumerate(self.design.phases)
        }
        for index, phase in enumerate(suite.phases):
            path = phase_paths.get(phase.phase_id)
            if path is not None:
                self.bindings[(CanonicalArtifact.SUITE, f"/phases/{index}")] = path
        calibration_paths = {
            value.semantic_id: f"/intent/calibrations/{index}"
            for index, value in enumerate(self.design.calibrations)
        }
        for index, artifact in enumerate(suite.artifacts):
            path = calibration_paths.get(artifact.artifact_id)
            if path is not None:
                self.bindings[(CanonicalArtifact.SUITE, f"/artifacts/{index}")] = path

    def _semantic_for_component(self, component_id: str) -> str | None:
        return next(
            (
                semantic
                for semantic, components in self.semantic_components.items()
                if component_id in components
            ),
            None,
        )

    def _binding_for(self, artifact: CanonicalArtifact, path: str) -> str | None:
        matches = [
            (prefix, draft_path)
            for (candidate, prefix), draft_path in self.bindings.items()
            if candidate == artifact and (not prefix or path == prefix or path.startswith(prefix + "/"))
        ]
        if matches:
            return max(matches, key=lambda item: len(item[0]))[1]
        if artifact == CanonicalArtifact.SUITE:
            component_match = _indexed_identity_path(path, "/components", tuple(sorted(self.components)))
            if component_match is not None:
                semantic = next((key for key, values in self.semantic_components.items() if component_match in values), None)
                return _semantic_draft_path(self.design, semantic)
        return None


def _design_requirements(suite: SuiteSpec) -> DeploymentRequirements:
    streams = {value.stream_id: value for value in suite.streams}
    execution_clock = str(suite.clock_policy["execution_clock_id"])
    requirements: list[DeploymentRequirement] = []
    for component in suite.components:
        if component.kind != ComponentKind.SOURCE or component.stream_id is None:
            continue
        stream = streams[component.stream_id]
        suffix = component.component_id.removeprefix("source.")
        requirements.append(
            DeploymentRequirement(
                f"requirement.source.{suffix}",
                DeploymentRequirementKind.SOURCE_BINDING,
                component_id=component.component_id,
                stream_id=stream.stream_id,
                contract=stream.contract.to_payload(),
            )
        )
        if stream.clock_id is not None and stream.clock_id != execution_clock:
            requirements.append(
                DeploymentRequirement(
                    f"requirement.clock.{suffix}",
                    DeploymentRequirementKind.CLOCK_MAPPING,
                    source_clock=stream.clock_id,
                    target_clock=execution_clock,
                )
            )
    requirements.append(DeploymentRequirement("requirement.storage.evidence", DeploymentRequirementKind.STORAGE, storage_kind="evidence"))
    for model in suite.model_uses:
        requirements.append(
            DeploymentRequirement(
                f"requirement.model.{model.component_id.removeprefix('model.')}",
                DeploymentRequirementKind.MODEL_ARTIFACT,
                component_id=model.component_id,
                manifest_digest=model.manifest_digest,
            )
        )
    for component in suite.components:
        for capability in component.action_capabilities:
            requirements.append(
                DeploymentRequirement(
                    f"requirement.authorization.{component.component_id.removeprefix('actuator.')}",
                    DeploymentRequirementKind.AUTHORIZATION,
                    component_id=component.component_id,
                    action_capability=capability,
                )
            )
    return DeploymentRequirements(suite.suite_id, tuple(requirements))


def _semantic_sources(payload: Mapping[str, Any], source_map: DraftSourceMap | None) -> dict[str, SourceLocation]:
    if source_map is None:
        return {}
    result = {"study": source_map.source_for("/study"), "recording": source_map.source_for("/recording")}
    categories = {
        "signals": ("signal_id", "signal"),
        "events": ("event_id", "event"),
        "processing": ("chain_id", "processing"),
        "windows": ("window_id", "window"),
        "quality_gates": ("gate_id", "quality"),
        "models": ("model_id", "model"),
        "comparisons": ("comparison_id", "comparison"),
        "outcomes": ("outcome_id", "outcome"),
        "adaptations": ("adaptation_id", "adaptation"),
        "calibrations": ("calibration_id", "calibration"),
        "policies": ("policy_id", "policy"),
        "actions": ("action_id", "action"),
        "phases": ("phase_id", "phase"),
        "acceptance": ("criterion_id", "acceptance"),
    }
    for category, (field_name, prefix) in categories.items():
        for index, value in enumerate(payload.get(category, ())):
            result[f"{prefix}.{value[field_name]}"] = source_map.source_for(f"/{category}/{index}")
    return result


def _semantic_draft_path(design: ExperimentDesign, semantic: str | None) -> str | None:
    if semantic is None:
        return None
    categories: tuple[tuple[str, Sequence[Any]], ...] = (
        ("signals", design.signals),
        ("events", design.events),
        ("processing", design.processing),
        ("windows", design.windows),
        ("quality_gates", design.quality_gates),
        ("models", design.models),
        ("comparisons", design.comparisons),
        ("outcomes", design.outcomes),
        ("adaptations", design.adaptations),
        ("calibrations", design.calibrations),
        ("policies", design.policies),
        ("actions", design.actions),
        ("phases", design.phases),
    )
    for category, values in categories:
        for index, value in enumerate(values):
            if value.semantic_id == semantic:
                return f"/intent/{category}/{index}"
    if semantic == "recording":
        return "/intent/recording"
    return None


def _indexed_identity_path(path: str, prefix: str, identities: tuple[str, ...]) -> str | None:
    if not path.startswith(prefix + "/"):
        return None
    tail = path[len(prefix) + 1 :]
    index_text = tail.split("/", 1)[0]
    if not index_text.isdigit():
        return None
    index = int(index_text)
    return identities[index] if index < len(identities) else None


def _reference_base(reference: str, candidates: set[str], *, allow_port: bool) -> str | None:
    if reference in candidates:
        return reference
    if not allow_port:
        return None
    matches = [candidate for candidate in candidates if reference.startswith(candidate + ".")]
    return max(matches, key=len, default=None)


def _seconds_to_samples(seconds: float | None, rate_hz: float, field: str) -> int:
    if seconds is None:
        raise ValueError(f"{field} is required")
    exact = seconds * rate_hz
    rounded = round(exact)
    if abs(exact - rounded) > 1e-9 or rounded <= 0:
        raise DraftLoweringError(
            (
                DraftIssue(
                    "authoring.window_samples",
                    "/intent/windows",
                    f"{field} at {rate_hz:g} Hz must resolve to a positive whole sample count",
                    _generated_source(field),
                ),
            )
        )
    return int(rounded)


def _materiality(artifact: CanonicalArtifact, path: str) -> ScientificMateriality:
    if artifact == CanonicalArtifact.PROTOCOL:
        return ScientificMateriality.OPERATIONAL if path in {"/schema", "/protocol_id"} else ScientificMateriality.SCIENTIFIC
    if path.startswith(("/streams", "/components", "/routes", "/model_uses", "/outcome_expectations", "/adaptations", "/clock_policy", "/validation")):
        return ScientificMateriality.SCIENTIFIC
    return ScientificMateriality.OPERATIONAL


def _leaf_paths(value: Any, path: str = "") -> Iterable[str]:
    if isinstance(value, Mapping):
        if not value:
            yield path
        for key in sorted(value):
            yield from _leaf_paths(value[key], f"{path}/{_escape_pointer(str(key))}")
    elif isinstance(value, (list, tuple)):
        if not value:
            yield path
        for index, item in enumerate(value):
            yield from _leaf_paths(item, f"{path}/{index}")
    else:
        yield path


def _field_names(value: type[Any]) -> set[str]:
    return set(value.__dataclass_fields__)  # type: ignore[attr-defined]


def _reject_unknown(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unknown {label} fields: {', '.join(unknown)}")


def _unique(values: Iterable[str], label: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{label} identities must be unique")


def _semantic_reference(value: str, field: str) -> str:
    normalized = require_identifier(value, field)
    if "." not in normalized:
        raise ValueError(f"{field} must use a named semantic prefix")
    return normalized


def _prefixed_reference(value: str, prefix: str, field: str) -> str:
    normalized = require_identifier(value, field)
    return normalized if normalized.startswith(prefix + ".") else f"{prefix}.{normalized}"


def _model_reference(value: str) -> str:
    return _prefixed_reference(value, "model", "model reference")


def _event_reference(value: str) -> str:
    return _prefixed_reference(value, "event", "event reference")


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _generated_source(symbol: str) -> SourceLocation:
    return SourceLocation(SourceKind.GENERATED, symbol=symbol)


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> Path:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


__all__ = [
    "AcceptanceDeclaration",
    "ActionDeclaration",
    "AdaptationDeclaration",
    "CalibrationDeclaration",
    "COMPOSED_EXPERIMENT_SCHEMA_ID",
    "COMPOSED_PROJECT_SCHEMA_ID",
    "ComparisonGroup",
    "ComposedExperiment",
    "EventDeclaration",
    "EXPERIMENT_DESIGN_JSON_SCHEMA",
    "EXPERIMENT_DESIGN_SCHEMA_ID",
    "ExperimentDesign",
    "ModelDeclaration",
    "OutcomeDeclaration",
    "PhaseDeclaration",
    "PolicyDeclaration",
    "ProcessingChain",
    "ProcessingStep",
    "QualityGateDeclaration",
    "RecordingDeclaration",
    "SignalDeclaration",
    "StudyIntent",
    "WindowDeclaration",
    "WindowKind",
    "WrittenComposedProject",
    "lower_experiment_design",
]
