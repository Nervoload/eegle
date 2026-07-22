"""A bounded sequence-aware buffer with explicit eviction accounting."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Generic, Iterator, TypeVar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BufferEntry(Generic[T]):
    sequence: int
    item: T


class BoundedBuffer(Generic[T]):
    def __init__(self, capacity: int) -> None:
        self.capacity = int(capacity)
        if self.capacity <= 0:
            raise ValueError("buffer capacity must be positive")
        self._entries: Deque[BufferEntry[T]] = deque()
        self.evicted_count = 0

    def append(self, sequence: int, item: T) -> BufferEntry[T] | None:
        normalized = int(sequence)
        if normalized < 0:
            raise ValueError("buffer sequence cannot be negative")
        if self._entries and normalized <= self._entries[-1].sequence:
            raise ValueError("buffer sequences must be strictly increasing")
        evicted = None
        if len(self._entries) == self.capacity:
            evicted = self._entries.popleft()
            self.evicted_count += 1
        self._entries.append(BufferEntry(normalized, item))
        return evicted

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[BufferEntry[T]]:
        return iter(tuple(self._entries))
