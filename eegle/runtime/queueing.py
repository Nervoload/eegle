"""Bounded deterministic event queue for the plan-owned executor."""

from __future__ import annotations

from dataclasses import dataclass, field
import heapq
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
        return heapq.heappop(self._events)

    def count_component(self, component_id: str) -> int:
        return sum(value.component_id == component_id for value in self._events)

    def push(self, event: QueuedEvent, *, rejectable: bool = True) -> bool:
        if len(self._events) >= self.max_pending_events:
            if self.reject_newest and rejectable:
                return False
            raise RuntimeError(
                "graph queue exceeded "
                f"max_pending_events={self.max_pending_events}"
            )
        heapq.heappush(self._events, event)
        return True
