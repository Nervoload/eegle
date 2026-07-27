"""Bounded direct-reference outcome enrollment owned by the execution engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from eegle._validation import freeze_json, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.compiler.plan import PlannedOutcomeExpectation
from eegle.models.predictions import Prediction
from eegle.runtime.outcomes import (
    Outcome,
    OutcomeDisposition,
    OutcomeDispositionStatus,
    OutcomeReference,
    OutcomeReferenceKind,
    OutcomeUse,
)
from eegle.streams.clocks import TimePoint


@dataclass(frozen=True, slots=True)
class OutcomeMatch:
    expectation_id: str
    prediction: Prediction
    outcome: Outcome
    uses_granted: tuple[OutcomeUse, ...]


@dataclass(frozen=True, slots=True)
class _Pending:
    expectation_id: str
    prediction: Prediction
    expires_at_seconds: float
    enrollment_order: int


class OutcomeCoordinator:
    """Enroll only declared predictions and match only stable direct references."""

    def __init__(
        self,
        expectations: tuple[PlannedOutcomeExpectation, ...],
        next_id: Callable[[str], str],
    ) -> None:
        self.expectations = expectations
        self._by_model: dict[str, tuple[PlannedOutcomeExpectation, ...]] = {}
        for value in expectations:
            self._by_model.setdefault(value.model_component_id, ())
            self._by_model[value.model_component_id] += (value,)
        self._by_id = {value.expectation_id: value for value in expectations}
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._history: dict[tuple[str, str], Mapping[str, Any]] = {}
        self._next_id = next_id
        self._order = 0

    def enroll(
        self,
        prediction: Prediction,
        now: TimePoint,
    ) -> tuple[OutcomeDisposition, ...]:
        dispositions: list[OutcomeDisposition] = list(self.expire(now))
        reference = OutcomeReference(
            OutcomeReferenceKind.PREDICTION, prediction.prediction_id
        )
        for expectation in self._by_model.get(prediction.component_id, ()):
            scoped = [
                value
                for key, value in self._pending.items()
                if key[0] == expectation.expectation_id
            ]
            if len(scoped) >= expectation.max_pending_predictions:
                if expectation.overflow_disposition == "reject_newest":
                    disposition = self._disposition(
                        expectation.expectation_id,
                        reference,
                        OutcomeDispositionStatus.OVERFLOWED,
                        now,
                        prediction_id=prediction.prediction_id,
                        reason_code="pending_limit_reject_newest",
                    )
                    self._history[(expectation.expectation_id, prediction.prediction_id)] = {
                        "status": disposition.status.value
                    }
                    dispositions.append(disposition)
                    continue
                oldest = min(scoped, key=lambda value: value.enrollment_order)
                del self._pending[(oldest.expectation_id, oldest.prediction.prediction_id)]
                overflow = self._disposition(
                    oldest.expectation_id,
                    OutcomeReference(
                        OutcomeReferenceKind.PREDICTION,
                        oldest.prediction.prediction_id,
                    ),
                    OutcomeDispositionStatus.OVERFLOWED,
                    now,
                    prediction_id=oldest.prediction.prediction_id,
                    reason_code="pending_limit_expire_oldest",
                )
                self._history[(oldest.expectation_id, oldest.prediction.prediction_id)] = {
                    "status": overflow.status.value
                }
                dispositions.append(overflow)
            self._order += 1
            self._pending[(expectation.expectation_id, prediction.prediction_id)] = _Pending(
                expectation.expectation_id,
                prediction,
                prediction.available_time.seconds + expectation.prediction_ttl_seconds,
                self._order,
            )
            dispositions.append(
                self._disposition(
                    expectation.expectation_id,
                    reference,
                    OutcomeDispositionStatus.PENDING,
                    now,
                    prediction_id=prediction.prediction_id,
                )
            )
        return tuple(dispositions)

    def receive(
        self,
        outcome_component_id: str,
        outcome: Outcome,
        now: TimePoint,
    ) -> tuple[tuple[OutcomeDisposition, ...], tuple[OutcomeMatch, ...]]:
        dispositions: list[OutcomeDisposition] = list(self.expire(now))
        matches: list[OutcomeMatch] = []
        relevant = tuple(
            value
            for value in self.expectations
            if outcome_component_id in value.outcome_component_ids
        )
        for reference in outcome.references:
            if reference.reference_kind != OutcomeReferenceKind.PREDICTION:
                dispositions.append(
                    self._disposition(
                        "expectation.unenrolled",
                        reference,
                        OutcomeDispositionStatus.UNMATCHED,
                        now,
                        outcome_id=outcome.outcome_id,
                        reason_code="reference_not_prediction_enrollment",
                    )
                )
                continue
            candidates = tuple(
                value
                for value in relevant
                if (value.expectation_id, reference.reference_id) in self._pending
                or (value.expectation_id, reference.reference_id) in self._history
            )
            if not candidates:
                dispositions.append(
                    self._disposition(
                        "expectation.unenrolled",
                        reference,
                        OutcomeDispositionStatus.UNMATCHED,
                        now,
                        outcome_id=outcome.outcome_id,
                        prediction_id=reference.reference_id,
                        reason_code="prediction_not_enrolled",
                    )
                )
                continue
            for expectation in candidates:
                key = (expectation.expectation_id, reference.reference_id)
                history = self._history.get(key)
                digest = canonical_hash(outcome.value)
                if history is not None:
                    if history.get("status") != OutcomeDispositionStatus.MATCHED.value:
                        dispositions.append(
                            self._disposition(
                                expectation.expectation_id,
                                reference,
                                OutcomeDispositionStatus.UNMATCHED,
                                now,
                                outcome_id=outcome.outcome_id,
                                prediction_id=reference.reference_id,
                                reason_code=f"prediction_{history.get('status', 'terminal')}",
                            )
                        )
                        continue
                    status = (
                        OutcomeDispositionStatus.DUPLICATE
                        if history.get("outcome_digest") == digest
                        and history.get("source_id") == outcome.source_id
                        else OutcomeDispositionStatus.DISPUTED
                    )
                    dispositions.append(
                        self._disposition(
                            expectation.expectation_id,
                            reference,
                            status,
                            now,
                            outcome_id=outcome.outcome_id,
                            prediction_id=reference.reference_id,
                            reason_code=(
                                "same_source_value" if status == OutcomeDispositionStatus.DUPLICATE
                                else "conflicting_terminal_outcome"
                            ),
                        )
                    )
                    continue
                pending = self._pending[key]
                allowed = {OutcomeUse(value) for value in expectation.permitted_uses}
                granted = tuple(sorted(outcome.permitted_uses.intersection(allowed), key=lambda x: x.value))
                denied = tuple(sorted(outcome.permitted_uses - set(granted), key=lambda x: x.value))
                if outcome.retrospective_only:
                    del self._pending[key]
                    disposition = self._disposition(
                        expectation.expectation_id,
                        reference,
                        OutcomeDispositionStatus.RETROSPECTIVE_ONLY,
                        now,
                        outcome_id=outcome.outcome_id,
                        prediction_id=reference.reference_id,
                        uses_denied=tuple(
                            sorted(outcome.permitted_uses, key=lambda item: item.value)
                        ),
                        reason_code="outcome_declared_retrospective_only",
                    )
                    self._history[key] = {"status": disposition.status.value}
                    dispositions.append(disposition)
                    continue
                if not granted:
                    dispositions.append(
                        self._disposition(
                            expectation.expectation_id,
                            reference,
                            OutcomeDispositionStatus.REJECTED,
                            now,
                            outcome_id=outcome.outcome_id,
                            prediction_id=reference.reference_id,
                            uses_denied=denied,
                            reason_code="no_permitted_use",
                        )
                    )
                    continue
                del self._pending[key]
                self._history[key] = {
                    "status": OutcomeDispositionStatus.MATCHED.value,
                    "outcome_digest": digest,
                    "source_id": outcome.source_id,
                    "outcome_id": outcome.outcome_id,
                }
                dispositions.append(
                    self._disposition(
                        expectation.expectation_id,
                        reference,
                        OutcomeDispositionStatus.MATCHED,
                        now,
                        outcome_id=outcome.outcome_id,
                        prediction_id=reference.reference_id,
                        uses_granted=granted,
                        uses_denied=denied,
                    )
                )
                matches.append(
                    OutcomeMatch(
                        expectation.expectation_id,
                        pending.prediction,
                        outcome,
                        granted,
                    )
                )
        return tuple(dispositions), tuple(matches)

    def expire(self, now: TimePoint) -> tuple[OutcomeDisposition, ...]:
        values: list[OutcomeDisposition] = []
        expired = sorted(
            (
                value
                for value in self._pending.values()
                if value.expires_at_seconds < now.seconds
            ),
            key=lambda value: (value.expires_at_seconds, value.enrollment_order),
        )
        for value in expired:
            key = (value.expectation_id, value.prediction.prediction_id)
            del self._pending[key]
            disposition = self._disposition(
                value.expectation_id,
                OutcomeReference(
                    OutcomeReferenceKind.PREDICTION,
                    value.prediction.prediction_id,
                ),
                OutcomeDispositionStatus.EXPIRED,
                now,
                prediction_id=value.prediction.prediction_id,
                reason_code="prediction_ttl_elapsed",
            )
            self._history[key] = {"status": disposition.status.value}
            values.append(disposition)
        return tuple(values)

    def close(
        self,
        now: TimePoint,
        *,
        cancelled: bool,
    ) -> tuple[OutcomeDisposition, ...]:
        values = list(self.expire(now))
        status = (
            OutcomeDispositionStatus.CANCELLED
            if cancelled
            else OutcomeDispositionStatus.PENDING_AT_CLOSE
        )
        reason = "execution_cancelled" if cancelled else "execution_closed_without_outcome"
        for key, pending in sorted(
            self._pending.items(), key=lambda item: item[1].enrollment_order
        ):
            disposition = self._disposition(
                pending.expectation_id,
                OutcomeReference(
                    OutcomeReferenceKind.PREDICTION,
                    pending.prediction.prediction_id,
                ),
                status,
                now,
                prediction_id=pending.prediction.prediction_id,
                reason_code=reason,
            )
            self._history[key] = {"status": disposition.status.value}
            values.append(disposition)
        self._pending.clear()
        return tuple(values)

    def snapshot(self) -> Mapping[str, Any]:
        return freeze_json(
            {
                "order": self._order,
                "pending": [
                    {
                        "expectation_id": value.expectation_id,
                        "prediction": value.prediction.to_payload(),
                        "expires_at_seconds": value.expires_at_seconds,
                        "enrollment_order": value.enrollment_order,
                    }
                    for value in sorted(
                        self._pending.values(), key=lambda item: item.enrollment_order
                    )
                ],
                "history": {
                    f"{key[0]}|{key[1]}": thaw_json(value)
                    for key, value in sorted(self._history.items())
                },
            }
        )

    def restore(self, payload: Mapping[str, Any]) -> None:
        value = thaw_json(freeze_json(payload))
        self._order = int(value.get("order", 0))
        self._pending = {}
        for item in value.get("pending", ()):
            prediction = Prediction.from_payload(item["prediction"])
            pending = _Pending(
                str(item["expectation_id"]),
                prediction,
                float(item["expires_at_seconds"]),
                int(item["enrollment_order"]),
            )
            if pending.expectation_id not in self._by_id:
                raise ValueError("outcome checkpoint references an unknown expectation")
            self._pending[(pending.expectation_id, prediction.prediction_id)] = pending
        self._history = {}
        for key, item in dict(value.get("history") or {}).items():
            expectation_id, prediction_id = str(key).split("|", 1)
            if expectation_id not in self._by_id:
                raise ValueError("outcome history references an unknown expectation")
            self._history[(expectation_id, prediction_id)] = freeze_json(item)

    def _disposition(
        self,
        expectation_id: str,
        reference: OutcomeReference,
        status: OutcomeDispositionStatus,
        now: TimePoint,
        *,
        outcome_id: str | None = None,
        prediction_id: str | None = None,
        uses_granted: tuple[OutcomeUse, ...] = (),
        uses_denied: tuple[OutcomeUse, ...] = (),
        reason_code: str | None = None,
    ) -> OutcomeDisposition:
        return OutcomeDisposition(
            disposition_id=self._next_id("outcome_disposition"),
            expectation_id=expectation_id,
            reference=reference,
            status=status,
            decided_time=now,
            outcome_id=outcome_id,
            prediction_id=prediction_id,
            uses_granted=uses_granted,
            uses_denied=uses_denied,
            reason_code=reason_code,
        )
