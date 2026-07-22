"""Evidence records, content-addressed artifacts, framing, and store protocols."""

from eegle.recording.artifacts import ArtifactReference, Sensitivity
from eegle.recording.evidence import EvidenceBundleManifest, EvidenceRecord, EvidenceStatus
from eegle.recording.framing import (
    EvidenceIntegrityError,
    FramedEvidenceWriter,
    TruncatedEvidenceError,
    iter_framed_payloads,
)
from eegle.recording.stores import SampleStore


__all__ = [
    "ArtifactReference",
    "EvidenceBundleManifest",
    "EvidenceIntegrityError",
    "EvidenceRecord",
    "EvidenceStatus",
    "FramedEvidenceWriter",
    "SampleStore",
    "Sensitivity",
    "TruncatedEvidenceError",
    "iter_framed_payloads",
]
