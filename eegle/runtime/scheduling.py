"""Deterministic scheduling, placement, lateness, and backpressure policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import freeze_json, require_finite, require_identifier, thaw_json
from eegle.runtime.state import StateTransition, TransitionStatus
from eegle.streams.clocks import TimePoint


class BackpressurePolicy(str, Enum):
    REJECT_NEWEST = "reject_newest"
    FAIL_RUN = "fail_run"


class LatenessPolicy(str, Enum):
    REJECT = "reject"
    FAIL_RUN = "fail_run"


class ComponentPlacement(str, Enum):
    IN_PROCESS = "in_process"
    SUBPROCESS_PROXY = "subprocess_proxy"
    EXTERNAL_PROXY = "external_proxy"


class TriggerDisposition(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"


@dataclass(frozen=True, slots=True)
class ScheduledTrigger:
    """One domain-neutral unit of virtual-time work."""

    trigger_id: str
    target_component_id: str
    scheduled_time: TimePoint
    payload: Mapping[str, Any] = None  # type: ignore[assignment]
    deadline_time: TimePoint | None = None
    parent_trigger_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger_id", require_identifier(self.trigger_id, "trigger_id"))
        object.__setattr__(
            self,
            "target_component_id",
            require_identifier(self.target_component_id, "target_component_id"),
        )
        if self.deadline_time is not None:
            if self.deadline_time.clock_id != self.scheduled_time.clock_id:
                raise ValueError("trigger deadline must use the scheduling clock")
            if self.deadline_time.seconds < self.scheduled_time.seconds:
                raise ValueError("trigger deadline cannot precede scheduled_time")
        if self.parent_trigger_id is not None:
            object.__setattr__(
                self,
                "parent_trigger_id",
                require_identifier(self.parent_trigger_id, "parent_trigger_id"),
            )
        object.__setattr__(self, "payload", freeze_json(self.payload or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "target_component_id": self.target_component_id,
            "scheduled_time": self.scheduled_time.to_payload(),
            "deadline_time": None
            if self.deadline_time is None
            else self.deadline_time.to_payload(),
            "parent_trigger_id": self.parent_trigger_id,
            "payload": thaw_json(self.payload),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ScheduledTrigger":
        deadline = payload.get("deadline_time")
        return cls(
            trigger_id=str(payload["trigger_id"]),
            target_component_id=str(payload["target_component_id"]),
            scheduled_time=TimePoint.from_payload(payload["scheduled_time"]),
            deadline_time=None if deadline is None else TimePoint.from_payload(deadline),
            parent_trigger_id=None
            if payload.get("parent_trigger_id") is None
            else str(payload["parent_trigger_id"]),
            payload=dict(payload.get("payload") or {}),
        )


@dataclass(frozen=True, slots=True)
class StateTriggerRule:
    """Schedule work when a matching state transition is emitted."""

    rule_id: str
    target_component_id: str
    transition_kind: str | None = None
    source_component_id: str | None = None
    statuses: frozenset[TransitionStatus] = frozenset({TransitionStatus.APPLIED})
    delay_seconds: float = 0.0
    payload: Mapping[str, Any] = None  # type: ignore[assignment]
    once: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", require_identifier(self.rule_id, "rule_id"))
        object.__setattr__(
            self,
            "target_component_id",
            require_identifier(self.target_component_id, "target_component_id"),
        )
        if self.transition_kind is not None:
            object.__setattr__(
                self,
                "transition_kind",
                require_identifier(self.transition_kind, "transition_kind"),
            )
        if self.source_component_id is not None:
            object.__setattr__(
                self,
                "source_component_id",
                require_identifier(self.source_component_id, "source_component_id"),
            )
        object.__setattr__(
            self,
            "statuses",
            frozenset(TransitionStatus(value) for value in self.statuses),
        )
        if not self.statuses:
            raise ValueError("state trigger rule requires at least one status")
        object.__setattr__(
            self,
            "delay_seconds",
            require_finite(self.delay_seconds, "delay_seconds"),
        )
        if self.delay_seconds < 0:
            raise ValueError("state trigger delay_seconds cannot be negative")
        object.__setattr__(self, "payload", freeze_json(self.payload or {}))

    def matches(self, transition: StateTransition) -> bool:
        return bool(
            transition.status in self.statuses
            and (
                self.transition_kind is None
                or transition.transition_kind == self.transition_kind
            )
            and (
                self.source_component_id is None
                or transition.component_id == self.source_component_id
            )
        )


@dataclass(frozen=True, slots=True)
class TriggerResult:
    disposition: TriggerDisposition = TriggerDisposition.COMPLETED
    transition: StateTransition | None = None
    next_trigger: ScheduledTrigger | None = None
    details: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "disposition", TriggerDisposition(self.disposition))
        if self.disposition == TriggerDisposition.RESCHEDULED and self.next_trigger is None:
            raise ValueError("rescheduled trigger result requires next_trigger")
        if self.disposition != TriggerDisposition.RESCHEDULED and self.next_trigger is not None:
            raise ValueError("only a rescheduled trigger result may include next_trigger")
        object.__setattr__(self, "details", freeze_json(self.details or {}))


@dataclass(frozen=True, slots=True)
class SchedulingPolicy:
    execution_clock_id: str
    max_pending_packets: int = 64
    allowed_lateness_seconds: float = 0.0
    backpressure: BackpressurePolicy = BackpressurePolicy.REJECT_NEWEST
    lateness: LatenessPolicy = LatenessPolicy.REJECT
    max_idle_cycles: int = 1
    shadow_queue_limit: int | None = None
    fail_fast: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "execution_clock_id",
            require_identifier(self.execution_clock_id, "execution_clock_id"),
        )
        object.__setattr__(self, "max_pending_packets", int(self.max_pending_packets))
        if self.max_pending_packets <= 0:
            raise ValueError("max_pending_packets must be positive")
        object.__setattr__(
            self,
            "allowed_lateness_seconds",
            require_finite(self.allowed_lateness_seconds, "allowed_lateness_seconds"),
        )
        if self.allowed_lateness_seconds < 0:
            raise ValueError("allowed_lateness_seconds cannot be negative")
        object.__setattr__(self, "backpressure", BackpressurePolicy(self.backpressure))
        object.__setattr__(self, "lateness", LatenessPolicy(self.lateness))
        object.__setattr__(self, "max_idle_cycles", int(self.max_idle_cycles))
        if self.max_idle_cycles <= 0:
            raise ValueError("max_idle_cycles must be positive")
        if self.shadow_queue_limit is not None:
            object.__setattr__(self, "shadow_queue_limit", int(self.shadow_queue_limit))
            if self.shadow_queue_limit < 0:
                raise ValueError("shadow_queue_limit cannot be negative")


@dataclass(frozen=True, slots=True)
class ProcessBoundary:
    """Deployment placement metadata for a typed component proxy."""

    placement: ComponentPlacement = ComponentPlacement.IN_PROCESS
    endpoint_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "placement", ComponentPlacement(self.placement))
        if self.placement == ComponentPlacement.IN_PROCESS and self.endpoint_id is not None:
            raise ValueError("in-process components cannot declare an external endpoint")
        if self.placement != ComponentPlacement.IN_PROCESS and self.endpoint_id is None:
            raise ValueError("component proxies require endpoint_id")
        if self.endpoint_id is not None:
            object.__setattr__(self, "endpoint_id", require_identifier(self.endpoint_id, "endpoint_id"))
