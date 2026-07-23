"""Generic session API plus narrow historical layout helpers."""

from __future__ import annotations

from eegle.session import SessionPaths, create_session, paths_for_existing_session
from eegle.recording.session import Session


__all__ = ["Session", "SessionPaths", "create_session", "paths_for_existing_session"]
