"""Lab Streaming Layer helpers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class LslStream:
    name: str
    type: str
    channel_count: int
    nominal_srate: float
    source_id: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "channel_count": self.channel_count,
            "nominal_srate": self.nominal_srate,
            "source_id": self.source_id,
        }


def resolve_streams(wait_time: float = 1.0) -> tuple[list[LslStream], str | None]:
    try:
        import pylsl
    except Exception as exc:  # pragma: no cover - depends on local liblsl install
        return [], f"pylsl import failed: {type(exc).__name__}: {exc}"

    try:
        infos = pylsl.resolve_streams(wait_time=wait_time)
    except Exception as exc:  # pragma: no cover - depends on live LSL network
        return [], f"LSL stream resolution failed: {type(exc).__name__}: {exc}"

    streams = [
        LslStream(
            name=info.name(),
            type=info.type(),
            channel_count=info.channel_count(),
            nominal_srate=info.nominal_srate(),
            source_id=info.source_id(),
        )
        for info in infos
    ]
    return streams, None


class NullMarkerOutlet:
    """Marker outlet used in dry-runs and tests."""

    def __init__(self, reason: str = "marker outlet disabled") -> None:
        self.reason = reason

    def push(self, label: str, timestamp: float | None = None) -> None:
        return None

    def close(self) -> None:
        return None


def lsl_local_clock() -> float | None:
    """Return the current LSL clock value when pylsl is available."""
    try:
        import pylsl
    except Exception:  # pragma: no cover - depends on local liblsl install
        return None
    try:
        return float(pylsl.local_clock())
    except Exception:  # pragma: no cover - depends on local liblsl install
        return None


class LslMarkerOutlet:
    """Small marker outlet wrapper for task event markers."""

    def __init__(self, name: str, stream_type: str = "Markers", source_id: str = "closedloop-markers") -> None:
        import pylsl

        info = pylsl.StreamInfo(name, stream_type, 1, 0, "string", source_id)
        self._outlet = pylsl.StreamOutlet(info)
        self.name = name
        self.stream_type = stream_type
        self.source_id = source_id

    def push(self, label: str, timestamp: float | None = None) -> None:
        if self._outlet is None:
            raise RuntimeError("marker outlet is closed")
        if timestamp is None:
            self._outlet.push_sample([label])
        else:
            self._outlet.push_sample([label], timestamp=timestamp)

    def close(self) -> None:
        # pylsl closes the native outlet when the Python object is released.
        self._outlet = None


def session_marker_source_id(session_dir: str | Path) -> str:
    """Return a stable marker source id unique to one task session."""
    root = Path(session_dir).expanduser().resolve()
    run_stamp = root.parent.name
    path_digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
    return f"closedloop-markers-{run_stamp}-{root.name}-{path_digest}"


def lsl_processing_flags(pylsl: Any, *, dejitter: bool) -> int:
    """Return inlet flags that translate remote clocks into the local LSL clock."""
    flags = int(getattr(pylsl, "proc_clocksync", 0)) | int(getattr(pylsl, "proc_monotonize", 0))
    if dejitter:
        flags |= int(getattr(pylsl, "proc_dejitter", 0))
    return flags


def inlet_time_correction(inlet: Any, timeout: float = 1.0) -> float | None:
    """Read the current remote-to-local clock correction without failing startup."""
    try:
        return float(inlet.time_correction(timeout=timeout))
    except Exception:
        return None
