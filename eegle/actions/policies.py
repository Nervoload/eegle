"""Dependency-light action policies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from eegle.actions.commands import ActionCommand
from eegle.compiler.lock import canonical_hash
from eegle.models.predictions import Prediction

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
    ) -> ActionCommand | None:
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
    ) -> None:
        self.capability = str(capability)
        self.matching_label = str(matching_label)
        self.output_key = str(output_key)
        self.parameters = dict(parameters or {})

    def decide(
        self,
        prediction: Prediction,
        state: Mapping[str, Any],
        context: "ExecutionContext",
    ) -> ActionCommand | None:
        if prediction.outputs.get(self.output_key) != self.matching_label:
            return None
        return ActionCommand(
            command_id=context.next_id("command"),
            capability=self.capability,
            requested_by=context.component_id,
            parameters=self.parameters,
            requested_time=context.current_time,
            available_time=context.current_time,
            prediction_id=prediction.prediction_id,
            policy_state_hash=canonical_hash(state),
        )
