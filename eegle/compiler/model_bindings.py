"""Compilation of portable model intent into exact plan-owned bindings."""

from __future__ import annotations

from typing import Any, Mapping

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from eegle._domain import ComponentKind, EquivalenceLevel
from eegle._validation import thaw_json
from eegle.compiler.diagnostics import CompilationDiagnostic
from eegle.compiler.graph import (
    CompiledGraph,
    CompiledPort,
    PortDirection,
    contract_issues,
)
from eegle.compiler.lock import canonical_hash
from eegle.compiler.plan import (
    PlannedModelArtifactBinding,
    PlannedModelBinding,
    PlannedModelFailureDisposition,
    PlannedModelQueueDisposition,
    PlannedModelRole,
)
from eegle.models.contracts import PreprocessingOwnership
from eegle.models.manifests import ModelManifest
from eegle.models.predictions import PREDICTION_RECORD_SCHEMA
from eegle.plugins.registry import PluginDescriptor
from eegle.specs.deployment import DeploymentSpec
from eegle.specs.protocol import ProtocolSpec
from eegle.specs.suite import (
    ComponentSpec,
    ModelFailureDisposition,
    ModelQueueDisposition,
    ModelRolePermissionsSpec,
    ModelRoleProfile,
    ModelRoleSpec,
    ModelUseSpec,
    SignalContract,
    SuiteSpec,
)


_EQUIVALENCE_STRENGTH = {
    EquivalenceLevel.BITWISE: 0,
    EquivalenceLevel.NUMERIC: 1,
    EquivalenceLevel.SEMANTIC: 2,
    EquivalenceLevel.TRACE: 3,
    EquivalenceLevel.NON_REPLAYABLE: 4,
}


