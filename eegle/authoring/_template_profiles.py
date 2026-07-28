"""Internal canonical builders for the bounded built-in template profiles."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from eegle._domain import ComponentKind
from eegle._validation import thaw_json
from eegle.authoring.contracts import (
    AuthoringOrigin,
    CanonicalArtifact,
    ConfirmationState,
    ScientificMateriality,
    SourceKind,
    SourceLocation,
)
from eegle.authoring.drafts import (
    DeploymentRequirement,
    DeploymentRequirementKind,
    DeploymentRequirements,
    ExperimentDraft,
    LoweredExperiment,
)
from eegle.authoring.provenance import (
    AuthoringProvenance,
    CanonicalTarget,
    DraftSourceMap,
    ProvenanceEntry,
    TemplateReference,
)
from eegle.authoring.templates import TemplateDefinition, TemplateProfile
from eegle.specs import ProtocolSpec, SuiteSpec
from eegle.specs.protocol import PROTOCOL_SPEC_SCHEMA
from eegle.specs.suite import SUITE_SPEC_SCHEMA


def build_template_profile(
    template: TemplateDefinition,
    draft: ExperimentDraft,
    parameters: Mapping[str, Any],
    *,
    explicit_parameters: frozenset[str],
    sources: DraftSourceMap,
) -> LoweredExperiment:
    builder = _PROFILE_BUILDERS[template.profile]
    protocol_payload, suite_payload = builder(draft.draft_id, thaw_json(parameters))
    protocol = ProtocolSpec.from_payload(protocol_payload)
    suite = SuiteSpec.from_payload(suite_payload)
    requirements = _deployment_requirements(suite)
    provenance = _template_provenance(
        template,
        draft,
        protocol,
        suite,
        explicit_parameters=explicit_parameters,
        sources=sources,
    )
    return LoweredExperiment(protocol, suite, requirements, provenance)


def _protocol(
    draft_id: str,
    statement: str,
    *,
    execution_mode: str = "causal",
) -> dict[str, Any]:
    return {
        "schema": PROTOCOL_SPEC_SCHEMA,
        "protocol_id": f"protocol.{draft_id}",
        "execution_mode": execution_mode,
        "claims": [
            {
                "claim_id": f"claim.{draft_id}",
                "statement": statement,
            }
        ],
        "metrics": [],
        "acceptance": [],
        "annotations": {},
    }


def _suite(
    draft_id: str,
    *,
    streams: list[dict[str, Any]],
    components: list[dict[str, Any]],
    routes: list[dict[str, Any]],
    phases: list[dict[str, Any]],
    initial_phase: str,
    model_uses: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    scheduling: Mapping[str, Any] | None = None,
    scheduled_triggers: list[dict[str, Any]] | None = None,
    state_triggers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": SUITE_SPEC_SCHEMA,
        "suite_id": f"suite.{draft_id}",
        "protocol_id": f"protocol.{draft_id}",
        "streams": streams,
        "components": components,
        "routes": routes,
        "phases": phases,
        "initial_phase": initial_phase,
        "artifacts": artifacts or [],
        "model_roles": [],
        "model_uses": model_uses or [],
        "outcome_expectations": [],
        "adaptations": [],
        "scheduling": dict(scheduling or {"backpressure": "fail_run"}),
        "scheduled_triggers": scheduled_triggers or [],
        "state_triggers": state_triggers or [],
        "clock_policy": {
            "execution_clock_id": "boundary.clock",
            "ordering": "availability_watermark",
        },
        "recording": {
            "execution_capture": True,
            "semantic_evidence": True,
            "raw_recording": "reference",
        },
        "validation": {"require_replay_equivalence": True},
    }


def _dense_stream(parameters: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "stream_id": "stream.neural",
        "modality": "eeg",
        "clock_id": "device.clock",
        "contract": {
            "type_id": "eegle.dense_sample_batch.v1",
            "unit": parameters["unit"],
            "channel_count": parameters["channel_count"],
            "nominal_rate_hz": parameters["sample_rate_hz"],
        },
    }


def _marker_stream(
    *,
    stream_id: str = "stream.markers",
    event_kind: str | None = None,
) -> dict[str, Any]:
    contract: dict[str, Any] = {"type_id": "eegle.sparse_event_batch.v1"}
    if event_kind is not None:
        contract["event_kinds"] = [event_kind]
    return {
        "stream_id": stream_id,
        "modality": "markers",
        "clock_id": "device.clock",
        "contract": contract,
    }


def _source(component_id: str, stream_id: str) -> dict[str, Any]:
    return {
        "component_id": component_id,
        "kind": "source",
        "plugin_id": None,
        "stream_id": stream_id,
        "config": {},
    }


def _route(
    route_id: str,
    source_component: str,
    source_port: str,
    target_component: str,
    target_port: str,
) -> dict[str, Any]:
    return {
        "route_id": route_id,
        "source": {"component": source_component, "port": source_port},
        "target": {"component": target_component, "port": target_port},
    }


def _plugin_component(
    component_id: str,
    kind: str,
    plugin_id: str,
    *,
    config: Mapping[str, Any] | None = None,
    version_spec: str = "~=0.1.0",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "component_id": component_id,
        "kind": kind,
        "plugin_id": plugin_id,
        "version_spec": version_spec,
        "config": dict(config or {}),
        **extra,
    }


def _model_component(
    component_id: str,
    parameters: Mapping[str, Any],
    *,
    threshold: float | None = None,
) -> dict[str, Any]:
    return _plugin_component(
        component_id,
        "model",
        str(parameters["model_plugin_id"]),
        version_spec=str(parameters["model_version_spec"]),
        config={
            "model_id": component_id,
            "threshold": parameters["threshold"] if threshold is None else threshold,
            "positive_label": parameters["positive_label"],
            "negative_label": parameters["negative_label"],
        },
    )


def _model_use(
    component_id: str,
    parameters: Mapping[str, Any],
    role_id: str,
    *,
    comparison_group: str | None = None,
) -> dict[str, Any]:
    return {
        "component_id": component_id,
        "manifest_digest": parameters["model_manifest_digest"],
        "role_id": role_id,
        "comparison_group": comparison_group,
    }


def _continuous_recording(
    draft_id: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    streams = [_dense_stream(parameters)]
    components = [
        _source("source.neural", "stream.neural"),
        _plugin_component(
            "sink.neural",
            "sink",
            "eegle.recording.dense_sink",
        ),
    ]
    routes = [
        _route(
            "route.neural_record",
            "source.neural",
            "samples",
            "sink.neural",
            "records",
        )
    ]
    phases = [
        {
            "phase_id": "phase.record",
            "components": ["source.neural", "sink.neural"],
            "transitions": [],
            "resume_policy": "checkpoint",
        }
    ]
    return (
        _protocol(draft_id, "Capture continuous EEG with exact timing evidence."),
        _suite(
            draft_id,
            streams=streams,
            components=components,
            routes=routes,
            phases=phases,
            initial_phase="phase.record",
        ),
    )


def _eeg_events_recording(
    draft_id: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    streams = [
        _dense_stream(parameters),
        _marker_stream(event_kind=str(parameters["event_kind"])),
    ]
    components = [
        _source("source.neural", "stream.neural"),
        _source("source.markers", "stream.markers"),
        _plugin_component(
            "sink.neural",
            "sink",
            "eegle.recording.dense_sink",
        ),
        _plugin_component(
            "sink.markers",
            "sink",
            "eegle.recording.sparse_sink",
        ),
    ]
    routes = [
        _route("route.neural_record", "source.neural", "samples", "sink.neural", "records"),
        _route("route.markers_record", "source.markers", "events", "sink.markers", "records"),
    ]
    phases = [
        {
            "phase_id": "phase.record",
            "components": [
                "source.neural",
                "source.markers",
                "sink.neural",
                "sink.markers",
            ],
            "transitions": [],
            "resume_policy": "checkpoint",
        }
    ]
    return (
        _protocol(draft_id, "Capture synchronized EEG and declared event markers."),
        _suite(
            draft_id,
            streams=streams,
            components=components,
            routes=routes,
            phases=phases,
            initial_phase="phase.record",
        ),
    )


def _continuous_observer(
    draft_id: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    components = [
        _source("source.neural", "stream.neural"),
        _plugin_component(
            "transform.identity",
            "transform",
            "eegle.processing.identity",
        ),
        _plugin_component(
            "window.continuous",
            "window",
            "eegle.processing.continuous_window",
            config={
                "window_samples": parameters["window_samples"],
                "step_samples": parameters["step_samples"],
            },
        ),
        _model_component("model.observer", parameters),
    ]
    routes = [
        _route("route.source_transform", "source.neural", "samples", "transform.identity", "samples"),
        _route("route.transform_window", "transform.identity", "samples", "window.continuous", "samples"),
        _route("route.window_model", "window.continuous", "windows", "model.observer", "window"),
    ]
    phases = [
        {
            "phase_id": "phase.observe",
            "components": [value["component_id"] for value in components],
            "transitions": [],
            "resume_policy": "checkpoint",
        }
    ]
    return (
        _protocol(draft_id, "Observe continuous windows with a label-blind model."),
        _suite(
            draft_id,
            streams=[_dense_stream(parameters)],
            components=components,
            routes=routes,
            phases=phases,
            initial_phase="phase.observe",
            model_uses=[_model_use("model.observer", parameters, "observer")],
        ),
    )


def _event_window_component(parameters: Mapping[str, Any]) -> dict[str, Any]:
    return _plugin_component(
        "window.event",
        "window",
        "eegle.processing.event_window",
        config={
            "start_offset_seconds": -0.02,
            "end_offset_seconds": 0.04,
            "event_kinds": [parameters["event_kind"]],
            "max_buffer_samples": 1000,
        },
    )


def _event_locked_model(
    draft_id: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    components = [
        _source("source.neural", "stream.neural"),
        _source("source.markers", "stream.markers"),
        _event_window_component(parameters),
        _model_component("model.observer", parameters),
    ]
    routes = [
        _route("route.neural_window", "source.neural", "samples", "window.event", "samples"),
        _route("route.markers_window", "source.markers", "events", "window.event", "events"),
        _route("route.window_model", "window.event", "windows", "model.observer", "window"),
    ]
    phases = [
        {
            "phase_id": "phase.observe",
            "components": [value["component_id"] for value in components],
            "transitions": [],
            "resume_policy": "checkpoint",
        }
    ]
    return (
        _protocol(draft_id, "Observe declared event-locked EEG windows with one model."),
        _suite(
            draft_id,
            streams=[
                _dense_stream(parameters),
                _marker_stream(event_kind=str(parameters["event_kind"])),
            ],
            components=components,
            routes=routes,
            phases=phases,
            initial_phase="phase.observe",
            model_uses=[_model_use("model.observer", parameters, "observer")],
        ),
    )


def _primary_shadow_comparison(
    draft_id: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    components = [
        _source("source.neural", "stream.neural"),
        _source("source.markers", "stream.markers"),
        _event_window_component(parameters),
        _model_component("model.primary", parameters),
        _model_component(
            "model.shadow",
            parameters,
            threshold=float(parameters["shadow_threshold"]),
        ),
    ]
    routes = [
        _route("route.neural_window", "source.neural", "samples", "window.event", "samples"),
        _route("route.markers_window", "source.markers", "events", "window.event", "events"),
        _route("route.window_primary", "window.event", "windows", "model.primary", "window"),
        _route("route.window_shadow", "window.event", "windows", "model.shadow", "window"),
    ]
    phases = [
        {
            "phase_id": "phase.compare",
            "components": [value["component_id"] for value in components],
            "transitions": [],
            "resume_policy": "checkpoint",
        }
    ]
    group = "comparison.template"
    return (
        _protocol(draft_id, "Compare primary and shadow models on identical admitted windows."),
        _suite(
            draft_id,
            streams=[
                _dense_stream(parameters),
                _marker_stream(event_kind=str(parameters["event_kind"])),
            ],
            components=components,
            routes=routes,
            phases=phases,
            initial_phase="phase.compare",
            model_uses=[
                _model_use("model.primary", parameters, "primary", comparison_group=group),
                _model_use("model.shadow", parameters, "shadow", comparison_group=group),
            ],
        ),
    )


def _calibration_validation(
    draft_id: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    components = [
        _source("source.neural", "stream.neural"),
        _source("source.markers", "stream.markers"),
        _plugin_component("transform.identity", "transform", "eegle.processing.identity"),
        _plugin_component(
            "window.calibration",
            "window",
            "eegle.processing.continuous_window",
            config={
                "window_samples": parameters["window_samples"],
                "step_samples": parameters["window_samples"],
            },
        ),
        _model_component("model.candidate", parameters),
        _plugin_component(
            "artifact.calibration",
            "artifact",
            "eegle.artifacts.prediction_summary",
            config={
                "artifact_id": "calibration.result",
                "role": "calibration_result",
            },
        ),
        _plugin_component("sink.neural", "sink", "eegle.recording.dense_sink"),
        _plugin_component("sink.markers", "sink", "eegle.recording.sparse_sink"),
    ]
    routes = [
        _route("route.neural_transform", "source.neural", "samples", "transform.identity", "samples"),
        _route("route.neural_sink", "source.neural", "samples", "sink.neural", "records"),
        _route("route.transform_window", "transform.identity", "samples", "window.calibration", "samples"),
        _route("route.window_model", "window.calibration", "windows", "model.candidate", "window"),
        _route("route.model_artifact", "model.candidate", "prediction", "artifact.calibration", "prediction"),
        _route("route.markers_sink", "source.markers", "events", "sink.markers", "records"),
    ]
    phases = [
        {
            "phase_id": "phase.calibrate",
            "components": [value["component_id"] for value in components],
            "transitions": [
                {"target_phase": "phase.validate", "condition": "complete"}
            ],
            "resume_policy": "checkpoint",
        },
        {
            "phase_id": "phase.validate",
            "components": [
                "source.neural",
                "source.markers",
                "transform.identity",
                "window.calibration",
                "model.candidate",
                "sink.neural",
                "sink.markers",
            ],
            "transitions": [],
            "required_artifacts": ["calibration.result"],
            "resume_policy": "checkpoint",
        },
    ]
    artifacts = [
        {
            "artifact_id": "calibration.result",
            "role": "calibration_result",
            "media_type": "application/json",
            "producer_phase": "phase.calibrate",
            "producer_component": "artifact.calibration",
            "producer_port": "artifact",
            "expected_digest": None,
        }
    ]
    return (
        _protocol(draft_id, "Calibrate once, then validate with the declared frozen artifact."),
        _suite(
            draft_id,
            streams=[_dense_stream(parameters), _marker_stream()],
            components=components,
            routes=routes,
            phases=phases,
            initial_phase="phase.calibrate",
            model_uses=[_model_use("model.candidate", parameters, "observer")],
            artifacts=artifacts,
        ),
    )


def _delayed_outcome_adaptation(
    draft_id: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    components = [
        _source("source.outcomes", "stream.outcomes"),
        _plugin_component(
            "outcome.delayed",
            "outcome",
            "eegle.outcomes.sparse_event",
            config={
                "event_kind": parameters["event_kind"],
                "permitted_uses": ["metrics", "adaptation"],
            },
            outcome_uses=["metrics", "adaptation"],
        ),
        _plugin_component(
            "adapter.counter",
            "adapter",
            "eegle.adapters.adaptive_counter",
            config={"required_use": "adaptation"},
            required_outcome_use="adaptation",
        ),
        _plugin_component(
            "sink.triggers",
            "sink",
            "eegle.runtime.trigger_record_sink",
        ),
    ]
    routes = [
        _route("route.events_outcomes", "source.outcomes", "events", "outcome.delayed", "events"),
        _route("route.outcomes_adapter", "outcome.delayed", "outcomes", "adapter.counter", "outcome"),
    ]
    phases = [
        {
            "phase_id": "phase.adapt",
            "components": [value["component_id"] for value in components],
            "transitions": [],
            "resume_policy": "checkpoint",
        }
    ]
    return (
        _protocol(draft_id, "Apply only delayed outcomes explicitly permissioned for adaptation."),
        _suite(
            draft_id,
            streams=[
                _marker_stream(
                    stream_id="stream.outcomes",
                    event_kind=str(parameters["event_kind"]),
                )
            ],
            components=components,
            routes=routes,
            phases=phases,
            initial_phase="phase.adapt",
            scheduling={"backpressure": "reject_newest"},
            scheduled_triggers=[
                {
                    "trigger_id": "trigger.phase_start",
                    "phase_id": "phase.adapt",
                    "target_component": "sink.triggers",
                    "scheduled_offset_seconds": 0.0,
                    "deadline_offset_seconds": None,
                    "payload": {"kind": "phase_start"},
                }
            ],
            state_triggers=[
                {
                    "rule_id": "rule.after_adaptation",
                    "phase_id": "phase.adapt",
                    "source_component": "adapter.counter",
                    "target_component": "sink.triggers",
                    "transition_kind": "adaptation",
                    "statuses": ["applied"],
                    "delay_seconds": 0.0,
                    "payload": {"kind": "adaptation_applied"},
                    "once": False,
                }
            ],
        ),
    )


def _simulated_closed_loop(
    draft_id: str,
    parameters: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    components = [
        _source("source.neural", "stream.neural"),
        _source("source.markers", "stream.markers"),
        _event_window_component(parameters),
        _model_component("model.primary", parameters),
        _plugin_component(
            "policy.action",
            "policy",
            "eegle.actions.label_action",
            config={
                "capability": parameters["action_capability"],
                "matching_label": parameters["positive_label"],
                "parameters": {"intensity": parameters["action_intensity"]},
            },
        ),
        _plugin_component(
            "actuator.simulated",
            "actuator",
            "eegle.actions.simulated_actuator",
            action_capabilities=[parameters["action_capability"]],
        ),
    ]
    routes = [
        _route("route.neural_window", "source.neural", "samples", "window.event", "samples"),
        _route("route.markers_window", "source.markers", "events", "window.event", "events"),
        _route("route.window_model", "window.event", "windows", "model.primary", "window"),
        _route("route.model_policy", "model.primary", "prediction", "policy.action", "prediction"),
        _route("route.policy_actuator", "policy.action", "request", "actuator.simulated", "request"),
    ]
    phases = [
        {
            "phase_id": "phase.run",
            "components": [value["component_id"] for value in components],
            "transitions": [],
            "resume_policy": "checkpoint",
        }
    ]
    return (
        _protocol(draft_id, "Request only simulation-authorized actions from model output."),
        _suite(
            draft_id,
            streams=[
                _dense_stream(parameters),
                _marker_stream(event_kind=str(parameters["event_kind"])),
            ],
            components=components,
            routes=routes,
            phases=phases,
            initial_phase="phase.run",
            model_uses=[_model_use("model.primary", parameters, "primary")],
        ),
    )


_PROFILE_BUILDERS = {
    TemplateProfile.CONTINUOUS_RECORDING: _continuous_recording,
    TemplateProfile.EEG_EVENTS_RECORDING: _eeg_events_recording,
    TemplateProfile.CONTINUOUS_OBSERVER: _continuous_observer,
    TemplateProfile.EVENT_LOCKED_MODEL: _event_locked_model,
    TemplateProfile.PRIMARY_SHADOW_COMPARISON: _primary_shadow_comparison,
    TemplateProfile.CALIBRATION_VALIDATION: _calibration_validation,
    TemplateProfile.DELAYED_OUTCOME_ADAPTATION: _delayed_outcome_adaptation,
    TemplateProfile.SIMULATED_CLOSED_LOOP: _simulated_closed_loop,
}


def _deployment_requirements(suite: SuiteSpec) -> DeploymentRequirements:
    streams = {value.stream_id: value for value in suite.streams}
    execution_clock = str(suite.clock_policy["execution_clock_id"])
    requirements: list[DeploymentRequirement] = []
    for component in suite.components:
        if component.kind != ComponentKind.SOURCE or component.stream_id is None:
            continue
        stream = streams[component.stream_id]
        suffix = component.component_id.split(".", 1)[-1]
        requirements.append(
            DeploymentRequirement(
                requirement_id=f"requirement.source.{suffix}",
                kind=DeploymentRequirementKind.SOURCE_BINDING,
                component_id=component.component_id,
                stream_id=stream.stream_id,
                contract=stream.contract.to_payload(),
            )
        )
        if stream.clock_id is not None and stream.clock_id != execution_clock:
            requirements.append(
                DeploymentRequirement(
                    requirement_id=f"requirement.clock.{suffix}",
                    kind=DeploymentRequirementKind.CLOCK_MAPPING,
                    source_clock=stream.clock_id,
                    target_clock=execution_clock,
                )
            )
    requirements.append(
        DeploymentRequirement(
            requirement_id="requirement.storage.evidence",
            kind=DeploymentRequirementKind.STORAGE,
            storage_kind="evidence",
        )
    )
    for model in suite.model_uses:
        suffix = model.component_id.split(".", 1)[-1]
        requirements.append(
            DeploymentRequirement(
                requirement_id=f"requirement.model.{suffix}",
                kind=DeploymentRequirementKind.MODEL_ARTIFACT,
                component_id=model.component_id,
                manifest_digest=model.manifest_digest,
            )
        )
    for component in suite.components:
        if component.kind != ComponentKind.ACTUATOR:
            continue
        for capability in component.action_capabilities:
            suffix = component.component_id.split(".", 1)[-1]
            requirements.append(
                DeploymentRequirement(
                    requirement_id=f"requirement.authorization.{suffix}",
                    kind=DeploymentRequirementKind.AUTHORIZATION,
                    component_id=component.component_id,
                    action_capability=capability,
                )
            )
    return DeploymentRequirements(suite.suite_id, tuple(requirements))


def _template_provenance(
    template: TemplateDefinition,
    draft: ExperimentDraft,
    protocol: ProtocolSpec,
    suite: SuiteSpec,
    *,
    explicit_parameters: frozenset[str],
    sources: DraftSourceMap,
) -> AuthoringProvenance:
    target_parameters: dict[tuple[CanonicalArtifact, str], str] = {}
    for parameter, targets in template.parameter_targets.items():
        for target in targets:
            target_parameters[(target.artifact, target.path)] = parameter
    leaves = {
        CanonicalArtifact.PROTOCOL: tuple(_leaf_paths(protocol.to_payload())),
        CanonicalArtifact.SUITE: tuple(_leaf_paths(suite.to_payload())),
    }
    leaf_targets = {
        (artifact, path)
        for artifact, paths in leaves.items()
        for path in paths
    }
    missing_targets = sorted(
        f"{artifact.value}:{path}"
        for artifact, path in set(target_parameters) - leaf_targets
    )
    if missing_targets:
        raise ValueError(
            "template parameter targets do not identify generated leaves: "
            + ", ".join(missing_targets)
        )
    entries: list[ProvenanceEntry] = []
    template_locator = f"{template.template_id}@{template.version}"
    for artifact, paths in leaves.items():
        for path in paths:
            parameter = target_parameters.get((artifact, path))
            if parameter in explicit_parameters:
                entries.append(
                    ProvenanceEntry(
                        target_artifact=artifact,
                        target_path=path,
                        origin=AuthoringOrigin.USER_EXPLICIT,
                        source=sources.source_for(f"/template/parameters/{parameter}"),
                        materiality=_materiality(artifact, path),
                    )
                )
            else:
                entries.append(
                    ProvenanceEntry(
                        target_artifact=artifact,
                        target_path=path,
                        origin=AuthoringOrigin.TEMPLATE_DEFAULT,
                        source=SourceLocation(
                            SourceKind.TEMPLATE,
                            locator=template_locator,
                            symbol=path,
                        ),
                        materiality=_materiality(artifact, path),
                        confirmation=ConfirmationState.NOT_REQUIRED,
                        template=TemplateReference(
                            template.template_id,
                            template.version,
                            None
                            if parameter is None
                            else f"/parameters/{parameter}",
                        ),
                    )
                )
    return AuthoringProvenance(
        draft_id=draft.draft_id,
        draft_revision=draft.revision,
        draft_digest=draft.draft_digest,
        canonical_targets={
            CanonicalArtifact.PROTOCOL: CanonicalTarget(
                PROTOCOL_SPEC_SCHEMA,
                protocol.spec_hash,
            ),
            CanonicalArtifact.SUITE: CanonicalTarget(
                SUITE_SPEC_SCHEMA,
                suite.spec_hash,
            ),
        },
        entries=tuple(entries),
    )


def _leaf_paths(value: Any, path: str = "") -> Iterable[str]:
    if isinstance(value, Mapping):
        if not value:
            yield path
        for key in sorted(value):
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from _leaf_paths(value[key], f"{path}/{escaped}")
    elif isinstance(value, (list, tuple)):
        if not value:
            yield path
        for index, item in enumerate(value):
            yield from _leaf_paths(item, f"{path}/{index}")
    else:
        yield path


def _materiality(
    artifact: CanonicalArtifact,
    path: str,
) -> ScientificMateriality:
    if artifact == CanonicalArtifact.PROTOCOL:
        if path in {"/schema", "/protocol_id"}:
            return ScientificMateriality.OPERATIONAL
        return ScientificMateriality.SCIENTIFIC
    scientific_prefixes = (
        "/streams",
        "/components",
        "/routes",
        "/model_roles",
        "/model_uses",
        "/outcome_expectations",
        "/adaptations",
        "/clock_policy",
        "/validation",
    )
    if path.startswith(scientific_prefixes):
        return ScientificMateriality.SCIENTIFIC
    return ScientificMateriality.OPERATIONAL
