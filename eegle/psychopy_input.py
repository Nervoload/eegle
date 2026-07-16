"""Normalize legacy PsychoPy event-key results without importing PsychoPy eagerly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PsychoPyKeyEvent:
    """Normalized key-down event returned by any PsychoPy keyboard backend."""

    name: str
    rt: float | None


def poll_psychopy_keys(event_module: Any, *, clock: Any | None = None) -> list[PsychoPyKeyEvent]:
    """Poll every legacy PsychoPy event key and normalize its return shape."""
    raw_events = event_module.getKeys(timeStamped=clock) if clock is not None else event_module.getKeys()
    normalized = []
    for value in raw_events or []:
        event = _normalize_key_event(value)
        if event.name:
            normalized.append(event)
    return normalized


def clear_psychopy_keys(event_module: Any) -> None:
    """Clear queued legacy PsychoPy keyboard events."""
    event_module.clearEvents(eventType="keyboard")


def _normalize_key_event(value: Any) -> PsychoPyKeyEvent:
    rt = _optional_float(getattr(value, "rt", None))
    name: Any = getattr(value, "name", value)

    # Direct event.getKeys(timeStamped=clock) returns [name, timestamp].
    if isinstance(value, (list, tuple)):
        if value:
            name = value[0]
        if rt is None and len(value) >= 2:
            rt = _optional_float(value[-1])

    # Tolerate nested event pairs from upstream wrappers or test adapters.
    if isinstance(name, (list, tuple)):
        if rt is None and len(name) >= 2:
            rt = _optional_float(name[-1])
        name = name[0] if name else ""

    return PsychoPyKeyEvent(str(name).strip().lower(), rt)


def _optional_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
