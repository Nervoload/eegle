"""Immutable plans consumed by the execution engine.

Version 2 adds the Phase 5 compiler's phase, role, and deployment records while
retaining exact v1 hashing so existing evidence bundles remain readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._domain import ComponentKind, ExecutionMode
from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash


LEGACY_EXECUTION_PLAN_SCHEMA = "eegle.execution_plan.v1"
EXECUTION_PLAN_SCHEMA = "eegle.execution_plan.v2"


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
        object.__setattr__(self, "config", freeze_json(self.config))
        object.__setattr__(self, "input_bindings", freeze_json(self.input_bindings or {}))
        object.__setattr__(self, "output_bindings", freeze_json(self.output_bindings or {}))

    def to_payload(self, *, include_phase5: bool = True) -> dict[str, Any]:
        payload = {
            "component_id": self.component_id,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "config": thaw_json(self.config),
            "input_bindings": thaw_json(self.input_bindings),
            "output_bindings": thaw_json(self.output_bindings),
        }
        if include_phase5:
            payload["role"] = self.role
            payload["stream_id"] = self.stream_id
            payload["required_capabilities"] = list(self.required_capabilities)
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

    def to_payload(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "component_ids": list(self.component_ids),
            "transitions": [value.to_payload() for value in self.transitions],
            "required_artifacts": list(self.required_artifacts),
            "retry_limit": self.retry_limit,
            "resume_policy": self.resume_policy,
            "operator_confirmation": self.operator_confirmation,
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
class ExecutionPlan:
    plan_id: str
    execution_mode: ExecutionMode
    plugins: tuple[LockedPlugin, ...]
    components: tuple[PlannedComponent, ...]
    spec_hashes: Mapping[str, str]
    clock_policy: Mapping[str, Any]
    recording_policy: Mapping[str, Any]
    validation_rules: Mapping[str, Any]
    phases: tuple[PlannedPhase, ...] = ()
    initial_phase: str | None = None
    placements: tuple[PlannedPlacement, ...] = ()
    schema: str = EXECUTION_PLAN_SCHEMA

    def __post_init__(self) -> None:
        if self.schema not in {EXECUTION_PLAN_SCHEMA, LEGACY_EXECUTION_PLAN_SCHEMA}:
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
        hashes = {str(key): require_digest(str(value), f"spec_hashes[{key}]") for key, value in self.spec_hashes.items()}
        object.__setattr__(self, "spec_hashes", freeze_json(hashes))
        object.__setattr__(self, "clock_policy", freeze_json(self.clock_policy))
        object.__setattr__(self, "recording_policy", freeze_json(self.recording_policy))
        object.__setattr__(self, "validation_rules", freeze_json(self.validation_rules))

    @property
    def plan_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        phase5 = self.schema == EXECUTION_PLAN_SCHEMA
        payload = {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "execution_mode": self.execution_mode.value,
            "plugins": [plugin.to_payload() for plugin in self.plugins],
            "components": [
                component.to_payload(include_phase5=phase5) for component in self.components
            ],
            "spec_hashes": thaw_json(self.spec_hashes),
            "clock_policy": thaw_json(self.clock_policy),
            "recording_policy": thaw_json(self.recording_policy),
            "validation_rules": thaw_json(self.validation_rules),
        }
        if phase5:
            payload["phases"] = [value.to_payload() for value in self.phases]
            payload["initial_phase"] = self.initial_phase
            payload["placements"] = [value.to_payload() for value in self.placements]
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
            phases=tuple(PlannedPhase.from_payload(value) for value in payload.get("phases", ())),
            initial_phase=None
            if payload.get("initial_phase") is None
            else str(payload["initial_phase"]),
            placements=tuple(
                PlannedPlacement.from_payload(value)
                for value in payload.get("placements", ())
            ),
        )
        if payload.get("plan_hash") != plan.plan_hash:
            raise ValueError("execution plan hash mismatch")
        return plan
