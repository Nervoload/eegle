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
    warning: bool = False


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
        self._last_data_file_size: int | None = None
        self._last_progress_at = monotonic()
        self._last_payload: dict[str, Any] | None = None
        self._active_warning_kind: str | None = None

    def check(self) -> RecordingHealth:
        if not self.required:
            return RecordingHealth(True, None, {"status": "not_required"})
        read_problem: str | None = None
        try:
            payload = load_status(self.status_file)
        except (OSError, ValueError) as exc:
            payload = None
            read_problem = f"recorder status file could not be read: {type(exc).__name__}: {exc}"
        if payload is None:
            if self._last_payload is None:
                return RecordingHealth(False, read_problem or "recorder status file is missing", {})
            payload = self._last_payload
            read_problem = read_problem or "recorder status file is temporarily missing"
        else:
            self._last_payload = payload
        status = str(payload.get("status", "missing"))
        if status != "recording":
            return RecordingHealth(False, f"recorder status changed to {status}", payload)
        try:
            status_age = max(0.0, time() - self.status_file.stat().st_mtime)
        except OSError:
            status_age = float("inf")
        summary = dict(payload.get("summary", {}) or {})
        try:
            sample_count = int(summary.get("sample_count", 0))
        except (TypeError, ValueError):
            sample_count = 0
        data_file_size = _recorded_data_file_size(summary)
        now = monotonic()
        sample_progress = self._last_sample_count is None or sample_count > self._last_sample_count
        file_progress = (
            data_file_size is not None
            and (self._last_data_file_size is None or data_file_size > self._last_data_file_size)
        )
        if sample_progress or file_progress:
            self._last_sample_count = sample_count
            self._last_data_file_size = data_file_size
            self._last_progress_at = now
        elif now - self._last_progress_at > self.stall_timeout_seconds:
            if read_problem or status_age > self.status_stale_seconds:
                reason = (
                    f"recorder heartbeat/status is unavailable and the source-preserving EEG file "
                    f"has not advanced for {now - self._last_progress_at:.1f} seconds"
                )
            else:
                reason = (
                    f"recorder sample count and source-preserving EEG file have not advanced for "
                    f"{now - self._last_progress_at:.1f} seconds"
                )
            return RecordingHealth(
                False,
                reason,
                payload,
            )
        if read_problem or status_age > self.status_stale_seconds:
            warning_kind = "status_read_problem" if read_problem else "stale_heartbeat"
            reason = (
                f"{read_problem}; source-preserving EEG data are still advancing"
                if read_problem
                else f"recorder heartbeat is {status_age:.1f} seconds old, but source-preserving EEG data are still advancing"
            )
            is_new = warning_kind != self._active_warning_kind
            self._active_warning_kind = warning_kind
            return RecordingHealth(True, reason if is_new else None, payload, warning=is_new)
        self._active_warning_kind = None
        return RecordingHealth(True, None, payload)


def _recorded_data_file_size(summary: dict[str, Any]) -> int | None:
    """Read progress from the CSV safety recording, never buffered XDF growth."""

    csv_mirror = dict(summary.get("csv_mirror") or {})
    raw_file = csv_mirror.get("raw_file") or summary.get("raw_file")
    if not raw_file:
        return None
    try:
        return Path(str(raw_file)).stat().st_size
    except OSError:
        return None
