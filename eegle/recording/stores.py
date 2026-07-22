"""Format-neutral protocol for archival or execution sample stores."""

from __future__ import annotations

from typing import Protocol

from eegle.recording.artifacts import ArtifactReference
from eegle.streams.channels import StreamSpec
from eegle.streams.packets import DenseSampleBatch, MetadataEvent, SparseEventBatch


class SampleStore(Protocol):
    def open_stream(self, stream: StreamSpec) -> None:
        ...

    def append(self, packet: DenseSampleBatch | SparseEventBatch | MetadataEvent) -> None:
        ...

    def close(self) -> ArtifactReference:
        ...
