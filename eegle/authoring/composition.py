"""Persistent immutable editing of named compositional experiment designs."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from eegle._domain import ExecutionMode
from eegle._validation import require_identifier
from eegle.authoring.contracts import AuthoringOrigin, SourceKind, SourceLocation
from eegle.authoring.design import (
    AcceptanceDeclaration, ActionDeclaration, AdaptationDeclaration,
    CalibrationDeclaration, ComparisonGroup, EventDeclaration, ModelDeclaration,
    OutcomeDeclaration, PhaseDeclaration, PolicyDeclaration, ProcessingChain,
    ProcessingStep, QualityGateDeclaration, RecordingDeclaration,
    SignalDeclaration, StudyIntent, WindowDeclaration, WindowKind,
    _optional_string, _prefixed_reference, _unique,
)
from eegle.authoring.design_provenance import (
    _escape_pointer, _generated_source, _leaf_paths, _semantic_key_for_draft_path,
    _semantic_origins, _semantic_sources,
)
from eegle.authoring.drafts import ExperimentDraft
from eegle.authoring.provenance import DraftSourceMap
from eegle.authoring.schemas import EXPERIMENT_DESIGN_JSON_SCHEMA, EXPERIMENT_DESIGN_SCHEMA_ID
from eegle.compiler import canonical_hash, canonical_json_bytes
from eegle.specs import ComparisonOperator, ResumePolicy, validate_payload

if TYPE_CHECKING:
    from eegle.authoring.composed_projects import ComposedExperiment


_UNSET = object()


@dataclass(frozen=True, slots=True)
class ExperimentDesign:
    """Immutable named scientific design above the canonical graph boundary."""

    experiment_id: str
    study: StudyIntent
    revision: int = 1
    signals: tuple[SignalDeclaration, ...] = ()
    events: tuple[EventDeclaration, ...] = ()
    processing: tuple[ProcessingChain, ...] = ()
    windows: tuple[WindowDeclaration, ...] = ()
    quality_gates: tuple[QualityGateDeclaration, ...] = ()
    models: tuple[ModelDeclaration, ...] = ()
    comparisons: tuple[ComparisonGroup, ...] = ()
    outcomes: tuple[OutcomeDeclaration, ...] = ()
    adaptations: tuple[AdaptationDeclaration, ...] = ()
    calibrations: tuple[CalibrationDeclaration, ...] = ()
    policies: tuple[PolicyDeclaration, ...] = ()
    actions: tuple[ActionDeclaration, ...] = ()
    phases: tuple[PhaseDeclaration, ...] = ()
    initial_phase: str | None = None
    recording: RecordingDeclaration = RecordingDeclaration()
    acceptance: tuple[AcceptanceDeclaration, ...] = ()
    sources: Mapping[str, SourceLocation] = field(default_factory=dict, repr=False, compare=False)
    field_origins: Mapping[str, AuthoringOrigin] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    schema: str = EXPERIMENT_DESIGN_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != EXPERIMENT_DESIGN_SCHEMA_ID:
            raise ValueError(f"unsupported experiment design schema: {self.schema}")
        object.__setattr__(self, "experiment_id", require_identifier(self.experiment_id, "experiment_id"))
        if not isinstance(self.study, StudyIntent):
            raise TypeError("study must be a StudyIntent")
        object.__setattr__(self, "revision", int(self.revision))
        if self.revision <= 0:
            raise ValueError("design revision must be positive")
        typed_collections = (
            ("signals", SignalDeclaration, "signal_id"),
            ("events", EventDeclaration, "event_id"),
            ("processing", ProcessingChain, "chain_id"),
            ("windows", WindowDeclaration, "window_id"),
            ("quality_gates", QualityGateDeclaration, "gate_id"),
            ("models", ModelDeclaration, "model_id"),
            ("comparisons", ComparisonGroup, "comparison_id"),
            ("outcomes", OutcomeDeclaration, "outcome_id"),
            ("adaptations", AdaptationDeclaration, "adaptation_id"),
            ("calibrations", CalibrationDeclaration, "calibration_id"),
            ("policies", PolicyDeclaration, "policy_id"),
            ("actions", ActionDeclaration, "action_id"),
            ("phases", PhaseDeclaration, "phase_id"),
            ("acceptance", AcceptanceDeclaration, "criterion_id"),
        )
        for name, expected, identity in typed_collections:
            values = tuple(getattr(self, name))
            if not all(isinstance(value, expected) for value in values):
                raise TypeError(f"{name} must contain {expected.__name__} values")
            values = tuple(sorted(values, key=lambda value: getattr(value, identity)))
            _unique((getattr(value, identity) for value in values), name)
            object.__setattr__(self, name, values)
        source_ids = {value.signal_id for value in self.signals}
        event_ids = {value.event_id for value in self.events}
        if source_ids & event_ids:
            overlap = ", ".join(sorted(source_ids & event_ids))
            raise ValueError(
                "dense signals and event streams cannot share generated stream IDs: "
                + overlap
            )
        if not isinstance(self.recording, RecordingDeclaration):
            raise TypeError("recording must be a RecordingDeclaration")
        if self.initial_phase is not None:
            object.__setattr__(
                self,
                "initial_phase",
                _prefixed_reference(self.initial_phase, "phase", "initial_phase"),
            )
        locations = {str(key): value for key, value in self.sources.items()}
        if not all(isinstance(value, SourceLocation) for value in locations.values()):
            raise TypeError("design sources must be SourceLocation values")
        object.__setattr__(self, "sources", MappingProxyType(locations))
        origins = {str(key): AuthoringOrigin(value) for key, value in self.field_origins.items()}
        object.__setattr__(self, "field_origins", MappingProxyType(origins))
        validate_payload(self.to_payload(), EXPERIMENT_DESIGN_JSON_SCHEMA)

    @classmethod
    def create(
        cls,
        experiment_id: str,
        statement: str,
        *,
        execution_mode: ExecutionMode | object = _UNSET,
        execution_clock_id: str | object = _UNSET,
        annotations: Mapping[str, Any] | None | object = _UNSET,
    ) -> "ExperimentDesign":
        source = SourceLocation(SourceKind.PYTHON, symbol="ExperimentDesign.create")
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {"statement"},
            {
                "execution_mode": (execution_mode, ExecutionMode.CAUSAL),
                "execution_clock_id": (execution_clock_id, "boundary.clock"),
                "annotations": (annotations, None),
                "protocol_id": (_UNSET, None),
                "suite_id": (_UNSET, None),
            },
        )
        sources = {"study": source}
        origins: dict[str, AuthoringOrigin] = {}
        for name in explicit:
            sources[f"study/{name}"] = source
            origins[f"study/{name}"] = AuthoringOrigin.USER_EXPLICIT
        for name in defaulted:
            sources[f"study/{name}"] = _generated_source(f"study/{name}")
            origins[f"study/{name}"] = AuthoringOrigin.AUTHORING_DEFAULT
        return cls(
            experiment_id=experiment_id,
            study=StudyIntent(
                statement,
                normalized["execution_mode"],
                str(normalized["execution_clock_id"]),
                annotations=normalized["annotations"] or {},
            ),
            sources=sources,
            field_origins=origins,
        )

    @property
    def design_digest(self) -> str:
        return canonical_hash(self.to_payload())

    def dense_signal(
        self,
        signal_id: str,
        *,
        modality: str,
        channels: Sequence[str],
        unit: str,
        rate_hz: float | None,
        clock_id: str | object = _UNSET,
        rate_model: str | object = _UNSET,
        channel_units: Mapping[str, str] | None | object = _UNSET,
        missing_data_policy: str | object = _UNSET,
        layout: str | object = _UNSET,
    ) -> "ExperimentDesign":
        optional = {
            "clock_id": (clock_id, "device.clock"),
            "rate_model": (rate_model, "regular"),
            "channel_units": (channel_units, None),
            "missing_data_policy": (missing_data_policy, "explicit_validity"),
            "layout": (layout, "samples_by_channels"),
        }
        normalized = {
            name: default if supplied is _UNSET else supplied
            for name, (supplied, default) in optional.items()
        }
        value = SignalDeclaration(
            signal_id,
            modality,
            tuple(channels),
            unit,
            rate_hz,
            str(normalized["clock_id"]),
            str(normalized["rate_model"]),
            normalized["channel_units"] or {},
            str(normalized["missing_data_policy"]),
            str(normalized["layout"]),
        )
        required_fields = {
            "signal_id",
            "modality",
            "channels",
            "unit",
            "nominal_rate_hz",
        }
        explicit_fields = required_fields | {
            name for name, (supplied, _) in optional.items() if supplied is not _UNSET
        }
        default_fields = set(optional) - explicit_fields
        return self._append(
            "signals",
            value,
            value.semantic_id,
            "ExperimentDesign.dense_signal",
            explicit_fields=explicit_fields,
            default_fields=default_fields,
        )

    def event_stream(
        self,
        event_id: str,
        *,
        kinds: Sequence[str],
        modality: str | object = _UNSET,
        clock_id: str | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {"event_id", "event_kinds"},
            {
                "modality": (modality, "markers"),
                "clock_id": (clock_id, "device.clock"),
            },
        )
        value = EventDeclaration(
            event_id,
            tuple(kinds),
            str(normalized["modality"]),
            str(normalized["clock_id"]),
        )
        return self._append(
            "events",
            value,
            value.semantic_id,
            "ExperimentDesign.event_stream",
            explicit_fields=explicit,
            default_fields=defaulted,
        )

    def processing_chain(
        self,
        chain_id: str,
        *,
        input: str,
        steps: Sequence[ProcessingStep],
    ) -> "ExperimentDesign":
        value = ProcessingChain(chain_id, input, tuple(steps))
        return self._append("processing", value, value.semantic_id, "ExperimentDesign.processing_chain")

    def continuous_window(
        self,
        window_id: str,
        *,
        input: str,
        duration_seconds: float,
        step_seconds: float,
        max_buffer_samples: int | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {"window_id", "kind", "input", "duration_seconds", "step_seconds"},
            {"max_buffer_samples": (max_buffer_samples, 100_000)},
        )
        value = WindowDeclaration(
            window_id,
            WindowKind.CONTINUOUS,
            input,
            duration_seconds=duration_seconds,
            step_seconds=step_seconds,
            max_buffer_samples=int(normalized["max_buffer_samples"]),
        )
        return self._append(
            "windows",
            value,
            value.semantic_id,
            "ExperimentDesign.continuous_window",
            explicit_fields=explicit,
            default_fields=defaulted,
        )

    def event_window(
        self,
        window_id: str,
        *,
        input: str,
        event_stream: str,
        event_kind: str,
        start_seconds: float,
        end_seconds: float,
        max_buffer_samples: int | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {
                "window_id",
                "kind",
                "input",
                "event_stream",
                "event_kind",
                "start_offset_seconds",
                "end_offset_seconds",
            },
            {"max_buffer_samples": (max_buffer_samples, 100_000)},
        )
        value = WindowDeclaration(
            window_id,
            WindowKind.EVENT,
            input,
            event_stream=event_stream,
            event_kind=event_kind,
            start_offset_seconds=start_seconds,
            end_offset_seconds=end_seconds,
            max_buffer_samples=int(normalized["max_buffer_samples"]),
        )
        return self._append(
            "windows",
            value,
            value.semantic_id,
            "ExperimentDesign.event_window",
            explicit_fields=explicit,
            default_fields=defaulted,
        )

    def quality_gate(
        self,
        gate_id: str,
        *,
        input: str,
        plugin_id: str | object = _UNSET,
        version_spec: str | object = _UNSET,
        config: Mapping[str, Any] | None | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {"gate_id", "input"},
            {
                "plugin_id": (plugin_id, "eegle.processing.finite_quality"),
                "version_spec": (version_spec, "~=0.1.0"),
                "config": (config, None),
                "input_port": (_UNSET, "item"),
                "output_port": (_UNSET, "decision"),
            },
        )
        value = QualityGateDeclaration(
            gate_id,
            input,
            str(normalized["plugin_id"]),
            str(normalized["version_spec"]),
            normalized["config"] or {},
        )
        return self._append(
            "quality_gates",
            value,
            value.semantic_id,
            "ExperimentDesign.quality_gate",
            explicit_fields=explicit,
            default_fields=defaulted,
        )

    def model(
        self,
        model_id: str,
        *,
        plugin_id: str,
        manifest_digest: str,
        role: str,
        inputs: Mapping[str, str],
        version_spec: str | object = _UNSET,
        config: Mapping[str, Any] | None | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {"model_id", "plugin_id", "manifest_digest", "role", "inputs"},
            {
                "version_spec": (version_spec, "~=0.1.0"),
                "config": (config, None),
            },
        )
        value = ModelDeclaration(
            model_id,
            plugin_id,
            manifest_digest,
            role,
            inputs,
            str(normalized["version_spec"]),
            normalized["config"] or {},
        )
        return self._append(
            "models",
            value,
            value.semantic_id,
            "ExperimentDesign.model",
            explicit_fields=explicit,
            default_fields=defaulted,
        )

    def comparison_group(self, comparison_id: str, *, members: Sequence[str]) -> "ExperimentDesign":
        value = ComparisonGroup(comparison_id, tuple(members))
        return self._append("comparisons", value, value.semantic_id, "ExperimentDesign.comparison_group")

    def outcome(
        self,
        outcome_id: str,
        *,
        event_stream: str,
        event_kind: str,
        models: Sequence[str],
        permitted_uses: Sequence[str] | object = _UNSET,
        max_pending_predictions: int | object = _UNSET,
        prediction_ttl_seconds: float | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {"outcome_id", "event_stream", "event_kind", "models"},
            {
                "permitted_uses": (permitted_uses, ("metrics",)),
                "max_pending_predictions": (max_pending_predictions, 128),
                "prediction_ttl_seconds": (prediction_ttl_seconds, 300.0),
            },
        )
        value = OutcomeDeclaration(
            outcome_id,
            event_stream,
            event_kind,
            tuple(models),
            tuple(normalized["permitted_uses"]),
            max_pending_predictions=int(normalized["max_pending_predictions"]),
            prediction_ttl_seconds=float(normalized["prediction_ttl_seconds"]),
        )
        return self._append(
            "outcomes",
            value,
            value.semantic_id,
            "ExperimentDesign.outcome",
            explicit_fields=explicit,
            default_fields=defaulted,
        )

    def adaptation(
        self,
        adaptation_id: str,
        *,
        model: str,
        outcome: str,
        enabled_phases: Sequence[str],
    ) -> "ExperimentDesign":
        value = AdaptationDeclaration(adaptation_id, model, outcome, tuple(enabled_phases))
        return self._append("adaptations", value, value.semantic_id, "ExperimentDesign.adaptation")

    def calibration(
        self,
        calibration_id: str,
        *,
        producer: str,
        producer_port: str,
        producer_phase: str,
        required_phases: Sequence[str],
        media_type: str | object = _UNSET,
        role: str | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {
                "calibration_id",
                "producer",
                "producer_port",
                "producer_phase",
                "required_phases",
            },
            {
                "media_type": (media_type, "application/json"),
                "role": (role, "calibration_state"),
            },
        )
        value = CalibrationDeclaration(
            calibration_id,
            producer,
            producer_port,
            producer_phase,
            tuple(required_phases),
            str(normalized["media_type"]),
            str(normalized["role"]),
        )
        return self._append(
            "calibrations",
            value,
            value.semantic_id,
            "ExperimentDesign.calibration",
            explicit_fields=explicit,
            default_fields=defaulted,
        )

    def policy(
        self,
        policy_id: str,
        *,
        plugin_id: str,
        inputs: Mapping[str, str],
        config: Mapping[str, Any] | None | object = _UNSET,
        version_spec: str | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {"policy_id", "plugin_id", "inputs"},
            {
                "config": (config, None),
                "version_spec": (version_spec, "~=0.1.0"),
            },
        )
        value = PolicyDeclaration(
            policy_id,
            plugin_id,
            inputs,
            normalized["config"] or {},
            str(normalized["version_spec"]),
        )
        return self._append(
            "policies",
            value,
            value.semantic_id,
            "ExperimentDesign.policy",
            explicit_fields=explicit,
            default_fields=defaulted,
        )

    def action(
        self,
        action_id: str,
        *,
        capability: str,
        policy: str,
        plugin_id: str,
        version_spec: str | object = _UNSET,
        config: Mapping[str, Any] | None | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {"action_id", "capability", "policy", "plugin_id"},
            {
                "version_spec": (version_spec, "~=0.1.0"),
                "config": (config, None),
            },
        )
        value = ActionDeclaration(
            action_id,
            capability,
            policy,
            plugin_id,
            str(normalized["version_spec"]),
            normalized["config"] or {},
        )
        return self._append(
            "actions",
            value,
            value.semantic_id,
            "ExperimentDesign.action",
            explicit_fields=explicit,
            default_fields=defaulted,
        )

    def phase(
        self,
        phase_id: str,
        *,
        active: Sequence[str] | None | object = _UNSET,
        goals: Sequence[str] | None | object = _UNSET,
        include_dependencies: bool | object = _UNSET,
        exclude: Sequence[str] | object = _UNSET,
        next_phases: Sequence[str] | object = _UNSET,
        timeout_seconds: float | None | object = _UNSET,
        retry_limit: int | object = _UNSET,
        resume_policy: ResumePolicy | object = _UNSET,
        operator_confirmation: bool | object = _UNSET,
        initial: bool | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {"phase_id"},
            {
                "active": (active, None),
                "goals": (goals, None),
                "include_dependencies": (include_dependencies, True),
                "exclude": (exclude, ()),
                "next_phases": (next_phases, ()),
                "timeout_seconds": (timeout_seconds, None),
                "retry_limit": (retry_limit, 0),
                "resume_policy": (resume_policy, ResumePolicy.CHECKPOINT),
                "operator_confirmation": (operator_confirmation, False),
                "initial": (initial, False),
            },
        )
        value = PhaseDeclaration(
            phase_id=phase_id,
            active=tuple(normalized["active"] or ()),
            goals=tuple(normalized["goals"] or ()),
            include_dependencies=bool(normalized["include_dependencies"]),
            exclude=tuple(normalized["exclude"]),
            next_phases=tuple(normalized["next_phases"]),
            timeout_seconds=normalized["timeout_seconds"],
            retry_limit=int(normalized["retry_limit"]),
            resume_policy=normalized["resume_policy"],
            operator_confirmation=bool(normalized["operator_confirmation"]),
        )
        result = self._append(
            "phases",
            value,
            value.semantic_id,
            "ExperimentDesign.phase",
            explicit_fields=explicit - {"initial"},
            default_fields=defaulted - {"initial"},
        )
        if normalized["initial"]:
            if self.initial_phase is not None:
                raise ValueError("an initial phase is already declared")
            sources = dict(result.sources)
            origins = dict(result.field_origins)
            source = SourceLocation(SourceKind.PYTHON, symbol="ExperimentDesign.phase")
            sources["initial_phase"] = source
            origins["initial_phase"] = AuthoringOrigin.USER_EXPLICIT
            result = replace(
                result,
                initial_phase=value.semantic_id,
                sources=sources,
                field_origins=origins,
            )
        return result

    def with_initial_phase(self, phase_id: str) -> "ExperimentDesign":
        """Select the explicit entry phase for a multi-phase design."""

        sources = dict(self.sources)
        origins = dict(self.field_origins)
        sources["initial_phase"] = SourceLocation(
            SourceKind.PYTHON,
            symbol="ExperimentDesign.with_initial_phase",
        )
        origins["initial_phase"] = AuthoringOrigin.USER_EXPLICIT
        return replace(
            self,
            initial_phase=_prefixed_reference(phase_id, "phase", "initial_phase"),
            sources=sources,
            field_origins=origins,
        )

    def record(
        self,
        *inputs: str,
        phases: Sequence[str] | object = _UNSET,
        execution_capture: bool | object = _UNSET,
        semantic_evidence: bool | object = _UNSET,
        raw_recording: str | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {"inputs"},
            {
                "phases": (phases, ()),
                "execution_capture": (execution_capture, True),
                "semantic_evidence": (semantic_evidence, True),
                "raw_recording": (raw_recording, "reference"),
            },
        )
        recording = RecordingDeclaration(
            inputs=tuple(inputs),
            phases=tuple(normalized["phases"]),
            execution_capture=bool(normalized["execution_capture"]),
            semantic_evidence=bool(normalized["semantic_evidence"]),
            raw_recording=str(normalized["raw_recording"]),
        )
        sources = dict(self.sources)
        origins = dict(self.field_origins)
        source = SourceLocation(SourceKind.PYTHON, symbol="ExperimentDesign.record")
        sources["recording"] = source
        for name in explicit:
            sources[f"recording/{name}"] = source
            origins[f"recording/{name}"] = AuthoringOrigin.USER_EXPLICIT
        for name in defaulted:
            key = f"recording/{name}"
            sources[key] = _generated_source(key)
            origins[key] = AuthoringOrigin.AUTHORING_DEFAULT
        return replace(
            self,
            recording=recording,
            sources=sources,
            field_origins=origins,
        )

    def accept(
        self,
        criterion_id: str,
        *,
        metric_id: str,
        measure: str,
        operator: ComparisonOperator,
        value: float | int | bool | str,
        parameters: Mapping[str, Any] | None | object = _UNSET,
    ) -> "ExperimentDesign":
        normalized, explicit, defaulted = _normalize_optional_arguments(
            {"criterion_id", "metric_id", "measure", "operator", "value"},
            {"parameters": (parameters, None)},
        )
        acceptance = AcceptanceDeclaration(
            criterion_id,
            metric_id,
            measure,
            operator,
            value,
            normalized["parameters"] or {},
        )
        return self._append(
            "acceptance",
            acceptance,
            f"acceptance.{criterion_id}",
            "ExperimentDesign.accept",
            explicit_fields=explicit,
            default_fields=defaulted,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "experiment_id": self.experiment_id,
            "revision": self.revision,
            "study": self.study.to_payload(),
            "signals": [value.to_payload() for value in self.signals],
            "events": [value.to_payload() for value in self.events],
            "processing": [value.to_payload() for value in self.processing],
            "windows": [value.to_payload() for value in self.windows],
            "quality_gates": [value.to_payload() for value in self.quality_gates],
            "models": [value.to_payload() for value in self.models],
            "comparisons": [value.to_payload() for value in self.comparisons],
            "outcomes": [value.to_payload() for value in self.outcomes],
            "adaptations": [value.to_payload() for value in self.adaptations],
            "calibrations": [value.to_payload() for value in self.calibrations],
            "policies": [value.to_payload() for value in self.policies],
            "actions": [value.to_payload() for value in self.actions],
            "phases": [value.to_payload() for value in self.phases],
            "initial_phase": self.initial_phase,
            "recording": self.recording.to_payload(),
            "acceptance": [value.to_payload() for value in self.acceptance],
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        source_map: DraftSourceMap | None = None,
    ) -> "ExperimentDesign":
        validate_payload(payload, EXPERIMENT_DESIGN_JSON_SCHEMA)
        sources = _semantic_sources(payload, source_map)
        field_origins = _semantic_origins(payload)
        return cls(
            schema=str(payload["schema"]),
            experiment_id=str(payload["experiment_id"]),
            revision=int(payload["revision"]),
            study=StudyIntent.from_payload(payload["study"]),
            signals=tuple(SignalDeclaration.from_payload(value) for value in payload["signals"]),
            events=tuple(EventDeclaration.from_payload(value) for value in payload["events"]),
            processing=tuple(ProcessingChain.from_payload(value) for value in payload["processing"]),
            windows=tuple(WindowDeclaration.from_payload(value) for value in payload["windows"]),
            quality_gates=tuple(QualityGateDeclaration.from_payload(value) for value in payload["quality_gates"]),
            models=tuple(ModelDeclaration.from_payload(value) for value in payload["models"]),
            comparisons=tuple(ComparisonGroup.from_payload(value) for value in payload["comparisons"]),
            outcomes=tuple(OutcomeDeclaration.from_payload(value) for value in payload["outcomes"]),
            adaptations=tuple(AdaptationDeclaration.from_payload(value) for value in payload["adaptations"]),
            calibrations=tuple(CalibrationDeclaration.from_payload(value) for value in payload["calibrations"]),
            policies=tuple(PolicyDeclaration.from_payload(value) for value in payload["policies"]),
            actions=tuple(ActionDeclaration.from_payload(value) for value in payload["actions"]),
            phases=tuple(PhaseDeclaration.from_payload(value) for value in payload["phases"]),
            initial_phase=_optional_string(payload.get("initial_phase")),
            recording=RecordingDeclaration.from_payload(payload["recording"]),
            acceptance=tuple(AcceptanceDeclaration.from_payload(value) for value in payload["acceptance"]),
            sources=sources,
            field_origins=field_origins,
        )

    def canonical_json(self) -> str:
        return canonical_json_bytes(self.to_payload()).decode("utf-8")

    def to_draft(self) -> ExperimentDraft:
        payload = self.to_payload()
        intent = {key: value for key, value in payload.items() if key not in {"schema", "experiment_id", "revision"}}
        return ExperimentDraft(
            draft_id=self.experiment_id,
            revision=self.revision,
            intent=intent,
            design_schema=self.schema,
        )

    def draft_source_map(self) -> DraftSourceMap:
        locations: dict[str, SourceLocation] = {
            "/intent/study": self.sources.get("study", _generated_source("study")),
            "/intent/recording": self.sources.get("recording", _generated_source("recording")),
        }
        if self.initial_phase is not None:
            locations["/intent/initial_phase"] = self.sources.get(
                "initial_phase",
                _generated_source("initial_phase"),
            )
        for semantic, prefix, payload in (
            ("study", "/intent/study", self.study.to_payload()),
            ("recording", "/intent/recording", self.recording.to_payload()),
        ):
            root_source = locations[prefix]
            for relative in _leaf_paths(payload):
                locations[prefix + relative] = self.source_for_semantic_key(
                    semantic + relative,
                    fallback=root_source,
                )
        categories: tuple[tuple[str, Sequence[Any], str], ...] = (
            ("signals", self.signals, "semantic_id"),
            ("events", self.events, "semantic_id"),
            ("processing", self.processing, "semantic_id"),
            ("windows", self.windows, "semantic_id"),
            ("quality_gates", self.quality_gates, "semantic_id"),
            ("models", self.models, "semantic_id"),
            ("comparisons", self.comparisons, "semantic_id"),
            ("outcomes", self.outcomes, "semantic_id"),
            ("adaptations", self.adaptations, "semantic_id"),
            ("calibrations", self.calibrations, "semantic_id"),
            ("policies", self.policies, "semantic_id"),
            ("actions", self.actions, "semantic_id"),
            ("phases", self.phases, "semantic_id"),
        )
        for category, values, identity in categories:
            for index, value in enumerate(values):
                semantic_id = getattr(value, identity)
                locations[f"/intent/{category}/{index}"] = self.sources.get(
                    semantic_id,
                    _generated_source(semantic_id),
                )
                prefix = f"/intent/{category}/{index}"
                for relative in _leaf_paths(value.to_payload()):
                    locations[prefix + relative] = self.source_for_semantic_key(
                        semantic_id + relative,
                        fallback=locations[prefix],
                    )
        for index, value in enumerate(self.acceptance):
            key = f"acceptance.{value.criterion_id}"
            locations[f"/intent/acceptance/{index}"] = self.sources.get(key, _generated_source(key))
            prefix = f"/intent/acceptance/{index}"
            for relative in _leaf_paths(value.to_payload()):
                locations[prefix + relative] = self.source_for_semantic_key(
                    key + relative,
                    fallback=locations[prefix],
                )
        return DraftSourceMap(locations, fallback=_generated_source(self.experiment_id))

    def origin_for_draft_path(self, path: str) -> AuthoringOrigin:
        semantic_key = _semantic_key_for_draft_path(self, path)
        if semantic_key is None:
            return AuthoringOrigin.AUTHORING_DERIVED
        matches = [
            (candidate, origin)
            for candidate, origin in self.field_origins.items()
            if semantic_key == candidate or semantic_key.startswith(candidate + "/")
        ]
        if matches:
            return max(matches, key=lambda value: len(value[0]))[1]
        if "/" in semantic_key:
            return AuthoringOrigin.AUTHORING_DEFAULT
        semantic = semantic_key.split("/", 1)[0]
        source = self.sources.get(semantic)
        if source is not None and source.kind != SourceKind.GENERATED:
            return AuthoringOrigin.USER_EXPLICIT
        return AuthoringOrigin.AUTHORING_DEFAULT

    def source_for_semantic_key(
        self,
        semantic_key: str,
        *,
        fallback: SourceLocation,
    ) -> SourceLocation:
        matches = [
            (candidate, source)
            for candidate, source in self.sources.items()
            if "/" in candidate
            and (semantic_key == candidate or semantic_key.startswith(candidate + "/"))
        ]
        if matches:
            return max(matches, key=lambda value: len(value[0]))[1]
        return fallback if "/" not in semantic_key else _generated_source(semantic_key)

    def build(self) -> "ComposedExperiment":
        from eegle.authoring.composed_projects import ComposedExperiment
        from eegle.authoring.lowering import lower_experiment_design

        return ComposedExperiment(self, lower_experiment_design(self))

    def _append(
        self,
        field_name: str,
        value: Any,
        semantic_id: str,
        symbol: str,
        *,
        explicit_fields: set[str] | None = None,
        default_fields: set[str] | None = None,
    ) -> "ExperimentDesign":
        values = (*getattr(self, field_name), value)
        sources = dict(self.sources)
        origins = dict(self.field_origins)
        source = SourceLocation(SourceKind.PYTHON, symbol=symbol)
        sources[semantic_id] = source
        payload = value.to_payload()
        explicit = set(payload) if explicit_fields is None else set(explicit_fields)
        defaulted = set() if default_fields is None else set(default_fields)
        for name in explicit:
            key = f"{semantic_id}/{_escape_pointer(name)}"
            sources[key] = source
            origins[key] = AuthoringOrigin.USER_EXPLICIT
        for name in defaulted:
            key = f"{semantic_id}/{_escape_pointer(name)}"
            sources[key] = _generated_source(key)
            origins[key] = AuthoringOrigin.AUTHORING_DEFAULT
        return replace(
            self,
            **{
                field_name: values,
                "sources": sources,
                "field_origins": origins,
            },
        )


def _normalize_optional_arguments(
    required_fields: set[str],
    options: Mapping[str, tuple[Any, Any]],
) -> tuple[dict[str, Any], set[str], set[str]]:
    normalized = {
        name: default if supplied is _UNSET else supplied
        for name, (supplied, default) in options.items()
    }
    explicit = set(required_fields) | {
        name for name, (supplied, _) in options.items() if supplied is not _UNSET
    }
    return normalized, explicit, set(options) - explicit


__all__ = ["ExperimentDesign"]
