"""Load a plan-bearing evidence bundle and replay its recorded execution."""

from __future__ import annotations

import json
from dataclasses import dataclass

from eegle._domain import ComponentKind, EquivalenceLevel
from eegle._validation import require_digest, require_identifier
from eegle.compiler.plan import ExecutionPlan
from eegle.plugins.registry import PluginRegistry
from eegle.recording.artifacts import ArtifactReference
from eegle.recording.bundles import EvidenceReader
from eegle.recording.evidence import EvidenceRecord
from eegle.recording.stores import FRAMED_SAMPLE_MEDIA_TYPE, read_framed_sample_store
from eegle.replay.compare import EquivalencePolicy, compare_runs
from eegle.replay.runner import ReplayExecution, require_simulation_only_actions
from eegle.replay.source import ReplayMode, build_replay_source_overrides
from eegle.runtime.phases import EngineRunResult, EngineStatus, ExecutionEngine, OperatorController
from eegle.runtime.model_admission import ArtifactResolver
from eegle.runtime.plan_runtime import ComponentProxyFactory
from eegle.streams.channels import StreamSpec
from eegle.streams.packets import Packet


@dataclass(frozen=True, slots=True)
class RecordedRun:
    """The comparison and input surface restored from immutable evidence."""

    execution_id: str
    plan_hash: str
    status: EngineStatus
    evidence: tuple[EvidenceRecord, ...]
    captured_packets: tuple[Packet, ...]
    equivalence_ceiling: EquivalenceLevel
    artifacts: tuple[ArtifactReference, ...] = ()
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class RecordedExecution:
    plan: ExecutionPlan
    streams: tuple[StreamSpec, ...]
    reference: RecordedRun


@dataclass(frozen=True, slots=True)
class ModelReplacementReplay:
    """Counterfactual replay result for an independently compiled model plan."""

    original_plan_hash: str
    replacement_plan_hash: str
    replaced_components: tuple[str, ...]
    execution: ReplayExecution

    def __post_init__(self) -> None:
        for field_name in ("original_plan_hash", "replacement_plan_hash"):
            require_digest(getattr(self, field_name), field_name)
        if self.original_plan_hash == self.replacement_plan_hash:
            raise ValueError("replacement replay requires distinct plan hashes")
        normalized = tuple(
            sorted(
                require_identifier(value, "replaced component")
                for value in self.replaced_components
            )
        )
        if not normalized or len(normalized) != len(set(normalized)):
            raise ValueError("replacement replay requires unique component identities")
        object.__setattr__(self, "replaced_components", normalized)
        if not isinstance(self.execution, ReplayExecution):
            raise TypeError("replacement replay execution must be a ReplayExecution")


