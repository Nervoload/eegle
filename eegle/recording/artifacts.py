"""Portable content-addressed artifacts and the session artifact registry."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash, canonical_json_bytes, content_hash


ARTIFACT_REFERENCE_SCHEMA = "eegle.artifact_reference.v1"
ARTIFACT_LINEAGE_SCHEMA = "eegle.artifact_lineage.v1"
ARTIFACT_ENTRY_SCHEMA = "eegle.artifact_entry.v1"
ARTIFACT_MANIFEST_SCHEMA = "eegle.artifact_manifest.v1"


class Sensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    PSEUDONYMIZED = "pseudonymized"
    RESTRICTED = "restricted"


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    artifact_id: str
    role: str
    uri: str
    digest: str
    media_type: str
    size_bytes: int
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    embedded: bool = False
    schema: str = ARTIFACT_REFERENCE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ARTIFACT_REFERENCE_SCHEMA:
            raise ValueError(f"unsupported artifact reference schema: {self.schema}")
        object.__setattr__(self, "artifact_id", require_identifier(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "role", require_identifier(self.role, "role"))
        object.__setattr__(self, "digest", require_digest(self.digest))
        if not self.uri.strip() or not self.media_type.strip():
            raise ValueError("artifact URI and media type cannot be empty")
        object.__setattr__(self, "size_bytes", int(self.size_bytes))
        if self.size_bytes < 0:
            raise ValueError("artifact size cannot be negative")
        object.__setattr__(self, "sensitivity", Sensitivity(self.sensitivity))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "artifact_id": self.artifact_id,
            "role": self.role,
            "uri": self.uri,
            "digest": self.digest,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sensitivity": self.sensitivity.value,
            "embedded": self.embedded,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArtifactReference":
        return cls(
            schema=str(payload["schema"]),
            artifact_id=str(payload["artifact_id"]),
            role=str(payload["role"]),
            uri=str(payload["uri"]),
            digest=str(payload["digest"]),
            media_type=str(payload["media_type"]),
            size_bytes=int(payload["size_bytes"]),
            sensitivity=Sensitivity(str(payload.get("sensitivity", "internal"))),
            embedded=bool(payload.get("embedded", False)),
        )


@dataclass(frozen=True, slots=True)
class ArtifactLineage:
    """Records the inputs and component state that produced an artifact."""

    component_id: str
    input_artifact_ids: tuple[str, ...] = ()
    input_digests: tuple[str, ...] = ()
    component_version: str | None = None
    component_state_artifact_id: str | None = None
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = ARTIFACT_LINEAGE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ARTIFACT_LINEAGE_SCHEMA:
            raise ValueError(f"unsupported artifact lineage schema: {self.schema}")
        object.__setattr__(self, "component_id", require_identifier(self.component_id, "component_id"))
        object.__setattr__(
            self,
            "input_artifact_ids",
            tuple(require_identifier(value, "input_artifact_id") for value in self.input_artifact_ids),
        )
        object.__setattr__(
            self,
            "input_digests",
            tuple(require_digest(value, "input_digest") for value in self.input_digests),
        )
        if len(self.input_artifact_ids) != len(self.input_digests):
            raise ValueError("artifact lineage requires one digest per input artifact")
        if self.component_version is not None and not self.component_version.strip():
            raise ValueError("component_version cannot be empty")
        if self.component_state_artifact_id is not None:
            object.__setattr__(
                self,
                "component_state_artifact_id",
                require_identifier(self.component_state_artifact_id, "component_state_artifact_id"),
            )
        object.__setattr__(self, "metadata", freeze_json(self.metadata or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "component_id": self.component_id,
            "component_version": self.component_version,
            "component_state_artifact_id": self.component_state_artifact_id,
            "input_artifact_ids": list(self.input_artifact_ids),
            "input_digests": list(self.input_digests),
            "metadata": thaw_json(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArtifactLineage":
        return cls(
            schema=str(payload["schema"]),
            component_id=str(payload["component_id"]),
            component_version=None
            if payload.get("component_version") is None
            else str(payload["component_version"]),
            component_state_artifact_id=None
            if payload.get("component_state_artifact_id") is None
            else str(payload["component_state_artifact_id"]),
            input_artifact_ids=tuple(str(value) for value in payload.get("input_artifact_ids", ())),
            input_digests=tuple(str(value) for value in payload.get("input_digests", ())),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    namespace: str
    reference: ArtifactReference
    lineage: ArtifactLineage | None = None
    schema: str = ARTIFACT_ENTRY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ARTIFACT_ENTRY_SCHEMA:
            raise ValueError(f"unsupported artifact entry schema: {self.schema}")
        object.__setattr__(self, "namespace", _namespace(self.namespace))

    @property
    def qualified_id(self) -> str:
        return f"{self.namespace}:{self.reference.artifact_id}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "namespace": self.namespace,
            "reference": self.reference.to_payload(),
            "lineage": None if self.lineage is None else self.lineage.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArtifactEntry":
        lineage = payload.get("lineage")
        return cls(
            schema=str(payload["schema"]),
            namespace=str(payload["namespace"]),
            reference=ArtifactReference.from_payload(payload["reference"]),
            lineage=None if lineage is None else ArtifactLineage.from_payload(lineage),
        )


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    session_id: str
    entries: tuple[ArtifactEntry, ...] = ()
    aliases: Mapping[str, str] = None  # type: ignore[assignment]
    schema: str = ARTIFACT_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ARTIFACT_MANIFEST_SCHEMA:
            raise ValueError(f"unsupported artifact manifest schema: {self.schema}")
        object.__setattr__(self, "session_id", require_identifier(self.session_id, "session_id"))
        qualified = tuple(entry.qualified_id for entry in self.entries)
        if len(qualified) != len(set(qualified)):
            raise ValueError("artifact manifest contains duplicate qualified identities")
        aliases: dict[str, str] = {}
        for name, uri in (self.aliases or {}).items():
            aliases[require_identifier(name, "artifact alias")] = _relative_uri(uri)
        object.__setattr__(self, "aliases", freeze_json(aliases))

    @property
    def manifest_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "session_id": self.session_id,
            "entries": [entry.to_payload() for entry in self.entries],
            "aliases": thaw_json(self.aliases),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["manifest_hash"] = self.manifest_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArtifactManifest":
        manifest = cls(
            schema=str(payload["schema"]),
            session_id=str(payload["session_id"]),
            entries=tuple(ArtifactEntry.from_payload(item) for item in payload.get("entries", ())),
            aliases={str(key): str(value) for key, value in dict(payload.get("aliases") or {}).items()},
        )
        if payload.get("manifest_hash") != manifest.manifest_hash:
            raise ValueError("artifact manifest hash mismatch")
        return manifest


class ArtifactStore:
    """Namespaced registry for embedded and external content references.

    The registry never infers scientific meaning from a filename. Compatibility
    aliases are optional names over registered relative locations and are kept
    out of target runtime code.
    """

    MANIFEST_URI = ".eegle/artifacts.json"

    def __init__(
        self,
        root: str | Path,
        manifest: ArtifactManifest,
        *,
        read_only: bool = False,
        persist: bool = True,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self._manifest = manifest
        self.read_only = bool(read_only)
        self.persist = bool(persist)
        self._manifest_path = self.root / self.MANIFEST_URI

    @classmethod
    def create(cls, root: str | Path, session_id: str) -> "ArtifactStore":
        target = Path(root).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        store = cls(target, ArtifactManifest(session_id=session_id))
        store._save()
        return store

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        read_only: bool = False,
        expected_session_id: str | None = None,
    ) -> "ArtifactStore":
        target = Path(root).expanduser().resolve()
        payload = _read_json(target / cls.MANIFEST_URI)
        manifest = ArtifactManifest.from_payload(payload)
        if expected_session_id is not None and manifest.session_id != expected_session_id:
            raise ValueError(
                f"artifact manifest session mismatch: expected {expected_session_id}, "
                f"observed {manifest.session_id}"
            )
        return cls(target, manifest, read_only=read_only)

    @classmethod
    def compatibility_view(
        cls,
        root: str | Path,
        session_id: str,
        aliases: Mapping[str, str],
    ) -> "ArtifactStore":
        """Build an in-memory, read-only registry over an existing tree."""

        target = Path(root).expanduser().resolve()
        entries: list[ArtifactEntry] = []
        for alias, uri in aliases.items():
            path = target / _relative_uri(uri)
            if not path.is_file():
                continue
            digest, size = _hash_file(path)
            entries.append(
                ArtifactEntry(
                    namespace="legacy",
                    reference=ArtifactReference(
                        artifact_id=f"legacy.{alias}",
                        role="legacy_artifact",
                        uri=path.relative_to(target).as_posix(),
                        digest=digest,
                        media_type="application/octet-stream",
                        size_bytes=size,
                        sensitivity=Sensitivity.RESTRICTED,
                        embedded=True,
                    ),
                )
            )
        return cls(
            target,
            ArtifactManifest(session_id=session_id, entries=tuple(entries), aliases=aliases),
            read_only=True,
            persist=False,
        )

    @property
    def manifest(self) -> ArtifactManifest:
        return self._manifest

    @property
    def entries(self) -> tuple[ArtifactEntry, ...]:
        return self._manifest.entries

    def declare_aliases(self, aliases: Mapping[str, str]) -> None:
        merged = dict(thaw_json(self._manifest.aliases))
        for name, uri in aliases.items():
            normalized_name = require_identifier(name, "artifact alias")
            normalized_uri = _relative_uri(uri)
            existing = merged.get(normalized_name)
            if existing is not None and existing != normalized_uri:
                raise ValueError(f"artifact alias {normalized_name} is already registered")
            merged[normalized_name] = normalized_uri
        self._replace(aliases=merged)

    def alias_path(self, alias: str) -> Path:
        name = require_identifier(alias, "artifact alias")
        uri = self._manifest.aliases.get(name)
        if uri is None:
            raise KeyError(f"artifact alias is not registered: {name}")
        return self.root / str(uri)

    def register_bytes(
        self,
        namespace: str,
        artifact_id: str,
        role: str,
        data: bytes,
        media_type: str,
        *,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        lineage: ArtifactLineage | None = None,
    ) -> ArtifactReference:
        self._require_writable()
        namespace = _namespace(namespace)
        digest = content_hash(data)
        suffix = digest.removeprefix("sha256:")
        relative = Path("artifacts") / namespace / suffix
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            observed, size = _hash_file(target)
            if observed != digest or size != len(data):
                raise ValueError(f"content-addressed artifact collision at {relative.as_posix()}")
        else:
            _atomic_write(target, data)
        reference = ArtifactReference(
            artifact_id=artifact_id,
            role=role,
            uri=relative.as_posix(),
            digest=digest,
            media_type=media_type,
            size_bytes=len(data),
            sensitivity=sensitivity,
            embedded=True,
        )
        self._register(namespace, reference, lineage)
        return reference

    def register_json(
        self,
        namespace: str,
        artifact_id: str,
        role: str,
        payload: Mapping[str, Any],
        *,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        lineage: ArtifactLineage | None = None,
    ) -> ArtifactReference:
        return self.register_bytes(
            namespace,
            artifact_id,
            role,
            canonical_json_bytes(payload),
            "application/json",
            sensitivity=sensitivity,
            lineage=lineage,
        )

    def register_file(
        self,
        namespace: str,
        artifact_id: str,
        role: str,
        source: str | Path,
        media_type: str,
        *,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        lineage: ArtifactLineage | None = None,
        copy: bool = True,
    ) -> ArtifactReference:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        if copy:
            self._require_writable()
            namespace = _namespace(namespace)
            digest, size = _hash_file(path)
            relative = Path("artifacts") / namespace / digest.removeprefix("sha256:")
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                observed, observed_size = _hash_file(target)
                if observed != digest or observed_size != size:
                    raise ValueError(
                        f"content-addressed artifact collision at {relative.as_posix()}"
                    )
            else:
                _atomic_copy(path, target)
            reference = ArtifactReference(
                artifact_id=artifact_id,
                role=role,
                uri=relative.as_posix(),
                digest=digest,
                media_type=media_type,
                size_bytes=size,
                sensitivity=sensitivity,
                embedded=True,
            )
            self._register(namespace, reference, lineage)
            return reference
        self._require_writable()
        try:
            relative = path.relative_to(self.root).as_posix()
        except ValueError as exc:
            raise ValueError("an embedded artifact without copying must be inside the session") from exc
        digest, size = _hash_file(path)
        reference = ArtifactReference(
            artifact_id=artifact_id,
            role=role,
            uri=relative,
            digest=digest,
            media_type=media_type,
            size_bytes=size,
            sensitivity=sensitivity,
            embedded=True,
        )
        self._register(namespace, reference, lineage)
        return reference

    def register_external(
        self,
        namespace: str,
        artifact_id: str,
        role: str,
        uri: str,
        digest: str,
        media_type: str,
        size_bytes: int,
        *,
        sensitivity: Sensitivity = Sensitivity.RESTRICTED,
        lineage: ArtifactLineage | None = None,
    ) -> ArtifactReference:
        self._require_writable()
        reference = ArtifactReference(
            artifact_id=artifact_id,
            role=role,
            uri=uri,
            digest=digest,
            media_type=media_type,
            size_bytes=size_bytes,
            sensitivity=sensitivity,
            embedded=False,
        )
        self._register(namespace, reference, lineage)
        return reference

    def register_external_file(
        self,
        namespace: str,
        artifact_id: str,
        role: str,
        source: str | Path,
        media_type: str,
        *,
        sensitivity: Sensitivity = Sensitivity.RESTRICTED,
        lineage: ArtifactLineage | None = None,
    ) -> ArtifactReference:
        """Hash and reference a local source-native file without copying it."""

        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        digest, size = _hash_file(path)
        return self.register_external(
            namespace,
            artifact_id,
            role,
            path.as_uri(),
            digest,
            media_type,
            size,
            sensitivity=sensitivity,
            lineage=lineage,
        )

    def get(self, namespace: str, artifact_id: str) -> ArtifactEntry:
        key = f"{_namespace(namespace)}:{require_identifier(artifact_id, 'artifact_id')}"
        for entry in self._manifest.entries:
            if entry.qualified_id == key:
                return entry
        raise KeyError(key)

    def resolve(self, reference: ArtifactReference) -> Path:
        if not reference.embedded:
            raise ValueError(f"artifact {reference.artifact_id} is an external reference")
        relative = _relative_uri(reference.uri)
        return self.root / relative

    def verify(self, reference: ArtifactReference) -> tuple[bool, str | None]:
        if not reference.embedded:
            return False, (
                f"external artifact content was not verified: {reference.artifact_id}; "
                "use an ExternalArtifactVerifier"
            )
        path = self.resolve(reference)
        if not path.is_file():
            return False, f"artifact missing: {reference.artifact_id} at {reference.uri}"
        digest, size = _hash_file(path)
        if size != reference.size_bytes:
            return (
                False,
                f"artifact size mismatch for {reference.artifact_id}: "
                f"expected {reference.size_bytes}, observed {size}",
            )
        if digest != reference.digest:
            return (
                False,
                f"artifact digest mismatch for {reference.artifact_id}: "
                f"expected {reference.digest}, observed {digest}",
            )
        return True, None

    def _register(
        self,
        namespace: str,
        reference: ArtifactReference,
        lineage: ArtifactLineage | None,
    ) -> None:
        entry = ArtifactEntry(namespace=namespace, reference=reference, lineage=lineage)
        existing = {item.qualified_id: item for item in self._manifest.entries}.get(entry.qualified_id)
        if existing is not None:
            if existing == entry:
                return
            raise ValueError(f"artifact identity is already registered: {entry.qualified_id}")
        self._replace(entries=(*self._manifest.entries, entry))

    def _replace(
        self,
        *,
        entries: Iterable[ArtifactEntry] | None = None,
        aliases: Mapping[str, str] | None = None,
    ) -> None:
        if self.read_only:
            raise PermissionError("artifact store is read-only")
        self._manifest = ArtifactManifest(
            session_id=self._manifest.session_id,
            entries=tuple(self._manifest.entries if entries is None else entries),
            aliases=thaw_json(self._manifest.aliases) if aliases is None else aliases,
        )
        self._save()

    def _require_writable(self) -> None:
        if self.read_only:
            raise PermissionError("artifact store is read-only")

    def _save(self) -> None:
        if not self.persist:
            return
        self._require_writable()
        _atomic_write(self._manifest_path, canonical_json_bytes(self._manifest.to_payload()))


def _relative_uri(value: str) -> str:
    path = Path(str(value))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("embedded artifact URI must be a safe relative path")
    return path.as_posix()


def _namespace(value: str) -> str:
    normalized = require_identifier(value, "namespace")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("namespace cannot contain empty, '.', or '..' path segments")
    return normalized


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> Mapping[str, Any]:
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid artifact manifest JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("artifact manifest must be a JSON object")
    return payload
