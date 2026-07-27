"""Canonical, independent snapshots for stateful runtime authorities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._validation import freeze_json, thaw_json
from eegle.compiler.lock import canonical_hash


@dataclass(frozen=True, slots=True)
class CanonicalStateSnapshot:
    """An immutable state copy paired with its canonical content hash."""

    frozen_state: Any
    state_hash: str

    def thaw(self) -> Any:
        """Return a fresh mutable JSON-compatible copy for restoration."""

        return thaw_json(self.frozen_state)


def canonicalize_state(state: Any) -> CanonicalStateSnapshot:
    """Freeze, copy, and hash one explicitly JSON-compatible state value."""

    frozen = freeze_json(state)
    return CanonicalStateSnapshot(
        frozen_state=frozen,
        state_hash=canonical_hash(thaw_json(frozen)),
    )


def capture_canonical_state(component: Any) -> CanonicalStateSnapshot:
    """Capture an independent canonical snapshot from a stateful component."""

    snapshot = getattr(component, "snapshot_state", None)
    if not callable(snapshot):
        raise TypeError("stateful component does not expose snapshot_state()")
    state = snapshot()
    if not isinstance(state, Mapping):
        raise TypeError("snapshot_state() must return a JSON-compatible mapping")
    return canonicalize_state(state)
