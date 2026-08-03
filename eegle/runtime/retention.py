"""Bounded phase-result retention for long-running executions."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Generic, TypeVar

T = TypeVar("T")


class PhaseRecordBuffer(Generic[T]):
    """Count every runtime record while retaining only requested details."""

    def __init__(
        self,
        *,
        retain_all: bool,
        retain_when: Callable[[T], bool] | None = None,
    ) -> None:
        self._retain_all = bool(retain_all)
        self._retain_when = retain_when
        self._records: list[T] = []
        self._count = 0

    def append(self, value: T) -> None:
        self._count += 1
        if self._retain_all or (
            self._retain_when is not None and self._retain_when(value)
        ):
            self._records.append(value)

    def __len__(self) -> int:
        return self._count

    def __iter__(self) -> Iterator[T]:
        return iter(self._records)

    @property
    def retained(self) -> tuple[T, ...]:
        return tuple(self._records)


__all__ = ["PhaseRecordBuffer"]
