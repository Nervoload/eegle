"""Task-independent health checks for a managed raw EEG recorder."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep, time
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
        self._last_status_elapsed_seconds: float | None = None
        self._last_status_progress_at = monotonic()
        self._active_warning_kind: str | None = None

    def check(self) -> RecordingHealth:
        if not self.required:
            return RecordingHealth(True, None, {"status": "not_required"})
        read_problem: str | None = None
        payload, read_problem = self._read_status_with_retry()
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
        now = monotonic()
        status_age = self._status_payload_age(payload, now)
        summary = dict(payload.get("summary", {}) or {})
        heartbeat = dict(summary.get("lsl_sample_heartbeat") or {})
        heartbeat_degraded = (
            summary.get("primary_format") == "xdf"
            and heartbeat.get("status") not in {None, "recording"}
        )
        try:
            sample_count = int(summary.get("sample_count", 0))
        except (TypeError, ValueError):
            sample_count = 0
        data_file_size = _recorded_data_file_size(summary)
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
            warning_kind = "recording_progress_warning"
            is_new = warning_kind != self._active_warning_kind
            self._active_warning_kind = warning_kind
            return RecordingHealth(True, reason if is_new else None, payload, warning=is_new)
        if heartbeat_degraded and not read_problem and status_age <= self.status_stale_seconds:
            warning_kind = "lsl_sample_heartbeat_degraded"
            is_new = warning_kind != self._active_warning_kind
            self._active_warning_kind = warning_kind
            reason = str(
                summary.get("lsl_sample_heartbeat_warning")
                or "diagnostic LSL sample heartbeat is degraded; authoritative XDF acquisition continues"
            )
            return RecordingHealth(True, reason if is_new else None, payload, warning=is_new)
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

    def _read_status_with_retry(self) -> tuple[dict[str, Any] | None, str | None]:
        last_error: BaseException | None = None
        for delay in (0.0, 0.005, 0.02):
            if delay:
                sleep(delay)
            try:
                return load_status(self.status_file), None
            except (OSError, ValueError) as exc:
                last_error = exc
        assert last_error is not None
        return None, (
            "recorder status file could not be read after retries: "
            f"{type(last_error).__name__}: {last_error}"
        )

    def _status_payload_age(self, payload: dict[str, Any], now: float) -> float:
        """Prefer payload progress over filesystem mtimes, which are unreliable on Windows."""

        try:
            elapsed = float(payload["elapsed_seconds"])
        except (KeyError, TypeError, ValueError):
            try:
                return max(0.0, time() - self.status_file.stat().st_mtime)
            except OSError:
                return float("inf")
        if self._last_status_elapsed_seconds is None or elapsed > self._last_status_elapsed_seconds:
            self._last_status_elapsed_seconds = elapsed
            self._last_status_progress_at = now
        return max(0.0, now - self._last_status_progress_at)


def _recorded_data_file_size(summary: dict[str, Any]) -> int | None:
    """Use an enabled healthy CSV observer, otherwise the primary XDF."""

    csv_mirror = dict(summary.get("csv_mirror") or {})
    mirror_healthy = csv_mirror.get("status") in {None, "recording"}
    raw_file = (
        csv_mirror.get("raw_file")
        if mirror_healthy
        else summary.get("xdf_file") or summary.get("raw_file")
    )
    if not raw_file:
        return None
    try:
        return Path(str(raw_file)).stat().st_size
    except OSError:
        return None
