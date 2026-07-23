"""Append-oriented, checksummed canonical JSON evidence framing v1."""

from __future__ import annotations

import hashlib
import json
import os
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping

from eegle.compiler.lock import canonical_json_bytes
from eegle.recording.evidence import EvidenceRecord


MAGIC = b"EEGLE-EVIDENCE\x00\x01\n"
_LENGTH = struct.Struct(">Q")
_DIGEST_SIZE = hashlib.sha256().digest_size
_MAX_FRAME_BYTES = 64 * 1024 * 1024


class EvidenceIntegrityError(ValueError):
    pass


class TruncatedEvidenceError(EvidenceIntegrityError):
    def __init__(
        self,
        message: str,
        *,
        last_complete_offset: int,
        frame_offset: int | None = None,
        expected_bytes: int | None = None,
        observed_bytes: int | None = None,
    ) -> None:
        self.last_complete_offset = last_complete_offset
        self.frame_offset = frame_offset
        self.expected_bytes = expected_bytes
        self.observed_bytes = observed_bytes
        super().__init__(message)


class IntegrityStatus(str, Enum):
    VALID = "valid"
    RECOVERABLE = "recoverable"
    UNRECOVERABLE = "unrecoverable"


class IntegrityIssueCode(str, Enum):
    TRUNCATED_HEADER = "truncated_header"
    INVALID_HEADER = "invalid_header"
    TRUNCATED_FRAME_LENGTH = "truncated_frame_length"
    TRUNCATED_FRAME_PAYLOAD = "truncated_frame_payload"
    FRAME_TOO_LARGE = "frame_too_large"
    CHECKSUM_MISMATCH = "checksum_mismatch"
    INVALID_JSON = "invalid_json"
    NONCANONICAL_JSON = "noncanonical_json"
    RECORD_HASH_MISMATCH = "record_hash_mismatch"
    SEQUENCE_GAP = "sequence_gap"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_SIZE_MISMATCH = "artifact_size_mismatch"
    ARTIFACT_DIGEST_MISMATCH = "artifact_digest_mismatch"
    MANIFEST_MISMATCH = "manifest_mismatch"


@dataclass(frozen=True, slots=True)
class IntegrityIssue:
    code: IntegrityIssueCode
    message: str
    artifact_id: str | None = None
    frame_offset: int | None = None
    last_complete_offset: int | None = None
    expected: str | int | None = None
    observed: str | int | None = None
    recoverable: bool = False


