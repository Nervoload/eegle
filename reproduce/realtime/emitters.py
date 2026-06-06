"""Feedback decision emitters for realtime workers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DisabledFeedbackEmitter:
    def emit(self, payload: dict[str, Any]) -> None:
        return None

    def close(self) -> None:
        return None


class JsonlFeedbackEmitter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def emit(self, payload: dict[str, Any]) -> None:
        self._handle.write(json.dumps(payload, sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


class LslFeedbackEmitter:
    def __init__(self, name: str, stream_type: str = "Feedback", source_id: str = "closedloop-feedback") -> None:
        import pylsl

        info = pylsl.StreamInfo(name, stream_type, 1, 0, "string", source_id)
        self._outlet = pylsl.StreamOutlet(info)

    def emit(self, payload: dict[str, Any]) -> None:
        self._outlet.push_sample([json.dumps(payload, sort_keys=True)])

    def close(self) -> None:
        return None
