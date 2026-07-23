"""Declared replay equivalence and localized divergence reporting."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Sequence

from eegle._domain import EquivalenceLevel
from eegle._validation import require_finite
from eegle.compiler.lock import canonical_json_bytes
from eegle.recording.evidence import EvidenceRecord
from eegle.runtime.engine import EngineRunResult


_RANK = {
    EquivalenceLevel.BITWISE: 0,
    EquivalenceLevel.NUMERIC: 1,
    EquivalenceLevel.SEMANTIC: 2,
    EquivalenceLevel.TRACE: 3,
    EquivalenceLevel.NON_REPLAYABLE: 4,
}

_COMPARABLE_RECORDS = frozenset(
    {
        "input_admitted",
        "input_rejected",
        "packet_produced",
        "window_produced",
        "quality_decision",
        "prediction",
        "prediction_pending",
        "prediction_matched",
        "prediction_expired",
        "prediction_overflowed",
        "prediction_pending_at_end",
        "prediction_cancelled",
        "outcome_received",
        "outcome_matched",
        "outcome_unmatched",
        "outcome_duplicate",
        "outcome_rejected",
        "outcome_use",
        "adaptation_eligibility",
        "trigger_fired",
        "trigger_cancelled",
        "trigger_timed_out",
        "trigger_failed",
        "trigger_rescheduled",
        "state_trigger_scheduled",
        "state_transition",
        "work",
        "component_state",
        "action_command",
        "action_receipt",
    }
)


@dataclass(frozen=True, slots=True)
class EquivalencePolicy:
    requested_level: EquivalenceLevel = EquivalenceLevel.SEMANTIC
    relative_tolerance: float = 1e-7
    absolute_tolerance: float = 1e-9
    compare_component_state: bool = True
    substitute_hardware_actions: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "requested_level", EquivalenceLevel(self.requested_level)
        )
        object.__setattr__(
            self,
            "relative_tolerance",
            require_finite(self.relative_tolerance, "relative_tolerance"),
        )
        object.__setattr__(
            self,
            "absolute_tolerance",
            require_finite(self.absolute_tolerance, "absolute_tolerance"),
        )
        if self.relative_tolerance < 0 or self.absolute_tolerance < 0:
            raise ValueError("numeric tolerances cannot be negative")


@dataclass(frozen=True, slots=True)
class Divergence:
    record_index: int
    record_type: str
    path: str
    message: str
    reference: Any = None
    candidate: Any = None


@dataclass(frozen=True, slots=True)
class EquivalenceReport:
    equivalent: bool
    requested_level: EquivalenceLevel
    evaluated_level: EquivalenceLevel
    downgraded: bool
    compared_record_count: int
    divergences: tuple[Divergence, ...]


def compare_runs(
    reference: EngineRunResult,
    candidate: EngineRunResult,
    policy: EquivalencePolicy,
) -> EquivalenceReport:
    level = max(
        (
            policy.requested_level,
            reference.equivalence_ceiling,
            candidate.equivalence_ceiling,
        ),
        key=_RANK.__getitem__,
    )
    if level == EquivalenceLevel.NON_REPLAYABLE:
        divergence = Divergence(
            record_index=0,
            record_type="run",
            path="$",
            message="a relevant component declared non_replayable behavior",
        )
        return EquivalenceReport(
            equivalent=False,
            requested_level=policy.requested_level,
            evaluated_level=level,
            downgraded=level != policy.requested_level,
            compared_record_count=0,
            divergences=(divergence,),
        )
    reference_records = _records(reference, policy)
    candidate_records = _records(candidate, policy)
    divergences: list[Divergence] = []
    if len(reference_records) != len(candidate_records):
        divergences.append(
            Divergence(
                record_index=min(len(reference_records), len(candidate_records)),
                record_type="trace",
                path="$",
                message="comparable evidence record counts differ",
                reference=len(reference_records),
                candidate=len(candidate_records),
            )
        )
    for index, (left, right) in enumerate(zip(reference_records, candidate_records)):
        if left.record_type != right.record_type:
            divergences.append(
                Divergence(
                    index,
                    right.record_type,
                    "$.record_type",
                    "evidence record types differ",
                    left.record_type,
                    right.record_type,
                )
            )
            continue
        left_value, right_value = _project(left, level), _project(right, level)
        difference = _first_difference(
            left_value,
            right_value,
            level=level,
            relative_tolerance=policy.relative_tolerance,
            absolute_tolerance=policy.absolute_tolerance,
        )
        if difference is not None:
            path, message, expected, observed = difference
            divergences.append(
                Divergence(
                    index,
                    left.record_type,
                    path,
                    message,
                    expected,
                    observed,
                )
            )
    return EquivalenceReport(
        equivalent=not divergences,
        requested_level=policy.requested_level,
        evaluated_level=level,
        downgraded=level != policy.requested_level,
        compared_record_count=min(len(reference_records), len(candidate_records)),
        divergences=tuple(divergences),
    )


def _records(
    result: EngineRunResult, policy: EquivalencePolicy
) -> tuple[EvidenceRecord, ...]:
    records = tuple(
        record for record in result.evidence if record.record_type in _COMPARABLE_RECORDS
    )
    if policy.compare_component_state:
        return records
    return tuple(record for record in records if record.record_type != "component_state")


def _project(record: EvidenceRecord, level: EquivalenceLevel) -> Any:
    if level == EquivalenceLevel.BITWISE:
        return canonical_json_bytes(record.content_payload())
    if level == EquivalenceLevel.NUMERIC:
        return record.content_payload()
    if level == EquivalenceLevel.TRACE:
        return _trace_projection(record)
    return _semantic_projection(record)


def _trace_projection(record: EvidenceRecord) -> Mapping[str, Any]:
    payload = record.payload
    projected: dict[str, Any] = {"record_type": record.record_type}
    if record.record_type == "work":
        work = payload["work"]
        projected.update(
            {
                "component_id": work["component_id"],
                "stage": work["stage"],
                "status": work["status"],
                "role": work.get("role"),
                "input_ids": work.get("input_ids", ()),
            }
        )
    elif record.record_type == "prediction":
        prediction = payload["prediction"]
        projected.update(
            {
                "role": prediction["role"],
                "input_ids": prediction["input_ids"],
                "component_id": prediction["lineage"]["component_id"],
            }
        )
    elif record.record_type == "quality_decision":
        decision = payload["decision"]
        projected.update({"status": decision["status"], "item_id": decision["item_id"]})
    elif record.record_type in {"input_admitted", "packet_produced"}:
        packet = payload["packet"]
        projected.update(
            {
                "stream_id": packet["stream_id"],
                "stream_revision": packet["stream_revision"],
                "sequence_start": packet.get("sequence_start", packet.get("sequence")),
            }
        )
    elif record.record_type == "window_produced":
        window = payload["window"]
        projected.update(
            {
                "stream_id": window["stream_id"],
                "sequence_start": window["sequence_start"],
                "input_ids": window["input_ids"],
            }
        )
    elif record.record_type.startswith("outcome_"):
        outcome = payload.get("outcome", payload)
        projected.update(
            {
                "outcome_id": outcome.get("outcome_id"),
                "prediction_ids": outcome.get("prediction_ids", ()),
            }
        )
    elif record.record_type.startswith("prediction_"):
        projected.update(
            {
                "prediction_id": payload.get("prediction_id"),
                "reason_code": payload.get("reason_code"),
            }
        )
    elif record.record_type.startswith("trigger_"):
        trigger = payload.get("trigger", payload.get("next_trigger", payload))
        projected.update(
            {
                "trigger_id": trigger.get("trigger_id"),
                "target_component_id": trigger.get("target_component_id"),
                "disposition": payload.get("disposition"),
            }
        )
    elif record.record_type == "state_transition":
        transition = payload["transition"]
        projected.update(
            {
                "component_id": transition["component_id"],
                "status": transition["status"],
                "transition_kind": transition["transition_kind"],
                "trigger_ids": transition["trigger_ids"],
            }
        )
    elif record.record_type == "state_trigger_scheduled":
        projected.update(
            {
                "rule_id": payload["rule_id"],
                "transition_id": payload["transition_id"],
                "trigger_id": payload["trigger"]["trigger_id"],
            }
        )
    elif record.record_type == "action_receipt":
        receipt = payload["receipt"]
        projected.update(
            {
                "command_id": receipt["command_id"],
                "actuator_id": receipt["actuator_id"],
                "status": receipt["status"],
            }
        )
    else:
        projected["payload_keys"] = sorted(payload)
    return projected


def _semantic_projection(record: EvidenceRecord) -> Any:
    trace = dict(_trace_projection(record))
    payload = record.payload
    if record.record_type == "prediction":
        prediction = payload["prediction"]
        trace.update(
            {
                "model_id": prediction["model_id"],
                "outputs": _nonnumeric_structure(prediction["outputs"]),
            }
        )
    elif record.record_type == "quality_decision":
        decision = payload["decision"]
        trace["reasons"] = decision.get("reasons", ())
    elif record.record_type == "work":
        work = payload["work"]
        trace["reason_code"] = work.get("reason_code")
    elif record.record_type == "component_state":
        state = payload.get("state", {})
        trace.update(
            {
                "component_id": payload.get("component_id"),
                "state_schema": state.get("schema") if isinstance(state, Mapping) else None,
                "state_hash": state.get("state_hash") if isinstance(state, Mapping) else None,
            }
        )
    elif record.record_type == "action_command":
        command = payload["command"]
        trace.update(
            {
                "capability": command["capability"],
                "parameters": command["parameters"],
            }
        )
    elif record.record_type == "outcome_received":
        outcome = payload["outcome"]
        trace.update(
            {
                "value": outcome["value"],
                "permitted_uses": outcome["permitted_uses"],
            }
        )
    elif record.record_type in {"outcome_use", "adaptation_eligibility"}:
        trace.update(
            {
                "eligible": payload.get("eligible"),
                "applied": payload.get(
                    "applied", payload.get("adaptation_applied")
                ),
                "use": payload.get("use"),
                "reason_code": payload.get("reason_code"),
            }
        )
    elif record.record_type == "state_transition":
        transition = payload["transition"]
        trace.update(
            {
                "prior_state_hash": transition["prior_state_hash"],
                "resulting_state_hash": transition["resulting_state_hash"],
            }
        )
    elif record.record_type == "action_receipt":
        receipt = payload["receipt"]
        trace["details"] = receipt.get("details", {})
    return trace


def _nonnumeric_structure(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _nonnumeric_structure(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_nonnumeric_structure(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return "<number>"
    return value


def _first_difference(
    left: Any,
    right: Any,
    *,
    level: EquivalenceLevel,
    relative_tolerance: float,
    absolute_tolerance: float,
    path: str = "$",
) -> tuple[str, str, Any, Any] | None:
    if isinstance(left, bytes) or isinstance(right, bytes):
        if left != right:
            return path, "canonical bytes differ", left, right
        return None
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left) != set(right):
            return path, "mapping keys differ", sorted(left), sorted(right)
        for key in sorted(left):
            found = _first_difference(
                left[key],
                right[key],
                level=level,
                relative_tolerance=relative_tolerance,
                absolute_tolerance=absolute_tolerance,
                path=f"{path}.{key}",
            )
            if found is not None:
                return found
        return None
    if isinstance(left, Sequence) and not isinstance(left, (str, bytes)) and isinstance(
        right, Sequence
    ) and not isinstance(right, (str, bytes)):
        if len(left) != len(right):
            return path, "sequence lengths differ", len(left), len(right)
        for index, (expected, observed) in enumerate(zip(left, right)):
            found = _first_difference(
                expected,
                observed,
                level=level,
                relative_tolerance=relative_tolerance,
                absolute_tolerance=absolute_tolerance,
                path=f"{path}[{index}]",
            )
            if found is not None:
                return found
        return None
    numeric = (
        level == EquivalenceLevel.NUMERIC
        and not isinstance(left, bool)
        and not isinstance(right, bool)
        and isinstance(left, (int, float))
        and isinstance(right, (int, float))
    )
    if numeric:
        if not math.isclose(
            float(left),
            float(right),
            rel_tol=relative_tolerance,
            abs_tol=absolute_tolerance,
        ):
            return path, "numeric values differ beyond tolerance", left, right
        return None
    if left != right:
        return path, "values differ", left, right
    return None
