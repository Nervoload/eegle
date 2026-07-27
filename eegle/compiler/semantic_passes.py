"""Independent semantic validation passes for suite compilation."""

from __future__ import annotations

import math
from typing import Any, Mapping

from eegle._domain import ComponentKind
from eegle.compiler.diagnostics import (
    CompilationDiagnostic,
    DiagnosticSeverity,
)
from eegle.compiler.plan import PlannedModelBinding
from eegle.plugins.registry import PluginDescriptor
from eegle.specs.deployment import DeploymentSpec
from eegle.specs.protocol import ProtocolSpec
from eegle.specs.suite import ComponentSpec, SuiteSpec
from eegle.runtime.outcomes import OutcomeUse


def validate_phases(
    suite: SuiteSpec,
    components: Mapping[str, ComponentSpec],
    resolved: Mapping[str, PluginDescriptor],
    diagnostics: list[CompilationDiagnostic],
) -> None:
    phases = {value.phase_id: value for value in suite.phases}
    incoming: dict[tuple[str, str], list[Any]] = {}
    for route in suite.routes:
        incoming.setdefault((route.target_component, route.target_port), []).append(route)
    for index, phase in enumerate(suite.phases):
        path = f"$.suite.phases[{index}]"
        active = set(phase.components)
        for component_id in phase.components:
            if component_id not in components:
                diagnostics.append(
                    _error(
                        "phase.component_reference",
                        f"{path}.components",
                        f"unknown component {component_id}",
                    )
                )
                continue
            descriptor = resolved.get(component_id)
            if descriptor is None:
                continue
            for port in descriptor.input_ports:
                if not port.required:
                    continue
                routes = incoming.get((component_id, port.name), ())
                if routes and not any(route.source_component in active for route in routes):
                    diagnostics.append(
                        _error(
                            "phase.inactive_dependency",
                            f"{path}.components",
                            f"active component {component_id} has no active upstream route "
                            f"for required input {port.name}",
                        )
                    )
        if phase.retry_limit and phase.resume_policy.value == "forbidden":
            diagnostics.append(
                _error(
                    "phase.retry_forbidden",
                    f"{path}.retry_limit",
                    "retry_limit must be zero when resume_policy is forbidden",
                )
            )
        transition_keys: set[tuple[str, str]] = set()
        automatic_conditions: dict[str, int] = {}
        for transition_index, transition in enumerate(phase.transitions):
            key = (transition.condition.value, transition.target_phase)
            if key in transition_keys:
                diagnostics.append(
                    _error(
                        "phase.duplicate_transition",
                        f"{path}.transitions[{transition_index}]",
                        "duplicate phase transition",
                    )
                )
            transition_keys.add(key)
            if transition.condition.value != "operator":
                automatic_conditions[transition.condition.value] = (
                    automatic_conditions.get(transition.condition.value, 0) + 1
                )
            if transition.target_phase not in phases:
                diagnostics.append(
                    _error(
                        "phase.transition_reference",
                        f"{path}.transitions[{transition_index}].target_phase",
                        f"unknown phase {transition.target_phase}",
                    )
                )
        for condition, count in sorted(automatic_conditions.items()):
            if count > 1:
                diagnostics.append(
                    _error(
                        "phase.ambiguous_transition",
                        f"{path}.transitions",
                        f"phase has {count} automatic {condition} transitions",
                    )
                )
    reachable: set[str] = set()
    pending = [suite.initial_phase]
    while pending:
        phase_id = pending.pop(0)
        if phase_id in reachable or phase_id not in phases:
            continue
        reachable.add(phase_id)
        pending.extend(value.target_phase for value in phases[phase_id].transitions)
    for phase_id in sorted(set(phases) - reachable):
        diagnostics.append(
            _error(
                "phase.unreachable",
                "$.suite.phases",
                f"phase {phase_id} is unreachable from {suite.initial_phase}",
            )
        )
    if reachable and not any(not phases[value].transitions for value in reachable):
        diagnostics.append(
            _error(
                "phase.no_terminal",
                "$.suite.phases",
                "no terminal phase is reachable from initial_phase",
            )
        )


