"""Exact-version compositional presets built from the public design vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle._domain import EquivalenceLevel, ExecutionMode
from eegle.authoring.composition import ExperimentDesign
from eegle.authoring.design import ContractUpdate, ProcessingStep
from eegle.compiler import canonical_hash
from eegle.models import (
    ModelContract,
    ModelImplementationRequirement,
    ModelInputContract,
    ModelManifest,
    ModelOutputContract,
    ModelStateBehavior,
    ModelStateContract,
)


DESIGN_PRESET_SCHEMA_ID = "eegle.design_preset.v1"
DESIGN_PRESET_VERSION = "2.0.0"


def reference_model_manifests() -> tuple[ModelManifest, ...]:
    """Return the artifact-free manifests used by reference plugin projects."""

    contract = ModelContract(
        inputs=(ModelInputContract("window", "eegle.dense_window.v1"),),
        outputs=(
            ModelOutputContract(
                "prediction",
                "eegle.prediction.v2",
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
    )
    adaptive_contract = ModelContract(
        inputs=contract.inputs,
        outputs=contract.outputs,
        state=ModelStateContract(
            behavior=ModelStateBehavior.SNAPSHOT_RESTORE,
            state_schema_id="eegle.example_models.adaptive_mean_state.v1",
            adaptation_supported=True,
            state_affects_predictions=True,
            replay_equivalence=EquivalenceLevel.NUMERIC,
        ),
        supported_modes=contract.supported_modes,
    )
    return (
        ModelManifest(
            model_id="eegle.reference.mean-threshold",
            model_version="1.0.0",
            contract=contract,
            artifacts=(),
            implementations=(
                ModelImplementationRequirement(
                    "eegle.example_models.mean_threshold",
                    "~=1.0",
                ),
            ),
            annotations={"purpose": "Phase 7 compositional reference project"},
        ),
        ModelManifest(
            model_id="eegle.reference.peak-threshold",
            model_version="1.0.0",
            contract=contract,
            artifacts=(),
            implementations=(
                ModelImplementationRequirement(
                    "eegle.example_models.peak_threshold",
                    "~=1.0",
                ),
            ),
            annotations={"purpose": "Phase 7 independent comparison reference"},
        ),
        ModelManifest(
            model_id="eegle.reference.adaptive-mean",
            model_version="1.0.0",
            contract=adaptive_contract,
            artifacts=(),
            implementations=(
                ModelImplementationRequirement(
                    "eegle.example_models.adaptive_mean",
                    "~=1.0",
                ),
            ),
            annotations={"purpose": "Phase 7 delayed-outcome adaptation reference"},
        ),
    )


_REFERENCE_MODEL_MANIFESTS = reference_model_manifests()
REFERENCE_MEAN_MANIFEST_DIGEST = _REFERENCE_MODEL_MANIFESTS[0].manifest_digest
REFERENCE_PEAK_MANIFEST_DIGEST = _REFERENCE_MODEL_MANIFESTS[1].manifest_digest
REFERENCE_ADAPTIVE_MANIFEST_DIGEST = _REFERENCE_MODEL_MANIFESTS[2].manifest_digest


@dataclass(frozen=True, slots=True)
class DesignPreset:
    """One immutable preset revision that expands to an ``ExperimentDesign``."""

    preset_id: str
    version: str
    title: str
    parameters: Mapping[str, Any]
    factory: Callable[[str, Mapping[str, Any]], ExperimentDesign]
    schema: str = DESIGN_PRESET_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != DESIGN_PRESET_SCHEMA_ID:
            raise ValueError(f"unsupported design preset schema: {self.schema}")
        object.__setattr__(self, "preset_id", require_identifier(self.preset_id, "preset_id"))
        if self.version != DESIGN_PRESET_VERSION:
            raise ValueError(f"unsupported compositional preset version: {self.version}")
        if not self.title.strip():
            raise ValueError("design preset title cannot be empty")
        object.__setattr__(self, "parameters", freeze_json(self.parameters))
        if not callable(self.factory):
            raise TypeError("design preset factory must be callable")

    @property
    def manifest_digest(self) -> str:
        return canonical_hash(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "preset_id": self.preset_id,
            "version": self.version,
            "title": self.title,
            "parameters": thaw_json(self.parameters),
            "implementation_revision": "compositional-presets-v1",
        }

    def expand(
        self,
        experiment_id: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> ExperimentDesign:
        supplied = dict(parameters or {})
        unknown = sorted(set(supplied) - set(self.parameters))
        if unknown:
            raise ValueError("unknown preset parameters: " + ", ".join(unknown))
        effective = {**thaw_json(self.parameters), **supplied}
        return self.factory(experiment_id, freeze_json(effective))


class DesignPresetCatalog:
    """Resolve compositional presets by exact identity and revision only."""

    def __init__(self, presets: Iterable[DesignPreset]) -> None:
        values = tuple(sorted(presets, key=lambda value: (value.preset_id, value.version)))
        by_key = {(value.preset_id, value.version): value for value in values}
        if len(by_key) != len(values):
            raise ValueError("design preset identities and versions must be unique")
        self._presets = values
        self._by_key = MappingProxyType(by_key)

    @property
    def presets(self) -> tuple[DesignPreset, ...]:
        return self._presets

    def resolve(self, preset_id: str, version: str) -> DesignPreset:
        try:
            return self._by_key[(str(preset_id), str(version))]
        except KeyError as exc:
            raise KeyError(f"unknown exact design preset revision: {preset_id}@{version}") from exc


_BASE_PARAMETERS = {
    "statement": "Run a bounded EEGle reference experiment.",
    "channels": ["C3", "C4"],
    "unit": "uV",
    "sample_rate_hz": 250.0,
}
_MODEL_PARAMETERS = {
    **_BASE_PARAMETERS,
    "model_plugin_id": "eegle.example_models.mean_threshold",
    "model_manifest_digest": REFERENCE_MEAN_MANIFEST_DIGEST,
    "peak_model_plugin_id": "eegle.example_models.peak_threshold",
    "peak_model_manifest_digest": REFERENCE_PEAK_MANIFEST_DIGEST,
}
_ADAPTIVE_PARAMETERS = {
    **_BASE_PARAMETERS,
    "model_plugin_id": "eegle.example_models.adaptive_mean",
    "model_manifest_digest": REFERENCE_ADAPTIVE_MANIFEST_DIGEST,
}


def _base(experiment_id: str, parameters: Mapping[str, Any]) -> ExperimentDesign:
    channels = tuple(str(value) for value in parameters["channels"])
    if not channels:
        raise ValueError("preset channels cannot be empty")
    return ExperimentDesign.create(experiment_id, str(parameters["statement"])).dense_signal(
        "neural",
        modality="eeg",
        channels=channels,
        unit=str(parameters["unit"]),
        rate_hz=float(parameters["sample_rate_hz"]),
    )


def _recording(experiment_id: str, parameters: Mapping[str, Any]) -> ExperimentDesign:
    return (
        _base(experiment_id, parameters)
        .phase("record", goals=("signal.neural",), initial=True)
        .record("signal.neural", phases=("record",))
    )


def _event_pipeline(
    experiment_id: str,
    parameters: Mapping[str, Any],
) -> ExperimentDesign:
    return (
        _base(experiment_id, parameters)
        .event_stream("markers", kinds=("stimulus",))
        .processing_chain(
            "clean",
            input="signal.neural",
            steps=(
                ProcessingStep(
                    "identity",
                    "eegle.processing.identity",
                    ContractUpdate(),
                ),
            ),
        )
        .event_window(
            "stimulus",
            input="processing.clean",
            event_stream="event.markers",
            event_kind="stimulus",
            start_seconds=-0.2,
            end_seconds=0.8,
        )
    )


def _event_locked_observation(
    experiment_id: str,
    parameters: Mapping[str, Any],
) -> ExperimentDesign:
    return (
        _event_pipeline(experiment_id, parameters)
        .phase("observe", goals=("window.stimulus",), initial=True)
        .record("signal.neural", "event.markers", phases=("observe",))
    )


def _model_design(experiment_id: str, parameters: Mapping[str, Any]) -> ExperimentDesign:
    return _event_pipeline(experiment_id, parameters).model(
        "primary",
        plugin_id=str(parameters["model_plugin_id"]),
        manifest_digest=str(parameters["model_manifest_digest"]),
        role="primary",
        inputs={"window": "window.stimulus"},
        version_spec="~=1.0",
        config={"threshold": 0.0},
    )


def _model_comparison(experiment_id: str, parameters: Mapping[str, Any]) -> ExperimentDesign:
    return (
        _model_design(experiment_id, parameters)
        .model(
            "candidate",
            plugin_id=str(parameters["peak_model_plugin_id"]),
            manifest_digest=str(parameters["peak_model_manifest_digest"]),
            role="candidate",
            inputs={"window": "window.stimulus"},
            version_spec="~=1.0",
            config={"threshold": 0.5},
        )
        .comparison_group("reference", members=("model.primary", "model.candidate"))
        .phase("compare", goals=("model.primary", "model.candidate"), initial=True)
        .record("signal.neural", "event.markers", phases=("compare",))
    )


def _adaptation(experiment_id: str, parameters: Mapping[str, Any]) -> ExperimentDesign:
    design = _model_design(experiment_id, parameters).event_stream(
        "outcomes",
        kinds=("label",),
    )
    return (
        design.outcome(
            "labels",
            event_stream="event.outcomes",
            event_kind="label",
            models=("model.primary",),
            permitted_uses=("metrics", "adaptation"),
        )
        .adaptation(
            "online",
            model="model.primary",
            outcome="outcome.labels",
            enabled_phases=("phase.adapt",),
        )
        .phase("adapt", goals=("model.primary", "outcome.labels"), initial=True)
        .record("signal.neural", "event.markers", "event.outcomes", phases=("adapt",))
    )


def _closed_loop(experiment_id: str, parameters: Mapping[str, Any]) -> ExperimentDesign:
    return (
        _model_design(experiment_id, parameters)
        .policy(
            "feedback",
            plugin_id="eegle.actions.structured_action",
            inputs={"prediction": "model.primary"},
            config={
                "capability": "feedback.vector",
                "output_parameters": {"x": "score"},
                "constant_parameters": {"mode": "simulation"},
            },
        )
        .action(
            "feedback",
            capability="feedback.vector",
            policy="policy.feedback",
            plugin_id="eegle.actions.simulated_actuator",
        )
        .phase("closed-loop", goals=("action.feedback",), initial=True)
        .record("signal.neural", "event.markers", phases=("closed-loop",))
    )


def _lsl_observe_only(experiment_id: str, parameters: Mapping[str, Any]) -> ExperimentDesign:
    return _recording(experiment_id, parameters)


BUILTIN_DESIGN_PRESETS = DesignPresetCatalog(
    (
        DesignPreset(
            "eegle.preset.recording",
            DESIGN_PRESET_VERSION,
            "Continuous recording",
            _BASE_PARAMETERS,
            _recording,
        ),
        DesignPreset(
            "eegle.preset.event_locked_observation",
            DESIGN_PRESET_VERSION,
            "Event-locked observation",
            _BASE_PARAMETERS,
            _event_locked_observation,
        ),
        DesignPreset(
            "eegle.preset.model_comparison",
            DESIGN_PRESET_VERSION,
            "Independent model comparison",
            _MODEL_PARAMETERS,
            _model_comparison,
        ),
        DesignPreset(
            "eegle.preset.adaptation",
            DESIGN_PRESET_VERSION,
            "Delayed-outcome adaptation",
            _ADAPTIVE_PARAMETERS,
            _adaptation,
        ),
        DesignPreset(
            "eegle.preset.simulated_closed_loop",
            DESIGN_PRESET_VERSION,
            "Simulated closed-loop action",
            _MODEL_PARAMETERS,
            _closed_loop,
        ),
        DesignPreset(
            "eegle.preset.lsl_observe_only",
            DESIGN_PRESET_VERSION,
            "LSL observe-only recording",
            _BASE_PARAMETERS,
            _lsl_observe_only,
        ),
    )
)


def expand_design_preset(
    preset_id: str,
    version: str,
    experiment_id: str,
    parameters: Mapping[str, Any] | None = None,
) -> ExperimentDesign:
    return BUILTIN_DESIGN_PRESETS.resolve(preset_id, version).expand(
        experiment_id,
        parameters,
    )


__all__ = [
    "BUILTIN_DESIGN_PRESETS",
    "DESIGN_PRESET_SCHEMA_ID",
    "DESIGN_PRESET_VERSION",
    "DesignPreset",
    "DesignPresetCatalog",
    "expand_design_preset",
    "reference_model_manifests",
]
