"""Generic session identity, lifecycle, and legacy read-only discovery."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from eegle._paths import io_path, opened
from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash, canonical_json_bytes
from eegle.recording.artifacts import ArtifactStore


SESSION_MANIFEST_SCHEMA = "eegle.session.v1"


class SessionStatus(str, Enum):
    OPEN = "open"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SessionManifest:
    session_id: str
    created_at: str
    status: SessionStatus
    artifact_registry_uri: str = ArtifactStore.MANIFEST_URI
    bundle_uris: tuple[str, ...] = ()
    participant_pseudonym: str | None = None
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]
    completed_at: str | None = None
    origin_schema: str | None = None
    schema: str = SESSION_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SESSION_MANIFEST_SCHEMA:
            raise ValueError(f"unsupported session manifest schema: {self.schema}")
        object.__setattr__(self, "session_id", require_identifier(self.session_id, "session_id"))
        object.__setattr__(self, "status", SessionStatus(self.status))
        if not self.created_at.strip():
            raise ValueError("session created_at cannot be empty")
        if self.artifact_registry_uri not in {
            ArtifactStore.MANIFEST_URI,
            ".eegle/legacy-read-only",
        }:
            raise ValueError(
                f"unsupported session artifact registry URI: {self.artifact_registry_uri}"
            )
        if Path(self.artifact_registry_uri).is_absolute() or ".." in Path(
            self.artifact_registry_uri
        ).parts:
            raise ValueError("artifact_registry_uri must be a safe relative path")
        bundle_uris = tuple(_safe_relative(value, "bundle_uri") for value in self.bundle_uris)
        if len(bundle_uris) != len(set(bundle_uris)):
            raise ValueError("session bundle URIs must be unique")
        object.__setattr__(self, "bundle_uris", bundle_uris)
        if self.participant_pseudonym is not None:
            object.__setattr__(
                self,
                "participant_pseudonym",
                require_identifier(self.participant_pseudonym, "participant_pseudonym"),
            )
        object.__setattr__(self, "metadata", freeze_json(self.metadata or {}))
        if self.status == SessionStatus.COMPLETE and self.completed_at is None:
            raise ValueError("complete session requires completed_at")

    @property
    def manifest_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "status": self.status.value,
            "artifact_registry_uri": self.artifact_registry_uri,
            "bundle_uris": list(self.bundle_uris),
            "participant_pseudonym": self.participant_pseudonym,
            "metadata": thaw_json(self.metadata),
            "origin_schema": self.origin_schema,
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["manifest_hash"] = self.manifest_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SessionManifest":
        manifest = cls(
            schema=str(payload["schema"]),
            session_id=str(payload["session_id"]),
            created_at=str(payload["created_at"]),
            completed_at=None
            if payload.get("completed_at") is None
            else str(payload["completed_at"]),
            status=SessionStatus(str(payload["status"])),
            artifact_registry_uri=str(
                payload.get("artifact_registry_uri", ArtifactStore.MANIFEST_URI)
            ),
            bundle_uris=tuple(str(value) for value in payload.get("bundle_uris", ())),
            participant_pseudonym=None
            if payload.get("participant_pseudonym") is None
            else str(payload["participant_pseudonym"]),
            metadata=dict(payload.get("metadata") or {}),
            origin_schema=None
            if payload.get("origin_schema") is None
            else str(payload["origin_schema"]),
        )
        if payload.get("manifest_hash") != manifest.manifest_hash:
            raise ValueError("session manifest hash mismatch")
        return manifest


class Session:
    """A suite-neutral session with a versioned artifact registry."""

    MANIFEST_NAME = "session.json"

    def __init__(
        self,
        root: str | Path,
        manifest: SessionManifest,
        artifacts: ArtifactStore,
        *,
        read_only: bool = False,
        legacy: bool = False,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.manifest = manifest
        self.artifacts = artifacts
        self.read_only = bool(read_only or manifest.status != SessionStatus.OPEN)
        if self.read_only:
            self.artifacts.read_only = True
        self.legacy = bool(legacy)

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        session_id: str,
        participant_pseudonym: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> "Session":
        target = Path(root).expanduser().resolve()
        os.makedirs(io_path(target), exist_ok=True)
        manifest_path = target / cls.MANIFEST_NAME
        if manifest_path.exists() or (target / ArtifactStore.MANIFEST_URI).exists():
            raise FileExistsError(f"session already exists at {target}")
        manifest = SessionManifest(
            session_id=session_id,
            created_at=created_at or _utc_now(),
            status=SessionStatus.OPEN,
            participant_pseudonym=participant_pseudonym,
            metadata=metadata or {},
        )
        artifacts = ArtifactStore.create(target, manifest.session_id)
        session = cls(target, manifest, artifacts)
        session._save()
        return session

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        read_only: bool = False,
        allow_legacy: bool = True,
    ) -> "Session":
        target = Path(root).expanduser().resolve()
        manifest_path = target / cls.MANIFEST_NAME
        if manifest_path.is_file():
            payload = _read_json(manifest_path)
            manifest = SessionManifest.from_payload(payload)
            artifacts = ArtifactStore.open(
                target,
                read_only=read_only,
                expected_session_id=manifest.session_id,
            )
            return cls(target, manifest, artifacts, read_only=read_only)
        if not allow_legacy:
            raise FileNotFoundError(f"no {cls.MANIFEST_NAME} at {target}")
        return cls._open_legacy(target)

    @classmethod
    def _open_legacy(cls, target: Path) -> "Session":
        legacy_manifest_path = target / "manifest.json"
        recognizable = legacy_manifest_path.is_file() or any(
            (target / name).exists() for name in ("raw", "events", "realtime", "parameters.json")
        )
        if not recognizable:
            raise FileNotFoundError(f"no supported EEGle session found at {target}")
        payload: Mapping[str, Any] = (
            _read_json(legacy_manifest_path) if legacy_manifest_path.is_file() else {}
        )
        identity_source: Mapping[str, Any]
        if payload:
            identity_source = {"legacy_manifest": payload}
        else:
            identity_source = {
                "legacy_files": sorted(
                    (
                        {
                            "uri": path.relative_to(target).as_posix(),
                            "size_bytes": path.stat().st_size,
                        }
                        for path in target.rglob("*")
                        if path.is_file()
                    ),
                    key=lambda item: str(item["uri"]),
                )
            }
        digest = canonical_hash(identity_source).removeprefix("sha256:")[:24]
        session_id = f"legacy.{digest}"
        created_at = str(payload.get("created_at") or _mtime_iso(target))
        status = (
            SessionStatus.COMPLETE
            if (target / "session_summary.json").is_file()
            else SessionStatus.PARTIAL
        )
        summary_path = target / "session_summary.json"
        manifest = SessionManifest(
            session_id=session_id,
            created_at=created_at,
            completed_at=_mtime_iso(summary_path) if status == SessionStatus.COMPLETE else None,
            status=status,
            artifact_registry_uri=".eegle/legacy-read-only",
            metadata={
                "import_mode": "read_only_compatibility_view",
                "source_manifest_present": legacy_manifest_path.is_file(),
            },
            origin_schema="eegle.legacy_session.bcipy_style.v1",
        )
        from eegle.recording.compat import LEGACY_PATH_ALIASES

        artifacts = ArtifactStore.compatibility_view(target, session_id, LEGACY_PATH_ALIASES)
        return cls(target, manifest, artifacts, read_only=True, legacy=True)

    @property
    def session_id(self) -> str:
        return self.manifest.session_id

    @property
    def status(self) -> SessionStatus:
        return self.manifest.status

    @property
    def bundle_paths(self) -> tuple[Path, ...]:
        return tuple(self.root / uri for uri in self.manifest.bundle_uris)

    def register_bundle(self, relative_uri: str) -> None:
        self._require_writable()
        if self.manifest.status != SessionStatus.OPEN:
            raise RuntimeError("cannot register a bundle after session finalization")
        uri = _safe_relative(relative_uri, "bundle_uri")
        if uri in self.manifest.bundle_uris:
            return
        self._replace(bundle_uris=(*self.manifest.bundle_uris, uri))

    def finalize(
        self,
        status: SessionStatus = SessionStatus.COMPLETE,
        *,
        completed_at: str | None = None,
    ) -> None:
        self._require_writable()
        if self.manifest.status != SessionStatus.OPEN:
            raise RuntimeError("session is already finalized")
        normalized = SessionStatus(status)
        if normalized == SessionStatus.OPEN:
            raise ValueError("finalize requires complete, partial, or failed status")
        self._replace(status=normalized, completed_at=completed_at or _utc_now())
        self.read_only = True
        self.artifacts.read_only = True

    def _replace(
        self,
        *,
        status: SessionStatus | None = None,
        completed_at: str | None = None,
        bundle_uris: tuple[str, ...] | None = None,
    ) -> None:
        self.manifest = SessionManifest(
            session_id=self.manifest.session_id,
            created_at=self.manifest.created_at,
            completed_at=self.manifest.completed_at if completed_at is None else completed_at,
            status=self.manifest.status if status is None else status,
            artifact_registry_uri=self.manifest.artifact_registry_uri,
            bundle_uris=self.manifest.bundle_uris if bundle_uris is None else bundle_uris,
            participant_pseudonym=self.manifest.participant_pseudonym,
            metadata=thaw_json(self.manifest.metadata),
            origin_schema=self.manifest.origin_schema,
        )
        self._save()

    def _save(self) -> None:
        self._require_writable()
        _atomic_write(self.root / self.MANIFEST_NAME, canonical_json_bytes(self.manifest.to_payload()))

    def _require_writable(self) -> None:
        if self.read_only:
            raise PermissionError("session is read-only")


def _safe_relative(value: str, field: str) -> str:
    path = Path(str(value))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"{field} must be a safe relative path")
    return path.as_posix()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _mtime_iso(path: Path) -> str:
    modified = os.stat(io_path(path)).st_mtime
    return datetime.fromtimestamp(modified, timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        with opened(path, "r", encoding="utf-8") as handle:
            payload = json.loads(handle.read())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid session manifest JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"session manifest must be a JSON object: {path}")
    return payload


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with opened(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(io_path(temporary), io_path(path))
    finally:
        if os.path.exists(io_path(temporary)):
            os.unlink(io_path(temporary))