def validate_artifacts(
    suite: SuiteSpec,
    resolved: Mapping[str, PluginDescriptor],
    diagnostics: list[CompilationDiagnostic],
) -> None:
    """Validate publication endpoints and phase-entry dependency dominance."""

    declarations = {value.artifact_id: value for value in suite.artifacts}
    required_ids = {
        artifact_id
        for phase in suite.phases
        for artifact_id in phase.required_artifacts
    }
    phases = {value.phase_id: value for value in suite.phases}
    phase_index = {value.phase_id: index for index, value in enumerate(suite.phases)}
    components = {value.component_id: value for value in suite.components}

    for index, phase in enumerate(suite.phases):
        for artifact_id in phase.required_artifacts:
            if artifact_id not in declarations:
                diagnostics.append(
                    _error(
                        "artifact.undeclared",
                        f"$.suite.phases[{index}].required_artifacts",
                        f"required artifact {artifact_id} is not declared",
                    )
                )

    for index, artifact in enumerate(suite.artifacts):
        if artifact.external:
            if artifact.artifact_id in required_ids and artifact.expected_digest is None:
                diagnostics.append(
                    _error(
                        "artifact.digest_required",
                        f"$.suite.artifacts[{index}].expected_digest",
                        f"required external artifact {artifact.artifact_id} must lock its digest",
                    )
                )
            continue
        path = f"$.suite.artifacts[{index}]"
        phase = phases.get(str(artifact.producer_phase))
        if phase is None:
            diagnostics.append(
                _error(
                    "artifact.producer_phase",
                    f"{path}.producer_phase",
                    f"unknown producer phase {artifact.producer_phase}",
                )
            )
        component = components.get(str(artifact.producer_component))
        if component is None:
            diagnostics.append(
                _error(
                    "artifact.producer_component",
                    f"{path}.producer_component",
                    f"unknown producer component {artifact.producer_component}",
                )
            )
            continue
        if phase is not None and component.component_id not in phase.components:
            diagnostics.append(
                _error(
                    "artifact.producer_inactive",
                    f"{path}.producer_component",
                    f"component {component.component_id} is not active in phase {phase.phase_id}",
                )
            )
        descriptor = resolved.get(component.component_id)
        port = None if descriptor is None else next(
            (
                value
                for value in descriptor.output_ports
                if value.name == artifact.producer_port
            ),
            None,
        )
        if port is None:
            diagnostics.append(
                _error(
                    "artifact.producer_port",
                    f"{path}.producer_port",
                    f"component {component.component_id} has no artifact output port "
                    f"{artifact.producer_port}",
                )
            )
        elif port.type_id != "eegle.artifact_publication.v1":
            diagnostics.append(
                _error(
                    "artifact.producer_type",
                    f"{path}.producer_port",
                    "producer port type must be eegle.artifact_publication.v1, observed "
                    f"{port.type_id}",
                )
            )

    predecessors: dict[str, set[str]] = {phase_id: set() for phase_id in phases}
    for phase in suite.phases:
        for transition in phase.transitions:
            if transition.target_phase in predecessors:
                predecessors[transition.target_phase].add(phase.phase_id)
    all_phases = set(phases)
    dominators = {
        phase_id: ({phase_id} if phase_id == suite.initial_phase else set(all_phases))
        for phase_id in phases
    }
    changed = True
    while changed:
        changed = False
        for phase_id in phases:
            if phase_id == suite.initial_phase:
                continue
            parents = predecessors[phase_id]
            common = (
                set.intersection(*(dominators[parent] for parent in parents))
                if parents
                else set()
            )
            updated = {phase_id, *common}
            if updated != dominators[phase_id]:
                dominators[phase_id] = updated
                changed = True
    for consumer in suite.phases:
        for artifact_id in consumer.required_artifacts:
            declaration = declarations.get(artifact_id)
            if declaration is None or declaration.external:
                continue
            producer_phase = str(declaration.producer_phase)
            if (
                producer_phase not in dominators[consumer.phase_id]
                or producer_phase == consumer.phase_id
            ):
                diagnostics.append(
                    _error(
                        "artifact.not_guaranteed",
                        f"$.suite.phases[{phase_index[consumer.phase_id]}].required_artifacts",
                        f"artifact {artifact_id} produced by phase {producer_phase} is not "
                        f"guaranteed before phase {consumer.phase_id}",
                    )
                )


