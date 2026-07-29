"""Canonical test model used by the retained Phase 5 graph fixtures.

The fixtures preserve compiler, graph, phase, and replay evidence. Model
semantics themselves use the definitive Phase 6 manifest/result boundary so
the production runtime needs no legacy prediction or role path.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

from eegle._domain import ComponentKind, Determinism, EquivalenceLevel, ExecutionMode
from eegle.compiler import compile_suite as _compile_suite
from eegle.models import (
    ModelContract,
    ModelImplementationRequirement,
    ModelInputContract,
    ModelManifest,
    ModelOutputContract,
    ModelResult,
    ModelStateContract,
)
from eegle.models.predictions import PREDICTION_RECORD_SCHEMA
from eegle.plugins import PluginCapabilities, PluginDescriptor, PortSpec, StateBehavior
from eegle.processing.windows import DenseWindow
from eegle.specs import SuiteSpec


_DENSE_WINDOW = "eegle.dense_window.v1"
_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
_PLUGIN_ID = "fixture.phase5.mean_threshold"
_PEAK_PLUGIN_ID = "fixture.phase5.peak_threshold"


class Phase5MeanThresholdModel:
    def __init__(self, config: Mapping[str, Any]) -> None:
        self.threshold = float(config.get("threshold", 0.0))
        self.negative_label = str(config.get("negative_label", "negative"))
        self.positive_label = str(config.get("positive_label", "positive"))
        self.latency_seconds = float(config.get("latency_seconds", 0.0))

    def predict(self, item: DenseWindow, context: Any) -> ModelResult:
        valid = np.isfinite(item.values)
        if item.validity_mask is not None:
            valid &= item.validity_mask
        score = float(np.mean(item.values[valid]))
        return ModelResult(
            {
                "label": (
                    self.positive_label
                    if score >= self.threshold
                    else self.negative_label
                ),
                "score": score,
                "threshold": self.threshold,
            },
            completion_delay_seconds=self.latency_seconds,
        )


class Phase5PeakThresholdModel:
    """A genuinely different implementation used for comparison acceptance."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        self.threshold = float(config.get("threshold", 0.0))
        self.negative_label = str(config.get("negative_label", "negative"))
        self.positive_label = str(config.get("positive_label", "positive"))
        self.latency_seconds = float(config.get("latency_seconds", 0.0))

    def predict(self, item: DenseWindow, context: Any) -> ModelResult:
        valid = np.isfinite(item.values)
        if item.validity_mask is not None:
            valid &= item.validity_mask
        score = float(np.max(np.abs(item.values[valid])))
        return ModelResult(
            {
                "label": (
                    self.positive_label
                    if score >= self.threshold
                    else self.negative_label
                ),
                "score": score,
                "threshold": self.threshold,
            },
            completion_delay_seconds=self.latency_seconds,
        )


def phase5_model_manifest() -> ModelManifest:
    return ModelManifest(
        model_id="fixture.phase5.mean-threshold",
        model_version="0.1.0",
        contract=ModelContract(
            inputs=(ModelInputContract("window", _DENSE_WINDOW),),
            outputs=(
                ModelOutputContract(
                    "prediction",
                    PREDICTION_RECORD_SCHEMA,
                    {
                        "type": "object",
                        "required": ["label", "score", "threshold"],
                        "properties": {
                            "label": {"type": "string"},
                            "score": {"type": "number"},
                            "threshold": {"type": "number"},
                        },
                        "additionalProperties": False,
                    },
                ),
            ),
            state=ModelStateContract(replay_equivalence=EquivalenceLevel.NUMERIC),
            supported_modes=frozenset(ExecutionMode),
        ),
        artifacts=(),
        implementations=(ModelImplementationRequirement(_PLUGIN_ID, "~=0.1"),),
        annotations={"scope": "test-only Phase 5 execution evidence"},
    )


