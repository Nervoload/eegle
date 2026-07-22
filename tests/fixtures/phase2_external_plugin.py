"""Third-party-style plugin fixture deliberately outside the EEGle package."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from eegle._domain import ComponentKind, Determinism, EquivalenceLevel, ExecutionMode
from eegle.plugins import (
    PluginCapabilities,
    PluginDescriptor,
    PortSpec,
    StateBehavior,
)


class ScaleTransform:
    def __init__(self, scale: float) -> None:
        self.scale = float(scale)

    def update(self, values: np.ndarray) -> np.ndarray:
        return np.asarray(values, dtype=float) * self.scale


def plugin() -> PluginDescriptor:
    return PluginDescriptor(
        plugin_id="fixture.external.scale",
        version="1.2.0",
        kind=ComponentKind.TRANSFORM,
        config_schema={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"scale": {"type": "number"}},
            "required": ["scale"],
            "additionalProperties": False,
        },
        input_ports=(PortSpec("samples", "eegle.dense_sample_batch.v1"),),
        output_ports=(PortSpec("scaled", "eegle.dense_sample_batch.v1"),),
        capabilities=PluginCapabilities(
            supported_modes=frozenset({ExecutionMode.CAUSAL, ExecutionMode.RETROSPECTIVE}),
            determinism=Determinism.DETERMINISTIC,
            equivalence=EquivalenceLevel.NUMERIC,
            state_behavior=StateBehavior.STATELESS,
        ),
        factory=lambda config: ScaleTransform(float(config["scale"])),
        implementation="tests.fixtures.phase2_external_plugin:ScaleTransform",
    )
