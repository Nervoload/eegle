"""Typed, non-hashing authoring provenance and source-map records."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from eegle._validation import require_digest, require_identifier
from eegle.authoring.contracts import (
    AuthoringOrigin,
    CanonicalArtifact,
    ConfirmationState,
    ScientificMateriality,
    SourceKind,
    SourceLocation,
)
from eegle.authoring.schemas import (
    AUTHORING_PROVENANCE_SCHEMA_ID,
    validate_authoring_provenance_payload,
)


def _require_pointer(value: str, field: str) -> str:
    normalized = str(value)
    if normalized and not normalized.startswith("/"):
        raise ValueError(f"{field} must be a JSON pointer")
    parts = normalized.split("/")[1:]
    if any("~" in part.replace("~0", "").replace("~1", "") for part in parts):
        raise ValueError(f"{field} contains an invalid JSON pointer escape")
    return normalized


@dataclass(frozen=True, slots=True)
class DraftSourceMap:
    """Locations for authoring JSON pointers, independent of draft values."""

    locations: Mapping[str, SourceLocation]
    fallback: SourceLocation = SourceLocation(
        kind=SourceKind.GENERATED,
        symbol="experiment_draft",
    )

    def __post_init__(self) -> None:
        normalized: dict[str, SourceLocation] = {}
        for path, source in self.locations.items():
            pointer = _require_pointer(str(path), "draft source path")
            if not isinstance(source, SourceLocation):
                raise TypeError("draft source locations must be SourceLocation values")
            normalized[pointer] = source
        if not isinstance(self.fallback, SourceLocation):
            raise TypeError("draft source fallback must be a SourceLocation")
        object.__setattr__(self, "locations", MappingProxyType(normalized))

    def source_for(self, path: str) -> SourceLocation:
        pointer = _require_pointer(path, "draft path")
        matches = (
            (candidate, source)
            for candidate, source in self.locations.items()
            if _pointer_contains(candidate, pointer)
        )
        return max(matches, key=lambda value: len(value[0]), default=("", self.fallback))[1]


@dataclass(frozen=True, slots=True)
class CanonicalTarget:
    schema: str
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", require_identifier(self.schema, "target schema"))
        object.__setattr__(self, "digest", require_digest(self.digest, "target digest"))

    def to_payload(self) -> dict[str, str]:
        return {"schema": self.schema, "digest": self.digest}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CanonicalTarget":
        return cls(schema=str(payload["schema"]), digest=str(payload["digest"]))


@dataclass(frozen=True, slots=True)
class TemplateReference:
    template_id: str
    version: str
    parameter_path: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "template_id",
            require_identifier(self.template_id, "template_id"),
        )
        if not self.version.strip():
            raise ValueError("template version cannot be empty")
        if self.parameter_path is not None:
            object.__setattr__(
                self,
                "parameter_path",
                _require_pointer(self.parameter_path, "template parameter_path"),
            )

    def to_payload(self) -> dict[str, Any]:
        payload = {"template_id": self.template_id, "version": self.version}
        if self.parameter_path is not None:
            payload["parameter_path"] = self.parameter_path
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TemplateReference":
        return cls(
            template_id=str(payload["template_id"]),
            version=str(payload["version"]),
            parameter_path=(
                None
                if payload.get("parameter_path") is None
                else str(payload["parameter_path"])
            ),
        )


@dataclass(frozen=True, slots=True)
class ProvenanceEntry:
    target_artifact: CanonicalArtifact
    target_path: str
    origin: AuthoringOrigin
    source: SourceLocation
    materiality: ScientificMateriality
    confirmation: ConfirmationState = ConfirmationState.NOT_REQUIRED
    template: TemplateReference | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_artifact",
            CanonicalArtifact(self.target_artifact),
        )
        object.__setattr__(
            self,
            "target_path",
            _require_pointer(self.target_path, "provenance target_path"),
        )
        object.__setattr__(self, "origin", AuthoringOrigin(self.origin))
        if not isinstance(self.source, SourceLocation):
            raise TypeError("provenance source must be a SourceLocation")
        object.__setattr__(
            self,
            "materiality",
            ScientificMateriality(self.materiality),
        )
        object.__setattr__(
            self,
            "confirmation",
            ConfirmationState(self.confirmation),
        )
        if self.template is not None and not isinstance(
            self.template, TemplateReference
        ):
            raise TypeError("provenance template must be a TemplateReference")
        if self.origin == AuthoringOrigin.TEMPLATE_DEFAULT and self.template is None:
            raise ValueError("template-default provenance requires a template reference")
        if self.origin != AuthoringOrigin.TEMPLATE_DEFAULT and self.template is not None:
            raise ValueError("template references are only valid for template defaults")

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "target_artifact": self.target_artifact.value,
            "target_path": self.target_path,
            "origin": self.origin.value,
            "source": self.source.to_payload(),
            "materiality": self.materiality.value,
            "confirmation": self.confirmation.value,
        }
        if self.template is not None:
            payload["template"] = self.template.to_payload()
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProvenanceEntry":
        return cls(
            target_artifact=CanonicalArtifact(str(payload["target_artifact"])),
            target_path=str(payload["target_path"]),
            origin=AuthoringOrigin(str(payload["origin"])),
            source=SourceLocation.from_payload(payload["source"]),
            materiality=ScientificMateriality(str(payload["materiality"])),
            confirmation=ConfirmationState(str(payload["confirmation"])),
            template=(
                None
                if payload.get("template") is None
                else TemplateReference.from_payload(payload["template"])
            ),
        )


@dataclass(frozen=True, slots=True)
class AuthoringProvenance:
    """A digest-bound sidecar that never participates in canonical hashes."""

    draft_id: str
    draft_revision: int
    draft_digest: str
    canonical_targets: Mapping[CanonicalArtifact, CanonicalTarget]
    entries: tuple[ProvenanceEntry, ...]
    schema: str = AUTHORING_PROVENANCE_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != AUTHORING_PROVENANCE_SCHEMA_ID:
            raise ValueError(f"unsupported authoring provenance schema: {self.schema}")
        object.__setattr__(self, "draft_id", require_identifier(self.draft_id, "draft_id"))
        revision = int(self.draft_revision)
        if revision <= 0:
            raise ValueError("draft_revision must be positive")
        object.__setattr__(self, "draft_revision", revision)
        object.__setattr__(
            self,
            "draft_digest",
            require_digest(self.draft_digest, "draft_digest"),
        )
        targets = {
            CanonicalArtifact(key): value
            for key, value in self.canonical_targets.items()
        }
        if not all(isinstance(value, CanonicalTarget) for value in targets.values()):
            raise TypeError("canonical targets must be CanonicalTarget values")
        required = {CanonicalArtifact.PROTOCOL, CanonicalArtifact.SUITE}
        if not required.issubset(targets):
            raise ValueError("authoring provenance requires protocol and suite targets")
        object.__setattr__(self, "canonical_targets", MappingProxyType(targets))
        entries = tuple(
            sorted(
                self.entries,
                key=lambda value: (value.target_artifact.value, value.target_path),
            )
        )
        keys = {(value.target_artifact, value.target_path) for value in entries}
        if len(keys) != len(entries):
            raise ValueError("canonical provenance target paths must be unique")
        if not entries:
            raise ValueError("authoring provenance requires at least one entry")
        object.__setattr__(self, "entries", entries)
        validate_authoring_provenance_payload(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "draft_id": self.draft_id,
            "draft_revision": self.draft_revision,
            "draft_digest": self.draft_digest,
            "canonical_targets": {
                key.value: value.to_payload()
                for key, value in sorted(
                    self.canonical_targets.items(), key=lambda item: item[0].value
                )
            },
            "entries": [value.to_payload() for value in self.entries],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AuthoringProvenance":
        validate_authoring_provenance_payload(payload)
        return cls(
            schema=str(payload["schema"]),
            draft_id=str(payload["draft_id"]),
            draft_revision=int(payload["draft_revision"]),
            draft_digest=str(payload["draft_digest"]),
            canonical_targets={
                CanonicalArtifact(str(key)): CanonicalTarget.from_payload(value)
                for key, value in payload["canonical_targets"].items()
            },
            entries=tuple(ProvenanceEntry.from_payload(value) for value in payload["entries"]),
        )

    def entry_for(
        self,
        artifact: CanonicalArtifact,
        target_path: str,
    ) -> ProvenanceEntry | None:
        artifact = CanonicalArtifact(artifact)
        pointer = _require_pointer(target_path, "canonical target path")
        matches: Iterable[ProvenanceEntry] = (
            entry
            for entry in self.entries
            if entry.target_artifact == artifact
            and _pointer_contains(entry.target_path, pointer)
        )
        return max(matches, key=lambda value: len(value.target_path), default=None)


def _pointer_contains(parent: str, child: str) -> bool:
    if parent == "":
        return True
    return child == parent or child.startswith(parent + "/")
