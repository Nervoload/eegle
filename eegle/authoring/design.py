"""Immutable bounded declarations for compositional experiment intent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from eegle._domain import ExecutionMode
from eegle._validation import (
    freeze_json,
    require_digest,
    require_finite,
    require_identifier,
    thaw_json,
)
from eegle.specs import ComparisonOperator, ResumePolicy, SignalContract


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
class ContractUpdate:
    """Explicit assertion describing one processing step's output contract.

    Unspecified fields are preserved from the input contract. ``drop_fields``
    removes constraints that no longer describe the transformed record before
    the asserted values are applied.
    """

    type_id: str | None = None
    unit: str | None = None
    nominal_rate_hz: float | None = None
    content_kind: str | None = None
    rate_model: str | None = None
    channel_ids: tuple[str, ...] | None = None
    feature_ids: tuple[str, ...] | None = None
    units: Mapping[str, str] | None = None
    missing_data_policy: str | None = None
    layout: str | None = None
    drop_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        allowed = set(SignalContract.__dataclass_fields__) - {"type_id"}
        drops = tuple(require_identifier(value, "contract drop field") for value in self.drop_fields)
        unknown = set(drops) - allowed
        if unknown:
            raise ValueError(f"unknown contract fields to drop: {sorted(unknown)}")
        if len(drops) != len(set(drops)):
            raise ValueError("contract drop fields must be unique")
        object.__setattr__(self, "drop_fields", drops)
        for name in (
            "type_id",
            "content_kind",
            "rate_model",
            "missing_data_policy",
            "layout",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, require_identifier(value, name))
        if self.unit is not None and not self.unit.strip():
            raise ValueError("contract unit cannot be empty")
        if self.nominal_rate_hz is not None:
            rate = require_finite(self.nominal_rate_hz, "nominal_rate_hz")
            if rate <= 0:
                raise ValueError("nominal_rate_hz must be positive")
            object.__setattr__(self, "nominal_rate_hz", rate)
        for name in ("channel_ids", "feature_ids"):
            value = getattr(self, name)
            if value is not None:
                normalized = tuple(require_identifier(item, name) for item in value)
                if len(normalized) != len(set(normalized)):
                    raise ValueError(f"{name} entries must be unique")
                object.__setattr__(self, name, normalized)
        if self.units is not None:
            units = {
                require_identifier(str(key), "contract unit key"): str(value)
                for key, value in self.units.items()
            }
            if any(not value.strip() for value in units.values()):
                raise ValueError("contract units cannot be empty")
            object.__setattr__(self, "units", freeze_json(units))

    def apply(self, input_contract: SignalContract) -> SignalContract:
        if not isinstance(input_contract, SignalContract):
            raise TypeError("contract updates require a SignalContract input")
        payload = input_contract.to_payload()
        for name in self.drop_fields:
            payload.pop(name, None)
        for name in (
            "type_id",
            "unit",
            "nominal_rate_hz",
            "content_kind",
            "rate_model",
            "channel_ids",
            "feature_ids",
            "units",
            "missing_data_policy",
            "layout",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = thaw_json(value) if isinstance(value, Mapping) else value
        if self.channel_ids is not None:
            payload["channel_count"] = len(self.channel_ids)
        return SignalContract.from_payload(payload)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for name in (
            "type_id",
            "unit",
            "nominal_rate_hz",
            "content_kind",
            "rate_model",
            "channel_ids",
            "feature_ids",
            "units",
            "missing_data_policy",
            "layout",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = thaw_json(value) if isinstance(value, Mapping) else value
        payload["drop_fields"] = list(self.drop_fields)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ContractUpdate":
        _reject_unknown(payload, _field_names(cls), "contract update")
        return cls(
            type_id=_optional_string(payload.get("type_id")),
            unit=_optional_string(payload.get("unit")),
            nominal_rate_hz=_optional_float(payload.get("nominal_rate_hz")),
            content_kind=_optional_string(payload.get("content_kind")),
            rate_model=_optional_string(payload.get("rate_model")),
            channel_ids=None
            if payload.get("channel_ids") is None
            else tuple(str(value) for value in payload["channel_ids"]),
            feature_ids=None
            if payload.get("feature_ids") is None
            else tuple(str(value) for value in payload["feature_ids"]),
            units=None
            if payload.get("units") is None
            else {str(key): str(value) for key, value in dict(payload["units"]).items()},
            missing_data_policy=_optional_string(payload.get("missing_data_policy")),
            layout=_optional_string(payload.get("layout")),
            drop_fields=tuple(str(value) for value in payload.get("drop_fields", ())),
        )


@dataclass(frozen=True, slots=True)
class ProcessingStep:
    step_id: str
    plugin_id: str
    output_contract: ContractUpdate
    config: Mapping[str, Any] = field(default_factory=dict)
    version_spec: str = "~=0.1.0"
    input_port: str = "samples"
    output_port: str = "samples"

    def __post_init__(self) -> None:
        for name in ("step_id", "plugin_id", "input_port", "output_port"):
            object.__setattr__(self, name, require_identifier(getattr(self, name), name))
        if not self.version_spec.strip():
            raise ValueError("processing version_spec cannot be empty")
        if not isinstance(self.output_contract, ContractUpdate):
            raise TypeError("processing steps require an explicit ContractUpdate")
        object.__setattr__(self, "config", freeze_json(self.config or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "plugin_id": self.plugin_id,
            "output_contract": self.output_contract.to_payload(),
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
            output_contract=ContractUpdate.from_payload(payload["output_contract"]),
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
    active: tuple[str, ...] = ()
    goals: tuple[str, ...] = ()
    include_dependencies: bool = True
    exclude: tuple[str, ...] = ()
    next_phases: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    retry_limit: int = 0
    resume_policy: ResumePolicy = ResumePolicy.CHECKPOINT
    operator_confirmation: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase_id", require_identifier(self.phase_id, "phase_id"))
        active = tuple(_semantic_reference(value, "phase active reference") for value in self.active)
        goals = tuple(_semantic_reference(value, "phase goal reference") for value in self.goals)
        if bool(active) == bool(goals):
            raise ValueError("phase must declare exactly one of active or goals")
        if len(active) != len(set(active)):
            raise ValueError("phase active references must be unique")
        if len(goals) != len(set(goals)):
            raise ValueError("phase goal references must be unique")
        object.__setattr__(self, "active", active)
        object.__setattr__(self, "goals", goals)
        excluded = tuple(_semantic_reference(value, "phase exclusion") for value in self.exclude)
        if len(excluded) != len(set(excluded)):
            raise ValueError("phase exclusions must be unique")
        object.__setattr__(self, "exclude", excluded)
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
            "goals": list(self.goals),
            "include_dependencies": self.include_dependencies,
            "exclude": list(self.exclude),
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
            active=tuple(str(v) for v in payload.get("active", ())),
            goals=tuple(str(v) for v in payload.get("goals", ())),
            include_dependencies=bool(payload.get("include_dependencies", True)),
            exclude=tuple(str(v) for v in payload.get("exclude", ())),
            next_phases=tuple(str(v) for v in payload.get("next_phases", ())),
            timeout_seconds=_optional_float(payload.get("timeout_seconds")),
            retry_limit=int(payload.get("retry_limit", 0)),
            resume_policy=ResumePolicy(str(payload.get("resume_policy", "checkpoint"))),
            operator_confirmation=bool(payload.get("operator_confirmation", False)),
        )


@dataclass(frozen=True, slots=True)
class RecordingDeclaration:
    inputs: tuple[str, ...] = ()
    phases: tuple[str, ...] = ()
    execution_capture: bool = True
    semantic_evidence: bool = True
    raw_recording: str = "reference"

    def __post_init__(self) -> None:
        inputs = tuple(_semantic_reference(value, "recording input") for value in self.inputs)
        if len(inputs) != len(set(inputs)):
            raise ValueError("recording inputs must be unique")
        object.__setattr__(self, "inputs", inputs)
        phases = tuple(_prefixed_reference(value, "phase", "recording phase") for value in self.phases)
        if len(phases) != len(set(phases)):
            raise ValueError("recording phases must be unique")
        object.__setattr__(self, "phases", phases)
        object.__setattr__(self, "raw_recording", require_identifier(self.raw_recording, "raw_recording"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "inputs": list(self.inputs),
            "phases": list(self.phases),
            "execution_capture": self.execution_capture,
            "semantic_evidence": self.semantic_evidence,
            "raw_recording": self.raw_recording,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RecordingDeclaration":
        _reject_unknown(payload, _field_names(cls), "recording")
        return cls(
            inputs=tuple(str(v) for v in payload.get("inputs", ())),
            phases=tuple(str(v) for v in payload.get("phases", ())),
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


__all__ = [
    "AcceptanceDeclaration",
    "ActionDeclaration",
    "AdaptationDeclaration",
    "CalibrationDeclaration",
    "ComparisonGroup",
    "ContractUpdate",
    "EventDeclaration",
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
]
