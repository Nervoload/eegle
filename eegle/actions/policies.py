"""Dependency-light action policies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from eegle.actions.commands import ActionRequest
from eegle._validation import freeze_json, require_identifier, thaw_json
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


class StructuredActionPolicy:
    """Map named prediction values into a structured action request.

    The mapping is declarative and bounded: values are selected from the
    canonical prediction with dotted object paths.  It does not evaluate
    expressions or grant action authority.  A missing value suppresses the
    request by default so an incomplete model result cannot become an
    actuator command accidentally.
    """

    def __init__(
        self,
        *,
        capability: str,
        output_parameters: Mapping[str, str],
        constant_parameters: Mapping[str, Any] | None = None,
        missing_output: str = "suppress",
        intended_delivery_delay_seconds: float | None = None,
        expires_after_seconds: float | None = None,
    ) -> None:
        self.capability = require_identifier(capability, "capability")
        mappings = {
            require_identifier(str(parameter), "action parameter"): _value_path(path)
            for parameter, path in output_parameters.items()
        }
        if not mappings:
            raise ValueError("structured action policy requires output_parameters")
        self.output_parameters = freeze_json(mappings)
        self.constant_parameters = freeze_json(constant_parameters or {})
        if set(self.output_parameters) & set(self.constant_parameters):
            raise ValueError("dynamic and constant action parameters cannot overlap")
        if missing_output not in {"suppress", "fail"}:
            raise ValueError("missing_output must be 'suppress' or 'fail'")
        self.missing_output = missing_output
        self.intended_delivery_delay_seconds = _optional_nonnegative(
            intended_delivery_delay_seconds,
            "intended_delivery_delay_seconds",
        )
        self.expires_after_seconds = _optional_nonnegative(
            expires_after_seconds,
            "expires_after_seconds",
        )

    def decide(
        self,
        prediction: Prediction,
        state: Mapping[str, Any],
        context: "ExecutionContext",
    ) -> ActionRequest | None:
        if prediction.abstained:
            return None
        if context.current_time.clock_id != prediction.available_time.clock_id:
            raise ValueError("policy context and prediction availability must share a clock")
        if context.current_time.seconds < prediction.available_time.seconds:
            raise ValueError("policy cannot inspect a prediction before it is available")

        parameters = thaw_json(self.constant_parameters)
        for parameter, path in self.output_parameters.items():
            found, value = _select_prediction_value(prediction, str(path))
            if not found:
                if self.missing_output == "suppress":
                    return None
                raise ValueError(f"prediction does not contain required output {path}")
            parameters[str(parameter)] = value
        return ActionRequest(
            request_id=context.next_id("action_request"),
            capability=self.capability,
            requested_by=context.component_id,
            parameters=parameters,
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


def _value_path(value: str) -> str:
    parts = str(value).split(".")
    if not parts or any(not part for part in parts):
        raise ValueError("prediction output paths must use non-empty dotted names")
    for part in parts:
        require_identifier(part, "prediction output path")
    return ".".join(parts)


def _optional_nonnegative(value: float | None, field: str) -> float | None:
    if value is None:
        return None
    normalized = float(value)
    if normalized < 0:
        raise ValueError(f"{field} cannot be negative")
    return normalized


def _select_prediction_value(
    prediction: Prediction,
    path: str,
) -> tuple[bool, Any]:
    value: Any = prediction.outputs
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return False, None
        value = value[part]
    return True, thaw_json(freeze_json(value))
