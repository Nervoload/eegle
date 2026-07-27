"""Independent stateful model plugin used by the Phase 6 wheel gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle.models import ModelResult
from eegle.plugins import (
    ComponentKind,
    Determinism,
    EquivalenceLevel,
    ExecutionMode,
    PluginCapabilities,
    PluginDescriptor,
    PortSpec,
    StateBehavior,
)
from eegle.streams import TimePoint


OBSERVATION_SCHEMA = "fixture.phase6_external_observation.v1"


@dataclass(frozen=True, slots=True)
class Observation:
    record_id: str
    value: float
    available_time: TimePoint
    schema: str = OBSERVATION_SCHEMA

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "record_id": self.record_id,
            "value": self.value,
            "available_time": self.available_time.to_payload(),
        }


class StatefulAccumulator:
    def __init__(self, initial_total: float = 0.0) -> None:
        self.total = float(initial_total)
        self.count = 0

    def predict(self, item: Observation, context: Any) -> ModelResult:
        self.total += float(item.value)
        self.count += 1
        return ModelResult(
            {"total": self.total, "count": self.count},
            validity={"valid": True},
        )

    def snapshot_state(self) -> Mapping[str, Any]:
        return {
            "schema": "fixture.phase6_accumulator_state.v1",
            "total": self.total,
            "count": self.count,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        if state.get("schema") != "fixture.phase6_accumulator_state.v1":
            raise ValueError("unsupported accumulator state")
        self.total = float(state["total"])
        self.count = int(state["count"])


def plugin() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="fixture.external.stateful_model",
        version="1.0.0",
        kind=ComponentKind.MODEL,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"initial_total": {"type": "number"}},
            "additionalProperties": False,
        },
        input_ports=(PortSpec("observation", OBSERVATION_SCHEMA, required=False),),
        output_ports=(PortSpec("prediction", "eegle.prediction.v2"),),
        capabilities=PluginCapabilities(
            supported_modes=frozenset({ExecutionMode.CAUSAL}),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.BITWISE,
            state_behavior=StateBehavior.SNAPSHOT_RESTORE,
        ),
        factory=lambda config: StatefulAccumulator(
            float(config.get("initial_total", 0.0))
        ),
        implementation="phase6_external_model:StatefulAccumulator",
    )
