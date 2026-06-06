"""Structured telemetry for experiment observability."""

from __future__ import annotations

import copy
import json
import sys
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Iterator


CONSOLE_LEVELS = ("quiet", "default", "realtime", "debug")
FILE_LEVELS = ("default", "realtime", "debug")
EVENT_RANKS = {
    "default": 1,
    "realtime": 2,
    "debug": 3,
}


DEFAULT_TELEMETRY_CONFIG = {
    "console_level": "default",
    "file_level": "realtime",
    "trace_enabled": False,
    "heartbeat_seconds": 5.0,
}


@dataclass
class TelemetryPaths:
    session_dir: Path
    telemetry_jsonl: Path
    debug_jsonl: Path


class Telemetry:
    """Write structured session events and route selected events to console."""

    def __init__(
        self,
        paths: TelemetryPaths,
        *,
        console_level: str = "default",
        file_level: str = "realtime",
        trace_enabled: bool = False,
        heartbeat_seconds: float = 5.0,
        default_component: str = "experiment",
    ) -> None:
        self.paths = paths
        self.console_level = _normalize_console_level(console_level)
        self.file_level = _normalize_file_level(file_level)
        self.trace_enabled = bool(trace_enabled)
        self._heartbeat_seconds = float(heartbeat_seconds)
        self.default_component = default_component
        self.paths.telemetry_jsonl.parent.mkdir(parents=True, exist_ok=True)
        self.paths.debug_jsonl.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any],
        paths: Any,
        *,
        component: str = "experiment",
    ) -> "Telemetry":
        telemetry_config = telemetry_config_from(config)
        return cls(
            TelemetryPaths(
                session_dir=Path(paths.root),
                telemetry_jsonl=Path(paths.telemetry_jsonl),
                debug_jsonl=Path(paths.debug_jsonl),
            ),
            console_level=telemetry_config["console_level"],
            file_level=telemetry_config["file_level"],
            trace_enabled=bool(telemetry_config["trace_enabled"]),
            heartbeat_seconds=float(telemetry_config["heartbeat_seconds"]),
            default_component=component,
        )

    @property
    def heartbeat_seconds(self) -> float:
        return self._heartbeat_seconds

    def emit(
        self,
        event: str,
        *,
        component: str | None = None,
        level: str = "default",
        message: str = "",
        metadata: dict[str, Any] | None = None,
        console: bool = True,
    ) -> dict[str, Any]:
        event_level = _normalize_event_level(level)
        record = {
            "timestamp_wall": datetime.now().isoformat(timespec="milliseconds"),
            "timestamp_monotonic": monotonic(),
            "session_id": self.paths.session_dir.name,
            "session_dir": str(self.paths.session_dir),
            "component": component or self.default_component,
            "event": event,
            "level": event_level,
            "message": message,
            "metadata": _json_safe(metadata or {}),
        }
        self._write_for_level(record, event_level)
        if console and _should_print(self.console_level, event_level):
            self._print(record)
        return record

    @contextmanager
    def span(
        self,
        name: str,
        *,
        component: str | None = None,
        level: str = "debug",
        message: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        if not self.trace_enabled:
            yield
            return

        started = monotonic()
        self.emit(
            f"trace.{name}.start",
            component=component,
            level=level,
            message=message or f"{name} started",
            metadata=metadata,
            console=False,
        )
        try:
            yield
        except Exception as exc:
            duration = monotonic() - started
            failure_metadata = dict(metadata or {})
            failure_metadata.update(
                {
                    "duration_seconds": duration,
                    "exception_type": type(exc).__name__,
                    "exception": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
            self.emit(
                f"trace.{name}.failed",
                component=component,
                level="debug",
                message=f"{name} failed",
                metadata=failure_metadata,
                console=True,
            )
            raise
        else:
            complete_metadata = dict(metadata or {})
            complete_metadata["duration_seconds"] = monotonic() - started
            self.emit(
                f"trace.{name}.complete",
                component=component,
                level=level,
                message=message or f"{name} complete",
                metadata=complete_metadata,
                console=False,
            )

    def _write_for_level(self, record: dict[str, Any], level: str) -> None:
        rank = EVENT_RANKS[level]
        if rank <= EVENT_RANKS[self.file_level]:
            _append_jsonl(self.paths.telemetry_jsonl, record)
        if level == "debug" or record["event"].startswith("trace."):
            _append_jsonl(self.paths.debug_jsonl, record)

    def _print(self, record: dict[str, Any]) -> None:
        component = record["component"]
        message = record["message"] or record["event"]
        print(f"[{component}] {message}", file=sys.stderr, flush=True)


def telemetry_config_from(config: dict[str, Any]) -> dict[str, Any]:
    telemetry = dict(DEFAULT_TELEMETRY_CONFIG)
    telemetry.update(config.get("telemetry", {}) or {})
    telemetry["console_level"] = _normalize_console_level(telemetry.get("console_level", "default"))
    telemetry["file_level"] = _normalize_file_level(telemetry.get("file_level", "realtime"))
    telemetry["trace_enabled"] = bool(telemetry.get("trace_enabled", False))
    telemetry["heartbeat_seconds"] = float(telemetry.get("heartbeat_seconds", 5.0))
    return telemetry


def apply_cli_telemetry_overrides(config: dict[str, Any], args: Any) -> dict[str, Any]:
    result = copy.deepcopy(config)
    telemetry = telemetry_config_from(result)
    log_level = getattr(args, "log_level", None)
    if log_level:
        if log_level == "quiet":
            telemetry["console_level"] = "quiet"
        else:
            telemetry["console_level"] = _normalize_console_level(log_level)
    if getattr(args, "quiet", False):
        telemetry["console_level"] = "quiet"
    if getattr(args, "trace", False):
        telemetry["trace_enabled"] = True
        if telemetry["file_level"] != "debug":
            telemetry["file_level"] = "debug"
    result["telemetry"] = telemetry
    return result


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _should_print(console_level: str, event_level: str) -> bool:
    if console_level == "quiet":
        return False
    return EVENT_RANKS[event_level] <= EVENT_RANKS[console_level]


def _normalize_console_level(level: Any) -> str:
    value = str(level or "default")
    if value not in CONSOLE_LEVELS:
        return "default"
    return value


def _normalize_file_level(level: Any) -> str:
    value = str(level or "realtime")
    if value == "quiet":
        return "default"
    if value not in FILE_LEVELS:
        return "realtime"
    return value


def _normalize_event_level(level: Any) -> str:
    value = str(level or "default")
    if value == "quiet":
        return "default"
    if value not in EVENT_RANKS:
        return "default"
    return value


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)
