"""Portable system and composition specifications.

The suite vocabulary is intentionally bounded to neurophysiological execution
semantics.  It is a typed component graph with phases, not a general workflow
language and not a container for arbitrary Python.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from eegle._domain import ComponentKind
from eegle._validation import (
    freeze_json,
    require_digest,
    require_finite,
    require_identifier,
    thaw_json,
)
from eegle.compiler.lock import canonical_hash
from eegle.specs.schemas import validate_payload


SUITE_SPEC_SCHEMA = "eegle.suite_spec.v1"
SUITE_OVERLAY_SCHEMA = "eegle.suite_overlay.v1"
_JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"


@dataclass(frozen=True, slots=True)
class SignalContract:
    """Machine-checkable requirements for one component port."""

    type_id: str
    unit: str | None = None
    channel_count: int | None = None
    minimum_channels: int | None = None
    maximum_channels: int | None = None
    nominal_rate_hz: float | None = None
    minimum_rate_hz: float | None = None
    maximum_rate_hz: float | None = None
    window_samples: int | None = None
    minimum_window_samples: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "type_id", require_identifier(self.type_id, "type_id"))
        if self.unit is not None and not self.unit.strip():
            raise ValueError("signal unit cannot be empty")
        for field in (
            "channel_count",
            "minimum_channels",
            "maximum_channels",
            "window_samples",
            "minimum_window_samples",
        ):
            value = getattr(self, field)
            if value is not None:
                normalized = int(value)
                if normalized <= 0:
                    raise ValueError(f"{field} must be positive")
                object.__setattr__(self, field, normalized)
        for field in ("nominal_rate_hz", "minimum_rate_hz", "maximum_rate_hz"):
            value = getattr(self, field)
            if value is not None:
                normalized = require_finite(value, field)
                if normalized <= 0:
                    raise ValueError(f"{field} must be positive")
                object.__setattr__(self, field, normalized)
        if (
            self.minimum_channels is not None
            and self.maximum_channels is not None
            and self.minimum_channels > self.maximum_channels
        ):
            raise ValueError("minimum_channels cannot exceed maximum_channels")
        if self.channel_count is not None and self.minimum_channels is not None:
            if self.channel_count < self.minimum_channels:
                raise ValueError("channel_count cannot be below minimum_channels")
        if self.channel_count is not None and self.maximum_channels is not None:
            if self.channel_count > self.maximum_channels:
                raise ValueError("channel_count cannot exceed maximum_channels")
        if (
            self.minimum_rate_hz is not None
            and self.maximum_rate_hz is not None
            and self.minimum_rate_hz > self.maximum_rate_hz
        ):
            raise ValueError("minimum_rate_hz cannot exceed maximum_rate_hz")
        if self.nominal_rate_hz is not None and self.minimum_rate_hz is not None:
            if self.nominal_rate_hz < self.minimum_rate_hz:
                raise ValueError("nominal_rate_hz cannot be below minimum_rate_hz")
        if self.nominal_rate_hz is not None and self.maximum_rate_hz is not None:
            if self.nominal_rate_hz > self.maximum_rate_hz:
                raise ValueError("nominal_rate_hz cannot exceed maximum_rate_hz")
        if self.window_samples is not None and self.minimum_window_samples is not None:
            if self.window_samples < self.minimum_window_samples:
                raise ValueError("window_samples cannot be below minimum_window_samples")

    def to_payload(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "type_id": self.type_id,
                "unit": self.unit,
                "channel_count": self.channel_count,
                "minimum_channels": self.minimum_channels,
                "maximum_channels": self.maximum_channels,
                "nominal_rate_hz": self.nominal_rate_hz,
                "minimum_rate_hz": self.minimum_rate_hz,
                "maximum_rate_hz": self.maximum_rate_hz,
                "window_samples": self.window_samples,
                "minimum_window_samples": self.minimum_window_samples,
            }.items()
            if value is not None
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SignalContract":
        return cls(**dict(payload))


@dataclass(frozen=True, slots=True)
class LogicalStreamSpec:
    stream_id: str
    contract: SignalContract
    modality: str | None = None
    clock_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stream_id", require_identifier(self.stream_id, "stream_id"))
        if self.modality is not None:
            object.__setattr__(self, "modality", require_identifier(self.modality, "modality"))
        if self.clock_id is not None:
            object.__setattr__(self, "clock_id", require_identifier(self.clock_id, "clock_id"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "contract": self.contract.to_payload(),
            "modality": self.modality,
            "clock_id": self.clock_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LogicalStreamSpec":
        return cls(
            stream_id=str(payload["stream_id"]),
            contract=SignalContract.from_payload(payload["contract"]),
            modality=None if payload.get("modality") is None else str(payload["modality"]),
            clock_id=None if payload.get("clock_id") is None else str(payload["clock_id"]),
        )


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    component_id: str
    kind: ComponentKind
    plugin_id: str | None = None
    version_spec: str | None = None
    config: Mapping[str, Any] = None  # type: ignore[assignment]
    role: str | None = None
    stream_id: str | None = None
    required_capabilities: tuple[str, ...] = ()
    input_contracts: Mapping[str, SignalContract] = None  # type: ignore[assignment]
    output_contracts: Mapping[str, SignalContract] = None  # type: ignore[assignment]
    outcome_uses: tuple[str, ...] = ()
    required_outcome_use: str | None = None
    action_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component_id", require_identifier(self.component_id, "component_id")
        )
        object.__setattr__(self, "kind", ComponentKind(self.kind))
        if self.plugin_id is not None:
            object.__setattr__(
                self, "plugin_id", require_identifier(self.plugin_id, "plugin_id")
            )
        if self.version_spec is not None and not self.version_spec.strip():
            raise ValueError("version_spec cannot be empty")
        if self.role is not None:
            object.__setattr__(self, "role", require_identifier(self.role, "role"))
        if self.stream_id is not None:
            object.__setattr__(
                self, "stream_id", require_identifier(self.stream_id, "stream_id")
            )
        object.__setattr__(self, "config", freeze_json(self.config or {}))
        capabilities = tuple(
            require_identifier(value, "required capability")
            for value in self.required_capabilities
        )
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("required capabilities must be unique")
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(
            self,
            "input_contracts",
            _freeze_contracts(self.input_contracts or {}, "input contract"),
        )
        object.__setattr__(
            self,
            "output_contracts",
            _freeze_contracts(self.output_contracts or {}, "output contract"),
        )
        uses = tuple(require_identifier(value, "outcome use") for value in self.outcome_uses)
        if len(uses) != len(set(uses)):
            raise ValueError("outcome uses must be unique")
        object.__setattr__(self, "outcome_uses", uses)
        if self.required_outcome_use is not None:
            object.__setattr__(
                self,
                "required_outcome_use",
                require_identifier(self.required_outcome_use, "required outcome use"),
            )
        capabilities = tuple(
            require_identifier(value, "action capability")
            for value in self.action_capabilities
        )
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("action capabilities must be unique")
        object.__setattr__(self, "action_capabilities", capabilities)

    def to_payload(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "kind": self.kind.value,
            "plugin_id": self.plugin_id,
            "version_spec": self.version_spec,
            "config": thaw_json(self.config),
            "role": self.role,
            "stream_id": self.stream_id,
            "required_capabilities": list(self.required_capabilities),
            "input_contracts": {
                key: value.to_payload() for key, value in self.input_contracts.items()
            },
            "output_contracts": {
                key: value.to_payload() for key, value in self.output_contracts.items()
            },
            "outcome_uses": list(self.outcome_uses),
            "required_outcome_use": self.required_outcome_use,
            "action_capabilities": list(self.action_capabilities),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ComponentSpec":
        return cls(
            component_id=str(payload["component_id"]),
            kind=ComponentKind(str(payload["kind"])),
            plugin_id=None if payload.get("plugin_id") is None else str(payload["plugin_id"]),
            version_spec=None
            if payload.get("version_spec") is None
            else str(payload["version_spec"]),
            config=dict(payload.get("config") or {}),
            role=None if payload.get("role") is None else str(payload["role"]),
            stream_id=None
            if payload.get("stream_id") is None
            else str(payload["stream_id"]),
            required_capabilities=tuple(
                str(value) for value in payload.get("required_capabilities", ())
            ),
            input_contracts={
                str(key): SignalContract.from_payload(value)
                for key, value in dict(payload.get("input_contracts") or {}).items()
            },
            output_contracts={
                str(key): SignalContract.from_payload(value)
                for key, value in dict(payload.get("output_contracts") or {}).items()
            },
            outcome_uses=tuple(str(value) for value in payload.get("outcome_uses", ())),
            required_outcome_use=None
            if payload.get("required_outcome_use") is None
            else str(payload["required_outcome_use"]),
            action_capabilities=tuple(
                str(value) for value in payload.get("action_capabilities", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class RouteSpec:
    route_id: str
    source_component: str
    source_port: str
    target_component: str
    target_port: str

    def __post_init__(self) -> None:
        for field in (
            "route_id",
            "source_component",
            "source_port",
            "target_component",
            "target_port",
        ):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        if self.source_component == self.target_component:
            raise ValueError("a component route cannot target itself")

    def to_payload(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "source": {"component": self.source_component, "port": self.source_port},
            "target": {"component": self.target_component, "port": self.target_port},
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RouteSpec":
        source = payload["source"]
        target = payload["target"]
        return cls(
            route_id=str(payload["route_id"]),
            source_component=str(source["component"]),
            source_port=str(source["port"]),
            target_component=str(target["component"]),
            target_port=str(target["port"]),
        )


class TransitionCondition(str, Enum):
    COMPLETE = "complete"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ACCEPTANCE_FAILED = "acceptance_failed"
    OPERATOR = "operator"


class ResumePolicy(str, Enum):
    RESTART = "restart"
    CHECKPOINT = "checkpoint"
    FORBIDDEN = "forbidden"


class BackpressureDisposition(str, Enum):
    FAIL_RUN = "fail_run"
    REJECT_NEWEST = "reject_newest"


class ShadowFailurePolicy(str, Enum):
    FAIL_RUN = "fail_run"
    CONTINUE = "continue"


@dataclass(frozen=True, slots=True)
class SchedulingSpec:
    backpressure: BackpressureDisposition = BackpressureDisposition.FAIL_RUN
    primary_first: bool = True
    shadow_queue_limit: int | None = None
    shadow_failure: ShadowFailurePolicy = ShadowFailurePolicy.FAIL_RUN

    def __post_init__(self) -> None:
        object.__setattr__(self, "backpressure", BackpressureDisposition(self.backpressure))
        object.__setattr__(self, "shadow_failure", ShadowFailurePolicy(self.shadow_failure))
        if self.shadow_queue_limit is not None:
            object.__setattr__(self, "shadow_queue_limit", int(self.shadow_queue_limit))
            if self.shadow_queue_limit < 0:
                raise ValueError("shadow_queue_limit cannot be negative")

    def to_payload(self) -> dict[str, Any]:
        return {
            "backpressure": self.backpressure.value,
            "primary_first": self.primary_first,
            "shadow_queue_limit": self.shadow_queue_limit,
            "shadow_failure": self.shadow_failure.value,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SchedulingSpec":
        return cls(
            backpressure=BackpressureDisposition(
                str(payload.get("backpressure", BackpressureDisposition.FAIL_RUN.value))
            ),
            primary_first=bool(payload.get("primary_first", True)),
            shadow_queue_limit=None
            if payload.get("shadow_queue_limit") is None
            else int(payload["shadow_queue_limit"]),
            shadow_failure=ShadowFailurePolicy(
                str(payload.get("shadow_failure", ShadowFailurePolicy.FAIL_RUN.value))
            ),
        )


@dataclass(frozen=True, slots=True)
class ScheduledTriggerSpec:
    trigger_id: str
    phase_id: str
    target_component: str
    scheduled_offset_seconds: float
    payload: Mapping[str, Any] = None  # type: ignore[assignment]
    deadline_offset_seconds: float | None = None

    def __post_init__(self) -> None:
        for field in ("trigger_id", "phase_id", "target_component"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        scheduled = require_finite(
            self.scheduled_offset_seconds, "scheduled_offset_seconds"
        )
        if scheduled < 0:
            raise ValueError("scheduled_offset_seconds cannot be negative")
        object.__setattr__(self, "scheduled_offset_seconds", scheduled)
        if self.deadline_offset_seconds is not None:
            deadline = require_finite(
                self.deadline_offset_seconds, "deadline_offset_seconds"
            )
            if deadline < scheduled:
                raise ValueError("trigger deadline cannot precede scheduled offset")
            object.__setattr__(self, "deadline_offset_seconds", deadline)
        object.__setattr__(self, "payload", freeze_json(self.payload or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "phase_id": self.phase_id,
            "target_component": self.target_component,
            "scheduled_offset_seconds": self.scheduled_offset_seconds,
            "deadline_offset_seconds": self.deadline_offset_seconds,
            "payload": thaw_json(self.payload),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ScheduledTriggerSpec":
        return cls(
            trigger_id=str(payload["trigger_id"]),
            phase_id=str(payload["phase_id"]),
            target_component=str(payload["target_component"]),
            scheduled_offset_seconds=float(payload["scheduled_offset_seconds"]),
            deadline_offset_seconds=None
            if payload.get("deadline_offset_seconds") is None
            else float(payload["deadline_offset_seconds"]),
            payload=dict(payload.get("payload") or {}),
        )


@dataclass(frozen=True, slots=True)
class StateTriggerSpec:
    rule_id: str
    phase_id: str
    source_component: str
    target_component: str
    transition_kind: str | None = None
    statuses: tuple[str, ...] = ("applied",)
    delay_seconds: float = 0.0
    payload: Mapping[str, Any] = None  # type: ignore[assignment]
    once: bool = True

    def __post_init__(self) -> None:
        for field in ("rule_id", "phase_id", "source_component", "target_component"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        if self.transition_kind is not None:
            object.__setattr__(
                self,
                "transition_kind",
                require_identifier(self.transition_kind, "transition_kind"),
            )
        statuses = tuple(require_identifier(value, "transition status") for value in self.statuses)
        if not statuses or len(statuses) != len(set(statuses)):
            raise ValueError("state trigger statuses must be non-empty and unique")
        object.__setattr__(self, "statuses", statuses)
        delay = require_finite(self.delay_seconds, "delay_seconds")
        if delay < 0:
            raise ValueError("state trigger delay cannot be negative")
        object.__setattr__(self, "delay_seconds", delay)
        object.__setattr__(self, "payload", freeze_json(self.payload or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "phase_id": self.phase_id,
            "source_component": self.source_component,
            "target_component": self.target_component,
            "transition_kind": self.transition_kind,
            "statuses": list(self.statuses),
            "delay_seconds": self.delay_seconds,
            "payload": thaw_json(self.payload),
            "once": self.once,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StateTriggerSpec":
        return cls(
            rule_id=str(payload["rule_id"]),
            phase_id=str(payload["phase_id"]),
            source_component=str(payload["source_component"]),
            target_component=str(payload["target_component"]),
            transition_kind=None
            if payload.get("transition_kind") is None
            else str(payload["transition_kind"]),
            statuses=tuple(str(value) for value in payload.get("statuses", ("applied",))),
            delay_seconds=float(payload.get("delay_seconds", 0.0)),
            payload=dict(payload.get("payload") or {}),
            once=bool(payload.get("once", True)),
        )


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    target_phase: str
    condition: TransitionCondition = TransitionCondition.COMPLETE

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "target_phase", require_identifier(self.target_phase, "target_phase")
        )
        object.__setattr__(self, "condition", TransitionCondition(self.condition))

    def to_payload(self) -> dict[str, Any]:
        return {"target_phase": self.target_phase, "condition": self.condition.value}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PhaseTransition":
        return cls(
            target_phase=str(payload["target_phase"]),
            condition=TransitionCondition(str(payload.get("condition", "complete"))),
        )


@dataclass(frozen=True, slots=True)
class PhaseSpec:
    phase_id: str
    components: tuple[str, ...]
    transitions: tuple[PhaseTransition, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    retry_limit: int = 0
    resume_policy: ResumePolicy = ResumePolicy.RESTART
    operator_confirmation: bool = False
    timeout_seconds: float | None = None
    acceptance_criteria: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase_id", require_identifier(self.phase_id, "phase_id"))
        components = tuple(require_identifier(value, "phase component") for value in self.components)
        if not components:
            raise ValueError("phase must activate at least one component")
        if len(components) != len(set(components)):
            raise ValueError("phase component identities must be unique")
        object.__setattr__(self, "components", components)
        artifacts = tuple(
            require_identifier(value, "required artifact") for value in self.required_artifacts
        )
        if len(artifacts) != len(set(artifacts)):
            raise ValueError("required artifact identities must be unique")
        object.__setattr__(self, "required_artifacts", artifacts)
        object.__setattr__(self, "retry_limit", int(self.retry_limit))
        if self.retry_limit < 0:
            raise ValueError("retry_limit cannot be negative")
        object.__setattr__(self, "resume_policy", ResumePolicy(self.resume_policy))
        if self.timeout_seconds is not None:
            timeout = require_finite(self.timeout_seconds, "timeout_seconds")
            if timeout <= 0:
                raise ValueError("timeout_seconds must be positive")
            object.__setattr__(self, "timeout_seconds", timeout)
        criteria = tuple(
            require_identifier(value, "acceptance criterion")
            for value in self.acceptance_criteria
        )
        if len(criteria) != len(set(criteria)):
            raise ValueError("phase acceptance criteria must be unique")
        object.__setattr__(self, "acceptance_criteria", criteria)

    def to_payload(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "components": list(self.components),
            "transitions": [value.to_payload() for value in self.transitions],
            "required_artifacts": list(self.required_artifacts),
            "retry_limit": self.retry_limit,
            "resume_policy": self.resume_policy.value,
            "operator_confirmation": self.operator_confirmation,
            "timeout_seconds": self.timeout_seconds,
            "acceptance_criteria": list(self.acceptance_criteria),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PhaseSpec":
        return cls(
            phase_id=str(payload["phase_id"]),
            components=tuple(str(value) for value in payload["components"]),
            transitions=tuple(
                PhaseTransition.from_payload(value)
                for value in payload.get("transitions", ())
            ),
            required_artifacts=tuple(
                str(value) for value in payload.get("required_artifacts", ())
            ),
            retry_limit=int(payload.get("retry_limit", 0)),
            resume_policy=ResumePolicy(str(payload.get("resume_policy", "restart"))),
            operator_confirmation=bool(payload.get("operator_confirmation", False)),
            timeout_seconds=None
            if payload.get("timeout_seconds") is None
            else float(payload["timeout_seconds"]),
            acceptance_criteria=tuple(
                str(value) for value in payload.get("acceptance_criteria", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    """Portable declaration of an external or graph-produced artifact."""

    artifact_id: str
    role: str
    media_type: str
    producer_phase: str | None = None
    producer_component: str | None = None
    producer_port: str | None = None
    expected_digest: str | None = None

    def __post_init__(self) -> None:
        for field in ("artifact_id", "role"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        if not self.media_type.strip():
            raise ValueError("artifact media_type cannot be empty")
        producer = (
            self.producer_phase,
            self.producer_component,
            self.producer_port,
        )
        if any(value is not None for value in producer) and not all(
            value is not None for value in producer
        ):
            raise ValueError(
                "artifact producer_phase, producer_component, and producer_port "
                "must be declared together"
            )
        for field in ("producer_phase", "producer_component", "producer_port"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, require_identifier(value, field))
        if self.expected_digest is not None:
            object.__setattr__(
                self,
                "expected_digest",
                require_digest(self.expected_digest, "expected_digest"),
            )

    @property
    def external(self) -> bool:
        return self.producer_component is None

    def to_payload(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "role": self.role,
            "media_type": self.media_type,
            "producer_phase": self.producer_phase,
            "producer_component": self.producer_component,
            "producer_port": self.producer_port,
            "expected_digest": self.expected_digest,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArtifactSpec":
        return cls(
            artifact_id=str(payload["artifact_id"]),
            role=str(payload["role"]),
            media_type=str(payload["media_type"]),
            producer_phase=None
            if payload.get("producer_phase") is None
            else str(payload["producer_phase"]),
            producer_component=None
            if payload.get("producer_component") is None
            else str(payload["producer_component"]),
            producer_port=None
            if payload.get("producer_port") is None
            else str(payload["producer_port"]),
            expected_digest=None
            if payload.get("expected_digest") is None
            else str(payload["expected_digest"]),
        )


@dataclass(frozen=True, slots=True)
class SuiteSpec:
    suite_id: str
    protocol_id: str
    streams: tuple[LogicalStreamSpec, ...]
    components: tuple[ComponentSpec, ...]
    routes: tuple[RouteSpec, ...]
    phases: tuple[PhaseSpec, ...]
    initial_phase: str
    clock_policy: Mapping[str, Any]
    recording: Mapping[str, Any]
    validation: Mapping[str, Any]
    artifacts: tuple[ArtifactSpec, ...] = ()
    scheduling: SchedulingSpec = SchedulingSpec()
    scheduled_triggers: tuple[ScheduledTriggerSpec, ...] = ()
    state_triggers: tuple[StateTriggerSpec, ...] = ()
    schema: str = SUITE_SPEC_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SUITE_SPEC_SCHEMA:
            raise ValueError(f"unsupported suite schema: {self.schema}")
        object.__setattr__(self, "suite_id", require_identifier(self.suite_id, "suite_id"))
        object.__setattr__(
            self, "protocol_id", require_identifier(self.protocol_id, "protocol_id")
        )
        _require_unique((value.stream_id for value in self.streams), "stream")
        _require_unique((value.component_id for value in self.components), "component")
        _require_unique((value.route_id for value in self.routes), "route")
        _require_unique((value.phase_id for value in self.phases), "phase")
        _require_unique((value.artifact_id for value in self.artifacts), "artifact")
        _require_unique(
            (value.trigger_id for value in self.scheduled_triggers),
            "scheduled trigger",
        )
        _require_unique((value.rule_id for value in self.state_triggers), "state trigger")
        object.__setattr__(
            self, "initial_phase", require_identifier(self.initial_phase, "initial_phase")
        )
        if self.initial_phase not in {value.phase_id for value in self.phases}:
            raise ValueError("initial_phase must reference a declared phase")
        object.__setattr__(self, "clock_policy", freeze_json(self.clock_policy))
        object.__setattr__(self, "recording", freeze_json(self.recording))
        object.__setattr__(self, "validation", freeze_json(self.validation))

    @property
    def spec_hash(self) -> str:
        return canonical_hash(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "suite_id": self.suite_id,
            "protocol_id": self.protocol_id,
            "streams": [value.to_payload() for value in self.streams],
            "components": [value.to_payload() for value in self.components],
            "routes": [value.to_payload() for value in self.routes],
            "phases": [value.to_payload() for value in self.phases],
            "initial_phase": self.initial_phase,
            "artifacts": [value.to_payload() for value in self.artifacts],
            "scheduling": self.scheduling.to_payload(),
            "scheduled_triggers": [
                value.to_payload() for value in self.scheduled_triggers
            ],
            "state_triggers": [value.to_payload() for value in self.state_triggers],
            "clock_policy": thaw_json(self.clock_policy),
            "recording": thaw_json(self.recording),
            "validation": thaw_json(self.validation),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SuiteSpec":
        validate_payload(payload, SUITE_JSON_SCHEMA)
        return cls(
            schema=str(payload.get("schema", SUITE_SPEC_SCHEMA)),
            suite_id=str(payload["suite_id"]),
            protocol_id=str(payload["protocol_id"]),
            streams=tuple(
                LogicalStreamSpec.from_payload(value) for value in payload.get("streams", ())
            ),
            components=tuple(ComponentSpec.from_payload(value) for value in payload["components"]),
            routes=tuple(RouteSpec.from_payload(value) for value in payload["routes"]),
            phases=tuple(PhaseSpec.from_payload(value) for value in payload["phases"]),
            initial_phase=str(payload["initial_phase"]),
            artifacts=tuple(
                ArtifactSpec.from_payload(value) for value in payload.get("artifacts", ())
            ),
            scheduling=SchedulingSpec.from_payload(payload.get("scheduling") or {}),
            scheduled_triggers=tuple(
                ScheduledTriggerSpec.from_payload(value)
                for value in payload.get("scheduled_triggers", ())
            ),
            state_triggers=tuple(
                StateTriggerSpec.from_payload(value)
                for value in payload.get("state_triggers", ())
            ),
            clock_policy=dict(payload["clock_policy"]),
            recording=dict(payload["recording"]),
            validation=dict(payload["validation"]),
        )

    @classmethod
    def load(cls, path: str | Path) -> "SuiteSpec":
        return cls.from_payload(_load_json_object(path))


@dataclass(frozen=True, slots=True)
class SuiteOverlay:
    """A constrained, ordered override of an existing suite.

    Overlays cannot add/remove graph nodes or alter phases and routes.  They may
    only override configuration of named components and keys in recording or
    validation policy.  This is the complete v1 composition model.
    """

    overlay_id: str
    component_config: Mapping[str, Mapping[str, Any]] = None  # type: ignore[assignment]
    recording: Mapping[str, Any] = None  # type: ignore[assignment]
    validation: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = SUITE_OVERLAY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SUITE_OVERLAY_SCHEMA:
            raise ValueError(f"unsupported suite overlay schema: {self.schema}")
        object.__setattr__(self, "overlay_id", require_identifier(self.overlay_id, "overlay_id"))
        object.__setattr__(self, "component_config", freeze_json(self.component_config or {}))
        object.__setattr__(self, "recording", freeze_json(self.recording or {}))
        object.__setattr__(self, "validation", freeze_json(self.validation or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "overlay_id": self.overlay_id,
            "component_config": thaw_json(self.component_config),
            "recording": thaw_json(self.recording),
            "validation": thaw_json(self.validation),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SuiteOverlay":
        validate_payload(payload, SUITE_OVERLAY_JSON_SCHEMA)
        return cls(
            schema=str(payload.get("schema", SUITE_OVERLAY_SCHEMA)),
            overlay_id=str(payload["overlay_id"]),
            component_config=dict(payload.get("component_config") or {}),
            recording=dict(payload.get("recording") or {}),
            validation=dict(payload.get("validation") or {}),
        )


def compose_suite(base: SuiteSpec, overlays: Iterable[SuiteOverlay]) -> SuiteSpec:
    """Apply ordered, typed overlays without mutating scientific topology."""

    components = {value.component_id: value for value in base.components}
    recording = thaw_json(base.recording)
    validation = thaw_json(base.validation)
    seen: set[str] = set()
    for overlay in overlays:
        if overlay.overlay_id in seen:
            raise ValueError(f"duplicate overlay identity: {overlay.overlay_id}")
        seen.add(overlay.overlay_id)
        for component_id, config in overlay.component_config.items():
            if component_id not in components:
                raise ValueError(
                    f"overlay {overlay.overlay_id} references unknown component {component_id}"
                )
            existing = components[component_id]
            components[component_id] = replace(
                existing,
                config=_merge_object(thaw_json(existing.config), thaw_json(config)),
            )
        recording = _merge_object(recording, thaw_json(overlay.recording))
        validation = _merge_object(validation, thaw_json(overlay.validation))
    return replace(
        base,
        components=tuple(components[value.component_id] for value in base.components),
        recording=recording,
        validation=validation,
    )


def _merge_object(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = {str(key): thaw_json(value) for key, value in base.items()}
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _merge_object(current, value)
        else:
            result[key] = thaw_json(value)
    return result


def _freeze_contracts(
    contracts: Mapping[str, SignalContract], label: str
) -> Mapping[str, SignalContract]:
    normalized: dict[str, SignalContract] = {}
    for key, value in contracts.items():
        port = require_identifier(str(key), label)
        if not isinstance(value, SignalContract):
            raise TypeError(f"{label} {port} must be a SignalContract")
        normalized[port] = value
    return MappingProxyType(normalized)


def _require_unique(values: Iterable[str], label: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{label} identities must be unique")


def _load_json_object(path: str | Path) -> Mapping[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must contain a JSON object")
    return value


_SIGNAL_CONTRACT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["type_id"],
    "properties": {
        "type_id": {"type": "string", "minLength": 1},
        "unit": {"type": "string", "minLength": 1},
        "channel_count": {"type": "integer", "minimum": 1},
        "minimum_channels": {"type": "integer", "minimum": 1},
        "maximum_channels": {"type": "integer", "minimum": 1},
        "nominal_rate_hz": {"type": "number", "exclusiveMinimum": 0},
        "minimum_rate_hz": {"type": "number", "exclusiveMinimum": 0},
        "maximum_rate_hz": {"type": "number", "exclusiveMinimum": 0},
        "window_samples": {"type": "integer", "minimum": 1},
        "minimum_window_samples": {"type": "integer", "minimum": 1},
    },
    "additionalProperties": False,
}
_COMPONENT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["component_id", "kind"],
    "properties": {
        "component_id": {"type": "string", "minLength": 1},
        "kind": {"enum": [value.value for value in ComponentKind]},
        "plugin_id": {"type": ["string", "null"], "minLength": 1},
        "version_spec": {"type": ["string", "null"], "minLength": 1},
        "config": {"type": "object"},
        "role": {"type": ["string", "null"], "minLength": 1},
        "stream_id": {"type": ["string", "null"], "minLength": 1},
        "required_capabilities": {"type": "array", "items": {"type": "string"}},
        "input_contracts": {
            "type": "object",
            "additionalProperties": _SIGNAL_CONTRACT_SCHEMA,
        },
        "output_contracts": {
            "type": "object",
            "additionalProperties": _SIGNAL_CONTRACT_SCHEMA,
        },
        "outcome_uses": {"type": "array", "items": {"type": "string"}},
        "required_outcome_use": {"type": ["string", "null"], "minLength": 1},
        "action_capabilities": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}
SUITE_JSON_SCHEMA: Mapping[str, Any] = {
    "$schema": _JSON_SCHEMA,
    "type": "object",
    "required": [
        "schema",
        "suite_id",
        "protocol_id",
        "components",
        "routes",
        "phases",
        "initial_phase",
        "clock_policy",
        "recording",
        "validation",
    ],
    "properties": {
        "schema": {"const": SUITE_SPEC_SCHEMA},
        "suite_id": {"type": "string", "minLength": 1},
        "protocol_id": {"type": "string", "minLength": 1},
        "streams": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["stream_id", "contract"],
                "properties": {
                    "stream_id": {"type": "string", "minLength": 1},
                    "contract": _SIGNAL_CONTRACT_SCHEMA,
                    "modality": {"type": ["string", "null"]},
                    "clock_id": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        },
        "components": {"type": "array", "minItems": 1, "items": _COMPONENT_SCHEMA},
        "routes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["route_id", "source", "target"],
                "properties": {
                    "route_id": {"type": "string", "minLength": 1},
                    "source": {
                        "type": "object",
                        "required": ["component", "port"],
                        "properties": {
                            "component": {"type": "string"},
                            "port": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                    "target": {
                        "type": "object",
                        "required": ["component", "port"],
                        "properties": {
                            "component": {"type": "string"},
                            "port": {"type": "string"},
                        },
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": False,
            },
        },
        "phases": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["phase_id", "components"],
                "properties": {
                    "phase_id": {"type": "string"},
                    "components": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    "transitions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["target_phase"],
                            "properties": {
                                "target_phase": {"type": "string"},
                                "condition": {"enum": [value.value for value in TransitionCondition]},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "required_artifacts": {"type": "array", "items": {"type": "string"}},
                    "retry_limit": {"type": "integer", "minimum": 0},
                    "resume_policy": {"enum": [value.value for value in ResumePolicy]},
                    "operator_confirmation": {"type": "boolean"},
                    "timeout_seconds": {"type": ["number", "null"], "exclusiveMinimum": 0},
                    "acceptance_criteria": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                    },
                },
                "additionalProperties": False,
            },
        },
        "initial_phase": {"type": "string", "minLength": 1},
        "artifacts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["artifact_id", "role", "media_type"],
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 1},
                    "role": {"type": "string", "minLength": 1},
                    "media_type": {"type": "string", "minLength": 1},
                    "producer_phase": {"type": ["string", "null"]},
                    "producer_component": {"type": ["string", "null"]},
                    "producer_port": {"type": ["string", "null"]},
                    "expected_digest": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
            },
        },
        "scheduling": {
            "type": "object",
            "properties": {
                "backpressure": {
                    "enum": [value.value for value in BackpressureDisposition]
                },
                "primary_first": {"type": "boolean"},
                "shadow_queue_limit": {"type": ["integer", "null"], "minimum": 0},
                "shadow_failure": {
                    "enum": [value.value for value in ShadowFailurePolicy]
                },
            },
            "additionalProperties": False,
        },
        "scheduled_triggers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "trigger_id",
                    "phase_id",
                    "target_component",
                    "scheduled_offset_seconds",
                ],
                "properties": {
                    "trigger_id": {"type": "string", "minLength": 1},
                    "phase_id": {"type": "string", "minLength": 1},
                    "target_component": {"type": "string", "minLength": 1},
                    "scheduled_offset_seconds": {"type": "number", "minimum": 0},
                    "deadline_offset_seconds": {"type": ["number", "null"], "minimum": 0},
                    "payload": {"type": "object"},
                },
                "additionalProperties": False,
            },
        },
        "state_triggers": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "rule_id",
                    "phase_id",
                    "source_component",
                    "target_component",
                ],
                "properties": {
                    "rule_id": {"type": "string", "minLength": 1},
                    "phase_id": {"type": "string", "minLength": 1},
                    "source_component": {"type": "string", "minLength": 1},
                    "target_component": {"type": "string", "minLength": 1},
                    "transition_kind": {"type": ["string", "null"], "minLength": 1},
                    "statuses": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "enum": ["applied", "rejected", "no_op", "rolled_back", "failed"]
                        },
                    },
                    "delay_seconds": {"type": "number", "minimum": 0},
                    "payload": {"type": "object"},
                    "once": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
        },
        "clock_policy": {"type": "object"},
        "recording": {"type": "object"},
        "validation": {"type": "object"},
    },
    "additionalProperties": False,
}
SUITE_OVERLAY_JSON_SCHEMA: Mapping[str, Any] = {
    "$schema": _JSON_SCHEMA,
    "type": "object",
    "required": ["schema", "overlay_id"],
    "properties": {
        "schema": {"const": SUITE_OVERLAY_SCHEMA},
        "overlay_id": {"type": "string", "minLength": 1},
        "component_config": {
            "type": "object",
            "additionalProperties": {"type": "object"},
        },
        "recording": {"type": "object"},
        "validation": {"type": "object"},
    },
    "additionalProperties": False,
}
