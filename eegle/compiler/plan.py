"""Immutable graph-bearing plans consumed by the execution engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._domain import ComponentKind, ExecutionMode
from eegle._validation import (
    freeze_json,
    require_digest,
    require_finite,
    require_identifier,
    thaw_json,
)
from eegle.compiler.graph import CompiledGraph
from eegle.compiler.lock import canonical_hash


EXECUTION_PLAN_SCHEMA = "eegle.execution_plan.v1"


@dataclass(frozen=True, slots=True)
class LockedPlugin:
    plugin_id: str
    version: str
    kind: ComponentKind
    descriptor_hash: str
    distribution: str | None
    implementation: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "plugin_id", require_identifier(self.plugin_id, "plugin_id"))
        object.__setattr__(self, "kind", ComponentKind(self.kind))
        object.__setattr__(
            self, "descriptor_hash", require_digest(self.descriptor_hash, "descriptor_hash")
        )
        if not self.version.strip() or not self.implementation.strip():
            raise ValueError("locked plugin version and implementation cannot be empty")

    def to_payload(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "kind": self.kind.value,
            "descriptor_hash": self.descriptor_hash,
            "distribution": self.distribution,
            "implementation": self.implementation,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LockedPlugin":
        return cls(
            plugin_id=str(payload["plugin_id"]),
            version=str(payload["version"]),
            kind=ComponentKind(str(payload["kind"])),
            descriptor_hash=str(payload["descriptor_hash"]),
            distribution=None
            if payload.get("distribution") is None
            else str(payload["distribution"]),
            implementation=str(payload["implementation"]),
        )


@dataclass(frozen=True, slots=True)
class PlannedComponent:
    component_id: str
    plugin_id: str
    plugin_version: str
    config: Mapping[str, Any]
    input_bindings: Mapping[str, Any] = None  # type: ignore[assignment]
    output_bindings: Mapping[str, Any] = None  # type: ignore[assignment]
    role: str | None = None
    stream_id: str | None = None
    required_capabilities: tuple[str, ...] = ()
    outcome_uses: tuple[str, ...] = ()
    required_outcome_use: str | None = None
    action_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component_id", require_identifier(self.component_id, "component_id")
        )
        object.__setattr__(self, "plugin_id", require_identifier(self.plugin_id, "plugin_id"))
        if not self.plugin_version.strip():
            raise ValueError("planned component plugin_version cannot be empty")
        if self.role is not None:
            object.__setattr__(self, "role", require_identifier(self.role, "role"))
        if self.stream_id is not None:
            object.__setattr__(
                self, "stream_id", require_identifier(self.stream_id, "stream_id")
            )
        capabilities = tuple(
            require_identifier(value, "required capability")
            for value in self.required_capabilities
        )
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("planned component capabilities must be unique")
        object.__setattr__(self, "required_capabilities", capabilities)
        outcome_uses = tuple(
            require_identifier(value, "outcome use") for value in self.outcome_uses
        )
        if len(outcome_uses) != len(set(outcome_uses)):
            raise ValueError("planned outcome uses must be unique")
        object.__setattr__(self, "outcome_uses", outcome_uses)
        if self.required_outcome_use is not None:
            object.__setattr__(
                self,
                "required_outcome_use",
                require_identifier(self.required_outcome_use, "required outcome use"),
            )
        action_capabilities = tuple(
            require_identifier(value, "action capability")
            for value in self.action_capabilities
        )
        if len(action_capabilities) != len(set(action_capabilities)):
            raise ValueError("planned action capabilities must be unique")
        object.__setattr__(self, "action_capabilities", action_capabilities)
        object.__setattr__(self, "config", freeze_json(self.config))
        object.__setattr__(self, "input_bindings", freeze_json(self.input_bindings or {}))
        object.__setattr__(self, "output_bindings", freeze_json(self.output_bindings or {}))

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "component_id": self.component_id,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "config": thaw_json(self.config),
            "input_bindings": thaw_json(self.input_bindings),
            "output_bindings": thaw_json(self.output_bindings),
        }
        payload["role"] = self.role
        payload["stream_id"] = self.stream_id
        payload["required_capabilities"] = list(self.required_capabilities)
        payload["outcome_uses"] = list(self.outcome_uses)
        payload["required_outcome_use"] = self.required_outcome_use
        payload["action_capabilities"] = list(self.action_capabilities)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PlannedComponent":
        return cls(
            component_id=str(payload["component_id"]),
            plugin_id=str(payload["plugin_id"]),
            plugin_version=str(payload["plugin_version"]),
            config=dict(payload.get("config") or {}),
            input_bindings=dict(payload.get("input_bindings") or {}),
            output_bindings=dict(payload.get("output_bindings") or {}),
            role=None if payload.get("role") is None else str(payload["role"]),
            stream_id=None
            if payload.get("stream_id") is None
            else str(payload["stream_id"]),
            required_capabilities=tuple(
                str(value) for value in payload.get("required_capabilities", ())
            ),
            outcome_uses=tuple(str(value) for value in payload.get("outcome_uses", ())),
            required_outcome_use=None
            if payload.get("required_outcome_use") is None
            else str(payload["required_outcome_use"]),
            action_capabilities=tuple(
                str(value) for value in payload.get("action_capabilities", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class PlannedTransition:
    target_phase: str
    condition: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "target_phase", require_identifier(self.target_phase, "target_phase")
        )
        object.__setattr__(self, "condition", require_identifier(self.condition, "condition"))

    def to_payload(self) -> dict[str, Any]:
        return {"target_phase": self.target_phase, "condition": self.condition}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PlannedTransition":
        return cls(target_phase=str(payload["target_phase"]), condition=str(payload["condition"]))


@dataclass(frozen=True, slots=True)
class PlannedPhase:
    phase_id: str
    component_ids: tuple[str, ...]
    transitions: tuple[PlannedTransition, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    retry_limit: int = 0
    resume_policy: str = "restart"
    operator_confirmation: bool = False
    timeout_seconds: float | None = None
    acceptance_criteria: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase_id", require_identifier(self.phase_id, "phase_id"))
        components = tuple(
            require_identifier(value, "phase component") for value in self.component_ids
        )
        if not components or len(components) != len(set(components)):
            raise ValueError("planned phase components must be non-empty and unique")
        object.__setattr__(self, "component_ids", components)
        artifacts = tuple(
            require_identifier(value, "required artifact") for value in self.required_artifacts
        )
        if len(artifacts) != len(set(artifacts)):
            raise ValueError("planned phase artifacts must be unique")
        object.__setattr__(self, "required_artifacts", artifacts)
        object.__setattr__(self, "retry_limit", int(self.retry_limit))
        if self.retry_limit < 0:
            raise ValueError("planned phase retry_limit cannot be negative")
        object.__setattr__(
            self, "resume_policy", require_identifier(self.resume_policy, "resume_policy")
        )
        if self.timeout_seconds is not None:
            timeout = require_finite(self.timeout_seconds, "timeout_seconds")
            if timeout <= 0:
                raise ValueError("planned phase timeout_seconds must be positive")
            object.__setattr__(self, "timeout_seconds", timeout)
        criteria = tuple(
            require_identifier(value, "acceptance criterion")
            for value in self.acceptance_criteria
        )
        if len(criteria) != len(set(criteria)):
            raise ValueError("planned phase acceptance criteria must be unique")
        object.__setattr__(self, "acceptance_criteria", criteria)

    def to_payload(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "component_ids": list(self.component_ids),
            "transitions": [value.to_payload() for value in self.transitions],
            "required_artifacts": list(self.required_artifacts),
            "retry_limit": self.retry_limit,
            "resume_policy": self.resume_policy,
            "operator_confirmation": self.operator_confirmation,
            "timeout_seconds": self.timeout_seconds,
            "acceptance_criteria": list(self.acceptance_criteria),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PlannedPhase":
        return cls(
            phase_id=str(payload["phase_id"]),
            component_ids=tuple(str(value) for value in payload["component_ids"]),
            transitions=tuple(
                PlannedTransition.from_payload(value)
                for value in payload.get("transitions", ())
            ),
            required_artifacts=tuple(
                str(value) for value in payload.get("required_artifacts", ())
            ),
            retry_limit=int(payload.get("retry_limit", 0)),
            resume_policy=str(payload.get("resume_policy", "restart")),
            operator_confirmation=bool(payload.get("operator_confirmation", False)),
            timeout_seconds=None
            if payload.get("timeout_seconds") is None
            else float(payload["timeout_seconds"]),
            acceptance_criteria=tuple(
                str(value) for value in payload.get("acceptance_criteria", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class PlannedScheduledTrigger:
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
            raise ValueError("planned trigger offset cannot be negative")
        object.__setattr__(self, "scheduled_offset_seconds", scheduled)
        if self.deadline_offset_seconds is not None:
            deadline = require_finite(
                self.deadline_offset_seconds, "deadline_offset_seconds"
            )
            if deadline < scheduled:
                raise ValueError("planned trigger deadline cannot precede its offset")
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
    def from_payload(cls, payload: Mapping[str, Any]) -> "PlannedScheduledTrigger":
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
class PlannedStateTrigger:
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
            raise ValueError("planned state trigger statuses must be non-empty and unique")
        object.__setattr__(self, "statuses", statuses)
        delay = require_finite(self.delay_seconds, "delay_seconds")
        if delay < 0:
            raise ValueError("planned state trigger delay cannot be negative")
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
    def from_payload(cls, payload: Mapping[str, Any]) -> "PlannedStateTrigger":
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
class PlannedPlacement:
    component_id: str
    placement: str
    endpoint_id: str | None = None
    resource_ids: tuple[str, ...] = ()
    secret_refs: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component_id", require_identifier(self.component_id, "component_id")
        )
        object.__setattr__(self, "placement", require_identifier(self.placement, "placement"))
        if self.endpoint_id is not None:
            object.__setattr__(
                self, "endpoint_id", require_identifier(self.endpoint_id, "endpoint_id")
            )
        resources = tuple(require_identifier(value, "resource_id") for value in self.resource_ids)
        if len(resources) != len(set(resources)):
            raise ValueError("planned placement resources must be unique")
        object.__setattr__(self, "resource_ids", resources)
        refs = {
            require_identifier(str(alias), "secret alias"): require_identifier(
                str(secret_id), "secret_id"
            )
            for alias, secret_id in (self.secret_refs or {}).items()
        }
        object.__setattr__(self, "secret_refs", freeze_json(refs))

    def to_payload(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "placement": self.placement,
            "endpoint_id": self.endpoint_id,
            "resource_ids": list(self.resource_ids),
            "secret_refs": thaw_json(self.secret_refs),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PlannedPlacement":
        return cls(
            component_id=str(payload["component_id"]),
            placement=str(payload["placement"]),
            endpoint_id=None
            if payload.get("endpoint_id") is None
            else str(payload["endpoint_id"]),
            resource_ids=tuple(str(value) for value in payload.get("resource_ids", ())),
            secret_refs={
                str(key): str(value)
                for key, value in dict(payload.get("secret_refs") or {}).items()
            },
        )


@dataclass(frozen=True, slots=True)
class PlannedArtifact:
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
            raise ValueError("planned artifact media_type cannot be empty")
        producer = (
            self.producer_phase,
            self.producer_component,
            self.producer_port,
        )
        if any(value is not None for value in producer) and not all(
            value is not None for value in producer
        ):
            raise ValueError("planned artifact producer fields must be declared together")
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
    def from_payload(cls, payload: Mapping[str, Any]) -> "PlannedArtifact":
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
class ExecutionPlan:
    plan_id: str
    execution_mode: ExecutionMode
    plugins: tuple[LockedPlugin, ...]
    components: tuple[PlannedComponent, ...]
    spec_hashes: Mapping[str, str]
    clock_policy: Mapping[str, Any]
    recording_policy: Mapping[str, Any]
    validation_rules: Mapping[str, Any]
    graph: CompiledGraph | None = None
    phases: tuple[PlannedPhase, ...] = ()
    initial_phase: str | None = None
    placements: tuple[PlannedPlacement, ...] = ()
    artifacts: tuple[PlannedArtifact, ...] = ()
    scheduling_policy: Mapping[str, Any] = None  # type: ignore[assignment]
    scheduled_triggers: tuple[PlannedScheduledTrigger, ...] = ()
    state_triggers: tuple[PlannedStateTrigger, ...] = ()
    schema: str = EXECUTION_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != EXECUTION_PLAN_SCHEMA:
            raise ValueError(f"unsupported execution plan schema: {self.schema}")
        object.__setattr__(self, "plan_id", require_identifier(self.plan_id, "plan_id"))
        object.__setattr__(self, "execution_mode", ExecutionMode(self.execution_mode))
        plugin_keys = tuple((plugin.plugin_id, plugin.version) for plugin in self.plugins)
        if len(plugin_keys) != len(set(plugin_keys)):
            raise ValueError("execution plan locked plugins must be unique")
        component_ids = tuple(component.component_id for component in self.components)
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("execution plan component identities must be unique")
        locked = set(plugin_keys)
        for component in self.components:
            if (component.plugin_id, component.plugin_version) not in locked:
                raise ValueError(
                    f"component {component.component_id} references an unlocked plugin version"
                )
        component_set = set(component_ids)
        if self.graph is not None:
            if set(self.graph.component_order) != component_set:
                raise ValueError("compiled graph component order differs from the plan")
            if len(self.graph.component_order) != len(component_ids):
                raise ValueError("compiled graph component order must contain each component once")
            port_components = {value.component_id for value in self.graph.ports}
            if not port_components.issubset(component_set):
                raise ValueError("compiled graph port references an unknown component")
        phase_ids = tuple(phase.phase_id for phase in self.phases)
        if len(phase_ids) != len(set(phase_ids)):
            raise ValueError("execution plan phase identities must be unique")
        for phase in self.phases:
            unknown = set(phase.component_ids) - component_set
            if unknown:
                raise ValueError(
                    f"phase {phase.phase_id} references unknown components: {sorted(unknown)}"
                )
            for transition in phase.transitions:
                if transition.target_phase not in set(phase_ids):
                    raise ValueError(
                        f"phase {phase.phase_id} references unknown phase "
                        f"{transition.target_phase}"
                    )
        if self.phases:
            if self.initial_phase not in set(phase_ids):
                raise ValueError("execution plan initial_phase must reference a phase")
        elif self.initial_phase is not None:
            raise ValueError("execution plan without phases cannot declare initial_phase")
        placement_ids = tuple(value.component_id for value in self.placements)
        if len(placement_ids) != len(set(placement_ids)):
            raise ValueError("execution plan placements must be unique")
        if set(placement_ids) - component_set:
            raise ValueError("execution plan placement references an unknown component")
        artifact_ids = tuple(value.artifact_id for value in self.artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("execution plan artifact identities must be unique")
        for artifact in self.artifacts:
            if artifact.producer_phase is not None and artifact.producer_phase not in set(phase_ids):
                raise ValueError("planned artifact producer phase is unknown")
            if artifact.producer_component is not None and artifact.producer_component not in component_set:
                raise ValueError("planned artifact producer component is unknown")
        trigger_ids = tuple(value.trigger_id for value in self.scheduled_triggers)
        if len(trigger_ids) != len(set(trigger_ids)):
            raise ValueError("planned scheduled trigger identities must be unique")
        rule_ids = tuple(value.rule_id for value in self.state_triggers)
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("planned state trigger identities must be unique")
        for trigger in self.scheduled_triggers:
            if trigger.phase_id not in set(phase_ids):
                raise ValueError("planned scheduled trigger phase is unknown")
            if trigger.target_component not in component_set:
                raise ValueError("planned scheduled trigger target is unknown")
        for rule in self.state_triggers:
            if rule.phase_id not in set(phase_ids):
                raise ValueError("planned state trigger phase is unknown")
            if rule.source_component not in component_set:
                raise ValueError("planned state trigger source is unknown")
            if rule.target_component not in component_set:
                raise ValueError("planned state trigger target is unknown")
        hashes = {str(key): require_digest(str(value), f"spec_hashes[{key}]") for key, value in self.spec_hashes.items()}
        object.__setattr__(self, "spec_hashes", freeze_json(hashes))
        object.__setattr__(self, "clock_policy", freeze_json(self.clock_policy))
        object.__setattr__(self, "recording_policy", freeze_json(self.recording_policy))
        object.__setattr__(self, "validation_rules", freeze_json(self.validation_rules))
        object.__setattr__(self, "scheduling_policy", freeze_json(self.scheduling_policy or {}))

    @property
    def plan_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "execution_mode": self.execution_mode.value,
            "plugins": [plugin.to_payload() for plugin in self.plugins],
            "components": [
                component.to_payload() for component in self.components
            ],
            "spec_hashes": thaw_json(self.spec_hashes),
            "clock_policy": thaw_json(self.clock_policy),
            "recording_policy": thaw_json(self.recording_policy),
            "validation_rules": thaw_json(self.validation_rules),
        }
        payload["graph"] = None if self.graph is None else self.graph.to_payload()
        payload["phases"] = [value.to_payload() for value in self.phases]
        payload["initial_phase"] = self.initial_phase
        payload["placements"] = [value.to_payload() for value in self.placements]
        payload["artifacts"] = [value.to_payload() for value in self.artifacts]
        payload["scheduling_policy"] = thaw_json(self.scheduling_policy)
        payload["scheduled_triggers"] = [
            value.to_payload() for value in self.scheduled_triggers
        ]
        payload["state_triggers"] = [value.to_payload() for value in self.state_triggers]
        return payload

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["plan_hash"] = self.plan_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ExecutionPlan":
        plan = cls(
            schema=str(payload.get("schema", EXECUTION_PLAN_SCHEMA)),
            plan_id=str(payload["plan_id"]),
            execution_mode=ExecutionMode(str(payload["execution_mode"])),
            plugins=tuple(LockedPlugin.from_payload(item) for item in payload["plugins"]),
            components=tuple(
                PlannedComponent.from_payload(item) for item in payload["components"]
            ),
            spec_hashes=dict(payload.get("spec_hashes") or {}),
            clock_policy=dict(payload.get("clock_policy") or {}),
            recording_policy=dict(payload.get("recording_policy") or {}),
            validation_rules=dict(payload.get("validation_rules") or {}),
            graph=None
            if payload.get("graph") is None
            else CompiledGraph.from_payload(payload["graph"]),
            phases=tuple(PlannedPhase.from_payload(value) for value in payload.get("phases", ())),
            initial_phase=None
            if payload.get("initial_phase") is None
            else str(payload["initial_phase"]),
            placements=tuple(
                PlannedPlacement.from_payload(value)
                for value in payload.get("placements", ())
            ),
            artifacts=tuple(
                PlannedArtifact.from_payload(value)
                for value in payload.get("artifacts", ())
            ),
            scheduling_policy=dict(payload.get("scheduling_policy") or {}),
            scheduled_triggers=tuple(
                PlannedScheduledTrigger.from_payload(value)
                for value in payload.get("scheduled_triggers", ())
            ),
            state_triggers=tuple(
                PlannedStateTrigger.from_payload(value)
                for value in payload.get("state_triggers", ())
            ),
        )
        if payload.get("plan_hash") != plan.plan_hash:
            raise ValueError("execution plan hash mismatch")
        return plan
