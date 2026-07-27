"""Dependency-light action policies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from eegle.actions.commands import ActionRequest
from eegle.compiler.lock import canonical_hash
from eegle.models.predictions import Prediction
from eegle.streams.clocks import TimePoint

if TYPE_CHECKING:
    from eegle.plugins.contracts import ExecutionContext


class ObserveOnlyPolicy:
    """Record policy evaluation while never requesting an external action."""

    policy_id = "eegle.observe_only"

    def decide(
        self,
        prediction: Prediction,
        state: Mapping[str, Any],
        context: "ExecutionContext",
    ) -> ActionRequest | None:
        if context.current_time.clock_id != prediction.available_time.clock_id:
            raise ValueError("policy context and prediction availability must share a clock")
        if context.current_time.seconds < prediction.available_time.seconds:
            raise ValueError("policy cannot inspect a prediction before it is available")
        return None


class LabelActionPolicy:
    """Issue one explicit action when a prediction label matches configuration."""

    def __init__(
        self,
        *,
        capability: str,
        matching_label: str,
        output_key: str = "label",
        parameters: Mapping[str, Any] | None = None,
        intended_delivery_delay_seconds: float | None = None,
        expires_after_seconds: float | None = None,
    ) -> None:
        self.capability = str(capability)
        self.matching_label = str(matching_label)
        self.output_key = str(output_key)
        self.parameters = dict(parameters or {})
        self.intended_delivery_delay_seconds = (
            None
            if intended_delivery_delay_seconds is None
            else float(intended_delivery_delay_seconds)
        )
        self.expires_after_seconds = (
            None if expires_after_seconds is None else float(expires_after_seconds)
        )
        for field in ("intended_delivery_delay_seconds", "expires_after_seconds"):
            value = getattr(self, field)
            if value is not None and value < 0:
                raise ValueError(f"{field} cannot be negative")

    def decide(
        self,
        prediction: Prediction,
        state: Mapping[str, Any],
        context: "ExecutionContext",
    ) -> ActionRequest | None:
        if prediction.outputs.get(self.output_key) != self.matching_label:
            return None
        return ActionRequest(
            request_id=context.next_id("action_request"),
            capability=self.capability,
            requested_by=context.component_id,
            parameters=self.parameters,
            requested_time=context.current_time,
            available_time=context.current_time,
            intended_delivery_time=None
            if self.intended_delivery_delay_seconds is None
            else TimePoint(
                context.current_time.seconds
                + self.intended_delivery_delay_seconds,
                context.current_time.clock_id,
            ),
            expires_at=None
            if self.expires_after_seconds is None
            else TimePoint(
                context.current_time.seconds + self.expires_after_seconds,
                context.current_time.clock_id,
            ),
            prediction_id=prediction.prediction_id,
            policy_state_hash=canonical_hash(state),
        )
