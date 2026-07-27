"""Canonical, path-free manifests for scientific model artifacts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.models.contracts import ModelContract
from eegle.recording.artifacts import ArtifactReference


MODEL_MANIFEST_SCHEMA = "eegle.model_manifest.v1"
MODEL_IMPLEMENTATION_REQUIREMENT_SCHEMA = "eegle.model_implementation_requirement.v1"


@dataclass(frozen=True, slots=True)
class ModelImplementationRequirement:
    """A compatible executable plugin requirement, not a construction factory."""

    plugin_id: str
    version_spec: str
    schema: str = MODEL_IMPLEMENTATION_REQUIREMENT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEL_IMPLEMENTATION_REQUIREMENT_SCHEMA:
            raise ValueError(
                f"unsupported model implementation requirement schema: {self.schema}"
            )
        object.__setattr__(self, "plugin_id", require_identifier(self.plugin_id, "plugin_id"))
        if not str(self.version_spec).strip():
            raise ValueError("model implementation version_spec cannot be empty")
        SpecifierSet(self.version_spec)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plugin_id": self.plugin_id,
            "version_spec": self.version_spec,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelImplementationRequirement":
        return cls(
            schema=str(
                payload.get("schema", MODEL_IMPLEMENTATION_REQUIREMENT_SCHEMA)
            ),
            plugin_id=str(payload["plugin_id"]),
            version_spec=str(payload["version_spec"]),
        )


@dataclass(frozen=True, slots=True)
class ModelManifest:
    """Portable scientific identity for one model artifact set.

    Artifact references use logical ``artifact://`` URIs. Deployment resolves
    those identities to local or remote materializations; the manifest never
    stores a machine path or performs copying, downloading, framework imports,
    or runtime environment inspection.
    """

    model_id: str
    model_version: str
    contract: ModelContract
    artifacts: tuple[ArtifactReference, ...]
    implementations: tuple[ModelImplementationRequirement, ...]
    initial_state_artifact_id: str | None = None
    training_provenance: Mapping[str, Any] = field(default_factory=dict)
    evaluation_provenance: Mapping[str, Any] = field(default_factory=dict)
    license: str | None = None
    annotations: Mapping[str, Any] = field(default_factory=dict)
    schema: str = MODEL_MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEL_MANIFEST_SCHEMA:
            raise ValueError(f"unsupported model manifest schema: {self.schema}")
        object.__setattr__(self, "model_id", require_identifier(self.model_id, "model_id"))
        Version(self.model_version)
        artifact_ids = tuple(value.artifact_id for value in self.artifacts)
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("model manifest artifact identities must be unique")
        for artifact in self.artifacts:
            parsed = urlparse(artifact.uri)
            if parsed.scheme != "artifact":
                raise ValueError(
                    "portable model manifest artifacts require logical artifact:// URIs"
                )
        if not self.implementations:
            raise ValueError("model manifest requires at least one implementation requirement")
        plugin_ids = tuple(value.plugin_id for value in self.implementations)
        if len(plugin_ids) != len(set(plugin_ids)):
            raise ValueError("model implementation plugin identities must be unique")
        if self.initial_state_artifact_id is not None:
            object.__setattr__(
                self,
                "initial_state_artifact_id",
                require_identifier(self.initial_state_artifact_id, "initial_state_artifact_id"),
            )
            if self.initial_state_artifact_id not in set(artifact_ids):
                raise ValueError("initial state artifact is not declared by the model manifest")
        if self.contract.state.initial_state_required and self.initial_state_artifact_id is None:
            raise ValueError("model contract requires an initial state artifact")
        if self.license is not None and not self.license.strip():
            raise ValueError("model manifest license cannot be empty")
        object.__setattr__(
            self,
            "training_provenance",
            freeze_json(self.training_provenance or {}),
        )
        object.__setattr__(
            self,
            "evaluation_provenance",
            freeze_json(self.evaluation_provenance or {}),
        )
        object.__setattr__(self, "annotations", freeze_json(self.annotations or {}))

    @property
    def manifest_digest(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "contract": self.contract.to_payload(),
            "contract_digest": self.contract.contract_digest,
            "artifacts": [value.to_payload() for value in self.artifacts],
            "implementations": [value.to_payload() for value in self.implementations],
            "initial_state_artifact_id": self.initial_state_artifact_id,
            "training_provenance": thaw_json(self.training_provenance),
            "evaluation_provenance": thaw_json(self.evaluation_provenance),
            "license": self.license,
            "annotations": thaw_json(self.annotations),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["manifest_digest"] = self.manifest_digest
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelManifest":
        contract = ModelContract.from_payload(payload["contract"])
        if payload.get("contract_digest") != contract.contract_digest:
            raise ValueError("model manifest contract digest mismatch")
        value = cls(
            schema=str(payload.get("schema", MODEL_MANIFEST_SCHEMA)),
            model_id=str(payload["model_id"]),
            model_version=str(payload["model_version"]),
            contract=contract,
            artifacts=tuple(
                ArtifactReference.from_payload(item) for item in payload.get("artifacts", ())
            ),
            implementations=tuple(
                ModelImplementationRequirement.from_payload(item)
                for item in payload.get("implementations", ())
            ),
            initial_state_artifact_id=None
            if payload.get("initial_state_artifact_id") is None
            else str(payload["initial_state_artifact_id"]),
            training_provenance=dict(payload.get("training_provenance") or {}),
            evaluation_provenance=dict(payload.get("evaluation_provenance") or {}),
            license=None if payload.get("license") is None else str(payload["license"]),
            annotations=dict(payload.get("annotations") or {}),
        )
        if payload.get("manifest_digest") != value.manifest_digest:
            raise ValueError("model manifest digest mismatch")
        return value
