"""Semantic validation and replay taxonomy for versioned evidence records."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from eegle._validation import require_digest, require_identifier
from eegle.compiler.lock import canonical_hash
from eegle.recording.evidence import EvidenceRecord
from eegle.streams.clocks import TimePoint
from eegle.validation.contracts import (
    EvidenceReference,
    ValidationLayer,
    ValidationObservation,
    ValidationResult,
    ValidationSeverity,
    ValidationStatus,
)

# Read-only replay remains able to compare older target-shaped bundles while
# current engine records are governed by CURRENT_RUNTIME_RECORD_TYPES.
LEGACY_REPLAY_RECORD_TYPES = frozenset(
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
        "outcome_matched",
        "outcome_unmatched",
        "outcome_duplicate",
        "outcome_rejected",
        "outcome_use",
        "trigger_fired",
        "trigger_cancelled",
        "trigger_timed_out",
        "trigger_failed",
        "trigger_rescheduled",
    }
)

PayloadValidator = Callable[[Mapping[str, Any]], None]


def validate_evidence_payloads(
    records: Sequence[EvidenceRecord],
    *,
    subject_id: str,
    bundle_id: str | None = None,
) -> ValidationResult:
    """Validate sequence identity and every known semantic payload."""

    values = tuple(records)
    if not values:
        return ValidationResult(
            "integrity.semantic_evidence",
            ValidationLayer.INTEGRITY,
            ValidationStatus.INSUFFICIENT_EVIDENCE,
            ValidationSeverity.WARNING,
            "No evidence records were available for semantic validation.",
            observations=(
                ValidationObservation(
                    "evidence.record_count",
                    ValidationStatus.INSUFFICIENT_EVIDENCE,
                    0,
                    0,
                    "semantic validation requires at least one evidence record",
                ),
            ),
        )

    issues: list[dict[str, Any]] = []
    references: list[EvidenceReference] = []
    unknown: Counter[str] = Counter()
    record_ids: set[str] = set()
    prior_sequence: int | None = None
    prior_time: TimePoint | None = None
    for record in values:
        reference = evidence_record_reference(
            record, subject_id=subject_id, bundle_id=bundle_id
        )
        if prior_sequence is None and record.sequence != 0:
            _issue(
                issues,
                references,
                reference,
                "sequence_start",
                f"expected evidence sequence 0, observed {record.sequence}",
            )
        if record.record_id in record_ids:
            _issue(issues, references, reference, "duplicate_record_id", "record_id is duplicated")
        record_ids.add(record.record_id)
        if prior_sequence is not None and record.sequence != prior_sequence + 1:
            _issue(
                issues,
                references,
                reference,
                "sequence_gap",
                f"expected evidence sequence {prior_sequence + 1}, observed {record.sequence}",
            )
        prior_sequence = record.sequence
        if prior_time is not None:
            if record.emitted_time.clock_id != prior_time.clock_id:
                _issue(
                    issues,
                    references,
                    reference,
                    "emission_clock_changed",
                    "evidence emission clock changed within one bundle",
                )
            elif record.emitted_time.seconds < prior_time.seconds:
                _issue(
                    issues,
                    references,
                    reference,
                    "emission_time_reversed",
                    "evidence emitted_time moved backwards",
                )
        prior_time = record.emitted_time
        validator = _PAYLOAD_VALIDATORS.get(record.record_type)
        if validator is None:
            unknown[record.record_type] += 1
            continue
        try:
            validator(record.payload)
        except (KeyError, TypeError, ValueError) as exc:
            _issue(
                issues,
                references,
                reference,
                "payload_invalid",
                f"{type(exc).__name__}: {exc}",
            )

    if issues:
        status = ValidationStatus.FAIL
        severity = ValidationSeverity.ERROR
        summary = f"{len(issues)} semantic evidence error(s) were found."
    elif unknown:
        status = ValidationStatus.WARNING
        severity = ValidationSeverity.WARNING
        summary = "Known evidence is valid; unregistered record types were not interpreted."
    else:
        status = ValidationStatus.PASS
        severity = ValidationSeverity.INFO
        summary = f"All {len(values)} evidence records passed semantic validation."
    return ValidationResult(
        "integrity.semantic_evidence",
        ValidationLayer.INTEGRITY,
        status,
        severity,
        summary,
        observations=(
            ValidationObservation(
                "evidence.record_count",
                ValidationStatus.PASS,
                len(values),
                len(values),
            ),
            ValidationObservation(
                "evidence.known_record_count",
                ValidationStatus.PASS,
                len(values) - sum(unknown.values()),
                len(values) - sum(unknown.values()),
            ),
        ),
        evidence=tuple(references),
        details={
            "issues": issues,
            "unknown_record_types": dict(sorted(unknown.items())),
            "registered_record_type_count": len(CURRENT_RUNTIME_RECORD_TYPES),
        },
    )


def evidence_record_reference(
    record: EvidenceRecord,
    *,
    subject_id: str,
    bundle_id: str | None = None,
    path: str | None = None,
) -> EvidenceReference:
    return EvidenceReference(
        subject_id=subject_id,
        bundle_id=bundle_id,
        record_id=record.record_id,
        sequence=record.sequence,
        path=path,
    )


def validate_evidence_record_payload(record: EvidenceRecord) -> None:
    """Raise when a registered record's payload violates its typed contract."""

    validator = _PAYLOAD_VALIDATORS.get(record.record_type)
    if validator is None:
        raise KeyError(f"unregistered evidence record type: {record.record_type}")
    validator(record.payload)