def validate_roles_and_actions(
    suite: SuiteSpec,
    deployment: DeploymentSpec,
    diagnostics: list[CompilationDiagnostic],
    model_bindings: tuple[PlannedModelBinding, ...] = (),
) -> None:
    bound_models = {value.component_id: value for value in model_bindings}
    components = {value.component_id: value for value in suite.components}
    incoming_models: dict[str, set[str]] = {}
    for route in suite.routes:
        source = components.get(route.source_component)
        target = components.get(route.target_component)
        if (
            source is not None
            and target is not None
            and source.kind == ComponentKind.MODEL
            and target.kind == ComponentKind.POLICY
        ):
            incoming_models.setdefault(target.component_id, set()).add(source.component_id)
    for phase_index, phase in enumerate(suite.phases):
        active = set(phase.components)
        active_policies = [
            value
            for value in suite.components
            if value.kind == ComponentKind.POLICY and value.component_id in active
        ]
        for policy in active_policies:
            routed_models = [
                components[component_id]
                for component_id in incoming_models.get(policy.component_id, set())
                if component_id in active
            ]
            if not routed_models:
                continue
            bound_routed = [
                bound_models[value.component_id]
                for value in routed_models
                if value.component_id in bound_models
            ]
            if len(bound_routed) != len(routed_models):
                continue
            authorized = [
                value for value in bound_routed if value.role.may_feed_policy
            ]
            if len(authorized) != 1 or len(bound_routed) != len(authorized):
                diagnostics.append(
                    _error(
                        "role.policy_feeder",
                        f"$.suite.phases[{phase_index}].components",
                        f"policy {policy.component_id} requires exactly one routed model "
                        "whose compiled role may feed policy",
                    )
                )
    component_ids = {value.component_id for value in suite.components}
    for index, permission in enumerate(deployment.permissions):
        for component_id in permission.component_ids:
            if component_id not in component_ids:
                diagnostics.append(
                    _error(
                        "action.permission_reference",
                        f"$.deployment.permissions[{index}].component_ids",
                        f"permission references unknown component {component_id}",
                    )
                )


def validate_triggers_and_permissions(
    protocol: ProtocolSpec,
    suite: SuiteSpec,
    deployment: DeploymentSpec,
    resolved: Mapping[str, PluginDescriptor],
    diagnostics: list[CompilationDiagnostic],
) -> None:
    """Validate active trigger endpoints and explicit data/action authority."""

    components = {value.component_id: value for value in suite.components}
    phases = {value.phase_id: value for value in suite.phases}
    criterion_ids = {value.criterion_id for value in protocol.acceptance}
    grants = {
        (component_id, permission.capability)
        for permission in deployment.permissions
        for component_id in permission.component_ids
    }

    for phase_index, phase in enumerate(suite.phases):
        for criterion in phase.acceptance_criteria:
            if criterion not in criterion_ids:
                diagnostics.append(
                    _error(
                        "phase.acceptance_reference",
                        f"$.suite.phases[{phase_index}].acceptance_criteria",
                        f"unknown protocol acceptance criterion {criterion}",
                    )
                )

    for index, trigger in enumerate(suite.scheduled_triggers):
        path = f"$.suite.scheduled_triggers[{index}]"
        phase = phases.get(trigger.phase_id)
        _validate_trigger_target(
            path,
            phase,
            trigger.target_component,
            components,
            resolved,
            diagnostics,
        )

    for index, rule in enumerate(suite.state_triggers):
        path = f"$.suite.state_triggers[{index}]"
        phase = phases.get(rule.phase_id)
        _validate_trigger_target(
            path,
            phase,
            rule.target_component,
            components,
            resolved,
            diagnostics,
        )
        if phase is not None and rule.source_component not in phase.components:
            diagnostics.append(
                _error(
                    "trigger.source_inactive",
                    f"{path}.source_component",
                    f"state trigger source {rule.source_component} is not active in phase {phase.phase_id}",
                )
            )
        source = resolved.get(rule.source_component)
        if source is not None and not any(
            port.type_id == "eegle.state_transition.v1" for port in source.output_ports
        ):
            diagnostics.append(
                _error(
                    "trigger.source_type",
                    f"{path}.source_component",
                    "state trigger source must emit eegle.state_transition.v1",
                )
            )

    incoming_outcomes: dict[str, set[str]] = {}
    for route in suite.routes:
        source = components.get(route.source_component)
        if source is not None and source.kind == ComponentKind.OUTCOME:
            incoming_outcomes.setdefault(route.target_component, set()).add(
                source.component_id
            )
    allowed_uses = {value.value for value in OutcomeUse}
    for index, component in enumerate(suite.components):
        path = f"$.suite.components[{index}]"
        unknown_uses = sorted(set(component.outcome_uses) - allowed_uses)
        if unknown_uses:
            diagnostics.append(
                _error(
                    "outcome.use",
                    f"{path}.outcome_uses",
                    f"unknown outcome uses: {', '.join(unknown_uses)}",
                )
            )
        if component.outcome_uses and component.kind != ComponentKind.OUTCOME:
            diagnostics.append(
                _error(
                    "outcome.use_kind",
                    f"{path}.outcome_uses",
                    "only outcome resolvers may declare produced outcome uses",
                )
            )
        required = component.required_outcome_use
        if required is not None:
            if component.kind not in {ComponentKind.ADAPTER, ComponentKind.POLICY}:
                diagnostics.append(
                    _error(
                        "outcome.required_use_kind",
                        f"{path}.required_outcome_use",
                        "only adapters and policies may declare a required outcome use",
                    )
                )
            if required not in allowed_uses:
                diagnostics.append(
                    _error(
                        "outcome.required_use",
                        f"{path}.required_outcome_use",
                        f"unknown required outcome use {required}",
                    )
                )
            producers = incoming_outcomes.get(component.component_id, set())
            if not producers:
                diagnostics.append(
                    _error(
                        "outcome.route",
                        f"{path}.required_outcome_use",
                        "component requires outcomes but has no routed outcome resolver",
                    )
                )
            for producer_id in sorted(producers):
                producer = components[producer_id]
                if required not in producer.outcome_uses:
                    diagnostics.append(
                        _error(
                            "outcome.permission",
                            f"{path}.required_outcome_use",
                            f"outcome resolver {producer_id} does not permit {required}",
                        )
                    )
            if required == OutcomeUse.ADAPTATION.value and (
                component.component_id,
                OutcomeUse.ADAPTATION.value,
            ) not in grants:
                diagnostics.append(
                    _error(
                        "adaptation.authorization",
                        path,
                        "adaptive component requires an independent adaptation permission grant",
                    )
                )
        if component.kind == ComponentKind.ACTUATOR:
            if not component.action_capabilities:
                diagnostics.append(
                    _error(
                        "action.capability",
                        f"{path}.action_capabilities",
                        "actuator must declare at least one action capability",
                    )
                )
        elif component.action_capabilities:
            diagnostics.append(
                _error(
                    "action.capability_kind",
                    f"{path}.action_capabilities",
                    "only actuator components may declare action capabilities",
                )
            )