def load_recorded_execution(reader: EvidenceReader) -> RecordedExecution:
    """Resolve and verify the plan, evidence ledger, and exact admitted capture."""

    report = reader.verify()
    if not report.valid:
        raise ValueError(f"cannot replay bundle with {report.status.value} integrity")
    plans = tuple(
        reference for reference in reader.bundle.artifacts if reference.role == "execution_plan"
    )
    if len(plans) != 1:
        raise ValueError(
            f"replay requires exactly one execution_plan artifact; observed {len(plans)}"
        )
    plan_reference = plans[0]
    if not plan_reference.embedded:
        raise ValueError("execution_plan must be embedded for bundle-driven replay")
    try:
        plan_payload = json.loads(
            reader.session.artifacts.resolve(plan_reference).read_text(encoding="utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("execution_plan artifact is not valid JSON") from exc
    if not isinstance(plan_payload, dict):
        raise ValueError("execution_plan artifact must contain a JSON object")
    plan = ExecutionPlan.from_payload(plan_payload)
    if plan.plan_hash != reader.bundle.plan_hash:
        raise ValueError("execution_plan hash does not match the evidence bundle")

    captures = reader.bundle.execution_captures
    if len(captures) != 1:
        raise ValueError(
            f"replay requires exactly one execution capture; observed {len(captures)}"
        )
    capture = captures[0]
    if not capture.embedded or capture.media_type != FRAMED_SAMPLE_MEDIA_TYPE:
        raise ValueError("bundle replay requires one embedded framed execution capture")
    restored = read_framed_sample_store(reader.session.artifacts.resolve(capture))
    metadata = reader.bundle.metadata
    try:
        status = EngineStatus(str(metadata["engine_status"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("bundle is missing a valid engine_status") from exc
    equivalence_value = metadata.get("equivalence_ceiling")
    if equivalence_value is None:
        raise ValueError("bundle is missing its equivalence_ceiling")
    reference = RecordedRun(
        execution_id=str(metadata.get("execution_id") or reader.bundle.bundle_id),
        plan_hash=reader.bundle.plan_hash,
        status=status,
        evidence=reader.records(),
        captured_packets=restored.packets,
        equivalence_ceiling=EquivalenceLevel(str(equivalence_value)),
        artifacts=tuple(
            value
            for value in reader.bundle.artifacts
            if value.role != "execution_plan"
        ),
        failure=None if metadata.get("failure") is None else str(metadata["failure"]),
    )
    return RecordedExecution(plan, restored.streams, reference)


class BundleReplayRunner:
    """Replay a self-describing bundle through its exact locked execution plan."""

    def __init__(
        self,
        registry: PluginRegistry,
        *,
        proxy_factory: ComponentProxyFactory | None = None,
        artifact_resolver: ArtifactResolver | None = None,
    ) -> None:
        self.registry = registry
        self.proxy_factory = proxy_factory
        self.artifact_resolver = artifact_resolver

    def run(
        self,
        reader: EvidenceReader,
        *,
        mode: ReplayMode = ReplayMode.ACCELERATED_CAUSAL,
        policy: EquivalencePolicy | None = None,
        operator: OperatorController | None = None,
    ) -> ReplayExecution:
        recorded = load_recorded_execution(reader)
        normalized_mode = ReplayMode(mode)
        require_simulation_only_actions(recorded.plan, self.registry)
        overrides = build_replay_source_overrides(
            recorded.plan,
            recorded.streams,
            recorded.reference.captured_packets,
            mode=normalized_mode,
        )
        engine = ExecutionEngine.from_plan(
            recorded.plan,
            self.registry,
            proxy_factory=self.proxy_factory,
            component_overrides=overrides,
            artifact_resolver=self.artifact_resolver,
        )
        external_ids = {
            value.artifact_id for value in recorded.plan.artifacts if value.external
        }
        initial_artifacts = {
            value.artifact_id: value
            for value in recorded.reference.artifacts
            if value.artifact_id in external_ids
        }
        result: EngineRunResult = engine.run(
            artifacts=initial_artifacts,
            operator=operator,
        )
        comparison = compare_runs(
            recorded.reference,
            result,
            policy or EquivalencePolicy(),
        )
        return ReplayExecution(normalized_mode, result, comparison)

    def run_with_model_replacements(
        self,
        reader: EvidenceReader,
        replacement_plan: ExecutionPlan,
        *,
        replaced_components: tuple[str, ...],
        mode: ReplayMode = ReplayMode.ACCELERATED_CAUSAL,
        policy: EquivalencePolicy | None = None,
        operator: OperatorController | None = None,
    ) -> ModelReplacementReplay:
        """Replay captured inputs through a separately compiled replacement plan.

        Only named model components may differ.  The protocol, graph, phases,
        non-model components, recording/clock/scheduling semantics, outcome and
        action authorities remain exact.  A changed model result is therefore a
        deliberate counterfactual divergence, never an equivalence claim about
        the recorded execution.
        """

        recorded = load_recorded_execution(reader)
        components = _validate_model_replacement_plan(
            recorded.plan,
            replacement_plan,
            replaced_components,
        )
        normalized_mode = ReplayMode(mode)
        require_simulation_only_actions(replacement_plan, self.registry)
        overrides = build_replay_source_overrides(
            replacement_plan,
            recorded.streams,
            recorded.reference.captured_packets,
            mode=normalized_mode,
        )
        engine = ExecutionEngine.from_plan(
            replacement_plan,
            self.registry,
            proxy_factory=self.proxy_factory,
            component_overrides=overrides,
            artifact_resolver=self.artifact_resolver,
        )
        external_ids = {
            value.artifact_id for value in replacement_plan.artifacts if value.external
        }
        initial_artifacts = {
            value.artifact_id: value
            for value in recorded.reference.artifacts
            if value.artifact_id in external_ids
        }
        result = engine.run(artifacts=initial_artifacts, operator=operator)
        comparison = compare_runs(
            recorded.reference,
            result,
            policy or EquivalencePolicy(),
        )
        return ModelReplacementReplay(
            recorded.plan.plan_hash,
            replacement_plan.plan_hash,
            components,
            ReplayExecution(normalized_mode, result, comparison),
        )


def _validate_model_replacement_plan(
    original: ExecutionPlan,
    replacement: ExecutionPlan,
    replaced_components: tuple[str, ...],
) -> tuple[str, ...]:
    components = tuple(sorted(set(replaced_components)))
    if not components or len(components) != len(replaced_components):
        raise ValueError("model replacement requires unique component identities")
    if original.plan_hash == replacement.plan_hash:
        raise ValueError("model replacement plan must differ from the recorded plan")
    if original.plan_id != replacement.plan_id:
        raise ValueError("model replacement must preserve plan identity")
    if original.execution_mode != replacement.execution_mode:
        raise ValueError("model replacement cannot change execution mode")
    if original.spec_hashes.get("protocol") != replacement.spec_hashes.get("protocol"):
        raise ValueError("model replacement must preserve portable protocol identity")

    original_components = {value.component_id: value for value in original.components}
    replacement_components = {
        value.component_id: value for value in replacement.components
    }
    if set(original_components) != set(replacement_components):
        raise ValueError("model replacement cannot add or remove components")
    original_kinds = _component_kinds(original)
    replacement_kinds = _component_kinds(replacement)
    for component_id in components:
        if component_id not in original_components:
            raise ValueError(f"unknown model replacement component: {component_id}")
        if (
            original_kinds[component_id] != ComponentKind.MODEL
            or replacement_kinds[component_id] != ComponentKind.MODEL
        ):
            raise ValueError(f"replacement target {component_id} is not a model")
    for component_id in set(original_components) - set(components):
        if original_components[component_id] != replacement_components[component_id]:
            raise ValueError(
                f"model replacement changed non-target component {component_id}"
            )
    _validate_replacement_plugins(
        original,
        replacement,
        components,
    )

    if original.graph != replacement.graph:
        raise ValueError("model replacement must preserve the compiled typed graph")
    for field_name in (
        "clock_policy",
        "recording_policy",
        "validation_rules",
        "phases",
        "initial_phase",
        "artifacts",
        "outcome_expectations",
        "adaptations",
        "authorization_providers",
        "action_grants",
        "scheduling_policy",
        "scheduled_triggers",
        "state_triggers",
    ):
        if getattr(original, field_name) != getattr(replacement, field_name):
            raise ValueError(f"model replacement changed {field_name}")

    original_placements = {value.component_id: value for value in original.placements}
    replacement_placements = {
        value.component_id: value for value in replacement.placements
    }
    if set(original_placements) != set(replacement_placements):
        raise ValueError("model replacement cannot add or remove placements")
    for component_id in set(original_placements) - set(components):
        if original_placements[component_id] != replacement_placements[component_id]:
            raise ValueError(
                f"model replacement changed non-target placement {component_id}"
            )

    original_bindings = {value.component_id: value for value in original.model_bindings}
    replacement_bindings = {
        value.component_id: value for value in replacement.model_bindings
    }
    if set(original_bindings) != set(replacement_bindings):
        raise ValueError("model replacement cannot add or remove model bindings")
    for component_id in original_bindings:
        left = original_bindings[component_id]
        right = replacement_bindings[component_id]
        if component_id not in components:
            if left != right:
                raise ValueError(
                    f"model replacement changed untargeted binding {component_id}"
                )
            continue
        if left.manifest.contract != right.manifest.contract:
            raise ValueError(
                f"replacement model {component_id} changes the scientific contract"
            )
        if (
            left.role != right.role
            or left.comparison_group != right.comparison_group
            or left.preprocessing_lineage != right.preprocessing_lineage
            or left.preprocessing_attestations != right.preprocessing_attestations
        ):
            raise ValueError(
                f"replacement model {component_id} changes role or input authority"
            )
    return components


def _component_kinds(plan: ExecutionPlan) -> dict[str, ComponentKind]:
    plugins = {
        (value.plugin_id, value.version): value.kind for value in plan.plugins
    }
    return {
        value.component_id: plugins[(value.plugin_id, value.plugin_version)]
        for value in plan.components
    }


def _validate_replacement_plugins(
    original: ExecutionPlan,
    replacement: ExecutionPlan,
    replaced_components: tuple[str, ...],
) -> None:
    original_components = {
        value.component_id: value for value in original.components
    }
    original_plugins = {
        (value.plugin_id, value.version): value for value in original.plugins
    }
    replacement_plugins = {
        (value.plugin_id, value.version): value for value in replacement.plugins
    }
    original_required = {
        (value.plugin_id, value.plugin_version)
        for value in original.components
    } | {
        (value.plugin_id, value.plugin_version)
        for value in original.authorization_providers
    }
    replacement_required = {
        (value.plugin_id, value.plugin_version)
        for value in replacement.components
    } | {
        (value.plugin_id, value.plugin_version)
        for value in replacement.authorization_providers
    }
    if set(original_plugins) != original_required:
        raise ValueError("recorded plan contains unused locked plugins")
    if set(replacement_plugins) != replacement_required:
        raise ValueError("replacement plan contains unused locked plugins")
    protected = {
        (
            original_components[component_id].plugin_id,
            original_components[component_id].plugin_version,
        )
        for component_id in set(original_components) - set(replaced_components)
    } | {
        (value.plugin_id, value.plugin_version)
        for value in original.authorization_providers
    }
    for plugin_key in protected:
        if original_plugins[plugin_key] != replacement_plugins.get(plugin_key):
            raise ValueError(
                "model replacement changed a non-target locked plugin descriptor"
            )
