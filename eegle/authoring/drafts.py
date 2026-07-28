"""Incomplete experiment drafts and deterministic canonical lowering."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._domain import ComponentKind, ExecutionMode
from eegle._validation import (
    freeze_json,
    require_digest,
    require_identifier,
    thaw_json,
)
from eegle.authoring.contracts import (
    AuthoringOrigin,
    CanonicalArtifact,
    ConfirmationState,
    ScientificMateriality,
    SourceKind,
    SourceLocation,
)
from eegle.authoring.provenance import (
    AuthoringProvenance,
    CanonicalTarget,
    DraftSourceMap,
    ProvenanceEntry,
)
from eegle.authoring.schemas import (
    DEPLOYMENT_REQUIREMENTS_SCHEMA_ID,
    EXPERIMENT_DRAFT_SCHEMA_ID,
    validate_deployment_requirements_payload,
    validate_experiment_draft_payload,
)
from eegle.compiler.lock import canonical_hash
from eegle.specs import (
    AcceptanceCriterion,
    ClaimSpec,
    ComparisonOperator,
    ComponentSpec,
    LogicalStreamSpec,
    MetricSpec,
    PhaseSpec,
    ProtocolSpec,
    ResumePolicy,
    RouteSpec,
    SignalContract,
    SuiteSpec,
)
from eegle.specs.protocol import PROTOCOL_SPEC_SCHEMA
from eegle.specs.suite import SUITE_SPEC_SCHEMA


class UnresolvedKind(str, Enum):
    REQUIRED = "required"
    CHOICE = "choice"
    DETECTION = "detection"


class DeploymentRequirementKind(str, Enum):
    SOURCE_BINDING = "source_binding"
    STORAGE = "storage"
    CLOCK_MAPPING = "clock_mapping"
    MODEL_ARTIFACT = "model_artifact"
    AUTHORIZATION = "authorization"


@dataclass(frozen=True, slots=True)
class UnresolvedChoice:
    path: str
    kind: UnresolvedKind
    prompt: str
    options: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if self.path and not self.path.startswith("/"):
            raise ValueError("unresolved path must be a JSON pointer")
        object.__setattr__(self, "kind", UnresolvedKind(self.kind))
        if not self.prompt.strip():
            raise ValueError("unresolved prompt cannot be empty")
        frozen = freeze_json(list(self.options))
        object.__setattr__(self, "options", tuple(frozen))

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": self.path,
            "kind": self.kind.value,
            "prompt": self.prompt,
        }
        if self.options:
            payload["options"] = thaw_json(self.options)
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "UnresolvedChoice":
        return cls(
            path=str(payload["path"]),
            kind=UnresolvedKind(str(payload["kind"])),
            prompt=str(payload["prompt"]),
            options=tuple(payload.get("options", ())),
        )


@dataclass(frozen=True, slots=True)
class TemplateSelection:
    """An exact, immutable template revision selected by an authoring draft."""

    template_id: str
    version: str
    manifest_digest: str
    parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "template_id",
            require_identifier(self.template_id, "template_id"),
        )
        if not self.version.strip():
            raise ValueError("template version cannot be empty")
        object.__setattr__(
            self,
            "manifest_digest",
            require_digest(self.manifest_digest, "template manifest_digest"),
        )
        object.__setattr__(self, "parameters", freeze_json(self.parameters))

    def to_payload(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "manifest_digest": self.manifest_digest,
            "parameters": thaw_json(self.parameters),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TemplateSelection":
        return cls(
            template_id=str(payload["template_id"]),
            version=str(payload["version"]),
            manifest_digest=str(payload["manifest_digest"]),
            parameters=dict(payload["parameters"]),
        )


@dataclass(frozen=True, slots=True)
class ExperimentDraft:
    """Versioned, non-executable and potentially incomplete authoring intent."""

    draft_id: str
    revision: int
    intent: Mapping[str, Any]
    unresolved: tuple[UnresolvedChoice, ...] = ()
    template: TemplateSelection | None = None
    schema: str = EXPERIMENT_DRAFT_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != EXPERIMENT_DRAFT_SCHEMA_ID:
            raise ValueError(f"unsupported experiment draft schema: {self.schema}")
        object.__setattr__(self, "draft_id", require_identifier(self.draft_id, "draft_id"))
        revision = int(self.revision)
        if revision <= 0:
            raise ValueError("draft revision must be positive")
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "intent", freeze_json(self.intent))
        unresolved = tuple(
            sorted(self.unresolved, key=lambda value: (value.path, value.kind.value))
        )
        if len({value.path for value in unresolved}) != len(unresolved):
            raise ValueError("unresolved paths must be unique")
        object.__setattr__(self, "unresolved", unresolved)
        if self.template is not None and not isinstance(
            self.template,
            TemplateSelection,
        ):
            raise TypeError("draft template must be a TemplateSelection")
        validate_experiment_draft_payload(self.to_payload())

    @property
    def complete(self) -> bool:
        return not self.unresolved

    @property
    def draft_digest(self) -> str:
        return canonical_hash(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "draft_id": self.draft_id,
            "revision": self.revision,
            "intent": thaw_json(self.intent),
            "template": (
                None if self.template is None else self.template.to_payload()
            ),
            "unresolved": [value.to_payload() for value in self.unresolved],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ExperimentDraft":
        validate_experiment_draft_payload(payload)
        return cls(
            schema=str(payload["schema"]),
            draft_id=str(payload["draft_id"]),
            revision=int(payload["revision"]),
            intent=dict(payload["intent"]),
            template=(
                None
                if payload.get("template") is None
                else TemplateSelection.from_payload(payload["template"])
            ),
            unresolved=tuple(
                UnresolvedChoice.from_payload(value)
                for value in payload.get("unresolved", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class DraftIssue:
    code: str
    path: str
    message: str
    source: SourceLocation

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", require_identifier(self.code, "draft issue code"))
        if self.path and not self.path.startswith("/"):
            raise ValueError("draft issue path must be a JSON pointer")
        if not self.message.strip():
            raise ValueError("draft issue message cannot be empty")
        if not isinstance(self.source, SourceLocation):
            raise TypeError("draft issue source must be a SourceLocation")


class DraftLoweringError(ValueError):
    def __init__(self, issues: tuple[DraftIssue, ...]) -> None:
        self.issues = tuple(sorted(issues, key=lambda value: (value.path, value.code)))
        if not self.issues:
            raise ValueError("DraftLoweringError requires at least one issue")
        super().__init__(
            "; ".join(
                f"{value.code} at {value.path or '/'}: {value.message}"
                for value in self.issues
            )
        )


@dataclass(frozen=True, slots=True)
class DeploymentRequirement:
    requirement_id: str
    kind: DeploymentRequirementKind
    component_id: str | None = None
    stream_id: str | None = None
    contract: Mapping[str, Any] | None = None
    source_clock: str | None = None
    target_clock: str | None = None
    storage_kind: str | None = None
    manifest_digest: str | None = None
    action_capability: str | None = None
    required_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "requirement_id",
            require_identifier(self.requirement_id, "requirement_id"),
        )
        object.__setattr__(self, "kind", DeploymentRequirementKind(self.kind))
        for field in (
            "component_id",
            "stream_id",
            "source_clock",
            "target_clock",
            "storage_kind",
            "action_capability",
        ):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, require_identifier(value, field))
        capabilities = tuple(
            sorted(
                require_identifier(value, "required capability")
                for value in self.required_capabilities
            )
        )
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("deployment requirement capabilities must be unique")
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(
            self,
            "contract",
            None if self.contract is None else freeze_json(self.contract),
        )
        if self.manifest_digest is not None:
            object.__setattr__(
                self,
                "manifest_digest",
                require_digest(self.manifest_digest, "manifest_digest"),
            )
        if self.kind == DeploymentRequirementKind.SOURCE_BINDING:
            if self.component_id is None or self.stream_id is None or self.contract is None:
                raise ValueError("source binding requirements need component, stream, and contract")
        elif self.kind == DeploymentRequirementKind.CLOCK_MAPPING:
            if self.source_clock is None or self.target_clock is None:
                raise ValueError("clock mapping requirements need source and target clocks")
        elif self.kind == DeploymentRequirementKind.STORAGE:
            if self.storage_kind is None:
                raise ValueError("storage requirements need storage_kind")
        elif self.kind == DeploymentRequirementKind.MODEL_ARTIFACT:
            if self.component_id is None or self.manifest_digest is None:
                raise ValueError(
                    "model artifact requirements need component and manifest digest"
                )
        elif self.kind == DeploymentRequirementKind.AUTHORIZATION:
            if self.component_id is None or self.action_capability is None:
                raise ValueError(
                    "authorization requirements need component and action capability"
                )

    def to_payload(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "kind": self.kind.value,
            "component_id": self.component_id,
            "stream_id": self.stream_id,
            "contract": None if self.contract is None else thaw_json(self.contract),
            "source_clock": self.source_clock,
            "target_clock": self.target_clock,
            "storage_kind": self.storage_kind,
            "manifest_digest": self.manifest_digest,
            "action_capability": self.action_capability,
            "required_capabilities": list(self.required_capabilities),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DeploymentRequirement":
        return cls(
            requirement_id=str(payload["requirement_id"]),
            kind=DeploymentRequirementKind(str(payload["kind"])),
            component_id=(
                None if payload.get("component_id") is None else str(payload["component_id"])
            ),
            stream_id=None if payload.get("stream_id") is None else str(payload["stream_id"]),
            contract=None if payload.get("contract") is None else dict(payload["contract"]),
            source_clock=(
                None if payload.get("source_clock") is None else str(payload["source_clock"])
            ),
            target_clock=(
                None if payload.get("target_clock") is None else str(payload["target_clock"])
            ),
            storage_kind=(
                None if payload.get("storage_kind") is None else str(payload["storage_kind"])
            ),
            manifest_digest=(
                None
                if payload.get("manifest_digest") is None
                else str(payload["manifest_digest"])
            ),
            action_capability=(
                None
                if payload.get("action_capability") is None
                else str(payload["action_capability"])
            ),
            required_capabilities=tuple(
                str(value) for value in payload.get("required_capabilities", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class DeploymentRequirements:
    suite_id: str
    requirements: tuple[DeploymentRequirement, ...]
    schema: str = DEPLOYMENT_REQUIREMENTS_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != DEPLOYMENT_REQUIREMENTS_SCHEMA_ID:
            raise ValueError(f"unsupported deployment requirements schema: {self.schema}")
        object.__setattr__(self, "suite_id", require_identifier(self.suite_id, "suite_id"))
        requirements = tuple(sorted(self.requirements, key=lambda value: value.requirement_id))
        if len({value.requirement_id for value in requirements}) != len(requirements):
            raise ValueError("deployment requirement identities must be unique")
        object.__setattr__(self, "requirements", requirements)
        validate_deployment_requirements_payload(self.to_payload())

    @property
    def requirements_hash(self) -> str:
        return canonical_hash(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "suite_id": self.suite_id,
            "requirements": [value.to_payload() for value in self.requirements],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DeploymentRequirements":
        validate_deployment_requirements_payload(payload)
        return cls(
            schema=str(payload["schema"]),
            suite_id=str(payload["suite_id"]),
            requirements=tuple(
                DeploymentRequirement.from_payload(value)
                for value in payload["requirements"]
            ),
        )


@dataclass(frozen=True, slots=True)
class LoweredExperiment:
    protocol: ProtocolSpec
    suite: SuiteSpec
    deployment_requirements: DeploymentRequirements
    provenance: AuthoringProvenance

    def __post_init__(self) -> None:
        if self.suite.protocol_id != self.protocol.protocol_id:
            raise ValueError("lowered suite must reference the lowered protocol")
        if self.deployment_requirements.suite_id != self.suite.suite_id:
            raise ValueError("deployment requirements must reference the lowered suite")


def lower_experiment_draft(
    draft: ExperimentDraft,
    *,
    source_map: DraftSourceMap | None = None,
) -> LoweredExperiment:
    """Lower complete intent while presenting invalid values as draft issues."""

    if not isinstance(draft, ExperimentDraft):
        raise TypeError("lowering requires an ExperimentDraft")
    if source_map is not None and not isinstance(source_map, DraftSourceMap):
        raise TypeError("source_map must be a DraftSourceMap")
    try:
        return _lower_recording_draft(draft, source_map=source_map)
    except DraftLoweringError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        sources = source_map or DraftSourceMap({})
        raise DraftLoweringError(
            (
                DraftIssue(
                    code="authoring.invalid_value",
                    path="/intent",
                    message=str(exc),
                    source=sources.source_for("/intent"),
                ),
            )
        ) from exc


def _lower_recording_draft(
    draft: ExperimentDraft,
    *,
    source_map: DraftSourceMap | None = None,
) -> LoweredExperiment:
    """Lower complete recording intent; never compile or construct runtime values."""

    if not isinstance(draft, ExperimentDraft):
        raise TypeError("lowering requires an ExperimentDraft")
    sources = source_map or DraftSourceMap({})
    if draft.unresolved:
        raise DraftLoweringError(
            tuple(
                DraftIssue(
                    code=f"authoring.unresolved_{value.kind.value}",
                    path=value.path,
                    message=value.prompt,
                    source=sources.source_for(value.path),
                )
                for value in draft.unresolved
            )
        )

    if draft.template is not None:
        from eegle.authoring.templates import _lower_template_draft

        return _lower_template_draft(draft, sources)

    intent = thaw_json(draft.intent)
    issues = _validate_recording_intent(intent, sources)
    if issues:
        raise DraftLoweringError(tuple(issues))

    study = intent["study"]
    signal_values = [
        (index, value) for index, value in enumerate(intent["signals"])
    ]
    signal_values.sort(key=lambda item: str(item[1]["signal_id"]))
    protocol_id = str(study.get("protocol_id") or f"protocol.{draft.draft_id}")
    suite_id = str(study.get("suite_id") or f"suite.{draft.draft_id}")

    claims = tuple(
        ClaimSpec(claim_id=str(value["claim_id"]), statement=str(value["statement"]))
        for value in sorted(study["claims"], key=lambda item: str(item["claim_id"]))
    )
    metrics, criteria = _acceptance_values(intent.get("acceptance") or {})
    protocol = ProtocolSpec(
        protocol_id=protocol_id,
        execution_mode=ExecutionMode(str(study["execution_mode"])),
        claims=claims,
        metrics=metrics,
        acceptance=criteria,
        annotations=dict(study.get("annotations") or {}),
    )

    streams: list[LogicalStreamSpec] = []
    components: list[ComponentSpec] = []
    routes: list[RouteSpec] = []
    requirements: list[DeploymentRequirement] = []
    entries: list[ProvenanceEntry] = []
    execution_clock = str(study["execution_clock_id"])

    _protocol_provenance(entries, protocol, sources, intent)
    for canonical_index, (draft_index, signal) in enumerate(signal_values):
        signal_id = str(signal["signal_id"])
        stream_id = f"stream.{signal_id}"
        source_id = f"source.{signal_id}"
        sink_id = f"sink.{signal_id}"
        sparse = str(signal["content_kind"]) == "sparse_events"
        contract = _signal_contract(signal)
        streams.append(
            LogicalStreamSpec(
                stream_id=stream_id,
                contract=contract,
                modality=str(signal["modality"]),
                clock_id=str(signal["clock_id"]),
            )
        )
        components.extend(
            (
                ComponentSpec(
                    component_id=source_id,
                    kind=ComponentKind.SOURCE,
                    stream_id=stream_id,
                ),
                ComponentSpec(
                    component_id=sink_id,
                    kind=ComponentKind.SINK,
                    plugin_id=(
                        "eegle.recording.sparse_sink"
                        if sparse
                        else "eegle.recording.dense_sink"
                    ),
                    version_spec="~=0.1.0",
                ),
            )
        )
        routes.append(
            RouteSpec(
                route_id=f"route.{signal_id}.record",
                source_component=source_id,
                source_port="events" if sparse else "samples",
                target_component=sink_id,
                target_port="records",
            )
        )
        requirements.append(
            DeploymentRequirement(
                requirement_id=f"requirement.source.{signal_id}",
                kind=DeploymentRequirementKind.SOURCE_BINDING,
                component_id=source_id,
                stream_id=stream_id,
                contract=contract.to_payload(),
                required_capabilities=tuple(signal.get("required_capabilities") or ()),
            )
        )
        signal_clock = str(signal["clock_id"])
        if signal_clock != execution_clock:
            requirements.append(
                DeploymentRequirement(
                    requirement_id=f"requirement.clock.{signal_id}",
                    kind=DeploymentRequirementKind.CLOCK_MAPPING,
                    source_clock=signal_clock,
                    target_clock=execution_clock,
                )
            )
        _signal_provenance(
            entries,
            canonical_index=canonical_index,
            draft_index=draft_index,
            signal=signal,
            sources=sources,
        )

    phase_value = (intent.get("phases") or [{}])[0]
    phase_id = str(phase_value.get("phase_id") or "phase.record")
    phase = PhaseSpec(
        phase_id=phase_id,
        components=tuple(sorted(value.component_id for value in components)),
        retry_limit=int(phase_value.get("retry_limit", 0)),
        resume_policy=ResumePolicy(str(phase_value.get("resume_policy", "checkpoint"))),
        operator_confirmation=bool(phase_value.get("operator_confirmation", False)),
        timeout_seconds=(
            None
            if phase_value.get("timeout_seconds") is None
            else float(phase_value["timeout_seconds"])
        ),
        acceptance_criteria=tuple(value.criterion_id for value in criteria),
    )
    recording = {
        "execution_capture": True,
        "semantic_evidence": True,
        "raw_recording": "reference",
        **dict(intent.get("recording") or {}),
    }
    suite = SuiteSpec(
        suite_id=suite_id,
        protocol_id=protocol_id,
        streams=tuple(streams),
        components=tuple(sorted(components, key=lambda value: value.component_id)),
        routes=tuple(sorted(routes, key=lambda value: value.route_id)),
        phases=(phase,),
        initial_phase=phase_id,
        clock_policy={
            "execution_clock_id": execution_clock,
            "ordering": "availability_watermark",
        },
        recording=recording,
        validation={"require_replay_equivalence": True},
    )
    requirements.append(
        DeploymentRequirement(
            requirement_id="requirement.storage.evidence",
            kind=DeploymentRequirementKind.STORAGE,
            storage_kind="evidence",
        )
    )
    deployment_requirements = DeploymentRequirements(
        suite_id=suite.suite_id,
        requirements=tuple(requirements),
    )
    _suite_structural_provenance(entries, suite, sources, intent)
    provenance = AuthoringProvenance(
        draft_id=draft.draft_id,
        draft_revision=draft.revision,
        draft_digest=draft.draft_digest,
        canonical_targets={
            CanonicalArtifact.PROTOCOL: CanonicalTarget(
                schema=PROTOCOL_SPEC_SCHEMA,
                digest=protocol.spec_hash,
            ),
            CanonicalArtifact.SUITE: CanonicalTarget(
                schema=SUITE_SPEC_SCHEMA,
                digest=suite.spec_hash,
            ),
        },
        entries=tuple(entries),
    )
    return LoweredExperiment(
        protocol=protocol,
        suite=suite,
        deployment_requirements=deployment_requirements,
        provenance=provenance,
    )


_STUDY_FIELDS = {
    "protocol_id",
    "suite_id",
    "execution_mode",
    "execution_clock_id",
    "claims",
    "annotations",
}
_SIGNAL_FIELDS = {
    "signal_id",
    "modality",
    "content_kind",
    "unit",
    "channel_count",
    "channel_ids",
    "nominal_rate_hz",
    "clock_id",
    "event_kinds",
    "required_capabilities",
}
_RECORDING_FIELDS = {"execution_capture", "semantic_evidence", "raw_recording"}
_PHASE_FIELDS = {
    "phase_id",
    "retry_limit",
    "resume_policy",
    "operator_confirmation",
    "timeout_seconds",
}


def _validate_recording_intent(
    intent: Mapping[str, Any],
    sources: DraftSourceMap,
) -> list[DraftIssue]:
    issues: list[DraftIssue] = []

    def issue(code: str, path: str, message: str) -> None:
        issues.append(DraftIssue(code, path, message, sources.source_for(path)))

    for section in ("events", "processing", "windows", "models", "outcomes", "actions"):
        if intent.get(section):
            issue(
                "authoring.recording_scope",
                f"/intent/{section}",
                f"{section} intent is not part of the recording draft lowerer",
            )
    if intent.get("deployment_requirements"):
        issue(
            "authoring.deployment_binding_scope",
            "/intent/deployment_requirements",
            "site requirement overrides arrive with deployment generation in P7-008",
        )
    study = intent.get("study")
    if not isinstance(study, Mapping):
        issue("authoring.required", "/intent/study", "study intent is required")
    else:
        _unknown_fields(study, _STUDY_FIELDS, "/intent/study", issue)
        for field in ("execution_mode", "execution_clock_id", "claims"):
            if not study.get(field):
                issue(
                    "authoring.required",
                    f"/intent/study/{field}",
                    f"study {field} is required before lowering",
                )
        try:
            if study.get("execution_mode"):
                ExecutionMode(str(study["execution_mode"]))
        except ValueError:
            issue(
                "authoring.execution_mode",
                "/intent/study/execution_mode",
                "execution_mode must be causal, retrospective, or oracle",
            )
        claims = study.get("claims")
        if claims and not isinstance(claims, list):
            issue("authoring.type", "/intent/study/claims", "claims must be a list")
        elif isinstance(claims, list):
            for index, value in enumerate(claims):
                path = f"/intent/study/claims/{index}"
                if not isinstance(value, Mapping):
                    issue("authoring.type", path, "each claim must be an object")
                elif set(value) - {"claim_id", "statement"}:
                    issue("authoring.field", path, "claims only accept claim_id and statement")
                elif not value.get("claim_id") or not value.get("statement"):
                    issue("authoring.required", path, "claim_id and statement are required")

    signals = intent.get("signals")
    if not isinstance(signals, list) or not signals:
        issue("authoring.required", "/intent/signals", "at least one signal is required")
    else:
        identities: list[str] = []
        for index, signal in enumerate(signals):
            path = f"/intent/signals/{index}"
            if not isinstance(signal, Mapping):
                issue("authoring.type", path, "each signal must be an object")
                continue
            _unknown_fields(signal, _SIGNAL_FIELDS, path, issue)
            for field in ("signal_id", "modality", "content_kind", "clock_id"):
                if not signal.get(field):
                    issue("authoring.required", f"{path}/{field}", f"signal {field} is required")
            signal_id = signal.get("signal_id")
            if signal_id:
                identities.append(str(signal_id))
            kind = signal.get("content_kind")
            if kind not in (None, "dense_samples", "sparse_events"):
                issue(
                    "authoring.content_kind",
                    f"{path}/content_kind",
                    "content_kind must be dense_samples or sparse_events",
                )
            if kind == "dense_samples":
                for field in ("unit", "channel_count", "nominal_rate_hz"):
                    if signal.get(field) is None:
                        issue(
                            "authoring.required",
                            f"{path}/{field}",
                            f"dense signal {field} is required",
                        )
        duplicates = sorted({value for value in identities if identities.count(value) > 1})
        for value in duplicates:
            issue(
                "authoring.duplicate_signal",
                "/intent/signals",
                f"signal_id {value} is declared more than once",
            )

    recording = intent.get("recording") or {}
    if not isinstance(recording, Mapping):
        issue("authoring.type", "/intent/recording", "recording must be an object")
    else:
        _unknown_fields(recording, _RECORDING_FIELDS, "/intent/recording", issue)
    phases = intent.get("phases") or []
    if not isinstance(phases, list):
        issue("authoring.type", "/intent/phases", "phases must be a list")
    elif len(phases) > 1:
        issue(
            "authoring.recording_phase_count",
            "/intent/phases",
            "the recording lowerer accepts zero or one phase",
        )
    elif phases:
        if not isinstance(phases[0], Mapping):
            issue("authoring.type", "/intent/phases/0", "phase must be an object")
        else:
            _unknown_fields(phases[0], _PHASE_FIELDS, "/intent/phases/0", issue)
    acceptance = intent.get("acceptance") or {}
    if not isinstance(acceptance, Mapping):
        issue(
            "authoring.type",
            "/intent/acceptance",
            "recording acceptance must be an object with metrics and criteria",
        )
    else:
        _validate_acceptance(acceptance, issue)
    return issues


def _unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    path: str,
    issue: Any,
) -> None:
    for field in sorted(set(value) - allowed):
        issue("authoring.field", f"{path}/{field}", f"unsupported authoring field: {field}")


def _validate_acceptance(value: Mapping[str, Any], issue: Any) -> None:
    _unknown_fields(value, {"metrics", "criteria"}, "/intent/acceptance", issue)
    for collection, fields in (
        ("metrics", {"metric_id", "measure", "parameters"}),
        ("criteria", {"criterion_id", "metric_id", "operator", "value"}),
    ):
        items = value.get(collection, [])
        if not isinstance(items, list):
            issue(
                "authoring.type",
                f"/intent/acceptance/{collection}",
                f"{collection} must be a list",
            )
            continue
        for index, item in enumerate(items):
            path = f"/intent/acceptance/{collection}/{index}"
            if not isinstance(item, Mapping):
                issue("authoring.type", path, f"each {collection} value must be an object")
                continue
            _unknown_fields(item, fields, path, issue)
            required = fields - ({"parameters"} if collection == "metrics" else set())
            for field in required:
                if field not in item or item[field] in (None, ""):
                    issue("authoring.required", f"{path}/{field}", f"{field} is required")


def _acceptance_values(
    value: Mapping[str, Any],
) -> tuple[tuple[MetricSpec, ...], tuple[AcceptanceCriterion, ...]]:
    metrics = tuple(
        MetricSpec(
            metric_id=str(item["metric_id"]),
            measure=str(item["measure"]),
            parameters=dict(item.get("parameters") or {}),
        )
        for item in sorted(value.get("metrics", ()), key=lambda item: str(item["metric_id"]))
    )
    criteria = tuple(
        AcceptanceCriterion(
            criterion_id=str(item["criterion_id"]),
            metric_id=str(item["metric_id"]),
            operator=ComparisonOperator(str(item["operator"])),
            value=item["value"],
        )
        for item in sorted(
            value.get("criteria", ()), key=lambda item: str(item["criterion_id"])
        )
    )
    return metrics, criteria


def _signal_contract(signal: Mapping[str, Any]) -> SignalContract:
    sparse = str(signal["content_kind"]) == "sparse_events"
    return SignalContract(
        type_id=(
            "eegle.sparse_event_batch.v1"
            if sparse
            else "eegle.dense_sample_batch.v1"
        ),
        unit=None if sparse else str(signal["unit"]),
        channel_count=None if sparse else int(signal["channel_count"]),
        nominal_rate_hz=None if sparse else float(signal["nominal_rate_hz"]),
        content_kind=str(signal["content_kind"]),
        rate_model="event" if sparse else "regular",
        channel_ids=tuple(str(value) for value in signal.get("channel_ids", ())),
        event_kinds=tuple(str(value) for value in signal.get("event_kinds", ())),
    )


def _explicit_entry(
    artifact: CanonicalArtifact,
    target_path: str,
    draft_path: str,
    sources: DraftSourceMap,
    materiality: ScientificMateriality,
) -> ProvenanceEntry:
    return ProvenanceEntry(
        target_artifact=artifact,
        target_path=target_path,
        origin=AuthoringOrigin.USER_EXPLICIT,
        source=sources.source_for(draft_path),
        materiality=materiality,
    )


def _default_entry(
    artifact: CanonicalArtifact,
    target_path: str,
    materiality: ScientificMateriality,
) -> ProvenanceEntry:
    return ProvenanceEntry(
        target_artifact=artifact,
        target_path=target_path,
        origin=AuthoringOrigin.AUTHORING_DEFAULT,
        source=SourceLocation(
            kind=SourceKind.GENERATED,
            symbol="eegle.authoring.recording_v1",
        ),
        materiality=materiality,
        confirmation=ConfirmationState.NOT_REQUIRED,
    )


def _protocol_provenance(
    entries: list[ProvenanceEntry],
    protocol: ProtocolSpec,
    sources: DraftSourceMap,
    intent: Mapping[str, Any],
) -> None:
    study = intent["study"]
    if study.get("protocol_id"):
        entries.append(
            _explicit_entry(
                CanonicalArtifact.PROTOCOL,
                "/protocol_id",
                "/intent/study/protocol_id",
                sources,
                ScientificMateriality.OPERATIONAL,
            )
        )
    else:
        entries.append(
            _default_entry(
                CanonicalArtifact.PROTOCOL,
                "/protocol_id",
                ScientificMateriality.OPERATIONAL,
            )
        )
    entries.append(
        _explicit_entry(
            CanonicalArtifact.PROTOCOL,
            "/execution_mode",
            "/intent/study/execution_mode",
            sources,
            ScientificMateriality.SCIENTIFIC,
        )
    )
    claims_by_id = {
        str(value["claim_id"]): index for index, value in enumerate(study["claims"])
    }
    for index, claim in enumerate(protocol.claims):
        draft_index = claims_by_id[claim.claim_id]
        for field in ("claim_id", "statement"):
            entries.append(
                _explicit_entry(
                    CanonicalArtifact.PROTOCOL,
                    f"/claims/{index}/{field}",
                    f"/intent/study/claims/{draft_index}/{field}",
                    sources,
                    ScientificMateriality.SCIENTIFIC,
                )
            )
    if protocol.metrics:
        entries.append(
            _explicit_entry(
                CanonicalArtifact.PROTOCOL,
                "/metrics",
                "/intent/acceptance/metrics",
                sources,
                ScientificMateriality.SCIENTIFIC,
            )
        )
    if protocol.acceptance:
        entries.append(
            _explicit_entry(
                CanonicalArtifact.PROTOCOL,
                "/acceptance",
                "/intent/acceptance/criteria",
                sources,
                ScientificMateriality.SCIENTIFIC,
            )
        )


def _signal_provenance(
    entries: list[ProvenanceEntry],
    *,
    canonical_index: int,
    draft_index: int,
    signal: Mapping[str, Any],
    sources: DraftSourceMap,
) -> None:
    base = f"/intent/signals/{draft_index}"
    target = f"/streams/{canonical_index}"
    entries.append(
        _explicit_entry(
            CanonicalArtifact.SUITE,
            target,
            base,
            sources,
            ScientificMateriality.SCIENTIFIC,
        )
    )
    for field in ("unit", "channel_count", "nominal_rate_hz", "event_kinds"):
        if field in signal:
            entries.append(
                _explicit_entry(
                    CanonicalArtifact.SUITE,
                    f"{target}/contract/{field}",
                    f"{base}/{field}",
                    sources,
                    ScientificMateriality.SCIENTIFIC,
                )
            )


def _suite_structural_provenance(
    entries: list[ProvenanceEntry],
    suite: SuiteSpec,
    sources: DraftSourceMap,
    intent: Mapping[str, Any],
) -> None:
    study = intent["study"]
    if study.get("suite_id"):
        entries.append(
            _explicit_entry(
                CanonicalArtifact.SUITE,
                "/suite_id",
                "/intent/study/suite_id",
                sources,
                ScientificMateriality.OPERATIONAL,
            )
        )
    else:
        entries.append(
            _default_entry(
                CanonicalArtifact.SUITE,
                "/suite_id",
                ScientificMateriality.OPERATIONAL,
            )
        )
    entries.extend(
        (
            _default_entry(
                CanonicalArtifact.SUITE,
                "/components",
                ScientificMateriality.OPERATIONAL,
            ),
            _default_entry(
                CanonicalArtifact.SUITE,
                "/routes",
                ScientificMateriality.OPERATIONAL,
            ),
            _default_entry(
                CanonicalArtifact.SUITE,
                "/clock_policy/ordering",
                ScientificMateriality.SCIENTIFIC,
            ),
            _explicit_entry(
                CanonicalArtifact.SUITE,
                "/clock_policy/execution_clock_id",
                "/intent/study/execution_clock_id",
                sources,
                ScientificMateriality.SCIENTIFIC,
            ),
            _default_entry(
                CanonicalArtifact.SUITE,
                "/validation/require_replay_equivalence",
                ScientificMateriality.SCIENTIFIC,
            ),
        )
    )
    recording = intent.get("recording") or {}
    for field in ("execution_capture", "semantic_evidence", "raw_recording"):
        if field in recording:
            entries.append(
                _explicit_entry(
                    CanonicalArtifact.SUITE,
                    f"/recording/{field}",
                    f"/intent/recording/{field}",
                    sources,
                    ScientificMateriality.OPERATIONAL,
                )
            )
        else:
            entries.append(
                _default_entry(
                    CanonicalArtifact.SUITE,
                    f"/recording/{field}",
                    ScientificMateriality.OPERATIONAL,
                )
            )
    phases = intent.get("phases") or []
    entries.append(
        _explicit_entry(
            CanonicalArtifact.SUITE,
            "/phases/0",
            "/intent/phases/0",
            sources,
            ScientificMateriality.OPERATIONAL,
        )
        if phases
        else _default_entry(
            CanonicalArtifact.SUITE,
            "/phases/0",
            ScientificMateriality.OPERATIONAL,
        )
    )
    if suite.protocol_id == str(study.get("protocol_id") or ""):
        entries.append(
            _explicit_entry(
                CanonicalArtifact.SUITE,
                "/protocol_id",
                "/intent/study/protocol_id",
                sources,
                ScientificMateriality.OPERATIONAL,
            )
        )
    else:
        entries.append(
            _default_entry(
                CanonicalArtifact.SUITE,
                "/protocol_id",
                ScientificMateriality.OPERATIONAL,
            )
        )
