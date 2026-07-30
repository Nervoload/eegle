"""Construction-safe plugin inspection and explicit conformance services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._validation import require_identifier
from eegle.plugins import (
    PLUGIN_CONFORMANCE_REPORT_SCHEMA_ID,
    ExecutionContext,
    ExecutionMode,
    ModelConstructionContext,
    PluginConformanceReport,
    PluginDescriptor,
    PluginExercise,
    PluginRegistry,
    check_plugin_conformance,
)

PLUGIN_INSPECTION_SCHEMA_ID = "eegle.plugin_inspection.v1"


@dataclass(frozen=True, slots=True)
class PluginInspection:
    descriptors: tuple[PluginDescriptor, ...]
    entry_points_loaded: bool
    schema: str = PLUGIN_INSPECTION_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != PLUGIN_INSPECTION_SCHEMA_ID:
            raise ValueError(f"unsupported plugin inspection schema: {self.schema}")
        descriptors = tuple(self.descriptors)
        identities = tuple((value.plugin_id, value.version) for value in descriptors)
        if len(identities) != len(set(identities)):
            raise ValueError("plugin inspection descriptor identities must be unique")
        object.__setattr__(
            self,
            "descriptors",
            tuple(sorted(descriptors, key=lambda value: (value.plugin_id, value.version))),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "entry_points_loaded": self.entry_points_loaded,
            "factory_invoked": False,
            "descriptors": [
                {**value.to_payload(), "descriptor_hash": value.descriptor_hash}
                for value in self.descriptors
            ],
        }


def inspect_plugins(
    plugin_id: str | None = None,
    *,
    version_spec: str | None = None,
    include_entry_points: bool = True,
) -> PluginInspection:
    """Inspect canonical descriptors without constructing their components."""

    registry = _inspection_registry(include_entry_points=include_entry_points)
    if plugin_id is None:
        if version_spec is not None:
            raise ValueError("version_spec requires a plugin_id")
        descriptors = registry.descriptors()
    else:
        normalized = require_identifier(plugin_id, "plugin_id")
        descriptors = (registry.resolve(normalized, version_spec),)
    return PluginInspection(descriptors, include_entry_points)


def check_plugin(
    plugin_id: str,
    *,
    version_spec: str | None = None,
    include_entry_points: bool = True,
    config: Mapping[str, Any] | None = None,
    construct: bool = False,
    construction_context: ModelConstructionContext | None = None,
    lifecycle_context: ExecutionContext | None = None,
    exercises: tuple[PluginExercise, ...] = (),
    mode: ExecutionMode | None = None,
) -> PluginConformanceReport:
    """Check one installed plugin; component construction is explicit."""

    registry = _inspection_registry(include_entry_points=include_entry_points)
    descriptor = registry.resolve(plugin_id, version_spec, mode=mode)
    return check_plugin_conformance(
        descriptor,
        config=config,
        construct=construct,
        construction_context=construction_context,
        lifecycle_context=lifecycle_context,
        exercises=exercises,
        mode=mode,
    )


def _inspection_registry(*, include_entry_points: bool) -> PluginRegistry:
    registry = PluginRegistry()
    registry.register_builtins()
    if include_entry_points:
        registry.load_entry_points()
    return registry


__all__ = [
    "PLUGIN_CONFORMANCE_REPORT_SCHEMA_ID",
    "PLUGIN_INSPECTION_SCHEMA_ID",
    "PluginInspection",
    "check_plugin",
    "inspect_plugins",
]
