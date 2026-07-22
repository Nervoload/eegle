"""Small first-party processing primitives with explicit causality."""

from eegle.processing.buffers import BoundedBuffer, BufferEntry
from eegle.processing.capabilities import TransformCapabilities
from eegle.processing.quality import FiniteQualityGate, QualityDecision, QualityStatus
from eegle.processing.transforms import CausalSosFilter, IdentityTransform, RetrospectiveSosFilter
from eegle.processing.windows import ContinuousWindowSpec, EventWindowSpec, Window


__all__ = [
    "BoundedBuffer",
    "BufferEntry",
    "CausalSosFilter",
    "ContinuousWindowSpec",
    "EventWindowSpec",
    "FiniteQualityGate",
    "IdentityTransform",
    "QualityDecision",
    "QualityStatus",
    "RetrospectiveSosFilter",
    "TransformCapabilities",
    "Window",
]
