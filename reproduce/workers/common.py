"""Shared helpers for managed experiment workers."""

from __future__ import annotations

import json
import signal
import threading
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any


class StatusWriter:
    def __init__(self, path: str | Path, name: str, backend: str, telemetry: Any | None = None) -> None:
        self.path = Path(path)
        self.name = name
        self.backend = backend
        self.telemetry = telemetry
        self._last_status: str | None = None
        self._ready_emitted = False
        self.started_at_monotonic = monotonic()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def update(self, status: str, **fields: Any) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "backend": self.backend,
            "status": status,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "elapsed_seconds": monotonic() - self.started_at_monotonic,
            **fields,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(self.path)
        self._emit_telemetry(payload)
        self._last_status = status
        return payload

    def _emit_telemetry(self, payload: dict[str, Any]) -> None:
        if self.telemetry is None:
            return
        status = str(payload.get("status", "unknown"))
        event, level, message = _status_event(self.name, status, self._last_status, self._ready_emitted)
        if event == "process.ready":
            self._ready_emitted = True
        self.telemetry.emit(
            event,
            component=self.name,
            level=level,
            message=message,
            metadata=payload,
        )


def append_jsonl(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


class JsonlWriter:
    """Long-lived JSONL writer for high-rate realtime streams."""

    def __init__(self, path: str | Path, flush_every: int = 10) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self._flush_every = max(1, int(flush_every))
        self._pending = 0

    def write(self, payload: dict[str, Any]) -> None:
        self._handle.write(json.dumps(payload, sort_keys=True) + "\n")
        self._pending += 1
        if self._pending >= self._flush_every:
            self.flush()

    def flush(self) -> None:
        self._handle.flush()
        self._pending = 0

    def close(self) -> None:
        self.flush()
        self._handle.close()


def install_stop_signal_handlers(stop_event: threading.Event) -> None:
    def _handle_stop(signum: int, frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)


def load_status(path: str | Path) -> dict[str, Any] | None:
    target = Path(path)
    if not target.exists():
        return None
    with target.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _status_event(name: str, status: str, last_status: str | None, ready_emitted: bool) -> tuple[str, str, str]:
    if status == "disabled":
        return "process.disabled", "default", f"{name} disabled"
    if status == "starting":
        return "process.start", "default", f"{name} starting"
    if status in {"recording", "running"}:
        if not ready_emitted:
            return "process.ready", "default", f"{name} ready"
        if status == last_status:
            return "process.heartbeat", "realtime", f"{name} heartbeat"
        return "process.ready", "default", f"{name} ready"
    if status in {"failed", "unsupported", "killed"}:
        return "process.failed", "default", f"{name} {status}"
    if status in {"stopped", "complete"}:
        return "process.stop", "default", f"{name} {status}"
    return "process.status", "realtime", f"{name} status: {status}"