def compile_model_bindings(
    protocol: ProtocolSpec,
    suite: SuiteSpec,
    deployment: DeploymentSpec,
    resolved: Mapping[str, PluginDescriptor],
    graph: CompiledGraph | None,
    manifests: Mapping[str, ModelManifest],
    diagnostics: list[CompilationDiagnostic],
) -> tuple[tuple[PlannedModelBinding, ...], dict[str, str]]:
    """Compile the exact model/plugin/artifact/role join without materializing files."""

    manifest_by_digest: dict[str, ModelManifest] = {}
    for key, manifest in manifests.items():
        path = f"$.model_manifests.{key}"
        if not isinstance(manifest, ModelManifest):
            diagnostics.append(
                _error(
                    "model.manifest_type",
                    path,
                    "model manifest catalog values must be ModelManifest",
                )
            )
            continue
        if str(key) != manifest.manifest_digest:
            diagnostics.append(
                _error(
                    "model.manifest_key",
                    path,
                    "model manifest catalog key must equal the canonical manifest digest",
                    observed=key,
                    expected=manifest.manifest_digest,
                )
            )
            continue
        manifest_by_digest[manifest.manifest_digest] = manifest

    components = {value.component_id: value for value in suite.components}
    uses = {value.component_id: value for value in suite.model_uses}
    custom_roles = {value.role_id: value for value in suite.model_roles}
    artifact_bindings = {
        (value.manifest_digest, value.artifact_id): value
        for value in deployment.model_artifacts
    }
    artifact_indices = {
        (value.manifest_digest, value.artifact_id): index
        for index, value in enumerate(deployment.model_artifacts)
    }
    used_artifact_keys: set[tuple[str, str]] = set()
    role_ids: dict[str, str] = {}
    planned: list[PlannedModelBinding] = []

    if suite.model_uses:
        legacy_scheduling = suite.scheduling
        if (
            not legacy_scheduling.primary_first
            or legacy_scheduling.shadow_queue_limit is not None
            or legacy_scheduling.shadow_failure.value != "fail_run"
        ):
            diagnostics.append(
                _error(
                    "model.legacy_role_scheduling",
                    "$.suite.scheduling",
                    "suite-wide primary/shadow scheduling is not valid for compiled model "
                    "bindings; use a custom model role permission set",
                )
            )
        for component_index, component in enumerate(suite.components):
            if component.kind == ComponentKind.MODEL and component.component_id not in uses:
                diagnostics.append(
                    _error(
                        "model.binding_missing",
                        f"$.suite.components[{component_index}]",
                        "model component is not joined to a manifest and compiled role",
                        component_id=component.component_id,
                    )
                )

    for index, use in enumerate(suite.model_uses):
        path = f"$.suite.model_uses[{index}]"
        component = components.get(use.component_id)
        descriptor = resolved.get(use.component_id)
        if component is None:
            diagnostics.append(
                _error(
                    "model.component_reference",
                    f"{path}.component_id",
                    f"unknown suite component {use.component_id}",
                )
            )
            continue
        if component.kind != ComponentKind.MODEL:
            diagnostics.append(
                _error(
                    "model.component_kind",
                    f"{path}.component_id",
                    f"component {use.component_id} is {component.kind.value}, not model",
                )
            )
            continue
        if descriptor is None or descriptor.kind != ComponentKind.MODEL:
            continue
        if component.role is not None and component.role != use.role_id:
            diagnostics.append(
                _error(
                    "model.role_conflict",
                    f"{path}.role_id",
                    f"model use role {use.role_id} conflicts with component role {component.role}",
                )
            )

        manifest = manifest_by_digest.get(use.manifest_digest)
        if manifest is None:
            diagnostics.append(
                _error(
                    "model.manifest_missing",
                    f"{path}.manifest_digest",
                    "model manifest is not present in the compiler catalog",
                    manifest_digest=use.manifest_digest,
                )
            )
            continue

        permissions = _resolve_model_role(
            use,
            custom_roles,
            path,
            diagnostics,
        )
        if permissions is None:
            continue
        role_ids[component.component_id] = use.role_id

        _validate_model_manifest_implementation(
            protocol,
            manifest,
            descriptor,
            path,
            diagnostics,
        )
        lineage = _validate_model_ports_and_preprocessing(
            component,
            manifest,
            descriptor,
            resolved,
            graph,
            path,
            diagnostics,
        )
        _validate_model_role_routes(
            component,
            permissions,
            components,
            graph,
            path,
            diagnostics,
        )

        materializations: list[PlannedModelArtifactBinding] = []
        for artifact in manifest.artifacts:
            key = (manifest.manifest_digest, artifact.artifact_id)
            binding = artifact_bindings.get(key)
            if binding is None:
                diagnostics.append(
                    _error(
                        "model.artifact_unbound",
                        "$.deployment.model_artifacts",
                        f"artifact {artifact.artifact_id} has no site materialization",
                        component_id=component.component_id,
                        manifest_digest=manifest.manifest_digest,
                    )
                )
                continue
            used_artifact_keys.add(key)
            if binding.digest != artifact.digest:
                binding_index = artifact_indices[key]
                diagnostics.append(
                    _error(
                        "model.artifact_digest",
                        f"$.deployment.model_artifacts[{binding_index}].digest",
                        "deployment artifact digest differs from the model manifest",
                        expected=artifact.digest,
                        observed=binding.digest,
                    )
                )
                continue
            materializations.append(
                PlannedModelArtifactBinding(
                    artifact_id=artifact.artifact_id,
                    digest=artifact.digest,
                    media_type=artifact.media_type,
                    size_bytes=artifact.size_bytes,
                    uri=binding.uri,
                )
            )

        if len(materializations) != len(manifest.artifacts):
            continue
        if permissions.requires_equivalent_inputs and use.comparison_group is None:
            diagnostics.append(
                _error(
                    "model.comparison_group_required",
                    f"{path}.comparison_group",
                    f"role {use.role_id} requires equivalent comparison inputs",
                )
            )
            continue
        planned.append(
            PlannedModelBinding(
                component_id=component.component_id,
                plugin_id=descriptor.plugin_id,
                plugin_version=descriptor.version,
                manifest=manifest,
                role=permissions,
                comparison_group=use.comparison_group,
                artifacts=tuple(materializations),
                preprocessing_lineage=lineage,
            )
        )

    for index, binding in enumerate(deployment.model_artifacts):
        key = (binding.manifest_digest, binding.artifact_id)
        if key not in used_artifact_keys:
            diagnostics.append(
                _error(
                    "model.artifact_unused",
                    f"$.deployment.model_artifacts[{index}]",
                    "deployment model artifact is not required by a suite model use",
                )
            )

    if graph is not None:
        _validate_comparison_groups(planned, graph, suite, diagnostics)
    return tuple(planned), role_ids


