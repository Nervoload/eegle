"""Replay captured inputs through the execution plan's locked graph."""

from __future__ import annotations

from dataclasses import dataclass
from eegle._domain import ComponentKind
from eegle.compiler.plan import ExecutionPlan
from eegle.plugins.registry import PluginRegistry
from eegle.replay.compare import EquivalencePolicy, EquivalenceReport, compare_runs
from eegle.replay.source import ReplayMode, build_replay_source_overrides
from eegle.runtime.phases import EngineRunResult, ExecutionEngine, OperatorController
from eegle.runtime.plan_runtime import ComponentProxyFactory
from eegle.streams.channels import StreamSpec


@dataclass(frozen=True, slots=True)
class ReplayExecution:
    mode: ReplayMode
    result: EngineRunResult
    equivalence: EquivalenceReport


class ReplayActionSafetyError(RuntimeError):
    """Raised before replay could construct a non-simulated action service."""


def require_simulation_only_actions(
    plan: ExecutionPlan,
    registry: PluginRegistry,
) -> None:
    """Fail closed before constructing physical or operator-facing services.

    A replay may reproduce simulated action traces. It must never instantiate a
    physical actuator or an authorization provider that could contact an
    operator or external interlock. A later trace-only replay mode may supply
    inert replacements explicitly; it must not weaken this default boundary.
    """

    unsafe: list[str] = []
    for provider in plan.authorization_providers:
        try:
            descriptor = registry.resolve(
                provider.plugin_id,
                f"=={provider.plugin_version}",
                mode=plan.execution_mode,
            )
        except (KeyError, ValueError) as exc:
            raise ReplayActionSafetyError(
                f"cannot verify replay authorization provider {provider.provider_id}: {exc}"
            ) from exc
        if descriptor.descriptor_hash != provider.descriptor_hash:
            raise ReplayActionSafetyError(
                f"replay authorization provider {provider.provider_id} differs from its lock"
            )
        if not provider.simulation_only or not descriptor.capabilities.simulation_only:
            unsafe.append(f"authorization provider {provider.provider_id}")

    locked_by_identity = {
        (value.plugin_id, value.version): value for value in plan.plugins
    }
    for component in plan.components:
        locked = locked_by_identity[(component.plugin_id, component.plugin_version)]
        if locked.kind != ComponentKind.ACTUATOR:
            continue
        try:
            descriptor = registry.resolve(
                component.plugin_id,
                f"=={component.plugin_version}",
                mode=plan.execution_mode,
            )
        except (KeyError, ValueError) as exc:
            raise ReplayActionSafetyError(
                f"cannot verify replay actuator {component.component_id}: {exc}"
            ) from exc
        if descriptor.descriptor_hash != locked.descriptor_hash:
            raise ReplayActionSafetyError(
                f"replay actuator {component.component_id} differs from its lock"
            )
        if not descriptor.capabilities.simulation_only:
            unsafe.append(f"actuator {component.component_id}")

    if unsafe:
        raise ReplayActionSafetyError(
            "replay refuses non-simulation action services: " + ", ".join(unsafe)
        )


class ReplayRunner:
    def __init__(
        self,
        plan: ExecutionPlan,
        registry: PluginRegistry,
        streams: tuple[StreamSpec, ...],
        *,
        proxy_factory: ComponentProxyFactory | None = None,
    ) -> None:
        self.plan = plan
        self.registry = registry
        self.streams = streams
        self.proxy_factory = proxy_factory

    def run(
        self,
        reference: EngineRunResult,
        *,
        mode: ReplayMode = ReplayMode.ACCELERATED_CAUSAL,
        policy: EquivalencePolicy | None = None,
        operator: OperatorController | None = None,
    ) -> ReplayExecution:
        normalized_mode = ReplayMode(mode)
        require_simulation_only_actions(self.plan, self.registry)
        overrides = build_replay_source_overrides(
            self.plan,
            self.streams,
            reference.captured_packets,
            mode=normalized_mode,
        )
        engine = ExecutionEngine.from_plan(
            self.plan,
            self.registry,
            proxy_factory=self.proxy_factory,
            component_overrides=overrides,
        )
        external_ids = {
            value.artifact_id for value in self.plan.artifacts if value.external
        }
        initial_artifacts = {
            value.artifact_id: value
            for value in reference.artifacts
            if value.artifact_id in external_ids
        }
        result = engine.run(artifacts=initial_artifacts, operator=operator)
        comparison = compare_runs(reference, result, policy or EquivalencePolicy())
        return ReplayExecution(normalized_mode, result, comparison)