def _issue(
    issues: list[dict[str, Any]],
    references: list[EvidenceReference],
    reference: EvidenceReference,
    code: str,
    message: str,
) -> None:
    references.append(reference)
    issues.append(
        {
            "code": code,
            "message": message,
            "record_id": reference.record_id,
            "sequence": reference.sequence,
        }
    )


def _required(payload: Mapping[str, Any], *fields: str) -> None:
    missing = [field for field in fields if field not in payload]
    if missing:
        raise ValueError("missing required payload field(s): " + ", ".join(missing))


def _identifiers(payload: Mapping[str, Any], *fields: str) -> None:
    _required(payload, *fields)
    for field in fields:
        require_identifier(str(payload[field]), field)


def _time(payload: Mapping[str, Any], field: str) -> TimePoint:
    _required(payload, field)
    value = payload[field]
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a time-point object")
    return TimePoint.from_payload(value)


def _work(payload: Mapping[str, Any]) -> None:
    from eegle.runtime.state import WorkRecord

    WorkRecord.from_payload(payload)


def _model_result_disposition(payload: Mapping[str, Any]) -> None:
    from eegle.runtime.model_runtime import ModelResultDisposition

    ModelResultDisposition.from_payload(payload)


def _model_comparison(payload: Mapping[str, Any]) -> None:
    from eegle.runtime.model_runtime import ModelComparison

    ModelComparison.from_payload(payload)


def _authorization_request(payload: Mapping[str, Any]) -> None:
    from eegle.actions.authorization import AuthorizationRequest

    AuthorizationRequest.from_payload(payload)


def _authorization_decision(payload: Mapping[str, Any]) -> None:
    from eegle.actions.authorization import AuthorizationDecision

    AuthorizationDecision.from_payload(payload)


def _authorized_command(payload: Mapping[str, Any]) -> None:
    from eegle.actions.commands import AuthorizedCommand

    AuthorizedCommand.from_payload(payload)


def _action_disposition(payload: Mapping[str, Any]) -> None:
    from eegle.actions.authorization import ActionDisposition

    ActionDisposition.from_payload(payload)


def _action_cancellation(payload: Mapping[str, Any]) -> None:
    from eegle.actions.authorization import ActionCancellation

    ActionCancellation.from_payload(payload)


def _action_request(payload: Mapping[str, Any]) -> None:
    from eegle.actions.commands import ActionRequest

    _identifiers(payload, "policy_component_id")
    request = payload.get("request")
    if not isinstance(request, Mapping):
        raise TypeError("action_request.request must be an object")
    value = ActionRequest.from_payload(request)
    if value.requested_by != payload["policy_component_id"]:
        raise ValueError("action request policy component identity mismatch")


def _action_receipt(payload: Mapping[str, Any]) -> None:
    from eegle.actions.receipts import ActionReceipt

    _identifiers(payload, "component_id")
    receipt = payload.get("receipt")
    if not isinstance(receipt, Mapping):
        raise TypeError("action_receipt.receipt must be an object")
    value = ActionReceipt.from_payload(receipt)
    if value.actuator_id != payload["component_id"]:
        raise ValueError("action receipt actuator identity mismatch")