def _resolve_model_role(
    use: ModelUseSpec,
    declared: Mapping[str, ModelRoleSpec],
    path: str,
    diagnostics: list[CompilationDiagnostic],
) -> PlannedModelRole | None:
    role_spec = declared.get(use.role_id)
    try:
        if role_spec is None:
            profile = ModelRoleProfile(use.role_id)
            if profile == ModelRoleProfile.CUSTOM:
                raise ValueError("custom is not a concrete built-in role identity")
            permissions = _builtin_model_role_permissions(profile)
            role_id = use.role_id
        elif role_spec.profile == ModelRoleProfile.CUSTOM:
            assert role_spec.permissions is not None
            profile = role_spec.profile
            permissions = role_spec.permissions
            role_id = role_spec.role_id
        else:
            profile = role_spec.profile
            permissions = _builtin_model_role_permissions(profile)
            role_id = role_spec.role_id
    except ValueError:
        diagnostics.append(
            _error(
                "model.role_unknown",
                f"{path}.role_id",
                f"unknown model role {use.role_id}; declare a custom role or use a built-in profile",
            )
        )
        return None
    return PlannedModelRole(
        role_id=role_id,
        profile=profile.value,
        scheduling_priority=permissions.scheduling_priority,
        requires_equivalent_inputs=permissions.requires_equivalent_inputs,
        may_feed_policy=permissions.may_feed_policy,
        may_receive_outcomes=permissions.may_receive_outcomes,
        may_adapt=permissions.may_adapt,
        failure_disposition=PlannedModelFailureDisposition(
            permissions.failure_disposition.value
        ),
        queue_disposition=PlannedModelQueueDisposition(
            permissions.queue_disposition.value
        ),
        queue_limit=permissions.queue_limit,
    )


def _builtin_model_role_permissions(
    profile: ModelRoleProfile,
) -> ModelRolePermissionsSpec:
    if profile == ModelRoleProfile.PRIMARY:
        return ModelRolePermissionsSpec(
            scheduling_priority=0,
            requires_equivalent_inputs=False,
            may_feed_policy=True,
            may_receive_outcomes=True,
            may_adapt=True,
            failure_disposition=ModelFailureDisposition.FAIL_RUN,
            queue_disposition=ModelQueueDisposition.FAIL_RUN,
        )
    if profile == ModelRoleProfile.SHADOW:
        return ModelRolePermissionsSpec(
            scheduling_priority=100,
            requires_equivalent_inputs=True,
            may_feed_policy=False,
            may_receive_outcomes=True,
            may_adapt=True,
            failure_disposition=ModelFailureDisposition.REJECT_RESULT,
            queue_disposition=ModelQueueDisposition.REJECT_NEWEST,
            queue_limit=1,
        )
    if profile == ModelRoleProfile.CANDIDATE:
        return ModelRolePermissionsSpec(
            scheduling_priority=50,
            requires_equivalent_inputs=True,
            may_feed_policy=False,
            may_receive_outcomes=True,
            may_adapt=True,
            failure_disposition=ModelFailureDisposition.REJECT_RESULT,
            queue_disposition=ModelQueueDisposition.REJECT_NEWEST,
            queue_limit=1,
        )
    if profile == ModelRoleProfile.OBSERVER:
        return ModelRolePermissionsSpec(
            scheduling_priority=200,
            requires_equivalent_inputs=False,
            may_feed_policy=False,
            may_receive_outcomes=False,
            may_adapt=False,
            failure_disposition=ModelFailureDisposition.REJECT_RESULT,
            queue_disposition=ModelQueueDisposition.SHED_OLDEST,
            queue_limit=1,
        )
    raise ValueError(f"no built-in permissions for {profile.value}")


def _validate_model_manifest_implementation(
    protocol: ProtocolSpec,
    manifest: ModelManifest,
    descriptor: PluginDescriptor,
    path: str,
    diagnostics: list[CompilationDiagnostic],
) -> None:
    if protocol.execution_mode not in manifest.contract.supported_modes:
        diagnostics.append(
            _error(
                "model.execution_mode",
                f"{path}.manifest_digest",
                f"model contract does not support {protocol.execution_mode.value} execution",
            )
        )
    compatible = any(
        value.plugin_id == descriptor.plugin_id
        and Version(descriptor.version) in SpecifierSet(value.version_spec)
        for value in manifest.implementations
    )
    if not compatible:
        diagnostics.append(
            _error(
                "model.implementation",
                f"{path}.manifest_digest",
                f"plugin {descriptor.plugin_id}=={descriptor.version} is not compatible with the manifest",
            )
        )
    required_state = manifest.contract.state.behavior.value
    observed_state = descriptor.capabilities.state_behavior.value
    state_compatible = required_state == "stateless" or required_state == observed_state
    if not state_compatible:
        diagnostics.append(
            _error(
                "model.state_behavior",
                f"{path}.manifest_digest",
                f"plugin state behavior {observed_state} does not satisfy model contract {required_state}",
            )
        )
    plugin_equivalence = descriptor.capabilities.equivalence
    required_equivalence = manifest.contract.state.replay_equivalence
    if _EQUIVALENCE_STRENGTH[plugin_equivalence] > _EQUIVALENCE_STRENGTH[required_equivalence]:
        diagnostics.append(
            _error(
                "model.replay_equivalence",
                f"{path}.manifest_digest",
                f"plugin equivalence {plugin_equivalence.value} is weaker than required "
                f"{required_equivalence.value}",
            )
        )


