"""Versioned, deployment-resolved construction inputs for executable plugins."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from eegle._validation import require_digest, require_identifier


@dataclass(frozen=True, slots=True)
class MaterializedArtifact:
    """One digest-verified local materialization admitted for construction."""

    artifact_id: str
    digest: str
    media_type: str
    size_bytes: int
    location: Path

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "artifact_id", require_identifier(self.artifact_id, "artifact_id")
        )
        object.__setattr__(self, "digest", require_digest(self.digest))
        if not self.media_type.strip():
            raise ValueError("materialized artifact media_type cannot be empty")
        object.__setattr__(self, "size_bytes", int(self.size_bytes))
        if self.size_bytes < 0:
            raise ValueError("materialized artifact size cannot be negative")
        object.__setattr__(self, "location", Path(self.location).resolve())


@dataclass(frozen=True, slots=True)
class ModelConstructionContext:
    """Plan-owned artifact context supplied only to model-context factories."""

    component_id: str
    manifest_digest: str
    contract_digest: str
    artifacts: Mapping[str, MaterializedArtifact]
    initial_state_artifact_id: str | None = None

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
        values = dict(self.artifacts)
        if set(values) != {value.artifact_id for value in values.values()}:
            raise ValueError("model construction artifact keys must equal artifact identities")
        object.__setattr__(self, "artifacts", MappingProxyType(values))
        if self.initial_state_artifact_id is not None:
            object.__setattr__(
                self,
                "initial_state_artifact_id",
                require_identifier(
                    self.initial_state_artifact_id, "initial_state_artifact_id"
                ),
            )
            if self.initial_state_artifact_id not in values:
                raise ValueError("initial state artifact is not materialized")
