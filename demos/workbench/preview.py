"""Independent, read-only LSL preview inlets for the Workbench UI."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from time import monotonic
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from eegle.integrations.lsl import LslSource
from eegle.streams import DenseSampleBatch, SparseEventBatch


class _PreviewThread(QThread):
    payload = Signal(object)

    def __init__(
        self,
        eeg_config: Mapping[str, Any],
        marker_config: Mapping[str, Any],
        labels: tuple[str, ...],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._eeg_config = dict(eeg_config)
        self._marker_config = dict(marker_config)
        self._labels = labels[:8]
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        eeg: LslSource | None = None
        markers: LslSource | None = None
        started = monotonic()
        last_emit = 0.0
        sample_count = 0
        times: deque[float] = deque(maxlen=10000)
        channels = [deque(maxlen=10000) for _ in self._labels]
        events: deque[dict[str, Any]] = deque(maxlen=80)
        try:
            eeg_config = dict(self._eeg_config)
            marker_config = dict(self._marker_config)
            eeg_config.update({"pull_timeout_seconds": 0.02, "max_samples": 256})
            marker_config.update({"pull_timeout_seconds": 0.01, "max_samples": 64})
            eeg = LslSource(eeg_config)
            markers = LslSource(marker_config)
            self.payload.emit({"type": "status", "status": "connected"})
            while not self._stop.is_set():
                dense = eeg.read()
                if isinstance(dense, DenseSampleBatch):
                    values = dense.values.tolist()
                    now = monotonic()
                    period = dense.sample_period_seconds
                    rate = (
                        1.0 / period
                        if period is not None and period > 0
                        else float(
                            dict(self._eeg_config.get("stream_spec") or {}).get(
                                "sample_rate_hz",
                                1000.0,
                            )
                        )
                    )
                    first = now - max(len(values) - 1, 0) / rate
                    for row_index, row in enumerate(values):
                        times.append(first + row_index / rate)
                        for index, series in enumerate(channels):
                            series.append(float(row[index]))
                    sample_count += len(values)
                sparse = markers.read()
                if isinstance(sparse, SparseEventBatch):
                    for event in sparse.events:
                        events.append(
                            {
                                "kind": event.kind,
                                "time": event.event_time.seconds,
                            }
                        )
                now = monotonic()
                if now - last_emit >= 0.05:
                    cutoff = now - 10.0
                    while times and times[0] < cutoff:
                        times.popleft()
                        for series in channels:
                            if series:
                                series.popleft()
                    stride = max(1, len(times) // 1200)
                    self.payload.emit(
                        {
                            "type": "preview",
                            "labels": self._labels,
                            "times": tuple(value - now for value in tuple(times)[::stride]),
                            "channels": tuple(
                                tuple(series)[::stride] for series in channels
                            ),
                            "events": tuple(events),
                            "effective_rate_hz": sample_count / max(now - started, 1e-9),
                        }
                    )
                    last_emit = now
        except Exception as exc:  # noqa: BLE001 - preview must not affect recording
            self.payload.emit(
                {
                    "type": "status",
                    "status": "disconnected",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            for source in (eeg, markers):
                if source is not None:
                    source.close()
            self.payload.emit({"type": "status", "status": "closed"})


class PreviewManager(QObject):
    """Own one disposable preview worker, separate from EEGle's runner source."""

    payload = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: _PreviewThread | None = None

    def start(
        self,
        eeg_config: Mapping[str, Any],
        marker_config: Mapping[str, Any],
        labels: tuple[str, ...],
    ) -> None:
        self.stop()
        self._thread = _PreviewThread(eeg_config, marker_config, labels, self)
        self._thread.payload.connect(self.payload)
        self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        self._thread = None
        if thread is None:
            return
        thread.request_stop()
        if not thread.wait(1500):
            self.payload.emit(
                {
                    "type": "status",
                    "status": "interrupted",
                    "message": "Preview inlet did not close within the bounded wait.",
                }
            )
