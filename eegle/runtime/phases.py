"""Generic orchestration of the phase machine locked in an execution plan."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Protocol

from eegle._domain import EquivalenceLevel
from eegle._validation import require_identifier, thaw_json
from eegle.compiler.plan import ExecutionPlan, PlannedPhase, PlannedTransition
from eegle.plugins.registry import PluginRegistry
from eegle.recording.evidence import EvidenceRecord
from eegle.recording.artifacts import ArtifactReference
from eegle.recording.publications import ArtifactPublication
from eegle.runtime.graph import (
    GraphInput,
    GraphPhaseResult,
    GraphRunStatus,
    PlanGraphExecutor,
)
from eegle.runtime.checkpoints import EngineCheckpoint
from eegle.runtime.plan_runtime import (
    ComponentProxyFactory,
    PlanRuntime,
    construct_plan_runtime,
)
from eegle.streams.clocks import TimePoint
from eegle.streams.packets import DenseSampleBatch, MetadataEvent, Packet, SparseEventBatch


class EngineStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    TIMED_OUT = "timed_out"
    CHECKPOINTED = "checkpointed"


class OperatorController(Protocol):
    """Human/operator boundary; UI and recipe code implement this protocol."""

    def confirm_phase_entry(
        self,
        phase: PlannedPhase,
        attempt: int,
        available_artifacts: tuple[str, ...],
    ) -> bool:
        ...

    def select_transition(
        self,
        phase: PlannedPhase,
        candidates: tuple[PlannedTransition, ...],
        result: GraphPhaseResult,
    ) -> str | None:
        ...


@dataclass(frozen=True, slots=True)
class ConfirmSingleOperatorTransition:
    """Test/headless controller that never guesses between operator branches."""

    def confirm_phase_entry(
        self,
        phase: PlannedPhase,
        attempt: int,
        available_artifacts: tuple[str, ...],
    ) -> bool:
        return True

    def select_transition(
        self,
        phase: PlannedPhase,
        candidates: tuple[PlannedTransition, ...],
        result: GraphPhaseResult,
    ) -> str | None:
        return candidates[0].target_phase if len(candidates) == 1 else None


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    criterion_id: str
    metric_id: str
    operator: str
    expected: Any
    observed: Any
    passed: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "criterion_id": self.criterion_id,
            "metric_id": self.metric_id,
            "operator": self.operator,
            "expected": self.expected,
            "observed": self.observed,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class PhaseAttempt:
    phase_id: str
    attempt: int
    result: GraphPhaseResult
    entry_snapshot_hash: str
    acceptance: tuple[AcceptanceResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase_id", require_identifier(self.phase_id, "phase_id"))
        object.__setattr__(self, "attempt", int(self.attempt))
        if self.attempt <= 0:
            raise ValueError("phase attempt must be positive")

    def to_payload(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "attempt": self.attempt,
            "status": self.result.status.value,
            "failure": self.result.failure,
            "entry_snapshot_hash": self.entry_snapshot_hash,
            "acceptance": [value.to_payload() for value in self.acceptance],
        }


@dataclass(frozen=True, slots=True)
class PhaseTransitionRecord:
    source_phase: str
    target_phase: str
    condition: str
    attempt: int
    transition_time: TimePoint

    def __post_init__(self) -> None:
        for field_name in ("source_phase", "target_phase", "condition"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "attempt", int(self.attempt))
        if self.attempt <= 0:
            raise ValueError("transition attempt must be positive")

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_phase": self.source_phase,
            "target_phase": self.target_phase,
            "condition": self.condition,
            "attempt": self.attempt,
            "transition_time": self.transition_time.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class EngineRunResult:
    execution_id: str
    plan_hash: str
    status: EngineStatus
    equivalence_ceiling: EquivalenceLevel
    phase_attempts: tuple[PhaseAttempt, ...]
    transitions: tuple[PhaseTransitionRecord, ...]
    evidence: tuple[EvidenceRecord, ...]
    artifacts: tuple[ArtifactReference | ArtifactPublication, ...]
    terminal_phase: str | None
    reason: str | None = None
    checkpoint: EngineCheckpoint | None = None

    @property
    def captured_packets(self) -> tuple[Packet, ...]:
        return tuple(
            value
            for attempt in self.phase_attempts
            for value in attempt.result.admitted_inputs
            if isinstance(value, (DenseSampleBatch, SparseEventBatch, MetadataEvent))
        )

    @property
    def work(self) -> tuple[Any, ...]:
        return tuple(
            value
            for attempt in self.phase_attempts
            for value in attempt.result.work
        )

    @property
    def failure(self) -> str | None:
        return self.reason if self.status == EngineStatus.FAILED else None

    @property
    def phase_results(self) -> tuple[GraphPhaseResult, ...]:
        return tuple(value.result for value in self.phase_attempts)

    @property
    def artifact_ids(self) -> tuple[str, ...]:
        return tuple(sorted(value.artifact_id for value in self.artifacts))


class ExecutionEngine:
    """Execute the generic typed graph and phase machine from one locked plan.

    This is the Phase 5 compiler/runtime join. It intentionally does not impose
    a classifier topology: a phase may contain sources and sinks only, a pure
    processing route, models without a policy, or a complete closed-loop route.
    """

    def __init__(
        self,
        runtime: PlanRuntime,
        *,
        execution_id: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.plan = runtime.plan
        self.graph = PlanGraphExecutor(runtime, execution_id=execution_id)
        self._consumed = False

    @classmethod
    def from_plan(
        cls,
        plan: ExecutionPlan,
        registry: PluginRegistry,
        *,
        execution_id: str | None = None,
        proxy_factory: ComponentProxyFactory | None = None,
        component_overrides: Mapping[str, Any] | None = None,
    ) -> "ExecutionEngine":
        return cls(
            construct_plan_runtime(
                plan,
                registry,
                proxy_factory=proxy_factory,
                component_overrides=component_overrides,
            ),
            execution_id=execution_id,
        )

    def cancel(self) -> None:
        self.graph.cancel()

    def run(
        self,
        *,
        inputs_by_phase: Mapping[str, tuple[GraphInput, ...]] | None = None,
        artifacts: Mapping[str, ArtifactReference | ArtifactPublication] | None = None,
        operator: OperatorController | None = None,
        checkpoint: EngineCheckpoint | None = None,
        checkpoint_after_inputs_by_phase: Mapping[str, int] | None = None,
    ) -> EngineRunResult:
        if self._consumed:
            raise RuntimeError("a plan execution engine can run its constructed runtime once")
        self._consumed = True
        phase_inputs = dict(inputs_by_phase or {})
        checkpoint_thresholds = dict(checkpoint_after_inputs_by_phase or {})
        available_artifacts = self._validate_initial_artifacts(artifacts or {})
        known_phases = {value.phase_id for value in self.plan.phases}
        unknown_inputs = sorted(set(phase_inputs) - known_phases)
        if unknown_inputs:
            raise KeyError(f"inputs reference unknown phases: {unknown_inputs}")
        unknown_checkpoints = sorted(set(checkpoint_thresholds) - known_phases)
        if unknown_checkpoints:
            raise KeyError(
                f"checkpoint thresholds reference unknown phases: {unknown_checkpoints}"
            )
        attempts: list[PhaseAttempt] = []
        transitions: list[PhaseTransitionRecord] = []
        evidence: list[EvidenceRecord] = []
        status = EngineStatus.FAILED
        reason: str | None = None
        terminal_phase: str | None = self.plan.initial_phase
        current_phase = self.plan.initial_phase
        resumed_phase_id: str | None = None
        output_checkpoint: EngineCheckpoint | None = None
        if checkpoint is not None:
            current_phase = self.graph.restore_checkpoint(checkpoint)
            resumed_phase_id = current_phase
            self._restore_checkpoint_artifacts(checkpoint, available_artifacts)
            resumed_phase = self._phase(current_phase)
            if resumed_phase.resume_policy != "checkpoint":
                raise ValueError(
                    f"phase {current_phase} does not permit checkpoint restoration"
                )
        validation = self.plan.validation_rules.get("suite", {})
        max_transitions = int(
            validation.get("max_phase_transitions", max(32, len(self.plan.phases) * 4))
        )
        if max_transitions <= 0:
            raise ValueError("max_phase_transitions must be positive")
        try:
            if current_phase is None:
                reason = "compiled plan does not declare an initial phase"
                evidence.append(self.graph.record_event("plan_blocked", {"reason": reason}))
                status = EngineStatus.BLOCKED
            while current_phase is not None and reason is None:
                phase = self._phase(current_phase)
                terminal_phase = phase.phase_id
                missing = tuple(
                    sorted(set(phase.required_artifacts) - set(available_artifacts))
                )
                if missing:
                    reason = (
                        f"phase {phase.phase_id} is missing required artifacts: "
                        + ", ".join(missing)
                    )
                    evidence.append(
                        self.graph.record_event(
                            "phase_blocked",
                            {
                                "phase_id": phase.phase_id,
                                "reason": "missing_artifacts",
                                "artifact_ids": list(missing),
                            },
                        )
                    )
                    status = EngineStatus.BLOCKED
                    break
                # A safe-boundary checkpoint can only have been produced after
                # phase entry was authorized. Requiring a second confirmation
                # on fresh-process restore would change the recorded phase
                # semantics and make unattended recovery impossible.
                if phase.operator_confirmation and phase.phase_id != resumed_phase_id:
                    if operator is None:
                        reason = f"phase {phase.phase_id} requires operator confirmation"
                        evidence.append(
                            self.graph.record_event(
                                "phase_blocked",
                                {
                                    "phase_id": phase.phase_id,
                                    "reason": "operator_confirmation_required",
                                },
                            )
                        )
                        status = EngineStatus.BLOCKED
                        break
                    if not operator.confirm_phase_entry(
                        phase, 1, tuple(sorted(available_artifacts))
                    ):
                        reason = f"operator declined phase {phase.phase_id}"
                        evidence.append(
                            self.graph.record_event(
                                "phase_blocked",
                                {
                                    "phase_id": phase.phase_id,
                                    "reason": "operator_declined",
                                },
                            )
                        )
                        status = EngineStatus.BLOCKED
                        break
                runtime_snapshot = self.runtime.snapshot_state()
                executor_state = self.graph.snapshot_component_state()
                attempt_number = 0
                result: GraphPhaseResult | None = None
                while True:
                    attempt_number += 1
                    result = self.graph.run_phase(
                        phase,
                        inputs=tuple(phase_inputs.get(phase.phase_id, ())),
                        checkpoint_after_inputs=checkpoint_thresholds.get(
                            phase.phase_id
                        ),
                    )
                    evidence.extend(result.evidence)
                    acceptance = self._evaluate_acceptance(phase, result)
                    for value in acceptance:
                        evidence.append(
                            self.graph.record_event(
                                "acceptance_result", value.to_payload()
                            )
                        )
                    attempts.append(
                        PhaseAttempt(
                            phase_id=phase.phase_id,
                            attempt=attempt_number,
                            result=result,
                            entry_snapshot_hash=runtime_snapshot.snapshot_hash,
                            acceptance=acceptance,
                        )
                    )
                    if result.status != GraphRunStatus.FAILED:
                        break
                    if attempt_number > phase.retry_limit:
                        break
                    if phase.resume_policy == "forbidden":
                        break
                    self.runtime.restore_state(runtime_snapshot)
                    self.graph.restore_component_state(executor_state)
                    evidence.append(
                        self.graph.record_event(
                            "phase_retry",
                            {
                                "phase_id": phase.phase_id,
                                "completed_attempt": attempt_number,
                                "next_attempt": attempt_number + 1,
                                "resume_policy": phase.resume_policy,
                                "entry_snapshot_hash": runtime_snapshot.snapshot_hash,
                            },
                        )
                    )
                assert result is not None
                resumed_phase_id = None
                if result.status == GraphRunStatus.CANCELLED:
                    status = EngineStatus.CANCELLED
                    break
                if result.status == GraphRunStatus.CHECKPOINTED:
                    status = EngineStatus.CHECKPOINTED
                    if result.checkpoint is None:
                        raise RuntimeError("checkpointed phase did not return a checkpoint")
                    output_checkpoint = self._checkpoint_with_artifacts(
                        result.checkpoint,
                        available_artifacts,
                    )
                    break
                if result.status == GraphRunStatus.PARTIAL:
                    status = EngineStatus.PARTIAL
                    reason = f"phase {phase.phase_id} ended without source completion"
                    break
                acceptance_failed = any(
                    not value.passed for value in attempts[-1].acceptance
                )
                if result.status == GraphRunStatus.COMPLETE:
                    for emission in result.emissions:
                        publication = emission.value
                        if not isinstance(publication, ArtifactPublication):
                            continue
                        self._register_publication(
                            phase,
                            emission.component_id,
                            emission.output_port,
                            publication,
                            available_artifacts,
                        )
                        evidence.append(
                            self.graph.record_event(
                                "artifact_registered",
                                {
                                    "phase_id": phase.phase_id,
                                    "artifact_id": publication.artifact_id,
                                    "digest": publication.reference.digest,
                                    "producer_component": emission.component_id,
                                    "producer_port": emission.output_port,
                                },
                            )
                        )
                if result.status == GraphRunStatus.TIMED_OUT:
                    condition = "timeout"
                elif acceptance_failed:
                    condition = "acceptance_failed"
                else:
                    condition = (
                        "complete"
                        if result.status == GraphRunStatus.COMPLETE
                        else "failed"
                    )
                selected = self._transition_for(phase, condition)
                if selected is None:
                    operator_candidates = tuple(
                        value for value in phase.transitions if value.condition == "operator"
                    )
                    if operator_candidates:
                        if operator is None:
                            status = EngineStatus.BLOCKED
                            reason = (
                                f"phase {phase.phase_id} requires an operator transition"
                            )
                            evidence.append(
                                self.graph.record_event(
                                    "phase_blocked",
                                    {
                                        "phase_id": phase.phase_id,
                                        "reason": "operator_transition_required",
                                        "targets": [
                                            value.target_phase
                                            for value in operator_candidates
                                        ],
                                    },
                                )
                            )
                            break
                        target = operator.select_transition(
                            phase, operator_candidates, result
                        )
                        selected = next(
                            (
                                value
                                for value in operator_candidates
                                if value.target_phase == target
                            ),
                            None,
                        )
                        if selected is None:
                            status = EngineStatus.BLOCKED
                            reason = (
                                f"operator did not select a valid transition from "
                                f"{phase.phase_id}"
                            )
                            evidence.append(
                                self.graph.record_event(
                                    "phase_blocked",
                                    {
                                        "phase_id": phase.phase_id,
                                        "reason": "operator_transition_not_selected",
                                    },
                                )
                            )
                            break
                if selected is None:
                    status = (
                        EngineStatus.COMPLETE
                        if result.status == GraphRunStatus.COMPLETE
                        and not acceptance_failed
                        else EngineStatus.TIMED_OUT
                        if result.status == GraphRunStatus.TIMED_OUT
                        else EngineStatus.FAILED
                    )
                    if status == EngineStatus.FAILED:
                        reason = (
                            f"phase {phase.phase_id} failed acceptance criteria"
                            if acceptance_failed
                            else result.failure or f"phase {phase.phase_id} failed"
                        )
                    elif status == EngineStatus.TIMED_OUT:
                        reason = result.failure
                    current_phase = None
                    continue
                if len(transitions) >= max_transitions:
                    status = EngineStatus.FAILED
                    reason = f"execution exceeded max_phase_transitions={max_transitions}"
                    evidence.append(
                        self.graph.record_event(
                            "plan_failure", {"reason": reason}
                        )
                    )
                    break
                transition = PhaseTransitionRecord(
                    source_phase=phase.phase_id,
                    target_phase=selected.target_phase,
                    condition=selected.condition,
                    attempt=attempt_number,
                    transition_time=self.graph.current_time,
                )
                transitions.append(transition)
                evidence.append(
                    self.graph.record_event(
                        "phase_transition", transition.to_payload()
                    )
                )
                current_phase = selected.target_phase
        except Exception as exc:
            status = EngineStatus.FAILED
            reason = f"{type(exc).__name__}: {exc}"
            evidence.append(self.graph.record_event("plan_failure", {"reason": reason}))
        finally:
            close_failures = self.runtime.close()
            if close_failures:
                status = EngineStatus.FAILED
                close_reason = "; ".join(close_failures)
                reason = close_reason if reason is None else f"{reason}; {close_reason}"
                evidence.append(
                    self.graph.record_event(
                        "runtime_close_failure", {"failures": list(close_failures)}
                    )
                )
        return EngineRunResult(
            execution_id=self.graph.execution_id,
            plan_hash=self.plan.plan_hash,
            status=status,
            equivalence_ceiling=self.runtime.equivalence_ceiling,
            phase_attempts=tuple(attempts),
            transitions=tuple(transitions),
            evidence=tuple(evidence),
            artifacts=tuple(
                available_artifacts[key] for key in sorted(available_artifacts)
            ),
            terminal_phase=terminal_phase,
            reason=reason,
            checkpoint=output_checkpoint,
        )

    def _evaluate_acceptance(
        self,
        phase: PlannedPhase,
        result: GraphPhaseResult,
    ) -> tuple[AcceptanceResult, ...]:
        if result.status != GraphRunStatus.COMPLETE or not phase.acceptance_criteria:
            return ()
        metrics = {
            str(value["metric_id"]): value
            for value in self.plan.validation_rules.get("metrics", ())
        }
        criteria = {
            str(value["criterion_id"]): value
            for value in self.plan.validation_rules.get("acceptance", ())
        }
        values: list[AcceptanceResult] = []
        for criterion_id in phase.acceptance_criteria:
            criterion = criteria[criterion_id]
            metric_id = str(criterion["metric_id"])
            observed = self._measure(metrics[metric_id], result)
            operator = str(criterion["operator"])
            expected = criterion["value"]
            passed = self._compare(operator, observed, expected)
            values.append(
                AcceptanceResult(
                    criterion_id=criterion_id,
                    metric_id=metric_id,
                    operator=operator,
                    expected=expected,
                    observed=observed,
                    passed=passed,
                )
            )
        return tuple(values)

    @staticmethod
    def _measure(metric: Mapping[str, Any], result: GraphPhaseResult) -> Any:
        measure = str(metric["measure"])
        parameters = metric.get("parameters") or {}
        if measure == "input_count":
            return len(result.admitted_inputs)
        if measure == "emission_count":
            return len(result.emissions)
        if measure == "work_count":
            return len(result.work)
        if measure == "artifact_count":
            return len(result.artifacts)
        if measure == "component_emission_count":
            component_id = str(parameters["component_id"])
            port = parameters.get("port")
            return sum(
                value.component_id == component_id
                and (port is None or value.output_port == port)
                for value in result.emissions
            )
        if measure == "work_status_count":
            status = str(parameters["status"])
            component_id = parameters.get("component_id")
            return sum(
                value.status.value == status
                and (component_id is None or value.component_id == component_id)
                for value in result.work
            )
        if measure == "prediction_coverage":
            numerator = str(parameters["prediction_component"])
            denominator = str(parameters["window_component"])
            predictions = sum(
                value.component_id == numerator for value in result.emissions
            )
            windows = sum(
                value.component_id == denominator for value in result.emissions
            )
            return 1.0 if windows == 0 else predictions / windows
        raise ValueError(f"unsupported acceptance metric measure: {measure}")

    @staticmethod
    def _compare(operator: str, observed: Any, expected: Any) -> bool:
        if operator == "lt":
            return bool(observed < expected)
        if operator == "lte":
            return bool(observed <= expected)
        if operator == "gt":
            return bool(observed > expected)
        if operator == "gte":
            return bool(observed >= expected)
        if operator == "eq":
            return bool(observed == expected)
        raise ValueError(f"unsupported acceptance operator: {operator}")

    def _phase(self, phase_id: str) -> PlannedPhase:
        for phase in self.plan.phases:
            if phase.phase_id == phase_id:
                return phase
        raise KeyError(f"unknown planned phase: {phase_id}")

    def _validate_initial_artifacts(
        self,
        artifacts: Mapping[str, ArtifactReference | ArtifactPublication],
    ) -> dict[str, ArtifactReference | ArtifactPublication]:
        declarations = {value.artifact_id: value for value in self.plan.artifacts}
        validated: dict[str, ArtifactReference | ArtifactPublication] = {}
        for artifact_id, value in artifacts.items():
            if not isinstance(value, (ArtifactReference, ArtifactPublication)):
                raise TypeError(
                    f"initial artifact {artifact_id} must be an ArtifactReference or "
                    "ArtifactPublication"
                )
            reference = value.reference if isinstance(value, ArtifactPublication) else value
            if artifact_id != reference.artifact_id:
                raise ValueError(
                    f"initial artifact key {artifact_id} differs from reference "
                    f"{reference.artifact_id}"
                )
            declaration = declarations.get(artifact_id)
            if declaration is None:
                raise ValueError(f"initial artifact {artifact_id} is not declared by the plan")
            if not declaration.external:
                raise ValueError(
                    f"initial artifact {artifact_id} is declared as graph-produced; "
                    "declare a separate external artifact for pre-existing inputs"
                )
            self._validate_artifact_reference(declaration, reference)
            validated[artifact_id] = value
        return validated

    def _checkpoint_with_artifacts(
        self,
        checkpoint: EngineCheckpoint,
        artifacts: Mapping[str, ArtifactReference | ArtifactPublication],
    ) -> EngineCheckpoint:
        state = thaw_json(checkpoint.state)
        serialized: list[dict[str, Any]] = []
        for artifact_id in sorted(artifacts):
            value = artifacts[artifact_id]
            payload = value.to_payload()
            if isinstance(value, ArtifactPublication):
                materialized = value.materialized_payload
                if materialized is not None:
                    payload["materialized_payload"] = thaw_json(materialized)
                kind = "publication"
            else:
                kind = "reference"
            serialized.append({"kind": kind, "payload": payload})
        state["available_artifacts"] = serialized
        return EngineCheckpoint(
            checkpoint_id=checkpoint.checkpoint_id,
            execution_id=checkpoint.execution_id,
            plan_hash=checkpoint.plan_hash,
            created_time=checkpoint.created_time,
            evidence_prefix_digest=checkpoint.evidence_prefix_digest,
            state=state,
            engine_implementation=checkpoint.engine_implementation,
            schema=checkpoint.schema,
        )

    def _restore_checkpoint_artifacts(
        self,
        checkpoint: EngineCheckpoint,
        available: dict[str, ArtifactReference | ArtifactPublication],
    ) -> None:
        declarations = {value.artifact_id: value for value in self.plan.artifacts}
        state = thaw_json(checkpoint.state)
        for index, entry in enumerate(state.get("available_artifacts", ())):
            if not isinstance(entry, Mapping):
                raise TypeError(
                    f"checkpoint available_artifacts[{index}] must be an object"
                )
            payload = dict(entry.get("payload") or {})
            kind = str(entry.get("kind"))
            if kind == "reference":
                value: ArtifactReference | ArtifactPublication = (
                    ArtifactReference.from_payload(payload)
                )
            elif kind == "publication":
                materialized = payload.pop("materialized_payload", None)
                publication = ArtifactPublication.from_payload(payload)
                value = (
                    publication
                    if materialized is None
                    else replace(publication, materialized_payload=materialized)
                )
            else:
                raise ValueError(
                    f"checkpoint available_artifacts[{index}] has unknown kind {kind!r}"
                )
            reference = value.reference if isinstance(value, ArtifactPublication) else value
            declaration = declarations.get(reference.artifact_id)
            if declaration is None:
                raise ValueError(
                    f"checkpoint artifact {reference.artifact_id} is not declared by the plan"
                )
            self._validate_artifact_reference(declaration, reference)
            existing = available.get(reference.artifact_id)
            if existing is not None:
                existing_reference = (
                    existing.reference
                    if isinstance(existing, ArtifactPublication)
                    else existing
                )
                if existing_reference.digest != reference.digest:
                    raise ValueError(
                        f"checkpoint artifact {reference.artifact_id} conflicts with "
                        "the supplied initial artifact"
                    )
            available[reference.artifact_id] = value

    def _register_publication(
        self,
        phase: PlannedPhase,
        component_id: str,
        output_port: str,
        publication: ArtifactPublication,
        available: dict[str, ArtifactReference | ArtifactPublication],
    ) -> None:
        declaration = next(
            (
                value
                for value in self.plan.artifacts
                if value.artifact_id == publication.artifact_id
            ),
            None,
        )
        if declaration is None:
            raise ValueError(
                f"component {component_id} published undeclared artifact "
                f"{publication.artifact_id}"
            )
        expected_endpoint = (
            declaration.producer_phase,
            declaration.producer_component,
            declaration.producer_port,
        )
        observed_endpoint = (phase.phase_id, component_id, output_port)
        if expected_endpoint != observed_endpoint:
            raise ValueError(
                f"artifact {publication.artifact_id} was published by "
                f"{'.'.join(observed_endpoint)}; expected "
                f"{'.'.join(str(value) for value in expected_endpoint)}"
            )
        if publication.producer_component_id != component_id:
            raise ValueError(
                "artifact publication producer identity differs from graph emission"
            )
        self._validate_artifact_reference(declaration, publication.reference)
        existing = available.get(publication.artifact_id)
        if existing is not None:
            reference = (
                existing.reference
                if isinstance(existing, ArtifactPublication)
                else existing
            )
            if reference.digest != publication.reference.digest:
                raise ValueError(
                    f"artifact {publication.artifact_id} was republished with a different digest"
                )
            return
        available[publication.artifact_id] = publication

    @staticmethod
    def _validate_artifact_reference(declaration: Any, reference: ArtifactReference) -> None:
        if reference.role != declaration.role:
            raise ValueError(
                f"artifact {reference.artifact_id} role {reference.role} differs from "
                f"declared role {declaration.role}"
            )
        if reference.media_type != declaration.media_type:
            raise ValueError(
                f"artifact {reference.artifact_id} media type {reference.media_type} differs "
                f"from declared {declaration.media_type}"
            )
        if (
            declaration.expected_digest is not None
            and reference.digest != declaration.expected_digest
        ):
            raise ValueError(
                f"artifact {reference.artifact_id} digest {reference.digest} differs from "
                f"locked digest {declaration.expected_digest}"
            )

    @staticmethod
    def _transition_for(
        phase: PlannedPhase,
        condition: str,
    ) -> PlannedTransition | None:
        matches = tuple(
            value for value in phase.transitions if value.condition == condition
        )
        if len(matches) > 1:
            raise RuntimeError(
                f"compiled phase {phase.phase_id} has ambiguous {condition} transitions"
            )
        return matches[0] if matches else None
