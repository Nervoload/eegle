"""The deterministic semantic execution engine shared by simulation and replay."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import heapq
from typing import Any, Mapping

from eegle._domain import (
    ComponentKind,
    EquivalenceLevel,
    ExecutionMode,
    WorkStatus,
)
from eegle._validation import require_finite, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.compiler.plan import ExecutionPlan
from eegle.actions.receipts import ActionReceipt
from eegle.models.predictions import Prediction
from eegle.models.roles import ModelRole, ModelRoleKind
from eegle.processing.quality import QualityStatus
from eegle.processing.windows import DenseWindow
from eegle.recording.evidence import EvidenceRecord
from eegle.recording.sinks import InMemoryEvidenceSink
from eegle.runtime.checkpoints import EngineCheckpoint
from eegle.runtime.context import DeterministicIdSource, RuntimeExecutionContext
from eegle.runtime.outcomes import (
    Outcome,
    OutcomeRoutingPolicy,
    OutcomeUse,
    PendingPredictionOverflow,
)
from eegle.runtime.scheduling import (
    BackpressurePolicy,
    LatenessPolicy,
    ProcessBoundary,
    ScheduledTrigger,
    SchedulingPolicy,
    StateTriggerRule,
    TriggerDisposition,
    TriggerResult,
)
from eegle.runtime.state import StateTransition, WorkRecord
from eegle.streams.clocks import TimePoint
from eegle.streams.packets import DenseSampleBatch, MetadataEvent, Packet, SparseEventBatch
from eegle.streams.synthetic import packet_available_time


class EngineStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EngineExecutionError(RuntimeError):
    pass


class EngineCheckpointError(EngineExecutionError):
    pass


@dataclass(frozen=True, slots=True)
class ComponentBinding:
    component_id: str
    component_version: str
    component: Any
    equivalence: EquivalenceLevel
    boundary: ProcessBoundary = ProcessBoundary()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component_id", require_identifier(self.component_id, "component_id")
        )
        if not str(self.component_version).strip():
            raise ValueError("component_version cannot be empty")
        object.__setattr__(self, "equivalence", EquivalenceLevel(self.equivalence))


@dataclass(frozen=True, slots=True)
class SourceBinding:
    component: ComponentBinding

    def __post_init__(self) -> None:
        source = self.component.component
        for member in ("stream_spec", "read", "close"):
            value = getattr(source, member, None)
            if member == "stream_spec" and value is None:
                raise TypeError("source binding requires stream_spec")
            if member != "stream_spec" and not callable(value):
                raise TypeError(f"source binding requires callable {member}")


@dataclass(frozen=True, slots=True)
class ModelBinding:
    component: ComponentBinding
    role: ModelRole
    deadline_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "deadline_seconds",
            require_finite(self.deadline_seconds, "deadline_seconds"),
        )
        if self.deadline_seconds < 0:
            raise ValueError("deadline_seconds cannot be negative")
        if not callable(getattr(self.component.component, "predict", None)):
            raise TypeError("model binding component must implement predict")


@dataclass(frozen=True, slots=True)
class TriggerBinding:
    component: ComponentBinding

    def __post_init__(self) -> None:
        if not callable(getattr(self.component.component, "handle_trigger", None)):
            raise TypeError("trigger binding component must implement handle_trigger")


@dataclass(frozen=True, slots=True)
class EngineComponents:
    transform: ComponentBinding
    window: ComponentBinding
    quality: ComponentBinding
    models: tuple[ModelBinding, ...]
    policy: ComponentBinding
    trigger_handlers: tuple[TriggerBinding, ...] = ()
    actuator: ComponentBinding | None = None

    def __post_init__(self) -> None:
        required = (
            (self.transform, "update"),
            (self.window, "update"),
            (self.quality, "evaluate"),
            (self.policy, "decide"),
        )
        for binding, method in required:
            if not callable(getattr(binding.component, method, None)):
                raise TypeError(f"component {binding.component_id} must implement {method}")
        if not self.models:
            raise ValueError("engine requires at least one model")
        primary = [value for value in self.models if value.role.kind == ModelRoleKind.PRIMARY]
        if len(primary) != 1:
            raise ValueError("engine requires exactly one primary model role")
        role_ids = tuple(value.role.role_id for value in self.models)
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("engine model role identities must be unique")
        trigger_ids = tuple(
            value.component.component_id for value in self.trigger_handlers
        )
        if len(trigger_ids) != len(set(trigger_ids)):
            raise ValueError("engine trigger handler identities must be unique")
        if self.actuator is not None and not callable(
            getattr(self.actuator.component, "submit", None)
        ):
            raise TypeError("actuator binding component must implement submit")

    @property
    def scheduled_models(self) -> tuple[ModelBinding, ...]:
        order = {
            ModelRoleKind.PRIMARY: 0,
            ModelRoleKind.CANDIDATE: 1,
            ModelRoleKind.SHADOW: 2,
            ModelRoleKind.OBSERVER: 3,
        }
        return tuple(
            sorted(
                self.models,
                key=lambda value: (
                    order[value.role.kind],
                    -value.role.scheduling_priority,
                    value.component.component_id,
                ),
            )
        )


@dataclass(frozen=True, slots=True)
class EngineRunResult:
    execution_id: str
    plan_hash: str
    status: EngineStatus
    evidence: tuple[EvidenceRecord, ...]
    captured_packets: tuple[Packet, ...]
    work: tuple[WorkRecord, ...]
    predictions: tuple[Prediction, ...]
    equivalence_ceiling: EquivalenceLevel
    failure: str | None = None
    checkpoint: EngineCheckpoint | None = None


@dataclass(order=True, slots=True)
class _QueuedPacket:
    sort_key: tuple[Any, ...]
    source_id: str = field(compare=False)
    packet: Packet = field(compare=False)


@dataclass(order=True, slots=True)
class _QueuedOutcome:
    sort_key: tuple[Any, ...]
    outcome: Outcome = field(compare=False)


@dataclass(order=True, slots=True)
class _QueuedTrigger:
    sort_key: tuple[Any, ...]
    trigger: ScheduledTrigger = field(compare=False)


@dataclass(frozen=True, slots=True)
class _PendingPrediction:
    prediction: Prediction
    registered_time: TimePoint
    expires_time: TimePoint


class ExecutionEngine:
    """Run a typed, locked plan through one deterministic semantic loop.

    Acquisition and heavyweight compute may live behind component proxies, but
    packet admission, ordering, causality, routing, work accounting, and
    evidence emission remain in this one engine.
    """

    def __init__(
        self,
        *,
        plan: ExecutionPlan,
        sources: tuple[SourceBinding, ...],
        components: EngineComponents,
        scheduling: SchedulingPolicy,
        execution_id: str = "execution.1",
        evidence_sink: Any | None = None,
        clock_mapping_revisions: Mapping[str, int] | None = None,
        outcomes: tuple[Outcome, ...] = (),
        outcome_policy: OutcomeRoutingPolicy | None = None,
        scheduled_triggers: tuple[ScheduledTrigger, ...] = (),
        state_triggers: tuple[StateTriggerRule, ...] = (),
    ) -> None:
        self.plan = plan
        self.sources = sources
        self.components = components
        self.scheduling = scheduling
        self.execution_id = require_identifier(execution_id, "execution_id")
        self.evidence_sink = evidence_sink or InMemoryEvidenceSink()
        if not callable(getattr(self.evidence_sink, "append", None)):
            raise TypeError("evidence_sink must implement append(record)")
        self.clock_mapping_revisions = {
            str(key): int(value) for key, value in (clock_mapping_revisions or {}).items()
        }
        if not sources:
            raise ValueError("execution engine requires at least one source")
        if plan.execution_mode not in {
            ExecutionMode.CAUSAL,
            ExecutionMode.RETROSPECTIVE,
            ExecutionMode.ORACLE,
        }:
            raise ValueError("unsupported execution mode")
        self._validate_plan_bindings()
        self._ids = DeterministicIdSource()
        self._evidence_sequence = 0
        self._evidence_records: list[EvidenceRecord] = []
        self._queue: list[_QueuedPacket] = []
        self._outcome_queue: list[_QueuedOutcome] = []
        self._trigger_queue: list[_QueuedTrigger] = []
        self._captured: list[Packet] = []
        self._work: list[WorkRecord] = []
        self._predictions: list[Prediction] = []
        self._pending_predictions: dict[str, _PendingPrediction] = {}
        self._processed_outcome_ids: set[str] = set()
        self._invalid_outcomes: list[tuple[str, str]] = []
        self.outcome_policy = outcome_policy or OutcomeRoutingPolicy()
        self.state_triggers = tuple(sorted(state_triggers, key=lambda value: value.rule_id))
        self._fired_state_trigger_rules: set[str] = set()
        self._cancelled_trigger_ids: set[str] = set()
        self._known_trigger_ids: set[str] = set()
        self._cancel_requested = False
        self._last_dispatched_seconds: float | None = None
        self._current_time = TimePoint(0.0, scheduling.execution_clock_id)
        self._semantic_items = 0
        self._checkpoint: EngineCheckpoint | None = None
        self._restored_from: str | None = None
        self._pending_predictions_finalized = False
        self._has_run = False
        for outcome in outcomes:
            self._enqueue_outcome(outcome)
        for trigger in scheduled_triggers:
            self._enqueue_trigger(trigger)
        self._validate_trigger_configuration()

    def cancel(self) -> None:
        self._cancel_requested = True

    def cancel_trigger(self, trigger_id: str) -> None:
        self._cancelled_trigger_ids.add(require_identifier(trigger_id, "trigger_id"))

    def restore_checkpoint(self, checkpoint: EngineCheckpoint) -> None:
        """Restore a verified checkpoint into this fresh engine and its components."""

        if self._has_run or self._evidence_records:
            raise EngineCheckpointError("checkpoint restoration requires a fresh engine")
        if not isinstance(checkpoint, EngineCheckpoint):
            raise TypeError("checkpoint must be an EngineCheckpoint")
        try:
            verified = EngineCheckpoint.from_payload(checkpoint.to_payload())
        except Exception as exc:
            raise EngineCheckpointError(f"checkpoint integrity validation failed: {exc}") from exc
        if verified.execution_id != self.execution_id:
            raise EngineCheckpointError("checkpoint execution_id mismatch")
        if verified.plan_hash != self.plan.plan_hash:
            raise EngineCheckpointError("checkpoint plan mismatch")
        state = thaw_json(verified.state)
        if state.get("run_status") != EngineStatus.PARTIAL.value:
            raise EngineCheckpointError("checkpoint does not represent a partial run")
        if state.get("scheduling") != self._scheduling_payload():
            raise EngineCheckpointError("checkpoint scheduling policy mismatch")
        if state.get("clock_mapping_revisions") != self.clock_mapping_revisions:
            raise EngineCheckpointError("checkpoint clock-mapping revisions mismatch")
        expected_bindings = self._binding_signatures()
        if state.get("bindings") != expected_bindings:
            raise EngineCheckpointError("checkpoint component identity or version mismatch")
        if state.get("outcome_policy") != self._outcome_policy_payload():
            raise EngineCheckpointError("checkpoint outcome routing policy mismatch")
        if state.get("state_triggers") != self._state_trigger_payloads():
            raise EngineCheckpointError("checkpoint state-trigger configuration mismatch")
        prefix = tuple(
            EvidenceRecord.from_payload(value)
            for value in state.get("evidence_prefix", ())
        )
        if canonical_hash([value.to_payload() for value in prefix]) != (
            verified.evidence_prefix_digest
        ):
            raise EngineCheckpointError("checkpoint evidence prefix digest mismatch")
        evidence_sequence = int(state["evidence_sequence"])
        if evidence_sequence != len(prefix):
            raise EngineCheckpointError("checkpoint evidence frontier mismatch")
        component_states = {
            str(value["component_id"]): value
            for value in state.get("component_states", ())
        }
        for binding in self._all_bindings():
            snapshot = getattr(binding.component, "snapshot_state", None)
            restore = getattr(binding.component, "restore_state", None)
            stateful = callable(snapshot) or callable(restore)
            stored = component_states.get(binding.component_id)
            if not stateful:
                if stored is not None:
                    raise EngineCheckpointError(
                        f"stateless component {binding.component_id} has checkpoint state"
                    )
                continue
            if not callable(snapshot) or not callable(restore) or stored is None:
                raise EngineCheckpointError(
                    f"component {binding.component_id} cannot restore checkpoint state"
                )
            component_state = dict(stored["state"])
            if canonical_hash(component_state) != stored.get("state_hash"):
                raise EngineCheckpointError(
                    f"component {binding.component_id} checkpoint state hash mismatch"
                )
            try:
                restore(component_state)
            except Exception as exc:
                raise EngineCheckpointError(
                    f"component {binding.component_id} restore failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        if set(component_states) != {
            binding.component_id
            for binding in self._all_bindings()
            if callable(getattr(binding.component, "snapshot_state", None))
            or callable(getattr(binding.component, "restore_state", None))
        }:
            raise EngineCheckpointError("checkpoint component-state set mismatch")
        self._ids.restore_state(dict(state["id_state"]))
        self._current_time = TimePoint.from_payload(state["current_time"])
        last_dispatched = state.get("last_dispatched_seconds")
        self._last_dispatched_seconds = (
            None if last_dispatched is None else float(last_dispatched)
        )
        self._evidence_sequence = evidence_sequence
        self._evidence_records = list(prefix)
        restore_prefix = getattr(self.evidence_sink, "restore_prefix", None)
        if prefix and not callable(restore_prefix):
            raise EngineCheckpointError(
                "evidence sink cannot restore a verified checkpoint prefix"
            )
        if callable(restore_prefix):
            try:
                restore_prefix(prefix)
            except Exception as exc:
                raise EngineCheckpointError(
                    f"evidence sink prefix restore failed: {type(exc).__name__}: {exc}"
                ) from exc
        self._captured = [
            _packet_from_payload(value) for value in state.get("captured_packets", ())
        ]
        self._work = [WorkRecord.from_payload(value) for value in state.get("work", ())]
        self._predictions = [
            Prediction.from_payload(value) for value in state.get("predictions", ())
        ]
        self._queue = []
        for value in state.get("packet_queue", ()):
            packet = _packet_from_payload(value["packet"])
            source_id = str(value["source_id"])
            source_binding = next(
                (
                    candidate
                    for candidate in self.sources
                    if candidate.component.component_id == source_id
                ),
                None,
            )
            if source_binding is None:
                raise EngineCheckpointError(
                    f"checkpoint source {source_id} is absent from the engine"
                )
            self._validate_packet_source(
                source_binding, packet, packet_available_time(packet)
            )
            queued = _QueuedPacket(tuple(value["sort_key"]), source_id, packet)
            self._queue.append(queued)
        heapq.heapify(self._queue)
        self._outcome_queue = [
            _QueuedOutcome(tuple(value["sort_key"]), Outcome.from_payload(value["outcome"]))
            for value in state.get("outcome_queue", ())
        ]
        heapq.heapify(self._outcome_queue)
        self._trigger_queue = [
            _QueuedTrigger(
                tuple(value["sort_key"]),
                ScheduledTrigger.from_payload(value["trigger"]),
            )
            for value in state.get("trigger_queue", ())
        ]
        heapq.heapify(self._trigger_queue)
        self._pending_predictions = {}
        for value in state.get("pending_predictions", ()):
            pending = _PendingPrediction(
                prediction=Prediction.from_payload(value["prediction"]),
                registered_time=TimePoint.from_payload(value["registered_time"]),
                expires_time=TimePoint.from_payload(value["expires_time"]),
            )
            self._pending_predictions[pending.prediction.prediction_id] = pending
        if len(self._pending_predictions) > self.outcome_policy.max_pending_predictions:
            raise EngineCheckpointError("checkpoint pending-prediction bound exceeded")
        self._invalid_outcomes = [
            (str(value[0]), str(value[1]))
            for value in state.get("invalid_outcomes", ())
        ]
        self._processed_outcome_ids = set(
            str(value) for value in state.get("processed_outcome_ids", ())
        )
        self._cancelled_trigger_ids = set(
            str(value) for value in state.get("cancelled_trigger_ids", ())
        )
        self._known_trigger_ids = set(
            str(value) for value in state.get("known_trigger_ids", ())
        )
        self._fired_state_trigger_rules = set(
            str(value) for value in state.get("fired_state_trigger_rules", ())
        )
        self._semantic_items = int(state["semantic_items"])
        self._cancel_requested = bool(state.get("cancel_requested", False))
        self._pending_predictions_finalized = False
        self._checkpoint = None
        self._restored_from = verified.checkpoint_id
        self._validate_trigger_configuration()

    def _binding_signatures(self) -> list[dict[str, Any]]:
        planned = {value.component_id: value for value in self.plan.components}
        signatures: list[dict[str, Any]] = []
        for binding in self._all_bindings():
            component = planned[binding.component_id]
            signatures.append(
                {
                    "component_id": binding.component_id,
                    "component_version": binding.component_version,
                    "plugin_id": component.plugin_id,
                    "placement": binding.boundary.placement.value,
                    "endpoint_id": binding.boundary.endpoint_id,
                    "stateful": bool(
                        callable(getattr(binding.component, "snapshot_state", None))
                        or callable(getattr(binding.component, "restore_state", None))
                    ),
                }
            )
        return signatures

    def _outcome_policy_payload(self) -> dict[str, Any]:
        return {
            "max_pending_predictions": self.outcome_policy.max_pending_predictions,
            "prediction_ttl_seconds": self.outcome_policy.prediction_ttl_seconds,
            "overflow": self.outcome_policy.overflow.value,
            "allowed_uses": sorted(
                value.value for value in self.outcome_policy.allowed_uses
            ),
        }

    def _scheduling_payload(self) -> dict[str, Any]:
        return {
            "execution_clock_id": self.scheduling.execution_clock_id,
            "max_pending_packets": self.scheduling.max_pending_packets,
            "allowed_lateness_seconds": self.scheduling.allowed_lateness_seconds,
            "backpressure": self.scheduling.backpressure.value,
            "lateness": self.scheduling.lateness.value,
            "max_idle_cycles": self.scheduling.max_idle_cycles,
            "shadow_queue_limit": self.scheduling.shadow_queue_limit,
            "fail_fast": self.scheduling.fail_fast,
        }

    def _state_trigger_payloads(self) -> list[dict[str, Any]]:
        return [
            {
                "rule_id": rule.rule_id,
                "target_component_id": rule.target_component_id,
                "transition_kind": rule.transition_kind,
                "source_component_id": rule.source_component_id,
                "statuses": sorted(value.value for value in rule.statuses),
                "delay_seconds": rule.delay_seconds,
                "payload": thaw_json(rule.payload),
                "once": rule.once,
            }
            for rule in self.state_triggers
        ]

    def run(
        self, *, checkpoint_after_semantic_items: int | None = None
    ) -> EngineRunResult:
        if self._has_run:
            raise RuntimeError("ExecutionEngine instances are single-use")
        checkpoint_target: int | None = None
        if checkpoint_after_semantic_items is not None:
            requested = int(checkpoint_after_semantic_items)
            if requested <= 0:
                raise ValueError("checkpoint_after_semantic_items must be positive")
            checkpoint_target = self._semantic_items + requested
        self._has_run = True
        status = EngineStatus.COMPLETE
        failure: str | None = None
        idle_cycles = 0
        self._emit(
            "run_resumed" if self._restored_from is not None else "run_started",
            {
                "execution_id": self.execution_id,
                "plan_hash": self.plan.plan_hash,
                "execution_mode": self.plan.execution_mode.value,
                "checkpoint_id": self._restored_from,
            },
        )
        try:
            self._start_components()
            while True:
                if self._cancel_requested:
                    status = EngineStatus.CANCELLED
                    self._cancel_pending()
                    break
                if (
                    checkpoint_target is not None
                    and self._semantic_items >= checkpoint_target
                ):
                    self._checkpoint = self._create_checkpoint()
                    self._emit(
                        "engine_checkpoint_created",
                        {
                            "checkpoint_id": self._checkpoint.checkpoint_id,
                            "checkpoint_hash": self._checkpoint.checkpoint_hash,
                            "evidence_prefix_digest": (
                                self._checkpoint.evidence_prefix_digest
                            ),
                            "semantic_items": self._semantic_items,
                        },
                    )
                    status = EngineStatus.PARTIAL
                    break
                invalid_outcome = self._dispatch_invalid_outcome()
                polled = self._poll_sources()
                dispatched = self._dispatch_eligible(checkpoint_target)
                if (
                    self._all_sources_exhausted()
                    and not self._queue
                    and not self._outcome_queue
                    and not self._trigger_queue
                    and not self._invalid_outcomes
                ):
                    break
                if invalid_outcome or polled or dispatched:
                    idle_cycles = 0
                    continue
                idle_cycles += 1
                if idle_cycles >= self.scheduling.max_idle_cycles:
                    status = EngineStatus.PARTIAL
                    self._mark_pending()
                    break
        except Exception as exc:
            status = EngineStatus.FAILED
            failure = f"{type(exc).__name__}: {exc}"
            self._emit("run_failure", {"failure": failure})
            self._cancel_pending(reason_code="run_failed")
        finally:
            finalization_failures: list[str] = []
            if self._checkpoint is None:
                self._finalize_pending_predictions(status)
            finalization_failures.extend(self._emit_state_snapshots())
            finalization_failures.extend(self._stop_components())
            for source in self.sources:
                try:
                    source.component.component.close()
                except Exception as exc:  # pragma: no cover - defensive adapter boundary
                    finalization_failures.append(
                        f"source {source.component.component_id} close failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
            if finalization_failures:
                status = EngineStatus.FAILED
                finalization = "; ".join(finalization_failures)
                failure = finalization if failure is None else f"{failure}; {finalization}"
                self._emit("run_failure", {"failure": failure, "stage": "finalization"})
            self._emit(
                "run_finished",
                {
                    "execution_id": self.execution_id,
                    "status": status.value,
                    "failure": failure,
                    "captured_packet_count": len(self._captured),
                    "prediction_count": len(self._predictions),
                    "work_count": len(self._work),
                    "checkpoint_id": None
                    if self._checkpoint is None
                    else self._checkpoint.checkpoint_id,
                },
            )
        return EngineRunResult(
            execution_id=self.execution_id,
            plan_hash=self.plan.plan_hash,
            status=status,
            evidence=tuple(self._evidence_records),
            captured_packets=tuple(self._captured),
            work=tuple(self._work),
            predictions=tuple(self._predictions),
            equivalence_ceiling=_weakest_equivalence(self._all_bindings()),
            failure=failure,
            checkpoint=self._checkpoint,
        )

    def _validate_plan_bindings(self) -> None:
        planned = {component.component_id: component for component in self.plan.components}
        locked = {
            (plugin.plugin_id, plugin.version): plugin.kind for plugin in self.plan.plugins
        }
        expected: list[tuple[ComponentBinding, ComponentKind]] = [
            *((source.component, ComponentKind.SOURCE) for source in self.sources),
            (self.components.transform, ComponentKind.TRANSFORM),
            (self.components.window, ComponentKind.WINDOW),
            (self.components.quality, ComponentKind.QUALITY),
            *((model.component, ComponentKind.MODEL) for model in self.components.models),
            (self.components.policy, ComponentKind.POLICY),
            *(
                (handler.component, ComponentKind.ADAPTER)
                for handler in self.components.trigger_handlers
            ),
            *(
                ()
                if self.components.actuator is None
                else ((self.components.actuator, ComponentKind.ACTUATOR),)
            ),
        ]
        bound_ids = tuple(binding.component_id for binding, _ in expected)
        if len(bound_ids) != len(set(bound_ids)):
            raise ValueError("runtime component identities must be unique")
        for binding, kind in expected:
            component = planned.get(binding.component_id)
            if component is None:
                raise ValueError(f"runtime component {binding.component_id} is absent from plan")
            if component.plugin_version != binding.component_version:
                raise ValueError(f"runtime component {binding.component_id} version differs from plan")
            if locked[(component.plugin_id, component.plugin_version)] != kind:
                raise ValueError(
                    f"runtime component {binding.component_id} kind differs from locked plan"
                )

    def _validate_trigger_configuration(self) -> None:
        handler_ids = {
            value.component.component_id for value in self.components.trigger_handlers
        }
        for queued in self._trigger_queue:
            if queued.trigger.target_component_id not in handler_ids:
                raise ValueError(
                    f"trigger target {queued.trigger.target_component_id} has no handler"
                )
        for rule in self.state_triggers:
            if rule.target_component_id not in handler_ids:
                raise ValueError(
                    f"state trigger target {rule.target_component_id} has no handler"
                )

    def _poll_sources(self) -> bool:
        progressed = False
        for source_binding in sorted(
            self.sources, key=lambda value: value.component.component_id
        ):
            source = source_binding.component.component
            if bool(getattr(source, "exhausted", False)):
                continue
            try:
                packet = source.read()
            except Exception as exc:
                self._record_work(
                    component=source_binding.component,
                    stage="source",
                    status=WorkStatus.FAILED,
                    started=self._current_time,
                    completed=self._current_time,
                    input_ids=(),
                    reason_code="component_error",
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
                raise
            if packet is None:
                continue
            progressed = True
            self._admit(source_binding, packet)
        return progressed

    def _enqueue_outcome(self, outcome: Outcome) -> None:
        if not isinstance(outcome, Outcome):
            self._invalid_outcomes.append(
                (f"malformed_outcome.{len(self._invalid_outcomes) + 1}", "malformed_outcome")
            )
            return
        if outcome.available_time.clock_id != self.scheduling.execution_clock_id:
            self._invalid_outcomes.append(
                (outcome.outcome_id, "availability_clock_mismatch")
            )
            return
        heapq.heappush(
            self._outcome_queue,
            _QueuedOutcome(
                (
                    outcome.available_time.seconds,
                    outcome.source_id,
                    outcome.outcome_id,
                ),
                outcome,
            ),
        )

    def _enqueue_trigger(self, trigger: ScheduledTrigger) -> None:
        if not isinstance(trigger, ScheduledTrigger):
            raise TypeError("scheduled trigger inputs must be ScheduledTrigger records")
        if trigger.scheduled_time.clock_id != self.scheduling.execution_clock_id:
            raise ValueError("trigger scheduled_time must use the execution clock")
        if (
            self._has_run
            and trigger.scheduled_time.seconds < self._current_time.seconds
        ):
            raise ValueError("trigger cannot be scheduled before the current frontier")
        if trigger.trigger_id in self._known_trigger_ids:
            raise ValueError(f"duplicate trigger identity: {trigger.trigger_id}")
        self._known_trigger_ids.add(trigger.trigger_id)
        heapq.heappush(
            self._trigger_queue,
            _QueuedTrigger(
                (
                    trigger.scheduled_time.seconds,
                    trigger.target_component_id,
                    trigger.trigger_id,
                ),
                trigger,
            ),
        )

    def _dispatch_invalid_outcome(self) -> bool:
        if not self._invalid_outcomes:
            return False
        outcome_id, reason = self._invalid_outcomes.pop(0)
        self._processed_outcome_ids.add(outcome_id)
        self._emit(
            "outcome_rejected",
            {"outcome_id": outcome_id, "reason_code": reason},
        )
        self._record_work(
            component_id="engine.outcomes",
            stage="outcome",
            status=WorkStatus.REJECTED,
            started=self._current_time,
            completed=self._current_time,
            input_ids=(outcome_id,),
            reason_code=reason,
        )
        self._semantic_items += 1
        return True

    def _admit(self, source_binding: SourceBinding, packet: Packet) -> None:
        available = packet_available_time(packet)
        self._validate_packet_source(source_binding, packet, available)
        packet_id = _packet_id(packet)
        if (
            self._last_dispatched_seconds is not None
            and available.seconds
            < self._last_dispatched_seconds - self.scheduling.allowed_lateness_seconds
        ):
            self._reject_admission(
                source_binding.component.component_id,
                packet_id,
                available,
                "late_packet",
            )
            if self.scheduling.lateness == LatenessPolicy.FAIL_RUN:
                raise EngineExecutionError(f"late packet rejected: {packet_id}")
            return
        if len(self._queue) >= self.scheduling.max_pending_packets:
            self._reject_admission(
                source_binding.component.component_id,
                packet_id,
                available,
                "backpressure",
            )
            if self.scheduling.backpressure == BackpressurePolicy.FAIL_RUN:
                raise EngineExecutionError("input queue capacity exceeded")
            return
        queued = _QueuedPacket(
            sort_key=(
                available.seconds,
                source_binding.component.component_id,
                packet.stream_id,
                packet.stream_revision,
                _packet_sequence(packet),
                packet_id,
            ),
            source_id=source_binding.component.component_id,
            packet=packet,
        )
        heapq.heappush(self._queue, queued)
        self._captured.append(packet)
        self._emit(
            "input_admitted",
            {
                "source_id": queued.source_id,
                "packet": _packet_evidence(packet),
                "queue_depth": len(self._queue),
            },
            emitted_time=available,
        )

    def _dispatch_eligible(self, checkpoint_target: int | None = None) -> bool:
        dispatched = False
        while self._queue or self._outcome_queue or self._trigger_queue:
            watermark = self._global_watermark()
            candidates: list[tuple[tuple[Any, ...], str]] = []
            if self._queue and self._queue[0].sort_key[0] <= watermark:
                packet = self._queue[0]
                candidates.append(
                    ((packet.sort_key[0], 0, *packet.sort_key[1:]), "packet")
                )
            if self._outcome_queue and self._outcome_queue[0].sort_key[0] <= watermark:
                outcome = self._outcome_queue[0]
                candidates.append(
                    ((outcome.sort_key[0], 1, *outcome.sort_key[1:]), "outcome")
                )
            if self._trigger_queue and self._trigger_queue[0].sort_key[0] <= watermark:
                trigger = self._trigger_queue[0]
                candidates.append(
                    ((trigger.sort_key[0], 2, *trigger.sort_key[1:]), "trigger")
                )
            if not candidates:
                break
            sort_key, kind = min(candidates, key=lambda value: value[0])
            available = TimePoint(float(sort_key[0]), self.scheduling.execution_clock_id)
            dispatch_time = available
            if kind == "trigger" and watermark != float("inf"):
                dispatch_time = TimePoint(
                    max(available.seconds, watermark), available.clock_id
                )
            self._expire_predictions_before(dispatch_time)
            self._last_dispatched_seconds = dispatch_time.seconds
            self._current_time = dispatch_time
            if kind == "packet":
                queued_packet = heapq.heappop(self._queue)
                self._process_packet(
                    queued_packet.packet, queue_depth=len(self._queue)
                )
            elif kind == "outcome":
                queued_outcome = heapq.heappop(self._outcome_queue)
                self._process_outcome(queued_outcome.outcome)
            else:
                queued_trigger = heapq.heappop(self._trigger_queue)
                self._process_trigger(queued_trigger.trigger)
            self._semantic_items += 1
            dispatched = True
            if (
                checkpoint_target is not None
                and self._semantic_items >= checkpoint_target
            ):
                break
        return dispatched

    def _register_prediction(self, prediction: Prediction) -> None:
        self._expire_predictions_before(prediction.available_time)
        pending = _PendingPrediction(
            prediction=prediction,
            registered_time=prediction.available_time,
            expires_time=TimePoint(
                prediction.available_time.seconds
                + self.outcome_policy.prediction_ttl_seconds,
                prediction.available_time.clock_id,
            ),
        )
        if len(self._pending_predictions) >= self.outcome_policy.max_pending_predictions:
            if self.outcome_policy.overflow == PendingPredictionOverflow.REJECT_NEWEST:
                self._record_prediction_disposition(
                    pending,
                    status=WorkStatus.REJECTED,
                    reason_code="pending_prediction_overflow",
                    record_type="prediction_overflowed",
                )
                return
            oldest = min(
                self._pending_predictions.values(),
                key=lambda value: (
                    value.registered_time.seconds,
                    value.prediction.prediction_id,
                ),
            )
            self._pending_predictions.pop(oldest.prediction.prediction_id)
            self._record_prediction_disposition(
                oldest,
                status=WorkStatus.REJECTED,
                reason_code="pending_prediction_overflow",
                record_type="prediction_overflowed",
            )
        self._pending_predictions[prediction.prediction_id] = pending
        self._emit(
            "prediction_pending",
            {
                "prediction_id": prediction.prediction_id,
                "registered_time": pending.registered_time.to_payload(),
                "expires_time": pending.expires_time.to_payload(),
                "pending_count": len(self._pending_predictions),
            },
            emitted_time=prediction.available_time,
        )

    def _expire_predictions_before(self, current_time: TimePoint) -> None:
        expired = sorted(
            (
                value
                for value in self._pending_predictions.values()
                if value.expires_time.seconds < current_time.seconds
            ),
            key=lambda value: (
                value.expires_time.seconds,
                value.prediction.prediction_id,
            ),
        )
        for pending in expired:
            self._pending_predictions.pop(pending.prediction.prediction_id, None)
            self._record_prediction_disposition(
                pending,
                status=WorkStatus.REJECTED,
                reason_code="outcome_expired",
                record_type="prediction_expired",
                completed_time=current_time,
            )

    def _record_prediction_disposition(
        self,
        pending: _PendingPrediction,
        *,
        status: WorkStatus,
        reason_code: str,
        record_type: str,
        completed_time: TimePoint | None = None,
        outcome_id: str | None = None,
    ) -> None:
        completed = completed_time or self._current_time
        payload = {
            "prediction_id": pending.prediction.prediction_id,
            "registered_time": pending.registered_time.to_payload(),
            "expires_time": pending.expires_time.to_payload(),
            "reason_code": reason_code,
            "outcome_id": outcome_id,
            "pending_count": len(self._pending_predictions),
        }
        self._emit(record_type, payload, emitted_time=completed)
        self._record_work(
            component_id="engine.outcomes",
            stage="pending_prediction",
            status=status,
            started=pending.registered_time,
            completed=completed,
            input_ids=(pending.prediction.prediction_id,),
            role=pending.prediction.role,
            reason_code=reason_code,
            details={
                "outcome_id": outcome_id,
                "expires_time": pending.expires_time.to_payload(),
            },
        )

    def _process_outcome(self, outcome: Outcome) -> None:
        available = outcome.available_time
        self._emit(
            "outcome_received",
            {"outcome": outcome.to_payload()},
            emitted_time=available,
        )
        if outcome.outcome_id in self._processed_outcome_ids:
            self._emit(
                "outcome_duplicate",
                {"outcome_id": outcome.outcome_id, "reason_code": "duplicate_outcome"},
                emitted_time=available,
            )
            self._record_work(
                component_id="engine.outcomes",
                stage="outcome",
                status=WorkStatus.REJECTED,
                started=available,
                completed=available,
                input_ids=(outcome.outcome_id,),
                reason_code="duplicate_outcome",
            )
            return
        self._processed_outcome_ids.add(outcome.outcome_id)
        if not outcome.prediction_ids:
            self._emit(
                "outcome_rejected",
                {
                    "outcome_id": outcome.outcome_id,
                    "reason_code": "missing_prediction_identity",
                },
                emitted_time=available,
            )
            self._record_work(
                component_id="engine.outcomes",
                stage="outcome",
                status=WorkStatus.REJECTED,
                started=available,
                completed=available,
                input_ids=(outcome.outcome_id,),
                reason_code="missing_prediction_identity",
            )
            return
        matched: list[_PendingPrediction] = []
        unmatched: list[str] = []
        for prediction_id in outcome.prediction_ids:
            pending = self._pending_predictions.pop(prediction_id, None)
            if pending is None:
                unmatched.append(prediction_id)
            else:
                matched.append(pending)
        if matched:
            matched_ids = tuple(value.prediction.prediction_id for value in matched)
            self._emit(
                "outcome_matched",
                {
                    "outcome_id": outcome.outcome_id,
                    "prediction_ids": list(matched_ids),
                    "unmatched_prediction_ids": unmatched,
                    "available_time": available.to_payload(),
                },
                emitted_time=available,
            )
            for pending in matched:
                self._record_prediction_disposition(
                    pending,
                    status=WorkStatus.COMPLETED,
                    reason_code="outcome_matched",
                    record_type="prediction_matched",
                    completed_time=available,
                    outcome_id=outcome.outcome_id,
                )
            for use in sorted(outcome.permitted_uses, key=lambda value: value.value):
                allowed = use in self.outcome_policy.allowed_uses
                self._emit(
                    "outcome_use",
                    {
                        "outcome_id": outcome.outcome_id,
                        "prediction_ids": list(matched_ids),
                        "use": use.value,
                        "eligible": allowed,
                        "applied": False,
                        "reason_code": "eligible"
                        if allowed
                        else "disallowed_outcome_use",
                    },
                    emitted_time=available,
                )
                if not allowed:
                    self._record_work(
                        component_id="engine.outcomes",
                        stage="outcome_use",
                        status=WorkStatus.SKIPPED,
                        started=available,
                        completed=available,
                        input_ids=(outcome.outcome_id, *matched_ids),
                        reason_code="disallowed_outcome_use",
                        details={"use": use.value},
                    )
            adaptation_eligible = bool(
                OutcomeUse.ADAPTATION in outcome.permitted_uses
                and OutcomeUse.ADAPTATION in self.outcome_policy.allowed_uses
            )
            self._emit(
                "adaptation_eligibility",
                {
                    "outcome_id": outcome.outcome_id,
                    "prediction_ids": list(matched_ids),
                    "eligible": adaptation_eligible,
                    "adaptation_applied": False,
                    "reason_code": "observe_only"
                    if adaptation_eligible
                    else "not_permitted",
                },
                emitted_time=available,
            )
        if unmatched:
            self._emit(
                "outcome_unmatched",
                {
                    "outcome_id": outcome.outcome_id,
                    "prediction_ids": unmatched,
                    "reason_code": "prediction_not_pending",
                },
                emitted_time=available,
            )
        self._record_work(
            component_id="engine.outcomes",
            stage="outcome",
            status=WorkStatus.COMPLETED if matched else WorkStatus.REJECTED,
            started=available,
            completed=available,
            input_ids=(outcome.outcome_id, *outcome.prediction_ids),
            reason_code=None
            if matched and not unmatched
            else "partial_outcome_match"
            if matched
            else "unmatched_outcome",
            details={
                "matched_prediction_ids": [
                    value.prediction.prediction_id for value in matched
                ],
                "unmatched_prediction_ids": unmatched,
            },
        )

    def _process_trigger(self, trigger: ScheduledTrigger) -> None:
        binding = next(
            value.component
            for value in self.components.trigger_handlers
            if value.component.component_id == trigger.target_component_id
        )
        if trigger.trigger_id in self._cancelled_trigger_ids:
            self._emit(
                "trigger_cancelled",
                {"trigger": trigger.to_payload(), "reason_code": "cancelled"},
                emitted_time=self._current_time,
            )
            self._record_work(
                component=binding,
                stage="trigger",
                status=WorkStatus.CANCELLED,
                started=trigger.scheduled_time,
                completed=self._current_time,
                input_ids=(trigger.trigger_id,),
                reason_code="cancelled",
            )
            return
        if (
            trigger.deadline_time is not None
            and self._current_time.seconds > trigger.deadline_time.seconds
        ):
            self._emit(
                "trigger_timed_out",
                {"trigger": trigger.to_payload(), "reason_code": "deadline_exceeded"},
            )
            self._record_work(
                component=binding,
                stage="trigger",
                status=WorkStatus.TIMED_OUT,
                started=trigger.scheduled_time,
                completed=self._current_time,
                deadline=trigger.deadline_time,
                input_ids=(trigger.trigger_id,),
                reason_code="deadline_exceeded",
            )
            return
        context = self._context(binding, self._current_time)
        try:
            result = binding.component.handle_trigger(trigger, context)
            if not isinstance(result, TriggerResult):
                raise TypeError("trigger handler must return TriggerResult")
        except Exception as exc:
            self._emit(
                "trigger_failed",
                {
                    "trigger": trigger.to_payload(),
                    "error": f"{type(exc).__name__}: {exc}",
                },
                emitted_time=self._current_time,
            )
            self._record_work(
                component=binding,
                stage="trigger",
                status=WorkStatus.FAILED,
                started=trigger.scheduled_time,
                completed=self._current_time,
                deadline=trigger.deadline_time,
                input_ids=(trigger.trigger_id,),
                reason_code="component_error",
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
            if self.scheduling.fail_fast:
                raise
            return
        status = (
            WorkStatus.CANCELLED
            if result.disposition == TriggerDisposition.CANCELLED
            else WorkStatus.COMPLETED
        )
        self._emit(
            "trigger_fired",
            {
                "trigger": trigger.to_payload(),
                "disposition": result.disposition.value,
                "details": thaw_json(result.details),
            },
            emitted_time=self._current_time,
        )
        self._record_work(
            component=binding,
            stage="trigger",
            status=status,
            started=trigger.scheduled_time,
            completed=self._current_time,
            deadline=trigger.deadline_time,
            input_ids=(trigger.trigger_id,),
            reason_code="handler_cancelled"
            if result.disposition == TriggerDisposition.CANCELLED
            else None,
        )
        if result.transition is not None:
            self._emit_state_transition(result.transition)
        if result.next_trigger is not None:
            if result.next_trigger.parent_trigger_id != trigger.trigger_id:
                raise ValueError("rescheduled trigger must identify its parent trigger")
            self._enqueue_trigger(result.next_trigger)
            self._emit(
                "trigger_rescheduled",
                {
                    "trigger_id": trigger.trigger_id,
                    "next_trigger": result.next_trigger.to_payload(),
                },
                emitted_time=self._current_time,
            )

    def _emit_state_transition(self, transition: StateTransition) -> None:
        self._emit(
            "state_transition",
            {"transition": transition.to_payload()},
            emitted_time=transition.transition_time,
        )
        for rule in self.state_triggers:
            if rule.once and rule.rule_id in self._fired_state_trigger_rules:
                continue
            if not rule.matches(transition):
                continue
            scheduled_time = TimePoint(
                transition.transition_time.seconds + rule.delay_seconds,
                transition.transition_time.clock_id,
            )
            payload = dict(thaw_json(rule.payload))
            payload["state_transition_id"] = transition.transition_id
            trigger = ScheduledTrigger(
                trigger_id=self._ids.next("trigger"),
                target_component_id=rule.target_component_id,
                scheduled_time=scheduled_time,
                payload=payload,
            )
            self._enqueue_trigger(trigger)
            if rule.once:
                self._fired_state_trigger_rules.add(rule.rule_id)
            self._emit(
                "state_trigger_scheduled",
                {
                    "rule_id": rule.rule_id,
                    "transition_id": transition.transition_id,
                    "trigger": trigger.to_payload(),
                },
                emitted_time=transition.transition_time,
            )

    def _process_packet(self, packet: Packet, *, queue_depth: int) -> None:
        transform_context = self._context(self.components.transform, packet_available_time(packet))
        try:
            transformed = self.components.transform.component.update(packet, transform_context)
        except Exception as exc:
            self._record_work(
                component=self.components.transform,
                stage="transform",
                status=WorkStatus.FAILED,
                started=transform_context.current_time,
                completed=transform_context.current_time,
                input_ids=(_packet_id(packet),),
                reason_code="component_error",
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        if transformed is None:
            self._record_work(
                component=self.components.transform,
                stage="transform",
                status=WorkStatus.SKIPPED,
                started=transform_context.current_time,
                completed=transform_context.current_time,
                input_ids=(_packet_id(packet),),
                reason_code="no_output",
            )
            return
        if not isinstance(transformed, (DenseSampleBatch, SparseEventBatch, MetadataEvent)):
            raise TypeError("transform returned an unsupported packet type")
        self._emit(
            "packet_produced",
            {
                "component_id": self.components.transform.component_id,
                "packet": _packet_evidence(transformed),
            },
            emitted_time=packet_available_time(transformed),
        )
        window_context = self._context(
            self.components.window, packet_available_time(transformed)
        )
        try:
            windows = tuple(self.components.window.component.update(transformed, window_context))
        except Exception as exc:
            self._record_work(
                component=self.components.window,
                stage="window",
                status=WorkStatus.FAILED,
                started=window_context.current_time,
                completed=window_context.current_time,
                input_ids=(_packet_id(transformed),),
                reason_code="component_error",
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        if not windows:
            self._record_work(
                component=self.components.window,
                stage="window",
                status=WorkStatus.PENDING,
                started=window_context.current_time,
                completed=None,
                input_ids=(_packet_id(transformed),),
                reason_code="awaiting_samples",
            )
            return
        for window in windows:
            if not isinstance(window, DenseWindow):
                raise TypeError("Phase 3 reference engine requires DenseWindow output")
            self._emit(
                "window_produced",
                {
                    "component_id": self.components.window.component_id,
                    "window": _window_evidence(window),
                },
                emitted_time=window.available_time,
            )
            self._process_window(window, queue_depth=queue_depth)

    def _process_window(self, window: DenseWindow, *, queue_depth: int) -> None:
        quality_context = self._context(self.components.quality, window.available_time)
        try:
            decision = self.components.quality.component.evaluate(window, quality_context)
        except Exception as exc:
            self._record_work(
                component=self.components.quality,
                stage="quality",
                status=WorkStatus.FAILED,
                started=quality_context.current_time,
                completed=quality_context.current_time,
                input_ids=window.input_ids,
                reason_code="component_error",
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        self._emit(
            "quality_decision",
            {"decision": decision.to_payload()},
            emitted_time=decision.decided_time,
        )
        if decision.status == QualityStatus.REJECTED:
            self._record_work(
                component=self.components.quality,
                stage="quality",
                status=WorkStatus.REJECTED,
                started=quality_context.current_time,
                completed=decision.decided_time,
                input_ids=window.input_ids,
                reason_code="quality_rejected",
                details={"quality_decision_id": decision.decision_id},
            )
            return
        component_time = window.available_time
        primary_prediction: Prediction | None = None
        primary_role: ModelRole | None = None
        for model in self.components.scheduled_models:
            if (
                model.role.kind in {ModelRoleKind.SHADOW, ModelRoleKind.CANDIDATE}
                and self.scheduling.shadow_queue_limit is not None
                and queue_depth >= self.scheduling.shadow_queue_limit
            ):
                self._record_work(
                    component=model.component,
                    stage="model",
                    status=WorkStatus.SKIPPED,
                    started=component_time,
                    completed=component_time,
                    input_ids=window.input_ids,
                    role=model.role.role_id,
                    reason_code="primary_first_backpressure",
                )
                continue
            deadline = TimePoint(
                window.available_time.seconds + model.deadline_seconds,
                window.available_time.clock_id,
            )
            context = self._context(model.component, component_time)
            try:
                prediction = model.component.component.predict(window, context)
                if prediction.input_ids != window.input_ids:
                    raise ValueError("model prediction did not preserve admitted window inputs")
                if prediction.role != model.role.role_id:
                    raise ValueError("model prediction role differs from its binding")
                self._predictions.append(prediction)
                self._emit(
                    "prediction",
                    {
                        "role_kind": model.role.kind.value,
                        "prediction": prediction.to_payload(),
                    },
                    emitted_time=prediction.available_time,
                )
                self._register_prediction(prediction)
                timed_out = prediction.available_time.seconds > deadline.seconds
                self._record_work(
                    component=model.component,
                    stage="model",
                    status=WorkStatus.TIMED_OUT if timed_out else WorkStatus.PREDICTED,
                    started=context.current_time,
                    completed=prediction.available_time,
                    deadline=deadline,
                    input_ids=window.input_ids,
                    role=model.role.role_id,
                    reason_code="deadline_exceeded" if timed_out else None,
                    details={"prediction_id": prediction.prediction_id},
                )
                component_time = prediction.available_time
                if model.role.kind == ModelRoleKind.PRIMARY and not timed_out:
                    primary_prediction = prediction
                    primary_role = model.role
            except Exception as exc:
                self._record_work(
                    component=model.component,
                    stage="model",
                    status=WorkStatus.FAILED,
                    started=context.current_time,
                    completed=context.current_time,
                    deadline=deadline,
                    input_ids=window.input_ids,
                    role=model.role.role_id,
                    reason_code="component_error",
                    details={"error": f"{type(exc).__name__}: {exc}"},
                )
                if self.scheduling.fail_fast or model.role.kind == ModelRoleKind.PRIMARY:
                    raise
        if primary_prediction is None or primary_role is None:
            return
        policy_context = self._context(self.components.policy, primary_prediction.available_time)
        try:
            command = self.components.policy.component.decide(
                primary_prediction, {}, policy_context
            )
        except Exception as exc:
            self._record_work(
                component=self.components.policy,
                stage="policy",
                status=WorkStatus.FAILED,
                started=policy_context.current_time,
                completed=policy_context.current_time,
                input_ids=(primary_prediction.prediction_id,),
                role=primary_role.role_id,
                reason_code="component_error",
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise
        if command is None:
            self._record_work(
                component=self.components.policy,
                stage="policy",
                status=WorkStatus.SKIPPED,
                started=policy_context.current_time,
                completed=policy_context.current_time,
                input_ids=(primary_prediction.prediction_id,),
                role=primary_role.role_id,
                reason_code="observe_only",
            )
            return
        if not primary_role.may_request_actions:
            raise EngineExecutionError("model role is not permitted to request actions")
        self._emit(
            "action_command",
            {"command": command.to_payload()},
            emitted_time=command.available_time,
        )
        self._record_work(
            component=self.components.policy,
            stage="policy",
            status=WorkStatus.PREDICTED,
            started=policy_context.current_time,
            completed=command.available_time,
            input_ids=(primary_prediction.prediction_id,),
            role=primary_role.role_id,
            details={"command_id": command.command_id},
        )
        if self.components.actuator is None:
            return
        actuator = self.components.actuator
        actuator_context = self._context(actuator, command.available_time)
        try:
            receipt = actuator.component.submit(command, actuator_context)
            if not isinstance(receipt, ActionReceipt):
                raise TypeError("actuator must return ActionReceipt")
            if receipt.command_id != command.command_id:
                raise ValueError("actuator receipt command_id differs from command")
            if receipt.observed_time.clock_id != self.scheduling.execution_clock_id:
                raise ValueError("actuator receipt must use the execution clock")
            if receipt.observed_time.seconds < command.available_time.seconds:
                raise ValueError("actuator receipt cannot precede command availability")
            self._emit(
                "action_receipt",
                {"receipt": receipt.to_payload()},
                emitted_time=receipt.observed_time,
            )
            self._record_work(
                component=actuator,
                stage="actuator",
                status=WorkStatus.COMPLETED,
                started=command.available_time,
                completed=receipt.observed_time,
                input_ids=(command.command_id,),
                reason_code=receipt.status.value,
                details={"receipt_id": receipt.receipt_id},
            )
        except Exception as exc:
            self._record_work(
                component=actuator,
                stage="actuator",
                status=WorkStatus.FAILED,
                started=command.available_time,
                completed=command.available_time,
                input_ids=(command.command_id,),
                reason_code="component_error",
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
            raise

    def _context(
        self, binding: ComponentBinding, current_time: TimePoint
    ) -> RuntimeExecutionContext:
        return RuntimeExecutionContext(
            execution_id=self.execution_id,
            component_id=binding.component_id,
            component_version=binding.component_version,
            execution_mode=self.plan.execution_mode,
            current_time=current_time,
            clock_mapping_revisions=self.clock_mapping_revisions,
            _ids=self._ids,
        )

    def _record_work(
        self,
        *,
        component: ComponentBinding | None = None,
        component_id: str | None = None,
        stage: str,
        status: WorkStatus,
        started: TimePoint,
        completed: TimePoint | None,
        input_ids: tuple[str, ...],
        deadline: TimePoint | None = None,
        role: str | None = None,
        reason_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> WorkRecord:
        if (component is None) == (component_id is None):
            raise ValueError("work requires exactly one component binding or component_id")
        resolved_component_id = (
            component.component_id if component is not None else str(component_id)
        )
        record = WorkRecord(
            work_id=self._ids.next("work"),
            component_id=resolved_component_id,
            stage=stage,
            status=status,
            started_time=started,
            completed_time=completed,
            deadline_time=deadline,
            input_ids=input_ids,
            role=role,
            reason_code=reason_code,
            details=details or {},
        )
        self._work.append(record)
        self._emit("work", {"work": record.to_payload()}, emitted_time=started)
        return record

    def _reject_admission(
        self,
        source_id: str,
        packet_id: str,
        time: TimePoint,
        reason_code: str,
    ) -> None:
        source = next(value for value in self.sources if value.component.component_id == source_id)
        self._record_work(
            component=source.component,
            stage="admission",
            status=WorkStatus.REJECTED,
            started=time,
            completed=time,
            input_ids=(packet_id,),
            reason_code=reason_code,
        )
        self._emit(
            "input_rejected",
            {"source_id": source_id, "packet_id": packet_id, "reason_code": reason_code},
            emitted_time=time,
        )

    def _validate_packet_source(
        self,
        source_binding: SourceBinding,
        packet: Packet,
        available: TimePoint,
    ) -> None:
        spec = source_binding.component.component.stream_spec
        if packet.stream_id != spec.stream_id or packet.stream_revision != spec.revision:
            raise ValueError("source emitted a packet outside its declared stream revision")
        if available.clock_id != self.scheduling.execution_clock_id:
            raise ValueError("packet availability must use the execution clock")

    def _global_watermark(self) -> float:
        active = []
        for source_binding in self.sources:
            source = source_binding.component.component
            if bool(getattr(source, "exhausted", False)):
                continue
            watermark = getattr(source, "watermark", None)
            if watermark is None:
                return float("-inf")
            if watermark.clock_id != self.scheduling.execution_clock_id:
                raise ValueError("source watermark must use the execution clock")
            active.append(float(watermark.seconds))
        return min(active) if active else float("inf")

    def _all_sources_exhausted(self) -> bool:
        return all(bool(getattr(value.component.component, "exhausted", False)) for value in self.sources)

    def _mark_pending(self) -> None:
        for queued in sorted(self._queue):
            available = packet_available_time(queued.packet)
            source = next(
                value for value in self.sources if value.component.component_id == queued.source_id
            )
            self._record_work(
                component=source.component,
                stage="admission",
                status=WorkStatus.PENDING,
                started=available,
                completed=None,
                input_ids=(_packet_id(queued.packet),),
                reason_code="awaiting_watermark",
            )
        for queued in sorted(self._outcome_queue):
            self._record_work(
                component_id="engine.outcomes",
                stage="outcome",
                status=WorkStatus.PENDING,
                started=queued.outcome.available_time,
                completed=None,
                input_ids=(queued.outcome.outcome_id,),
                reason_code="awaiting_watermark",
            )
        for queued in sorted(self._trigger_queue):
            self._record_work(
                component_id=queued.trigger.target_component_id,
                stage="trigger",
                status=WorkStatus.PENDING,
                started=queued.trigger.scheduled_time,
                completed=None,
                input_ids=(queued.trigger.trigger_id,),
                deadline=queued.trigger.deadline_time,
                reason_code="awaiting_watermark",
            )

    def _cancel_pending(self, reason_code: str = "cancelled") -> None:
        while self._queue:
            queued = heapq.heappop(self._queue)
            available = packet_available_time(queued.packet)
            source = next(
                value for value in self.sources if value.component.component_id == queued.source_id
            )
            self._record_work(
                component=source.component,
                stage="admission",
                status=WorkStatus.CANCELLED,
                started=available,
                completed=TimePoint(
                    max(self._current_time.seconds, available.seconds),
                    available.clock_id,
                ),
                input_ids=(_packet_id(queued.packet),),
                reason_code=reason_code,
            )
        while self._outcome_queue:
            queued_outcome = heapq.heappop(self._outcome_queue)
            available = queued_outcome.outcome.available_time
            self._record_work(
                component_id="engine.outcomes",
                stage="outcome",
                status=WorkStatus.CANCELLED,
                started=available,
                completed=TimePoint(
                    max(self._current_time.seconds, available.seconds),
                    available.clock_id,
                ),
                input_ids=(queued_outcome.outcome.outcome_id,),
                reason_code=reason_code,
            )
        while self._trigger_queue:
            queued_trigger = heapq.heappop(self._trigger_queue)
            scheduled = queued_trigger.trigger.scheduled_time
            self._record_work(
                component_id=queued_trigger.trigger.target_component_id,
                stage="trigger",
                status=WorkStatus.CANCELLED,
                started=scheduled,
                completed=TimePoint(
                    max(self._current_time.seconds, scheduled.seconds),
                    scheduled.clock_id,
                ),
                input_ids=(queued_trigger.trigger.trigger_id,),
                deadline=queued_trigger.trigger.deadline_time,
                reason_code=reason_code,
            )

    def _finalize_pending_predictions(self, status: EngineStatus) -> None:
        if self._pending_predictions_finalized:
            return
        self._pending_predictions_finalized = True
        terminal = status in {EngineStatus.FAILED, EngineStatus.CANCELLED}
        for pending in sorted(
            self._pending_predictions.values(),
            key=lambda value: (
                value.registered_time.seconds,
                value.prediction.prediction_id,
            ),
        ):
            if terminal:
                self._record_prediction_disposition(
                    pending,
                    status=WorkStatus.CANCELLED,
                    reason_code="run_failed"
                    if status == EngineStatus.FAILED
                    else "cancelled",
                    record_type="prediction_cancelled",
                    completed_time=self._current_time,
                )
                continue
            self._emit(
                "prediction_pending_at_end",
                {
                    "prediction_id": pending.prediction.prediction_id,
                    "registered_time": pending.registered_time.to_payload(),
                    "expires_time": pending.expires_time.to_payload(),
                    "reason_code": "awaiting_outcome",
                },
            )
            self._record_work(
                component_id="engine.outcomes",
                stage="pending_prediction",
                status=WorkStatus.PENDING,
                started=pending.registered_time,
                completed=None,
                input_ids=(pending.prediction.prediction_id,),
                role=pending.prediction.role,
                reason_code="awaiting_outcome",
                details={"expires_time": pending.expires_time.to_payload()},
            )
        if terminal:
            self._pending_predictions.clear()

    def _create_checkpoint(self) -> EngineCheckpoint:
        component_states: list[dict[str, Any]] = []
        for binding in self._all_bindings():
            snapshot = getattr(binding.component, "snapshot_state", None)
            restore = getattr(binding.component, "restore_state", None)
            if callable(snapshot) != callable(restore):
                raise EngineCheckpointError(
                    f"component {binding.component_id} does not support symmetric "
                    "snapshot/restore"
                )
            if not callable(snapshot):
                continue
            state = snapshot()
            if not isinstance(state, Mapping):
                raise EngineCheckpointError(
                    f"component {binding.component_id} snapshot must be a mapping"
                )
            state_payload = thaw_json(state)
            component_states.append(
                {
                    "component_id": binding.component_id,
                    "component_version": binding.component_version,
                    "state_hash": canonical_hash(state_payload),
                    "state": state_payload,
                }
            )
        prefix = tuple(self._evidence_records)
        prefix_digest = canonical_hash([value.to_payload() for value in prefix])
        checkpoint_id = self._ids.next("checkpoint")
        state = {
            "bindings": self._binding_signatures(),
            "run_status": EngineStatus.PARTIAL.value,
            "scheduling": self._scheduling_payload(),
            "clock_mapping_revisions": dict(sorted(self.clock_mapping_revisions.items())),
            "outcome_policy": self._outcome_policy_payload(),
            "state_triggers": self._state_trigger_payloads(),
            "current_time": self._current_time.to_payload(),
            "last_dispatched_seconds": self._last_dispatched_seconds,
            "semantic_items": self._semantic_items,
            "cancel_requested": self._cancel_requested,
            "id_state": self._ids.snapshot_state(),
            "evidence_sequence": self._evidence_sequence,
            "evidence_prefix": [value.to_payload() for value in prefix],
            "captured_packets": [value.to_payload() for value in self._captured],
            "work": [value.to_payload() for value in self._work],
            "predictions": [value.to_payload() for value in self._predictions],
            "packet_queue": [
                {
                    "sort_key": list(value.sort_key),
                    "source_id": value.source_id,
                    "packet": value.packet.to_payload(),
                }
                for value in sorted(self._queue)
            ],
            "outcome_queue": [
                {
                    "sort_key": list(value.sort_key),
                    "outcome": value.outcome.to_payload(),
                }
                for value in sorted(self._outcome_queue)
            ],
            "trigger_queue": [
                {
                    "sort_key": list(value.sort_key),
                    "trigger": value.trigger.to_payload(),
                }
                for value in sorted(self._trigger_queue)
            ],
            "pending_predictions": [
                {
                    "prediction": value.prediction.to_payload(),
                    "registered_time": value.registered_time.to_payload(),
                    "expires_time": value.expires_time.to_payload(),
                }
                for value in sorted(
                    self._pending_predictions.values(),
                    key=lambda pending: pending.prediction.prediction_id,
                )
            ],
            "invalid_outcomes": [list(value) for value in self._invalid_outcomes],
            "processed_outcome_ids": sorted(self._processed_outcome_ids),
            "cancelled_trigger_ids": sorted(self._cancelled_trigger_ids),
            "known_trigger_ids": sorted(self._known_trigger_ids),
            "fired_state_trigger_rules": sorted(self._fired_state_trigger_rules),
            "component_states": component_states,
        }
        return EngineCheckpoint(
            checkpoint_id=checkpoint_id,
            execution_id=self.execution_id,
            plan_hash=self.plan.plan_hash,
            created_time=self._current_time,
            evidence_prefix_digest=prefix_digest,
            state=state,
        )

    def _start_components(self) -> None:
        for binding in self._all_bindings():
            start = getattr(binding.component, "start", None)
            if callable(start):
                try:
                    start(self._context(binding, self._current_time))
                except Exception as exc:
                    self._record_work(
                        component=binding,
                        stage="lifecycle",
                        status=WorkStatus.FAILED,
                        started=self._current_time,
                        completed=self._current_time,
                        input_ids=(),
                        reason_code="start_failed",
                        details={"error": f"{type(exc).__name__}: {exc}"},
                    )
                    raise
            self._emit(
                "component_started",
                {
                    "component_id": binding.component_id,
                    "component_version": binding.component_version,
                    "placement": binding.boundary.placement.value,
                    "endpoint_id": binding.boundary.endpoint_id,
                },
            )

    def _stop_components(self) -> list[str]:
        failures: list[str] = []
        source_ids = {source.component.component_id for source in self.sources}
        for binding in reversed(self._all_bindings()):
            if binding.component_id not in source_ids:
                stop = getattr(binding.component, "stop", None)
                if callable(stop):
                    try:
                        stop(self._context(binding, self._current_time))
                    except Exception as exc:  # pragma: no cover - defensive proxy boundary
                        failures.append(
                            f"component {binding.component_id} stop failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
            self._emit(
                "component_stopped",
                {
                    "component_id": binding.component_id,
                    "component_version": binding.component_version,
                    "placement": binding.boundary.placement.value,
                    "endpoint_id": binding.boundary.endpoint_id,
                },
            )
        return failures

    def _emit_state_snapshots(self) -> list[str]:
        failures: list[str] = []
        for binding in self._all_bindings():
            snapshot = getattr(binding.component, "snapshot_state", None)
            if not callable(snapshot):
                continue
            try:
                state = snapshot()
                if not isinstance(state, Mapping):
                    raise TypeError("component snapshot_state must return a mapping")
                self._emit(
                    "component_state",
                    {"component_id": binding.component_id, "state": thaw_json(state)},
                )
            except Exception as exc:  # pragma: no cover - defensive proxy boundary
                failures.append(
                    f"component {binding.component_id} snapshot failed: "
                    f"{type(exc).__name__}: {exc}"
                )
        return failures

    def _all_bindings(self) -> tuple[ComponentBinding, ...]:
        return (
            *(source.component for source in self.sources),
            self.components.transform,
            self.components.window,
            self.components.quality,
            *(model.component for model in self.components.scheduled_models),
            self.components.policy,
            *(handler.component for handler in self.components.trigger_handlers),
            *(() if self.components.actuator is None else (self.components.actuator,)),
        )

    def _emit(
        self,
        record_type: str,
        payload: Mapping[str, Any],
        *,
        emitted_time: TimePoint | None = None,
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            record_id=self._ids.next("evidence"),
            record_type=record_type,
            sequence=self._evidence_sequence,
            emitted_time=emitted_time or self._current_time,
            payload=payload,
        )
        self._evidence_sequence += 1
        self._evidence_records.append(record)
        self.evidence_sink.append(record)
        return record


def _packet_id(packet: Packet) -> str:
    if isinstance(packet, (DenseSampleBatch, SparseEventBatch)):
        return packet.batch_id
    if isinstance(packet, MetadataEvent):
        return packet.event_id
    raise TypeError(f"unsupported packet type: {type(packet).__name__}")


def _packet_from_payload(payload: Mapping[str, Any]) -> Packet:
    schema = payload.get("schema")
    if schema == "eegle.dense_sample_batch.v1":
        return DenseSampleBatch.from_payload(payload)
    if schema == "eegle.sparse_event_batch.v1":
        return SparseEventBatch.from_payload(payload)
    if schema == "eegle.metadata_event.v1":
        return MetadataEvent.from_payload(payload)
    raise ValueError(f"unsupported checkpoint packet schema: {schema}")


def _packet_sequence(packet: Packet) -> int:
    if isinstance(packet, (DenseSampleBatch, SparseEventBatch)):
        return packet.sequence_start
    if isinstance(packet, MetadataEvent):
        return packet.sequence
    raise TypeError(f"unsupported packet type: {type(packet).__name__}")


def _weakest_equivalence(bindings: tuple[ComponentBinding, ...]) -> EquivalenceLevel:
    rank = {
        EquivalenceLevel.BITWISE: 0,
        EquivalenceLevel.NUMERIC: 1,
        EquivalenceLevel.SEMANTIC: 2,
        EquivalenceLevel.TRACE: 3,
        EquivalenceLevel.NON_REPLAYABLE: 4,
    }
    return max((binding.equivalence for binding in bindings), key=rank.__getitem__)


def _packet_evidence(packet: Packet) -> dict[str, Any]:
    payload = packet.to_payload()
    return {
        "schema": payload["schema"],
        "packet_id": _packet_id(packet),
        "stream_id": packet.stream_id,
        "stream_revision": packet.stream_revision,
        "sequence_start": _packet_sequence(packet),
        "sequence_end": getattr(packet, "sequence_end", _packet_sequence(packet)),
        "available_time": packet_available_time(packet).to_payload(),
        "lineage": payload.get("lineage"),
        "content_hash": canonical_hash(payload),
    }


def _window_evidence(window: DenseWindow) -> dict[str, Any]:
    payload = window.to_payload()
    return {
        "schema": payload["schema"],
        "window_id": window.window_id,
        "stream_id": window.stream_id,
        "stream_revision": window.stream_revision,
        "sequence_start": window.sequence_start,
        "sequence_end": window.sequence_end,
        "shape": list(window.values.shape),
        "dtype": window.values.dtype.name,
        "available_time": window.available_time.to_payload(),
        "input_ids": list(window.input_ids),
        "lineage": window.lineage.to_payload(),
        "content_hash": canonical_hash(payload),
    }
