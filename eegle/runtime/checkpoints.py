"""Integrity-checked safe-boundary checkpoints for fresh-engine restoration."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.streams.clocks import TimePoint


ENGINE_CHECKPOINT_SCHEMA = "eegle.engine_checkpoint.v1"
ENGINE_IMPLEMENTATION_ID = "eegle.runtime.execution_engine.v1"


@dataclass(frozen=True, slots=True)
class EngineCheckpoint:
    checkpoint_id: str
    execution_id: str
    plan_hash: str
    created_time: TimePoint
    evidence_prefix_digest: str
    state: Mapping[str, Any]
    engine_implementation: str = ENGINE_IMPLEMENTATION_ID
    schema: str = ENGINE_CHECKPOINT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != ENGINE_CHECKPOINT_SCHEMA:
            raise ValueError(f"unsupported engine checkpoint schema: {self.schema}")
        object.__setattr__(
            self, "checkpoint_id", require_identifier(self.checkpoint_id, "checkpoint_id")
        )
        object.__setattr__(
            self, "execution_id", require_identifier(self.execution_id, "execution_id")
        )
        object.__setattr__(self, "plan_hash", require_digest(self.plan_hash, "plan_hash"))
        object.__setattr__(
            self,
            "evidence_prefix_digest",
            require_digest(self.evidence_prefix_digest, "evidence_prefix_digest"),
        )
        if self.engine_implementation != ENGINE_IMPLEMENTATION_ID:
            raise ValueError("unsupported engine checkpoint implementation")
        object.__setattr__(self, "state", freeze_json(self.state))

    @property
    def checkpoint_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "checkpoint_id": self.checkpoint_id,
            "execution_id": self.execution_id,
            "plan_hash": self.plan_hash,
            "engine_implementation": self.engine_implementation,
            "created_time": self.created_time.to_payload(),
            "evidence_prefix_digest": self.evidence_prefix_digest,
            "state": thaw_json(self.state),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["checkpoint_hash"] = self.checkpoint_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "EngineCheckpoint":
        checkpoint = cls(
            schema=str(payload.get("schema", ENGINE_CHECKPOINT_SCHEMA)),
            checkpoint_id=str(payload["checkpoint_id"]),
            execution_id=str(payload["execution_id"]),
            plan_hash=str(payload["plan_hash"]),
            engine_implementation=str(payload["engine_implementation"]),
            created_time=TimePoint.from_payload(payload["created_time"]),
            evidence_prefix_digest=str(payload["evidence_prefix_digest"]),
            state=dict(payload["state"]),
        )
        if payload.get("checkpoint_hash") != checkpoint.checkpoint_hash:
            raise ValueError("engine checkpoint hash mismatch")
        return checkpoint


def write_checkpoint(path: str | Path, checkpoint: EngineCheckpoint) -> Path:
    """Persist an integrity-checked checkpoint using an atomic local replace."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(
        json.dumps(checkpoint.to_payload(), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


def read_checkpoint(path: str | Path) -> EngineCheckpoint:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise TypeError("engine checkpoint file must contain a JSON object")
    return EngineCheckpoint.from_payload(payload)
