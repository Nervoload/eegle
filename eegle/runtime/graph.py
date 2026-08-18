"""Generic deterministic execution of the typed graph locked in a plan."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from threading import Lock
from time import monotonic, sleep
from typing import Any

from eegle._domain import ComponentKind, WorkStatus
from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.actions.authorization import (
    ActionDisposition,
    ActionDispositionStatus,
)
from eegle.actions.commands import ActionRequest, AuthorizedCommand
from eegle.actions.receipts import ActionReceipt
from eegle.compiler.lock import canonical_hash
from eegle.compiler.plan import (
    PlannedModelFailureDisposition,
    PlannedModelQueueDisposition,
    PlannedPhase,
)
from eegle.models.input_safety import causal_model_input_rejection
from eegle.models.predictions import Prediction
from eegle.recording.evidence import EvidenceRecord
from eegle.recording.publications import ArtifactPublication
from eegle.runtime.action_broker import (
    ActionBroker,
    BrokerOutcome,
    PendingAuthorization,
)
from eegle.runtime.admission import (
    SourceAdmissionState,
    poll_source,
    watermark_blockers,
)
from eegle.runtime.canonical_state import capture_canonical_state
from eegle.runtime.checkpoints import EngineCheckpoint
from eegle.runtime.context import DeterministicIdSource, RuntimeExecutionContext
from eegle.runtime.model_runtime import (
    ModelComparison,
    ModelComparisonStatus,
    ModelResultDisposition,
    ModelResultDispositionStatus,
    ModelResultRejected,
)
from eegle.runtime.outcome_coordinator import OutcomeCoordinator, OutcomeMatch
from eegle.runtime.outcomes import Outcome, OutcomeDisposition, OutcomeUse
from eegle.runtime.plan_runtime import (
    PlanRuntime,
    PlanRuntimeSnapshot,
    RuntimeNode,
)
from eegle.runtime.queueing import EventQueue, PendingEmission, QueuedEvent
from eegle.runtime.retention import PhaseRecordBuffer
from eegle.runtime.routing import (
    available_time,
    dispatch_component,
    sequence_key,
    validate_input_port,
    value_id,
    value_payload,
    value_type,
)
from eegle.runtime.scheduling import ScheduledTrigger, SchedulingPolicy, TriggerResult
from eegle.runtime.state import (
    AdaptationEligibilityDecision,
    AdaptationEligibilityStatus,
    AdaptationResult,
    StateTransition,
    TransitionStatus,
    WorkRecord,
)
from eegle.runtime.work import (
    PendingWork,
    completed_work,
    component_deadline,
    failed_work,
)
from eegle.streams.clocks import TimePoint
from eegle.streams.packets import (
    DenseSampleBatch,
    MetadataEvent,
    Packet,
    SparseEventBatch,
)

_SOURCE_OBSERVATION_RECORD_TYPES = frozenset(
    {"source_clock_observation", "source_packet_loss", "source_reconnect"}
)


class GraphRunStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    CHECKPOINTED = "checkpointed"


@dataclass(frozen=True, slots=True)
class GraphInput:
    target_component: str
    target_port: str
    value: Any

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "target_component",
            require_identifier(self.target_component, "target_component"),
        )
        object.__setattr__(
            self, "target_port", require_identifier(self.target_port, "target_port")
        )


@dataclass(frozen=True, slots=True)
class WorkSchedulingDisposition:
    rejected_reason: str | None = None
    evicted_event: QueuedEvent | None = None


@dataclass(frozen=True, slots=True)
class GraphEmission:
    emission_id: str
    component_id: str
    output_port: str
    value_id: str
    value_type: str
    available_time: TimePoint
    input_ids: tuple[str, ...]
    value_hash: str
    value: Any

    def __post_init__(self) -> None:
        for field_name in (
            "emission_id",
            "component_id",
            "output_port",
            "value_id",
            "value_type",
        ):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "input_ids",
            tuple(require_identifier(value, "input_id") for value in self.input_ids),
        )
        object.__setattr__(self, "value_hash", require_digest(self.value_hash, "value_hash"))

    @property
    def endpoint(self) -> str:
        return f"{self.component_id}.{self.output_port}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "emission_id": self.emission_id,
            "component_id": self.component_id,
            "output_port": self.output_port,
            "value_id": self.value_id,
            "value_type": self.value_type,
            "available_time": self.available_time.to_payload(),
            "input_ids": list(self.input_ids),
            "value_hash": self.value_hash,
            "value": value_payload(self.value),
        }


@dataclass(frozen=True, slots=True)
class GraphPhaseResult:
    execution_id: str
    plan_hash: str
    phase_id: str
    status: GraphRunStatus
    evidence: tuple[EvidenceRecord, ...]
    admitted_inputs: tuple[Any, ...]
    emissions: tuple[GraphEmission, ...]
    work: tuple[WorkRecord, ...]
    artifacts: tuple[ArtifactPublication, ...] = ()
    failure: str | None = None
    checkpoint: EngineCheckpoint | None = None
    terminal_reason: str | None = None

    def emissions_from(self, component_id: str, output_port: str) -> tuple[Any, ...]:
        return tuple(
            value.value
            for value in self.emissions
            if value.component_id == component_id and value.output_port == output_port
        )


@dataclass(frozen=True, slots=True)
class GraphRetrySnapshot:
    """Rewindable executor state, excluding monotonic audit identities."""

    state: Mapping[str, Any]


class PlanGraphExecutor:
    """Execute any active phase subgraph from one exact :class:`PlanRuntime`."""

    def __init__(
        self,
        runtime: PlanRuntime,
        *,
        execution_id: str | None = None,
        evidence_sink: Callable[[EvidenceRecord], None] | None = None,
        capture_sink: Callable[[Packet], None] | None = None,
        retain_evidence: bool = True,
        retain_phase_details: bool = True,
    ) -> None:
        self.runtime = runtime
        self.plan = runtime.plan
        self.execution_id = require_identifier(
            execution_id or f"execution.{self.plan.plan_hash[-16:]}",
            "execution_id",
        )
        suite_clock = self.plan.clock_policy.get("suite", {})
        clock_id = suite_clock.get("execution_clock_id")
        if not isinstance(clock_id, str):
            raise ValueError("compiled plan is missing execution_clock_id")
        self.execution_clock_id = require_identifier(clock_id, "execution_clock_id")
        self._evidence_sink = evidence_sink
        self._capture_sink = capture_sink
        self._retain_evidence = bool(retain_evidence)
        self._retain_phase_details = bool(retain_phase_details)
        if self.plan.graph is None:  # PlanRuntime already enforces this boundary.
            raise ValueError("plan graph executor requires a typed compiled graph")
        targets: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for route in self.plan.graph.routes:
            source_component, source_port = route.source.rsplit(".", 1)
            target_component, target_port = route.target.rsplit(".", 1)
            targets.setdefault((source_component, source_port), []).append(
                (target_component, target_port)
            )
        self._targets = {
            key: tuple(sorted(values)) for key, values in targets.items()
        }
        port_contracts = {
            value.endpoint: value.contract for value in self.plan.graph.ports
        }
        routed_input_safety: dict[str, list[str | None]] = {}
        for route in self.plan.graph.routes:
            routed_input_safety.setdefault(route.target, []).append(
                port_contracts[route.source].model_input_safety
            )
        self._model_input_safety = {
            target: (
                "label_bearing"
                if "label_bearing" in declarations
                else "label_blind"
                if declarations
                and all(value == "label_blind" for value in declarations)
                else None
            )
            for target, declarations in routed_input_safety.items()
        }
        self._component_order = {
            component_id: index
            for index, component_id in enumerate(self.plan.graph.component_order)
        }
        validation = self.plan.validation_rules.get("suite", {})
        self.max_events = int(validation.get("max_graph_events", 100_000))
        self.max_pending_events = int(validation.get("max_pending_events", 1_024))
        self.max_idle_cycles = int(validation.get("max_idle_cycles", 1))
        scheduling = self.plan.scheduling_policy
        self.scheduling = SchedulingPolicy(
            execution_clock_id=self.execution_clock_id,
            max_pending_packets=self.max_pending_events,
            allowed_lateness_seconds=float(
                scheduling.get("allowed_lateness_seconds", 0.0)
            ),
            backpressure=str(scheduling.get("backpressure", "fail_run")),
            lateness=str(scheduling.get("lateness", "reject")),
            max_idle_cycles=self.max_idle_cycles,
        )
        self.backpressure = self.scheduling.backpressure.value
        self.allowed_lateness_seconds = self.scheduling.allowed_lateness_seconds
        self.lateness = self.scheduling.lateness.value
        if (
            self.max_events <= 0
            or self.max_pending_events <= 0
            or self.max_idle_cycles <= 0
        ):
            raise ValueError("graph event, queue, and idle limits must be positive")
        raw_deadlines = validation.get("component_deadlines_seconds", {})
        if not isinstance(raw_deadlines, Mapping):
            raise TypeError("component_deadlines_seconds must be an object")
        known_components = {value.component_id for value in self.plan.components}
        unknown_deadlines = set(raw_deadlines) - known_components
        if unknown_deadlines:
            raise ValueError(
                "component deadlines reference unknown components: "
                f"{sorted(unknown_deadlines)}"
            )
        self.component_deadlines: dict[str, float] = {}
        for component_id, raw_value in raw_deadlines.items():
            value = float(raw_value)
            if not math.isfinite(value) or value < 0:
                raise ValueError("component deadlines must be finite and non-negative")
            self.component_deadlines[str(component_id)] = value
        self._ids = DeterministicIdSource()
        self._action_broker = ActionBroker(runtime, self._ids.next)
        self._evidence_sequence = 0
        self._event_sequence = 0
        self._current_time = TimePoint(0.0, self.execution_clock_id)
        self._component_state: dict[str, dict[str, Any]] = {}
        self._admitted_input_lineage: dict[str, tuple[str, ...]] = {}
        self._emitted_predictions: dict[str, Prediction] = {}
        comparison_members: dict[str, list[str]] = {}
        for binding in self.plan.model_bindings:
            if binding.comparison_group is not None:
                comparison_members.setdefault(binding.comparison_group, []).append(
                    binding.component_id
                )
        self._comparison_members = {
            key: tuple(sorted(values))
            for key, values in comparison_members.items()
        }
        self._pending_model_comparisons: dict[
            str, dict[str, Prediction]
        ] = {}
        self._outcomes = OutcomeCoordinator(
            self.plan.outcome_expectations,
            self._ids.next,
        )
        self._adaptations = {
            value.expectation_id: value for value in self.plan.adaptations
        }
        self._clock_mapping_revisions = {
            str(key): int(value)
            for key, value in self.plan.clock_policy.get(
                "mapping_revisions", {}
            ).items()
        }
        self._control_lock = Lock()
        self._terminal_action: str | None = None
        self._terminal_reason: str | None = None
        self._control_recorded = False
        self._fired_state_rules: set[str] = set()
        self._resumed_phase_id: str | None = None
        self._evidence_prefix_digest: str | None = None
        self._resumed_phase_started_time: TimePoint | None = None

    @property
    def current_time(self) -> TimePoint:
        return self._current_time

    def record_event(
        self,
        record_type: str,
        payload: Mapping[str, Any],
    ) -> EvidenceRecord:
        """Create an orchestration record in the same deterministic evidence stream."""

        return self._emit([], record_type, payload)

    def snapshot_component_state(self) -> Mapping[str, Any]:
        """Capture executor-owned policy/adapter state at a phase boundary."""

        return freeze_json(self._component_state)

    def restore_component_state(self, state: Mapping[str, Any]) -> None:
        restored = thaw_json(freeze_json(state))
        if not isinstance(restored, dict):  # pragma: no cover - defensive type boundary
            raise TypeError("graph component state must be an object")
        self._component_state = {
            str(component_id): dict(value)
            for component_id, value in restored.items()
        }

    def snapshot_retry_state(self) -> GraphRetrySnapshot:
        """Capture semantic executor state at a retry-safe phase boundary.

        Evidence sequence numbers and generated IDs intentionally remain outside
        this snapshot: records from a failed attempt stay in the audit trail and
        identities emitted by the next attempt must not collide with them.
        """

        return GraphRetrySnapshot(
            freeze_json(
                {
                    "current_time": self._current_time.to_payload(),
                    "event_sequence": self._event_sequence,
                    "component_state": thaw_json(self.snapshot_component_state()),
                    "clock_mapping_revisions": dict(self._clock_mapping_revisions),
                    "fired_state_rules": sorted(self._fired_state_rules),
                    "admitted_input_lineage": {
                        key: list(value)
                        for key, value in sorted(self._admitted_input_lineage.items())
                    },
                    "emitted_predictions": {
                        key: value.to_payload()
                        for key, value in sorted(self._emitted_predictions.items())
                    },
                    "pending_model_comparisons": {
                        key: {
                            component_id: prediction.to_payload()
                            for component_id, prediction in sorted(value.items())
                        }
                        for key, value in sorted(
                            self._pending_model_comparisons.items()
                        )
                    },
                    "outcome_lifecycle": thaw_json(self._outcomes.snapshot()),
                    "cancel_requested": self._control_request()[0] == "cancel",
                    "resumed_phase_id": self._resumed_phase_id,
                    "resumed_phase_started_time": (
                        None
                        if self._resumed_phase_started_time is None
                        else self._resumed_phase_started_time.to_payload()
                    ),
                }
            )
        )

    def restore_retry_state(self, snapshot: GraphRetrySnapshot) -> None:
        """Restore all rewindable executor state after a failed attempt."""

        if not isinstance(snapshot, GraphRetrySnapshot):
            raise TypeError("retry state must be a GraphRetrySnapshot")
        state = thaw_json(snapshot.state)
        self._current_time = TimePoint.from_payload(state["current_time"])
        self._require_execution_time(self._current_time)
        self._event_sequence = int(state["event_sequence"])
        self.restore_component_state(state.get("component_state") or {})
        self._clock_mapping_revisions = {
            str(key): int(value)
            for key, value in dict(state["clock_mapping_revisions"]).items()
        }
        self._fired_state_rules = {
            require_identifier(str(value), "state trigger rule")
            for value in state.get("fired_state_rules", ())
        }
        self._admitted_input_lineage = {
            require_identifier(str(key), "lineage value_id"): tuple(
                require_identifier(str(item), "admitted input_id") for item in value
            )
            for key, value in dict(
                state.get("admitted_input_lineage") or {}
            ).items()
        }
        self._emitted_predictions = {
            require_identifier(str(key), "prediction_id"): Prediction.from_payload(
                value
            )
            for key, value in dict(
                state.get("emitted_predictions") or {}
            ).items()
        }
        self._pending_model_comparisons = {
            str(key): {
                require_identifier(str(component_id), "comparison component"): (
                    Prediction.from_payload(prediction)
                )
                for component_id, prediction in dict(value).items()
            }
            for key, value in dict(
                state.get("pending_model_comparisons") or {}
            ).items()
        }
        self._outcomes.restore(state.get("outcome_lifecycle") or {})
        if bool(state.get("cancel_requested", False)):
            self.cancel("restored_cancel_request")
        resumed_phase_id = state.get("resumed_phase_id")
        self._resumed_phase_id = (
            None
            if resumed_phase_id is None
            else require_identifier(str(resumed_phase_id), "resumed_phase_id")
        )
        resumed_time = state.get("resumed_phase_started_time")
        self._resumed_phase_started_time = (
            None if resumed_time is None else TimePoint.from_payload(resumed_time)
        )

    def restore_checkpoint(self, checkpoint: EngineCheckpoint) -> str:
        """Restore a fresh executor at a persisted, queue-empty phase boundary."""

        if checkpoint.plan_hash != self.plan.plan_hash:
            raise ValueError("engine checkpoint belongs to a different plan")
        if checkpoint.execution_id != self.execution_id:
            raise ValueError("engine checkpoint execution identity differs from executor")
        state = thaw_json(checkpoint.state)
        phase_id = require_identifier(str(state["phase_id"]), "phase_id")
        if phase_id not in {value.phase_id for value in self.plan.phases}:
            raise ValueError("engine checkpoint references an unknown phase")
        self.runtime.restore_state(
            PlanRuntimeSnapshot.from_payload(state["runtime_snapshot"])
        )
        self.restore_component_state(state.get("component_state") or {})
        executor = dict(state["executor"])
        self._current_time = TimePoint.from_payload(executor["current_time"])
        self._require_execution_time(self._current_time)
        self._ids.restore_state(executor["id_source"])
        self._evidence_sequence = int(executor["evidence_sequence"])
        self._event_sequence = int(executor["event_sequence"])
        self._clock_mapping_revisions = {
            str(key): int(value)
            for key, value in dict(executor["clock_mapping_revisions"]).items()
        }
        self._fired_state_rules = {
            require_identifier(str(value), "state trigger rule")
            for value in executor.get("fired_state_rules", ())
        }
        self._admitted_input_lineage = {
            require_identifier(str(key), "lineage value_id"): tuple(
                require_identifier(str(item), "admitted input_id") for item in value
            )
            for key, value in dict(
                executor.get("admitted_input_lineage") or {}
            ).items()
        }
        self._pending_model_comparisons = {
            str(key): {
                require_identifier(str(component_id), "comparison component"): (
                    Prediction.from_payload(prediction)
                )
                for component_id, prediction in dict(value).items()
            }
            for key, value in dict(
                executor.get("pending_model_comparisons") or {}
            ).items()
        }
        self._outcomes.restore(executor.get("outcome_lifecycle") or {})
        self._evidence_prefix_digest = checkpoint.evidence_prefix_digest
        self._resumed_phase_id = phase_id
        self._resumed_phase_started_time = TimePoint.from_payload(
            executor["phase_started_time"]
        )
        self._require_execution_time(self._resumed_phase_started_time)
        return phase_id

    def cancel(self, reason: str = "cancel_requested") -> bool:
        return self._request_terminal("cancel", reason)

    def complete(self, reason: str = "completion_requested") -> bool:
        return self._request_terminal("complete", reason)

    def _request_terminal(self, action: str, reason: str) -> bool:
        normalized = require_identifier(reason, "run control reason")
        with self._control_lock:
            if self._terminal_action is not None:
                return False
            self._terminal_action = action
            self._terminal_reason = normalized
            return True

    def _control_request(self) -> tuple[str | None, str | None]:
        with self._control_lock:
            return self._terminal_action, self._terminal_reason

    def _observe_control_request(
        self,
        evidence: list[EvidenceRecord],
    ) -> tuple[str | None, str | None]:
        action, reason = self._control_request()
        if action is not None and not self._control_recorded:
            self._emit(
                evidence,
                "run_control_requested",
                {"action": action, "reason": reason},
            )
            self._control_recorded = True
        return action, reason

    def run_phase(
        self,
        phase: PlannedPhase | str,
        *,
        inputs: tuple[GraphInput, ...] = (),
        checkpoint_after_inputs: int | None = None,
    ) -> GraphPhaseResult:
        planned_phase = self._phase(phase)
        resumed = self._resumed_phase_id is not None
        if resumed and self._resumed_phase_id != planned_phase.phase_id:
            raise ValueError("checkpoint phase differs from requested phase")
        if checkpoint_after_inputs is not None:
            checkpoint_after_inputs = int(checkpoint_after_inputs)
            if checkpoint_after_inputs <= 0:
                raise ValueError("checkpoint_after_inputs must be positive")
            if not self._retain_evidence:
                raise ValueError(
                    "in-memory phase evidence is required to create an engine checkpoint"
                )
        active = set(planned_phase.component_ids)
        evidence: list[EvidenceRecord] = []
        retain_details = (
            self._retain_phase_details
            or bool(planned_phase.acceptance_criteria)
            or any(value.condition == "operator" for value in planned_phase.transitions)
        )
        work = PhaseRecordBuffer[WorkRecord](retain_all=retain_details)
        emissions = PhaseRecordBuffer[GraphEmission](
            retain_all=retain_details,
            retain_when=lambda value: isinstance(value.value, ArtifactPublication),
        )
        admitted = PhaseRecordBuffer[Any](retain_all=retain_details)
        queue = EventQueue(
            self.max_pending_events,
            reject_newest=self.backpressure == "reject_newest",
        )
        admission = SourceAdmissionState()
        status = GraphRunStatus.COMPLETE
        failure: str | None = None
        terminal_reason: str | None = None
        lifecycle_started: list[RuntimeNode] = []
        checkpoint_requested = False
        checkpoint: EngineCheckpoint | None = None
        evidence_start = self._evidence_sequence
        phase_started_time = (
            self._resumed_phase_started_time
            if resumed and self._resumed_phase_started_time is not None
            else self._current_time
        )
        phase_deadline = (
            None
            if planned_phase.timeout_seconds is None
            else TimePoint(
                phase_started_time.seconds + planned_phase.timeout_seconds,
                self.execution_clock_id,
            )
        )
        source_nodes = self._ordered_nodes(active, kind=ComponentKind.SOURCE)
        live_source_ids = {
            node.component_id
            for node in source_nodes
            if bool(getattr(node.component, "is_live", False))
        }
        live_wall_deadline = (
            monotonic() + planned_phase.timeout_seconds
            if live_source_ids and planned_phase.timeout_seconds is not None
            else None
        )
        self._emit(
            evidence,
            "phase_resumed" if resumed else "phase_started",
            {
                "phase_id": planned_phase.phase_id,
                "component_ids": sorted(active),
                "timeout_seconds": planned_phase.timeout_seconds,
            },
        )
        try:
            for node in self._ordered_nodes(active):
                start = getattr(node.component, "start", None)
                if callable(start):
                    start(self._context(node))
                lifecycle_started.append(node)
                self._emit(
                    evidence,
                    "component_started",
                    {
                        "phase_id": planned_phase.phase_id,
                        "component_id": node.component_id,
                        "component_version": node.plugin.version,
                    },
                )
            for graph_input in inputs:
                if resumed:
                    raise ValueError("resumed phase cannot accept replacement external inputs")
                if graph_input.target_component not in active:
                    raise ValueError(
                        f"graph input targets inactive component {graph_input.target_component}"
                    )
                node = self.runtime.node(graph_input.target_component)
                validate_input_port(node, graph_input.target_port, graph_input.value)
                graph_value_id = value_id(graph_input.value)
                self._admitted_input_lineage[graph_value_id] = (graph_value_id,)
                scheduling = self._schedule_work(
                    queue,
                    graph_input.target_component,
                    graph_input.target_port,
                    graph_input.value,
                    source_endpoint="external.input",
                    input_id=graph_value_id,
                )
                self._record_scheduling_disposition(
                    scheduling,
                    graph_input.target_component,
                    graph_value_id,
                    evidence,
                    work,
                )
                if scheduling.rejected_reason is not None:
                    self._record_skipped_work(
                        graph_input.target_component,
                        graph_value_id,
                        scheduling.rejected_reason,
                        evidence,
                        work,
                    )
                admitted.append(graph_input.value)
                self._capture_input(graph_input.value)
            if not resumed:
                self._schedule_phase_triggers(planned_phase, queue, phase_started_time)
            action, control_reason = self._observe_control_request(evidence)
            if action == "cancel":
                status = GraphRunStatus.CANCELLED
                terminal_reason = control_reason
            else:
                if action == "complete":
                    terminal_reason = control_reason
                for node in source_nodes:
                    self._poll_source(
                        node,
                        queue,
                        admission,
                        evidence,
                    )

            idle_cycles = 0
            event_count = 0
            while status == GraphRunStatus.COMPLETE and (
                queue or admission.incomplete_sources
            ):
                action, control_reason = self._observe_control_request(evidence)
                if action == "cancel":
                    status = GraphRunStatus.CANCELLED
                    terminal_reason = control_reason
                    break
                if action == "complete":
                    terminal_reason = control_reason
                    admission.incomplete_sources.difference_update(live_source_ids)
                if live_wall_deadline is not None and monotonic() >= live_wall_deadline:
                    status = GraphRunStatus.TIMED_OUT
                    failure = (
                        f"phase exceeded timeout_seconds={planned_phase.timeout_seconds}"
                    )
                    self._emit(
                        evidence,
                        "phase_timeout",
                        {
                            "phase_id": planned_phase.phase_id,
                            "timeout_seconds": planned_phase.timeout_seconds,
                            "clock": "wall",
                        },
                    )
                    break
                if not queue:
                    if action == "complete":
                        admission.incomplete_sources.difference_update(
                            live_source_ids
                        )
                        if not admission.incomplete_sources:
                            break
                    if (
                        checkpoint_after_inputs is not None
                        and len(admitted) >= checkpoint_after_inputs
                        and admission.incomplete_sources
                    ):
                        status = GraphRunStatus.CHECKPOINTED
                        checkpoint_requested = True
                        break
                    progressed = False
                    for component_id in sorted(tuple(admission.incomplete_sources)):
                        if self._poll_source(
                            self.runtime.node(component_id),
                            queue,
                            admission,
                            evidence,
                        ):
                            progressed = True
                    if progressed:
                        idle_cycles = 0
                        continue
                    if admission.incomplete_sources & live_source_ids:
                        wait_seconds = 0.001
                        if live_wall_deadline is not None:
                            wait_seconds = min(
                                wait_seconds,
                                max(0.0, live_wall_deadline - monotonic()),
                            )
                        if wait_seconds > 0:
                            sleep(wait_seconds)
                        continue
                    idle_cycles += 1
                    if idle_cycles >= self.max_idle_cycles:
                        status = GraphRunStatus.PARTIAL
                        break
                    continue
                next_time = queue.peek().scheduled_time
                blocked_sources = watermark_blockers(
                    self.runtime,
                    next_time,
                    admission,
                    self._require_execution_time,
                )
                if blocked_sources:
                    progressed = False
                    for component_id in blocked_sources:
                        if self._poll_source(
                            self.runtime.node(component_id),
                            queue,
                            admission,
                            evidence,
                        ):
                            progressed = True
                    if progressed:
                        idle_cycles = 0
                        continue
                    if set(blocked_sources) & live_source_ids:
                        wait_seconds = 0.001
                        if live_wall_deadline is not None:
                            wait_seconds = min(
                                wait_seconds,
                                max(0.0, live_wall_deadline - monotonic()),
                            )
                        if wait_seconds > 0:
                            sleep(wait_seconds)
                        continue
                    idle_cycles += 1
                    if idle_cycles >= self.max_idle_cycles:
                        status = GraphRunStatus.PARTIAL
                        break
                    continue
                idle_cycles = 0
                if (
                    phase_deadline is not None
                    and queue.peek().scheduled_time.seconds > phase_deadline.seconds
                ):
                    self._advance_time(phase_deadline)
                    status = GraphRunStatus.TIMED_OUT
                    failure = (
                        f"phase exceeded timeout_seconds={planned_phase.timeout_seconds}"
                    )
                    self._emit(
                        evidence,
                        "phase_timeout",
                        {
                            "phase_id": planned_phase.phase_id,
                            "timeout_seconds": planned_phase.timeout_seconds,
                        },
                    )
                    break
                event = queue.pop()
                event_count += 1
                if event_count > self.max_events:
                    raise RuntimeError(
                        f"phase exceeded max_graph_events={self.max_events}"
                    )
                self._advance_time(event.scheduled_time)
                self._record_outcome_dispositions(
                    self._outcomes.expire(self._current_time), evidence
                )
                node = self.runtime.node(event.component_id)
                if event.kind == "trigger":
                    self._handle_trigger(
                        planned_phase,
                        node,
                        event.value,
                        active,
                        queue,
                        evidence,
                        work,
                    )
                    continue
                if event.kind == "emission":
                    pending = event.value
                    if not isinstance(pending, PendingEmission):
                        raise TypeError("invalid queued graph emission")
                    self._record_emission(
                        node,
                        pending.output_port,
                        pending.value,
                        pending.input_ids,
                        active,
                        queue,
                        evidence,
                        emissions,
                        work,
                        planned_phase.phase_id,
                    )
                    continue
                if event.kind == "work_completion":
                    pending = event.value
                    if not isinstance(pending, PendingWork):
                        raise TypeError("invalid queued work completion")
                    completed = WorkRecord(
                        work_id=self._ids.next("work"),
                        component_id=node.component_id,
                        stage=pending.stage,
                        status=pending.status,
                        started_time=pending.started_time,
                        completed_time=self._current_time,
                        deadline_time=pending.deadline_time,
                        input_ids=pending.input_ids,
                        role=node.planned.role,
                        reason_code=pending.reason_code,
                    )
                    work.append(completed)
                    self._emit(evidence, "work", completed.to_payload())
                    continue
                if event.kind == "source":
                    admitted.append(event.value)
                    self._capture_input(event.value)
                    packet_key = (node.component_id, value_id(event.value))
                    rejection = admission.packet_rejections.pop(packet_key, None)
                    rejection_code = admission.packet_rejection_codes.pop(
                        packet_key,
                        "source_contract_violation",
                    )
                    late_packet = admission.late_packets.pop(packet_key, None)
                    if late_packet is not None:
                        self._emit(
                            evidence,
                            "source_lateness",
                            {
                                "component_id": node.component_id,
                                "packet_id": value_id(event.value),
                                "lateness_seconds": late_packet[0],
                                "allowed_lateness_seconds": (
                                    self.allowed_lateness_seconds
                                ),
                                "disposition": late_packet[1],
                            },
                        )
                    sequence_gap = admission.sequence_gaps.pop(packet_key, None)
                    if sequence_gap is not None:
                        self._emit(
                            evidence,
                            "source_sequence_gap",
                            {
                                "component_id": node.component_id,
                                "packet_id": value_id(event.value),
                                "expected_sequence": sequence_gap[0],
                                "observed_sequence": sequence_gap[1],
                            },
                        )
                    if rejection is not None:
                        rejected = WorkRecord(
                            work_id=self._ids.next("work"),
                            component_id=node.component_id,
                            stage="source",
                            status=WorkStatus.REJECTED,
                            started_time=self._current_time,
                            completed_time=self._current_time,
                            input_ids=(value_id(event.value),),
                            role=node.planned.role,
                            reason_code=rejection_code,
                            details={"error": rejection},
                        )
                        work.append(rejected)
                        self._emit(evidence, "work", rejected.to_payload())
                        self._emit(
                            evidence,
                            "source_packet_rejected",
                            {
                                "component_id": node.component_id,
                                "packet_id": value_id(event.value),
                                "reason": rejection,
                                "reason_code": rejection_code,
                                "packet_hash": canonical_hash(value_payload(event.value)),
                            },
                        )
                        if not bool(getattr(node.component, "exhausted", False)):
                            if (
                                node.component_id in live_source_ids
                                and self._control_request()[0] != "complete"
                            ):
                                admission.incomplete_sources.add(node.component_id)
                            else:
                                self._poll_source(node, queue, admission, evidence)
                        continue
                    output_time = self._record_emission(
                        node,
                        event.port,
                        event.value,
                        (),
                        active,
                        queue,
                        evidence,
                        emissions,
                        work,
                        planned_phase.phase_id,
                    )
                    source_work = completed_work(
                        self._ids,
                        node,
                        "source",
                        (),
                        self._current_time,
                        output_time,
                    )
                    work.append(source_work)
                    self._emit(evidence, "work", source_work.to_payload())
                    if (
                        node.component_id in live_source_ids
                        and self._control_request()[0] != "complete"
                        and not bool(
                            getattr(node.component, "exhausted", False)
                        )
                    ):
                        admission.incomplete_sources.add(node.component_id)
                    elif (
                        checkpoint_after_inputs is None
                        and not bool(getattr(node.component, "exhausted", False))
                    ):
                        self._poll_source(node, queue, admission, evidence)
                    elif not bool(getattr(node.component, "exhausted", False)):
                        admission.incomplete_sources.add(node.component_id)
                    continue
                started = self._current_time
                try:
                    if event.kind == "authorization":
                        if not isinstance(event.value, PendingAuthorization):
                            raise TypeError("invalid queued authorization state")
                        self._handle_pending_authorization(
                            node,
                            event.value,
                            active,
                            queue,
                            evidence,
                            emissions,
                            work,
                            planned_phase.phase_id,
                        )
                        continue
                    if node.plugin.kind == ComponentKind.ACTUATOR:
                        self._handle_action_request(
                            node,
                            event.port,
                            event.value,
                            event.source_endpoint,
                            event.input_id,
                            active,
                            queue,
                            evidence,
                            emissions,
                            work,
                            planned_phase.phase_id,
                            started,
                        )
                        continue
                    self._validate_model_role_input(node, event.port, event.value)
                    outputs = dispatch_component(
                        node,
                        event.port,
                        event.value,
                        self._context(node),
                        self._component_state,
                        input_id=event.input_id,
                        admitted_input_ids=self._root_input_ids(event.input_id),
                    )
                    output_time = started
                    input_ids = (
                        (event.input_id,) if event.input_id is not None else ()
                    )
                    pending_outputs: list[tuple[str, Any, TimePoint]] = []
                    for output_port, values in outputs.items():
                        for value in values:
                            available = available_time(value, self._current_time)
                            self._require_execution_time(available)
                            if available.seconds < self._current_time.seconds:
                                raise ValueError(
                                    f"component {node.component_id} emitted output before "
                                    "its input was available"
                                )
                            if available.seconds > output_time.seconds:
                                output_time = available
                            pending_outputs.append((output_port, value, available))
                    deadline_time = component_deadline(
                        node.component_id,
                        started,
                        self.component_deadlines,
                    )
                    if (
                        deadline_time is not None
                        and output_time.seconds > deadline_time.seconds
                    ):
                        for output_port, value, _ in pending_outputs:
                            if isinstance(value, Prediction):
                                self._record_model_result_disposition(
                                    node,
                                    output_port,
                                    value,
                                    ModelResultDispositionStatus.LATE,
                                    evidence,
                                    reason_code="deadline_exceeded",
                                )
                        self._schedule_work_completion(
                            queue,
                            node,
                            node.plugin.kind.value,
                            input_ids,
                            started,
                            deadline_time,
                            status=WorkStatus.TIMED_OUT,
                            deadline_time=deadline_time,
                            reason_code="deadline_exceeded",
                        )
                        continue
                    for output_port, value, available in pending_outputs:
                        if available.seconds > self._current_time.seconds:
                            self._schedule_emission(
                                queue,
                                node,
                                output_port,
                                value,
                                input_ids,
                                available,
                            )
                        else:
                            self._record_emission(
                                node,
                                output_port,
                                value,
                                input_ids,
                                active,
                                queue,
                                evidence,
                                emissions,
                                work,
                                planned_phase.phase_id,
                            )
                    if output_time.seconds > self._current_time.seconds:
                        self._schedule_work_completion(
                            queue,
                            node,
                            node.plugin.kind.value,
                            input_ids,
                            started,
                            output_time,
                        )
                    else:
                        completed = completed_work(
                            self._ids,
                            node,
                            node.plugin.kind.value,
                            input_ids,
                            started,
                            output_time,
                        )
                        work.append(completed)
                        self._emit(evidence, "work", completed.to_payload())
                except Exception as exc:
                    if isinstance(exc, ModelResultRejected):
                        self._record_rejected_model_result(node, exc, evidence)
                    failed = failed_work(
                        self._ids,
                        node,
                        started,
                        self._current_time,
                        (event.input_id,) if event.input_id is not None else (),
                        exc,
                    )
                    work.append(failed)
                    self._emit(evidence, "work", failed.to_payload())
                    role = None if node.model_binding is None else node.model_binding.role
                    if (
                        role is not None
                        and role.failure_disposition
                        == PlannedModelFailureDisposition.REJECT_RESULT
                    ):
                        self._emit(
                            evidence,
                            "model_role_failure_disposition",
                            {
                                "component_id": node.component_id,
                                "role_id": role.role_id,
                                "disposition": role.failure_disposition.value,
                                "failure": f"{type(exc).__name__}: {exc}",
                            },
                        )
                        continue
                    raise
        except Exception as exc:
            status = GraphRunStatus.FAILED
            failure = f"{type(exc).__name__}: {exc}"
            self._emit(
                evidence,
                "phase_failure",
                {"phase_id": planned_phase.phase_id, "failure": failure},
            )
        finally:
            if status in {
                GraphRunStatus.CANCELLED,
                GraphRunStatus.TIMED_OUT,
                GraphRunStatus.FAILED,
                GraphRunStatus.PARTIAL,
            }:
                self._cancel_pending_actions(queue, status, evidence, work)
                self._record_cancelled_model_results(queue, status, evidence)
            if status != GraphRunStatus.CHECKPOINTED:
                self._finalize_model_comparisons(status, evidence)
            for node in lifecycle_started:
                snapshot = getattr(node.component, "snapshot_state", None)
                if not callable(snapshot):
                    continue
                try:
                    state_snapshot = capture_canonical_state(node.component)
                    self._emit(
                        evidence,
                        "component_state",
                        {
                            "phase_id": planned_phase.phase_id,
                            "component_id": node.component_id,
                            "component_kind": node.plugin.kind.value,
                            "state": state_snapshot.thaw(),
                            "state_hash": state_snapshot.state_hash,
                        },
                    )
                except Exception as exc:  # pragma: no cover - external boundary
                    self._emit(
                        evidence,
                        "component_state_unavailable",
                        {
                            "phase_id": planned_phase.phase_id,
                            "component_id": node.component_id,
                            "failure": f"{type(exc).__name__}: {exc}",
                        },
                    )
            stop_failures: list[str] = []
            for node in reversed(lifecycle_started):
                stop = getattr(node.component, "stop", None)
                if callable(stop):
                    try:
                        stop(self._context(node))
                    except Exception as exc:  # pragma: no cover - external boundary
                        stop_failures.append(
                            f"{node.component_id}: {type(exc).__name__}: {exc}"
                        )
            if stop_failures:
                status = GraphRunStatus.FAILED
                suffix = "; ".join(stop_failures)
                failure = suffix if failure is None else f"{failure}; {suffix}"
            self._emit(
                evidence,
                "phase_finished",
                {
                    "phase_id": planned_phase.phase_id,
                    "status": status.value,
                    "failure": failure,
                    "admitted_input_count": len(admitted),
                    "emission_count": len(emissions),
                    "work_count": len(work),
                },
            )
        if evidence and evidence[0].sequence != evidence_start:
            raise RuntimeError("phase evidence sequence start drifted")
        if checkpoint_requested:
            checkpoint = self._create_checkpoint(
                planned_phase, phase_started_time, evidence
            )
        self._resumed_phase_id = None
        self._resumed_phase_started_time = None
        return GraphPhaseResult(
            execution_id=self.execution_id,
            plan_hash=self.plan.plan_hash,
            phase_id=planned_phase.phase_id,
            status=status,
            evidence=tuple(evidence),
            admitted_inputs=admitted.retained,
            emissions=emissions.retained,
            work=work.retained,
            artifacts=tuple(
                value.value
                for value in emissions
                if isinstance(value.value, ArtifactPublication)
            ),
            failure=failure,
            checkpoint=checkpoint,
            terminal_reason=terminal_reason,
        )

    def _phase(self, value: PlannedPhase | str) -> PlannedPhase:
        if isinstance(value, PlannedPhase):
            if value not in self.plan.phases:
                raise ValueError("phase does not belong to this execution plan")
            return value
        for phase in self.plan.phases:
            if phase.phase_id == value:
                return phase
        raise KeyError(f"unknown phase: {value}")

    def _ordered_nodes(
        self,
        active: set[str],
        *,
        kind: ComponentKind | None = None,
    ) -> tuple[RuntimeNode, ...]:
        values = [
            value
            for value in self.runtime.nodes
            if value.component_id in active and (kind is None or value.plugin.kind == kind)
        ]
        return tuple(
            sorted(
                values,
                key=lambda value: (
                    self._component_order[value.component_id],
                    value.component_id,
                ),
            )
        )

    def _poll_source(
        self,
        node: RuntimeNode,
        queue: EventQueue,
        admission: SourceAdmissionState,
        evidence: list[EvidenceRecord],
    ) -> bool:
        if (
            self._control_request()[0] == "complete"
            and bool(getattr(node.component, "is_live", False))
        ):
            admission.incomplete_sources.discard(node.component_id)
            return False
        try:
            admitted = poll_source(node, admission, self._require_execution_time)
        finally:
            self._record_source_observations(node, evidence)
        if admitted is None:
            return False
        packet = admitted.packet
        available = admitted.available_time
        scheduled = available
        packet_key = (node.component_id, value_id(packet))
        graph_lateness = max(0.0, self._current_time.seconds - available.seconds)
        watermark_lateness = admission.watermark_lateness.pop(packet_key, 0.0)
        lateness = max(graph_lateness, watermark_lateness)
        if lateness > 0:
            if lateness > self.allowed_lateness_seconds:
                if self.lateness == "fail_run":
                    raise RuntimeError(
                        f"source {node.component_id} exceeded allowed lateness by "
                        f"{lateness - self.allowed_lateness_seconds:.9g} seconds"
                    )
                admission.packet_rejections[packet_key] = (
                    f"packet arrived {lateness:.9g} seconds behind the declared "
                    "source or graph frontier"
                )
                admission.packet_rejection_codes[packet_key] = (
                    "source_late_input"
                    if graph_lateness > 0
                    else "source_watermark_violation"
                )
                disposition = "rejected"
            else:
                disposition = "accepted"
            admission.late_packets[packet_key] = (lateness, disposition)
            if available.seconds < self._current_time.seconds:
                scheduled = self._current_time
        self._event_sequence += 1
        queue.push(
            QueuedEvent(
                (
                    scheduled.seconds,
                    0,
                    node.component_id,
                    sequence_key(packet),
                    value_id(packet),
                    self._event_sequence,
                ),
                "source",
                node.component_id,
                admitted.output_port,
                packet,
                scheduled,
            ),
            rejectable=False,
        )
        return True

    def _record_source_observations(
        self,
        node: RuntimeNode,
        evidence: list[EvidenceRecord],
    ) -> None:
        drain = getattr(node.component, "drain_evidence_observations", None)
        if not callable(drain):
            return
        for observation in drain():
            if not isinstance(observation, tuple) or len(observation) != 2:
                raise TypeError(
                    f"source {node.component_id} returned an invalid evidence observation"
                )
            record_type, payload = observation
            if record_type not in _SOURCE_OBSERVATION_RECORD_TYPES:
                raise ValueError(
                    f"source {node.component_id} returned unsupported observation "
                    f"{record_type}"
                )
            if not isinstance(payload, Mapping):
                raise TypeError("source evidence observation payload must be an object")
            declared_component = payload.get("component_id")
            if declared_component not in {None, node.component_id}:
                raise ValueError("source observation component identity mismatch")
            self._emit(
                evidence,
                record_type,
                {**thaw_json(payload), "component_id": node.component_id},
            )

    def _record_emission(
        self,
        node: RuntimeNode,
        output_port: str,
        value: Any,
        input_ids: tuple[str, ...],
        active: set[str],
        queue: EventQueue,
        evidence: list[EvidenceRecord],
        emissions: PhaseRecordBuffer[GraphEmission],
        work: PhaseRecordBuffer[WorkRecord],
        phase_id: str,
    ) -> TimePoint:
        port = next(
            (value_port for value_port in node.descriptor.output_ports if value_port.name == output_port),
            None,
        )
        if port is None:
            raise ValueError(f"component {node.component_id} has no output port {output_port}")
        if isinstance(value, AuthorizedCommand):
            raise PermissionError(
                "authorized commands cannot be emitted by suite graph components"
            )
        observed_type = value_type(value)
        if observed_type != port.type_id:
            raise TypeError(
                f"component {node.component_id}.{output_port} emitted {observed_type}; "
                f"expected {port.type_id}"
            )
        available = available_time(value, self._current_time)
        self._require_execution_time(available)
        if (
            available.seconds < self._current_time.seconds
            and node.plugin.kind != ComponentKind.SOURCE
        ):
            raise ValueError(
                f"component {node.component_id} emitted output before its input was available"
            )
        emitted_value_id = value_id(value)
        if input_ids:
            roots: list[str] = []
            for input_id in input_ids:
                roots.extend(self._root_input_ids(input_id))
            self._admitted_input_lineage[emitted_value_id] = tuple(
                dict.fromkeys(roots)
            )
        else:
            self._admitted_input_lineage[emitted_value_id] = (emitted_value_id,)
        emission = GraphEmission(
            emission_id=self._ids.next("emission"),
            component_id=node.component_id,
            output_port=output_port,
            value_id=emitted_value_id,
            value_type=observed_type,
            available_time=available,
            input_ids=input_ids,
            value_hash=canonical_hash(value_payload(value)),
            value=value,
        )
        emissions.append(emission)
        if isinstance(value, Prediction) and node.model_binding is not None:
            self._emitted_predictions[value.prediction_id] = value
            self._record_model_result_disposition(
                node,
                output_port,
                value,
                ModelResultDispositionStatus.EMITTED,
                evidence,
            )
        self._emit(evidence, "graph_emission", emission.to_payload())
        if isinstance(value, Prediction) and node.model_binding is not None:
            self._record_outcome_dispositions(
                self._outcomes.enroll(value, self._current_time), evidence
            )
            self._record_model_comparison(value, evidence)
        if isinstance(value, ActionRequest):
            if node.plugin.kind != ComponentKind.POLICY:
                raise PermissionError("only policy components may emit action requests")
            if value.requested_by != node.component_id:
                raise PermissionError("action request identity differs from emitting policy")
            if value.prediction_id is not None and value.prediction_id not in input_ids:
                raise PermissionError(
                    "action request prediction identity differs from policy inputs"
                )
            if value.policy_state_hash is not None:
                expected_state_hash = canonical_hash(
                    self._component_state.get(node.component_id, {})
                )
                if value.policy_state_hash != expected_state_hash:
                    raise PermissionError(
                        "action request policy state differs from runtime-owned state"
                    )
            self._emit(
                evidence,
                "action_request",
                {"policy_component_id": node.component_id, "request": value.to_payload()},
            )
        if isinstance(value, Outcome):
            self._emit(
                evidence,
                "outcome_received",
                {
                    "component_id": node.component_id,
                    "outcome": value.to_payload(),
                },
            )
            dispositions, matches = self._outcomes.receive(
                node.component_id,
                value,
                self._current_time,
            )
            self._record_outcome_dispositions(dispositions, evidence)
            for match in matches:
                self._apply_adaptation(
                    match,
                    phase_id,
                    active,
                    queue,
                    evidence,
                )
        if isinstance(value, StateTransition):
            self._schedule_state_triggers(phase_id, value, active, queue, evidence)
        targets = self._targets.get((node.component_id, output_port), ())
        if not targets:
            return available
        for component_id, input_port in targets:
            if component_id not in active:
                continue
            scheduling = self._schedule_work(
                queue,
                component_id,
                input_port,
                value,
                source_endpoint=emission.endpoint,
                input_id=emitted_value_id,
            )
            self._record_scheduling_disposition(
                scheduling,
                component_id,
                emitted_value_id,
                evidence,
                work,
            )
            if scheduling.rejected_reason is not None:
                self._record_skipped_work(
                    component_id,
                    emitted_value_id,
                    scheduling.rejected_reason,
                    evidence,
                    work,
                )
        if available.seconds < self._current_time.seconds:
            return self._current_time
        return available

    def _handle_action_request(
        self,
        node: RuntimeNode,
        input_port: str,
        value: Any,
        source_endpoint: str | None,
        input_id: str | None,
        active: set[str],
        queue: EventQueue,
        evidence: list[EvidenceRecord],
        emissions: PhaseRecordBuffer[GraphEmission],
        work: PhaseRecordBuffer[WorkRecord],
        phase_id: str,
        started: TimePoint,
    ) -> None:
        validate_input_port(node, input_port, value)
        if not isinstance(value, ActionRequest):
            raise TypeError("actuator graph ingress requires an ActionRequest")
        if input_id != value.request_id:
            raise PermissionError("actuator input identity differs from action request")
        if source_endpoint is None or "." not in source_endpoint:
            raise PermissionError("action request must arrive from a routed policy")
        source_component = source_endpoint.rsplit(".", 1)[0]
        source = self.runtime.node(source_component)
        if source.plugin.kind != ComponentKind.POLICY:
            raise PermissionError("actuator action request source is not a policy")
        if value.requested_by != source_component:
            raise PermissionError("action request requested_by differs from routed policy")
        outcome = self._action_broker.begin(
            value,
            node.component_id,
            self._current_time,
            self._context(node),
            input_ids=(value.request_id,),
        )
        self._record_broker_outcome(outcome, evidence, include_request=True)
        if outcome.pending is not None:
            self._schedule_pending_authorization(queue, node, outcome.pending)
            return
        if outcome.command is None:
            self._finish_action_work(
                node,
                work,
                evidence,
                started,
                (value.request_id,),
                WorkStatus.SKIPPED,
                outcome.disposition.status.value,
            )
            return
        self._submit_authorized_action(
            node,
            value,
            outcome.command,
            active,
            queue,
            evidence,
            emissions,
            work,
            phase_id,
            started,
            (value.request_id,),
        )

    def _handle_pending_authorization(
        self,
        node: RuntimeNode,
        pending: PendingAuthorization,
        active: set[str],
        queue: EventQueue,
        evidence: list[EvidenceRecord],
        emissions: PhaseRecordBuffer[GraphEmission],
        work: PhaseRecordBuffer[WorkRecord],
        phase_id: str,
    ) -> None:
        outcome = self._action_broker.resolve(
            pending,
            self._current_time,
            self._context(node),
        )
        self._record_broker_outcome(outcome, evidence, include_request=False)
        if outcome.command is None:
            self._finish_action_work(
                node,
                work,
                evidence,
                pending.started_time,
                pending.input_ids,
                WorkStatus.SKIPPED,
                outcome.disposition.status.value,
            )
            return
        self._submit_authorized_action(
            node,
            pending.action_request,
            outcome.command,
            active,
            queue,
            evidence,
            emissions,
            work,
            phase_id,
            pending.started_time,
            pending.input_ids,
        )

    def _submit_authorized_action(
        self,
        node: RuntimeNode,
        request: ActionRequest,
        command: AuthorizedCommand,
        active: set[str],
        queue: EventQueue,
        evidence: list[EvidenceRecord],
        emissions: PhaseRecordBuffer[GraphEmission],
        work: PhaseRecordBuffer[WorkRecord],
        phase_id: str,
        started: TimePoint,
        input_ids: tuple[str, ...],
    ) -> None:
        submitted = self._action_broker.submitted(
            request, command, self._current_time
        )
        self._emit(evidence, "action_disposition", submitted.to_payload())
        try:
            receipt = node.component.submit(command, self._context(node))
            if not isinstance(receipt, ActionReceipt):
                raise TypeError("actuator must return ActionReceipt")
            self._require_execution_time(receipt.observed_time)
            if receipt.observed_time != self._current_time:
                raise ValueError(
                    "in-process actuator receipt must be available at submission time"
                )
            self._emit(
                evidence,
                "action_receipt",
                {"component_id": node.component_id, "receipt": receipt.to_payload()},
            )
            self._record_emission(
                node,
                node.descriptor.output_ports[0].name,
                receipt,
                input_ids,
                active,
                queue,
                evidence,
                emissions,
                work,
                phase_id,
            )
            receipt_disposition = self._action_broker.receipt_disposition(
                request, command, receipt
            )
            self._emit(
                evidence,
                "action_disposition",
                receipt_disposition.to_payload(),
            )
            self._finish_action_work(
                node,
                work,
                evidence,
                started,
                input_ids,
                WorkStatus.COMPLETED,
                None,
            )
        except Exception as exc:
            failed = ActionDisposition(
                disposition_id=self._ids.next("action_disposition"),
                action_request_id=request.request_id,
                actuator_id=node.component_id,
                status=ActionDispositionStatus.FAILED,
                decided_time=self._current_time,
                authorization_request_id=command.authorization_request_id,
                authorization_decision_id=command.authorization_decision_id,
                command_id=command.command_id,
                reason=f"{type(exc).__name__}: {exc}",
                terminal=True,
            )
            self._emit(evidence, "action_disposition", failed.to_payload())
            raise

    def _record_broker_outcome(
        self,
        outcome: BrokerOutcome,
        evidence: list[EvidenceRecord],
        *,
        include_request: bool,
    ) -> None:
        if include_request:
            self._emit(
                evidence,
                "authorization_request",
                outcome.authorization_request.to_payload(),
            )
        for decision in outcome.decisions:
            self._emit(evidence, "authorization_decision", decision.to_payload())
        if outcome.cancellation is not None:
            self._emit(
                evidence,
                "action_cancellation",
                outcome.cancellation.to_payload(),
            )
        if outcome.command is not None:
            self._emit(evidence, "authorized_command", outcome.command.to_payload())
        self._emit(evidence, "action_disposition", outcome.disposition.to_payload())

    def _schedule_pending_authorization(
        self,
        queue: EventQueue,
        node: RuntimeNode,
        pending: PendingAuthorization,
    ) -> None:
        self._event_sequence += 1
        queue.push(
            QueuedEvent(
                (
                    pending.ready_time.seconds,
                    3,
                    node.component_id,
                    pending.action_request.request_id,
                    self._event_sequence,
                ),
                "authorization",
                node.component_id,
                "request",
                pending,
                pending.ready_time,
                "eegle.action_broker",
                pending.action_request.request_id,
            ),
            rejectable=False,
        )

    def _finish_action_work(
        self,
        node: RuntimeNode,
        work: PhaseRecordBuffer[WorkRecord],
        evidence: list[EvidenceRecord],
        started: TimePoint,
        input_ids: tuple[str, ...],
        status: WorkStatus,
        reason_code: str | None,
    ) -> None:
        record = WorkRecord(
            work_id=self._ids.next("work"),
            component_id=node.component_id,
            stage="actuator",
            status=status,
            started_time=started,
            completed_time=self._current_time,
            input_ids=input_ids,
            role=node.planned.role,
            reason_code=reason_code,
        )
        work.append(record)
        self._emit(evidence, "work", record.to_payload())

    def _cancel_pending_actions(
        self,
        queue: EventQueue,
        status: GraphRunStatus,
        evidence: list[EvidenceRecord],
        work: PhaseRecordBuffer[WorkRecord],
    ) -> None:
        seen: set[str] = set()
        for event in queue.events():
            if event.kind != "authorization" or not isinstance(
                event.value, PendingAuthorization
            ):
                continue
            pending = event.value
            if pending.action_request.request_id in seen:
                continue
            seen.add(pending.action_request.request_id)
            outcome = self._action_broker.cancel(
                pending,
                self._current_time,
                reason=f"phase_{status.value}",
            )
            self._record_broker_outcome(outcome, evidence, include_request=False)
            self._finish_action_work(
                self.runtime.node(pending.actuator_id),
                work,
                evidence,
                pending.started_time,
                pending.input_ids,
                WorkStatus.CANCELLED,
                f"phase_{status.value}",
            )

    def finalize_outcomes(self, *, cancelled: bool) -> tuple[EvidenceRecord, ...]:
        """Close enrolled predictions exactly once at execution termination."""

        records: list[EvidenceRecord] = []
        self._record_outcome_dispositions(
            self._outcomes.close(self._current_time, cancelled=cancelled),
            records,
        )
        return tuple(records)

    def _record_outcome_dispositions(
        self,
        dispositions: tuple[OutcomeDisposition, ...],
        evidence: list[EvidenceRecord],
    ) -> None:
        for disposition in dispositions:
            self._emit(evidence, "outcome_disposition", disposition.to_payload())

    def _apply_adaptation(
        self,
        match: OutcomeMatch,
        phase_id: str,
        active: set[str],
        queue: EventQueue,
        evidence: list[EvidenceRecord],
    ) -> None:
        planned = self._adaptations.get(match.expectation_id)
        if planned is None:
            return
        node = self.runtime.node(planned.model_component_id)
        reason: str | None = None
        if OutcomeUse.ADAPTATION not in match.uses_granted:
            reason = "adaptation_use_not_granted"
        elif phase_id not in planned.enabled_phases:
            reason = "adaptation_phase_not_enabled"
        elif planned.model_component_id not in active:
            reason = "adaptive_model_inactive"
        adapt = getattr(node.component, "adapt", None)
        snapshot = getattr(node.component, "snapshot_state", None)
        restore = getattr(node.component, "restore_state", None)
        if reason is None and (
            not callable(adapt) or not callable(snapshot) or not callable(restore)
        ):
            reason = "adaptive_model_protocol_missing"
        eligibility = AdaptationEligibilityDecision(
            decision_id=self._ids.next("adaptation_eligibility"),
            adaptation_id=planned.adaptation_id,
            model_component_id=planned.model_component_id,
            prediction_id=match.prediction.prediction_id,
            outcome_id=match.outcome.outcome_id,
            status=(
                AdaptationEligibilityStatus.ELIGIBLE
                if reason is None
                else AdaptationEligibilityStatus.REJECTED
            ),
            decided_time=self._current_time,
            reason_code=reason,
        )
        self._emit(evidence, "adaptation_eligibility", eligibility.to_payload())
        if reason is not None:
            return
        assert callable(adapt) and callable(snapshot) and callable(restore)
        prior_state = capture_canonical_state(node.component)
        prior_hash = prior_state.state_hash
        trigger_ids = (match.prediction.prediction_id, match.outcome.outcome_id)
        requested = StateTransition(
            transition_id=self._ids.next("transition"),
            component_id=planned.model_component_id,
            status=TransitionStatus.REQUESTED,
            transition_kind="adaptation",
            transition_time=self._current_time,
            requested_time=self._current_time,
            prior_state_hash=prior_hash,
            resulting_state_hash=prior_hash,
            trigger_ids=trigger_ids,
            metadata={"adaptation_id": planned.adaptation_id},
        )
        self._emit(evidence, "state_transition", requested.to_payload())
        try:
            result = adapt(match.prediction, match.outcome, self._context(node))
            if not isinstance(result, AdaptationResult):
                raise TypeError("adaptive model must return AdaptationResult")
            resulting_hash = capture_canonical_state(node.component).state_hash
            if result.status == TransitionStatus.APPLIED and resulting_hash == prior_hash:
                raise ValueError("applied adaptation did not change model state")
            if result.status != TransitionStatus.APPLIED and resulting_hash != prior_hash:
                raise ValueError("non-applied adaptation changed model state")
            transition = StateTransition(
                transition_id=self._ids.next("transition"),
                component_id=planned.model_component_id,
                status=result.status,
                transition_kind="adaptation",
                transition_time=self._current_time,
                requested_time=requested.transition_time,
                prior_state_hash=prior_hash,
                resulting_state_hash=resulting_hash,
                trigger_ids=trigger_ids,
                reason=result.reason,
                metadata={
                    "adaptation_id": planned.adaptation_id,
                    **thaw_json(result.metadata),
                },
            )
            self._emit(evidence, "state_transition", transition.to_payload())
            self._schedule_state_triggers(
                phase_id, transition, active, queue, evidence
            )
        except Exception as exc:
            # A failed updater may leave state in a value that cannot itself be
            # serialized. Restoration must not depend on successfully
            # inspecting that damaged intermediate state.
            changed_hash: str | None = None
            try:
                changed_hash = capture_canonical_state(node.component).state_hash
            except Exception:
                pass
            try:
                restore(prior_state.thaw())
            except Exception as rollback_exc:
                raise RuntimeError("adaptive model rollback failed") from rollback_exc
            restored_hash = capture_canonical_state(node.component).state_hash
            if restored_hash != prior_hash:
                raise RuntimeError("adaptive model rollback did not restore prior state") from exc
            failed = StateTransition(
                transition_id=self._ids.next("transition"),
                component_id=planned.model_component_id,
                status=TransitionStatus.FAILED,
                transition_kind="adaptation",
                transition_time=self._current_time,
                requested_time=requested.transition_time,
                prior_state_hash=prior_hash,
                resulting_state_hash=prior_hash,
                trigger_ids=trigger_ids,
                reason=f"{type(exc).__name__}: {exc}",
                metadata={"adaptation_id": planned.adaptation_id},
            )
            self._emit(evidence, "state_transition", failed.to_payload())
            if changed_hash is None or changed_hash != prior_hash:
                rolled_back = StateTransition(
                    transition_id=self._ids.next("transition"),
                    component_id=planned.model_component_id,
                    status=TransitionStatus.ROLLED_BACK,
                    transition_kind="adaptation",
                    transition_time=self._current_time,
                    requested_time=requested.transition_time,
                    prior_state_hash=prior_hash,
                    resulting_state_hash=prior_hash,
                    trigger_ids=trigger_ids,
                    reason="failed_update_restored",
                    metadata={"adaptation_id": planned.adaptation_id},
                )
                self._emit(evidence, "state_transition", rolled_back.to_payload())

    def _record_model_comparison(
        self,
        prediction: Prediction,
        evidence: list[EvidenceRecord],
    ) -> None:
        group_id = prediction.comparison_group
        if group_id is None:
            return
        members = self._comparison_members.get(group_id)
        if members is None or prediction.component_id not in members:
            raise RuntimeError("prediction comparison group differs from the locked plan")
        key = canonical_hash(
            {
                "group_id": group_id,
                "output_port": prediction.output_port,
                "input_ids": list(prediction.input_ids),
                "admitted_input_ids": list(prediction.admitted_input_ids),
            }
        )
        pending = self._pending_model_comparisons.setdefault(key, {})
        if prediction.component_id in pending:
            raise RuntimeError(
                f"comparison group {group_id} emitted duplicate member "
                f"{prediction.component_id} for one input"
            )
        pending[prediction.component_id] = prediction
        if set(pending) != set(members):
            return
        ordered = tuple(pending[value] for value in members)
        outputs_equal = len(
            {
                canonical_hash(
                    {
                        "value": value.value,
                        "uncertainty": value.uncertainty,
                        "validity": value.validity,
                        "abstained": value.abstained,
                        "abstention_reason": value.abstention_reason,
                    }
                )
                for value in ordered
            }
        ) == 1
        comparison = ModelComparison(
            comparison_id=self._ids.next("model_comparison"),
            group_id=group_id,
            output_port=prediction.output_port,
            status=ModelComparisonStatus.COMPLETE,
            compared_time=self._current_time,
            member_components=members,
            prediction_ids={value.component_id: value.prediction_id for value in ordered},
            result_digests={value.component_id: value.result_digest for value in ordered},
            input_ids=prediction.input_ids,
            admitted_input_ids=prediction.admitted_input_ids,
            outputs_equal=outputs_equal,
        )
        self._emit(evidence, "model_comparison", comparison.to_payload())
        del self._pending_model_comparisons[key]

    def _finalize_model_comparisons(
        self,
        status: GraphRunStatus,
        evidence: list[EvidenceRecord],
    ) -> None:
        for key in sorted(self._pending_model_comparisons):
            pending = self._pending_model_comparisons[key]
            first = next(iter(pending.values()))
            group_id = first.comparison_group
            if group_id is None:  # pragma: no cover - guarded on admission
                continue
            members = self._comparison_members[group_id]
            missing = tuple(value for value in members if value not in pending)
            comparison = ModelComparison(
                comparison_id=self._ids.next("model_comparison"),
                group_id=group_id,
                output_port=first.output_port,
                status=ModelComparisonStatus.INCOMPLETE,
                compared_time=self._current_time,
                member_components=members,
                prediction_ids={
                    value.component_id: value.prediction_id
                    for value in pending.values()
                },
                result_digests={
                    value.component_id: value.result_digest
                    for value in pending.values()
                },
                input_ids=first.input_ids,
                admitted_input_ids=first.admitted_input_ids,
                missing_members=missing,
                reason_code=f"phase_{status.value}",
            )
            self._emit(evidence, "model_comparison", comparison.to_payload())
        self._pending_model_comparisons.clear()

    def _validate_model_role_input(
        self,
        node: RuntimeNode,
        input_port: str,
        value: Any,
    ) -> None:
        if node.plugin.kind == ComponentKind.MODEL:
            rejection = causal_model_input_rejection(
                self.plan.execution_mode,
                value_type(value),
                model_input_safety=self._model_input_safety.get(
                    f"{node.component_id}.{input_port}"
                ),
            )
            if rejection is not None:
                raise PermissionError(rejection)
        if node.plugin.kind != ComponentKind.POLICY or not isinstance(value, Prediction):
            return
        emitted = self._emitted_predictions.get(value.prediction_id)
        if emitted is None or emitted != value:
            raise PermissionError(
                "policy inputs must be canonical predictions emitted by this execution"
            )
        try:
            source = self.runtime.node(value.component_id)
        except KeyError as exc:
            raise PermissionError("prediction component is not part of the locked plan") from exc
        binding = source.model_binding
        if binding is None:
            raise PermissionError("prediction does not have a planned model binding")
        if (
            value.plugin_id != binding.plugin_id
            or value.plugin_version != binding.plugin_version
            or value.manifest_digest != binding.manifest_digest
            or value.contract_digest != binding.contract_digest
            or value.role_id != binding.role.role_id
        ):
            raise PermissionError("prediction identity differs from its planned model binding")
        if not binding.role.may_feed_policy:
            raise PermissionError(
                f"model role {binding.role.role_id} may not feed policy"
            )

    def _root_input_ids(self, input_id: str | None) -> tuple[str, ...]:
        if input_id is None:
            return ()
        normalized = require_identifier(input_id, "input_id")
        return self._admitted_input_lineage.get(normalized, (normalized,))

    def _record_model_result_disposition(
        self,
        node: RuntimeNode,
        output_port: str,
        prediction: Prediction,
        status: ModelResultDispositionStatus,
        evidence: list[EvidenceRecord],
        *,
        reason_code: str | None = None,
    ) -> None:
        disposition = ModelResultDisposition(
            disposition_id=self._ids.next("model_result_disposition"),
            component_id=node.component_id,
            output_port=output_port,
            status=status,
            decided_time=self._current_time,
            result_digest=prediction.result_digest,
            prediction_id=prediction.prediction_id,
            reason_code=reason_code,
        )
        self._emit(evidence, "model_result_disposition", disposition.to_payload())

    def _record_rejected_model_result(
        self,
        node: RuntimeNode,
        error: ModelResultRejected,
        evidence: list[EvidenceRecord],
    ) -> None:
        for violation in error.violations:
            disposition = ModelResultDisposition(
                disposition_id=self._ids.next("model_result_disposition"),
                component_id=node.component_id,
                output_port=violation.output_port,
                status=ModelResultDispositionStatus.REJECTED,
                decided_time=self._current_time,
                result_digest=violation.result_digest,
                reason_code=violation.reason_code,
            )
            self._emit(evidence, "model_result_disposition", disposition.to_payload())

    def _record_cancelled_model_results(
        self,
        queue: EventQueue,
        status: GraphRunStatus,
        evidence: list[EvidenceRecord],
    ) -> None:
        reason_code = f"phase_{status.value}"
        seen: set[str] = set()
        for event in queue.events():
            if event.kind != "emission" or not isinstance(event.value, PendingEmission):
                continue
            prediction = event.value.value
            if not isinstance(prediction, Prediction):
                continue
            if prediction.prediction_id in seen:
                continue
            seen.add(prediction.prediction_id)
            node = self.runtime.node(event.component_id)
            if node.model_binding is None:
                continue
            self._record_model_result_disposition(
                node,
                event.value.output_port,
                prediction,
                ModelResultDispositionStatus.CANCELLED,
                evidence,
                reason_code=reason_code,
            )

    def _schedule_work(
        self,
        queue: EventQueue,
        component_id: str,
        input_port: str,
        value: Any,
        *,
        source_endpoint: str,
        input_id: str,
    ) -> WorkSchedulingDisposition:
        available = available_time(value, self._current_time)
        self._require_execution_time(available)
        if available.seconds < self._current_time.seconds:
            lateness = self._current_time.seconds - available.seconds
            if lateness > self.allowed_lateness_seconds:
                if self.lateness == "fail_run":
                    raise RuntimeError(
                        f"input exceeded allowed lateness by "
                        f"{lateness - self.allowed_lateness_seconds:.9g} seconds"
                    )
                return WorkSchedulingDisposition("late_input")
            available = self._current_time
        node = self.runtime.node(component_id)
        role = None if node.model_binding is None else node.model_binding.role
        evicted: QueuedEvent | None = None
        if role is not None and role.queue_limit is not None:
            queued = queue.count_component(component_id, kind="work")
            if queued >= role.queue_limit:
                if role.queue_disposition == PlannedModelQueueDisposition.FAIL_RUN:
                    raise RuntimeError(
                        f"model role {role.role_id} exceeded queue_limit={role.queue_limit}"
                    )
                if role.queue_disposition == PlannedModelQueueDisposition.REJECT_NEWEST:
                    return WorkSchedulingDisposition("model_role_queue_limit")
                if role.queue_disposition == PlannedModelQueueDisposition.SHED_OLDEST:
                    evicted = queue.pop_oldest_component(component_id, kind="work")
                    if evicted is None or role.queue_limit == 0:
                        return WorkSchedulingDisposition("model_role_queue_limit")
        priority = 25
        if role is not None:
            priority = role.scheduling_priority
        self._event_sequence += 1
        accepted = queue.push(
            QueuedEvent(
                (
                    available.seconds,
                    4,
                    priority,
                    component_id,
                    input_port,
                    input_id,
                    self._event_sequence,
                ),
                "work",
                component_id,
                input_port,
                value,
                available,
                source_endpoint,
                input_id,
            ),
        )
        return WorkSchedulingDisposition(
            None if accepted else "queue_full",
            evicted,
        )

    def _record_scheduling_disposition(
        self,
        scheduling: WorkSchedulingDisposition,
        component_id: str,
        input_id: str,
        evidence: list[EvidenceRecord],
        work: PhaseRecordBuffer[WorkRecord],
    ) -> None:
        evicted = scheduling.evicted_event
        if evicted is None:
            return
        evicted_input = evicted.input_id
        if evicted_input is None:
            raise RuntimeError("evicted model work lacks an input identity")
        self._record_skipped_work(
            component_id,
            evicted_input,
            "model_role_shed_oldest",
            evidence,
            work,
        )

    def _record_skipped_work(
        self,
        component_id: str,
        input_id: str,
        reason_code: str,
        evidence: list[EvidenceRecord],
        work: PhaseRecordBuffer[WorkRecord],
    ) -> None:
        node = self.runtime.node(component_id)
        skipped = WorkRecord(
            work_id=self._ids.next("work"),
            component_id=component_id,
            stage=node.plugin.kind.value,
            status=WorkStatus.SKIPPED,
            started_time=self._current_time,
            completed_time=self._current_time,
            input_ids=(input_id,),
            role=node.planned.role,
            reason_code=reason_code,
        )
        work.append(skipped)
        self._emit(evidence, "work", skipped.to_payload())

    def _schedule_phase_triggers(
        self,
        phase: PlannedPhase,
        queue: EventQueue,
        phase_started_time: TimePoint,
    ) -> None:
        for planned in self.plan.scheduled_triggers:
            if planned.phase_id != phase.phase_id:
                continue
            scheduled = TimePoint(
                phase_started_time.seconds + planned.scheduled_offset_seconds,
                self.execution_clock_id,
            )
            deadline = (
                None
                if planned.deadline_offset_seconds is None
                else TimePoint(
                    phase_started_time.seconds + planned.deadline_offset_seconds,
                    self.execution_clock_id,
                )
            )
            self._schedule_trigger(
                queue,
                ScheduledTrigger(
                    trigger_id=planned.trigger_id,
                    target_component_id=planned.target_component,
                    scheduled_time=scheduled,
                    deadline_time=deadline,
                    payload=planned.payload,
                ),
            )

    def _schedule_state_triggers(
        self,
        phase_id: str,
        transition: StateTransition,
        active: set[str],
        queue: EventQueue,
        evidence: list[EvidenceRecord],
    ) -> None:
        for rule in self.plan.state_triggers:
            if rule.phase_id != phase_id or rule.target_component not in active:
                continue
            if rule.once and rule.rule_id in self._fired_state_rules:
                continue
            if rule.source_component != transition.component_id:
                continue
            if rule.transition_kind is not None and rule.transition_kind != transition.transition_kind:
                continue
            if transition.status.value not in rule.statuses:
                continue
            trigger = ScheduledTrigger(
                trigger_id=f"{rule.rule_id}.{transition.transition_id}",
                target_component_id=rule.target_component,
                scheduled_time=TimePoint(
                    transition.transition_time.seconds + rule.delay_seconds,
                    transition.transition_time.clock_id,
                ),
                parent_trigger_id=transition.trigger_ids[-1],
                payload={
                    **thaw_json(rule.payload),
                    "rule_id": rule.rule_id,
                    "transition_id": transition.transition_id,
                },
            )
            self._schedule_trigger(queue, trigger)
            if rule.once:
                self._fired_state_rules.add(rule.rule_id)
            self._emit(
                evidence,
                "state_trigger_scheduled",
                {"rule_id": rule.rule_id, "trigger": trigger.to_payload()},
            )

    def _schedule_trigger(self, queue: EventQueue, trigger: ScheduledTrigger) -> None:
        self._require_execution_time(trigger.scheduled_time)
        self._event_sequence += 1
        queue.push(
            QueuedEvent(
                (
                    trigger.scheduled_time.seconds,
                    0,
                    trigger.target_component_id,
                    trigger.trigger_id,
                    self._event_sequence,
                ),
                "trigger",
                trigger.target_component_id,
                "trigger",
                trigger,
                trigger.scheduled_time,
                input_id=trigger.trigger_id,
            ),
            rejectable=False,
        )

    def _handle_trigger(
        self,
        phase: PlannedPhase,
        node: RuntimeNode,
        trigger: Any,
        active: set[str],
        queue: EventQueue,
        evidence: list[EvidenceRecord],
        work: PhaseRecordBuffer[WorkRecord],
    ) -> None:
        if not isinstance(trigger, ScheduledTrigger):
            raise TypeError("invalid scheduled trigger event")
        if trigger.deadline_time is not None and (
            self._current_time.seconds > trigger.deadline_time.seconds
        ):
            self._record_skipped_work(
                node.component_id,
                trigger.trigger_id,
                "trigger_deadline",
                evidence,
                work,
            )
            return
        handler = getattr(node.component, "handle_trigger", None)
        if not callable(handler):
            raise TypeError(f"component {node.component_id} cannot handle triggers")
        started = self._current_time
        result = handler(trigger, self._context(node))
        if not isinstance(result, TriggerResult):
            raise TypeError("trigger handler must return TriggerResult")
        completed = completed_work(
            self._ids,
            node,
            "trigger",
            (trigger.trigger_id,),
            started,
            self._current_time,
        )
        work.append(completed)
        self._emit(evidence, "work", completed.to_payload())
        self._emit(
            evidence,
            "trigger_result",
            {
                "phase_id": phase.phase_id,
                "trigger": trigger.to_payload(),
                "disposition": result.disposition.value,
                "details": thaw_json(result.details),
            },
        )
        if result.transition is not None:
            self._emit(evidence, "state_transition", result.transition.to_payload())
            self._schedule_state_triggers(
                phase.phase_id,
                result.transition,
                active,
                queue,
                evidence,
            )
        if result.next_trigger is not None:
            self._schedule_trigger(queue, result.next_trigger)

    def _create_checkpoint(
        self,
        phase: PlannedPhase,
        phase_started_time: TimePoint,
        evidence: list[EvidenceRecord],
    ) -> EngineCheckpoint:
        checkpoint_id = self._ids.next("checkpoint")
        self._emit(
            evidence,
            "checkpoint_created",
            {"checkpoint_id": checkpoint_id, "phase_id": phase.phase_id},
        )
        segment_digest = canonical_hash([value.to_payload() for value in evidence])
        prefix_digest = canonical_hash(
            {"prior": self._evidence_prefix_digest, "segment": segment_digest}
        )
        return EngineCheckpoint(
            checkpoint_id=checkpoint_id,
            execution_id=self.execution_id,
            plan_hash=self.plan.plan_hash,
            created_time=self._current_time,
            evidence_prefix_digest=prefix_digest,
            state={
                "phase_id": phase.phase_id,
                "runtime_snapshot": self.runtime.snapshot_state().to_payload(),
                "component_state": thaw_json(self.snapshot_component_state()),
                "executor": {
                    "current_time": self._current_time.to_payload(),
                    "phase_started_time": phase_started_time.to_payload(),
                    "id_source": self._ids.snapshot_state(),
                    "evidence_sequence": self._evidence_sequence,
                    "event_sequence": self._event_sequence,
                    "clock_mapping_revisions": dict(self._clock_mapping_revisions),
                    "fired_state_rules": sorted(self._fired_state_rules),
                    "admitted_input_lineage": {
                        key: list(value)
                        for key, value in sorted(
                            self._admitted_input_lineage.items()
                        )
                    },
                    "pending_model_comparisons": {
                        key: {
                            component_id: prediction.to_payload()
                            for component_id, prediction in sorted(value.items())
                        }
                        for key, value in sorted(
                            self._pending_model_comparisons.items()
                        )
                    },
                    "outcome_lifecycle": thaw_json(self._outcomes.snapshot()),
                },
            },
        )

    def _schedule_emission(
        self,
        queue: EventQueue,
        node: RuntimeNode,
        output_port: str,
        value: Any,
        input_ids: tuple[str, ...],
        available: TimePoint,
    ) -> None:
        self._event_sequence += 1
        queue.push(
            QueuedEvent(
                (
                    available.seconds,
                    1,
                    node.component_id,
                    output_port,
                    value_id(value),
                    self._event_sequence,
                ),
                "emission",
                node.component_id,
                output_port,
                PendingEmission(output_port, value, input_ids),
                available,
            ),
            rejectable=False,
        )

    def _schedule_work_completion(
        self,
        queue: EventQueue,
        node: RuntimeNode,
        stage: str,
        input_ids: tuple[str, ...],
        started: TimePoint,
        completed: TimePoint,
        *,
        status: WorkStatus = WorkStatus.COMPLETED,
        deadline_time: TimePoint | None = None,
        reason_code: str | None = None,
    ) -> None:
        self._event_sequence += 1
        queue.push(
            QueuedEvent(
                (
                    completed.seconds,
                    2,
                    node.component_id,
                    stage,
                    self._event_sequence,
                ),
                "work_completion",
                node.component_id,
                stage,
                PendingWork(
                    stage,
                    input_ids,
                    started,
                    status,
                    deadline_time,
                    reason_code,
                ),
                completed,
            ),
            rejectable=False,
        )

    def _context(self, node: RuntimeNode) -> RuntimeExecutionContext:
        return RuntimeExecutionContext(
            execution_id=self.execution_id,
            component_id=node.component_id,
            component_version=node.planned.plugin_version,
            execution_mode=self.plan.execution_mode,
            current_time=self._current_time,
            clock_mapping_revisions=self._clock_mapping_revisions,
            _ids=self._ids,
        )

    def _advance_time(self, point: TimePoint) -> None:
        self._require_execution_time(point)
        if point.seconds < self._current_time.seconds:
            raise ValueError("graph semantic time moved backwards")
        self._current_time = point

    def _capture_input(self, value: Any) -> None:
        if self._capture_sink is not None and isinstance(
            value,
            (DenseSampleBatch, SparseEventBatch, MetadataEvent),
        ):
            self._capture_sink(value)

    def _require_execution_time(self, point: TimePoint) -> None:
        if point.clock_id != self.execution_clock_id:
            raise ValueError(
                f"record uses clock {point.clock_id}; expected {self.execution_clock_id}"
            )

    def _emit(
        self,
        target: list[EvidenceRecord],
        record_type: str,
        payload: Mapping[str, Any],
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            record_id=self._ids.next("evidence"),
            record_type=record_type,
            sequence=self._evidence_sequence,
            emitted_time=self._current_time,
            payload=payload,
        )
        if self._evidence_sink is not None:
            self._evidence_sink(record)
        if self._retain_evidence:
            target.append(record)
        self._evidence_sequence += 1
        return record
