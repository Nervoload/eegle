"""Dependency-light deployment authorization providers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eegle.actions.authorization import (
    AuthorizationDecision,
    AuthorizationRequest,
    AuthorizationResult,
    AuthorizationStatus,
)

if TYPE_CHECKING:
    from eegle.plugins.contracts import ExecutionContext


class SimulationAuthorizationProvider:
    """Deterministic provider restricted by its descriptor to simulation."""

    def __init__(
        self,
        *,
        decision: str = "authorized",
        decision_delay_seconds: float = 0.0,
        valid_for_seconds: float | None = None,
    ) -> None:
        self.decision = AuthorizationStatus(decision)
        if self.decision in {
            AuthorizationStatus.PENDING,
            AuthorizationStatus.EXPIRED,
            AuthorizationStatus.CANCELLED,
        }:
            raise ValueError("simulation provider requires a terminal provider decision")
        self.decision_delay_seconds = float(decision_delay_seconds)
        if self.decision_delay_seconds < 0:
            raise ValueError("simulation authorization delay cannot be negative")
        self.valid_for_seconds = (
            None if valid_for_seconds is None else float(valid_for_seconds)
        )
        if self.valid_for_seconds is not None and self.valid_for_seconds < 0:
            raise ValueError("simulation authorization validity cannot be negative")

    def authorize(
        self,
        request: AuthorizationRequest,
        context: "ExecutionContext",
    ) -> AuthorizationResult:
        if self.decision_delay_seconds > 0:
            return AuthorizationResult(
                status=AuthorizationStatus.PENDING,
                completion_delay_seconds=self.decision_delay_seconds,
                reason="simulation_decision_pending",
                evidence={"simulated": True},
            )
        return self._terminal()

    def resolve(
        self,
        request: AuthorizationRequest,
        pending: AuthorizationDecision,
        context: "ExecutionContext",
    ) -> AuthorizationResult:
        if pending.status != AuthorizationStatus.PENDING:
            raise ValueError("simulation provider can resolve only a pending decision")
        return self._terminal()

    def _terminal(self) -> AuthorizationResult:
        return AuthorizationResult(
            status=self.decision,
            valid_for_seconds=(
                self.valid_for_seconds
                if self.decision == AuthorizationStatus.AUTHORIZED
                else None
            ),
            reason=f"simulation_{self.decision.value}",
            evidence={"simulated": True},
        )
