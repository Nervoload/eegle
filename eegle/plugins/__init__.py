"""Executable plugin descriptors, discovery, component protocols, and registry."""

from eegle.plugins.contracts import (
    Actuator,
    Adapter,
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
)


__all__ = [
    "Actuator",
    "Adapter",
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
]
