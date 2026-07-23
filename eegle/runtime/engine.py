"""The deterministic semantic execution engine shared by simulation and replay."""

from __future__ import annotations

from dataclasses import dataclass
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
from eegle.models.predictions import Prediction
from eegle.models.roles import ModelRole, ModelRoleKind
from eegle.processing.quality import QualityStatus
from eegle.processing.windows import DenseWindow
from eegle.recording.evidence import EvidenceRecord
from eegle.recording.sinks import InMemoryEvidenceSink
from eegle.runtime.context import DeterministicIdSource, RuntimeExecutionContext
from eegle.runtime.scheduling import (
    BackpressurePolicy,
    LatenessPolicy,
    ProcessBoundary,
    SchedulingPolicy,
)
from eegle.runtime.state import WorkRecord
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
class EngineComponents:
    transform: ComponentBinding
    window: ComponentBinding
    quality: ComponentBinding
    models: tuple[ModelBinding, ...]
    policy: ComponentBinding

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


@dataclass(order=True, slots=True)
class _QueuedPacket:
    sort_key: tuple[Any, ...]
    source_id: str
    packet: Packet


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
        self._captured: list[Packet] = []
        self._work: list[WorkRecord] = []
        self._predictions: list[Prediction] = []
        self._cancel_requested = False
        self._last_dispatched_seconds: float | None = None
        self._current_time = TimePoint(0.0, scheduling.execution_clock_id)
        self._has_run = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def run(self) -> EngineRunResult:
        if self._has_run:
            raise RuntimeError("ExecutionEngine instances are single-use")
        self._has_run = True
        status = EngineStatus.COMPLETE
        failure: str | None = None
        idle_cycles = 0
        self._emit(
            "run_started",
            {
                "execution_id": self.execution_id,
                "plan_hash": self.plan.plan_hash,
                "execution_mode": self.plan.execution_mode.value,
            },
        )
        try:
            self._start_components()
            while True:
                if self._cancel_requested:
                    status = EngineStatus.CANCELLED
                    self._cancel_pending()
                    break
                polled = self._poll_sources()
                dispatched = self._dispatch_eligible()
                if self._all_sources_exhausted() and not self._queue:
                    break
                if polled or dispatched:
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
        self._current_time = available
        self._emit(
            "input_admitted",
            {
                "source_id": queued.source_id,
                "packet": _packet_evidence(packet),
                "queue_depth": len(self._queue),
            },
            emitted_time=available,
        )

    def _dispatch_eligible(self) -> bool:
        dispatched = False
        while self._queue:
            watermark = self._global_watermark()
            if self._queue[0].sort_key[0] > watermark:
                break
            queued = heapq.heappop(self._queue)
            available = packet_available_time(queued.packet)
            self._last_dispatched_seconds = available.seconds
            self._current_time = available
            self._process_packet(queued.packet, queue_depth=len(self._queue))
            dispatched = True
        return dispatched

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
        component: ComponentBinding,
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
        record = WorkRecord(
            work_id=self._ids.next("work"),
            component_id=component.component_id,
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
