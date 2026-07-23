"""Phase 5 compiler from portable intent and deployment to an exact plan."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from packaging.specifiers import InvalidSpecifier

from eegle._domain import ComponentKind, ExecutionMode
from eegle._validation import thaw_json
from eegle.compiler.diagnostics import (
    CompilationDiagnostic,
    CompilationError,
    DiagnosticSeverity,
    sort_diagnostics,
)
from eegle.compiler.graph import (
    CompiledGraph,
    CompiledPort,
    CompiledRoute,
    PortDirection,
    contract_issues,
    topological_order,
)
from eegle.compiler.lock import canonical_hash
from eegle.compiler.lockfile import ExecutionLock
from eegle.compiler.plan import (
    ExecutionPlan,
    LockedPlugin,
    PlannedArtifact,
    PlannedComponent,
    PlannedPhase,
    PlannedPlacement,
    PlannedScheduledTrigger,
    PlannedStateTrigger,
    PlannedTransition,
)
from eegle.compiler.semantic_passes import (
    validate_artifacts,
    validate_phases,
    validate_roles_and_actions,
    validate_runtime_policy,
    validate_triggers_and_permissions,
)
from eegle.plugins.registry import PluginDescriptor, PluginRegistry, PortSpec
from eegle.specs.deployment import (
    ClockMappingStrategy,
    ComponentBindingSpec,
    DeploymentSpec,
    Placement,
)
from eegle.specs.protocol import PROTOCOL_JSON_SCHEMA, ProtocolSpec
from eegle.specs.schemas import SchemaValidationError, validate_payload
from eegle.specs.suite import (
    SUITE_JSON_SCHEMA,
    ComponentSpec,
    SignalContract,
    SuiteSpec,
)
from eegle.specs.deployment import DEPLOYMENT_JSON_SCHEMA


@dataclass(frozen=True, slots=True)
class CompilationResult:
    plan: ExecutionPlan
    lock: ExecutionLock
    graph: CompiledGraph
    diagnostics: tuple[CompilationDiagnostic, ...] = ()

    @property
    def valid(self) -> bool:
        return not any(
            value.severity == DiagnosticSeverity.ERROR for value in self.diagnostics
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.compilation_result.v1",
            "valid": self.valid,
            "plan": self.plan.to_payload(),
            "lock": self.lock.to_payload(),
            "graph": self.graph.to_payload(),
            "diagnostics": [value.to_payload() for value in self.diagnostics],
        }


def compile_suite(
    protocol: ProtocolSpec,
    suite: SuiteSpec,
    deployment: DeploymentSpec,
    registry: PluginRegistry,
) -> CompilationResult:
    """Compile without constructing plugins or contacting site resources."""

    diagnostics: list[CompilationDiagnostic] = []
    _validate_references(protocol, suite, deployment, diagnostics)

    components_by_id = {value.component_id: value for value in suite.components}
    bindings_by_id = {
        value.component_id: value for value in deployment.component_bindings
    }
    binding_indices = {
        value.component_id: index
        for index, value in enumerate(deployment.component_bindings)
    }
    resources_by_id = {value.resource_id: value for value in deployment.resources}
    secrets_by_id = {value.secret_id: value for value in deployment.secrets}
    streams_by_id = {value.stream_id: value for value in suite.streams}

    _validate_deployment_bindings(
        suite,
        deployment,
        components_by_id,
        resources_by_id,
        secrets_by_id,
        streams_by_id,
        diagnostics,
    )
    _validate_clocks(protocol, suite, deployment, diagnostics)

    resolved: dict[str, PluginDescriptor] = {}
    merged_configs: dict[str, Mapping[str, Any]] = {}
    for index, component in enumerate(suite.components):
        path = f"$.suite.components[{index}]"
        binding = bindings_by_id.get(component.component_id)
        binding_path = (
            None
            if binding is None
            else f"$.deployment.component_bindings[{binding_indices[component.component_id]}]"
        )
        plugin_id = binding.plugin_id if binding and binding.plugin_id else component.plugin_id
        version_spec = (
            binding.version_spec
            if binding and binding.version_spec is not None
            else component.version_spec
        )
        if plugin_id is None:
            diagnostics.append(
                _error(
                    "plugin.unbound",
                    f"{path}.plugin_id",
                    "component has no portable or deployment plugin binding",
                    component_id=component.component_id,
                )
            )
            continue
        try:
            descriptor = registry.resolve(
                plugin_id,
                version_spec,
                mode=protocol.execution_mode,
            )
        except (KeyError, ValueError, InvalidSpecifier) as exc:
            diagnostics.append(
                _error(
                    "plugin.resolve",
                    f"{binding_path}.plugin_id"
                    if binding is not None and binding.plugin_id is not None
                    else f"{path}.plugin_id",
                    str(exc),
                    plugin_id=plugin_id,
                    version_spec=version_spec,
                )
            )
            continue
        resolved[component.component_id] = descriptor
        if descriptor.kind != component.kind:
            diagnostics.append(
                _error(
                    "plugin.kind",
                    f"{path}.kind",
                    f"plugin declares {descriptor.kind.value}, component requires {component.kind.value}",
                    plugin_id=plugin_id,
                )
            )
        missing = sorted(
            set(component.required_capabilities) - _capability_tokens(descriptor)
        )
        if missing:
            diagnostics.append(
                _error(
                    "plugin.capability",
                    f"{path}.required_capabilities",
                    f"plugin does not provide required capabilities: {', '.join(missing)}",
                    plugin_id=plugin_id,
                )
            )
        config = _merge_object(
            thaw_json(component.config),
            {} if binding is None else thaw_json(binding.config),
        )
        merged_configs[component.component_id] = config
        try:
            validate_payload(config, descriptor.config_schema)
        except SchemaValidationError as exc:
            suffix = "" if exc.path == "$" else exc.path[1:]
            config_path = f"{path}.config"
            if binding is not None and binding_path is not None and binding.config:
                first_key = _first_path_key(suffix)
                if first_key is None or first_key in binding.config:
                    config_path = f"{binding_path}.config"
            diagnostics.append(
                _error(
                    "plugin.config",
                    f"{config_path}{suffix}",
                    str(exc).split(": ", 1)[-1],
                    plugin_id=plugin_id,
                )
            )
        _validate_descriptor_resources(
            component,
            descriptor,
            binding,
            resources_by_id,
            path,
            diagnostics,
        )

    graph = _compile_graph(suite, resolved, streams_by_id, diagnostics)
    validate_phases(suite, components_by_id, resolved, diagnostics)
    validate_artifacts(suite, resolved, diagnostics)
    validate_roles_and_actions(suite, deployment, diagnostics)
    validate_runtime_policy(suite, diagnostics)
    validate_triggers_and_permissions(
        protocol, suite, deployment, resolved, diagnostics
    )

    errors = [
        value for value in diagnostics if value.severity == DiagnosticSeverity.ERROR
    ]
    if errors or graph is None:
        raise CompilationError(diagnostics or (_error("graph.invalid", "$.suite.routes", "graph could not compile"),))

    locked_plugins = _locked_plugins(resolved)
    input_bindings, output_bindings = _plan_bindings(graph)
    planned_components = tuple(
        PlannedComponent(
            component_id=component.component_id,
            plugin_id=resolved[component.component_id].plugin_id,
            plugin_version=resolved[component.component_id].version,
            config=merged_configs[component.component_id],
            input_bindings=input_bindings.get(component.component_id, {}),
            output_bindings=output_bindings.get(component.component_id, {}),
            role=component.role,
            stream_id=component.stream_id,
            required_capabilities=component.required_capabilities,
            outcome_uses=component.outcome_uses,
            required_outcome_use=component.required_outcome_use,
            action_capabilities=component.action_capabilities,
        )
        for component in suite.components
    )
    phases = tuple(
        PlannedPhase(
            phase_id=phase.phase_id,
            component_ids=phase.components,
            transitions=tuple(
                PlannedTransition(
                    target_phase=value.target_phase,
                    condition=value.condition.value,
                )
                for value in phase.transitions
            ),
            required_artifacts=phase.required_artifacts,
            retry_limit=phase.retry_limit,
            resume_policy=phase.resume_policy.value,
            operator_confirmation=phase.operator_confirmation,
            timeout_seconds=phase.timeout_seconds,
            acceptance_criteria=phase.acceptance_criteria,
        )
        for phase in suite.phases
    )
    artifacts = tuple(
        PlannedArtifact(
            artifact_id=value.artifact_id,
            role=value.role,
            media_type=value.media_type,
            producer_phase=value.producer_phase,
            producer_component=value.producer_component,
            producer_port=value.producer_port,
            expected_digest=value.expected_digest,
        )
        for value in suite.artifacts
    )
    placements = tuple(
        _planned_placement(component.component_id, bindings_by_id.get(component.component_id))
        for component in suite.components
    )
    spec_hashes = {
        "protocol": protocol.spec_hash,
        "suite": suite.spec_hash,
        "deployment": deployment.spec_hash,
    }
    validation_policy = thaw_json(suite.validation)
    validation_policy.setdefault("max_graph_events", 100_000)
    validation_policy.setdefault("max_pending_events", 1_024)
    validation_policy.setdefault("max_idle_cycles", 1)
    validation_policy.setdefault("component_deadlines_seconds", {})
    validation_policy.setdefault("max_phase_transitions", max(32, len(phases) * 4))
    mapping_revisions = {
        _clock_mapping_id(value.source_clock, value.target_clock): 1
        for value in deployment.clock_mappings
    }
    plan = ExecutionPlan(
        plan_id=_derived_id("plan", suite.suite_id, deployment.deployment_id),
        execution_mode=protocol.execution_mode,
        plugins=locked_plugins,
        components=planned_components,
        spec_hashes=spec_hashes,
        clock_policy={
            "suite": thaw_json(suite.clock_policy),
            "mappings": [value.to_payload() for value in deployment.clock_mappings],
            "mapping_revisions": mapping_revisions,
        },
        recording_policy=thaw_json(suite.recording),
        validation_rules={
            "suite": validation_policy,
            "claims": [value.to_payload() for value in protocol.claims],
            "metrics": [value.to_payload() for value in protocol.metrics],
            "acceptance": [value.to_payload() for value in protocol.acceptance],
            "permissions": [value.to_payload() for value in deployment.permissions],
            "graph_hash": graph.graph_hash,
        },
        graph=graph,
        phases=phases,
        initial_phase=suite.initial_phase,
        placements=placements,
        artifacts=artifacts,
        scheduling_policy=suite.scheduling.to_payload(),
        scheduled_triggers=tuple(
            PlannedScheduledTrigger(**value.to_payload())
            for value in suite.scheduled_triggers
        ),
        state_triggers=tuple(
            PlannedStateTrigger(**value.to_payload())
            for value in suite.state_triggers
        ),
    )
    lock = ExecutionLock(
        lock_id=_derived_id("lock", suite.suite_id, deployment.deployment_id),
        plan_hash=plan.plan_hash,
        spec_hashes=spec_hashes,
        plugins=locked_plugins,
        component_hashes={
            value.component_id: canonical_hash(value.to_payload())
            for value in planned_components
        },
        schema_hashes={
            "protocol.v1": canonical_hash(PROTOCOL_JSON_SCHEMA),
            "suite.v1": canonical_hash(SUITE_JSON_SCHEMA),
            "deployment.v1": canonical_hash(DEPLOYMENT_JSON_SCHEMA),
        },
        graph_hash=graph.graph_hash,
        artifact_hashes={
            value.artifact_id: value.expected_digest
            for value in artifacts
            if value.expected_digest is not None
        },
    )
    lock.verify_plan(plan)
    return CompilationResult(
        plan=plan,
        lock=lock,
        graph=graph,
        diagnostics=sort_diagnostics(diagnostics),
    )


def _validate_references(
    protocol: ProtocolSpec,
    suite: SuiteSpec,
    deployment: DeploymentSpec,
    diagnostics: list[CompilationDiagnostic],
) -> None:
    if suite.protocol_id != protocol.protocol_id:
        diagnostics.append(
            _error(
                "spec.protocol_reference",
                "$.suite.protocol_id",
                f"expected {protocol.protocol_id}, observed {suite.protocol_id}",
            )
        )
    if deployment.suite_id != suite.suite_id:
        diagnostics.append(
            _error(
                "spec.suite_reference",
                "$.deployment.suite_id",
                f"expected {suite.suite_id}, observed {deployment.suite_id}",
            )
        )


def _validate_deployment_bindings(
    suite: SuiteSpec,
    deployment: DeploymentSpec,
    components: Mapping[str, ComponentSpec],
    resources: Mapping[str, Any],
    secrets: Mapping[str, Any],
    streams: Mapping[str, Any],
    diagnostics: list[CompilationDiagnostic],
) -> None:
    for index, binding in enumerate(deployment.component_bindings):
        path = f"$.deployment.component_bindings[{index}]"
        if binding.component_id not in components:
            diagnostics.append(
                _error(
                    "deployment.component_reference",
                    f"{path}.component_id",
                    f"unknown suite component {binding.component_id}",
                )
            )
        for resource_id in binding.resource_ids:
            if resource_id not in resources:
                diagnostics.append(
                    _error(
                        "deployment.resource_reference",
                        f"{path}.resource_ids",
                        f"unknown resource {resource_id}",
                    )
                )
        for secret_id in binding.secret_refs.values():
            if secret_id not in secrets:
                diagnostics.append(
                    _error(
                        "deployment.secret_reference",
                        f"{path}.secret_refs",
                        f"unknown secret reference {secret_id}",
                    )
                )
    stream_bindings = {value.stream_id: value for value in deployment.stream_bindings}
    for index, binding in enumerate(deployment.stream_bindings):
        path = f"$.deployment.stream_bindings[{index}]"
        if binding.stream_id not in streams:
            diagnostics.append(
                _error(
                    "deployment.stream_reference",
                    f"{path}.stream_id",
                    f"unknown logical stream {binding.stream_id}",
                )
            )
            continue
        resource = resources.get(binding.resource_id)
        if resource is None:
            diagnostics.append(
                _error(
                    "deployment.resource_reference",
                    f"{path}.resource_id",
                    f"unknown resource {binding.resource_id}",
                )
            )
            continue
        if resource.contract is None:
            diagnostics.append(
                _error(
                    "stream.capability_unproven",
                    f"$.deployment.resources.{resource.resource_id}.contract",
                    "bound resource does not declare a signal contract",
                    stream_id=binding.stream_id,
                )
            )
        else:
            for issue in contract_issues(resource.contract, streams[binding.stream_id].contract):
                diagnostics.append(
                    _error(
                        "stream.capability",
                        f"{path}.resource_id",
                        issue,
                        stream_id=binding.stream_id,
                        resource_id=resource.resource_id,
                    )
                )
    for stream_id in streams:
        if stream_id not in stream_bindings:
            diagnostics.append(
                _error(
                    "deployment.stream_unbound",
                    "$.deployment.stream_bindings",
                    f"logical stream {stream_id} has no deployment resource binding",
                )
            )


def _validate_descriptor_resources(
    component: ComponentSpec,
    descriptor: PluginDescriptor,
    binding: ComponentBindingSpec | None,
    resources: Mapping[str, Any],
    path: str,
    diagnostics: list[CompilationDiagnostic],
) -> None:
    if not descriptor.capabilities.resources:
        return
    bound = () if binding is None else binding.resource_ids
    provided: set[str] = set()
    for resource_id in bound:
        resource = resources.get(resource_id)
        if resource is not None:
            provided.add(resource.kind)
            provided.update(resource.capabilities)
    missing = sorted(set(descriptor.capabilities.resources) - provided)
    if missing:
        diagnostics.append(
            _error(
                "plugin.resource",
                f"{path}.required_capabilities",
                f"deployment does not provide plugin resources: {', '.join(missing)}",
                component_id=component.component_id,
            )
        )


def _validate_clocks(
    protocol: ProtocolSpec,
    suite: SuiteSpec,
    deployment: DeploymentSpec,
    diagnostics: list[CompilationDiagnostic],
) -> None:
    execution_clock = suite.clock_policy.get("execution_clock_id")
    if not isinstance(execution_clock, str) or not execution_clock:
        diagnostics.append(
            _error(
                "clock.execution_missing",
                "$.suite.clock_policy.execution_clock_id",
                "suite must explicitly identify the execution clock",
            )
        )
        return
    pairs: set[tuple[str, str]] = set()
    for index, mapping in enumerate(deployment.clock_mappings):
        pair = (mapping.source_clock, mapping.target_clock)
        if pair in pairs:
            diagnostics.append(
                _error(
                    "clock.mapping_duplicate",
                    f"$.deployment.clock_mappings[{index}]",
                    f"duplicate mapping from {mapping.source_clock} to {mapping.target_clock}",
                )
            )
        pairs.add(pair)
        if protocol.execution_mode == ExecutionMode.CAUSAL and mapping.strategy in {
            ClockMappingStrategy.POSTHOC,
            ClockMappingStrategy.RECORDED_REPLAY,
        }:
            diagnostics.append(
                _error(
                    "clock.mapping_mode",
                    f"$.deployment.clock_mappings[{index}].strategy",
                    f"{mapping.strategy.value} clock mapping is not available during causal execution",
                )
            )
    for index, stream in enumerate(suite.streams):
        if stream.clock_id is None:
            diagnostics.append(
                _error(
                    "clock.stream_missing",
                    f"$.suite.streams[{index}].clock_id",
                    f"logical stream {stream.stream_id} must declare a clock",
                )
            )
        elif stream.clock_id != execution_clock and (stream.clock_id, execution_clock) not in pairs:
            diagnostics.append(
                _error(
                    "clock.mapping_missing",
                    "$.deployment.clock_mappings",
                    f"no mapping from stream clock {stream.clock_id} to execution clock {execution_clock}",
                    stream_id=stream.stream_id,
                )
            )
    maximum = suite.clock_policy.get("maximum_uncertainty_seconds")
    if maximum is not None:
        try:
            threshold = float(maximum)
            if not math.isfinite(threshold) or threshold < 0:
                raise ValueError
        except (TypeError, ValueError):
            diagnostics.append(
                _error(
                    "clock.uncertainty_invalid",
                    "$.suite.clock_policy.maximum_uncertainty_seconds",
                    "maximum uncertainty must be a finite non-negative number",
                )
            )
        else:
            for index, mapping in enumerate(deployment.clock_mappings):
                if mapping.maximum_uncertainty_seconds > threshold:
                    diagnostics.append(
                        _error(
                            "clock.uncertainty",
                            f"$.deployment.clock_mappings[{index}].maximum_uncertainty_seconds",
                            f"mapping uncertainty {mapping.maximum_uncertainty_seconds} exceeds suite maximum {threshold}",
                        )
                    )


def _compile_graph(
    suite: SuiteSpec,
    resolved: Mapping[str, PluginDescriptor],
    streams: Mapping[str, Any],
    diagnostics: list[CompilationDiagnostic],
) -> CompiledGraph | None:
    ports: list[CompiledPort] = []
    port_lookup: dict[tuple[str, PortDirection, str], CompiledPort] = {}
    components = {value.component_id: value for value in suite.components}
    for index, component in enumerate(suite.components):
        descriptor = resolved.get(component.component_id)
        if descriptor is None:
            continue
        if component.stream_id is not None and component.stream_id not in streams:
            diagnostics.append(
                _error(
                    "stream.reference",
                    f"$.suite.components[{index}].stream_id",
                    f"unknown logical stream {component.stream_id}",
                )
            )
        if component.kind == ComponentKind.SOURCE and component.stream_id is None:
            diagnostics.append(
                _error(
                    "stream.source_unbound",
                    f"$.suite.components[{index}].stream_id",
                    "source component must identify its portable logical stream",
                )
            )
        for direction, declared, contracts in (
            (PortDirection.INPUT, descriptor.input_ports, component.input_contracts),
            (PortDirection.OUTPUT, descriptor.output_ports, component.output_contracts),
        ):
            descriptor_names = {value.name for value in declared}
            for name in contracts:
                if name not in descriptor_names:
                    diagnostics.append(
                        _error(
                            "port.unknown_contract",
                            f"$.suite.components[{index}].{direction.value}_contracts.{name}",
                            f"plugin does not declare {direction.value} port {name}",
                        )
                    )
            for descriptor_port in declared:
                contract = contracts.get(descriptor_port.name)
                if (
                    contract is None
                    and direction == PortDirection.OUTPUT
                    and component.kind == ComponentKind.SOURCE
                    and component.stream_id in streams
                ):
                    contract = streams[component.stream_id].contract
                if contract is None:
                    contract = SignalContract(type_id=descriptor_port.type_id)
                elif contract.type_id != descriptor_port.type_id:
                    diagnostics.append(
                        _error(
                            "port.contract_type",
                            f"$.suite.components[{index}].{direction.value}_contracts.{descriptor_port.name}.type_id",
                            f"contract type {contract.type_id} does not match plugin port type {descriptor_port.type_id}",
                        )
                    )
                    contract = SignalContract(type_id=descriptor_port.type_id)
                compiled = _compiled_port(component, descriptor_port, direction, contract)
                ports.append(compiled)
                port_lookup[(component.component_id, direction, descriptor_port.name)] = compiled

    routes: list[CompiledRoute] = []
    target_counts: dict[tuple[str, str], int] = {}
    component_edges: list[tuple[str, str]] = []
    for index, route in enumerate(suite.routes):
        path = f"$.suite.routes[{index}]"
        source = port_lookup.get(
            (route.source_component, PortDirection.OUTPUT, route.source_port)
        )
        target = port_lookup.get(
            (route.target_component, PortDirection.INPUT, route.target_port)
        )
        if route.source_component not in components:
            diagnostics.append(
                _error("route.source_component", f"{path}.source.component", f"unknown component {route.source_component}")
            )
        elif source is None:
            diagnostics.append(
                _error("route.source_port", f"{path}.source.port", f"unknown output port {route.source_port}")
            )
        if route.target_component not in components:
            diagnostics.append(
                _error("route.target_component", f"{path}.target.component", f"unknown component {route.target_component}")
            )
        elif target is None:
            diagnostics.append(
                _error("route.target_port", f"{path}.target.port", f"unknown input port {route.target_port}")
            )
        if source is None or target is None:
            continue
        key = (target.component_id, target.name)
        target_counts[key] = target_counts.get(key, 0) + 1
        if target_counts[key] > 1 and not target.multiple:
            diagnostics.append(
                _error(
                    "route.input_multiplicity",
                    f"{path}.target",
                    f"input {target.endpoint} accepts only one route",
                )
            )
        issues = contract_issues(source.contract, target.contract)
        for issue in issues:
            diagnostics.append(
                _error(
                    "route.contract",
                    path,
                    issue,
                    source=source.endpoint,
                    target=target.endpoint,
                )
            )
        if source.type_id == target.type_id:
            routes.append(
                CompiledRoute(
                    route_id=route.route_id,
                    source=source.endpoint,
                    target=target.endpoint,
                    type_id=source.type_id,
                )
            )
            component_edges.append((source.component_id, target.component_id))
        else:
            diagnostics.append(
                _error(
                    "route.type",
                    path,
                    f"output type {source.type_id} cannot connect to input type {target.type_id}",
                )
            )
    for port in ports:
        if port.direction == PortDirection.INPUT and port.required:
            if target_counts.get((port.component_id, port.name), 0) == 0:
                diagnostics.append(
                    _error(
                        "route.required_input",
                        "$.suite.routes",
                        f"required input {port.endpoint} is not connected",
                    )
                )
    component_ids = tuple(value.component_id for value in suite.components)
    order = topological_order(component_ids, tuple(component_edges))
    if order is None:
        diagnostics.append(
            _error("route.cycle", "$.suite.routes", "component data graph contains a cycle")
        )
        return None
    return CompiledGraph(ports=tuple(ports), routes=tuple(routes), component_order=order)


def _locked_plugins(resolved: Mapping[str, PluginDescriptor]) -> tuple[LockedPlugin, ...]:
    unique = {
        (value.plugin_id, value.version): value for value in resolved.values()
    }
    return tuple(
        LockedPlugin(
            plugin_id=value.plugin_id,
            version=value.version,
            kind=value.kind,
            descriptor_hash=value.descriptor_hash,
            distribution=value.distribution,
            implementation=value.implementation,
        )
        for _, value in sorted(unique.items())
    )


def _plan_bindings(
    graph: CompiledGraph,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    inputs: dict[str, dict[str, Any]] = {}
    output_lists: dict[str, dict[str, list[str]]] = {}
    for route in graph.routes:
        source_component, source_port = route.source.rsplit(".", 1)
        target_component, target_port = route.target.rsplit(".", 1)
        inputs.setdefault(target_component, {})[target_port] = route.source
        output_lists.setdefault(source_component, {}).setdefault(source_port, []).append(
            route.target
        )
    outputs: dict[str, dict[str, Any]] = {}
    for component_id, ports in output_lists.items():
        outputs[component_id] = {
            port: values[0] if len(values) == 1 else sorted(values)
            for port, values in ports.items()
        }
    return inputs, outputs


def _planned_placement(
    component_id: str, binding: ComponentBindingSpec | None
) -> PlannedPlacement:
    if binding is None:
        return PlannedPlacement(component_id=component_id, placement=Placement.IN_PROCESS.value)
    return PlannedPlacement(
        component_id=component_id,
        placement=binding.placement.value,
        endpoint_id=binding.endpoint_id,
        resource_ids=binding.resource_ids,
        secret_refs=binding.secret_refs,
    )


def _compiled_port(
    component: ComponentSpec,
    port: PortSpec,
    direction: PortDirection,
    contract: SignalContract,
) -> CompiledPort:
    return CompiledPort(
        component_id=component.component_id,
        name=port.name,
        direction=direction,
        type_id=port.type_id,
        required=port.required,
        multiple=port.multiple,
        contract=contract,
    )


def _capability_tokens(descriptor: PluginDescriptor) -> set[str]:
    capabilities = descriptor.capabilities
    values = {
        *(f"mode:{value.value}" for value in capabilities.supported_modes),
        f"determinism:{capabilities.determinism.value}",
        f"equivalence:{capabilities.equivalence.value}",
        f"state:{capabilities.state_behavior.value}",
        *(f"resource:{value}" for value in capabilities.resources),
    }
    if capabilities.requires_future:
        values.add("requires_future")
    if capabilities.supports_triggers:
        values.add("supports_triggers")
    return values


def _merge_object(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = {str(key): thaw_json(value) for key, value in base.items()}
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_object(current, value)
        else:
            merged[key] = thaw_json(value)
    return merged


def _error(code: str, path: str, message: str, **details: Any) -> CompilationDiagnostic:
    return CompilationDiagnostic(code=code, path=path, message=message, details=details)


def _first_path_key(suffix: str) -> str | None:
    if not suffix.startswith("."):
        return None
    return suffix[1:].split(".", 1)[0].split("[", 1)[0]


def _derived_id(prefix: str, *parts: str) -> str:
    candidate = ".".join((prefix, *parts))
    if len(candidate) <= 255:
        return candidate
    digest = canonical_hash(list(parts)).removeprefix("sha256:")
    return f"{prefix}.{digest}"


def _clock_mapping_id(source_clock: str, target_clock: str) -> str:
    """Name a compiled mapping independently of machine clock objects."""

    candidate = f"mapping.{source_clock}.to.{target_clock}"
    if len(candidate) <= 255:
        return candidate
    digest = canonical_hash([source_clock, target_clock]).removeprefix("sha256:")
    return f"mapping.{digest}"