def _validate_model_ports_and_preprocessing(
    component: ComponentSpec,
    manifest: ModelManifest,
    descriptor: PluginDescriptor,
    resolved: Mapping[str, PluginDescriptor],
    graph: CompiledGraph | None,
    path: str,
    diagnostics: list[CompilationDiagnostic],
) -> dict[str, tuple[str, ...]]:
    if graph is None:
        return {}
    ports = {(value.component_id, value.direction, value.name): value for value in graph.ports}
    required_inputs = {value.name for value in descriptor.input_ports if value.required}
    required_outputs = {value.name for value in descriptor.output_ports if value.required}
    contract_inputs = {value.port_name for value in manifest.contract.inputs}
    contract_outputs = {value.port_name for value in manifest.contract.outputs}
    for missing in sorted(required_inputs - contract_inputs):
        diagnostics.append(
            _error(
                "model.input_missing",
                f"{path}.manifest_digest",
                f"model contract does not declare required plugin input port {missing}",
            )
        )
    for missing in sorted(required_outputs - contract_outputs):
        diagnostics.append(
            _error(
                "model.output_missing",
                f"{path}.manifest_digest",
                f"model contract does not declare required plugin output port {missing}",
            )
        )

    lineage_by_port: dict[str, tuple[str, ...]] = {}
    for input_contract in manifest.contract.inputs:
        compiled = ports.get((component.component_id, PortDirection.INPUT, input_contract.port_name))
        if compiled is None:
            diagnostics.append(
                _error(
                    "model.input_port",
                    f"{path}.manifest_digest",
                    f"model contract input {input_contract.port_name} is not a plugin input port",
                )
            )
            continue
        if compiled.type_id != input_contract.type_id:
            diagnostics.append(
                _error(
                    "model.input_type",
                    f"{path}.manifest_digest",
                    f"model input {input_contract.port_name} requires {input_contract.type_id}, "
                    f"plugin declares {compiled.type_id}",
                )
            )
            continue
        target = _model_signal_contract(input_contract, path, diagnostics)
        for source in _route_sources(graph, compiled.endpoint):
            for issue in contract_issues(source.contract, target):
                diagnostics.append(
                    _error(
                        "model.input_contract",
                        f"{path}.manifest_digest",
                        issue,
                        input_port=input_contract.port_name,
                        source=source.endpoint,
                    )
                )
        lineage = _upstream_lineage(graph, compiled.endpoint)
        lineage_by_port[input_contract.port_name] = lineage
        _validate_preprocessing_requirements(
            input_contract.preprocessing,
            component.component_id,
            descriptor,
            lineage,
            resolved,
            path,
            diagnostics,
        )

    for output_contract in manifest.contract.outputs:
        compiled = ports.get((component.component_id, PortDirection.OUTPUT, output_contract.port_name))
        if compiled is None:
            diagnostics.append(
                _error(
                    "model.output_port",
                    f"{path}.manifest_digest",
                    f"model contract output {output_contract.port_name} is not a plugin output port",
                )
            )
        elif (
            compiled.type_id != output_contract.type_id
            or compiled.type_id != PREDICTION_RECORD_SCHEMA
        ):
            diagnostics.append(
                _error(
                    "model.output_type",
                    f"{path}.manifest_digest",
                    f"model output {output_contract.port_name} must use the canonical "
                    f"{PREDICTION_RECORD_SCHEMA} envelope; contract requires "
                    f"{output_contract.type_id}, plugin declares {compiled.type_id}",
                )
            )
    return lineage_by_port


