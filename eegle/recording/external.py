"""Verification contracts for content-addressed external artifacts."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from eegle.recording.artifacts import ArtifactReference


class ExternalVerificationStatus(str, Enum):
    REFERENCE_ONLY = "reference_only"
    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class ExternalArtifactVerification:
    artifact_id: str
    status: ExternalVerificationStatus
    expected_digest: str
    expected_size_bytes: int
    observed_digest: str | None = None
    observed_size_bytes: int | None = None
    message: str | None = None


@runtime_checkable
class ExternalArtifactVerifier(Protocol):
    """Verify an external reference without importing it into the session."""

    def verify(self, reference: ArtifactReference) -> ExternalArtifactVerification: ...


class LocalFileArtifactVerifier:
    """Verify content-addressed ``file:`` references on the current host."""

    def verify(self, reference: ArtifactReference) -> ExternalArtifactVerification:
        if reference.embedded:
            raise ValueError("LocalFileArtifactVerifier only accepts external artifacts")
        parsed = urlparse(reference.uri)
        if parsed.scheme != "file" or (parsed.netloc and parsed.netloc != "localhost"):
            return ExternalArtifactVerification(
                reference.artifact_id,
                ExternalVerificationStatus.UNAVAILABLE,
                reference.digest,
                reference.size_bytes,
                message=f"unsupported external URI for local verification: {parsed.scheme or 'none'}",
            )
        path_value = url2pathname(unquote(parsed.path))
        if os.name == "nt" and path_value.startswith("\\") and len(path_value) > 2:
            if path_value[2] == ":":
                path_value = path_value[1:]
        path = Path(path_value)
        if not path.is_file():
            return ExternalArtifactVerification(
                reference.artifact_id,
                ExternalVerificationStatus.UNAVAILABLE,
                reference.digest,
                reference.size_bytes,
                message="external file is unavailable",
            )
        observed_digest, observed_size = _hash_file(path)
        if observed_size != reference.size_bytes or observed_digest != reference.digest:
            return ExternalArtifactVerification(
                reference.artifact_id,
                ExternalVerificationStatus.MISMATCH,
                reference.digest,
                reference.size_bytes,
                observed_digest,
                observed_size,
                "external file size or digest does not match its reference",
            )
        return ExternalArtifactVerification(
            reference.artifact_id,
            ExternalVerificationStatus.VERIFIED,
            reference.digest,
            reference.size_bytes,
            observed_digest,
            observed_size,
        )


def reference_only(reference: ArtifactReference) -> ExternalArtifactVerification:
    return ExternalArtifactVerification(
        reference.artifact_id,
        ExternalVerificationStatus.REFERENCE_ONLY,
        reference.digest,
        reference.size_bytes,
        message="external content was not available to this verification pass",
    )


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}", size