def _outcome_received(payload: Mapping[str, Any]) -> None:
    from eegle.runtime.outcomes import Outcome

    _identifiers(payload, "component_id")
    outcome = payload.get("outcome")
    if not isinstance(outcome, Mapping):
        raise TypeError("outcome_received.outcome must be an object")
    Outcome.from_payload(outcome)


def _outcome_disposition(payload: Mapping[str, Any]) -> None:
    from eegle.runtime.outcomes import OutcomeDisposition

    OutcomeDisposition.from_payload(payload)


def _adaptation_eligibility(payload: Mapping[str, Any]) -> None:
    from eegle.runtime.state import AdaptationEligibilityDecision

    AdaptationEligibilityDecision.from_payload(payload)


def _state_transition(payload: Mapping[str, Any]) -> None:
    from eegle.runtime.state import StateTransition

    StateTransition.from_payload(payload)


def _graph_emission(payload: Mapping[str, Any]) -> None:
    _identifiers(
        payload,
        "emission_id",
        "component_id",
        "output_port",
        "value_id",
        "value_type",
    )
    _time(payload, "available_time")
    _required(payload, "input_ids", "value_hash", "value")
    for value in payload["input_ids"]:
        require_identifier(str(value), "input_id")
    require_digest(str(payload["value_hash"]), "value_hash")
    if canonical_hash(payload["value"]) != payload["value_hash"]:
        raise ValueError("graph emission value_hash does not match value")
    value = payload["value"]
    if (
        isinstance(value, Mapping)
        and value.get("schema") is not None
        and str(value["schema"]) != str(payload["value_type"])
    ):
        raise ValueError("graph emission value_type does not match value schema")


def _acceptance_result(payload: Mapping[str, Any]) -> None:
    from eegle.validation.contracts import ValidationStatus

    _identifiers(payload, "criterion_id", "metric_id", "operator")
    _required(payload, "expected", "observed", "passed")
    status = (
        ValidationStatus(str(payload["status"]))
        if payload.get("status") is not None
        else ValidationStatus.PASS
        if bool(payload["passed"])
        else ValidationStatus.FAIL
    )
    if bool(payload["passed"]) != (status == ValidationStatus.PASS):
        raise ValueError("acceptance passed flag disagrees with status")
    count = int(payload.get("evidence_count", 0))
    if count < 0:
        raise ValueError("acceptance evidence_count cannot be negative")
    if status == ValidationStatus.INSUFFICIENT_EVIDENCE and not payload.get("reason"):
        raise ValueError("insufficient acceptance result requires a reason")


def _phase_lifecycle(payload: Mapping[str, Any]) -> None:
    _identifiers(payload, "phase_id")


def _phase_finished(payload: Mapping[str, Any]) -> None:
    _identifiers(payload, "phase_id", "status")
    for field in ("admitted_input_count", "emission_count", "work_count"):
        _required(payload, field)
        if int(payload[field]) < 0:
            raise ValueError(f"{field} cannot be negative")


def _phase_transition(payload: Mapping[str, Any]) -> None:
    _identifiers(payload, "source_phase", "target_phase", "condition")
    _time(payload, "transition_time")
    if int(payload["attempt"]) <= 0:
        raise ValueError("phase transition attempt must be positive")


def _component_state(payload: Mapping[str, Any]) -> None:
    _identifiers(payload, "component_id", "component_kind")
    _required(payload, "state")
    if not isinstance(payload["state"], Mapping):
        raise TypeError("component state must be an object")


def _artifact_registered(payload: Mapping[str, Any]) -> None:
    _identifiers(
        payload,
        "phase_id",
        "artifact_id",
        "producer_component",
        "producer_port",
    )
    require_digest(str(payload["digest"]), "digest")


def _trigger_result(payload: Mapping[str, Any]) -> None:
    from eegle.runtime.scheduling import ScheduledTrigger, TriggerDisposition

    _identifiers(payload, "phase_id", "disposition")
    TriggerDisposition(str(payload["disposition"]))
    trigger = payload.get("trigger")
    if not isinstance(trigger, Mapping):
        raise TypeError("trigger_result.trigger must be an object")
    ScheduledTrigger.from_payload(trigger)


def _state_trigger_scheduled(payload: Mapping[str, Any]) -> None:
    from eegle.runtime.scheduling import ScheduledTrigger

    _identifiers(payload, "rule_id")
    trigger = payload.get("trigger")
    if not isinstance(trigger, Mapping):
        raise TypeError("state_trigger_scheduled.trigger must be an object")
    ScheduledTrigger.from_payload(trigger)


