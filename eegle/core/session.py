"""Public session-layout helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eegle.session import SessionPaths, create_session, paths_for_existing_session


@dataclass(frozen=True)
class Session:
    """Small public wrapper around an EEGle session directory."""

    paths: SessionPaths
    config: dict[str, Any] | None = None

    @property
    def root(self) -> Path:
        return self.paths.root

    def payload(self) -> dict[str, Any]:
        return {
            "session_dir": str(self.paths.root),
            "parameters": str(self.paths.parameters),
            "manifest": str(self.paths.manifest),
            "realtime": str(self.paths.realtime),
            "reports": str(self.paths.reports),
        }


__all__ = ["Session", "SessionPaths", "create_session", "paths_for_existing_session"]
