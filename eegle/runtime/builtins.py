"""Dependency-light outcome, adaptation, and trigger reference components."""

from __future__ import annotations

from typing import Any, Mapping

from eegle.compiler.lock import canonical_hash
from eegle.runtime.outcomes import Outcome, OutcomeUse
from eegle.runtime.scheduling import ScheduledTrigger, TriggerResult
from eegle.runtime.state import StateTransition, TransitionStatus
from eegle.streams.packets import SparseEventBatch


class SparseEventOutcomeResolver:
    """Convert matching sparse events into explicitly permissioned outcomes."""

    def __init__(
        self,
        *,
        event_kind: str,
        permitted_uses: tuple[str, ...],
        default_subject_id: str = "subject.unknown",
    ) -> None:
        self.event_kind = str(event_kind)
        self.permitted_uses = frozenset(OutcomeUse(value) for value in permitted_uses)
        self.default_subject_id = str(default_subject_id)

    def update(self, packet: SparseEventBatch, context: Any) -> tuple[Outcome, ...]:
        outcomes: list[Outcome] = []
        for event in packet.events:
            if event.kind != self.event_kind:
                continue
            payload = event.value if isinstance(event.value, Mapping) else {"value": event.value}
            outcomes.append(
                Outcome(
                    outcome_id=context.next_id("outcome"),
                    subject_id=str(payload.get("subject_id", self.default_subject_id)),
                    source_id=event.event_id,
                    value=payload.get("value"),
                    event_time=event.event_time,
                    available_time=event.available_time,
                    permitted_uses=self.permitted_uses,
                    prediction_ids=tuple(
                        str(value) for value in payload.get("prediction_ids", ())
                    ),
                )
            )
        return tuple(outcomes)


class AdaptiveCounter:
    """Small stateful adapter proving delayed-outcome and transition semantics."""

    def __init__(self, *, required_use: str = "adaptation") -> None:
        self.required_use = OutcomeUse(required_use)

    def update(
        self,
        outcome: Outcome,
        state: Mapping[str, Any],
        context: Any,
    ) -> StateTransition:
        if self.required_use not in outcome.permitted_uses:
            raise PermissionError(
                f"outcome {outcome.outcome_id} does not permit {self.required_use.value}"
            )
        mutable = state
        if not isinstance(mutable, dict):
            raise TypeError("adaptive counter requires mutable engine-owned state")
        prior_hash = canonical_hash(mutable)
        mutable["count"] = int(mutable.get("count", 0)) + 1
        mutable["last_outcome_id"] = outcome.outcome_id
        resulting_hash = canonical_hash(mutable)
        return StateTransition(
            transition_id=context.next_id("transition"),
            component_id=context.component_id,
            status=TransitionStatus.APPLIED,
            transition_kind="adaptation",
            transition_time=context.current_time,
            prior_state_hash=prior_hash,
            resulting_state_hash=resulting_hash,
            trigger_ids=(outcome.outcome_id,),
            metadata={"count": mutable["count"], "required_use": self.required_use.value},
        )


class TriggerRecordSink:
    """Snapshot-capable trigger endpoint used by durable orchestration suites."""

    def __init__(self) -> None:
        self.records: list[Mapping[str, Any]] = []

    def append(self, record: Any) -> None:
        self.records.append({"kind": "input", "value": str(record)})

    def handle_trigger(self, trigger: ScheduledTrigger, context: Any) -> TriggerResult:
        self.records.append(trigger.to_payload())
        return TriggerResult(details={"recorded": True, "count": len(self.records)})

    def snapshot_state(self) -> Mapping[str, Any]:
        return {"schema": "eegle.trigger_record_sink_state.v1", "records": self.records}

    def restore_state(self, state: Mapping[str, Any]) -> None:
        if state.get("schema") != "eegle.trigger_record_sink_state.v1":
            raise ValueError("unsupported trigger record sink state")
        self.records = [dict(value) for value in state.get("records", ())]