@dataclass(frozen=True, slots=True)
class FramingInspection:
    status: IntegrityStatus
    payloads: tuple[dict[str, Any], ...]
    last_complete_offset: int
    file_size: int
    issues: tuple[IntegrityIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return self.status == IntegrityStatus.VALID


class FramedEvidenceWriter:
    def __init__(self, path: str | Path, *, durable: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.durable = bool(durable)
        if self.path.exists() and self.path.stat().st_size:
            tuple(iter_framed_payloads(self.path))
        self._handle = self.path.open("ab")
        if self._handle.tell() == 0:
            self._handle.write(MAGIC)
            self._flush()

    def append(self, record: EvidenceRecord | Mapping[str, Any]) -> int:
        payload = record.to_payload() if isinstance(record, EvidenceRecord) else dict(record)
        encoded = canonical_json_bytes(payload)
        if len(encoded) > _MAX_FRAME_BYTES:
            raise ValueError(f"evidence frame exceeds {_MAX_FRAME_BYTES} bytes")
        offset = self._handle.tell()
        self._handle.write(_LENGTH.pack(len(encoded)))
        self._handle.write(encoded)
        self._handle.write(hashlib.sha256(encoded).digest())
        self._flush()
        return offset

    def _flush(self) -> None:
        self._handle.flush()
        if self.durable:
            os.fsync(self._handle.fileno())

    def close(self) -> None:
        if not self._handle.closed:
            self._flush()
            self._handle.close()

    def __enter__(self) -> "FramedEvidenceWriter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def iter_framed_payloads(
    path: str | Path,
    *,
    allow_truncated_final_frame: bool = False,
) -> Iterator[dict[str, Any]]:
    source = Path(path)
    with source.open("rb") as handle:
        header = handle.read(len(MAGIC))
        if header != MAGIC:
            raise EvidenceIntegrityError("unsupported or missing evidence log header")
        last_complete = handle.tell()
        while True:
            length_bytes = handle.read(_LENGTH.size)
            if not length_bytes:
                return
            if len(length_bytes) != _LENGTH.size:
                if allow_truncated_final_frame:
                    return
                raise TruncatedEvidenceError(
                    f"truncated evidence frame length at {last_complete}: "
                    f"expected {_LENGTH.size} bytes, observed {len(length_bytes)}",
                    last_complete_offset=last_complete,
                    frame_offset=last_complete,
                    expected_bytes=_LENGTH.size,
                    observed_bytes=len(length_bytes),
                )
            length = _LENGTH.unpack(length_bytes)[0]
            if length > _MAX_FRAME_BYTES:
                raise EvidenceIntegrityError(f"evidence frame length {length} exceeds limit")
            encoded = handle.read(length)
            digest = handle.read(_DIGEST_SIZE)
            if len(encoded) != length or len(digest) != _DIGEST_SIZE:
                if allow_truncated_final_frame:
                    return
                raise TruncatedEvidenceError(
                    f"truncated evidence frame payload at {last_complete}: expected "
                    f"{length + _DIGEST_SIZE} bytes, observed {len(encoded) + len(digest)}",
                    last_complete_offset=last_complete,
                    frame_offset=last_complete,
                    expected_bytes=length + _DIGEST_SIZE,
                    observed_bytes=len(encoded) + len(digest),
                )
            observed = hashlib.sha256(encoded).digest()
            if observed != digest:
                raise EvidenceIntegrityError(f"evidence frame checksum mismatch at {last_complete}")
            try:
                payload = json.loads(encoded.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EvidenceIntegrityError(
                    f"invalid canonical JSON evidence frame at {last_complete}"
                ) from exc
            if not isinstance(payload, dict):
                raise EvidenceIntegrityError("evidence frame payload must be a JSON object")
            if canonical_json_bytes(payload) != encoded:
                raise EvidenceIntegrityError(
                    f"evidence frame at {last_complete} is valid JSON but not canonical"
                )
            last_complete = handle.tell()
            yield payload


def inspect_framed_payloads(path: str | Path) -> FramingInspection:
    """Inspect a log without hiding a partial final frame.

    A partial final length or payload is recoverable by truncating to
    ``last_complete_offset``. Header, checksum, canonicalization, and interior
    corruption are unrecoverable because EEGle cannot prove the next boundary.
    """

    source = Path(path)
    file_size = source.stat().st_size
    payloads: list[dict[str, Any]] = []
    try:
        payloads.extend(iter_framed_payloads(source))
    except TruncatedEvidenceError as exc:
        code = (
            IntegrityIssueCode.TRUNCATED_FRAME_LENGTH
            if "frame length" in str(exc)
            else IntegrityIssueCode.TRUNCATED_FRAME_PAYLOAD
        )
        return FramingInspection(
            status=IntegrityStatus.RECOVERABLE,
            payloads=tuple(iter_framed_payloads(source, allow_truncated_final_frame=True)),
            last_complete_offset=exc.last_complete_offset,
            file_size=file_size,
            issues=(
                IntegrityIssue(
                    code=code,
                    message=str(exc),
                    frame_offset=exc.frame_offset,
                    last_complete_offset=exc.last_complete_offset,
                    expected=exc.expected_bytes,
                    observed=exc.observed_bytes,
                    recoverable=True,
                ),
            ),
        )
    except EvidenceIntegrityError as exc:
        message = str(exc)
        if "header" in message:
            code = (
                IntegrityIssueCode.TRUNCATED_HEADER
                if file_size < len(MAGIC)
                else IntegrityIssueCode.INVALID_HEADER
            )
        elif "checksum" in message:
            code = IntegrityIssueCode.CHECKSUM_MISMATCH
        elif "exceeds limit" in message:
            code = IntegrityIssueCode.FRAME_TOO_LARGE
        elif "not canonical" in message:
            code = IntegrityIssueCode.NONCANONICAL_JSON
        else:
            code = IntegrityIssueCode.INVALID_JSON
        return FramingInspection(
            status=IntegrityStatus.UNRECOVERABLE,
            payloads=tuple(payloads),
            last_complete_offset=len(MAGIC) if file_size >= len(MAGIC) else 0,
            file_size=file_size,
            issues=(IntegrityIssue(code=code, message=message),),
        )
    return FramingInspection(
        status=IntegrityStatus.VALID,
        payloads=tuple(payloads),
        last_complete_offset=file_size,
        file_size=file_size,
    )


def recover_framed_prefix(source: str | Path, destination: str | Path) -> FramingInspection:
    """Copy the proven complete prefix to a new file; never mutate the source."""

    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    if source_path == destination_path:
        raise ValueError("recovery destination must differ from the source")
    inspection = inspect_framed_payloads(source_path)
    if inspection.status == IntegrityStatus.UNRECOVERABLE:
        issue = inspection.issues[0].message if inspection.issues else "unknown integrity issue"
        raise EvidenceIntegrityError(f"cannot recover an unproven frame prefix: {issue}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with source_path.open("rb") as reader, destination_path.open("xb") as writer:
        remaining = inspection.last_complete_offset
        while remaining:
            chunk = reader.read(min(1024 * 1024, remaining))
            if not chunk:
                raise EvidenceIntegrityError("source ended before the verified recovery boundary")
            writer.write(chunk)
            remaining -= len(chunk)
    recovered = inspect_framed_payloads(destination_path)
    if recovered.status != IntegrityStatus.VALID:
        raise EvidenceIntegrityError("recovered frame prefix did not verify")
    return recovered
