"""Generic deterministic execution of the typed graph locked in a plan."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Mapping

from eegle._domain import ComponentKind, WorkStatus
from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.compiler.plan import PlannedPhase
from eegle.recording.evidence import EvidenceRecord
from eegle.recording.publications import ArtifactPublication
from eegle.runtime.admission import (
    SourceAdmissionState,
    poll_source,
    watermark_blockers,
)
from eegle.runtime.context import DeterministicIdSource, RuntimeExecutionContext
from eegle.runtime.checkpoints import EngineCheckpoint
from eegle.runtime.plan_runtime import (
    PlanRuntime,
    PlanRuntimeSnapshot,
    RuntimeNode,
)
from eegle.runtime.queueing import EventQueue, PendingEmission, QueuedEvent
from eegle.runtime.routing import (
    available_time,
    dispatch_component,
    sequence_key,
    validate_input_port,
    value_id,
    value_payload,
    value_type,
)
from eegle.runtime.scheduling import ScheduledTrigger, TriggerResult
from eegle.runtime.state import StateTransition, WorkRecord
from eegle.runtime.work import (
    PendingWork,
    completed_work,
    component_deadline,
    failed_work,
)
from eegle.streams.clocks import TimePoint


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

    def emissions_from(self, component_id: str, output_port: str) -> tuple[Any, ...]:
        return tuple(
            value.value
            for value in self.emissions
            if value.component_id == component_id and value.output_port == output_port
        )


class PlanGraphExecutor:
    """Execute any active phase subgraph from one exact :class:`PlanRuntime`."""

    def __init__(
        self,
        runtime: PlanRuntime,
        *,
        execution_id: str | None = None,
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
        self._component_order = {
            component_id: index
            for index, component_id in enumerate(self.plan.graph.component_order)
        }
        validation = self.plan.validation_rules.get("suite", {})
        self.max_events = int(validation.get("max_graph_events", 100_000))
        self.max_pending_events = int(validation.get("max_pending_events", 1_024))
        self.max_idle_cycles = int(validation.get("max_idle_cycles", 1))
        scheduling = self.plan.scheduling_policy
        self.backpressure = str(scheduling.get("backpressure", "fail_run"))
        self.primary_first = bool(scheduling.get("primary_first", True))
        raw_shadow_limit = scheduling.get("shadow_queue_limit")
        self.shadow_queue_limit = (
            None if raw_shadow_limit is None else int(raw_shadow_limit)
        )
        self.shadow_failure = str(scheduling.get("shadow_failure", "fail_run"))
        if self.backpressure not in {"fail_run", "reject_newest"}:
            raise ValueError("unknown graph backpressure policy")
        if self.shadow_failure not in {"fail_run", "continue"}:
            raise ValueError("unknown shadow failure policy")
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
        self._evidence_sequence = 0
        self._event_sequence = 0
        self._current_time = TimePoint(0.0, self.execution_clock_id)
        self._component_state: dict[str, dict[str, Any]] = {}
        self._clock_mapping_revisions = {
            str(key): int(value)
            for key, value in self.plan.clock_policy.get(
                "mapping_revisions", {}
            ).items()
        }
        self._cancel_requested = False
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

        records: list[EvidenceRecord] = []
        self._emit(records, record_type, payload)
        return records[0]

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
        self._evidence_prefix_digest = checkpoint.evidence_prefix_digest
        self._resumed_phase_id = phase_id
        self._resumed_phase_started_time = TimePoint.from_payload(
            executor["phase_started_time"]
        )
        self._require_execution_time(self._resumed_phase_started_time)
        return phase_id

    def cancel(self) -> None:
        self._cancel_requested = True

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
        active = set(planned_phase.component_ids)
        evidence: list[EvidenceRecord] = []
        work: list[WorkRecord] = []
        emissions: list[GraphEmission] = []
        admitted: list[Any] = []
        queue = EventQueue(
            self.max_pending_events,
            reject_newest=self.backpressure == "reject_newest",
        )
        admission = SourceAdmissionState()
        status = GraphRunStatus.COMPLETE
        failure: str | None = None
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
            for graph_input in inputs:
                if resumed:
                    raise ValueError("resumed phase cannot accept replacement external inputs")
                if graph_input.target_component not in active:
                    raise ValueError(
                        f"graph input targets inactive component {graph_input.target_component}"
                    )
                node = self.runtime.node(graph_input.target_component)
                validate_input_port(node, graph_input.target_port, graph_input.value)
                rejected = self._schedule_work(
                    queue,
                    graph_input.target_component,
                    graph_input.target_port,
                    graph_input.value,
                    source_endpoint="external.input",
                    input_id=value_id(graph_input.value),
                )
                if rejected is not None:
                    self._record_skipped_work(
                        graph_input.target_component,
                        value_id(graph_input.value),
                        rejected,
                        evidence,
                        work,
                    )
                admitted.append(graph_input.value)
            if not resumed:
                self._schedule_phase_triggers(planned_phase, queue, phase_started_time)
            for node in self._ordered_nodes(active, kind=ComponentKind.SOURCE):
                self._poll_source(
                    node,
                    queue,
                    admission,
                )

            idle_cycles = 0
            event_count = 0
            while queue or admission.incomplete_sources:
                if self._cancel_requested:
                    status = GraphRunStatus.CANCELLED
                    break
                if not queue:
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
                        ):
                            progressed = True
                    if progressed:
                        idle_cycles = 0
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
                        ):
                            progressed = True
                    if progressed:
                        idle_cycles = 0
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
                        checkpoint_after_inputs is None
                        and not bool(getattr(node.component, "exhausted", False))
                    ):
                        self._poll_source(node, queue, admission)
                    elif not bool(getattr(node.component, "exhausted", False)):
                        admission.incomplete_sources.add(node.component_id)
                    continue
                started = self._current_time
                try:
                    outputs = dispatch_component(
                        node,
                        event.port,
                        event.value,
                        self._context(node),
                        self._component_state,
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
                    if node.planned.role == "shadow" and self.shadow_failure == "continue":
                        self._emit(
                            evidence,
                            "shadow_failure_continued",
                            {
                                "component_id": node.component_id,
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
            admitted_inputs=tuple(admitted),
            emissions=tuple(emissions),
            work=tuple(work),
            artifacts=tuple(
                value.value
                for value in emissions
                if isinstance(value.value, ArtifactPublication)
            ),
            failure=failure,
            checkpoint=checkpoint,
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
    ) -> bool:
        admitted = poll_source(node, admission, self._require_execution_time)
        if admitted is None:
            return False
        packet = admitted.packet
        available = admitted.available_time
        self._event_sequence += 1
        queue.push(
            QueuedEvent(
                (
                    available.seconds,
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
                available,
            ),
            rejectable=False,
        )
        return True

    def _record_emission(
        self,
        node: RuntimeNode,
        output_port: str,
        value: Any,
        input_ids: tuple[str, ...],
        active: set[str],
        queue: EventQueue,
        evidence: list[EvidenceRecord],
        emissions: list[GraphEmission],
        work: list[WorkRecord],
        phase_id: str,
    ) -> TimePoint:
        port = next(
            (value_port for value_port in node.descriptor.output_ports if value_port.name == output_port),
            None,
        )
        if port is None:
            raise ValueError(f"component {node.component_id} has no output port {output_port}")
        observed_type = value_type(value)
        if observed_type != port.type_id:
            raise TypeError(
                f"component {node.component_id}.{output_port} emitted {observed_type}; "
                f"expected {port.type_id}"
            )
        available = available_time(value, self._current_time)
        self._require_execution_time(available)
        if available.seconds < self._current_time.seconds:
            raise ValueError(
                f"component {node.component_id} emitted output before its input was available"
            )
        emitted_value_id = value_id(value)
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
        self._emit(evidence, "graph_emission", emission.to_payload())
        if isinstance(value, StateTransition):
            self._schedule_state_triggers(phase_id, value, active, queue, evidence)
        targets = self._targets.get((node.component_id, output_port), ())
        if not targets:
            return available
        for component_id, input_port in targets:
            if component_id not in active:
                continue
            rejected = self._schedule_work(
                queue,
                component_id,
                input_port,
                value,
                source_endpoint=emission.endpoint,
                input_id=emitted_value_id,
            )
            if rejected is not None:
                self._record_skipped_work(
                    component_id,
                    emitted_value_id,
                    rejected,
                    evidence,
                    work,
                )
        return available

    def _schedule_work(
        self,
        queue: EventQueue,
        component_id: str,
        input_port: str,
        value: Any,
        *,
        source_endpoint: str,
        input_id: str,
    ) -> str | None:
        available = available_time(value, self._current_time)
        self._require_execution_time(available)
        planned = self.runtime.node(component_id).planned
        if (
            planned.role == "shadow"
            and self.shadow_queue_limit is not None
            and queue.count_component(component_id) >= self.shadow_queue_limit
        ):
            return "shadow_queue_limit"
        priority = 4
        if self.primary_first and planned.role == "primary":
            priority = 3
        elif self.primary_first and planned.role == "shadow":
            priority = 5
        self._event_sequence += 1
        accepted = queue.push(
            QueuedEvent(
                (
                    available.seconds,
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
        return None if accepted else "queue_full"

    def _record_skipped_work(
        self,
        component_id: str,
        input_id: str,
        reason_code: str,
        evidence: list[EvidenceRecord],
        work: list[WorkRecord],
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
        work: list[WorkRecord],
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
    ) -> None:
        target.append(
            EvidenceRecord(
                record_id=self._ids.next("evidence"),
                record_type=record_type,
                sequence=self._evidence_sequence,
                emitted_time=self._current_time,
                payload=payload,
            )
        )
        self._evidence_sequence += 1
