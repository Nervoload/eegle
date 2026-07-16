"""Task-independent health checks for a managed raw EEG recorder."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import monotonic, time
from typing import Any

from eegle.workers.common import load_status


@dataclass(frozen=True)
class RecordingHealth:
    ok: bool
    reason: str | None
    status: dict[str, Any]


class RecorderHealthMonitor:
    """Detect recorder exit, stale heartbeats, and a stalled EEG sample count."""

    def __init__(
        self,
        status_file: str | Path,
        *,
        required: bool,
        stall_timeout_seconds: float = 5.0,
        status_stale_seconds: float = 5.0,
    ) -> None:
        self.status_file = Path(status_file)
        self.required = bool(required)
        self.stall_timeout_seconds = max(1.0, float(stall_timeout_seconds))
        self.status_stale_seconds = max(1.0, float(status_stale_seconds))
        self._last_sample_count: int | None = None
        self._last_progress_at = monotonic()

    def check(self) -> RecordingHealth:
        if not self.required:
            return RecordingHealth(True, None, {"status": "not_required"})
        payload = load_status(self.status_file)
        if payload is None:
            return RecordingHealth(False, "recorder status file is missing", {})
        status = str(payload.get("status", "missing"))
        if status != "recording":
            return RecordingHealth(False, f"recorder status changed to {status}", payload)
        try:
            status_age = max(0.0, time() - self.status_file.stat().st_mtime)
        except OSError:
            status_age = float("inf")
        if status_age > self.status_stale_seconds:
            return RecordingHealth(False, f"recorder heartbeat is {status_age:.1f} seconds old", payload)
        summary = dict(payload.get("summary", {}) or {})
        try:
            sample_count = int(summary.get("sample_count", 0))
        except (TypeError, ValueError):
            sample_count = 0
        now = monotonic()
        if self._last_sample_count is None or sample_count > self._last_sample_count:
            self._last_sample_count = sample_count
            self._last_progress_at = now
        elif now - self._last_progress_at > self.stall_timeout_seconds:
            return RecordingHealth(
                False,
                f"recorder sample count has not advanced for {now - self._last_progress_at:.1f} seconds",
                payload,
            )
        return RecordingHealth(True, None, payload)
