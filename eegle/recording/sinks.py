"""Evidence sinks used by the semantic engine."""

from __future__ import annotations

from dataclasses import dataclass, field

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
