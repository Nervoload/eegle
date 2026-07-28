"""Joined authoring/compiler explanations and non-mutating diagnostic guidance."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.authoring import (
    AuthoredExperiment,
    AuthoringOrigin,
    CanonicalArtifact,
    ComposedExperiment,
    DraftLoweringError,
    ScientificMateriality,
    SourceLocation,
)
from eegle.compiler import (
    CompilationDiagnostic,
    CompilationError,
    ExecutionPlan,
    canonical_hash,
    diff_plans,
    explain_plan,
)
from eegle.operations.contracts import (
    ExitCode,
    OperationCategory,
    OperationDiagnostic,
    OperationError,
    OperationIssueSeverity,
    RepairKind,
    RepairOption,
)
from eegle.operations.diagnostics import map_compilation_diagnostics


EXPERIMENT_EXPLANATION_SCHEMA_ID = "eegle.experiment_explanation.v1"
EXPERIMENT_DIFF_SCHEMA_ID = "eegle.experiment_diff.v1"


class ExplanationViewKind(str, Enum):
    SCIENTIFIC_INTENT = "scientific_intent"
    DATAFLOW = "dataflow"
    CAUSALITY = "causality"
    MODEL_COMPARISON = "model_comparison"
    ACTION_INFLUENCE = "action_influence"
    DEFAULTS_PROVENANCE = "defaults_provenance"


class DifferenceImpact(str, Enum):
    SCIENTIFIC = "scientific"
    OPERATIONAL = "operational"
    PRESENTATIONAL = "presentational"
    REPLAY_AFFECTING = "replay_affecting"


_IMPACT_ORDER = {
    DifferenceImpact.SCIENTIFIC: 0,
    DifferenceImpact.OPERATIONAL: 1,
    DifferenceImpact.PRESENTATIONAL: 2,
    DifferenceImpact.REPLAY_AFFECTING: 3,
}


@dataclass(frozen=True, slots=True)
class ExplanationView:
    kind: ExplanationViewKind
    title: str
    summary: str
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ExplanationViewKind(self.kind))
        if not self.title.strip():
            raise ValueError("explanation view title cannot be empty")
        if not self.summary.strip():
            raise ValueError("explanation view summary cannot be empty")
        object.__setattr__(self, "details", freeze_json(self.details))

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "title": self.title,
            "summary": self.summary,
            "details": thaw_json(self.details),
        }


@dataclass(frozen=True, slots=True)
class ExperimentExplanation:
    draft_id: str
    draft_revision: int
    template: Mapping[str, Any]
    canonical_hashes: Mapping[str, str]
    views: tuple[ExplanationView, ...]
    compiler_projection: Mapping[str, Any] | None = None
    diagnostics: tuple[OperationDiagnostic, ...] = ()
    schema: str = EXPERIMENT_EXPLANATION_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != EXPERIMENT_EXPLANATION_SCHEMA_ID:
            raise ValueError(f"unsupported experiment explanation schema: {self.schema}")
        object.__setattr__(self, "draft_id", require_identifier(self.draft_id, "draft_id"))
        revision = int(self.draft_revision)
        if revision <= 0:
            raise ValueError("draft_revision must be positive")
        object.__setattr__(self, "draft_revision", revision)
        object.__setattr__(self, "template", freeze_json(self.template))
        hashes = {
            require_identifier(str(name), "canonical artifact"): require_digest(
                str(value), f"{name} canonical hash"
            )
            for name, value in self.canonical_hashes.items()
        }
        object.__setattr__(self, "canonical_hashes", freeze_json(hashes))
        views = tuple(self.views)
        if {value.kind for value in views} != set(ExplanationViewKind):
            raise ValueError("experiment explanation must contain every required view")
        if len(views) != len(ExplanationViewKind):
            raise ValueError("experiment explanation view kinds must be unique")
        object.__setattr__(
            self,
            "views",
            tuple(sorted(views, key=lambda value: value.kind.value)),
        )
        projection = self.compiler_projection
        object.__setattr__(
            self,
            "compiler_projection",
            None if projection is None else freeze_json(projection),
        )
        diagnostics = tuple(self.diagnostics)
        if not all(isinstance(value, OperationDiagnostic) for value in diagnostics):
            raise TypeError("explanation diagnostics must be OperationDiagnostic values")
        object.__setattr__(
            self,
            "diagnostics",
            tuple(
                sorted(
                    diagnostics,
                    key=lambda value: (value.path or "", value.code, value.message),
                )
            ),
        )

    def view(self, kind: ExplanationViewKind) -> ExplanationView:
        selected = ExplanationViewKind(kind)
        return next(value for value in self.views if value.kind == selected)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "draft_id": self.draft_id,
            "draft_revision": self.draft_revision,
            "template": thaw_json(self.template),
            "canonical_hashes": thaw_json(self.canonical_hashes),
            "views": [value.to_payload() for value in self.views],
            "compiler_projection": None
            if self.compiler_projection is None
            else thaw_json(self.compiler_projection),
            "diagnostics": [value.to_payload() for value in self.diagnostics],
        }


@dataclass(frozen=True, slots=True)
class ExperimentChange:
    path: str
    impacts: tuple[DifferenceImpact, ...]
    summary: str
    before: Any
    after: Any

    def __post_init__(self) -> None:
        if not self.path.startswith("$"):
            raise ValueError("experiment change path must start at '$'")
        impacts = tuple(
            sorted(
                {DifferenceImpact(value) for value in self.impacts},
                key=lambda value: _IMPACT_ORDER[value],
            )
        )
        if not impacts:
            raise ValueError("experiment change requires at least one impact")
        object.__setattr__(self, "impacts", impacts)
        if not self.summary.strip():
            raise ValueError("experiment change summary cannot be empty")
        object.__setattr__(self, "before", freeze_json(self.before))
        object.__setattr__(self, "after", freeze_json(self.after))

    def to_payload(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "impacts": [value.value for value in self.impacts],
            "summary": self.summary,
            "before": thaw_json(self.before),
            "after": thaw_json(self.after),
        }


@dataclass(frozen=True, slots=True)
class ExperimentDiff:
    before_authored_hash: str
    after_authored_hash: str
    changes: tuple[ExperimentChange, ...]
    schema: str = EXPERIMENT_DIFF_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != EXPERIMENT_DIFF_SCHEMA_ID:
            raise ValueError(f"unsupported experiment diff schema: {self.schema}")
        object.__setattr__(
            self,
            "before_authored_hash",
            require_digest(self.before_authored_hash, "before authored hash"),
        )
        object.__setattr__(
            self,
            "after_authored_hash",
            require_digest(self.after_authored_hash, "after authored hash"),
        )
        object.__setattr__(
            self,
            "changes",
            tuple(sorted(self.changes, key=lambda value: value.path)),
        )

    @property
    def equivalent(self) -> bool:
        return not self.changes

    @property
    def scientifically_equivalent(self) -> bool:
        return not any(
            DifferenceImpact.SCIENTIFIC in value.impacts for value in self.changes
        )

    @property
    def replay_equivalent(self) -> bool:
        return not any(
            DifferenceImpact.REPLAY_AFFECTING in value.impacts
            for value in self.changes
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "before_authored_hash": self.before_authored_hash,
            "after_authored_hash": self.after_authored_hash,
            "equivalent": self.equivalent,
            "scientifically_equivalent": self.scientifically_equivalent,
            "replay_equivalent": self.replay_equivalent,
            "changes": [value.to_payload() for value in self.changes],
        }


def explain_authored_experiment(
    authored: AuthoredExperiment,
    *,
    plan: ExecutionPlan | None = None,
    diagnostics: Iterable[OperationDiagnostic] = (),
) -> ExperimentExplanation:
    """Explain canonical intent and an optional matching locked plan."""

    if not isinstance(authored, AuthoredExperiment):
        raise TypeError("experiment explanation requires an AuthoredExperiment")
    projection: Mapping[str, Any] | None = None
    if plan is not None:
        if not isinstance(plan, ExecutionPlan):
            raise TypeError("compiler explanation requires an ExecutionPlan")
        _require_matching_plan(authored, plan)
        projection = explain_plan(plan).to_payload()
    protocol = authored.protocol.to_payload()
    suite = authored.suite.to_payload()
    views = (
        _scientific_view(protocol, suite),
        _dataflow_view(suite, projection),
        _causality_view(protocol, suite),
        _comparison_view(suite, authored.requirements.to_payload(), projection),
        _action_view(suite, authored.requirements.to_payload(), projection),
        _provenance_view(authored, projection),
    )
    return ExperimentExplanation(
        draft_id=authored.draft.draft_id,
        draft_revision=authored.draft.revision,
        template={
            "template_id": authored.expansion.template.template_id,
            "version": authored.expansion.template.version,
            "manifest_digest": authored.expansion.template.manifest_digest,
            "expansion_digest": authored.expansion.expansion_digest,
        },
        canonical_hashes={
            "protocol": authored.protocol.spec_hash,
            "suite": authored.suite.spec_hash,
            "deployment_requirements": authored.requirements.requirements_hash,
        },
        views=views,
        compiler_projection=projection,
        diagnostics=tuple(diagnostics),
    )


def explain_composed_experiment(
    authored: ComposedExperiment,
    *,
    plan: ExecutionPlan | None = None,
    diagnostics: Iterable[OperationDiagnostic] = (),
) -> ExperimentExplanation:
    """Explain a named compositional design and optional matching plan."""

    if not isinstance(authored, ComposedExperiment):
        raise TypeError("composed explanation requires a ComposedExperiment")
    projection: Mapping[str, Any] | None = None
    if plan is not None:
        if not isinstance(plan, ExecutionPlan):
            raise TypeError("compiler explanation requires an ExecutionPlan")
        _require_matching_plan(authored, plan)
        projection = explain_plan(plan).to_payload()
    protocol = authored.protocol.to_payload()
    suite = authored.suite.to_payload()
    views = (
        _scientific_view(protocol, suite),
        _dataflow_view(suite, projection),
        _causality_view(protocol, suite),
        _comparison_view(suite, authored.requirements.to_payload(), projection),
        _action_view(suite, authored.requirements.to_payload(), projection),
        _provenance_view(authored, projection),
    )
    return ExperimentExplanation(
        draft_id=authored.design.experiment_id,
        draft_revision=authored.design.revision,
        template={
            "kind": "composed_design",
            "schema": authored.design.schema,
            "design_digest": authored.design.design_digest,
        },
        canonical_hashes={
            "protocol": authored.protocol.spec_hash,
            "suite": authored.suite.spec_hash,
            "deployment_requirements": authored.requirements.requirements_hash,
        },
        views=views,
        compiler_projection=projection,
        diagnostics=tuple(diagnostics),
    )


def diff_authored_experiments(
    before: AuthoredExperiment,
    after: AuthoredExperiment,
    *,
    before_plan: ExecutionPlan | None = None,
    after_plan: ExecutionPlan | None = None,
) -> ExperimentDiff:
    """Classify source, canonical, operational, and locked-plan differences."""

    if not isinstance(before, AuthoredExperiment) or not isinstance(
        after, AuthoredExperiment
    ):
        raise TypeError("experiment diff requires AuthoredExperiment values")
    if (before_plan is None) != (after_plan is None):
        raise ValueError("experiment plan diff requires both plans or neither plan")
    changes: list[ExperimentChange] = []
    _append_hash_change(
        changes,
        "$.protocol",
        before.protocol.spec_hash,
        after.protocol.spec_hash,
        (DifferenceImpact.SCIENTIFIC, DifferenceImpact.REPLAY_AFFECTING),
        "Scientific protocol changed.",
    )
    _append_hash_change(
        changes,
        "$.suite",
        before.suite.spec_hash,
        after.suite.spec_hash,
        (DifferenceImpact.SCIENTIFIC, DifferenceImpact.REPLAY_AFFECTING),
        "Portable suite changed.",
    )
    _append_hash_change(
        changes,
        "$.deployment_requirements",
        before.requirements.requirements_hash,
        after.requirements.requirements_hash,
        (DifferenceImpact.OPERATIONAL,),
        "Portable deployment requirements changed.",
    )
    before_provenance = before.provenance.to_payload()
    after_provenance = after.provenance.to_payload()
    if before_provenance != after_provenance:
        changes.append(
            ExperimentChange(
                "$.authoring_provenance",
                (DifferenceImpact.PRESENTATIONAL,),
                "Authoring origins or source locations changed without becoming canonical values.",
                canonical_hash(before_provenance),
                canonical_hash(after_provenance),
            )
        )
    if before_plan is not None and after_plan is not None:
        _require_matching_plan(before, before_plan)
        _require_matching_plan(after, after_plan)
        for change in diff_plans(before_plan, after_plan).changes:
            impact = DifferenceImpact(change.materiality.value)
            changes.append(
                ExperimentChange(
                    f"$.plan{change.path[1:]}",
                    (impact, DifferenceImpact.REPLAY_AFFECTING),
                    f"Locked-plan {impact.value} value changed.",
                    change.before,
                    change.after,
                )
            )
    return ExperimentDiff(
        canonical_hash(before.to_payload()),
        canonical_hash(after.to_payload()),
        tuple(changes),
    )


def guide_compilation_diagnostics(
    diagnostics: Iterable[CompilationDiagnostic],
    authored: AuthoredExperiment,
) -> tuple[OperationDiagnostic, ...]:
    """Attach source-aware guidance without altering compiler diagnostics or input."""

    if not isinstance(authored, AuthoredExperiment):
        raise TypeError("guided diagnostics require an AuthoredExperiment")
    guided: list[OperationDiagnostic] = []
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, CompilationDiagnostic):
            raise TypeError("guided compiler diagnostics require CompilationDiagnostic values")
        mapped = map_compilation_diagnostics(
            (diagnostic,), authored.provenance
        )[0]
        guidance = _diagnostic_guidance(
            diagnostic.code,
            diagnostic.path,
            diagnostic.message,
        )
        source = mapped.source or _related_authoring_source(
            authored,
            diagnostic,
            guidance.family,
        )
        materiality = mapped.scientific_impact
        if materiality == ScientificMateriality.UNKNOWN:
            materiality = guidance.materiality
        guided.append(
            OperationDiagnostic(
                code=diagnostic.code,
                category=OperationCategory.COMPILATION,
                title=guidance.title,
                message=diagnostic.message,
                likely_cause=guidance.likely_cause,
                severity=OperationIssueSeverity(diagnostic.severity.value),
                path=diagnostic.path,
                source=source,
                scientific_impact=materiality,
                repairs=guidance.repairs,
                documentation=guidance.documentation,
                details={
                    **dict(diagnostic.details),
                    **dict(mapped.details),
                    "guidance_family": guidance.family,
                },
            )
        )
    return tuple(
        sorted(guided, key=lambda value: (value.path or "", value.code, value.message))
    )


def diagnose_compilation_failure(
    error: CompilationError,
    authored: AuthoredExperiment,
) -> OperationError:
    if not isinstance(error, CompilationError):
        raise TypeError("compilation failure diagnosis requires CompilationError")
    return OperationError(
        "compile",
        ExitCode.REJECTED,
        guide_compilation_diagnostics(error.diagnostics, authored),
    )


def diagnose_authoring_failure(error: DraftLoweringError) -> OperationError:
    """Convert typed draft issues into the shared actionable diagnostic envelope."""

    if not isinstance(error, DraftLoweringError):
        raise TypeError("authoring failure diagnosis requires DraftLoweringError")
    diagnostics: list[OperationDiagnostic] = []
    for issue in error.issues:
        guidance = _diagnostic_guidance(issue.code, issue.path, issue.message)
        diagnostics.append(
            OperationDiagnostic(
                code=issue.code,
                category=OperationCategory.AUTHORING,
                title=guidance.title,
                message=issue.message,
                likely_cause=guidance.likely_cause,
                path=issue.path,
                source=issue.source,
                scientific_impact=guidance.materiality,
                repairs=guidance.repairs,
                documentation=guidance.documentation,
                details={"guidance_family": guidance.family},
            )
        )
    return OperationError("author", ExitCode.INVALID_INPUT, diagnostics)


@dataclass(frozen=True, slots=True)
class _Guidance:
    family: str
    title: str
    likely_cause: str
    materiality: ScientificMateriality
    repairs: tuple[RepairOption, ...]
    documentation: str


def _diagnostic_guidance(code: str, path: str, message: str) -> _Guidance:
    searchable = f"{code} {path} {message}".lower()
    if "unit" in searchable:
        return _signal_guidance(
            "signal.unit",
            "Signal unit mismatch",
            "The authored signal unit and a component or bound resource contract disagree.",
            "unit",
            path,
        )
    if "rate" in searchable or "hz" in searchable:
        return _signal_guidance(
            "signal.rate",
            "Signal rate mismatch",
            "The authored nominal sample rate is outside a plugin or resource contract.",
            "nominal sample rate",
            path,
        )
    if "channel" in searchable:
        return _signal_guidance(
            "signal.channels",
            "Signal channel mismatch",
            "The authored channel count or channel identities do not satisfy the selected contract.",
            "channel contract",
            path,
        )
    if code.startswith("clock."):
        return _Guidance(
            "clock",
            "Clock mapping is incomplete or incompatible",
            "The portable stream clock and deployment execution clock are not joined by a causal-compatible mapping.",
            ScientificMateriality.SCIENTIFIC,
            (
                _repair(
                    "review.clock_mapping",
                    "Review or add an explicit clock mapping",
                    "Confirm source, target, strategy, and uncertainty before recompiling.",
                    path,
                    changes_scientific_semantics=True,
                ),
            ),
            "docs/EEGLE.md#5-time-clocks-and-causality",
        )
    if code.startswith("plugin."):
        return _Guidance(
            "plugin",
            "Plugin resolution or capability failed",
            "The selected plugin is unavailable, version-incompatible, the wrong kind, or lacks a required capability/configuration.",
            ScientificMateriality.SCIENTIFIC,
            (
                _repair(
                    "review.plugin_binding",
                    "Install or select a compatible exact plugin",
                    "Review the plugin ID, version constraint, kind, capabilities, and configuration.",
                    path,
                    changes_scientific_semantics=True,
                ),
            ),
            "docs/EEGLE.md#7-component-and-plugin-model",
        )
    if code.startswith("model.") or code.startswith("adaptation."):
        return _Guidance(
            "model",
            "Model contract or binding failed",
            "The model manifest, implementation, ports, preprocessing lineage, role, or artifact binding does not match the authored model use.",
            ScientificMateriality.SCIENTIFIC,
            (
                _repair(
                    "review.model_binding",
                    "Select a compatible manifest and model implementation",
                    "Review the exact manifest digest, model role, preprocessing lineage, artifacts, and implementation contract.",
                    path,
                    changes_scientific_semantics=True,
                ),
            ),
            "docs/EEGLE.md#9-models-outcomes-calibration-and-adaptation",
        )
    if code.startswith("authorization.") or code.startswith("action."):
        return _Guidance(
            "authorization",
            "Action authorization failed closed",
            "The authored action capability lacks a matching independent deployment provider or bounded grant.",
            ScientificMateriality.OPERATIONAL,
            (
                _repair(
                    "review.authorization",
                    "Create or correct a reviewed deployment authorization proposal",
                    "Match the declared capability to an independent provider and explicit parameter/timing bounds; device presence is not authorization.",
                    path,
                    changes_scientific_semantics=False,
                ),
            ),
            "docs/EEGLE.md#10-actions-and-device-boundaries",
        )
    return _Guidance(
        "general",
        code.replace("_", " ").replace(".", " ").title(),
        "A canonical authoring, compiler, plugin, or deployment constraint was not satisfied.",
        ScientificMateriality.UNKNOWN,
        (
            _repair(
                "review.diagnostic_path",
                "Review the reported value and its owning contract",
                "Inspect the canonical path and mapped authoring source before making a change.",
                path,
                changes_scientific_semantics=True,
            ),
        ),
        "docs/PHASE5_COMPILER.md#4-semantic-validation-passes",
    )


def _signal_guidance(
    family: str,
    title: str,
    likely_cause: str,
    field: str,
    path: str,
) -> _Guidance:
    return _Guidance(
        family,
        title,
        likely_cause,
        ScientificMateriality.SCIENTIFIC,
        (
            _repair(
                f"review.{family.replace('.', '_')}",
                f"Align the authored and deployed {field}",
                "Inspect the authored signal contract and select a compatible resource or explicitly revise scientific intent.",
                path,
                changes_scientific_semantics=True,
            ),
        ),
        "docs/EEGLE.md#6-modality-neutral-data-plane",
    )


def _repair(
    repair_id: str,
    title: str,
    description: str,
    path: str | None,
    *,
    changes_scientific_semantics: bool,
) -> RepairOption:
    return RepairOption(
        repair_id=repair_id,
        title=title,
        kind=RepairKind.MANUAL,
        changes_scientific_semantics=changes_scientific_semantics,
        description=description,
        proposal={
            "operation": "review_and_propose",
            "target_path": path,
            "applied": False,
        },
    )


def _scientific_view(
    protocol: Mapping[str, Any],
    suite: Mapping[str, Any],
) -> ExplanationView:
    claims = tuple(protocol.get("claims", ()))
    metrics = tuple(protocol.get("metrics", ()))
    acceptance = tuple(protocol.get("acceptance", ()))
    mode = str(protocol["execution_mode"])
    return ExplanationView(
        ExplanationViewKind.SCIENTIFIC_INTENT,
        "Scientific intent",
        f"{mode.capitalize()} protocol with {len(claims)} claim(s), {len(metrics)} metric(s), and {len(acceptance)} acceptance criterion/criteria.",
        {
            "execution_mode": mode,
            "claims": list(claims),
            "metrics": list(metrics),
            "acceptance": list(acceptance),
            "recording": suite.get("recording", {}),
            "validation": suite.get("validation", {}),
        },
    )


def _dataflow_view(
    suite: Mapping[str, Any],
    projection: Mapping[str, Any] | None,
) -> ExplanationView:
    components = tuple(suite.get("components", ()))
    routes = tuple(suite.get("routes", ()))
    streams = tuple(suite.get("streams", ()))
    phases = tuple(suite.get("phases", ()))
    order = (
        list(projection["component_order"])
        if projection is not None
        else _portable_component_order(components, routes)
    )
    return ExplanationView(
        ExplanationViewKind.DATAFLOW,
        "Dataflow",
        f"{len(streams)} stream(s) traverse {len(routes)} route(s) across {len(components)} component(s) in {len(phases)} phase(s).",
        {
            "streams": list(streams),
            "components": list(components),
            "routes": list(routes),
            "phases": list(phases),
            "component_order": order,
            "order_authority": "locked_plan" if projection is not None else "portable_suite",
        },
    )


def _causality_view(
    protocol: Mapping[str, Any],
    suite: Mapping[str, Any],
) -> ExplanationView:
    clock_policy = dict(suite.get("clock_policy") or {})
    streams = tuple(suite.get("streams", ()))
    scheduled = tuple(suite.get("scheduled_triggers", ()))
    state = tuple(suite.get("state_triggers", ()))
    mode = str(protocol["execution_mode"])
    return ExplanationView(
        ExplanationViewKind.CAUSALITY,
        "Causality and time",
        f"{mode.capitalize()} execution orders work by {clock_policy.get('ordering', 'an explicit clock policy')} with {len(scheduled)} scheduled and {len(state)} state trigger(s).",
        {
            "execution_mode": mode,
            "clock_policy": clock_policy,
            "stream_clocks": [
                {"stream_id": value["stream_id"], "clock_id": value.get("clock_id")}
                for value in streams
            ],
            "scheduled_triggers": list(scheduled),
            "state_triggers": list(state),
            "phase_transitions": [
                {
                    "phase_id": phase["phase_id"],
                    "transitions": phase.get("transitions", []),
                    "timeout_seconds": phase.get("timeout_seconds"),
                }
                for phase in suite.get("phases", ())
            ],
        },
    )


def _comparison_view(
    suite: Mapping[str, Any],
    requirements: Mapping[str, Any],
    projection: Mapping[str, Any] | None,
) -> ExplanationView:
    uses = tuple(suite.get("model_uses", ()))
    groups: dict[str, list[Mapping[str, Any]]] = {}
    ungrouped: list[Mapping[str, Any]] = []
    for use in uses:
        group = use.get("comparison_group")
        if group is None:
            ungrouped.append(use)
        else:
            groups.setdefault(str(group), []).append(use)
    manifest_needs = [
        value
        for value in requirements.get("requirements", ())
        if value.get("kind") == "model_artifact"
    ]
    summary = (
        f"{len(groups)} comparison group(s) cover {sum(len(value) for value in groups.values())} role-bound model use(s)."
        if groups
        else f"No equivalence-bearing model comparison is declared; {len(ungrouped)} ungrouped model use(s) remain."
    )
    return ExplanationView(
        ExplanationViewKind.MODEL_COMPARISON,
        "Model comparison",
        summary,
        {
            "roles": list(suite.get("model_roles", ())),
            "groups": {
                key: sorted(value, key=lambda item: str(item.get("component_id", "")))
                for key, value in sorted(groups.items())
            },
            "ungrouped_uses": ungrouped,
            "model_artifact_requirements": manifest_needs,
            "locked_model_bindings": []
            if projection is None
            else projection.get("model_bindings", []),
        },
    )


def _action_view(
    suite: Mapping[str, Any],
    requirements: Mapping[str, Any],
    projection: Mapping[str, Any] | None,
) -> ExplanationView:
    components = tuple(suite.get("components", ()))
    policies = [value for value in components if value.get("kind") == "policy"]
    actuators = [value for value in components if value.get("kind") == "actuator"]
    authorization_needs = [
        value
        for value in requirements.get("requirements", ())
        if value.get("kind") == "authorization"
    ]
    grants = [] if projection is None else list(projection.get("action_grants", ()))
    providers = (
        []
        if projection is None
        else list(projection.get("authorization_providers", ()))
    )
    if not policies and not actuators:
        summary = "No policy or actuator can influence the environment."
    elif not grants:
        summary = "Actions are observe-only until an independent deployment provider and bounded grant compile successfully."
    else:
        summary = f"{len(grants)} locked grant(s) authorize {len(actuators)} actuator component(s) through {len(providers)} provider(s)."
    return ExplanationView(
        ExplanationViewKind.ACTION_INFLUENCE,
        "Action influence",
        summary,
        {
            "policies": policies,
            "actuators": actuators,
            "authorization_requirements": authorization_needs,
            "authorization_providers": providers,
            "action_grants": grants,
            "observe_only": bool(policies or actuators) and not grants,
        },
    )


def _provenance_view(
    authored: AuthoredExperiment | ComposedExperiment,
    projection: Mapping[str, Any] | None,
) -> ExplanationView:
    entries = authored.provenance.entries
    origins = Counter(value.origin.value for value in entries)
    confirmations = Counter(value.confirmation.value for value in entries)
    materiality = Counter(value.materiality.value for value in entries)
    compiler_derived: list[Mapping[str, Any]] = []
    if projection is not None:
        for field in (
            "component_order",
            "components",
            "placements",
            "artifacts",
            "model_bindings",
            "authorization_providers",
            "action_grants",
        ):
            compiler_derived.append(
                {
                    "field": field,
                    "origin": "compiler_derived",
                    "source": None,
                    "value": projection.get(field),
                }
            )
    return ExplanationView(
        ExplanationViewKind.DEFAULTS_PROVENANCE,
        "Defaults and provenance",
        f"{len(entries)} authoring attribution(s) remain separate from {len(compiler_derived)} compiler-derived projection field(s).",
        {
            "origin_counts": dict(sorted(origins.items())),
            "confirmation_counts": dict(sorted(confirmations.items())),
            "materiality_counts": dict(sorted(materiality.items())),
            "authoring_entries": [value.to_payload() for value in entries],
            "compiler_derived": compiler_derived,
            "compiler_values_are_authoring_origins": False,
            "template_default_origin": AuthoringOrigin.TEMPLATE_DEFAULT.value,
        },
    )


def _portable_component_order(
    components: Iterable[Mapping[str, Any]],
    routes: Iterable[Mapping[str, Any]],
) -> list[str]:
    identities = [str(value["component_id"]) for value in components]
    edges: dict[str, set[str]] = {value: set() for value in identities}
    indegree = {value: 0 for value in identities}
    for route in routes:
        source = str(route["source"]["component"])
        target = str(route["target"]["component"])
        if target not in edges.get(source, set()):
            edges.setdefault(source, set()).add(target)
            indegree[target] = indegree.get(target, 0) + 1
    ready = sorted(value for value, degree in indegree.items() if degree == 0)
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for target in sorted(edges.get(current, ())):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    return ordered


def _require_matching_plan(
    authored: AuthoredExperiment | ComposedExperiment,
    plan: ExecutionPlan,
) -> None:
    expected = {
        "protocol": authored.protocol.spec_hash,
        "suite": authored.suite.spec_hash,
    }
    observed = {key: plan.spec_hashes.get(key) for key in expected}
    if observed != expected:
        raise ValueError("locked plan specification hashes do not match authored experiment")


def _append_hash_change(
    changes: list[ExperimentChange],
    path: str,
    before: str,
    after: str,
    impacts: tuple[DifferenceImpact, ...],
    summary: str,
) -> None:
    if before != after:
        changes.append(ExperimentChange(path, impacts, summary, before, after))


def _related_authoring_source(
    authored: AuthoredExperiment,
    diagnostic: CompilationDiagnostic,
    family: str,
) -> SourceLocation | None:
    suite = authored.suite.to_payload()
    pointer: str | None = None
    if family.startswith("signal."):
        stream_id = diagnostic.details.get("stream_id")
        leaf = {
            "signal.unit": "unit",
            "signal.rate": "nominal_rate_hz",
            "signal.channels": "channel_count",
        }[family]
        for index, stream in enumerate(suite.get("streams", ())):
            if stream_id is None or stream.get("stream_id") == stream_id:
                pointer = f"/streams/{index}/contract/{leaf}"
                break
    elif family == "clock":
        stream_id = diagnostic.details.get("stream_id")
        for index, stream in enumerate(suite.get("streams", ())):
            if stream_id is not None and stream.get("stream_id") == stream_id:
                pointer = f"/streams/{index}/clock_id"
                break
        pointer = pointer or "/clock_policy"
    elif family == "plugin":
        component_id = diagnostic.details.get("component_id")
        plugin_id = diagnostic.details.get("plugin_id")
        for index, component in enumerate(suite.get("components", ())):
            if component_id is not None and component.get("component_id") != component_id:
                continue
            if plugin_id is not None and component.get("plugin_id") != plugin_id:
                continue
            pointer = f"/components/{index}/plugin_id"
            break
    elif family == "model":
        if suite.get("model_uses"):
            pointer = "/model_uses/0/manifest_digest"
    elif family == "authorization":
        candidates = [
            value
            for value in authored.provenance.entries
            if value.target_artifact == CanonicalArtifact.SUITE
            and "action_capabilities" in value.target_path
        ]
        if candidates:
            return candidates[0].source
    if pointer is None:
        return None
    entry = authored.provenance.entry_for(CanonicalArtifact.SUITE, pointer)
    return None if entry is None else entry.source
