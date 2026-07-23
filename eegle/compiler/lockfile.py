"""Deterministic lock manifest for a compiled execution plan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.compiler.plan import ExecutionPlan, LockedPlugin


EXECUTION_LOCK_SCHEMA = "eegle.execution_lock.v1"


@dataclass(frozen=True, slots=True)
class ExecutionLock:
    lock_id: str
    plan_hash: str
    spec_hashes: Mapping[str, str]
    plugins: tuple[LockedPlugin, ...]
    component_hashes: Mapping[str, str]
    schema_hashes: Mapping[str, str]
    graph_hash: str
    artifact_hashes: Mapping[str, str] = None  # type: ignore[assignment]
    schema: str = EXECUTION_LOCK_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != EXECUTION_LOCK_SCHEMA:
            raise ValueError(f"unsupported execution lock schema: {self.schema}")
        object.__setattr__(self, "lock_id", require_identifier(self.lock_id, "lock_id"))
        object.__setattr__(self, "plan_hash", require_digest(self.plan_hash, "plan_hash"))
        object.__setattr__(self, "graph_hash", require_digest(self.graph_hash, "graph_hash"))
        plugin_keys = tuple((value.plugin_id, value.version) for value in self.plugins)
        if len(plugin_keys) != len(set(plugin_keys)):
            raise ValueError("execution lock plugins must be unique")
        for field in ("spec_hashes", "component_hashes", "schema_hashes", "artifact_hashes"):
            values = getattr(self, field) or {}
            normalized = {
                require_identifier(str(key), f"{field} key"): require_digest(
                    str(value), f"{field}[{key}]"
                )
                for key, value in values.items()
            }
            object.__setattr__(self, field, freeze_json(normalized))

    @property
    def lock_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "lock_id": self.lock_id,
            "plan_hash": self.plan_hash,
            "spec_hashes": thaw_json(self.spec_hashes),
            "plugins": [value.to_payload() for value in self.plugins],
            "component_hashes": thaw_json(self.component_hashes),
            "schema_hashes": thaw_json(self.schema_hashes),
            "graph_hash": self.graph_hash,
            "artifact_hashes": thaw_json(self.artifact_hashes),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["lock_hash"] = self.lock_hash
        return payload

    def verify_plan(self, plan: ExecutionPlan) -> None:
        if plan.plan_hash != self.plan_hash:
            raise ValueError(
                f"lock plan hash mismatch: expected {self.plan_hash}, observed {plan.plan_hash}"
            )
        if dict(plan.spec_hashes) != dict(self.spec_hashes):
            raise ValueError("lock specification hashes differ from execution plan")
        expected_components = {
            value.component_id: canonical_hash(value.to_payload()) for value in plan.components
        }
        if expected_components != dict(self.component_hashes):
            raise ValueError("lock component hashes differ from execution plan")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ExecutionLock":
        value = cls(
            schema=str(payload.get("schema", EXECUTION_LOCK_SCHEMA)),
            lock_id=str(payload["lock_id"]),
            plan_hash=str(payload["plan_hash"]),
            spec_hashes={str(key): str(item) for key, item in payload["spec_hashes"].items()},
            plugins=tuple(LockedPlugin.from_payload(value) for value in payload["plugins"]),
            component_hashes={
                str(key): str(item) for key, item in payload["component_hashes"].items()
            },
            schema_hashes={
                str(key): str(item) for key, item in payload["schema_hashes"].items()
            },
            graph_hash=str(payload["graph_hash"]),
            artifact_hashes={
                str(key): str(item)
                for key, item in dict(payload.get("artifact_hashes") or {}).items()
            },
        )
        if payload.get("lock_hash") != value.lock_hash:
            raise ValueError("execution lock hash mismatch")
        return value
