"""Small source protocol shared by live, simulated, and replay adapters."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from eegle.streams.channels import StreamSpec
from eegle.streams.packets import Packet


@runtime_checkable
class Source(Protocol):
    @property
    def stream_spec(self) -> StreamSpec:
        ...

    def read(self) -> Packet | None:
        ...

    def close(self) -> None:
        ...