def phase5_peak_model_manifest() -> ModelManifest:
    base = phase5_model_manifest()
    return ModelManifest(
        model_id="fixture.phase5.peak-threshold",
        model_version="0.1.0",
        contract=base.contract,
        artifacts=(),
        implementations=(ModelImplementationRequirement(_PEAK_PLUGIN_ID, "~=0.1"),),
        annotations={"scope": "test-only independent peak execution evidence"},
    )


def phase5_model_manifests() -> Mapping[str, ModelManifest]:
    manifests = (phase5_model_manifest(), phase5_peak_model_manifest())
    return {manifest.manifest_digest: manifest for manifest in manifests}


def bind_phase5_models(suite: SuiteSpec) -> SuiteSpec:
    """Add canonical model uses to old graph fixtures before compilation."""

    if suite.model_uses:
        return suite
    payload = suite.to_payload()
    models = [value for value in payload["components"] if value["kind"] == "model"]
    if not models:
        return suite
    manifest = phase5_model_manifest()
    has_shadow = any(value["component_id"].endswith(".shadow") for value in models)
    comparison_group = "comparison.phase5" if has_shadow else None
    uses = []
    for component in models:
        component_id = component["component_id"]
        feeds_policy = any(
            route["source"]["component"] == component_id
            and next(
                value for value in payload["components"]
                if value["component_id"] == route["target"]["component"]
            )["kind"] == "policy"
            for route in payload["routes"]
        )
        role = (
            "shadow"
            if component_id.endswith(".shadow")
            else "primary"
            if feeds_policy
            else "observer"
        )
        uses.append(
            {
                "component_id": component["component_id"],
                "manifest_digest": manifest.manifest_digest,
                "role_id": role,
                "comparison_group": comparison_group,
            }
        )
    payload["model_uses"] = uses
    return SuiteSpec.from_payload(payload)


def compile_phase5_suite(protocol, suite, deployment, registry, **kwargs):
    manifests = dict(phase5_model_manifests())
    manifests.update(kwargs.pop("model_manifests", {}) or {})
    return _compile_suite(
        protocol,
        bind_phase5_models(suite),
        deployment,
        registry,
        model_manifests=manifests,
        **kwargs,
    )


def phase5_plugin_descriptors() -> tuple[PluginDescriptor, ...]:
    common_schema = {
        "$schema": _SCHEMA,
        "type": "object",
        "properties": {
            "model_id": {"type": "string"},
            "threshold": {"type": "number"},
            "negative_label": {"type": "string"},
            "positive_label": {"type": "string"},
            "latency_seconds": {"type": "number", "minimum": 0.0},
        },
        "additionalProperties": False,
    }
    return (
        PluginDescriptor(
            plugin_id=_PLUGIN_ID,
            version="0.1.0",
            kind=ComponentKind.MODEL,
            config_schema=common_schema,
            input_ports=(PortSpec("window", _DENSE_WINDOW),),
            output_ports=(PortSpec("prediction", PREDICTION_RECORD_SCHEMA),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.NUMERIC,
                state_behavior=StateBehavior.STATELESS,
            ),
            factory=Phase5MeanThresholdModel,
            implementation=(
                "tests.fixtures.phase5_model_components:Phase5MeanThresholdModel"
            ),
            distribution="tests",
        ),
        PluginDescriptor(
            plugin_id=_PEAK_PLUGIN_ID,
            version="0.1.0",
            kind=ComponentKind.MODEL,
            config_schema=common_schema,
            input_ports=(PortSpec("window", _DENSE_WINDOW),),
            output_ports=(PortSpec("prediction", PREDICTION_RECORD_SCHEMA),),
            capabilities=PluginCapabilities(
                supported_modes=frozenset(ExecutionMode),
                determinism=Determinism.DETERMINISTIC,
                equivalence=EquivalenceLevel.NUMERIC,
                state_behavior=StateBehavior.STATELESS,
            ),
            factory=Phase5PeakThresholdModel,
            implementation=(
                "tests.fixtures.phase5_model_components:Phase5PeakThresholdModel"
            ),
            distribution="tests",
        ),
    )


def register_phase5_plugins(registry: Any) -> None:
    for descriptor in phase5_plugin_descriptors():
        registry.register(descriptor)
