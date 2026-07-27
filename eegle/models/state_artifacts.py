"""Canonical artifact envelope for model-owned initial state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from packaging.version import Version

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash


MODEL_STATE_ARTIFACT_SCHEMA = "eegle.model_state_artifact.v1"


@dataclass(frozen=True, slots=True)
class ModelStateArtifact:
    model_id: str
    model_version: str
    contract_digest: str
    state_schema_id: str
    state_hash: str
    state: Mapping[str, Any]
    schema: str = MODEL_STATE_ARTIFACT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEL_STATE_ARTIFACT_SCHEMA:
            raise ValueError(f"unsupported model state artifact schema: {self.schema}")
        object.__setattr__(self, "model_id", require_identifier(self.model_id, "model_id"))
        Version(self.model_version)
        object.__setattr__(
            self, "contract_digest", require_digest(self.contract_digest, "contract_digest")
        )
        object.__setattr__(
            self,
            "state_schema_id",
            require_identifier(self.state_schema_id, "state_schema_id"),
        )
        frozen_state = freeze_json(self.state)
        if not isinstance(frozen_state, Mapping):
            raise TypeError("model state artifact state must be a JSON object")
        object.__setattr__(self, "state", frozen_state)
        object.__setattr__(self, "state_hash", require_digest(self.state_hash, "state_hash"))
        if canonical_hash(thaw_json(frozen_state)) != self.state_hash:
            raise ValueError("model state artifact state hash mismatch")

    @classmethod
    def create(
        cls,
        *,
        model_id: str,
        model_version: str,
        contract_digest: str,
        state_schema_id: str,
        state: Mapping[str, Any],
    ) -> "ModelStateArtifact":
        frozen = freeze_json(state)
        return cls(
            model_id=model_id,
            model_version=model_version,
            contract_digest=contract_digest,
            state_schema_id=state_schema_id,
            state_hash=canonical_hash(thaw_json(frozen)),
            state=frozen,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "contract_digest": self.contract_digest,
            "state_schema_id": self.state_schema_id,
            "state_hash": self.state_hash,
            "state": thaw_json(self.state),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelStateArtifact":
        return cls(
            schema=str(payload.get("schema", MODEL_STATE_ARTIFACT_SCHEMA)),
            model_id=str(payload["model_id"]),
            model_version=str(payload["model_version"]),
            contract_digest=str(payload["contract_digest"]),
            state_schema_id=str(payload["state_schema_id"]),
            state_hash=str(payload["state_hash"]),
            state=dict(payload["state"]),
        )
