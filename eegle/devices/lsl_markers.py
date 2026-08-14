"""Independent LSL marker receipt evidence for acquisition sessions."""

from __future__ import annotations

import csv
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, monotonic_ns, sleep
from typing import Any


@dataclass
class MarkerReceiptSummary:
    status: str
    source_id: str
    csv_file: str
    metadata_file: str
    received_count: int = 0
    first_lsl_timestamp: float | None = None
    last_lsl_timestamp: float | None = None
    drain_expected_count: int | None = None
    drain_completed: bool | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_id": self.source_id,
            "csv_file": self.csv_file,
            "metadata_file": self.metadata_file,
            "received_count": self.received_count,
            "first_lsl_timestamp": self.first_lsl_timestamp,
            "last_lsl_timestamp": self.last_lsl_timestamp,
            "drain_expected_count": self.drain_expected_count,
            "drain_completed": self.drain_completed,
            "error": self.error,
        }


class LslMarkerReceiptRecorder:
    """Subscribe to one run-specific marker outlet and persist what LSL delivered."""

    def __init__(
        self,
        source_id: str,
        csv_file: str | Path,
        metadata_file: str | Path,
        *,
        discovery_timeout_seconds: float = 5.0,
    ) -> None:
        self.source_id = str(source_id)
        self.csv_file = Path(csv_file)
        self.metadata_file = Path(metadata_file)
        self.discovery_timeout_seconds = max(0.1, float(discovery_timeout_seconds))
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._summary = MarkerReceiptSummary(
            status="initialized",
            source_id=self.source_id,
            csv_file=str(self.csv_file),
            metadata_file=str(self.metadata_file),
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("marker receipt recorder is already running")
        self.csv_file.parent.mkdir(parents=True, exist_ok=True)
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._record, name="lsl-marker-receipt", daemon=True)
        self._thread.start()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        wait_seconds = self.discovery_timeout_seconds + 1.0 if timeout is None else float(timeout)
        return self._ready.wait(timeout=wait_seconds)

    def snapshot(self) -> dict[str, Any]:
        return self._summary.as_dict()

    def wait_for_count(self, expected_count: int, *, timeout: float = 2.0) -> bool:
        """Wait until every successfully emitted marker has reached this inlet."""

        expected = max(0, int(expected_count))
        self._summary.drain_expected_count = expected
        deadline = monotonic() + max(0.0, float(timeout))
        while self._summary.received_count < expected and monotonic() < deadline:
            if self._summary.status != "recording":
                break
            sleep(0.01)
        completed = self._summary.received_count >= expected
        self._summary.drain_completed = completed
        return completed

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                self._summary.status = "failed"
                self._summary.error = "marker receipt recorder did not stop within 5 seconds"
        self._write_metadata()
        return self._summary.as_dict()

    def close(self) -> None:
        summary = self.stop()
        if summary.get("status") != "stopped":
            raise RuntimeError(str(summary.get("error") or "marker receipt recorder did not stop cleanly"))

    def _record(self) -> None:
        inlet: Any | None = None
        try:
            import pylsl

            infos = pylsl.resolve_byprop(
                "source_id",
                self.source_id,
                minimum=1,
                timeout=self.discovery_timeout_seconds,
            )
            if len(infos) != 1:
                raise RuntimeError(
                    f"expected exactly one marker stream for source_id={self.source_id}, found {len(infos)}"
                )
            inlet = pylsl.StreamInlet(
                infos[0],
                max_buflen=60,
                recover=False,
                processing_flags=int(getattr(pylsl, "proc_none", 0)),
            )
            inlet.open_stream(timeout=self.discovery_timeout_seconds)
            with self.csv_file.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["marker_label", "lsl_timestamp", "local_received_lsl_timestamp"])
                handle.flush()
                self._summary.status = "recording"
                self._ready.set()
                while not self._stop.is_set():
                    sample, timestamp = inlet.pull_sample(timeout=0.2)
                    if not sample:
                        continue
                    if len(sample) != 1:
                        raise RuntimeError(f"marker stream sample width changed: expected 1, got {len(sample)}")
                    marker_timestamp = float(timestamp)
                    local_received = float(pylsl.local_clock())
                    writer.writerow([str(sample[0]), f"{marker_timestamp:.9f}", f"{local_received:.9f}"])
                    handle.flush()
                    self._summary.received_count += 1
                    if self._summary.first_lsl_timestamp is None:
                        self._summary.first_lsl_timestamp = marker_timestamp
                    self._summary.last_lsl_timestamp = marker_timestamp
            self._summary.status = "stopped"
        except Exception as exc:
            self._summary.status = "failed"
            self._summary.error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            if inlet is not None:
                try:
                    inlet.close_stream()
                except Exception:
                    pass
            self._write_metadata()

    def _write_metadata(self) -> None:
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.metadata_file.with_name(
            f".{self.metadata_file.name}.{os.getpid()}.{monotonic_ns()}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(self._summary.as_dict(), handle, indent=2, sort_keys=True)
                handle.write("\n")
            last_error: OSError | None = None
            for delay in (0.0, 0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8):
                if delay:
                    sleep(delay)
                try:
                    temporary.replace(self.metadata_file)
                    return
                except OSError as exc:
                    if not isinstance(exc, PermissionError) and getattr(exc, "winerror", None) not in {5, 32, 33}:
                        raise
                    last_error = exc
            if last_error is not None:
                raise last_error
        finally:
            temporary.unlink(missing_ok=True)
