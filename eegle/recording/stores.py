"""Format-neutral sample-store contracts and a framed reference store."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from eegle._validation import require_identifier
from eegle.recording.artifacts import ArtifactReference, ArtifactStore, Sensitivity
from eegle.recording.framing import (
    FramedEvidenceWriter,
    FramingInspection,
    inspect_framed_payloads,
)
from eegle.streams.channels import ContentKind, StreamSpec
from eegle.streams.packets import (
    DENSE_SAMPLE_BATCH_SCHEMA,
    METADATA_EVENT_SCHEMA,
    SPARSE_EVENT_BATCH_SCHEMA,
    DenseSampleBatch,
    MetadataEvent,
    Packet,
    SparseEventBatch,
)


SAMPLE_STORE_FRAME_SCHEMA = "eegle.sample_store_frame.v1"
FRAMED_SAMPLE_MEDIA_TYPE = "application/vnd.eegle.samples-framed+json"


class SampleStorePurpose(str, Enum):
    EXECUTION_CAPTURE = "execution_capture"
    ARCHIVAL_RAW = "archival_raw"


@runtime_checkable
class SampleStore(Protocol):
    """Write dense, sparse, or metadata packets without prescribing a format."""

    @property
    def store_id(self) -> str:
        ...

    @property
    def purpose(self) -> SampleStorePurpose:
        ...

    def open_stream(self, stream: StreamSpec) -> None:
        ...

    def append(self, packet: DenseSampleBatch | SparseEventBatch | MetadataEvent) -> None:
        ...

    def close(self) -> ArtifactReference:
        ...


@dataclass(frozen=True, slots=True)
class SampleStoreReadResult:
    streams: tuple[StreamSpec, ...]
    packets: tuple[Packet, ...]
    integrity: FramingInspection


class FramedSampleStore:
    """Dependency-light reference store for exact admitted packet capture.

    It is deliberately not EEGle's universal raw format. Modality-native stores
    implement :class:`SampleStore` and return the same ``ArtifactReference``.
    """

    def __init__(
        self,
        artifact_store: ArtifactStore,
        *,
        store_id: str,
        purpose: SampleStorePurpose,
        namespace: str,
        relative_uri: str,
        sensitivity: Sensitivity = Sensitivity.RESTRICTED,
        durable: bool = False,
    ) -> None:
        self._artifact_store = artifact_store
        self._store_id = require_identifier(store_id, "store_id")
        self._purpose = SampleStorePurpose(purpose)
        self._namespace = require_identifier(namespace, "namespace")
        if any(part in {"", ".", ".."} for part in self._namespace.split("/")):
            raise ValueError("sample-store namespace contains an unsafe path segment")
        relative = Path(relative_uri)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ValueError("sample store path must be a safe session-relative path")
        self._path = artifact_store.root / relative
        self._sensitivity = Sensitivity(sensitivity)
        self._writer = FramedEvidenceWriter(self._path, durable=durable)
        self._streams: dict[tuple[str, int], StreamSpec] = {}
        self._closed_reference: ArtifactReference | None = None
        self._aborted = False

    @property
    def store_id(self) -> str:
        return self._store_id

    @property
    def purpose(self) -> SampleStorePurpose:
        return self._purpose

    def open_stream(self, stream: StreamSpec) -> None:
        self._require_open()
        key = (stream.stream_id, stream.revision)
        existing = self._streams.get(key)
        if existing is not None:
            if existing != stream:
                raise ValueError(f"stream revision is already registered with different metadata: {key}")
            return
        self._streams[key] = stream
        self._writer.append(
            {
                "schema": SAMPLE_STORE_FRAME_SCHEMA,
                "kind": "stream",
                "stream": stream.to_payload(),
            }
        )

    def append(self, packet: DenseSampleBatch | SparseEventBatch | MetadataEvent) -> None:
        self._require_open()
        key = (packet.stream_id, packet.stream_revision)
        stream = self._streams.get(key)
        if stream is None:
            raise ValueError(
                f"packet binds unregistered stream revision {packet.stream_id}@{packet.stream_revision}"
            )
        if isinstance(packet, DenseSampleBatch) and stream.content_kind != ContentKind.DENSE_SAMPLES:
            raise ValueError("dense packet requires a dense-sample stream declaration")
        if isinstance(packet, SparseEventBatch) and stream.content_kind != ContentKind.SPARSE_EVENTS:
            raise ValueError("sparse packet requires a sparse-event stream declaration")
        self._writer.append(
            {
                "schema": SAMPLE_STORE_FRAME_SCHEMA,
                "kind": "packet",
                "packet": packet.to_payload(),
            }
        )

    def append_all(self, packets: Iterable[Packet]) -> None:
        for packet in packets:
            self.append(packet)

    def close(self) -> ArtifactReference:
        if self._aborted:
            raise RuntimeError("aborted sample store cannot be finalized")
        if self._closed_reference is not None:
            return self._closed_reference
        self._writer.close()
        self._closed_reference = self._artifact_store.register_file(
            self._namespace,
            self.store_id,
            self.purpose.value,
            self._path,
            FRAMED_SAMPLE_MEDIA_TYPE,
            sensitivity=self._sensitivity,
            copy=False,
        )
        return self._closed_reference

    def __enter__(self) -> "FramedSampleStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.close()
        else:
            self._writer.close()
            self._aborted = True

    def _require_open(self) -> None:
        if self._aborted:
            raise RuntimeError("sample store was aborted")
        if self._closed_reference is not None:
            raise RuntimeError("sample store is closed")


def read_framed_sample_store(path: str | Path) -> SampleStoreReadResult:
    inspection = inspect_framed_payloads(path)
    streams: list[StreamSpec] = []
    packets: list[Packet] = []
    known: dict[tuple[str, int], StreamSpec] = {}
    for frame in inspection.payloads:
        if frame.get("schema") != SAMPLE_STORE_FRAME_SCHEMA:
            raise ValueError(f"unsupported sample-store frame schema: {frame.get('schema')}")
        kind = frame.get("kind")
        if kind == "stream":
            stream = StreamSpec.from_payload(frame["stream"])
            key = (stream.stream_id, stream.revision)
            if key in known and known[key] != stream:
                raise ValueError(f"conflicting stream declaration in sample store: {key}")
            if key not in known:
                known[key] = stream
                streams.append(stream)
            continue
        if kind != "packet":
            raise ValueError(f"unknown sample-store frame kind: {kind}")
        packet = _packet_from_payload(frame["packet"])
        key = (packet.stream_id, packet.stream_revision)
        stream = known.get(key)
        if stream is None:
            raise ValueError(f"sample-store packet precedes stream declaration: {key}")
        if isinstance(packet, DenseSampleBatch) and stream.content_kind != ContentKind.DENSE_SAMPLES:
            raise ValueError("dense packet is bound to a non-dense stream declaration")
        if isinstance(packet, SparseEventBatch) and stream.content_kind != ContentKind.SPARSE_EVENTS:
            raise ValueError("sparse packet is bound to a non-sparse stream declaration")
        packets.append(packet)
    return SampleStoreReadResult(tuple(streams), tuple(packets), inspection)


def _packet_from_payload(payload: dict[str, object]) -> Packet:
    schema = payload.get("schema")
    if schema == DENSE_SAMPLE_BATCH_SCHEMA:
        return DenseSampleBatch.from_payload(payload)
    if schema == SPARSE_EVENT_BATCH_SCHEMA:
        return SparseEventBatch.from_payload(payload)
    if schema == METADATA_EVENT_SCHEMA:
        return MetadataEvent.from_payload(payload)
    raise ValueError(f"unsupported packet schema in sample store: {schema}")