def _model_signal_contract(
    input_contract: Any,
    path: str,
    diagnostics: list[CompilationDiagnostic],
) -> SignalContract:
    requirements = thaw_json(input_contract.requirements)
    aliases = {"sample_rate_hz": "nominal_rate_hz"}
    allowed = {
        "unit",
        "channel_count",
        "minimum_channels",
        "maximum_channels",
        "nominal_rate_hz",
        "minimum_rate_hz",
        "maximum_rate_hz",
        "window_samples",
        "minimum_window_samples",
        "content_kind",
        "rate_model",
        "channel_ids",
        "required_channel_ids",
        "feature_ids",
        "required_feature_ids",
        "units",
        "event_kinds",
        "required_event_kinds",
        "missing_data_policy",
        "layout",
        "window_duration_seconds",
        "minimum_duration_seconds",
    }
    normalized: dict[str, Any] = {}
    for key, value in requirements.items():
        target = aliases.get(key, key)
        if target not in allowed:
            diagnostics.append(
                _error(
                    "model.input_requirement_unknown",
                    f"{path}.manifest_digest",
                    f"input requirement {key} has no compiler semantic contract",
                    input_port=input_contract.port_name,
                )
            )
            continue
        if target in normalized:
            diagnostics.append(
                _error(
                    "model.input_requirement_duplicate",
                    f"{path}.manifest_digest",
                    f"input requirement {key} duplicates {target}",
                    input_port=input_contract.port_name,
                )
            )
            continue
        normalized[target] = value
    try:
        return SignalContract(type_id=input_contract.type_id, **normalized)
    except (TypeError, ValueError) as exc:
        diagnostics.append(
            _error(
                "model.input_requirement_invalid",
                f"{path}.manifest_digest",
                str(exc),
                input_port=input_contract.port_name,
            )
        )
        return SignalContract(type_id=input_contract.type_id)


def _route_sources(graph: CompiledGraph, target: str) -> tuple[CompiledPort, ...]:
    ports = {value.endpoint: value for value in graph.ports}
    return tuple(
        ports[value.source]
        for value in graph.routes
        if value.target == target and value.source in ports
    )


def _upstream_lineage(graph: CompiledGraph, target: str) -> tuple[str, ...]:
    reverse: dict[str, set[str]] = {}
    starts: set[str] = set()
    for route in graph.routes:
        source_component = route.source.rsplit(".", 1)[0]
        target_component = route.target.rsplit(".", 1)[0]
        reverse.setdefault(target_component, set()).add(source_component)
        if route.target == target:
            starts.add(source_component)
    seen: set[str] = set()
    pending = list(starts)
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(reverse.get(current, ()))
    return tuple(value for value in graph.component_order if value in seen)


def _validate_preprocessing_requirements(
    requirements: Any,
    component_id: str,
    descriptor: PluginDescriptor,
    lineage: tuple[str, ...],
    resolved: Mapping[str, PluginDescriptor],
    path: str,
    diagnostics: list[CompilationDiagnostic],
) -> None:
    upstream_operations = {
        operation
        for upstream in lineage
        for operation in (
            ()
            if resolved.get(upstream) is None
            else resolved[upstream].capabilities.processing_operations
        )
    }
    internal_operations = set(descriptor.capabilities.processing_operations)
    for requirement in requirements:
        missing_lineage = set(requirement.required_lineage) - set(lineage)
        if missing_lineage:
            diagnostics.append(
                _error(
                    "model.preprocessing_lineage",
                    f"{path}.manifest_digest",
                    "required preprocessing lineage is absent: "
                    + ", ".join(sorted(missing_lineage)),
                    component_id=component_id,
                    requirement_id=requirement.requirement_id,
                )
            )
        if requirement.ownership == PreprocessingOwnership.UPSTREAM:
            if requirement.operation not in upstream_operations:
                diagnostics.append(
                    _error(
                        "model.preprocessing_missing",
                        f"{path}.manifest_digest",
                        f"required upstream operation {requirement.operation} is not declared",
                        requirement_id=requirement.requirement_id,
                    )
                )
        elif requirement.ownership == PreprocessingOwnership.MODEL_INTERNAL:
            if requirement.operation not in internal_operations:
                diagnostics.append(
                    _error(
                        "model.preprocessing_internal_missing",
                        f"{path}.manifest_digest",
                        f"model plugin does not declare internal operation {requirement.operation}",
                        requirement_id=requirement.requirement_id,
                    )
                )
            if requirement.operation in upstream_operations:
                diagnostics.append(
                    _error(
                        "model.preprocessing_duplicate",
                        f"{path}.manifest_digest",
                        f"model-internal operation {requirement.operation} is also upstream",
                        requirement_id=requirement.requirement_id,
                    )
                )
        elif requirement.ownership == PreprocessingOwnership.ARTIFACT_PREPARED:
            if requirement.operation in upstream_operations | internal_operations:
                diagnostics.append(
                    _error(
                        "model.preprocessing_duplicate",
                        f"{path}.manifest_digest",
                        f"artifact-prepared operation {requirement.operation} is repeated at runtime",
                        requirement_id=requirement.requirement_id,
                    )
                )
        elif requirement.ownership == PreprocessingOwnership.FORBIDDEN:
            if requirement.operation in upstream_operations | internal_operations:
                diagnostics.append(
                    _error(
                        "model.preprocessing_forbidden",
                        f"{path}.manifest_digest",
                        f"forbidden operation {requirement.operation} is present in the route",
                        requirement_id=requirement.requirement_id,
                    )
                )


