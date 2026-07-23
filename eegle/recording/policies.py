"""Versioned privacy, portable-export, redaction, and retention policies."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash, canonical_json_bytes, content_hash
from eegle.recording.artifacts import ArtifactReference, Sensitivity

if TYPE_CHECKING:
    from eegle.recording.bundles import EvidenceReader
    from eegle.recording.session import Session


EXPORT_POLICY_SCHEMA = "eegle.export_policy.v1"
EXPORT_MANIFEST_SCHEMA = "eegle.portable_export.v1"
JSON_REDACTION_SCHEMA = "eegle.json_redaction.v1"
RETENTION_POLICY_SCHEMA = "eegle.retention_policy.v1"


class JsonRedactionAction(str, Enum):
    REMOVE = "remove"
    REPLACE = "replace"


class ExternalReferencePolicy(str, Enum):
    EXCLUDE = "exclude"
    REFERENCE = "reference"


class ExportDisposition(str, Enum):
    INCLUDED = "included"
    REDACTED = "redacted"
    REFERENCED = "referenced"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class JsonFieldRule:
    pointer: str
    action: JsonRedactionAction
    replacement: Any = None

    def __post_init__(self) -> None:
        if not self.pointer.startswith("/") or self.pointer == "/":
            raise ValueError("redaction pointer must address a non-root JSON field")
        object.__setattr__(self, "action", JsonRedactionAction(self.action))
        if self.action == JsonRedactionAction.REMOVE and self.replacement is not None:
            raise ValueError("remove redaction cannot declare a replacement")
        if self.action == JsonRedactionAction.REPLACE:
            object.__setattr__(self, "replacement", freeze_json(self.replacement))

    def to_payload(self) -> dict[str, Any]:
        return {
            "pointer": self.pointer,
            "action": self.action.value,
            "replacement": thaw_json(self.replacement),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "JsonFieldRule":
        return cls(
            pointer=str(payload["pointer"]),
            action=JsonRedactionAction(str(payload["action"])),
            replacement=payload.get("replacement"),
        )


@dataclass(frozen=True, slots=True)
class JsonRedactionSpec:
    source_digest: str
    rules: tuple[JsonFieldRule, ...]
    schema: str = JSON_REDACTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != JSON_REDACTION_SCHEMA:
            raise ValueError(f"unsupported JSON redaction schema: {self.schema}")
        object.__setattr__(self, "source_digest", require_digest(self.source_digest))
        if not self.rules:
            raise ValueError("JSON redaction requires at least one field rule")
        pointers = tuple(rule.pointer for rule in self.rules)
        if len(pointers) != len(set(pointers)):
            raise ValueError("JSON redaction pointers must be unique")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "source_digest": self.source_digest,
            "rules": [rule.to_payload() for rule in self.rules],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "JsonRedactionSpec":
        return cls(
            schema=str(payload.get("schema", JSON_REDACTION_SCHEMA)),
            source_digest=str(payload["source_digest"]),
            rules=tuple(JsonFieldRule.from_payload(item) for item in payload["rules"]),
        )


@dataclass(frozen=True, slots=True)
class ExportPolicy:
    policy_id: str = "portable.safe-default"
    allowed_sensitivities: tuple[Sensitivity, ...] = (
        Sensitivity.PUBLIC,
        Sensitivity.PSEUDONYMIZED,
    )
    include_roles: tuple[str, ...] = ()
    exclude_roles: tuple[str, ...] = ()
    include_source_session_id: bool = False
    include_participant_pseudonym: bool = False
    external_references: ExternalReferencePolicy = ExternalReferencePolicy.EXCLUDE
    redactions: tuple[JsonRedactionSpec, ...] = ()
    schema: str = EXPORT_POLICY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != EXPORT_POLICY_SCHEMA:
            raise ValueError(f"unsupported export policy schema: {self.schema}")
        object.__setattr__(self, "policy_id", require_identifier(self.policy_id, "policy_id"))
        sensitivities = tuple(Sensitivity(value) for value in self.allowed_sensitivities)
        if not sensitivities or len(sensitivities) != len(set(sensitivities)):
            raise ValueError("allowed export sensitivities must be non-empty and unique")
        object.__setattr__(self, "allowed_sensitivities", sensitivities)
        object.__setattr__(
            self,
            "include_roles",
            tuple(require_identifier(value, "include_role") for value in self.include_roles),
        )
        object.__setattr__(
            self,
            "exclude_roles",
            tuple(require_identifier(value, "exclude_role") for value in self.exclude_roles),
        )
        if set(self.include_roles) & set(self.exclude_roles):
            raise ValueError("an artifact role cannot be both included and excluded")
        object.__setattr__(
            self, "external_references", ExternalReferencePolicy(self.external_references)
        )
        digests = tuple(spec.source_digest for spec in self.redactions)
        if len(digests) != len(set(digests)):
            raise ValueError("only one redaction specification is allowed per source digest")

    @property
    def policy_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "policy_id": self.policy_id,
            "allowed_sensitivities": sorted(value.value for value in self.allowed_sensitivities),
            "include_roles": sorted(self.include_roles),
            "exclude_roles": sorted(self.exclude_roles),
            "include_source_session_id": self.include_source_session_id,
            "include_participant_pseudonym": self.include_participant_pseudonym,
            "external_references": self.external_references.value,
            "redactions": [value.to_payload() for value in self.redactions],
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["policy_hash"] = self.policy_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ExportPolicy":
        policy = cls(
            schema=str(payload.get("schema", EXPORT_POLICY_SCHEMA)),
            policy_id=str(payload.get("policy_id", "portable.safe-default")),
            allowed_sensitivities=tuple(
                Sensitivity(str(value)) for value in payload["allowed_sensitivities"]
            ),
            include_roles=tuple(str(value) for value in payload.get("include_roles", ())),
            exclude_roles=tuple(str(value) for value in payload.get("exclude_roles", ())),
            include_source_session_id=bool(payload.get("include_source_session_id", False)),
            include_participant_pseudonym=bool(
                payload.get("include_participant_pseudonym", False)
            ),
            external_references=ExternalReferencePolicy(
                str(payload.get("external_references", "exclude"))
            ),
            redactions=tuple(
                JsonRedactionSpec.from_payload(value)
                for value in payload.get("redactions", ())
            ),
        )
        if payload.get("policy_hash") != policy.policy_hash:
            raise ValueError("export policy hash mismatch")
        return policy


@dataclass(frozen=True, slots=True)
class PortableExportEntry:
    artifact_id: str
    role: str
    sensitivity: Sensitivity
    disposition: ExportDisposition
    source_digest: str
    export_digest: str | None = None
    export_uri: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    external_uri: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", require_identifier(self.artifact_id, "artifact_id"))
        object.__setattr__(self, "role", require_identifier(self.role, "role"))
        object.__setattr__(self, "sensitivity", Sensitivity(self.sensitivity))
        object.__setattr__(self, "disposition", ExportDisposition(self.disposition))
        object.__setattr__(self, "source_digest", require_digest(self.source_digest))
        if self.export_digest is not None:
            object.__setattr__(self, "export_digest", require_digest(self.export_digest))
        if self.size_bytes is not None and int(self.size_bytes) < 0:
            raise ValueError("portable export artifact size cannot be negative")
        if self.disposition in {ExportDisposition.INCLUDED, ExportDisposition.REDACTED}:
            if None in (self.export_digest, self.export_uri, self.media_type, self.size_bytes):
                raise ValueError("included portable artifacts require digest, URI, media type, and size")
            _safe_relative(str(self.export_uri))
            if self.external_uri is not None or self.reason is not None:
                raise ValueError("included portable artifacts cannot declare external URI or reason")
        elif self.disposition == ExportDisposition.REFERENCED:
            if (
                self.external_uri is None
                or self.export_digest != self.source_digest
                or self.media_type is None
                or self.size_bytes is None
            ):
                raise ValueError(
                    "referenced artifacts require an unchanged digest, external URI, "
                    "media type, and size"
                )
            if self.export_uri is not None or self.reason is not None:
                raise ValueError("referenced artifacts cannot declare export URI or exclusion reason")
        elif self.disposition == ExportDisposition.EXCLUDED:
            if self.reason is None:
                raise ValueError("excluded portable artifacts require a reason")
            if any(
                value is not None
                for value in (
                    self.export_digest,
                    self.export_uri,
                    self.media_type,
                    self.size_bytes,
                    self.external_uri,
                )
            ):
                raise ValueError("excluded artifacts cannot expose export content or external URIs")

    def to_payload(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "role": self.role,
            "sensitivity": self.sensitivity.value,
            "disposition": self.disposition.value,
            "source_digest": self.source_digest,
            "export_digest": self.export_digest,
            "export_uri": self.export_uri,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "external_uri": self.external_uri,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PortableExportEntry":
        return cls(
            artifact_id=str(payload["artifact_id"]),
            role=str(payload["role"]),
            sensitivity=Sensitivity(str(payload["sensitivity"])),
            disposition=ExportDisposition(str(payload["disposition"])),
            source_digest=str(payload["source_digest"]),
            export_digest=None
            if payload.get("export_digest") is None
            else str(payload["export_digest"]),
            export_uri=None if payload.get("export_uri") is None else str(payload["export_uri"]),
            media_type=None if payload.get("media_type") is None else str(payload["media_type"]),
            size_bytes=None if payload.get("size_bytes") is None else int(payload["size_bytes"]),
            external_uri=None
            if payload.get("external_uri") is None
            else str(payload["external_uri"]),
            reason=None if payload.get("reason") is None else str(payload["reason"]),
        )


@dataclass(frozen=True, slots=True)
class PortableExportManifest:
    source_bundle_id: str
    source_bundle_hash: str
    source_plan_hash: str
    policy_hash: str
    entries: tuple[PortableExportEntry, ...]
    source_session_id: str | None = None
    participant_pseudonym: str | None = None
    schema: str = EXPORT_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != EXPORT_MANIFEST_SCHEMA:
            raise ValueError(f"unsupported portable export schema: {self.schema}")
        if self.source_session_id is not None:
            object.__setattr__(
                self, "source_session_id", require_identifier(self.source_session_id, "session_id")
            )
        object.__setattr__(
            self, "source_bundle_id", require_identifier(self.source_bundle_id, "bundle_id")
        )
        object.__setattr__(self, "source_bundle_hash", require_digest(self.source_bundle_hash))
        object.__setattr__(self, "source_plan_hash", require_digest(self.source_plan_hash))
        object.__setattr__(self, "policy_hash", require_digest(self.policy_hash))
        if self.participant_pseudonym is not None:
            object.__setattr__(
                self,
                "participant_pseudonym",
                require_identifier(self.participant_pseudonym, "participant_pseudonym"),
            )
        identities = tuple(
            (entry.artifact_id, entry.role, entry.source_digest) for entry in self.entries
        )
        if len(identities) != len(set(identities)):
            raise ValueError("portable export contains duplicate artifact entries")

    @property
    def export_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "source_session_id": self.source_session_id,
            "source_bundle_id": self.source_bundle_id,
            "source_bundle_hash": self.source_bundle_hash,
            "source_plan_hash": self.source_plan_hash,
            "policy_hash": self.policy_hash,
            "participant_pseudonym": self.participant_pseudonym,
            "entries": [value.to_payload() for value in self.entries],
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["export_hash"] = self.export_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PortableExportManifest":
        manifest = cls(
            schema=str(payload.get("schema", EXPORT_MANIFEST_SCHEMA)),
            source_session_id=None
            if payload.get("source_session_id") is None
            else str(payload["source_session_id"]),
            source_bundle_id=str(payload["source_bundle_id"]),
            source_bundle_hash=str(payload["source_bundle_hash"]),
            source_plan_hash=str(payload["source_plan_hash"]),
            policy_hash=str(payload["policy_hash"]),
            participant_pseudonym=None
            if payload.get("participant_pseudonym") is None
            else str(payload["participant_pseudonym"]),
            entries=tuple(
                PortableExportEntry.from_payload(value) for value in payload.get("entries", ())
            ),
        )
        if payload.get("export_hash") != manifest.export_hash:
            raise ValueError("portable export manifest hash mismatch")
        return manifest


def export_evidence_bundle(
    reader: "EvidenceReader",
    destination: str | Path,
    policy: ExportPolicy | None = None,
) -> PortableExportManifest:
    """Create an atomic, portable projection without mutating the source session."""

    selected = policy or ExportPolicy()
    report = reader.verify()
    if not report.valid:
        raise ValueError(f"cannot export bundle with {report.status.value} integrity")
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"export destination already exists: {target}")
    redactions = {value.source_digest: value for value in selected.redactions}
    prepared: list[tuple[PortableExportEntry, bytes | None]] = []
    seen: set[tuple[str, str, str]] = set()
    for reference in reader.bundle.references:
        key = (reference.artifact_id, reference.role, reference.digest)
        if key in seen:
            continue
        seen.add(key)
        entry, data = _prepare_export_entry(reader, reference, selected, redactions)
        prepared.append((entry, data))
    manifest = PortableExportManifest(
        source_session_id=(
            reader.session.session_id if selected.include_source_session_id else None
        ),
        source_bundle_id=reader.bundle.bundle_id,
        source_bundle_hash=reader.bundle.bundle_hash,
        source_plan_hash=reader.bundle.plan_hash,
        policy_hash=selected.policy_hash,
        participant_pseudonym=(
            reader.session.manifest.participant_pseudonym
            if selected.include_participant_pseudonym
            else None
        ),
        entries=tuple(value for value, _ in prepared),
    )
    unused_redactions = set(redactions) - {
        entry.source_digest
        for entry, _ in prepared
        if entry.disposition == ExportDisposition.REDACTED
    }
    if unused_redactions:
        raise ValueError(
            "redaction specifications did not select an exported JSON artifact: "
            + ", ".join(sorted(unused_redactions))
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        for entry, data in prepared:
            if data is None or entry.export_uri is None:
                continue
            output = temporary / entry.export_uri
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
        (temporary / "export.json").write_bytes(canonical_json_bytes(manifest.to_payload()))
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def read_portable_export(root: str | Path) -> PortableExportManifest:
    """Read a portable export and verify every embedded exported object."""

    source = Path(root).expanduser().resolve()
    try:
        payload = json.loads((source / "export.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid portable export manifest: {source / 'export.json'}") from exc
    if not isinstance(payload, dict):
        raise ValueError("portable export manifest must be a JSON object")
    manifest = PortableExportManifest.from_payload(payload)
    for entry in manifest.entries:
        if entry.disposition not in {ExportDisposition.INCLUDED, ExportDisposition.REDACTED}:
            continue
        path = source / _safe_relative(str(entry.export_uri))
        if not path.is_file():
            raise ValueError(f"portable export object is missing: {entry.artifact_id}")
        data = path.read_bytes()
        if len(data) != entry.size_bytes:
            raise ValueError(f"portable export object size mismatch: {entry.artifact_id}")
        if content_hash(data) != entry.export_digest:
            raise ValueError(f"portable export object digest mismatch: {entry.artifact_id}")
    return manifest


def _prepare_export_entry(
    reader: "EvidenceReader",
    reference: ArtifactReference,
    policy: ExportPolicy,
    redactions: Mapping[str, JsonRedactionSpec],
) -> tuple[PortableExportEntry, bytes | None]:
    reason = _exclusion_reason(reference, policy)
    if reason is not None:
        return (
            PortableExportEntry(
                reference.artifact_id,
                reference.role,
                reference.sensitivity,
                ExportDisposition.EXCLUDED,
                reference.digest,
                reason=reason,
            ),
            None,
        )
    if not reference.embedded:
        if policy.external_references == ExternalReferencePolicy.EXCLUDE:
            return (
                PortableExportEntry(
                    reference.artifact_id,
                    reference.role,
                    reference.sensitivity,
                    ExportDisposition.EXCLUDED,
                    reference.digest,
                    reason="external references are excluded by policy",
                ),
                None,
            )
        return (
            PortableExportEntry(
                reference.artifact_id,
                reference.role,
                reference.sensitivity,
                ExportDisposition.REFERENCED,
                reference.digest,
                export_digest=reference.digest,
                media_type=reference.media_type,
                size_bytes=reference.size_bytes,
                external_uri=reference.uri,
            ),
            None,
        )
    data = reader.session.artifacts.resolve(reference).read_bytes()
    disposition = ExportDisposition.INCLUDED
    redaction = redactions.get(reference.digest)
    if redaction is not None:
        if reference.media_type != "application/json":
            raise ValueError(
                f"redaction target {reference.artifact_id} is not an application/json artifact"
            )
        payload = _json_object(data, reference.artifact_id)
        for rule in redaction.rules:
            _apply_rule(payload, rule)
        data = canonical_json_bytes(payload)
        disposition = ExportDisposition.REDACTED
    if reference.media_type == "application/json":
        _reject_secret_fields(_json_object(data, reference.artifact_id), reference.artifact_id)
    digest = content_hash(data)
    uri = f"objects/{digest.removeprefix('sha256:')}"
    return (
        PortableExportEntry(
            reference.artifact_id,
            reference.role,
            reference.sensitivity,
            disposition,
            reference.digest,
            export_digest=digest,
            export_uri=uri,
            media_type=reference.media_type,
            size_bytes=len(data),
        ),
        data,
    )


def _exclusion_reason(reference: ArtifactReference, policy: ExportPolicy) -> str | None:
    if reference.sensitivity not in policy.allowed_sensitivities:
        return f"{reference.sensitivity.value} sensitivity is not allowed"
    if policy.include_roles and reference.role not in policy.include_roles:
        return "artifact role is not selected"
    if reference.role in policy.exclude_roles:
        return "artifact role is explicitly excluded"
    return None


def _json_object(data: bytes, artifact_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON artifact is invalid: {artifact_id}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON artifact must contain an object: {artifact_id}")
    return payload


def _apply_rule(payload: dict[str, Any], rule: JsonFieldRule) -> None:
    parts = [_unescape_pointer(value) for value in rule.pointer.split("/")[1:]]
    parent: Any = payload
    for part in parts[:-1]:
        if isinstance(parent, dict) and part in parent:
            parent = parent[part]
        elif isinstance(parent, list) and part.isdigit() and int(part) < len(parent):
            parent = parent[int(part)]
        else:
            raise ValueError(f"redaction pointer does not exist: {rule.pointer}")
    final = parts[-1]
    if isinstance(parent, dict) and final in parent:
        if rule.action == JsonRedactionAction.REMOVE:
            del parent[final]
        else:
            parent[final] = thaw_json(rule.replacement)
        return
    if isinstance(parent, list) and final.isdigit() and int(final) < len(parent):
        index = int(final)
        if rule.action == JsonRedactionAction.REMOVE:
            del parent[index]
        else:
            parent[index] = thaw_json(rule.replacement)
        return
    raise ValueError(f"redaction pointer does not exist: {rule.pointer}")


def _unescape_pointer(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "~":
            result.append(value[index])
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            raise ValueError("invalid JSON pointer escape")
        result.append("~" if value[index + 1] == "0" else "/")
        index += 2
    return "".join(result)


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("portable export URI must be a safe relative path")
    return path


_SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "api_token",
        "access_token",
        "refresh_token",
        "api_key",
        "secret",
        "client_secret",
        "credential",
        "credentials",
        "private_key",
    }
)


def _reject_secret_fields(value: Any, artifact_id: str, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = key.casefold().replace("-", "_").replace(" ", "_")
            if normalized in _SECRET_KEYS:
                raise ValueError(
                    f"portable export rejected secret-shaped field {path}.{key} "
                    f"in artifact {artifact_id}; remove it with an explicit redaction rule"
                )
            _reject_secret_fields(child, artifact_id, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_fields(child, artifact_id, f"{path}[{index}]")


class RetentionAction(str, Enum):
    RETAIN = "retain"
    REVIEW = "review"
    DELETE_AFTER = "delete_after"
    EXTERNAL_ONLY = "external_only"


@dataclass(frozen=True, slots=True)
class RetentionRule:
    sensitivity: Sensitivity
    action: RetentionAction
    after_days: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "sensitivity", Sensitivity(self.sensitivity))
        object.__setattr__(self, "action", RetentionAction(self.action))
        if self.action == RetentionAction.DELETE_AFTER:
            if self.after_days is None or int(self.after_days) <= 0:
                raise ValueError("delete_after retention requires positive after_days")
            object.__setattr__(self, "after_days", int(self.after_days))
        elif self.after_days is not None:
            raise ValueError("after_days is only valid for delete_after retention")

    def to_payload(self) -> dict[str, Any]:
        return {
            "sensitivity": self.sensitivity.value,
            "action": self.action.value,
            "after_days": self.after_days,
        }


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    policy_id: str = "retention.review-default"
    rules: tuple[RetentionRule, ...] = (
        RetentionRule(Sensitivity.PUBLIC, RetentionAction.RETAIN),
        RetentionRule(Sensitivity.PSEUDONYMIZED, RetentionAction.REVIEW),
        RetentionRule(Sensitivity.INTERNAL, RetentionAction.REVIEW),
        RetentionRule(Sensitivity.RESTRICTED, RetentionAction.REVIEW),
    )
    schema: str = RETENTION_POLICY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != RETENTION_POLICY_SCHEMA:
            raise ValueError(f"unsupported retention policy schema: {self.schema}")
        object.__setattr__(self, "policy_id", require_identifier(self.policy_id, "policy_id"))
        sensitivities = tuple(rule.sensitivity for rule in self.rules)
        if set(sensitivities) != set(Sensitivity) or len(sensitivities) != len(set(sensitivities)):
            raise ValueError("retention policy requires exactly one rule for every sensitivity")

    @property
    def policy_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "policy_id": self.policy_id,
            "rules": [
                rule.to_payload()
                for rule in sorted(self.rules, key=lambda value: value.sensitivity.value)
            ],
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["policy_hash"] = self.policy_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RetentionPolicy":
        policy = cls(
            schema=str(payload.get("schema", RETENTION_POLICY_SCHEMA)),
            policy_id=str(payload.get("policy_id", "retention.review-default")),
            rules=tuple(
                RetentionRule(
                    Sensitivity(str(value["sensitivity"])),
                    RetentionAction(str(value["action"])),
                    None if value.get("after_days") is None else int(value["after_days"]),
                )
                for value in payload["rules"]
            ),
        )
        if payload.get("policy_hash") != policy.policy_hash:
            raise ValueError("retention policy hash mismatch")
        return policy


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    qualified_artifact_id: str
    sensitivity: Sensitivity
    action: RetentionAction
    after_days: int | None
    embedded: bool


def evaluate_retention(
    session: "Session", policy: RetentionPolicy | None = None
) -> tuple[RetentionDecision, ...]:
    """Return deterministic decisions; this function never changes or deletes data."""

    selected = policy or RetentionPolicy()
    rules = {rule.sensitivity: rule for rule in selected.rules}
    return tuple(
        RetentionDecision(
            entry.qualified_id,
            entry.reference.sensitivity,
            rules[entry.reference.sensitivity].action,
            rules[entry.reference.sensitivity].after_days,
            entry.reference.embedded,
        )
        for entry in sorted(session.artifacts.entries, key=lambda value: value.qualified_id)
    )
