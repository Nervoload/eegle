"""Append-oriented, checksummed canonical JSON evidence framing v1."""

from __future__ import annotations

import hashlib
import json
import os
import struct
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
    def __init__(self, message: str, *, last_complete_offset: int) -> None:
        self.last_complete_offset = last_complete_offset
        super().__init__(message)


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
                    "truncated evidence frame length",
                    last_complete_offset=last_complete,
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
                    "truncated evidence frame payload",
                    last_complete_offset=last_complete,
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
