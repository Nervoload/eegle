"""Exact construction boundary from a locked plan to executable components."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from eegle._domain import ComponentKind, EquivalenceLevel
from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.compiler.plan import (
    EXECUTION_PLAN_SCHEMA,
    ExecutionPlan,
    LockedPlugin,
    PlannedComponent,
    PlannedAuthorizationProvider,
    PlannedModelBinding,
    PlannedPlacement,
)
from eegle.plugins.registry import (
    PluginDescriptor,
    PluginRegistry,
    StateBehavior,
    validate_component_instance,
)


RUNTIME_SNAPSHOT_SCHEMA = "eegle.plan_runtime_snapshot.v1"


class PlanConstructionError(ValueError):
    pass


class ComponentProxyFactory(Protocol):
    def create_proxy(
        self,
        component: PlannedComponent,
        plugin: LockedPlugin,
        descriptor: PluginDescriptor,
        placement: PlannedPlacement,
    ) -> Any:
        ...


@dataclass(frozen=True, slots=True)
class RuntimeNode:
    planned: PlannedComponent
    plugin: LockedPlugin
    descriptor: PluginDescriptor
    placement: PlannedPlacement
    component: Any
    model_binding: PlannedModelBinding | None = None

    @property
    def component_id(self) -> str:
        return self.planned.component_id


@dataclass(frozen=True, slots=True)
class RuntimeAuthorizationProvider:
    planned: PlannedAuthorizationProvider
    plugin: LockedPlugin
    descriptor: PluginDescriptor
    provider: Any

    @property
    def provider_id(self) -> str:
        return self.planned.provider_id


@dataclass(frozen=True, slots=True)
class PlanRuntimeSnapshot:
    plan_hash: str
    component_states: Mapping[str, Any]
    schema: str = RUNTIME_SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RUNTIME_SNAPSHOT_SCHEMA:
            raise ValueError(f"unsupported runtime snapshot schema: {self.schema}")
        object.__setattr__(self, "plan_hash", require_digest(self.plan_hash, "plan_hash"))
        states = {
            require_identifier(str(key), "component_id"): freeze_json(value)
            for key, value in self.component_states.items()
        }
        object.__setattr__(self, "component_states", freeze_json(states))

    @property
    def snapshot_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plan_hash": self.plan_hash,
            "component_states": thaw_json(self.component_states),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["snapshot_hash"] = self.snapshot_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PlanRuntimeSnapshot":
        value = cls(
            schema=str(payload.get("schema", RUNTIME_SNAPSHOT_SCHEMA)),
            plan_hash=str(payload["plan_hash"]),
            component_states=dict(payload.get("component_states") or {}),
        )
        if payload.get("snapshot_hash") != value.snapshot_hash:
            raise ValueError("plan runtime snapshot hash mismatch")
        return value


class PlanRuntime:
    """Constructed, exact component set for one immutable plan."""

    def __init__(
        self,
        plan: ExecutionPlan,
        nodes: tuple[RuntimeNode, ...],
        authorization_providers: tuple[RuntimeAuthorizationProvider, ...] = (),
    ) -> None:
        if plan.schema != EXECUTION_PLAN_SCHEMA:
            raise PlanConstructionError("graph execution requires execution plan v1")
        if plan.graph is None:
            raise PlanConstructionError("graph execution requires the typed compiled graph")
        if plan.validation_rules.get("graph_hash") != plan.graph.graph_hash:
            raise PlanConstructionError("compiled graph hash differs from plan validation lock")
        self.plan = plan
        self.nodes = nodes
        self.authorization_providers = authorization_providers
        self._authorization_by_id = {
            value.provider_id: value for value in authorization_providers
        }
        if len(self._authorization_by_id) != len(authorization_providers):
            raise PlanConstructionError("runtime authorization provider identities must be unique")
        if set(self._authorization_by_id) != {
            value.provider_id for value in plan.authorization_providers
        }:
            raise PlanConstructionError(
                "runtime authorization providers do not match the immutable plan"
            )
        self._by_id = {value.component_id: value for value in nodes}
        if len(self._by_id) != len(nodes):
            raise PlanConstructionError("runtime component identities must be unique")
        planned_ids = {value.component_id for value in plan.components}
        if set(self._by_id) != planned_ids:
            raise PlanConstructionError("runtime nodes do not exactly match planned components")
        planned_model_bindings = {
            value.component_id: value for value in plan.model_bindings
        }
        observed_model_bindings = {
            value.component_id: value.model_binding
            for value in nodes
            if value.model_binding is not None
        }
        if observed_model_bindings != planned_model_bindings:
            raise PlanConstructionError(
                "runtime model bindings do not exactly match the immutable plan"
            )
        if planned_model_bindings:
            model_components = {
                value.component_id
                for value in nodes
                if value.plugin.kind == ComponentKind.MODEL
            }
            if set(planned_model_bindings) != model_components:
                raise PlanConstructionError(
                    "every model component requires exactly one planned model binding"
                )
        self._verify_graph_ports()
        self._closed = False

    def _verify_graph_ports(self) -> None:
        assert self.plan.graph is not None
        graph_ports = {
            (value.component_id, value.direction.value, value.name): (
                value.type_id,
                value.required,
                value.multiple,
            )
            for value in self.plan.graph.ports
        }
        descriptor_ports: dict[tuple[str, str, str], tuple[str, bool, bool]] = {}
        for node in self.nodes:
            for direction, ports in (
                ("input", node.descriptor.input_ports),
                ("output", node.descriptor.output_ports),
            ):
                for port in ports:
                    descriptor_ports[(node.component_id, direction, port.name)] = (
                        port.type_id,
                        port.required,
                        port.multiple,
                    )
        if graph_ports != descriptor_ports:
            raise PlanConstructionError(
                "typed graph ports differ from the exact locked plugin descriptors"
            )
        known_outputs = {
            f"{value.component_id}.{value.name}"
            for value in self.plan.graph.ports
            if value.direction.value == "output"
        }
        known_inputs = {
            f"{value.component_id}.{value.name}"
            for value in self.plan.graph.ports
            if value.direction.value == "input"
        }
        for route in self.plan.graph.routes:
            if route.source not in known_outputs or route.target not in known_inputs:
                raise PlanConstructionError(
                    f"compiled route {route.route_id} references an unknown typed port"
                )

    def node(self, component_id: str) -> RuntimeNode:
        try:
            return self._by_id[component_id]
        except KeyError as exc:
            raise KeyError(f"unknown runtime component: {component_id}") from exc

    def authorization_provider(self, provider_id: str) -> RuntimeAuthorizationProvider:
        try:
            return self._authorization_by_id[provider_id]
        except KeyError as exc:
            raise KeyError(f"unknown runtime authorization provider: {provider_id}") from exc

    @property
    def equivalence_ceiling(self) -> EquivalenceLevel:
        rank = {
            EquivalenceLevel.BITWISE: 0,
            EquivalenceLevel.NUMERIC: 1,
            EquivalenceLevel.SEMANTIC: 2,
            EquivalenceLevel.TRACE: 3,
            EquivalenceLevel.NON_REPLAYABLE: 4,
        }
        return max(
            (
                value.descriptor.capabilities.equivalence
                for value in (*self.nodes, *self.authorization_providers)
            ),
            key=lambda value: rank[value],
            default=EquivalenceLevel.BITWISE,
        )

    def snapshot_state(self) -> PlanRuntimeSnapshot:
        states: dict[str, Any] = {}
        for node in self.nodes:
            snapshot = getattr(node.component, "snapshot_state", None)
            restore = getattr(node.component, "restore_state", None)
            if callable(snapshot) != callable(restore):
                raise PlanConstructionError(
                    f"component {node.component_id} must expose snapshot and restore together"
                )
            if callable(snapshot):
                states[node.component_id] = snapshot()
        for provider in self.authorization_providers:
            snapshot = getattr(provider.provider, "snapshot_state", None)
            restore = getattr(provider.provider, "restore_state", None)
            if callable(snapshot) != callable(restore):
                raise PlanConstructionError(
                    f"authorization provider {provider.provider_id} must expose snapshot and restore together"
                )
            if callable(snapshot):
                states[f"authorization:{provider.provider_id}"] = snapshot()
        return PlanRuntimeSnapshot(self.plan.plan_hash, states)

    def restore_state(self, snapshot: PlanRuntimeSnapshot) -> None:
        if snapshot.plan_hash != self.plan.plan_hash:
            raise PlanConstructionError("runtime snapshot belongs to a different plan")
        expected = {
            node.component_id
            for node in self.nodes
            if node.descriptor.capabilities.state_behavior
            == StateBehavior.SNAPSHOT_RESTORE
        }
        expected.update(
            f"authorization:{value.provider_id}"
            for value in self.authorization_providers
            if value.descriptor.capabilities.state_behavior
            == StateBehavior.SNAPSHOT_RESTORE
        )
        observed = set(snapshot.component_states)
        if observed != expected:
            missing = sorted(expected - observed)
            unexpected = sorted(observed - expected)
            raise PlanConstructionError(
                "runtime snapshot state set differs from the locked plan "
                f"(missing={missing}, unexpected={unexpected})"
            )
        for component_id, state in snapshot.component_states.items():
            if component_id.startswith("authorization:"):
                provider_id = component_id.split(":", 1)[1]
                restore = getattr(
                    self.authorization_provider(provider_id).provider,
                    "restore_state",
                    None,
                )
            else:
                restore = getattr(self.node(component_id).component, "restore_state", None)
            if not callable(restore):
                raise PlanConstructionError(
                    f"runtime authority {component_id} cannot restore locked state"
                )
            restore(thaw_json(state))

    def close(self) -> tuple[str, ...]:
        if self._closed:
            return ()
        failures: list[str] = []
        for node in self.nodes:
            if node.plugin.kind.value != "source":
                continue
            close = getattr(node.component, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # pragma: no cover - external boundary
                    failures.append(
                        f"{node.component_id}: {type(exc).__name__}: {exc}"
                    )
        self._closed = True
        return tuple(failures)


def construct_plan_runtime(
    plan: ExecutionPlan,
    registry: PluginRegistry,
    *,
    proxy_factory: ComponentProxyFactory | None = None,
    component_overrides: Mapping[str, Any] | None = None,
) -> PlanRuntime:
    """Construct exactly the implementations locked by a compiled plan."""

    if plan.schema != EXECUTION_PLAN_SCHEMA:
        raise PlanConstructionError("compiled graph runtime requires execution plan v1")
    locked = {(value.plugin_id, value.version): value for value in plan.plugins}
    placements = {value.component_id: value for value in plan.placements}
    overrides = dict(component_overrides or {})
    unknown_overrides = set(overrides) - {
        value.component_id for value in plan.components
    }
    if unknown_overrides:
        raise PlanConstructionError(
            f"component overrides reference unknown components: {sorted(unknown_overrides)}"
        )
    nodes: list[RuntimeNode] = []
    model_bindings = {value.component_id: value for value in plan.model_bindings}
    for component in plan.components:
        key = (component.plugin_id, component.plugin_version)
        plugin = locked.get(key)
        if plugin is None:
            raise PlanConstructionError(
                f"component {component.component_id} references an unlocked plugin"
            )
        try:
            descriptor = registry.resolve(
                component.plugin_id,
                f"=={component.plugin_version}",
                mode=plan.execution_mode,
            )
        except (KeyError, ValueError) as exc:
            raise PlanConstructionError(
                f"cannot resolve locked plugin for {component.component_id}: {exc}"
            ) from exc
        _verify_descriptor(plugin, descriptor, component.component_id)
        placement = placements.get(
            component.component_id,
            PlannedPlacement(component.component_id, "in_process"),
        )
        if component.component_id in overrides:
            if plugin.kind != ComponentKind.SOURCE:
                raise PlanConstructionError(
                    "runtime component overrides are restricted to replay source substitution"
                )
            instance = overrides.pop(component.component_id)
            validate_component_instance(descriptor, instance)
        elif placement.placement == "in_process":
            if placement.secret_refs:
                raise PlanConstructionError(
                    f"in-process secret resolution is not configured for {component.component_id}"
                )
            instance = registry.create(
                component.plugin_id,
                component.config,
                f"=={component.plugin_version}",
                mode=plan.execution_mode,
            )
        else:
            if proxy_factory is None:
                raise PlanConstructionError(
                    f"component {component.component_id} requires a {placement.placement} proxy"
                )
            instance = proxy_factory.create_proxy(
                component,
                plugin,
                descriptor,
                placement,
            )
            validate_component_instance(descriptor, instance)
        nodes.append(
            RuntimeNode(
                component,
                plugin,
                descriptor,
                placement,
                instance,
                model_bindings.get(component.component_id),
            )
        )
    if overrides:  # pragma: no cover - guarded above, retained defensively
        raise PlanConstructionError(f"unused component overrides: {sorted(overrides)}")
    authorization_providers: list[RuntimeAuthorizationProvider] = []
    for planned in plan.authorization_providers:
        key = (planned.plugin_id, planned.plugin_version)
        plugin = locked.get(key)
        if plugin is None:
            raise PlanConstructionError(
                f"authorization provider {planned.provider_id} references an unlocked plugin"
            )
        try:
            descriptor = registry.resolve(
                planned.plugin_id,
                f"=={planned.plugin_version}",
                mode=plan.execution_mode,
            )
        except (KeyError, ValueError) as exc:
            raise PlanConstructionError(
                f"cannot resolve authorization provider {planned.provider_id}: {exc}"
            ) from exc
        _verify_descriptor(plugin, descriptor, planned.provider_id)
        if descriptor.descriptor_hash != planned.descriptor_hash:
            raise PlanConstructionError(
                f"authorization provider descriptor drift for {planned.provider_id}"
            )
        instance = registry.create(
            planned.plugin_id,
            planned.config,
            f"=={planned.plugin_version}",
            mode=plan.execution_mode,
        )
        authorization_providers.append(
            RuntimeAuthorizationProvider(planned, plugin, descriptor, instance)
        )
    return PlanRuntime(plan, tuple(nodes), tuple(authorization_providers))


def _verify_descriptor(
    locked: LockedPlugin,
    descriptor: PluginDescriptor,
    component_id: str,
) -> None:
    observed = {
        "kind": descriptor.kind,
        "descriptor_hash": descriptor.descriptor_hash,
        "implementation": descriptor.implementation,
        "distribution": descriptor.distribution,
    }
    expected = {
        "kind": locked.kind,
        "descriptor_hash": locked.descriptor_hash,
        "implementation": locked.implementation,
        "distribution": locked.distribution,
    }
    if observed != expected:
        differences = sorted(key for key in expected if expected[key] != observed[key])
        raise PlanConstructionError(
            f"locked plugin drift for {component_id}: {', '.join(differences)}"
        )
