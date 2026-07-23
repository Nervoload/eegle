"""Typed append-only evidence ledgers built on the recording frame format."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from eegle.recording.evidence import EvidenceRecord
from eegle.recording.framing import (
    FramedEvidenceWriter,
    FramingInspection,
    IntegrityIssue,
    IntegrityIssueCode,
    IntegrityStatus,
    inspect_framed_payloads,
)


@dataclass(frozen=True, slots=True)
class EvidenceLedgerReadResult:
    records: tuple[EvidenceRecord, ...]
    integrity: FramingInspection


class EvidenceLedgerWriter:
    """A contiguous typed ledger suitable for direct engine evidence sinks."""

    def __init__(self, path: str | Path, *, durable: bool = False) -> None:
        self.path = Path(path)
        existing = read_evidence_ledger(self.path) if self.path.exists() else None
        if existing is not None and existing.integrity.status != IntegrityStatus.VALID:
            raise ValueError("cannot append to an evidence ledger that is not integrity-valid")
        self._last_sequence = (
            None if existing is None or not existing.records else existing.records[-1].sequence
        )
        self._writer = FramedEvidenceWriter(self.path, durable=durable)

    def append(self, record: EvidenceRecord) -> None:
        if not isinstance(record, EvidenceRecord):
            raise TypeError("evidence ledger requires EvidenceRecord values")
        expected = 0 if self._last_sequence is None else self._last_sequence + 1
        if record.sequence != expected:
            raise ValueError(
                f"evidence ledger sequence gap: expected {expected}, observed {record.sequence}"
            )
        self._writer.append(record)
        self._last_sequence = record.sequence

    def close(self) -> None:
        self._writer.close()

    def __enter__(self) -> "EvidenceLedgerWriter":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def read_evidence_ledger(path: str | Path) -> EvidenceLedgerReadResult:
    inspection = inspect_framed_payloads(path)
    records: list[EvidenceRecord] = []
    issues = list(inspection.issues)
    expected_sequence = 0
    for payload in inspection.payloads:
        try:
            record = EvidenceRecord.from_payload(payload)
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(
                IntegrityIssue(
                    code=IntegrityIssueCode.RECORD_HASH_MISMATCH,
                    message=str(exc),
                    recoverable=False,
                )
            )
            break
        if record.sequence != expected_sequence:
            issues.append(
                IntegrityIssue(
                    code=IntegrityIssueCode.SEQUENCE_GAP,
                    message=(
                        f"evidence ledger sequence gap: expected {expected_sequence}, "
                        f"observed {record.sequence}"
                    ),
                    expected=expected_sequence,
                    observed=record.sequence,
                    recoverable=False,
                )
            )
            break
        records.append(record)
        expected_sequence += 1
    status = inspection.status
    if any(not issue.recoverable for issue in issues):
        status = IntegrityStatus.UNRECOVERABLE
    normalized = FramingInspection(
        status=status,
        payloads=inspection.payloads,
        last_complete_offset=inspection.last_complete_offset,
        file_size=inspection.file_size,
        issues=tuple(issues),
    )
    return EvidenceLedgerReadResult(tuple(records), normalized)
