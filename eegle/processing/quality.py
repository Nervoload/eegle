"""General quality decisions, separate from modality-specific signal rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping

import numpy as np

from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.streams.clocks import TimePoint
from eegle.processing.windows import DenseWindow
from eegle.streams.packets import DenseSampleBatch

if TYPE_CHECKING:
    from eegle.plugins.contracts import ExecutionContext


QUALITY_DECISION_SCHEMA = "eegle.quality_decision.v1"


class QualityStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WARNING = "warning"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class QualityDecision:
    decision_id: str
    item_id: str
    gate_id: str
    status: QualityStatus
    decided_time: TimePoint
    reasons: tuple[str, ...] = ()
    metrics: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = QUALITY_DECISION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != QUALITY_DECISION_SCHEMA:
            raise ValueError(f"unsupported quality decision schema: {self.schema}")
        for field in ("decision_id", "item_id", "gate_id"):
            object.__setattr__(self, field, require_identifier(getattr(self, field), field))
        object.__setattr__(self, "status", QualityStatus(self.status))
        object.__setattr__(self, "reasons", tuple(str(reason) for reason in self.reasons))
        object.__setattr__(self, "metrics", freeze_json(self.metrics or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "decision_id": self.decision_id,
            "item_id": self.item_id,
            "gate_id": self.gate_id,
            "status": self.status.value,
            "decided_time": self.decided_time.to_payload(),
            "reasons": list(self.reasons),
            "metrics": thaw_json(self.metrics),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "QualityDecision":
        return cls(
            schema=str(payload.get("schema", QUALITY_DECISION_SCHEMA)),
            decision_id=str(payload["decision_id"]),
            item_id=str(payload["item_id"]),
            gate_id=str(payload["gate_id"]),
            status=QualityStatus(str(payload["status"])),
            decided_time=TimePoint.from_payload(payload["decided_time"]),
            reasons=tuple(str(value) for value in payload.get("reasons", ())),
            metrics=dict(payload.get("metrics") or {}),
        )


class FiniteQualityGate:
    def __init__(self, gate_id: str = "eegle.finite", minimum_valid_fraction: float = 1.0) -> None:
        self.gate_id = require_identifier(gate_id, "gate_id")
        self.minimum_valid_fraction = float(minimum_valid_fraction)
        if not 0.0 <= self.minimum_valid_fraction <= 1.0:
            raise ValueError("minimum_valid_fraction must be in [0, 1]")

    def evaluate(
        self,
        item: Any,
        context: "ExecutionContext",
    ) -> QualityDecision:
        if not isinstance(item, (DenseSampleBatch, DenseWindow)):
            raise TypeError("finite quality gate requires dense packet or window input")
        dense = item
        if context.current_time.clock_id != dense.available_time.clock_id:
            raise ValueError("quality decision and packet availability must use the execution clock")
        if context.current_time.seconds < dense.available_time.seconds:
            raise ValueError("quality gate cannot inspect an item before it is available")
        finite = np.isfinite(dense.values)
        if dense.validity_mask is None:
            valid = finite
        else:
            valid = finite & dense.validity_mask
        valid_fraction = float(np.mean(valid))
        accepted = valid_fraction >= self.minimum_valid_fraction
        return QualityDecision(
            decision_id=context.next_id("quality"),
            item_id=(dense.batch_id if isinstance(dense, DenseSampleBatch) else dense.window_id),
            gate_id=self.gate_id,
            status=QualityStatus.ACCEPTED if accepted else QualityStatus.REJECTED,
            decided_time=context.current_time,
            reasons=() if accepted else ("insufficient_valid_values",),
            metrics={"valid_fraction": valid_fraction},
        )
