"""Deterministic scheduling, placement, lateness, and backpressure policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from eegle._validation import require_finite, require_identifier


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