def _validate_trigger_target(
    path: str,
    phase: Any,
    target_component: str,
    components: Mapping[str, ComponentSpec],
    resolved: Mapping[str, PluginDescriptor],
    diagnostics: list[CompilationDiagnostic],
) -> None:
    if phase is None:
        diagnostics.append(
            _error("trigger.phase_reference", f"{path}.phase_id", "unknown trigger phase")
        )
        return
    if target_component not in components:
        diagnostics.append(
            _error(
                "trigger.target_reference",
                f"{path}.target_component",
                f"unknown component {target_component}",
            )
        )
        return
    if target_component not in phase.components:
        diagnostics.append(
            _error(
                "trigger.target_inactive",
                f"{path}.target_component",
                f"trigger target {target_component} is not active in phase {phase.phase_id}",
            )
        )
    descriptor = resolved.get(target_component)
    if descriptor is not None and not descriptor.capabilities.supports_triggers:
        diagnostics.append(
            _error(
                "trigger.unsupported",
                f"{path}.target_component",
                f"component {target_component} does not declare trigger support",
            )
        )


def validate_runtime_policy(
    suite: SuiteSpec,
    diagnostics: list[CompilationDiagnostic],
) -> None:
    """Validate scheduling policy before a plan can construct hardware."""

    validation = suite.validation
    for key in (
        "max_graph_events",
        "max_pending_events",
        "max_idle_cycles",
        "max_phase_transitions",
    ):
        if key not in validation:
            continue
        raw_value = validation[key]
        if not isinstance(raw_value, int) or isinstance(raw_value, bool) or raw_value <= 0:
            diagnostics.append(
                _error(
                    "runtime.limit",
                    f"$.suite.validation.{key}",
                    f"{key} must be a positive integer",
                )
            )
    deadlines = validation.get("component_deadlines_seconds", {})
    if not isinstance(deadlines, Mapping):
        diagnostics.append(
            _error(
                "runtime.deadlines",
                "$.suite.validation.component_deadlines_seconds",
                "component deadlines must be an object keyed by component ID",
            )
        )
        return
    component_ids = {value.component_id for value in suite.components}
    for component_id, raw_value in deadlines.items():
        path = f"$.suite.validation.component_deadlines_seconds.{component_id}"
        if component_id not in component_ids:
            diagnostics.append(
                _error(
                    "runtime.deadline_component",
                    path,
                    f"deadline references unknown component {component_id}",
                )
            )
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            value = float("nan")
        if not math.isfinite(value) or value < 0:
            diagnostics.append(
                _error(
                    "runtime.deadline_value",
                    path,
                    "component deadline must be finite and non-negative",
                )
            )


def _error(
    code: str,
    path: str,
    message: str,
    **details: Any,
) -> CompilationDiagnostic:
    return CompilationDiagnostic(
        code=code,
        severity=DiagnosticSeverity.ERROR,
        path=path,
        message=message,
        details=details,
    )
