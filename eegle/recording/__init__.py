"""Sessions, evidence bundles, content-addressed artifacts, and sample stores."""

from eegle.recording.artifacts import (
    ArtifactEntry,
    ArtifactLineage,
    ArtifactManifest,
    ArtifactReference,
    ArtifactStore,
    Sensitivity,
)
from eegle.recording.bundles import (
    BundleIntegrityReport,
    ComponentStateSnapshot,
    EvidenceBundle,
    EvidenceReader,
    EvidenceWriter,
    persist_engine_run,
)
from eegle.recording.compat import SessionPaths
from eegle.recording.evidence import EvidenceBundleManifest, EvidenceRecord, EvidenceStatus
from eegle.recording.framing import (
    EvidenceIntegrityError,
    FramingInspection,
    FramedEvidenceWriter,
    IntegrityIssue,
    IntegrityIssueCode,
    IntegrityStatus,
    TruncatedEvidenceError,
    inspect_framed_payloads,
    iter_framed_payloads,
    recover_framed_prefix,
)
from eegle.recording.ledgers import (
    EvidenceLedgerReadResult,
    EvidenceLedgerWriter,
    read_evidence_ledger,
)
from eegle.recording.session import Session, SessionManifest, SessionStatus
from eegle.recording.stores import (
    FramedSampleStore,
    SampleStore,
    SampleStorePurpose,
    SampleStoreReadResult,
    read_framed_sample_store,
)
from eegle.recording.sinks import InMemoryEvidenceSink


__all__ = [
    "ArtifactEntry",
    "ArtifactLineage",
    "ArtifactManifest",
    "ArtifactReference",
    "ArtifactStore",
    "BundleIntegrityReport",
    "ComponentStateSnapshot",
    "EvidenceBundle",
    "EvidenceBundleManifest",
    "EvidenceIntegrityError",
    "EvidenceLedgerReadResult",
    "EvidenceLedgerWriter",
    "EvidenceReader",
    "EvidenceRecord",
    "EvidenceStatus",
    "EvidenceWriter",
    "FramedSampleStore",
    "FramingInspection",
    "FramedEvidenceWriter",
    "InMemoryEvidenceSink",
    "IntegrityIssue",
    "IntegrityIssueCode",
    "IntegrityStatus",
    "SampleStore",
    "SampleStorePurpose",
    "SampleStoreReadResult",
    "Sensitivity",
    "Session",
    "SessionManifest",
    "SessionPaths",
    "SessionStatus",
    "TruncatedEvidenceError",
    "inspect_framed_payloads",
    "iter_framed_payloads",
    "read_evidence_ledger",
    "read_framed_sample_store",
    "recover_framed_prefix",
    "persist_engine_run",
]
