"""Stream acquisition and marker helpers."""

from eegle.streams.lsl import LslMarkerOutlet, LslStream, NullMarkerOutlet, resolve_streams
from eegle.streams.markers import MarkerEvent


__all__ = [
    "LslMarkerOutlet",
    "LslStream",
    "MarkerEvent",
    "NullMarkerOutlet",
    "resolve_streams",
]
