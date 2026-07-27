"""Deployment-owned authorization between policy requests and actuators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from eegle._validation import thaw_json
from eegle.actions.authorization import (
    ActionCancellation,
    ActionDisposition,
    ActionDispositionStatus,
    AuthorizationDecision,
    AuthorizationRequest,
    AuthorizationResult,
    AuthorizationStatus,
)
from eegle.actions.commands import ActionRequest, AuthorizedCommand
from eegle.actions.receipts import ActionReceipt, ReceiptStatus
from eegle.compiler.lock import canonical_hash
from eegle.compiler.plan import PlannedActionGrant
from eegle.runtime.plan_runtime import PlanRuntime, RuntimeAuthorizationProvider
from eegle.streams.clocks import TimePoint


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    action_request: ActionRequest
    authorization_request: AuthorizationRequest
    pending_decision: AuthorizationDecision
    actuator_id: str
    started_time: TimePoint
    input_ids: tuple[str, ...]

    @property
    def ready_time(self) -> TimePoint:
        assert self.pending_decision.next_check_time is not None
        return self.pending_decision.next_check_time


@dataclass(frozen=True, slots=True)
class BrokerOutcome:
    authorization_request: AuthorizationRequest
    decisions: tuple[AuthorizationDecision, ...]
    disposition: ActionDisposition
    command: AuthorizedCommand | None = None
    pending: PendingAuthorization | None = None
    cancellation: ActionCancellation | None = None


class ActionBroker:
    """The only runtime authority allowed to construct `AuthorizedCommand`."""

    def __init__(
        self,
        runtime: PlanRuntime,
        next_id: Callable[[str], str],
    ) -> None:
        self._runtime = runtime
        self._next_id = next_id
        self._grants: dict[tuple[str, str], PlannedActionGrant] = {}
        for grant in runtime.plan.action_grants:
            for actuator_id in grant.actuator_ids:
                self._grants[(actuator_id, grant.capability)] = grant

    def begin(
        self,
        request: ActionRequest,
        actuator_id: str,
        now: TimePoint,
        context: Any,
        *,
        input_ids: tuple[str, ...] = (),
    ) -> BrokerOutcome:
        self._validate_request_time(request, now)
        actuator = self._runtime.node(actuator_id)
        if request.capability not in actuator.planned.action_capabilities:
            authorization_request = self._authorization_request(
                request,
                actuator_id,
                now,
                permission_id="eegle.no_permission",
                provider_id="eegle.runtime_bounds",
            )
            decision = self._decision(
                authorization_request,
                AuthorizationStatus.DENIED,
                now,
                reason="actuator_capability_not_declared",
            )
            return self._terminal(request, actuator_id, authorization_request, decision)
        grant = self._grants.get((actuator_id, request.capability))
        if grant is None:
            authorization_request = self._authorization_request(
                request,
                actuator_id,
                now,
                permission_id="eegle.no_permission",
                provider_id="eegle.observe_only",
            )
            decision = self._decision(
                authorization_request,
                AuthorizationStatus.OBSERVE_ONLY,
                now,
                reason="no_independent_authorization_provider",
            )
            return self._terminal(request, actuator_id, authorization_request, decision)

        authorization_request = self._authorization_request(
            request,
            actuator_id,
            now,
            permission_id=grant.permission_id,
            provider_id=grant.provider_id,
        )
        bound_reason = self._constraint_rejection(request, grant, now)
        if bound_reason is not None:
            decision = self._decision(
                authorization_request,
                AuthorizationStatus.DENIED,
                now,
                reason=bound_reason,
                evidence={"locked_bounds": True},
            )
            return self._terminal(request, actuator_id, authorization_request, decision)

        runtime_provider = self._runtime.authorization_provider(grant.provider_id)
        try:
            result = runtime_provider.provider.authorize(authorization_request, context)
            return self._from_result(
                request,
                actuator_id,
                authorization_request,
                grant,
                result,
                now,
                started_time=now,
                input_ids=input_ids,
            )
        except Exception as exc:
            return self._provider_failure(
                request,
                actuator_id,
                authorization_request,
                runtime_provider,
                now,
                exc,
            )

    def resolve(
        self,
        pending: PendingAuthorization,
        now: TimePoint,
        context: Any,
    ) -> BrokerOutcome:
        if now.clock_id != pending.ready_time.clock_id:
            raise ValueError("pending authorization and runtime clock differ")
        if now.seconds < pending.ready_time.seconds:
            raise ValueError("pending authorization cannot resolve before next_check_time")
        request = pending.action_request
        authorization_request = pending.authorization_request
        if request.expires_at is not None and now.seconds > request.expires_at.seconds:
            decision = self._decision(
                authorization_request,
                AuthorizationStatus.EXPIRED,
                now,
                reason="action_request_expired_while_pending",
            )
            return self._terminal(
                request, pending.actuator_id, authorization_request, decision
            )
        grant = self._grants.get((pending.actuator_id, request.capability))
        if grant is None or grant.permission_id != authorization_request.permission_id:
            raise RuntimeError("pending authorization no longer matches the immutable plan")
        runtime_provider = self._runtime.authorization_provider(grant.provider_id)
        try:
            result = runtime_provider.provider.resolve(
                authorization_request,
                pending.pending_decision,
                context,
            )
            if result.status == AuthorizationStatus.PENDING:
                raise ValueError("authorization provider resolve() must return a terminal result")
            return self._from_result(
                request,
                pending.actuator_id,
                authorization_request,
                grant,
                result,
                now,
                started_time=pending.started_time,
                input_ids=pending.input_ids,
            )
        except Exception as exc:
            return self._provider_failure(
                request,
                pending.actuator_id,
                authorization_request,
                runtime_provider,
                now,
                exc,
            )

    def cancel(
        self,
        pending: PendingAuthorization,
        now: TimePoint,
        *,
        reason: str,
    ) -> BrokerOutcome:
        decision = self._decision(
            pending.authorization_request,
            AuthorizationStatus.CANCELLED,
            now,
            reason=reason,
        )
        cancellation = ActionCancellation(
            cancellation_id=self._next_id("action_cancellation"),
            action_request_id=pending.action_request.request_id,
            actuator_id=pending.actuator_id,
            cancelled_time=now,
            reason=reason,
            authorization_request_id=(
                pending.authorization_request.authorization_request_id
            ),
            authorization_decision_id=decision.decision_id,
        )
        outcome = self._terminal(
            pending.action_request,
            pending.actuator_id,
            pending.authorization_request,
            decision,
        )
        return BrokerOutcome(
            authorization_request=outcome.authorization_request,
            decisions=outcome.decisions,
            disposition=outcome.disposition,
            cancellation=cancellation,
        )

    def submitted(
        self,
        request: ActionRequest,
        command: AuthorizedCommand,
        now: TimePoint,
    ) -> ActionDisposition:
        return ActionDisposition(
            disposition_id=self._next_id("action_disposition"),
            action_request_id=request.request_id,
            actuator_id=command.actuator_id,
            status=ActionDispositionStatus.SUBMITTED,
            decided_time=now,
            authorization_request_id=command.authorization_request_id,
            authorization_decision_id=command.authorization_decision_id,
            command_id=command.command_id,
            terminal=False,
        )

    def receipt_disposition(
        self,
        request: ActionRequest,
        command: AuthorizedCommand,
        receipt: ActionReceipt,
    ) -> ActionDisposition:
        if receipt.command_id != command.command_id:
            raise ValueError("actuator receipt references a different command")
        if receipt.actuator_id != command.actuator_id:
            raise ValueError("actuator receipt identity differs from the authorized command")
        if receipt.authorization_decision_id != command.authorization_decision_id:
            raise ValueError("actuator receipt authorization differs from the command")
        status = {
            ReceiptStatus.ACCEPTED: ActionDispositionStatus.ACKNOWLEDGED,
            ReceiptStatus.DELIVERED: ActionDispositionStatus.DELIVERED,
            ReceiptStatus.REJECTED: ActionDispositionStatus.ACTUATOR_REJECTED,
            ReceiptStatus.FAILED: ActionDispositionStatus.FAILED,
            ReceiptStatus.CANCELLED: ActionDispositionStatus.CANCELLED,
            ReceiptStatus.EXPIRED: ActionDispositionStatus.EXPIRED,
        }[receipt.status]
        return ActionDisposition(
            disposition_id=self._next_id("action_disposition"),
            action_request_id=request.request_id,
            actuator_id=command.actuator_id,
            status=status,
            decided_time=receipt.observed_time,
            authorization_request_id=command.authorization_request_id,
            authorization_decision_id=command.authorization_decision_id,
            command_id=command.command_id,
            receipt_id=receipt.receipt_id,
            terminal=True,
        )

    def _authorization_request(
        self,
        request: ActionRequest,
        actuator_id: str,
        now: TimePoint,
        *,
        permission_id: str,
        provider_id: str,
    ) -> AuthorizationRequest:
        return AuthorizationRequest.from_action(
            authorization_request_id=self._next_id("authorization_request"),
            action_request_id=request.request_id,
            permission_id=permission_id,
            provider_id=provider_id,
            actuator_id=actuator_id,
            capability=request.capability,
            parameters=request.parameters,
            requested_time=now,
            available_time=now,
            expires_at=request.expires_at,
        )

    def _from_result(
        self,
        request: ActionRequest,
        actuator_id: str,
        authorization_request: AuthorizationRequest,
        grant: PlannedActionGrant,
        result: AuthorizationResult,
        now: TimePoint,
        *,
        started_time: TimePoint,
        input_ids: tuple[str, ...],
    ) -> BrokerOutcome:
        if not isinstance(result, AuthorizationResult):
            raise TypeError("authorization provider must return AuthorizationResult")
        if (
            result.status == AuthorizationStatus.PENDING
            and grant.maximum_decision_delay_seconds is not None
            and result.completion_delay_seconds
            > grant.maximum_decision_delay_seconds
        ):
            decision = self._decision(
                authorization_request,
                AuthorizationStatus.DENIED,
                now,
                reason="provider_delay_exceeds_locked_bound",
            )
            return self._terminal(request, actuator_id, authorization_request, decision)
        if result.status == AuthorizationStatus.PENDING:
            decision = self._decision(
                authorization_request,
                AuthorizationStatus.PENDING,
                now,
                next_check_time=TimePoint(
                    now.seconds + result.completion_delay_seconds,
                    now.clock_id,
                ),
                reason=result.reason,
                evidence=thaw_json(result.evidence),
            )
            disposition = self._disposition(
                request,
                actuator_id,
                decision,
                ActionDispositionStatus.PENDING,
                terminal=False,
            )
            return BrokerOutcome(
                authorization_request=authorization_request,
                decisions=(decision,),
                disposition=disposition,
                pending=PendingAuthorization(
                    request,
                    authorization_request,
                    decision,
                    actuator_id,
                    started_time,
                    input_ids,
                ),
            )
        valid_until = None
        if result.status == AuthorizationStatus.AUTHORIZED:
            candidates: list[TimePoint] = []
            if request.expires_at is not None:
                candidates.append(request.expires_at)
            if result.valid_for_seconds is not None:
                candidates.append(
                    TimePoint(now.seconds + result.valid_for_seconds, now.clock_id)
                )
            if candidates:
                valid_until = min(candidates, key=lambda value: value.seconds)
        decision = self._decision(
            authorization_request,
            result.status,
            now,
            valid_until=valid_until,
            reason=result.reason,
            evidence=thaw_json(result.evidence),
        )
        return self._terminal(request, actuator_id, authorization_request, decision)

    def _terminal(
        self,
        request: ActionRequest,
        actuator_id: str,
        authorization_request: AuthorizationRequest,
        decision: AuthorizationDecision,
        *,
        prior_decisions: tuple[AuthorizationDecision, ...] = (),
    ) -> BrokerOutcome:
        command = None
        status = ActionDispositionStatus(decision.status.value)
        terminal = True
        if decision.status == AuthorizationStatus.AUTHORIZED:
            command = AuthorizedCommand(
                command_id=self._next_id("authorized_command"),
                request_id=request.request_id,
                authorization_request_id=authorization_request.authorization_request_id,
                authorization_decision_id=decision.decision_id,
                permission_id=authorization_request.permission_id,
                provider_id=authorization_request.provider_id,
                actuator_id=actuator_id,
                capability=request.capability,
                parameters=request.parameters,
                requested_time=request.requested_time,
                available_time=decision.available_time,
                intended_delivery_time=request.intended_delivery_time,
                expires_at=decision.valid_until or request.expires_at,
                prediction_id=request.prediction_id,
                policy_state_hash=request.policy_state_hash,
            )
            terminal = False
        disposition = self._disposition(
            request,
            actuator_id,
            decision,
            status,
            terminal=terminal,
            command=command,
        )
        return BrokerOutcome(
            authorization_request=authorization_request,
            decisions=(*prior_decisions, decision),
            disposition=disposition,
            command=command,
        )

    def _provider_failure(
        self,
        request: ActionRequest,
        actuator_id: str,
        authorization_request: AuthorizationRequest,
        provider: RuntimeAuthorizationProvider,
        now: TimePoint,
        error: Exception,
    ) -> BrokerOutcome:
        failure = self._decision(
            authorization_request,
            AuthorizationStatus.FAILED,
            now,
            reason=f"{type(error).__name__}: {error}",
        )
        fallback_status = (
            AuthorizationStatus.OBSERVE_ONLY
            if provider.planned.failure_disposition == "observe_only"
            else AuthorizationStatus.DENIED
        )
        fallback = self._decision(
            authorization_request,
            fallback_status,
            now,
            reason=f"provider_failed_{provider.planned.failure_disposition}",
        )
        return self._terminal(
            request,
            actuator_id,
            authorization_request,
            fallback,
            prior_decisions=(failure,),
        )

    def _decision(
        self,
        request: AuthorizationRequest,
        status: AuthorizationStatus,
        now: TimePoint,
        *,
        valid_until: TimePoint | None = None,
        next_check_time: TimePoint | None = None,
        reason: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> AuthorizationDecision:
        return AuthorizationDecision(
            decision_id=self._next_id("authorization_decision"),
            authorization_request_id=request.authorization_request_id,
            action_request_id=request.action_request_id,
            permission_id=request.permission_id,
            provider_id=request.provider_id,
            status=status,
            decided_time=now,
            available_time=now,
            valid_until=valid_until,
            next_check_time=next_check_time,
            reason=reason,
            evidence=evidence or {},
        )

    def _disposition(
        self,
        request: ActionRequest,
        actuator_id: str,
        decision: AuthorizationDecision,
        status: ActionDispositionStatus,
        *,
        terminal: bool,
        command: AuthorizedCommand | None = None,
    ) -> ActionDisposition:
        return ActionDisposition(
            disposition_id=self._next_id("action_disposition"),
            action_request_id=request.request_id,
            actuator_id=actuator_id,
            status=status,
            decided_time=decision.available_time,
            authorization_request_id=decision.authorization_request_id,
            authorization_decision_id=decision.decision_id,
            command_id=None if command is None else command.command_id,
            reason=decision.reason,
            terminal=terminal,
        )

    @staticmethod
    def _validate_request_time(request: ActionRequest, now: TimePoint) -> None:
        if request.available_time.clock_id != now.clock_id:
            raise ValueError("action request and runtime clocks differ")
        if request.available_time.seconds > now.seconds:
            raise ValueError("action request cannot be authorized before availability")

    @staticmethod
    def _constraint_rejection(
        request: ActionRequest,
        grant: PlannedActionGrant,
        now: TimePoint,
    ) -> str | None:
        if request.expires_at is not None and now.seconds > request.expires_at.seconds:
            return "action_request_expired"
        if grant.maximum_request_ttl_seconds is not None:
            if request.expires_at is None:
                return "action_request_expiry_required"
            ttl = request.expires_at.seconds - request.requested_time.seconds
            if ttl > grant.maximum_request_ttl_seconds:
                return "action_request_ttl_exceeds_locked_bound"
        if grant.maximum_delivery_delay_seconds is not None:
            if request.intended_delivery_time is None:
                return "intended_delivery_time_required"
            delay = (
                request.intended_delivery_time.seconds
                - request.requested_time.seconds
            )
            if delay < 0 or delay > grant.maximum_delivery_delay_seconds:
                return "delivery_delay_exceeds_locked_bound"
        constraints = thaw_json(grant.parameter_constraints)
        for key, value in request.parameters.items():
            raw = constraints.get(key)
            if raw is None:
                if not grant.allow_unlisted_parameters:
                    return f"unlisted_parameter:{key}"
                continue
            allowed = raw.get("allowed_values") or []
            if allowed and canonical_hash(value) not in {
                canonical_hash(item) for item in allowed
            }:
                return f"parameter_not_allowed:{key}"
            minimum = raw.get("minimum")
            maximum = raw.get("maximum")
            if minimum is not None or maximum is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return f"parameter_not_numeric:{key}"
                if minimum is not None and value < minimum:
                    return f"parameter_below_minimum:{key}"
                if maximum is not None and value > maximum:
                    return f"parameter_above_maximum:{key}"
        return None
