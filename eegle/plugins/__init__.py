"""Executable plugin descriptors, discovery, component protocols, and registry."""

from eegle._domain import ComponentKind, Determinism, EquivalenceLevel, ExecutionMode
from eegle.plugins.contracts import (
    Actuator,
    Adapter,
    ExecutionContext,
    LifecycleComponent,
    Model,
    OutcomeResolver,
    Policy,
    QualityGate,
    Sink,
    Source,
    StatefulComponent,
    Transform,
    WindowBuilder,
)
from eegle.plugins.registry import (
    PluginCapabilities,
    PluginDescriptor,
    PluginRegistry,
    PortSpec,
    StateBehavior,
    validate_component_instance,
)


__all__ = [
    "Actuator",
    "Adapter",
    "ComponentKind",
    "Determinism",
    "EquivalenceLevel",
    "ExecutionContext",
    "ExecutionMode",
    "LifecycleComponent",
    "Model",
    "OutcomeResolver",
    "PluginCapabilities",
    "PluginDescriptor",
    "PluginRegistry",
    "Policy",
    "PortSpec",
    "QualityGate",
    "Sink",
    "Source",
    "StateBehavior",
    "StatefulComponent",
    "Transform",
    "WindowBuilder",
    "validate_component_instance",
]
