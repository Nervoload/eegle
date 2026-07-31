"""Deadline calculation and terminal work-record construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from eegle._domain import WorkStatus
from eegle.runtime.context import DeterministicIdSource
from eegle.runtime.plan_runtime import RuntimeNode
from eegle.runtime.state import WorkRecord
from eegle.streams.clocks import TimePoint


@dataclass(frozen=True, slots=True)
class PendingWork:
    stage: str
    input_ids: tuple[str, ...]
    started_time: TimePoint
    status: WorkStatus = WorkStatus.COMPLETED
    deadline_time: TimePoint | None = None
    reason_code: str | None = None


def component_deadline(
    component_id: str,
    started: TimePoint,
    deadlines_seconds: Mapping[str, float],
) -> TimePoint | None:
    seconds = deadlines_seconds.get(component_id)
    if seconds is None:
        return None
    return TimePoint(started.seconds + seconds, started.clock_id)


def completed_work(
    ids: DeterministicIdSource,
    node: RuntimeNode,
    stage: str,
    input_ids: tuple[str, ...],
    started: TimePoint,
    completed: TimePoint,
) -> WorkRecord:
    if completed.seconds < started.seconds:
        raise ValueError("completed work time cannot precede its start")
    return WorkRecord(
        work_id=ids.next("work"),
        component_id=node.component_id,
        stage=stage,
        status=WorkStatus.COMPLETED,
        started_time=started,
        completed_time=completed,
        input_ids=input_ids,
        role=node.planned.role,
    )


def failed_work(
    ids: DeterministicIdSource,
    node: RuntimeNode,
    started: TimePoint,
    completed: TimePoint,
    input_ids: tuple[str, ...],
    error: Exception,
) -> WorkRecord:
    return WorkRecord(
        work_id=ids.next("work"),
        component_id=node.component_id,
        stage=node.plugin.kind.value,
        status=WorkStatus.FAILED,
        started_time=started,
        completed_time=completed,
        input_ids=input_ids,
        role=node.planned.role,
        reason_code="component_error",
        details={"error": f"{type(error).__name__}: {error}"},
    )
