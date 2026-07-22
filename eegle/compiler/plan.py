"""Immutable Phase 2 draft of the plan consumed by the future engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._domain import ComponentKind, ExecutionMode
from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
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
    input_bindings: Mapping[str, str] = None  # type: ignore[assignment]
    output_bindings: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component_id", require_identifier(self.component_id, "component_id")
        )
        object.__setattr__(self, "plugin_id", require_identifier(self.plugin_id, "plugin_id"))
        if not self.plugin_version.strip():
            raise ValueError("planned component plugin_version cannot be empty")
        object.__setattr__(self, "config", freeze_json(self.config))
        object.__setattr__(self, "input_bindings", freeze_json(self.input_bindings or {}))
        object.__setattr__(self, "output_bindings", freeze_json(self.output_bindings or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "config": thaw_json(self.config),
            "input_bindings": thaw_json(self.input_bindings),
            "output_bindings": thaw_json(self.output_bindings),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PlannedComponent":
        return cls(
            component_id=str(payload["component_id"]),
            plugin_id=str(payload["plugin_id"]),
            plugin_version=str(payload["plugin_version"]),
            config=dict(payload.get("config") or {}),
            input_bindings=dict(payload.get("input_bindings") or {}),
            output_bindings=dict(payload.get("output_bindings") or {}),
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
        hashes = {str(key): require_digest(str(value), f"spec_hashes[{key}]") for key, value in self.spec_hashes.items()}
        object.__setattr__(self, "spec_hashes", freeze_json(hashes))
        object.__setattr__(self, "clock_policy", freeze_json(self.clock_policy))
        object.__setattr__(self, "recording_policy", freeze_json(self.recording_policy))
        object.__setattr__(self, "validation_rules", freeze_json(self.validation_rules))

    @property
    def plan_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "execution_mode": self.execution_mode.value,
            "plugins": [plugin.to_payload() for plugin in self.plugins],
            "components": [component.to_payload() for component in self.components],
            "spec_hashes": thaw_json(self.spec_hashes),
            "clock_policy": thaw_json(self.clock_policy),
            "recording_policy": thaw_json(self.recording_policy),
            "validation_rules": thaw_json(self.validation_rules),
        }

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
        )
        if payload.get("plan_hash") != plan.plan_hash:
            raise ValueError("execution plan hash mismatch")
        return plan
