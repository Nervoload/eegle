"""Normalize PsychoPy keyboard results without importing PsychoPy eagerly."""

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


def create_hardware_keyboard(keyboard_module: Any, *, backend: str = "ptb") -> Any:
    """Create PsychoPy's asynchronous hardware keyboard and clear stale keys.

    The PTB backend records key-down times in its operating-system queue rather
    than assigning a time when the task happens to poll the queue.  Keeping the
    construction in this import-light module also makes the live display probe
    and the task use the same backend contract.
    """

    keyboard = keyboard_module.Keyboard(backend=str(backend))
    clock = getattr(keyboard, "clock", None)
    if clock is None:
        raise RuntimeError("PsychoPy hardware Keyboard did not expose its timestamp clock")
    clock.reset()
    keyboard.clearEvents()
    return keyboard


def poll_hardware_keyboard(keyboard: Any) -> list[PsychoPyKeyEvent]:
    """Drain asynchronous key-down events from a hardware ``Keyboard``."""

    raw_events = keyboard.getKeys(keyList=None, waitRelease=False, clear=True)
    normalized = []
    for value in raw_events or []:
        event = _normalize_key_event(value)
        if event.name:
            normalized.append(event)
    return normalized


def stop_hardware_keyboard(keyboard: Any | None) -> None:
    """Stop a PsychoPy hardware queue without depending on private PTB APIs.

    PsychoPy's PTB keyboard backend owns a native queue and a background
    thread.  Closing the task window does not stop that queue.  In particular,
    leaving a preflight queue active can compete with the fresh task process
    for Windows' combined keyboard device.  ``Keyboard.stop`` is the public,
    backend-neutral lifecycle operation and is safe to call during cleanup.
    """

    if keyboard is None:
        return
    stop = getattr(keyboard, "stop", None)
    if not callable(stop):
        raise RuntimeError("PsychoPy hardware Keyboard did not expose stop()")
    stop()


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
