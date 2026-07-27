"""One executable registry for built-in and external component plugins."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from importlib import metadata
from typing import Any, Callable, Iterable, Mapping

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from eegle._domain import ComponentKind, Determinism, EquivalenceLevel, ExecutionMode
from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.specs.schemas import validate_payload, validate_schema


PLUGIN_ENTRY_POINT_GROUP = "eegle.plugins"
PLUGIN_DESCRIPTOR_SCHEMA = "eegle.plugin_descriptor.v1"
PluginFactory = Callable[[Mapping[str, Any]], Any]


class StateBehavior(str, Enum):
    STATELESS = "stateless"
    SNAPSHOT_RESTORE = "snapshot_restore"
    RECORD_ONLY = "record_only"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class PortSpec:
    name: str
    type_id: str
    required: bool = True
    multiple: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_identifier(self.name, "port name"))
        object.__setattr__(self, "type_id", require_identifier(self.type_id, "port type_id"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type_id": self.type_id,
            "required": self.required,
            "multiple": self.multiple,
        }


@dataclass(frozen=True, slots=True)
class PluginCapabilities:
    supported_modes: frozenset[ExecutionMode]
    determinism: Determinism
    equivalence: EquivalenceLevel
    state_behavior: StateBehavior
    requires_future: bool = False
    resources: tuple[str, ...] = ()
    supports_triggers: bool = False
    processing_operations: tuple[str, ...] = ()
    simulation_only: bool = False

    def __post_init__(self) -> None:
        modes = frozenset(ExecutionMode(value) for value in self.supported_modes)
        if not modes:
            raise ValueError("plugin must support at least one execution mode")
        object.__setattr__(self, "supported_modes", modes)
        object.__setattr__(self, "determinism", Determinism(self.determinism))
        object.__setattr__(self, "equivalence", EquivalenceLevel(self.equivalence))
        object.__setattr__(self, "state_behavior", StateBehavior(self.state_behavior))
        object.__setattr__(
            self,
            "resources",
            tuple(require_identifier(value, "resource") for value in self.resources),
        )
        operations = tuple(
            require_identifier(value, "processing operation")
            for value in self.processing_operations
        )
        if len(operations) != len(set(operations)):
            raise ValueError("plugin processing operations must be unique")
        object.__setattr__(self, "processing_operations", operations)
        if ExecutionMode.CAUSAL in modes and self.requires_future:
            raise ValueError("a causal plugin cannot require future information")

    def to_payload(self) -> dict[str, Any]:
        return {
            "supported_modes": sorted(mode.value for mode in self.supported_modes),
            "determinism": self.determinism.value,
            "equivalence": self.equivalence.value,
            "state_behavior": self.state_behavior.value,
            "requires_future": self.requires_future,
            "resources": list(self.resources),
            "supports_triggers": self.supports_triggers,
            "processing_operations": list(self.processing_operations),
            "simulation_only": self.simulation_only,
        }


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    plugin_id: str
    version: str
    kind: ComponentKind
    config_schema: Mapping[str, Any]
    input_ports: tuple[PortSpec, ...]
    output_ports: tuple[PortSpec, ...]
    capabilities: PluginCapabilities
    factory: PluginFactory
    implementation: str
    distribution: str | None = None
    schema: str = PLUGIN_DESCRIPTOR_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PLUGIN_DESCRIPTOR_SCHEMA:
            raise ValueError(f"unsupported plugin descriptor schema: {self.schema}")
        object.__setattr__(self, "plugin_id", require_identifier(self.plugin_id, "plugin_id"))
        try:
            Version(self.version)
        except InvalidVersion as exc:
            raise ValueError(f"invalid plugin version {self.version!r}") from exc
        object.__setattr__(self, "kind", ComponentKind(self.kind))
        if not callable(self.factory):
            raise TypeError("plugin factory must be callable")
        if not self.implementation.strip():
            raise ValueError("plugin implementation provenance cannot be empty")
        inputs = tuple(port.name for port in self.input_ports)
        outputs = tuple(port.name for port in self.output_ports)
        if len(inputs) != len(set(inputs)) or len(outputs) != len(set(outputs)):
            raise ValueError("plugin port names must be unique within each direction")
        schema_copy = thaw_json(freeze_json(self.config_schema))
        validate_schema(schema_copy)
        object.__setattr__(self, "config_schema", freeze_json(schema_copy))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plugin_id": self.plugin_id,
            "version": self.version,
            "kind": self.kind.value,
            "config_schema": thaw_json(self.config_schema),
            "input_ports": [port.to_payload() for port in self.input_ports],
            "output_ports": [port.to_payload() for port in self.output_ports],
            "capabilities": self.capabilities.to_payload(),
            "implementation": self.implementation,
            "distribution": self.distribution,
        }

    @property
    def descriptor_hash(self) -> str:
        return canonical_hash(self.to_payload())


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, dict[Version, PluginDescriptor]] = {}

    def register(self, descriptor: PluginDescriptor) -> None:
        versions = self._plugins.setdefault(descriptor.plugin_id, {})
        version = Version(descriptor.version)
        if version in versions:
            raise ValueError(
                f"plugin {descriptor.plugin_id} version {descriptor.version} is already registered"
            )
        versions[version] = descriptor

    def register_builtins(self) -> tuple[str, ...]:
        """Register dependency-light first-party components.

        The import is intentionally local so plugin contracts never acquire a
        package-import cycle through processing implementations.
        """

        from eegle.plugins.builtins import builtin_plugin_descriptors

        registered: list[str] = []
        for descriptor in builtin_plugin_descriptors():
            self.register(descriptor)
            registered.append(descriptor.plugin_id)
        return tuple(registered)

    def unregister(self, plugin_id: str, version: str | None = None) -> None:
        normalized = require_identifier(plugin_id, "plugin_id")
        if normalized not in self._plugins:
            return
        if version is None:
            del self._plugins[normalized]
            return
        self._plugins[normalized].pop(Version(version), None)
        if not self._plugins[normalized]:
            del self._plugins[normalized]

    def resolve(
        self,
        plugin_id: str,
        version_spec: str | None = None,
        *,
        mode: ExecutionMode | None = None,
    ) -> PluginDescriptor:
        normalized = require_identifier(plugin_id, "plugin_id")
        versions = self._plugins.get(normalized)
        if not versions:
            raise KeyError(f"unknown plugin: {normalized}")
        specifier = SpecifierSet(version_spec or "")
        candidates = [version for version in versions if version in specifier]
        if not candidates:
            raise KeyError(
                f"plugin {normalized} has no version matching {version_spec or '<any>'}"
            )
        descriptor = versions[max(candidates)]
        if mode is not None and ExecutionMode(mode) not in descriptor.capabilities.supported_modes:
            raise ValueError(
                f"plugin {normalized} {descriptor.version} does not support {ExecutionMode(mode).value}"
            )
        return descriptor

    def create(
        self,
        plugin_id: str,
        config: Mapping[str, Any],
        version_spec: str | None = None,
        *,
        mode: ExecutionMode | None = None,
    ) -> Any:
        descriptor = self.resolve(plugin_id, version_spec, mode=mode)
        config_copy = thaw_json(freeze_json(config))
        validate_payload(config_copy, descriptor.config_schema)
        component = descriptor.factory(config_copy)
        validate_component_instance(descriptor, component)
        return component

    def descriptors(self) -> tuple[PluginDescriptor, ...]:
        return tuple(
            self._plugins[plugin_id][version]
            for plugin_id in sorted(self._plugins)
            for version in sorted(self._plugins[plugin_id])
        )

    def load_entry_points(self, group: str = PLUGIN_ENTRY_POINT_GROUP) -> tuple[str, ...]:
        discovered = metadata.entry_points()
        if hasattr(discovered, "select"):
            entries = tuple(discovered.select(group=group))
        else:  # pragma: no cover - compatibility with older importlib.metadata
            entries = tuple(discovered.get(group, ()))
        loaded_ids: list[str] = []
        for entry in sorted(entries, key=lambda value: (value.name, value.value)):
            loaded = entry.load()
            materialized = loaded() if callable(loaded) and not isinstance(loaded, PluginDescriptor) else loaded
            descriptors = _coerce_descriptors(materialized)
            distribution = getattr(getattr(entry, "dist", None), "name", None)
            for descriptor in descriptors:
                if descriptor.distribution is None and distribution:
                    descriptor = replace(descriptor, distribution=str(distribution))
                self.register(descriptor)
                loaded_ids.append(descriptor.plugin_id)
        return tuple(loaded_ids)


def _coerce_descriptors(value: Any) -> tuple[PluginDescriptor, ...]:
    if isinstance(value, PluginDescriptor):
        return (value,)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        descriptors = tuple(value)
        if all(isinstance(item, PluginDescriptor) for item in descriptors):
            return descriptors
    raise TypeError("plugin entry point must load a PluginDescriptor or iterable of descriptors")


_REQUIRED_COMPONENT_MEMBERS: Mapping[ComponentKind, tuple[str, ...]] = {
    ComponentKind.SOURCE: ("stream_spec", "read", "close"),
    ComponentKind.TRANSFORM: ("update",),
    ComponentKind.WINDOW: ("update",),
    ComponentKind.QUALITY: ("evaluate",),
    ComponentKind.MODEL: ("predict",),
    ComponentKind.OUTCOME: ("update",),
    ComponentKind.ADAPTER: ("update",),
    ComponentKind.POLICY: ("decide",),
    ComponentKind.AUTHORIZATION: ("authorize", "resolve"),
    ComponentKind.ACTUATOR: ("submit",),
    ComponentKind.ARTIFACT: ("produce",),
    ComponentKind.SINK: ("append",),
}


def validate_component_instance(descriptor: PluginDescriptor, component: Any) -> None:
    """Fail construction when a factory cannot satisfy its declared role.

    This deliberately checks behavior names rather than concrete classes.
    Reusable contract tests remain responsible for exercising exact calls and
    return types because Python runtime protocols do not validate signatures.
    """

    graph_process = getattr(component, "process", None)
    required_members = (
        ()
        if descriptor.kind != ComponentKind.SOURCE and callable(graph_process)
        else _REQUIRED_COMPONENT_MEMBERS[descriptor.kind]
    )
    for member in required_members:
        value = getattr(component, member, None)
        if member != "stream_spec" and not callable(value):
            raise TypeError(
                f"plugin {descriptor.plugin_id} factory produced {type(component).__name__} "
                f"without callable {member}"
            )
        if member == "stream_spec" and value is None:
            raise TypeError(
                f"plugin {descriptor.plugin_id} source does not expose stream_spec"
            )
    if descriptor.capabilities.state_behavior == StateBehavior.SNAPSHOT_RESTORE:
        for member in ("snapshot_state", "restore_state"):
            if not callable(getattr(component, member, None)):
                raise TypeError(
                    f"plugin {descriptor.plugin_id} declares snapshot_restore but lacks {member}"
                )
    if descriptor.capabilities.supports_triggers and not callable(
        getattr(component, "handle_trigger", None)
    ):
        raise TypeError(
            f"plugin {descriptor.plugin_id} declares trigger support but lacks handle_trigger"
        )
