"""Bounded deterministic event queue for the plan-owned executor."""

from __future__ import annotations

import heapq
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from eegle.streams.clocks import TimePoint


@dataclass(order=True, slots=True)
class QueuedEvent:
    sort_key: tuple[Any, ...]
    kind: str = field(compare=False)
    component_id: str = field(compare=False)
    port: str = field(compare=False)
    value: Any = field(compare=False)
    scheduled_time: TimePoint = field(compare=False)
    source_endpoint: str | None = field(compare=False, default=None)
    input_id: str | None = field(compare=False, default=None)


@dataclass(frozen=True, slots=True)
class PendingEmission:
    output_port: str
    value: Any
    input_ids: tuple[str, ...]


class EventQueue:
    """Min-heap with a plan-locked fail-run or reject-newest policy."""

    def __init__(self, max_pending_events: int, *, reject_newest: bool = False) -> None:
        self.max_pending_events = int(max_pending_events)
        if self.max_pending_events <= 0:
            raise ValueError("max_pending_events must be positive")
        self._events: list[QueuedEvent] = []
        self._component_counts: Counter[str] = Counter()
        self._component_kind_counts: Counter[tuple[str, str]] = Counter()
        self.reject_newest = bool(reject_newest)

    def __bool__(self) -> bool:
        return bool(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def peek(self) -> QueuedEvent:
        if not self._events:
            raise IndexError("cannot inspect an empty graph event queue")
        return self._events[0]

    def pop(self) -> QueuedEvent:
        if not self._events:
            raise IndexError("cannot pop an empty graph event queue")
        value = heapq.heappop(self._events)
        self._decrement(value)
        return value

    def count_component(self, component_id: str, *, kind: str | None = None) -> int:
        return (
            self._component_counts[component_id]
            if kind is None
            else self._component_kind_counts[(component_id, kind)]
        )

    def pop_oldest_component(
        self,
        component_id: str,
        *,
        kind: str | None = None,
    ) -> QueuedEvent | None:
        """Remove the earliest matching event while preserving heap semantics."""

        matches = [
            (index, value)
            for index, value in enumerate(self._events)
            if value.component_id == component_id
            and (kind is None or value.kind == kind)
        ]
        if not matches:
            return None
        index, selected = min(matches, key=lambda item: item[1].sort_key)
        last = self._events.pop()
        if index < len(self._events):
            self._events[index] = last
            heapq.heapify(self._events)
        self._decrement(selected)
        return selected

    def events(self) -> tuple[QueuedEvent, ...]:
        """Return a stable inspection view without mutating scheduling order."""

        return tuple(sorted(self._events))

    def push(self, event: QueuedEvent, *, rejectable: bool = True) -> bool:
        if len(self._events) >= self.max_pending_events:
            if self.reject_newest and rejectable:
                return False
            raise RuntimeError(
                "graph queue exceeded "
                f"max_pending_events={self.max_pending_events}"
            )
        heapq.heappush(self._events, event)
        self._component_counts[event.component_id] += 1
        self._component_kind_counts[(event.component_id, event.kind)] += 1
        return True

    def _decrement(self, event: QueuedEvent) -> None:
        self._component_counts[event.component_id] -= 1
        if self._component_counts[event.component_id] == 0:
            del self._component_counts[event.component_id]
        key = (event.component_id, event.kind)
        self._component_kind_counts[key] -= 1
        if self._component_kind_counts[key] == 0:
            del self._component_kind_counts[key]
