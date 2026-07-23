"""Replay captured inputs through the execution plan's locked graph."""

from __future__ import annotations

from dataclasses import dataclass
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
