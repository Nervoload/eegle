"""Dependency-light action policies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from eegle.actions.commands import ActionCommand
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
