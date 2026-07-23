"""Read-only-compatible framing for historical CLRE1 engine captures.

The format is retained only so selected existing sessions remain readable.
New execution capture uses :mod:`eegle.recording.stores`.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np


LEGACY_ENGINE_CAPTURE_MAGIC = b"CLRE1\n"


class LegacyEngineCaptureWriter:
    def __init__(self, path: str | Path, header: dict[str, Any]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("wb")
        encoded = json.dumps(header, sort_keys=True).encode("utf-8")
        self._handle.write(LEGACY_ENGINE_CAPTURE_MAGIC)
        self._handle.write(struct.pack("<I", len(encoded)))
        self._handle.write(encoded)

    def write_eeg(self, timestamps: np.ndarray, data: np.ndarray) -> None:
        ts = np.asarray(timestamps, dtype="<f8")
        values = np.asarray(data, dtype="<f8")
        if ts.size == 0:
            return
        if values.ndim != 2 or values.shape[0] != ts.size:
            raise ValueError(
                f"EEG capture frame shape mismatch: timestamps={ts.size}, data_shape={values.shape}"
            )
        self._handle.write(
            b"".join(
                [
                    b"E",
                    struct.pack("<II", ts.size, values.shape[1]),
                    ts.tobytes(order="C"),
                    values.tobytes(order="C"),
                ]
            )
        )
        self.flush()

    def write_marker(self, marker: Any) -> None:
        payload = json.dumps(
            {
                "label": marker.label,
                "timestamp": marker.timestamp,
                "timebase": marker.timebase,
                "source": marker.source,
            },
            sort_keys=True,
        ).encode("utf-8")
        self._handle.write(b"M" + struct.pack("<I", len(payload)) + payload)
        self.flush()

    def flush(self) -> None:
        self._handle.flush()

    def close(self) -> None:
        self.flush()
        self._handle.close()


def read_legacy_engine_capture(
    path: str | Path,
    *,
    marker_factory: Callable[..., Any],
) -> tuple[dict[str, Any], Iterator[tuple[str, Any]]]:
    target = Path(path)
    handle = target.open("rb")
    if handle.read(len(LEGACY_ENGINE_CAPTURE_MAGIC)) != LEGACY_ENGINE_CAPTURE_MAGIC:
        handle.close()
        raise ValueError("invalid realtime engine capture magic")
    header_size = struct.unpack("<I", _read_exact(handle, 4, "capture header size"))[0]
    header = json.loads(_read_exact(handle, header_size, "capture header").decode("utf-8"))

    def records() -> Iterator[tuple[str, Any]]:
        try:
            while True:
                kind = handle.read(1)
                if not kind:
                    break
                if kind == b"E":
                    sample_count, channel_count = struct.unpack(
                        "<II", _read_exact(handle, 8, "EEG frame header")
                    )
                    timestamps = np.frombuffer(
                        _read_exact(
                            handle,
                            sample_count * 8,
                            f"EEG timestamp payload ({sample_count} samples)",
                        ),
                        dtype="<f8",
                    ).copy()
                    data = np.frombuffer(
                        _read_exact(
                            handle,
                            sample_count * channel_count * 8,
                            f"EEG sample payload ({sample_count}x{channel_count})",
                        ),
                        dtype="<f8",
                    ).copy()
                    yield "eeg", (timestamps, data.reshape(sample_count, channel_count))
                elif kind == b"M":
                    size = struct.unpack("<I", _read_exact(handle, 4, "marker frame header"))[0]
                    row = json.loads(_read_exact(handle, size, "marker payload").decode("utf-8"))
                    yield "marker", marker_factory(
                        label=str(row["label"]),
                        timestamp=float(row["timestamp"]),
                        timebase=str(row.get("timebase", "lsl")),
                        source=str(row.get("source", "capture")),
                    )
                else:
                    raise ValueError(f"unknown realtime capture frame {kind!r}")
        finally:
            handle.close()

    return header, records()


def _read_exact(handle: Any, size: int, label: str) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise ValueError(
            f"truncated realtime engine capture: {label} expected {size} bytes, got {len(data)}"
        )
    return data
