"""Concrete deterministic execution context passed to runtime components."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from eegle._domain import ExecutionMode
from eegle._validation import require_identifier
from eegle.streams.clocks import TimePoint


@dataclass(slots=True)
class DeterministicIdSource:
    """Execution-local IDs stable for an identical ordered trace."""

    _sequences: dict[str, int] = field(default_factory=dict)

    def next(self, namespace: str) -> str:
        normalized = require_identifier(namespace, "id namespace")
        sequence = self._sequences.get(normalized, 0) + 1
        self._sequences[normalized] = sequence
        return f"{normalized}.{sequence:08d}"

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "schema": "eegle.deterministic_id_state.v1",
            "sequences": dict(sorted(self._sequences.items())),
        }

    def restore_state(self, payload: Mapping[str, Any]) -> None:
        if payload.get("schema") != "eegle.deterministic_id_state.v1":
            raise ValueError("unsupported deterministic ID state schema")
        sequences: dict[str, int] = {}
        for namespace, value in dict(payload.get("sequences") or {}).items():
            normalized = require_identifier(str(namespace), "id namespace")
            sequence = int(value)
            if sequence < 0:
                raise ValueError("deterministic ID sequence cannot be negative")
            sequences[normalized] = sequence
        self._sequences = sequences


@dataclass(slots=True)
class RuntimeExecutionContext:
    execution_id: str
    component_id: str
    component_version: str
    execution_mode: ExecutionMode
    current_time: TimePoint
    clock_mapping_revisions: Mapping[str, int]
    _ids: DeterministicIdSource

    def __post_init__(self) -> None:
        self.execution_id = require_identifier(self.execution_id, "execution_id")
        self.component_id = require_identifier(self.component_id, "component_id")
        if not str(self.component_version).strip():
            raise ValueError("component_version cannot be empty")
        self.execution_mode = ExecutionMode(self.execution_mode)

    def next_id(self, namespace: str) -> str:
        return self._ids.next(namespace)
