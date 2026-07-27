"""Framework-neutral model artifact materialization and state admission."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from eegle._validation import require_digest, require_identifier, thaw_json
from eegle.compiler.plan import PlannedModelArtifactBinding, PlannedModelBinding
from eegle.models.state_artifacts import ModelStateArtifact
from eegle.plugins.construction import MaterializedArtifact, ModelConstructionContext
from eegle.plugins.registry import StateBehavior
from eegle.runtime.canonical_state import capture_canonical_state


class ArtifactResolver(Protocol):
    def materialize(
        self, binding: PlannedModelArtifactBinding
    ) -> MaterializedArtifact:
        ...


class LocalFileArtifactResolver:
    """Resolve explicit local ``file:`` URIs and verify bytes independently."""

    def materialize(
        self, binding: PlannedModelArtifactBinding
    ) -> MaterializedArtifact:
        parsed = urlsplit(binding.uri)
        if parsed.scheme != "file":
            raise ValueError(
                f"artifact {binding.artifact_id} requires a resolver for {parsed.scheme!r} URIs"
            )
        path_text = url2pathname(unquote(parsed.path))
        if parsed.netloc and parsed.netloc != "localhost":
            path_text = f"//{parsed.netloc}{path_text}"
        path = Path(path_text).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(
                f"model artifact {binding.artifact_id} is unavailable at {path}"
            )
        digest, size = _hash_file(path)
        if size != binding.size_bytes:
            raise ValueError(
                f"model artifact {binding.artifact_id} size mismatch: "
                f"expected {binding.size_bytes}, observed {size}"
            )
        if digest != binding.digest:
            raise ValueError(
                f"model artifact {binding.artifact_id} digest mismatch: "
                f"expected {binding.digest}, observed {digest}"
            )
        return MaterializedArtifact(
            binding.artifact_id,
            digest,
            binding.media_type,
            size,
            path,
        )


@dataclass(frozen=True, slots=True)
class ModelAdmissionReceipt:
    component_id: str
    manifest_digest: str
    contract_digest: str
    artifact_digests: Mapping[str, str]
    initial_state_artifact_id: str | None = None
    restored_state_hash: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component_id", require_identifier(self.component_id, "component_id")
        )
        object.__setattr__(
            self, "manifest_digest", require_digest(self.manifest_digest, "manifest_digest")
        )
        object.__setattr__(
            self, "contract_digest", require_digest(self.contract_digest, "contract_digest")
        )
        digests = {
            require_identifier(key, "artifact_id"): require_digest(value)
            for key, value in self.artifact_digests.items()
        }
        object.__setattr__(self, "artifact_digests", MappingProxyType(digests))
        if self.initial_state_artifact_id is not None:
            object.__setattr__(
                self,
                "initial_state_artifact_id",
                require_identifier(
                    self.initial_state_artifact_id, "initial_state_artifact_id"
                ),
            )
        if self.restored_state_hash is not None:
            object.__setattr__(
                self,
                "restored_state_hash",
                require_digest(self.restored_state_hash, "restored_state_hash"),
            )


def materialize_model_context(
    binding: PlannedModelBinding,
    resolver: ArtifactResolver,
) -> ModelConstructionContext:
    materialized: dict[str, MaterializedArtifact] = {}
    for planned in binding.artifacts:
        artifact = resolver.materialize(planned)
        expected = (
            planned.artifact_id,
            planned.digest,
            planned.media_type,
            planned.size_bytes,
        )
        observed = (
            artifact.artifact_id,
            artifact.digest,
            artifact.media_type,
            artifact.size_bytes,
        )
        if observed != expected:
            raise ValueError(
                f"artifact resolver returned mismatched admission data for {planned.artifact_id}"
            )
        materialized[artifact.artifact_id] = artifact
    return ModelConstructionContext(
        component_id=binding.component_id,
        manifest_digest=binding.manifest_digest,
        contract_digest=binding.contract_digest,
        artifacts=materialized,
        initial_state_artifact_id=binding.manifest.initial_state_artifact_id,
    )


def admit_model_instance(
    binding: PlannedModelBinding,
    context: ModelConstructionContext,
    component: object,
    *,
    state_behavior: StateBehavior,
) -> ModelAdmissionReceipt:
    initial_id = binding.manifest.initial_state_artifact_id
    restored_hash: str | None = None
    if initial_id is not None:
        if state_behavior != StateBehavior.SNAPSHOT_RESTORE:
            raise TypeError("initial model state requires snapshot/restore runtime behavior")
        artifact = context.artifacts[initial_id]
        try:
            payload = json.loads(artifact.location.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("initial model state artifact is not valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ValueError("initial model state artifact must contain a JSON object")
        envelope = ModelStateArtifact.from_payload(payload)
        contract = binding.manifest.contract.state
        expected = (
            binding.manifest.model_id,
            binding.manifest.model_version,
            binding.contract_digest,
            contract.state_schema_id,
        )
        observed = (
            envelope.model_id,
            envelope.model_version,
            envelope.contract_digest,
            envelope.state_schema_id,
        )
        if observed != expected:
            raise ValueError("initial model state identity differs from the locked model binding")
        restore = getattr(component, "restore_state", None)
        if not callable(restore):
            raise TypeError("initial model state requires restore_state()")
        restore(thaw_json(envelope.state))
        restored = capture_canonical_state(component)
        if restored.state_hash != envelope.state_hash:
            raise ValueError("model did not restore the exact admitted initial state")
        restored_hash = restored.state_hash
    elif state_behavior == StateBehavior.SNAPSHOT_RESTORE:
        restored_hash = capture_canonical_state(component).state_hash
    return ModelAdmissionReceipt(
        component_id=binding.component_id,
        manifest_digest=binding.manifest_digest,
        contract_digest=binding.contract_digest,
        artifact_digests={
            artifact_id: value.digest
            for artifact_id, value in context.artifacts.items()
        },
        initial_state_artifact_id=initial_id,
        restored_state_hash=restored_hash,
    )


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size
