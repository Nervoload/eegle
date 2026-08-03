"""Read-only aggregation over authoritative EEGle validation evidence."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from eegle.recording.evidence import EvidenceRecord
from eegle.streams.clocks import TimePoint
from eegle.validation.contracts import (
    EvidenceReference,
    ValidationLayer,
    ValidationObservation,
    ValidationReport,
    ValidationResult,
    ValidationSeverity,
    ValidationStatus,
)
from eegle.validation.evidence import (
    evidence_record_reference,
    validate_evidence_payloads,
)
from eegle.validation.metrics import METRIC_REGISTRY


def validate_evidence(
    records: Sequence[EvidenceRecord],
    *,
    subject_id: str,
    bundle_id: str | None = None,
    integrity_status: str | None = None,
    integrity_issues: Iterable[Mapping[str, Any]] = (),
    replay_report: Any | None = None,
    required_source_observations: Iterable[str] = (),
    source_clock_drift_tolerance_seconds: float | None = None,
) -> ValidationReport:
    """Validate one immutable evidence stream without mutating or recovering it."""

    values = tuple(records)
    results = [
        _integrity_result(
            values,
            subject_id=subject_id,
            bundle_id=bundle_id,
            status=integrity_status,
            issues=tuple(integrity_issues),
        ),
        validate_evidence_payloads(
            values,
            subject_id=subject_id,
            bundle_id=bundle_id,
        ),
        _source_health_result(
            values,
            subject_id=subject_id,
            bundle_id=bundle_id,
            required=tuple(required_source_observations),
            clock_drift_tolerance_seconds=source_clock_drift_tolerance_seconds,
        ),
        _timing_result(values, subject_id=subject_id, bundle_id=bundle_id),
        _causality_result(values, subject_id=subject_id, bundle_id=bundle_id),
        _execution_result(values, subject_id=subject_id, bundle_id=bundle_id),
        _model_result(values, subject_id=subject_id, bundle_id=bundle_id),
        _adaptation_result(values, subject_id=subject_id, bundle_id=bundle_id),
        _action_result(values, subject_id=subject_id, bundle_id=bundle_id),
        _protocol_result(values, subject_id=subject_id, bundle_id=bundle_id),
        validate_replay_report(
            replay_report,
            subject_id=subject_id,
            bundle_id=bundle_id,
        ),
    ]
    identity = bundle_id or subject_id
    return ValidationReport(
        report_id=f"validation.{identity}",
        subject_id=subject_id,
        results=tuple(results),
        metadata={
            "bundle_id": bundle_id,
            "record_count": len(values),
            "read_only": True,
        },
    )


def _source_health_result(
    records: Sequence[EvidenceRecord],
    *,
    subject_id: str,
    bundle_id: str | None,
    required: tuple[str, ...],
    clock_drift_tolerance_seconds: float | None,
) -> ValidationResult:
    supported = {
        "source_clock_observation",
        "source_packet_loss",
        "source_reconnect",
    }
    unknown = sorted(set(required) - supported)
    if unknown:
        raise ValueError(f"unsupported required source observations: {unknown}")
    observed = Counter(
        value.record_type for value in records if value.record_type in supported
    )
    missing = sorted(set(required) - set(observed))
    reconnects = max(
        (
            int(value.payload.get("reconnect_count", 0))
            for value in records
            if value.record_type == "source_reconnect"
        ),
        default=0,
    )
    loss_events = [
        value for value in records if value.record_type == "source_packet_loss"
    ]
    missing_samples = sum(
        int(value.payload.get("estimated_missing_samples", 0))
        for value in loss_events
    )
    source_records = [
        value for value in records if value.record_type in supported
    ]
    corrections_by_mapping: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for value in records:
        if value.record_type != "source_clock_observation":
            continue
        key = (
            str(value.payload["component_id"]),
            str(value.payload["source_clock_id"]),
            str(value.payload["target_clock_id"]),
        )
        corrections_by_mapping[key].append(
            float(value.payload["correction_seconds"])
        )
    correction_spans = {
        key: 0.0 if len(values) < 2 else max(values) - min(values)
        for key, values in corrections_by_mapping.items()
    }
    correction_count = sum(len(values) for values in corrections_by_mapping.values())
    correction_span = max(correction_spans.values(), default=0.0)
    drift_tolerance = (
        None
        if clock_drift_tolerance_seconds is None
        else float(clock_drift_tolerance_seconds)
    )
    if drift_tolerance is not None and (
        not math.isfinite(drift_tolerance) or drift_tolerance < 0
    ):
        raise ValueError("source clock drift tolerance must be finite and nonnegative")
    drift_detected = (
        drift_tolerance is not None and correction_span > drift_tolerance
    )
    if missing:
        status = ValidationStatus.INSUFFICIENT_EVIDENCE
        severity = ValidationSeverity.WARNING
        summary = "Required source clock or health observations are absent."
    elif reconnects or loss_events or drift_detected:
        status = ValidationStatus.WARNING
        severity = ValidationSeverity.WARNING
        summary = (
            "Source observations are complete and record reconnect, loss, or "
            "clock-drift conditions."
        )
    elif source_records or required:
        status = ValidationStatus.PASS
        severity = ValidationSeverity.INFO
        summary = "Required source clock and health observations are present."
    else:
        status = ValidationStatus.NOT_APPLICABLE
        severity = ValidationSeverity.INFO
        summary = "This evidence does not require live source observations."
    return ValidationResult(
        "source.acquisition_observations",
        ValidationLayer.CLOCKS_TIMING,
        status,
        severity,
        summary,
        observations=(
            ValidationObservation(
                "source.clock_observation_count",
                ValidationStatus.INSUFFICIENT_EVIDENCE
                if "source_clock_observation" in missing
                else ValidationStatus.PASS,
                observed["source_clock_observation"],
                observed["source_clock_observation"],
                "live source validation requires a clock mapping observation"
                if "source_clock_observation" in missing
                else None,
            ),
            ValidationObservation(
                "source.reconnect_count",
                ValidationStatus.WARNING if reconnects else ValidationStatus.PASS,
                reconnects,
                observed["source_reconnect"],
            ),
            ValidationObservation(
                "source.packet_loss_event_count",
                ValidationStatus.WARNING if loss_events else ValidationStatus.PASS,
                len(loss_events),
                len(loss_events),
            ),
            ValidationObservation(
                "source.estimated_missing_sample_count",
                ValidationStatus.WARNING if missing_samples else ValidationStatus.PASS,
                missing_samples,
                len(loss_events),
            ),
            ValidationObservation(
                "source.clock_correction_span_seconds",
                ValidationStatus.WARNING
                if drift_detected
                else ValidationStatus.PASS,
                correction_span,
                correction_count,
                (
                    "clock correction span exceeds the compiled mapping uncertainty"
                    if drift_detected
                    else None
                ),
            ),
        ),
        evidence=tuple(
            evidence_record_reference(
                value,
                subject_id=subject_id,
                bundle_id=bundle_id,
            )
            for value in source_records
        ),
        details={
            "required_record_types": list(required),
            "missing_record_types": missing,
            "record_type_counts": dict(sorted(observed.items())),
            "clock_drift_tolerance_seconds": drift_tolerance,
            "clock_drift_detected": drift_detected,
            "clock_correction_spans_seconds": {
                f"{component}:{source}->{target}": span
                for (component, source, target), span in sorted(
                    correction_spans.items()
                )
            },
        },
    )


def validate_locked_plan(
    plan: Any,
    lock: Any,
    *,
    subject_id: str,
) -> ValidationReport:
    """Project the existing plan/lock and graph authorities into validation results."""

    results: list[ValidationResult] = []
    try:
        lock.verify_plan(plan)
    except (TypeError, ValueError) as exc:
        results.append(
            ValidationResult(
                "definition.plan_lock",
                ValidationLayer.DEFINITION,
                ValidationStatus.FAIL,
                ValidationSeverity.ERROR,
                "Execution plan and lock do not verify.",
                details={"error": f"{type(exc).__name__}: {exc}"},
            )
        )
    else:
        results.append(
            ValidationResult(
                "definition.plan_lock",
                ValidationLayer.DEFINITION,
                ValidationStatus.PASS,
                ValidationSeverity.INFO,
                "Execution plan and lock verify with the same canonical identity.",
                observations=(
                    ValidationObservation(
                        "plan.hash",
                        ValidationStatus.PASS,
                        plan.plan_hash,
                        1,
                    ),
                ),
            )
        )

    graph = getattr(plan, "graph", None)
    if graph is None:
        results.append(
            ValidationResult(
                "compatibility.compiled_graph",
                ValidationLayer.COMPATIBILITY,
                ValidationStatus.FAIL,
                ValidationSeverity.ERROR,
                "Locked plan has no compiled graph.",
            )
        )
    else:
        results.append(
            ValidationResult(
                "compatibility.compiled_graph",
                ValidationLayer.COMPATIBILITY,
                ValidationStatus.PASS,
                ValidationSeverity.INFO,
                "Port, route, and component compatibility is represented by the compiled graph.",
                observations=(
                    ValidationObservation(
                        "graph.hash",
                        ValidationStatus.PASS,
                        graph.graph_hash,
                        len(graph.routes) + len(graph.ports),
                    ),
                ),
                details={
                    "component_count": len(plan.components),
                    "port_count": len(graph.ports),
                    "route_count": len(graph.routes),
                },
            )
        )

    metrics = tuple(plan.validation_rules.get("metrics", ()))
    unsupported = sorted(
        {
            str(value.get("measure"))
            for value in metrics
            if str(value.get("measure")) not in METRIC_REGISTRY
        }
    )
    if unsupported:
        metric_status = ValidationStatus.FAIL
        metric_severity = ValidationSeverity.ERROR
        metric_summary = "Locked plan contains unsupported protocol metrics."
    else:
        metric_status = ValidationStatus.PASS
        metric_severity = ValidationSeverity.INFO
        metric_summary = "Locked protocol metrics use the registered Phase 8 vocabulary."
    results.append(
        ValidationResult(
            "protocol.metric_registry",
            ValidationLayer.PROTOCOL,
            metric_status,
            metric_severity,
            metric_summary,
            observations=(
                ValidationObservation(
                    "protocol.metric_count",
                    ValidationStatus.PASS,
                    len(metrics),
                    len(metrics),
                ),
            ),
            details={
                "unsupported": unsupported,
                "supported": sorted(METRIC_REGISTRY),
            },
        )
    )
    return ValidationReport(
        report_id=f"validation.{subject_id}.definition",
        subject_id=subject_id,
        results=tuple(results),
        metadata={"plan_hash": getattr(plan, "plan_hash", None), "read_only": True},
    )


def validate_replay_report(
    replay_report: Any | None,
    *,
    subject_id: str,
    bundle_id: str | None = None,
) -> ValidationResult:
    if replay_report is None:
        return ValidationResult(
            "replay.equivalence",
            ValidationLayer.REPLAY,
            ValidationStatus.INSUFFICIENT_EVIDENCE,
            ValidationSeverity.WARNING,
            "Replay equivalence was not evaluated.",
        )
    compared = int(getattr(replay_report, "compared_record_count", 0))
    divergences = tuple(getattr(replay_report, "divergences", ()))
    if compared == 0:
        status = ValidationStatus.INSUFFICIENT_EVIDENCE
        severity = ValidationSeverity.WARNING
        summary = "Replay produced no comparable evidence records."
    elif bool(getattr(replay_report, "equivalent", False)):
        status = ValidationStatus.PASS
        severity = ValidationSeverity.INFO
        summary = f"Replay matched across {compared} comparable evidence records."
    else:
        status = ValidationStatus.FAIL
        severity = ValidationSeverity.ERROR
        summary = f"Replay diverged across {compared} comparable evidence records."
    return ValidationResult(
        "replay.equivalence",
        ValidationLayer.REPLAY,
        status,
        severity,
        summary,
        observations=(
            ValidationObservation(
                "replay.compared_record_count",
                ValidationStatus.PASS
                if compared
                else ValidationStatus.INSUFFICIENT_EVIDENCE,
                compared,
                compared,
                None if compared else "replay comparison requires comparable records",
            ),
        ),
        evidence=tuple(
            EvidenceReference(subject_id, bundle_id=bundle_id)
            for _ in (() if bundle_id is None else (bundle_id,))
        ),
        details={
            "equivalent": getattr(replay_report, "equivalent", None),
            "requested_level": _enum_value(
                getattr(replay_report, "requested_level", None)
            ),
            "evaluated_level": _enum_value(
                getattr(replay_report, "evaluated_level", None)
            ),
            "divergence_count": len(divergences),
            "first_divergence": None
            if not divergences
            else _divergence_payload(divergences[0]),
        },
    )


def combine_validation_reports(
    report_id: str,
    subject_id: str,
    reports: Iterable[ValidationReport],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> ValidationReport:
    """Combine reports while preserving each owning result without reinterpretation."""

    results: list[ValidationResult] = []
    for report_index, report in enumerate(reports):
        for result in report.results:
            results.append(
                ValidationResult(
                    f"report{report_index}.{result.result_id}",
                    result.layer,
                    result.status,
                    result.severity,
                    result.summary,
                    result.observations,
                    result.evidence,
                    result.details,
                )
            )
    return ValidationReport(
        report_id,
        subject_id,
        tuple(results),
        metadata or {},
    )


def _integrity_result(
    records: Sequence[EvidenceRecord],
    *,
    subject_id: str,
    bundle_id: str | None,
    status: str | None,
    issues: tuple[Mapping[str, Any], ...],
) -> ValidationResult:
    normalized = None if status is None else str(status)
    if normalized == "valid":
        result_status = ValidationStatus.PASS
        severity = ValidationSeverity.INFO
        summary = "The evidence bundle passed framing, digest, and manifest verification."
    elif normalized == "recoverable":
        result_status = ValidationStatus.WARNING
        severity = ValidationSeverity.WARNING
        summary = "The evidence bundle is recoverable but not complete as published."
    elif normalized == "unrecoverable":
        result_status = ValidationStatus.FAIL
        severity = ValidationSeverity.ERROR
        summary = "The evidence bundle failed integrity verification."
    else:
        result_status = ValidationStatus.INSUFFICIENT_EVIDENCE
        severity = ValidationSeverity.WARNING
        summary = "No authoritative bundle integrity report was supplied."
    evidence = ()
    if bundle_id is not None:
        evidence = (EvidenceReference(subject_id, bundle_id=bundle_id),)
    return ValidationResult(
        "integrity.bundle",
        ValidationLayer.INTEGRITY,
        result_status,
        severity,
        summary,
        observations=(
            ValidationObservation(
                "integrity.record_count",
                ValidationStatus.PASS if records else ValidationStatus.INSUFFICIENT_EVIDENCE,
                len(records),
                len(records),
                None if records else "integrity validation found no readable records",
            ),
        ),
        evidence=evidence,
        details={"integrity_status": normalized, "issues": list(issues)},
    )


def _timing_result(
    records: Sequence[EvidenceRecord], *, subject_id: str, bundle_id: str | None
) -> ValidationResult:
    checked = 0
    violations: list[dict[str, Any]] = []
    evidence: list[EvidenceReference] = []
    for record in records:
        if record.record_type != "graph_emission":
            continue
        available = record.payload.get("available_time")
        if not isinstance(available, Mapping):
            continue
        checked += 1
        point = TimePoint.from_payload(available)
        if (
            point.clock_id != record.emitted_time.clock_id
            or point.seconds > record.emitted_time.seconds
        ):
            reference = evidence_record_reference(
                record,
                subject_id=subject_id,
                bundle_id=bundle_id,
                path="$.payload.available_time",
            )
            evidence.append(reference)
            violations.append(
                {
                    "record_id": record.record_id,
                    "available_time": point.to_payload(),
                    "emitted_time": record.emitted_time.to_payload(),
                }
            )
    if not checked:
        status = ValidationStatus.NOT_APPLICABLE
        severity = ValidationSeverity.INFO
        summary = "No graph emissions required timing validation."
    elif violations:
        status = ValidationStatus.FAIL
        severity = ValidationSeverity.ERROR
        summary = "One or more graph values were emitted before they were available."
    else:
        status = ValidationStatus.PASS
        severity = ValidationSeverity.INFO
        summary = f"All {checked} graph emissions respect availability time."
    return ValidationResult(
        "timing.availability",
        ValidationLayer.CLOCKS_TIMING,
        status,
        severity,
        summary,
        observations=(
            ValidationObservation(
                "timing.checked_emission_count",
                ValidationStatus.PASS,
                checked,
                checked,
            ),
        ),
        evidence=tuple(evidence),
        details={"violations": violations},
    )


def _causality_result(
    records: Sequence[EvidenceRecord], *, subject_id: str, bundle_id: str | None
) -> ValidationResult:
    predictions = []
    violations: list[dict[str, Any]] = []
    evidence: list[EvidenceReference] = []
    for record in records:
        if record.record_type != "graph_emission":
            continue
        value = record.payload.get("value")
        if not isinstance(value, Mapping) or value.get("schema") != "eegle.prediction.v2":
            continue
        predictions.append(value)
        produced = TimePoint.from_payload(value["produced_time"])
        available = TimePoint.from_payload(value["available_time"])
        lineage = value.get("lineage")
        latest = None if not isinstance(lineage, Mapping) else lineage.get(
            "latest_input_available_time"
        )
        invalid = not value.get("input_ids") or not value.get("admitted_input_ids")
        if isinstance(latest, Mapping):
            latest_time = TimePoint.from_payload(latest)
            invalid = invalid or (
                latest_time.clock_id != produced.clock_id
                or latest_time.seconds > produced.seconds
            )
        invalid = invalid or (
            produced.clock_id != available.clock_id
            or produced.seconds > available.seconds
        )
        if invalid:
            evidence.append(
                evidence_record_reference(
                    record, subject_id=subject_id, bundle_id=bundle_id
                )
            )
            violations.append({"prediction_id": value.get("prediction_id")})
    if not predictions:
        status = ValidationStatus.NOT_APPLICABLE
        severity = ValidationSeverity.INFO
        summary = "No prediction lineage required causality validation."
    elif violations:
        status = ValidationStatus.FAIL
        severity = ValidationSeverity.ERROR
        summary = "Prediction lineage violates input-availability causality."
    else:
        status = ValidationStatus.PASS
        severity = ValidationSeverity.INFO
        summary = f"All {len(predictions)} predictions have causal input lineage."
    return ValidationResult(
        "causality.prediction_lineage",
        ValidationLayer.CAUSALITY,
        status,
        severity,
        summary,
        observations=(
            ValidationObservation(
                "causality.prediction_count",
                ValidationStatus.PASS,
                len(predictions),
                len(predictions),
            ),
        ),
        evidence=tuple(evidence),
        details={"violations": violations},
    )


def _execution_result(
    records: Sequence[EvidenceRecord], *, subject_id: str, bundle_id: str | None
) -> ValidationResult:
    terminal = [
        value
        for value in records
        if value.record_type
        in {"phase_finished", "phase_blocked", "plan_blocked", "plan_failure"}
    ]
    failed = [
        value
        for value in records
        if value.record_type == "plan_failure"
        or value.record_type == "phase_finished"
        and value.payload.get("status") not in {"complete", "checkpointed"}
    ]
    pending_work = [
        value
        for value in records
        if value.record_type == "work" and value.payload.get("status") == "pending"
    ]
    if not records or not terminal:
        status = ValidationStatus.INSUFFICIENT_EVIDENCE
        severity = ValidationSeverity.WARNING
        summary = "Execution has no terminal evidence."
    elif failed or pending_work:
        status = ValidationStatus.FAIL
        severity = ValidationSeverity.ERROR
        summary = "Execution ended with a failed terminal state or pending work."
    else:
        status = ValidationStatus.PASS
        severity = ValidationSeverity.INFO
        summary = "Execution has explicit terminal phase and work accounting."
    references = tuple(
        evidence_record_reference(value, subject_id=subject_id, bundle_id=bundle_id)
        for value in (*failed, *pending_work)
    )
    return ValidationResult(
        "execution.terminal_accounting",
        ValidationLayer.EXECUTION,
        status,
        severity,
        summary,
        observations=(
            ValidationObservation(
                "execution.terminal_record_count",
                ValidationStatus.PASS
                if terminal
                else ValidationStatus.INSUFFICIENT_EVIDENCE,
                len(terminal),
                len(terminal),
                None if terminal else "a completed validation requires terminal evidence",
            ),
        ),
        evidence=references,
        details={
            "failed_terminal_count": len(failed),
            "pending_work_count": len(pending_work),
        },
    )


def _model_result(
    records: Sequence[EvidenceRecord], *, subject_id: str, bundle_id: str | None
) -> ValidationResult:
    predictions: set[str] = set()
    dispositions: dict[str, str] = {}
    non_emitted: Counter[str] = Counter()
    for record in records:
        if record.record_type == "graph_emission":
            value = record.payload.get("value")
            if isinstance(value, Mapping) and value.get("schema") == "eegle.prediction.v2":
                predictions.add(str(value["prediction_id"]))
        elif record.record_type == "model_result_disposition":
            prediction_id = record.payload.get("prediction_id")
            status = str(record.payload.get("status"))
            if prediction_id is not None:
                dispositions[str(prediction_id)] = status
            if status != "emitted":
                non_emitted[status] += 1
    if not predictions and not dispositions and not non_emitted:
        status = ValidationStatus.NOT_APPLICABLE
        severity = ValidationSeverity.INFO
        summary = "Execution contains no model results."
        missing: list[str] = []
    else:
        missing = sorted(predictions - set(dispositions))
        mismatched = sorted(
            value for value in predictions if dispositions.get(value) != "emitted"
        )
        missing.extend(value for value in mismatched if value not in missing)
        if missing:
            status = ValidationStatus.FAIL
            severity = ValidationSeverity.ERROR
            summary = "One or more emitted predictions lack an emitted terminal disposition."
        elif non_emitted:
            status = ValidationStatus.WARNING
            severity = ValidationSeverity.WARNING
            summary = "Model results are terminally accounted for, with non-emitted dispositions."
        else:
            status = ValidationStatus.PASS
            severity = ValidationSeverity.INFO
            summary = "Every emitted prediction has an emitted terminal disposition."
    return ValidationResult(
        "model.result_accounting",
        ValidationLayer.MODEL,
        status,
        severity,
        summary,
        observations=(
            ValidationObservation(
                "model.prediction_count",
                ValidationStatus.PASS,
                len(predictions),
                len(predictions),
            ),
        ),
        details={
            "missing_prediction_ids": missing,
            "non_emitted_status_counts": dict(sorted(non_emitted.items())),
        },
    )


def _adaptation_result(
    records: Sequence[EvidenceRecord], *, subject_id: str, bundle_id: str | None
) -> ValidationResult:
    eligibility = [value for value in records if value.record_type == "adaptation_eligibility"]
    transitions = [
        value
        for value in records
        if value.record_type == "state_transition"
        and value.payload.get("transition_kind") == "adaptation"
    ]
    failures = [
        value
        for value in transitions
        if value.payload.get("status") in {"failed", "rolled_back"}
    ]
    if not eligibility and not transitions:
        status = ValidationStatus.NOT_APPLICABLE
        severity = ValidationSeverity.INFO
        summary = "Execution contains no adaptation decisions."
    elif failures:
        status = ValidationStatus.FAIL
        severity = ValidationSeverity.ERROR
        summary = "Adaptation contains failed or rolled-back state transitions."
    else:
        status = ValidationStatus.PASS
        severity = ValidationSeverity.INFO
        summary = "Adaptation eligibility and state transitions are explicitly recorded."
    return ValidationResult(
        "adaptation.transitions",
        ValidationLayer.ADAPTATION,
        status,
        severity,
        summary,
        evidence=tuple(
            evidence_record_reference(value, subject_id=subject_id, bundle_id=bundle_id)
            for value in failures
        ),
        details={
            "eligibility_count": len(eligibility),
            "transition_count": len(transitions),
        },
    )


def _action_result(
    records: Sequence[EvidenceRecord], *, subject_id: str, bundle_id: str | None
) -> ValidationResult:
    requests: dict[str, EvidenceRecord] = {}
    terminal: set[str] = set()
    authorized_commands: dict[str, tuple[str, str]] = {}
    decisions: dict[str, str] = {}
    receipts: set[str] = set()
    for record in records:
        payload = record.payload
        if record.record_type == "action_request":
            request = payload.get("request")
            if isinstance(request, Mapping) and request.get("request_id") is not None:
                requests[str(request["request_id"])] = record
        elif record.record_type == "action_disposition" and payload.get("terminal"):
            terminal.add(str(payload["action_request_id"]))
        elif record.record_type == "authorization_decision":
            decisions[str(payload["decision_id"])] = str(payload["status"])
        elif record.record_type == "authorized_command":
            authorized_commands[str(payload["command_id"])] = (
                str(payload["authorization_decision_id"]),
                str(payload["request_id"]),
            )
        elif record.record_type == "action_receipt":
            receipt = payload.get("receipt")
            if isinstance(receipt, Mapping):
                receipts.add(str(receipt["command_id"]))
    if not requests and not terminal and not authorized_commands:
        status = ValidationStatus.NOT_APPLICABLE
        severity = ValidationSeverity.INFO
        summary = "Execution contains no action requests."
        missing_terminal: list[str] = []
        bad_authorization: list[str] = []
    else:
        missing_terminal = sorted(set(requests) - terminal)
        bad_authorization = sorted(
            command_id
            for command_id, (decision_id, request_id) in authorized_commands.items()
            if decisions.get(decision_id) != "authorized" or request_id not in requests
        )
        if missing_terminal or bad_authorization:
            status = ValidationStatus.FAIL
            severity = ValidationSeverity.ERROR
            summary = "Action requests lack terminal or authorized command accounting."
        elif set(authorized_commands) - receipts:
            status = ValidationStatus.WARNING
            severity = ValidationSeverity.WARNING
            summary = "Authorized commands are terminally accounted for without every receipt."
        else:
            status = ValidationStatus.PASS
            severity = ValidationSeverity.INFO
            summary = "Action requests, authorization, commands, and receipts are accounted for."
    return ValidationResult(
        "actions.authorization_receipts",
        ValidationLayer.ACTIONS,
        status,
        severity,
        summary,
        evidence=tuple(
            evidence_record_reference(
                requests[value], subject_id=subject_id, bundle_id=bundle_id
            )
            for value in missing_terminal
        ),
        details={
            "request_count": len(requests),
            "terminal_request_count": len(terminal),
            "authorized_command_count": len(authorized_commands),
            "receipt_count": len(receipts),
            "missing_terminal_request_ids": missing_terminal,
            "invalid_authorization_command_ids": bad_authorization,
        },
    )


def _protocol_result(
    records: Sequence[EvidenceRecord], *, subject_id: str, bundle_id: str | None
) -> ValidationResult:
    acceptance = [value for value in records if value.record_type == "acceptance_result"]
    if not acceptance:
        return ValidationResult(
            "protocol.acceptance",
            ValidationLayer.PROTOCOL,
            ValidationStatus.NOT_APPLICABLE,
            ValidationSeverity.INFO,
            "The executed phase declared no protocol acceptance criteria.",
        )
    statuses = [
        str(value.payload.get("status"))
        if value.payload.get("status") is not None
        else "pass"
        if value.payload.get("passed")
        else "fail"
        for value in acceptance
    ]
    if "fail" in statuses:
        status = ValidationStatus.FAIL
        severity = ValidationSeverity.ERROR
        summary = "At least one protocol acceptance criterion failed."
    elif "insufficient_evidence" in statuses:
        status = ValidationStatus.INSUFFICIENT_EVIDENCE
        severity = ValidationSeverity.WARNING
        summary = "Protocol acceptance lacks required evidence."
    elif "warning" in statuses:
        status = ValidationStatus.WARNING
        severity = ValidationSeverity.WARNING
        summary = "Protocol acceptance completed with warnings."
    else:
        status = ValidationStatus.PASS
        severity = ValidationSeverity.INFO
        summary = "All evaluated protocol acceptance criteria passed."
    return ValidationResult(
        "protocol.acceptance",
        ValidationLayer.PROTOCOL,
        status,
        severity,
        summary,
        observations=tuple(
            ValidationObservation(
                f"criterion.{value.payload['criterion_id']}",
                ValidationStatus(status_value),
                value.payload.get("observed"),
                int(value.payload.get("evidence_count", 0)),
                value.payload.get("reason"),
                (
                    evidence_record_reference(
                        value, subject_id=subject_id, bundle_id=bundle_id
                    ),
                ),
            )
            for value, status_value in zip(acceptance, statuses)
        ),
    )


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _divergence_payload(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return {
            "record_index": value.get("record_index"),
            "record_type": value.get("record_type"),
            "path": value.get("path"),
            "message": value.get("message"),
        }
    return {
        "record_index": getattr(value, "record_index", None),
        "record_type": getattr(value, "record_type", None),
        "path": getattr(value, "path", None),
        "message": getattr(value, "message", None),
    }