def _clock_observation(payload: Mapping[str, Any]) -> None:
    _identifiers(payload, "component_id", "source_clock_id", "target_clock_id")
    for field in ("correction_seconds", "measured_at_seconds"):
        _required(payload, field)
        float(payload[field])
    if payload.get("timestamp_mode") not in {None, "raw_with_mapping"}:
        raise ValueError("unsupported source timestamp_mode")


def _source_observation(payload: Mapping[str, Any]) -> None:
    _identifiers(payload, "component_id")


def _source_packet_loss(payload: Mapping[str, Any]) -> None:
    _source_observation(payload)
    _required(
        payload,
        "expected_period_seconds",
        "observed_gap_seconds",
        "estimated_missing_samples",
        "previous_timestamp",
        "current_timestamp",
    )
    if float(payload["expected_period_seconds"]) <= 0:
        raise ValueError("expected packet period must be positive")
    if float(payload["observed_gap_seconds"]) <= 0:
        raise ValueError("observed packet gap must be positive")
    if int(payload["estimated_missing_samples"]) <= 0:
        raise ValueError("estimated missing samples must be positive")


def _source_reconnect(payload: Mapping[str, Any]) -> None:
    _source_observation(payload)
    _required(payload, "reconnect_count")
    if int(payload["reconnect_count"]) <= 0:
        raise ValueError("reconnect count must be positive")


def _simple_identifiers(*fields: str) -> PayloadValidator:
    def validate(payload: Mapping[str, Any]) -> None:
        _identifiers(payload, *fields)

    return validate


_PAYLOAD_VALIDATORS: Mapping[str, PayloadValidator] = {
    "acceptance_result": _acceptance_result,
    "action_cancellation": _action_cancellation,
    "action_disposition": _action_disposition,
    "action_receipt": _action_receipt,
    "action_request": _action_request,
    "adaptation_eligibility": _adaptation_eligibility,
    "artifact_registered": _artifact_registered,
    "authorization_decision": _authorization_decision,
    "authorization_request": _authorization_request,
    "authorized_command": _authorized_command,
    "checkpoint_created": _simple_identifiers("checkpoint_id", "phase_id"),
    "component_started": _simple_identifiers(
        "phase_id", "component_id", "component_version"
    ),
    "component_state": _component_state,
    "component_state_unavailable": _simple_identifiers("component_id"),
    "graph_emission": _graph_emission,
    "model_comparison": _model_comparison,
    "model_result_disposition": _model_result_disposition,
    "model_role_failure_disposition": _simple_identifiers(
        "component_id", "role_id", "disposition"
    ),
    "outcome_disposition": _outcome_disposition,
    "outcome_received": _outcome_received,
    "phase_blocked": _phase_lifecycle,
    "phase_failure": _phase_lifecycle,
    "phase_finished": _phase_finished,
    "phase_resumed": _phase_lifecycle,
    "phase_retry": _phase_lifecycle,
    "phase_started": _phase_lifecycle,
    "phase_timeout": _phase_lifecycle,
    "phase_transition": _phase_transition,
    "plan_blocked": lambda payload: _required(payload, "reason"),
    "plan_failure": lambda payload: _required(payload, "reason"),
    "runtime_close_failure": lambda payload: _required(payload, "failures"),
    "source_clock_observation": _clock_observation,
    "source_lateness": _simple_identifiers(
        "component_id", "packet_id", "disposition"
    ),
    "source_packet_loss": _source_packet_loss,
    "source_packet_rejected": _simple_identifiers(
        "component_id", "packet_id", "reason_code"
    ),
    "source_reconnect": _source_reconnect,
    "source_sequence_gap": _simple_identifiers("component_id", "packet_id"),
    "state_transition": _state_transition,
    "state_trigger_scheduled": _state_trigger_scheduled,
    "trigger_result": _trigger_result,
    "work": _work,
}

# The semantic decoder registry is the current evidence schema. Replay derives
# from it so adding a typed runtime record cannot silently leave comparison.
CURRENT_RUNTIME_RECORD_TYPES = frozenset(_PAYLOAD_VALIDATORS)
REPLAY_COMPARABLE_RECORD_TYPES = (
    CURRENT_RUNTIME_RECORD_TYPES
    - {"source_clock_observation", "source_packet_loss", "source_reconnect"}
) | LEGACY_REPLAY_RECORD_TYPES
