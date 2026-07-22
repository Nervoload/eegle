"""General model roles and permissions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import require_identifier


MODEL_ROLE_SCHEMA = "eegle.model_role.v1"


class ModelRoleKind(str, Enum):
    PRIMARY = "primary"
    SHADOW = "shadow"
    CANDIDATE = "candidate"
    OBSERVER = "observer"


@dataclass(frozen=True, slots=True)
class ModelRole:
    role_id: str
    kind: ModelRoleKind
    scheduling_priority: int
    may_request_actions: bool = False
    must_share_admitted_inputs_with: tuple[str, ...] = ()
    schema: str = MODEL_ROLE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEL_ROLE_SCHEMA:
            raise ValueError(f"unsupported model role schema: {self.schema}")
        object.__setattr__(self, "role_id", require_identifier(self.role_id, "role_id"))
        object.__setattr__(self, "kind", ModelRoleKind(self.kind))
        object.__setattr__(self, "scheduling_priority", int(self.scheduling_priority))
        object.__setattr__(
            self,
            "must_share_admitted_inputs_with",
            tuple(
                require_identifier(value, "comparison_role_id")
                for value in self.must_share_admitted_inputs_with
            ),
        )
        if self.kind in {ModelRoleKind.SHADOW, ModelRoleKind.OBSERVER} and self.may_request_actions:
            raise ValueError(f"{self.kind.value} roles cannot request actions")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "role_id": self.role_id,
            "kind": self.kind.value,
            "scheduling_priority": self.scheduling_priority,
            "may_request_actions": self.may_request_actions,
            "must_share_admitted_inputs_with": list(self.must_share_admitted_inputs_with),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelRole":
        return cls(
            schema=str(payload.get("schema", MODEL_ROLE_SCHEMA)),
            role_id=str(payload["role_id"]),
            kind=ModelRoleKind(str(payload["kind"])),
            scheduling_priority=int(payload["scheduling_priority"]),
            may_request_actions=bool(payload.get("may_request_actions", False)),
            must_share_admitted_inputs_with=tuple(
                str(value) for value in payload.get("must_share_admitted_inputs_with", ())
            ),
        )
