"""Deterministic lowering from named designs to canonical EEGle specifications."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._domain import ComponentKind
from eegle._validation import thaw_json
from eegle.authoring.composition import ExperimentDesign
from eegle.authoring.contracts import (
    AuthoringOrigin,
    CanonicalArtifact,
    ConfirmationState,
    ScientificMateriality,
)
from eegle.authoring.design import WindowKind
from eegle.authoring.design_provenance import (
    _escape_pointer,
    _generated_source,
    _leaf_paths,
    _materiality,
    _semantic_draft_path,
)
from eegle.authoring.drafts import (
    DeploymentRequirement,
    DeploymentRequirementKind,
    DeploymentRequirements,
    DraftIssue,
    DraftLoweringError,
    LoweredExperiment,
)
from eegle.authoring.provenance import AuthoringProvenance, CanonicalTarget, ProvenanceEntry
from eegle.specs import (
    AcceptanceCriterion,
    AdaptationSpec,
    ArtifactSpec,
    ClaimSpec,
    ComponentSpec,
    LogicalStreamSpec,
    MetricSpec,
    ModelUseSpec,
    OutcomeExpectationSpec,
    PhaseSpec,
    PhaseTransition,
    ProtocolSpec,
    ResumePolicy,
    RouteSpec,
    SchedulingSpec,
    SignalContract,
    SuiteSpec,
    TransitionCondition,
)
from eegle.specs.protocol import PROTOCOL_SPEC_SCHEMA
from eegle.specs.suite import SUITE_SPEC_SCHEMA


def lower_experiment_design(design: ExperimentDesign) -> LoweredExperiment:
    """Lower one complete named design through the canonical authority boundary."""

    if not isinstance(design, ExperimentDesign):
        raise TypeError("design lowering requires an ExperimentDesign")
    return _DesignLowerer(design).lower()


@dataclass(frozen=True, slots=True)
class _OutputBinding:
    component_id: str
    port: str
    kind: str
    contract: SignalContract | None = None

    @property
    def rate_hz(self) -> float | None:
        return None if self.contract is None else self.contract.nominal_rate_hz


class _DesignLowerer:
    def __init__(self, design: ExperimentDesign) -> None:
        self.design = design
        self.draft = design.to_draft()
        self.source_map = design.draft_source_map()
        self.components: dict[str, ComponentSpec] = {}
        self.routes: dict[str, RouteSpec] = {}
        self.outputs: dict[str, _OutputBinding] = {}
        self.semantic_components: dict[str, tuple[str, ...]] = {}
        self.recording_components: dict[str, tuple[str, tuple[str, ...]]] = {}
        self.bindings: dict[tuple[CanonicalArtifact, str], str] = {}

    def lower(self) -> LoweredExperiment:
        issues = self._reference_issues()
        if issues:
            raise DraftLoweringError(tuple(issues))
        protocol = self._protocol()
        streams = self._sources()
        self._processing()
        self._windows()
        self._quality()
        model_uses = self._models()
        outcome_expectations = self._outcomes()
        self._policies()
        self._actions()
        self._recording()
        phases = self._phases()
        artifacts = self._calibrations(phases)
        adaptations = self._adaptations()
        suite = SuiteSpec(
            suite_id=self.design.study.suite_id or f"suite.{self.design.experiment_id}",
            protocol_id=protocol.protocol_id,
            streams=tuple(sorted(streams, key=lambda value: value.stream_id)),
            components=tuple(sorted(self.components.values(), key=lambda value: value.component_id)),
            routes=tuple(sorted(self.routes.values(), key=lambda value: value.route_id)),
            phases=phases,
            initial_phase=self._initial_phase(phases),
            clock_policy={
                "execution_clock_id": self.design.study.execution_clock_id,
                "ordering": "availability_watermark",
            },
            recording={
                "execution_capture": self.design.recording.execution_capture,
                "semantic_evidence": self.design.recording.semantic_evidence,
                "raw_recording": self.design.recording.raw_recording,
            },
            validation={"require_replay_equivalence": True},
            artifacts=artifacts,
            model_uses=model_uses,
            outcome_expectations=outcome_expectations,
            adaptations=adaptations,
            scheduling=SchedulingSpec(),
        )
        requirements = _design_requirements(suite)
        provenance = self._provenance(protocol, suite)
        return LoweredExperiment(protocol, suite, requirements, provenance)

    def _reference_issues(self) -> list[DraftIssue]:
        signal_refs = {value.semantic_id for value in self.design.signals}
        event_refs = {value.semantic_id for value in self.design.events}
        processing_refs = {value.semantic_id for value in self.design.processing}
        window_refs = {value.semantic_id for value in self.design.windows}
        quality_refs = {value.semantic_id for value in self.design.quality_gates}
        model_refs = {value.semantic_id for value in self.design.models}
        outcome_refs = {value.semantic_id for value in self.design.outcomes}
        policy_refs = {value.semantic_id for value in self.design.policies}
        action_refs = {value.semantic_id for value in self.design.actions}
        valid = signal_refs | event_refs | processing_refs | window_refs | quality_refs | model_refs | outcome_refs | policy_refs | action_refs
        phases = {value.semantic_id for value in self.design.phases}
        issues: list[DraftIssue] = []

        def require(ref: str, path: str, *, allowed: set[str] = valid, port: bool = True) -> None:
            if _reference_base(ref, allowed, allow_port=port) is None:
                issues.append(self._issue("authoring.missing_reference", path, f"unknown semantic reference {ref}"))

        for index, chain in enumerate(self.design.processing):
            require(
                chain.input,
                f"/intent/processing/{index}/input",
                allowed=signal_refs | processing_refs,
            )
        for index, window in enumerate(self.design.windows):
            require(
                window.input,
                f"/intent/windows/{index}/input",
                allowed=signal_refs | processing_refs,
            )
            if window.event_stream is not None:
                require(window.event_stream, f"/intent/windows/{index}/event_stream", allowed=event_refs, port=False)
        for index, gate in enumerate(self.design.quality_gates):
            require(
                gate.input,
                f"/intent/quality_gates/{index}/input",
                allowed=signal_refs | processing_refs | window_refs,
            )
        for index, model in enumerate(self.design.models):
            for port, ref in model.inputs.items():
                require(
                    str(ref),
                    f"/intent/models/{index}/inputs/{_escape_pointer(str(port))}",
                    allowed=signal_refs
                    | event_refs
                    | processing_refs
                    | window_refs
                    | quality_refs,
                )
        for index, comparison in enumerate(self.design.comparisons):
            for member in comparison.members:
                require(member, f"/intent/comparisons/{index}/members", allowed=model_refs, port=False)
        for index, outcome in enumerate(self.design.outcomes):
            require(outcome.event_stream, f"/intent/outcomes/{index}/event_stream", allowed=event_refs, port=False)
            for model in outcome.models:
                require(model, f"/intent/outcomes/{index}/models", allowed=model_refs, port=False)
        for index, adaptation in enumerate(self.design.adaptations):
            require(adaptation.model, f"/intent/adaptations/{index}/model", allowed=model_refs, port=False)
            require(adaptation.outcome, f"/intent/adaptations/{index}/outcome", allowed=outcome_refs, port=False)
            for phase in adaptation.enabled_phases:
                require(phase, f"/intent/adaptations/{index}/enabled_phases", allowed=phases, port=False)
        for index, policy in enumerate(self.design.policies):
            for port, ref in policy.inputs.items():
                require(
                    str(ref),
                    f"/intent/policies/{index}/inputs/{_escape_pointer(str(port))}",
                    allowed=model_refs | quality_refs | outcome_refs,
                )
        for index, action in enumerate(self.design.actions):
            require(action.policy, f"/intent/actions/{index}/policy", allowed=policy_refs, port=False)
        for index, phase in enumerate(self.design.phases):
            for field, refs in (
                ("active", phase.active),
                ("goals", phase.goals),
                ("exclude", phase.exclude),
            ):
                for ref in refs:
                    require(ref, f"/intent/phases/{index}/{field}")
            for target in phase.next_phases:
                require(target, f"/intent/phases/{index}/next_phases", allowed=phases, port=False)
        if self.design.initial_phase is not None:
            require(
                self.design.initial_phase,
                "/intent/initial_phase",
                allowed=phases,
                port=False,
            )
        elif len(self.design.phases) > 1:
            issues.append(
                self._issue(
                    "authoring.initial_phase",
                    "/intent/initial_phase",
                    "multi-phase designs require an explicit initial_phase",
                )
            )
        for index, calibration in enumerate(self.design.calibrations):
            require(calibration.producer, f"/intent/calibrations/{index}/producer")
            require(calibration.producer_phase, f"/intent/calibrations/{index}/producer_phase", allowed=phases, port=False)
            for phase in calibration.required_phases:
                require(phase, f"/intent/calibrations/{index}/required_phases", allowed=phases, port=False)
        for index, ref in enumerate(self.design.recording.inputs):
            require(
                ref,
                f"/intent/recording/inputs/{index}",
                allowed=signal_refs | event_refs | processing_refs,
            )
        for index, phase in enumerate(self.design.recording.phases):
            require(
                phase,
                f"/intent/recording/phases/{index}",
                allowed=phases,
                port=False,
            )
        if self.design.recording.phases and not self.design.phases:
            issues.append(
                self._issue(
                    "authoring.recording_phase",
                    "/intent/recording/phases",
                    "phase-scoped recording requires explicit phases",
                )
            )
        membership: dict[str, str] = {}
        for group in self.design.comparisons:
            for member in group.members:
                if member in membership:
                    issues.append(self._issue("authoring.comparison_membership", "/intent/comparisons", f"{member} belongs to both {membership[member]} and {group.semantic_id}"))
                membership[member] = group.semantic_id
        for model in self.design.models:
            if model.role in {"shadow", "candidate"} and model.semantic_id not in membership:
                issues.append(self._issue("authoring.comparison_required", "/intent/models", f"{model.semantic_id} role {model.role} requires a comparison group"))
        return issues

    def _protocol(self) -> ProtocolSpec:
        metrics = tuple(MetricSpec(value.metric_id, value.measure, value.parameters) for value in self.design.acceptance)
        criteria = tuple(AcceptanceCriterion(value.criterion_id, value.metric_id, value.operator, value.value) for value in self.design.acceptance)
        protocol = ProtocolSpec(
            protocol_id=self.design.study.protocol_id or f"protocol.{self.design.experiment_id}",
            execution_mode=self.design.study.execution_mode,
            claims=(ClaimSpec(f"claim.{self.design.experiment_id}", self.design.study.statement),),
            metrics=metrics,
            acceptance=criteria,
            annotations=self.design.study.annotations,
        )
        if self.design.study.protocol_id is not None:
            self._bind(CanonicalArtifact.PROTOCOL, "/protocol_id", "/intent/study/protocol_id")
        self._bind(CanonicalArtifact.PROTOCOL, "/execution_mode", "/intent/study/execution_mode")
        self._bind(CanonicalArtifact.PROTOCOL, "/claims/0/statement", "/intent/study/statement")
        for relative in _leaf_paths(self.design.study.annotations):
            self._bind(
                CanonicalArtifact.PROTOCOL,
                "/annotations" + relative,
                "/intent/study/annotations" + relative,
            )
        for index, acceptance in enumerate(self.design.acceptance):
            draft = f"/intent/acceptance/{index}"
            for target, source in (
                (f"/metrics/{index}/metric_id", "/metric_id"),
                (f"/metrics/{index}/measure", "/measure"),
                (f"/acceptance/{index}/criterion_id", "/criterion_id"),
                (f"/acceptance/{index}/metric_id", "/metric_id"),
                (f"/acceptance/{index}/operator", "/operator"),
                (f"/acceptance/{index}/value", "/value"),
            ):
                self._bind(CanonicalArtifact.PROTOCOL, target, draft + source)
            for relative in _leaf_paths(acceptance.parameters):
                self._bind(
                    CanonicalArtifact.PROTOCOL,
                    f"/metrics/{index}/parameters" + relative,
                    draft + "/parameters" + relative,
                )
        return protocol

    def _sources(self) -> list[LogicalStreamSpec]:
        streams: list[LogicalStreamSpec] = []
        for signal in self.design.signals:
            stream_id = f"stream.{signal.signal_id}"
            component_id = f"source.{signal.signal_id}"
            units = {channel: signal.channel_units.get(channel, signal.unit) for channel in signal.channels}
            contract = SignalContract(
                type_id="eegle.dense_sample_batch.v1",
                unit=signal.unit,
                channel_count=len(signal.channels),
                nominal_rate_hz=signal.nominal_rate_hz,
                content_kind="dense_samples",
                rate_model=signal.rate_model,
                channel_ids=signal.channels,
                units=units,
                missing_data_policy=signal.missing_data_policy,
                layout=signal.layout,
            )
            streams.append(LogicalStreamSpec(stream_id, contract, signal.modality, signal.clock_id))
            self._component(ComponentSpec(component_id, ComponentKind.SOURCE, stream_id=stream_id))
            self.outputs[signal.semantic_id] = _OutputBinding(
                component_id,
                "samples",
                "dense",
                contract,
            )
            self.semantic_components[signal.semantic_id] = (component_id,)
        for event in self.design.events:
            stream_id = f"stream.{event.event_id}"
            component_id = f"source.{event.event_id}"
            contract = SignalContract(
                type_id="eegle.sparse_event_batch.v1",
                content_kind="sparse_events",
                rate_model="irregular",
                event_kinds=event.event_kinds,
            )
            streams.append(LogicalStreamSpec(stream_id, contract, event.modality, event.clock_id))
            self._component(ComponentSpec(component_id, ComponentKind.SOURCE, stream_id=stream_id))
            self.outputs[event.semantic_id] = _OutputBinding(
                component_id,
                "events",
                "sparse",
                contract,
            )
            self.semantic_components[event.semantic_id] = (component_id,)
        return streams

    def _processing(self) -> None:
        pending = list(self.design.processing)
        while pending:
            progress = False
            for chain in tuple(pending):
                if self._resolve(chain.input) is None:
                    continue
                previous = self._resolve_required(chain.input)
                chain_components: list[str] = []
                for step in chain.steps:
                    if previous.contract is None:
                        raise DraftLoweringError(
                            (
                                self._issue(
                                    "authoring.processing_input_contract",
                                    "/intent/processing",
                                    f"{chain.semantic_id}.{step.step_id} needs an explicit input contract",
                                ),
                            )
                        )
                    output_contract = step.output_contract.apply(previous.contract)
                    component_id = f"processing.{chain.chain_id}.{step.step_id}"
                    self._component(
                        ComponentSpec(
                            component_id,
                            ComponentKind.TRANSFORM,
                            step.plugin_id,
                            step.version_spec,
                            step.config,
                            input_contracts={step.input_port: previous.contract},
                            output_contracts={step.output_port: output_contract},
                        )
                    )
                    self._route(previous, component_id, step.input_port)
                    previous = _OutputBinding(
                        component_id,
                        step.output_port,
                        _record_kind(output_contract),
                        output_contract,
                    )
                    chain_components.append(component_id)
                self.outputs[chain.semantic_id] = previous
                self.semantic_components[chain.semantic_id] = tuple(chain_components)
                pending.remove(chain)
                progress = True
            if not progress:
                refs = ", ".join(value.semantic_id for value in pending)
                raise DraftLoweringError((self._issue("authoring.processing_cycle", "/intent/processing", f"processing chains contain a cycle: {refs}"),))

    def _windows(self) -> None:
        for window in self.design.windows:
            source = self._resolve_required(window.input)
            component_id = f"window.{window.window_id}"
            if window.kind == WindowKind.CONTINUOUS:
                if source.rate_hz is None:
                    raise DraftLoweringError((self._issue("authoring.window_rate", "/intent/windows", f"{window.semantic_id} needs an explicit input rate"),))
                window_samples = _seconds_to_samples(window.duration_seconds, source.rate_hz, "duration_seconds")
                step_samples = _seconds_to_samples(window.step_seconds, source.rate_hz, "step_seconds")
                component = ComponentSpec(
                    component_id,
                    ComponentKind.WINDOW,
                    "eegle.processing.continuous_window",
                    "~=0.1.0",
                    {"window_samples": window_samples, "step_samples": step_samples},
                    input_contracts={"samples": source.contract}
                    if source.contract is not None
                    else {},
                )
                self._component(component)
                self._route(source, component_id, "samples")
            else:
                events = self._resolve_required(str(window.event_stream))
                component = ComponentSpec(
                    component_id,
                    ComponentKind.WINDOW,
                    "eegle.processing.event_window",
                    "~=0.1.0",
                    {
                        "start_offset_seconds": window.start_offset_seconds,
                        "end_offset_seconds": window.end_offset_seconds,
                        "event_kinds": [window.event_kind],
                        "max_buffer_samples": window.max_buffer_samples,
                    },
                )
                self._component(component)
                self._route(source, component_id, "samples")
                self._route(events, component_id, "events")
            window_contract = SignalContract(
                type_id="eegle.dense_window.v1",
                unit=None if source.contract is None else source.contract.unit,
                channel_count=None
                if source.contract is None
                else source.contract.channel_count,
                nominal_rate_hz=source.rate_hz,
                window_samples=(window_samples if window.kind == WindowKind.CONTINUOUS else None),
                content_kind="dense_window",
                rate_model=None if source.contract is None else source.contract.rate_model,
                channel_ids=()
                if source.contract is None
                else source.contract.channel_ids,
                units={}
                if source.contract is None
                else source.contract.units,
                missing_data_policy=None
                if source.contract is None
                else source.contract.missing_data_policy,
                layout=None if source.contract is None else source.contract.layout,
                window_duration_seconds=(
                    window.duration_seconds
                    if window.kind == WindowKind.CONTINUOUS
                    else float(window.end_offset_seconds) - float(window.start_offset_seconds)
                ),
            )
            self.outputs[window.semantic_id] = _OutputBinding(
                component_id,
                "windows",
                "window",
                window_contract,
            )
            self.semantic_components[window.semantic_id] = (component_id,)

    def _quality(self) -> None:
        for gate in self.design.quality_gates:
            source = self._resolve_required(gate.input)
            component_id = f"quality.{gate.gate_id}"
            config = {"gate_id": gate.gate_id, **thaw_json(gate.config)}
            self._component(ComponentSpec(component_id, ComponentKind.QUALITY, gate.plugin_id, gate.version_spec, config))
            self._route(source, component_id, gate.input_port)
            self.outputs[gate.semantic_id] = _OutputBinding(component_id, gate.output_port, "quality")
            self.semantic_components[gate.semantic_id] = (component_id,)

    def _models(self) -> tuple[ModelUseSpec, ...]:
        groups = {member: group.comparison_id for group in self.design.comparisons for member in group.members}
        uses: list[ModelUseSpec] = []
        for model in self.design.models:
            component_id = f"model.{model.model_id}"
            self._component(ComponentSpec(component_id, ComponentKind.MODEL, model.plugin_id, model.version_spec, model.config))
            for port, ref in sorted(model.inputs.items()):
                self._route(self._resolve_required(str(ref)), component_id, str(port))
            self.outputs[model.semantic_id] = _OutputBinding(component_id, "prediction", "prediction")
            self.semantic_components[model.semantic_id] = (component_id,)
            uses.append(ModelUseSpec(component_id, model.manifest_digest, model.role, groups.get(model.semantic_id)))
        return tuple(sorted(uses, key=lambda value: value.component_id))

    def _outcomes(self) -> tuple[OutcomeExpectationSpec, ...]:
        expectations: list[OutcomeExpectationSpec] = []
        for outcome in self.design.outcomes:
            component_id = f"outcome.{outcome.outcome_id}"
            self._component(
                ComponentSpec(
                    component_id,
                    ComponentKind.OUTCOME,
                    outcome.plugin_id,
                    outcome.version_spec,
                    {"event_kind": outcome.event_kind, "permitted_uses": list(outcome.permitted_uses)},
                    outcome_uses=outcome.permitted_uses,
                )
            )
            self._route(self._resolve_required(outcome.event_stream), component_id, "events")
            self.outputs[outcome.semantic_id] = _OutputBinding(component_id, "outcomes", "outcome")
            self.semantic_components[outcome.semantic_id] = (component_id,)
            for model_ref in outcome.models:
                model_component = self._resolve_required(model_ref).component_id
                expectations.append(
                    OutcomeExpectationSpec(
                        f"expectation.{outcome.outcome_id}.{model_component.removeprefix('model.')}",
                        model_component,
                        (component_id,),
                        outcome.permitted_uses,
                        outcome.max_pending_predictions,
                        outcome.prediction_ttl_seconds,
                    )
                )
        return tuple(sorted(expectations, key=lambda value: value.expectation_id))

    def _policies(self) -> None:
        for policy in self.design.policies:
            component_id = f"policy.{policy.policy_id}"
            self._component(ComponentSpec(component_id, ComponentKind.POLICY, policy.plugin_id, policy.version_spec, policy.config))
            for port, ref in sorted(policy.inputs.items()):
                self._route(self._resolve_required(str(ref)), component_id, str(port))
            self.outputs[policy.semantic_id] = _OutputBinding(component_id, "request", "action_request")
            self.semantic_components[policy.semantic_id] = (component_id,)

    def _actions(self) -> None:
        for action in self.design.actions:
            component_id = f"actuator.{action.action_id}"
            self._component(
                ComponentSpec(
                    component_id,
                    ComponentKind.ACTUATOR,
                    action.plugin_id,
                    action.version_spec,
                    action.config,
                    action_capabilities=(action.capability,),
                )
            )
            policy = self._resolve_required(action.policy)
            self._route(_OutputBinding(policy.component_id, action.policy_output_port, policy.kind), component_id, action.actuator_input_port)
            self.outputs[action.semantic_id] = _OutputBinding(component_id, "receipt", "receipt")
            self.semantic_components[action.semantic_id] = (component_id,)

    def _recording(self) -> None:
        components: list[str] = []
        for ref in self.design.recording.inputs:
            source = self._resolve_required(ref)
            if source.kind not in {"dense", "sparse"}:
                raise DraftLoweringError((self._issue("authoring.recording_contract", "/intent/recording/inputs", f"{ref} is not a recordable dense or sparse stream"),))
            suffix = ref.replace(":", ".").replace("/", ".")
            component_id = f"sink.{suffix}"
            plugin_id = "eegle.recording.sparse_sink" if source.kind == "sparse" else "eegle.recording.dense_sink"
            self._component(ComponentSpec(component_id, ComponentKind.SINK, plugin_id, "~=0.1.0"))
            self._route(source, component_id, "records")
            components.append(component_id)
            self.recording_components[component_id] = (
                source.component_id,
                self.design.recording.phases,
            )
        self.semantic_components["recording"] = tuple(components)

    def _phases(self) -> tuple[PhaseSpec, ...]:
        required: dict[str, list[str]] = {}
        for calibration in self.design.calibrations:
            for phase in calibration.required_phases:
                required.setdefault(phase, []).append(f"calibration.{calibration.calibration_id}")
        if not self.design.phases:
            return (
                PhaseSpec(
                    "phase.run",
                    tuple(sorted(self.components)),
                    resume_policy=ResumePolicy.CHECKPOINT,
                ),
            )
        phases: list[PhaseSpec] = []
        for phase in self.design.phases:
            component_ids: set[str] = set()
            references = phase.goals or phase.active
            for ref in references:
                base = _reference_base(ref, set(self.semantic_components), allow_port=True)
                if base is None:
                    continue
                component_ids.update(self.semantic_components[base])
            if phase.goals and phase.include_dependencies:
                component_ids = self._upstream_closure(component_ids)
            excluded: set[str] = set()
            for ref in phase.exclude:
                base = _reference_base(ref, set(self.semantic_components), allow_port=True)
                if base is not None:
                    excluded.update(self.semantic_components[base])
            component_ids.difference_update(excluded)
            for sink, (source_component, recording_phases) in self.recording_components.items():
                if source_component not in component_ids:
                    continue
                if recording_phases and phase.semantic_id not in recording_phases:
                    continue
                component_ids.add(sink)
            transitions = tuple(
                PhaseTransition(target, TransitionCondition.COMPLETE)
                for target in phase.next_phases
            )
            phases.append(
                PhaseSpec(
                    phase.semantic_id,
                    tuple(sorted(component_ids)),
                    transitions=transitions,
                    required_artifacts=tuple(sorted(required.get(phase.semantic_id, ()))),
                    retry_limit=phase.retry_limit,
                    resume_policy=phase.resume_policy,
                    operator_confirmation=phase.operator_confirmation,
                    timeout_seconds=phase.timeout_seconds,
                )
            )
        return tuple(phases)

    def _upstream_closure(self, component_ids: set[str]) -> set[str]:
        closure = set(component_ids)
        changed = True
        routes = tuple(self.routes.values())
        while changed:
            changed = False
            for route in routes:
                if route.target_component in closure and route.source_component not in closure:
                    closure.add(route.source_component)
                    changed = True
        return closure

    def _calibrations(self, phases: tuple[PhaseSpec, ...]) -> tuple[ArtifactSpec, ...]:
        phase_ids = {value.phase_id for value in phases}
        artifacts: list[ArtifactSpec] = []
        for calibration in self.design.calibrations:
            producer = self._resolve_required(calibration.producer)
            if calibration.producer_phase not in phase_ids:
                raise DraftLoweringError((self._issue("authoring.calibration_phase", "/intent/calibrations", f"unknown producer phase {calibration.producer_phase}"),))
            artifacts.append(
                ArtifactSpec(
                    f"calibration.{calibration.calibration_id}",
                    calibration.role,
                    calibration.media_type,
                    calibration.producer_phase,
                    producer.component_id,
                    calibration.producer_port,
                )
            )
        return tuple(artifacts)

    def _initial_phase(self, phases: tuple[PhaseSpec, ...]) -> str:
        if self.design.initial_phase is not None:
            return self.design.initial_phase
        if len(phases) == 1:
            return phases[0].phase_id
        if not self.design.phases:
            return "phase.run"
        raise DraftLoweringError(
            (
                self._issue(
                    "authoring.initial_phase",
                    "/intent/initial_phase",
                    "multi-phase designs require an explicit initial_phase",
                ),
            )
        )

    def _adaptations(self) -> tuple[AdaptationSpec, ...]:
        adaptations: list[AdaptationSpec] = []
        for value in self.design.adaptations:
            model_component = self._resolve_required(value.model).component_id
            outcome_id = value.outcome.removeprefix("outcome.")
            expectation_id = f"expectation.{outcome_id}.{model_component.removeprefix('model.')}"
            adaptations.append(AdaptationSpec(value.semantic_id, expectation_id, model_component, value.enabled_phases))
        return tuple(adaptations)

    def _component(self, component: ComponentSpec) -> None:
        if component.component_id in self.components:
            raise DraftLoweringError(
                (
                    self._issue(
                        "authoring.generated_identity",
                        "/intent",
                        f"declarations collide at generated component {component.component_id}",
                    ),
                )
            )
        self.components[component.component_id] = component

    def _bind(
        self,
        artifact: CanonicalArtifact,
        canonical_path: str,
        draft_path: str,
    ) -> None:
        """Bind one canonical field to one exact authoring field."""

        self.bindings[(artifact, canonical_path)] = draft_path

    def _route(self, source: _OutputBinding, target_component: str, target_port: str) -> None:
        route_id = f"route.{target_component}.{target_port}"
        if route_id in self.routes:
            raise DraftLoweringError((self._issue("authoring.duplicate_input", "/intent", f"multiple inputs target {target_component}.{target_port}"),))
        self.routes[route_id] = RouteSpec(route_id, source.component_id, source.port, target_component, target_port)

    def _resolve(self, reference: str) -> _OutputBinding | None:
        base = _reference_base(reference, set(self.outputs), allow_port=True)
        if base is None:
            return None
        output = self.outputs.get(base)
        if output is None:
            return None
        if reference == base:
            return output
        return _OutputBinding(
            output.component_id,
            reference[len(base) + 1 :],
            output.kind,
            output.contract,
        )

    def _resolve_required(self, reference: str) -> _OutputBinding:
        value = self._resolve(reference)
        if value is None:
            raise DraftLoweringError((self._issue("authoring.unresolved_reference", "/intent", f"cannot lower unresolved reference {reference}"),))
        return value

    def _issue(self, code: str, path: str, message: str) -> DraftIssue:
        return DraftIssue(code, path, message, self.source_map.source_for(path))

    def _provenance(self, protocol: ProtocolSpec, suite: SuiteSpec) -> AuthoringProvenance:
        self._index_bindings(suite)
        entries: list[ProvenanceEntry] = []
        for artifact, payload in (
            (CanonicalArtifact.PROTOCOL, protocol.to_payload()),
            (CanonicalArtifact.SUITE, suite.to_payload()),
        ):
            for path in _leaf_paths(payload):
                draft_path = self._binding_for(artifact, path)
                origin = (
                    self.design.origin_for_draft_path(draft_path)
                    if draft_path is not None
                    else AuthoringOrigin.AUTHORING_DERIVED
                )
                materiality = _materiality(artifact, path)
                confirmation = (
                    ConfirmationState.PENDING
                    if origin in {
                        AuthoringOrigin.AUTHORING_DEFAULT,
                        AuthoringOrigin.TEMPLATE_DEFAULT,
                        AuthoringOrigin.DETECTION_PROPOSAL,
                    }
                    and materiality == ScientificMateriality.SCIENTIFIC
                    else ConfirmationState.NOT_REQUIRED
                )
                entries.append(
                    ProvenanceEntry(
                        artifact,
                        path,
                        origin,
                        self.source_map.source_for(draft_path or "/intent"),
                        materiality,
                        confirmation,
                    )
                )
        return AuthoringProvenance(
            self.draft.draft_id,
            self.draft.revision,
            self.draft.draft_digest,
            {
                CanonicalArtifact.PROTOCOL: CanonicalTarget(PROTOCOL_SPEC_SCHEMA, protocol.spec_hash),
                CanonicalArtifact.SUITE: CanonicalTarget(SUITE_SPEC_SCHEMA, suite.spec_hash),
            },
            tuple(entries),
        )

    def _index_bindings(self, suite: SuiteSpec) -> None:
        signals = {
            f"stream.{value.signal_id}": (index, value)
            for index, value in enumerate(self.design.signals)
        }
        events = {
            f"stream.{value.event_id}": (index, value)
            for index, value in enumerate(self.design.events)
        }
        for index, stream in enumerate(suite.streams):
            canonical = f"/streams/{index}"
            if stream.stream_id in signals:
                design_index, signal = signals[stream.stream_id]
                draft = f"/intent/signals/{design_index}"
                for target, source in {
                    "/modality": "/modality",
                    "/clock_id": "/clock_id",
                    "/contract/unit": "/unit",
                    "/contract/nominal_rate_hz": "/nominal_rate_hz",
                    "/contract/rate_model": "/rate_model",
                    "/contract/missing_data_policy": "/missing_data_policy",
                    "/contract/layout": "/layout",
                }.items():
                    self.bindings[(CanonicalArtifact.SUITE, canonical + target)] = draft + source
                for channel_index, channel in enumerate(signal.channels):
                    self.bindings[
                        (CanonicalArtifact.SUITE, f"{canonical}/contract/channel_ids/{channel_index}")
                    ] = f"{draft}/channels/{channel_index}"
                    unit_path = (
                        f"{draft}/channel_units/{_escape_pointer(channel)}"
                        if channel in signal.channel_units
                        else f"{draft}/unit"
                    )
                    self.bindings[
                        (CanonicalArtifact.SUITE, f"{canonical}/contract/units/{_escape_pointer(channel)}")
                    ] = unit_path
            elif stream.stream_id in events:
                design_index, event = events[stream.stream_id]
                draft = f"/intent/events/{design_index}"
                for target, source in {
                    "/modality": "/modality",
                    "/clock_id": "/clock_id",
                }.items():
                    self.bindings[(CanonicalArtifact.SUITE, canonical + target)] = draft + source
                for event_index, _ in enumerate(event.event_kinds):
                    self.bindings[
                        (CanonicalArtifact.SUITE, f"{canonical}/contract/event_kinds/{event_index}")
                    ] = f"{draft}/event_kinds/{event_index}"
        if self.design.study.suite_id is not None:
            self._bind(CanonicalArtifact.SUITE, "/suite_id", "/intent/study/suite_id")
        if self.design.study.protocol_id is not None:
            self._bind(CanonicalArtifact.SUITE, "/protocol_id", "/intent/study/protocol_id")
        self._bind(
            CanonicalArtifact.SUITE,
            "/clock_policy/execution_clock_id",
            "/intent/study/execution_clock_id",
        )
        if self.design.initial_phase is not None:
            self._bind(CanonicalArtifact.SUITE, "/initial_phase", "/intent/initial_phase")
        for field in ("execution_capture", "semantic_evidence", "raw_recording"):
            self._bind(
                CanonicalArtifact.SUITE,
                f"/recording/{field}",
                f"/intent/recording/{field}",
            )
        for index, component in enumerate(suite.components):
            self._index_component_bindings(index, component)
        for index, route in enumerate(suite.routes):
            self._index_route_bindings(index, route)
        for index, model in enumerate(suite.model_uses):
            declaration = next(
                value
                for value in self.design.models
                if f"model.{value.model_id}" == model.component_id
            )
            design_index = self.design.models.index(declaration)
            draft = f"/intent/models/{design_index}"
            self._bind(
                CanonicalArtifact.SUITE,
                f"/model_uses/{index}/manifest_digest",
                draft + "/manifest_digest",
            )
            self._bind(
                CanonicalArtifact.SUITE,
                f"/model_uses/{index}/role_id",
                draft + "/role",
            )
            if model.comparison_group is not None:
                comparison = next(
                    value
                    for value in self.design.comparisons
                    if declaration.semantic_id in value.members
                )
                comparison_index = self.design.comparisons.index(comparison)
                member_index = comparison.members.index(declaration.semantic_id)
                self._bind(
                    CanonicalArtifact.SUITE,
                    f"/model_uses/{index}/comparison_group",
                    f"/intent/comparisons/{comparison_index}/members/{member_index}",
                )
        for index, outcome in enumerate(suite.outcome_expectations):
            component_id = outcome.outcome_component_ids[0]
            declaration = next(
                value
                for value in self.design.outcomes
                if f"outcome.{value.outcome_id}" == component_id
            )
            design_index = self.design.outcomes.index(declaration)
            draft = f"/intent/outcomes/{design_index}"
            model_id = outcome.model_component_id
            model_index = declaration.models.index(model_id)
            self._bind(
                CanonicalArtifact.SUITE,
                f"/outcome_expectations/{index}/model_component_id",
                f"{draft}/models/{model_index}",
            )
            for target, source in (
                ("permitted_uses", "permitted_uses"),
                ("max_pending_predictions", "max_pending_predictions"),
                ("prediction_ttl_seconds", "prediction_ttl_seconds"),
            ):
                canonical_value = getattr(outcome, target)
                for relative in _leaf_paths(canonical_value):
                    self._bind(
                        CanonicalArtifact.SUITE,
                        f"/outcome_expectations/{index}/{target}" + relative,
                        f"{draft}/{source}" + relative,
                    )
        for index, adaptation in enumerate(suite.adaptations):
            path = _semantic_draft_path(self.design, adaptation.adaptation_id)
            if path is not None:
                self._bind(
                    CanonicalArtifact.SUITE,
                    f"/adaptations/{index}/adaptation_id",
                    path + "/adaptation_id",
                )
                self._bind(
                    CanonicalArtifact.SUITE,
                    f"/adaptations/{index}/model_component_id",
                    path + "/model",
                )
                self._bind(
                    CanonicalArtifact.SUITE,
                    f"/adaptations/{index}/expectation_id",
                    path + "/outcome",
                )
                for phase_index, _ in enumerate(adaptation.enabled_phases):
                    self._bind(
                        CanonicalArtifact.SUITE,
                        f"/adaptations/{index}/enabled_phases/{phase_index}",
                        f"{path}/enabled_phases/{phase_index}",
                    )
        phase_paths = {
            value.semantic_id: f"/intent/phases/{index}"
            for index, value in enumerate(self.design.phases)
        }
        for index, phase in enumerate(suite.phases):
            path = phase_paths.get(phase.phase_id)
            if path is not None:
                self._bind(
                    CanonicalArtifact.SUITE,
                    f"/phases/{index}/phase_id",
                    path + "/phase_id",
                )
                for field in (
                    "retry_limit",
                    "resume_policy",
                    "operator_confirmation",
                    "timeout_seconds",
                ):
                    self._bind(
                        CanonicalArtifact.SUITE,
                        f"/phases/{index}/{field}",
                        f"{path}/{field}",
                    )
                for transition_index, _ in enumerate(phase.transitions):
                    self._bind(
                        CanonicalArtifact.SUITE,
                        f"/phases/{index}/transitions/{transition_index}/target_phase",
                        f"{path}/next_phases/{transition_index}",
                    )
        calibration_paths = {
            value.semantic_id: f"/intent/calibrations/{index}"
            for index, value in enumerate(self.design.calibrations)
        }
        for index, artifact in enumerate(suite.artifacts):
            path = calibration_paths.get(artifact.artifact_id)
            if path is not None:
                for target, source in (
                    ("artifact_id", "calibration_id"),
                    ("role", "role"),
                    ("media_type", "media_type"),
                    ("producer_phase", "producer_phase"),
                    ("producer_component", "producer"),
                    ("producer_port", "producer_port"),
                ):
                    self._bind(
                        CanonicalArtifact.SUITE,
                        f"/artifacts/{index}/{target}",
                        f"{path}/{source}",
                    )

    def _index_component_bindings(self, index: int, component: ComponentSpec) -> None:
        canonical = f"/components/{index}"
        for chain_index, chain in enumerate(self.design.processing):
            for step_index, step in enumerate(chain.steps):
                if component.component_id != f"processing.{chain.chain_id}.{step.step_id}":
                    continue
                draft = f"/intent/processing/{chain_index}/steps/{step_index}"
                self._bind(CanonicalArtifact.SUITE, canonical + "/plugin_id", draft + "/plugin_id")
                self._bind(CanonicalArtifact.SUITE, canonical + "/version_spec", draft + "/version_spec")
                self._bind_json_fields(canonical + "/config", draft + "/config", step.config)
                update = step.output_contract.to_payload()
                for field, value in update.items():
                    if field == "drop_fields":
                        continue
                    for relative in _leaf_paths(value):
                        self._bind(
                            CanonicalArtifact.SUITE,
                            f"{canonical}/output_contracts/{_escape_pointer(step.output_port)}/{field}" + relative,
                            f"{draft}/output_contract/{field}" + relative,
                        )
                return

        declaration_groups = (
            ("quality_gates", self.design.quality_gates, "quality", "gate_id"),
            ("models", self.design.models, "model", "model_id"),
            ("outcomes", self.design.outcomes, "outcome", "outcome_id"),
            ("policies", self.design.policies, "policy", "policy_id"),
            ("actions", self.design.actions, "actuator", "action_id"),
        )
        for category, declarations, component_prefix, identity_field in declaration_groups:
            for design_index, declaration in enumerate(declarations):
                identity = getattr(declaration, identity_field)
                if component.component_id != f"{component_prefix}.{identity}":
                    continue
                draft = f"/intent/{category}/{design_index}"
                self._bind(CanonicalArtifact.SUITE, canonical + "/plugin_id", draft + "/plugin_id")
                self._bind(CanonicalArtifact.SUITE, canonical + "/version_spec", draft + "/version_spec")
                if category in {"quality_gates", "models", "policies", "actions"}:
                    self._bind_json_fields(canonical + "/config", draft + "/config", declaration.config)
                if category == "quality_gates":
                    self._bind(
                        CanonicalArtifact.SUITE,
                        canonical + "/config/gate_id",
                        draft + "/gate_id",
                    )
                elif category == "outcomes":
                    self._bind(
                        CanonicalArtifact.SUITE,
                        canonical + "/config/event_kind",
                        draft + "/event_kind",
                    )
                    for value_index, _ in enumerate(declaration.permitted_uses):
                        for target in ("config/permitted_uses", "outcome_uses"):
                            self._bind(
                                CanonicalArtifact.SUITE,
                                f"{canonical}/{target}/{value_index}",
                                f"{draft}/permitted_uses/{value_index}",
                            )
                elif category == "actions":
                    self._bind(
                        CanonicalArtifact.SUITE,
                        canonical + "/action_capabilities/0",
                        draft + "/capability",
                    )
                return

        for design_index, window in enumerate(self.design.windows):
            if component.component_id != f"window.{window.window_id}":
                continue
            draft = f"/intent/windows/{design_index}"
            if window.kind == WindowKind.EVENT:
                for target, source in (
                    ("start_offset_seconds", "start_offset_seconds"),
                    ("end_offset_seconds", "end_offset_seconds"),
                    ("max_buffer_samples", "max_buffer_samples"),
                    ("event_kinds/0", "event_kind"),
                ):
                    self._bind(
                        CanonicalArtifact.SUITE,
                        f"{canonical}/config/{target}",
                        f"{draft}/{source}",
                    )
            return

    def _bind_json_fields(self, canonical: str, draft: str, value: Mapping[str, Any]) -> None:
        if not value:
            return
        for relative in _leaf_paths(value):
            self._bind(CanonicalArtifact.SUITE, canonical + relative, draft + relative)

    def _index_route_bindings(self, index: int, route: RouteSpec) -> None:
        source_path, target_path = self._route_authoring_paths(route)
        canonical = f"/routes/{index}"
        if source_path is not None:
            for field in ("component", "port"):
                self._bind(
                    CanonicalArtifact.SUITE,
                    f"{canonical}/source/{field}",
                    source_path,
                )
        if target_path is not None:
            for field in ("component", "port"):
                self._bind(
                    CanonicalArtifact.SUITE,
                    f"{canonical}/target/{field}",
                    target_path,
                )

    def _route_authoring_paths(self, route: RouteSpec) -> tuple[str | None, str | None]:
        for chain_index, chain in enumerate(self.design.processing):
            for step_index, step in enumerate(chain.steps):
                if route.target_component == f"processing.{chain.chain_id}.{step.step_id}":
                    draft = f"/intent/processing/{chain_index}"
                    source = draft + "/input" if step_index == 0 else None
                    return source, f"{draft}/steps/{step_index}/input_port"
        for design_index, window in enumerate(self.design.windows):
            if route.target_component == f"window.{window.window_id}":
                field = "event_stream" if route.target_port == "events" else "input"
                path = f"/intent/windows/{design_index}/{field}"
                return path, path
        for design_index, gate in enumerate(self.design.quality_gates):
            if route.target_component == f"quality.{gate.gate_id}":
                return (
                    f"/intent/quality_gates/{design_index}/input",
                    f"/intent/quality_gates/{design_index}/input_port",
                )
        for category, declarations, prefix in (
            ("models", self.design.models, "model"),
            ("policies", self.design.policies, "policy"),
        ):
            for design_index, declaration in enumerate(declarations):
                identity = getattr(declaration, f"{prefix}_id" if prefix == "model" else "policy_id")
                if route.target_component == f"{prefix}.{identity}":
                    path = f"/intent/{category}/{design_index}/inputs/{_escape_pointer(route.target_port)}"
                    return path, path
        for design_index, outcome in enumerate(self.design.outcomes):
            if route.target_component == f"outcome.{outcome.outcome_id}":
                path = f"/intent/outcomes/{design_index}/event_stream"
                return path, path
        for design_index, action in enumerate(self.design.actions):
            if route.target_component == f"actuator.{action.action_id}":
                return (
                    f"/intent/actions/{design_index}/policy",
                    f"/intent/actions/{design_index}/actuator_input_port",
                )
        for recording_index, reference in enumerate(self.design.recording.inputs):
            if route.target_component == f"sink.{reference.replace(':', '.').replace('/', '.')}":
                path = f"/intent/recording/inputs/{recording_index}"
                return path, path
        return None, None

    def _binding_for(self, artifact: CanonicalArtifact, path: str) -> str | None:
        matches = [
            (prefix, draft_path)
            for (candidate, prefix), draft_path in self.bindings.items()
            if candidate == artifact and (not prefix or path == prefix or path.startswith(prefix + "/"))
        ]
        if matches:
            return max(matches, key=lambda item: len(item[0]))[1]
        return None


def _design_requirements(suite: SuiteSpec) -> DeploymentRequirements:
    streams = {value.stream_id: value for value in suite.streams}
    execution_clock = str(suite.clock_policy["execution_clock_id"])
    requirements: list[DeploymentRequirement] = []
    for component in suite.components:
        if component.kind != ComponentKind.SOURCE or component.stream_id is None:
            continue
        stream = streams[component.stream_id]
        suffix = component.component_id.removeprefix("source.")
        requirements.append(
            DeploymentRequirement(
                f"requirement.source.{suffix}",
                DeploymentRequirementKind.SOURCE_BINDING,
                component_id=component.component_id,
                stream_id=stream.stream_id,
                contract=stream.contract.to_payload(),
            )
        )
        if stream.clock_id is not None and stream.clock_id != execution_clock:
            requirements.append(
                DeploymentRequirement(
                    f"requirement.clock.{suffix}",
                    DeploymentRequirementKind.CLOCK_MAPPING,
                    source_clock=stream.clock_id,
                    target_clock=execution_clock,
                )
            )
    requirements.append(DeploymentRequirement("requirement.storage.evidence", DeploymentRequirementKind.STORAGE, storage_kind="evidence"))
    for model in suite.model_uses:
        requirements.append(
            DeploymentRequirement(
                f"requirement.model.{model.component_id.removeprefix('model.')}",
                DeploymentRequirementKind.MODEL_ARTIFACT,
                component_id=model.component_id,
                manifest_digest=model.manifest_digest,
            )
        )
    for component in suite.components:
        for capability in component.action_capabilities:
            requirements.append(
                DeploymentRequirement(
                    f"requirement.authorization.{component.component_id.removeprefix('actuator.')}",
                    DeploymentRequirementKind.AUTHORIZATION,
                    component_id=component.component_id,
                    action_capability=capability,
                )
            )
    return DeploymentRequirements(suite.suite_id, tuple(requirements))


def _reference_base(reference: str, candidates: set[str], *, allow_port: bool) -> str | None:
    if reference in candidates:
        return reference
    if not allow_port:
        return None
    matches = [candidate for candidate in candidates if reference.startswith(candidate + ".")]
    return max(matches, key=len, default=None)


def _seconds_to_samples(seconds: float | None, rate_hz: float, field: str) -> int:
    if seconds is None:
        raise ValueError(f"{field} is required")
    exact = seconds * rate_hz
    rounded = round(exact)
    if abs(exact - rounded) > 1e-9 or rounded <= 0:
        raise DraftLoweringError(
            (
                DraftIssue(
                    "authoring.window_samples",
                    "/intent/windows",
                    f"{field} at {rate_hz:g} Hz must resolve to a positive whole sample count",
                    _generated_source(field),
                ),
            )
        )
    return int(rounded)


def _record_kind(contract: SignalContract) -> str:
    if contract.type_id == "eegle.dense_sample_batch.v1":
        return "dense"
    if contract.type_id == "eegle.sparse_event_batch.v1":
        return "sparse"
    return "structured"


__all__ = ["lower_experiment_design"]
