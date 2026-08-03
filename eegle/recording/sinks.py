"""Evidence sinks used by the semantic engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eegle.recording.evidence import EvidenceRecord


@dataclass(slots=True)
class InMemoryEvidenceSink:
    """Append-only evidence sink for tests, simulation, and composition."""

    _records: list[EvidenceRecord] = field(default_factory=list)

    def append(self, record: EvidenceRecord) -> None:
        if not isinstance(record, EvidenceRecord):
            raise TypeError("in-memory evidence sink requires EvidenceRecord values")
        if self._records and record.sequence != self._records[-1].sequence + 1:
            raise ValueError("evidence sequences must be contiguous")
        if not self._records and record.sequence != 0:
            raise ValueError("the first evidence sequence must be zero")
        self._records.append(record)

    def restore_prefix(self, records: tuple[EvidenceRecord, ...]) -> None:
        """Seed a verified immutable prefix before resumed appends."""

        if self._records:
            raise ValueError("cannot restore an evidence prefix into a non-empty sink")
        for expected, record in enumerate(records):
            if not isinstance(record, EvidenceRecord):
                raise TypeError("evidence prefix requires EvidenceRecord values")
            if record.sequence != expected:
                raise ValueError("evidence prefix sequences must be contiguous from zero")
        self._records.extend(records)

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records)


@dataclass(slots=True)
class InMemoryRecordSink:
    """Dependency-light observer sink for arbitrary typed runtime records."""

    _records: list[Any] = field(default_factory=list)
    retention_limit: int | None = None
    total_count: int = 0

    def __post_init__(self) -> None:
        if self.retention_limit is not None:
            self.retention_limit = int(self.retention_limit)
            if self.retention_limit < 0:
                raise ValueError("record sink retention_limit cannot be negative")

    def append(self, record: Any) -> None:
        if not callable(getattr(record, "to_payload", None)):
            raise TypeError("record sink values must expose an explicit to_payload() contract")
        self.total_count += 1
        if self.retention_limit is None:
            self._records.append(record)
        elif self.retention_limit > 0:
            self._records.append(record)
            if len(self._records) > self.retention_limit:
                del self._records[0]

    @property
    def records(self) -> tuple[Any, ...]:
        return tuple(self._records)