def _validate_model_role_routes(
    component: ComponentSpec,
    role: PlannedModelRole,
    components: Mapping[str, ComponentSpec],
    graph: CompiledGraph | None,
    path: str,
    diagnostics: list[CompilationDiagnostic],
) -> None:
    if component.outcome_uses and not role.may_receive_outcomes:
        diagnostics.append(
            _error(
                "model.role_outcome_permission",
                f"{path}.role_id",
                f"role {role.role_id} cannot receive outcomes",
            )
        )
    if graph is None:
        return
    for route in graph.routes:
        if route.source.rsplit(".", 1)[0] != component.component_id:
            continue
        target_id = route.target.rsplit(".", 1)[0]
        target = components.get(target_id)
        if target is not None and target.kind == ComponentKind.POLICY and not role.may_feed_policy:
            diagnostics.append(
                _error(
                    "model.role_policy_permission",
                    f"{path}.role_id",
                    f"role {role.role_id} cannot feed policy component {target_id}",
                )
            )


def _validate_comparison_groups(
    bindings: list[PlannedModelBinding],
    graph: CompiledGraph,
    suite: SuiteSpec,
    diagnostics: list[CompilationDiagnostic],
) -> None:
    groups: dict[str, list[PlannedModelBinding]] = {}
    for binding in bindings:
        if binding.comparison_group is not None:
            groups.setdefault(binding.comparison_group, []).append(binding)
    use_indices = {value.component_id: index for index, value in enumerate(suite.model_uses)}
    for group_id, members in groups.items():
        if any(value.role.requires_equivalent_inputs for value in members) and len(members) < 2:
            member = members[0]
            diagnostics.append(
                _error(
                    "model.comparison_group_size",
                    f"$.suite.model_uses[{use_indices[member.component_id]}].comparison_group",
                    f"comparison group {group_id} requires at least two model uses",
                )
            )
            continue
        signatures = {
            value.component_id: tuple(
                sorted(
                    route.source
                    for route in graph.routes
                    if route.target.rsplit(".", 1)[0] == value.component_id
                )
            )
            for value in members
        }
        expected = next(iter(signatures.values()), ())
        for member in members:
            if member.role.requires_equivalent_inputs and signatures[member.component_id] != expected:
                diagnostics.append(
                    _error(
                        "model.comparison_inputs",
                        f"$.suite.model_uses[{use_indices[member.component_id]}].comparison_group",
                        f"model {member.component_id} does not receive equivalent admitted inputs "
                        f"within comparison group {group_id}",
                    )
                )
        output_signatures = {
            value.component_id: canonical_hash(
                [item.to_payload() for item in value.manifest.contract.outputs]
            )
            for value in members
        }
        expected_output = next(iter(output_signatures.values()), None)
        for member in members:
            if (
                member.role.requires_equivalent_inputs
                and output_signatures[member.component_id] != expected_output
            ):
                diagnostics.append(
                    _error(
                        "model.comparison_outputs",
                        f"$.suite.model_uses[{use_indices[member.component_id]}].comparison_group",
                        f"model {member.component_id} has incompatible output contracts "
                        f"within comparison group {group_id}",
                    )
                )
        member_ids = {value.component_id for value in members}
        if any(value.role.requires_equivalent_inputs for value in members):
            for phase_index, phase in enumerate(suite.phases):
                active_members = member_ids.intersection(phase.components)
                if active_members and active_members != member_ids:
                    diagnostics.append(
                        _error(
                            "model.comparison_phase",
                            f"$.suite.phases[{phase_index}].components",
                            f"comparison group {group_id} must activate all members together",
                            active=sorted(active_members),
                            missing=sorted(member_ids - active_members),
                        )
                    )


def _error(code: str, path: str, message: str, **details: Any) -> CompilationDiagnostic:
    return CompilationDiagnostic(code=code, path=path, message=message, details=details)
