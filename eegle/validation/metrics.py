"""Typed protocol metric registry shared by compilation and evaluation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from eegle._domain import WorkStatus
from eegle._validation import freeze_json, thaw_json
from eegle.models.predictions import Prediction
from eegle.processing.windows import DenseWindow
from eegle.validation.contracts import ValidationStatus


@dataclass(frozen=True, slots=True)
class MetricObservation:
    measure: str
    value: Any
    evidence_count: int
    status: ValidationStatus = ValidationStatus.PASS
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ValidationStatus(self.status))
        count = int(self.evidence_count)
        if count < 0:
            raise ValueError("metric evidence_count cannot be negative")
        object.__setattr__(self, "evidence_count", count)
        object.__setattr__(self, "value", freeze_json(self.value))
        if self.status == ValidationStatus.INSUFFICIENT_EVIDENCE and (
            self.reason is None or not self.reason.strip()
        ):
            raise ValueError("insufficient metric evidence requires a reason")

    @property
    def sufficient(self) -> bool:
        return self.status != ValidationStatus.INSUFFICIENT_EVIDENCE


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    measure: str
    required_parameters: frozenset[str]
    optional_parameters: frozenset[str]
    component_parameters: frozenset[str]
    port_parameters: Mapping[str, tuple[str, str | None]]
    evaluator: Callable[[Mapping[str, Any], Any], MetricObservation]

    @property
    def parameters(self) -> frozenset[str]:
        return self.required_parameters | self.optional_parameters


@dataclass(frozen=True, slots=True)
class MetricSpecIssue:
    code: str
    path: str
    message: str
    details: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", freeze_json(self.details or {}))


def _count_observation(measure: str, value: int) -> MetricObservation:
    return MetricObservation(measure, value, value)


def _input_count(_: Mapping[str, Any], result: Any) -> MetricObservation:
    return _count_observation("input_count", len(result.admitted_inputs))


def _emission_count(_: Mapping[str, Any], result: Any) -> MetricObservation:
    return _count_observation("emission_count", len(result.emissions))


def _work_count(_: Mapping[str, Any], result: Any) -> MetricObservation:
    return _count_observation("work_count", len(result.work))


def _artifact_count(_: Mapping[str, Any], result: Any) -> MetricObservation:
    return _count_observation("artifact_count", len(result.artifacts))


def _component_emission_count(
    parameters: Mapping[str, Any], result: Any
) -> MetricObservation:
    component_id = str(parameters["component_id"])
    port = parameters.get("port")
    count = sum(
        value.component_id == component_id
        and (port is None or value.output_port == str(port))
        for value in result.emissions
    )
    return _count_observation("component_emission_count", count)


def _work_status_count(
    parameters: Mapping[str, Any], result: Any
) -> MetricObservation:
    status = str(parameters["status"])
    component_id = parameters.get("component_id")
    count = sum(
        value.status.value == status
        and (component_id is None or value.component_id == str(component_id))
        for value in result.work
    )
    return _count_observation("work_status_count", count)


def _prediction_coverage(
    parameters: Mapping[str, Any], result: Any
) -> MetricObservation:
    prediction_component = str(parameters["prediction_component"])
    window_component = str(parameters["window_component"])
    prediction_port = parameters.get("prediction_port")
    window_port = parameters.get("window_port")
    predictions = sum(
        value.component_id == prediction_component
        and isinstance(value.value, Prediction)
        and (
            prediction_port is None
            or value.output_port == str(prediction_port)
        )
        for value in result.emissions
    )
    windows = sum(
        value.component_id == window_component
        and isinstance(value.value, DenseWindow)
        and (window_port is None or value.output_port == str(window_port))
        for value in result.emissions
    )
    if windows == 0:
        return MetricObservation(
            "prediction_coverage",
            None,
            0,
            ValidationStatus.INSUFFICIENT_EVIDENCE,
            "prediction coverage requires at least one admitted window",
        )
    return MetricObservation(
        "prediction_coverage",
        predictions / windows,
        windows,
    )


_DEFINITIONS = {
    "input_count": MetricDefinition(
        "input_count", frozenset(), frozenset(), frozenset(), {}, _input_count
    ),
    "emission_count": MetricDefinition(
        "emission_count", frozenset(), frozenset(), frozenset(), {}, _emission_count
    ),
    "work_count": MetricDefinition(
        "work_count", frozenset(), frozenset(), frozenset(), {}, _work_count
    ),
    "artifact_count": MetricDefinition(
        "artifact_count", frozenset(), frozenset(), frozenset(), {}, _artifact_count
    ),
    "component_emission_count": MetricDefinition(
        "component_emission_count",
        frozenset({"component_id"}),
        frozenset({"port"}),
        frozenset({"component_id"}),
        {"port": ("component_id", None)},
        _component_emission_count,
    ),
    "work_status_count": MetricDefinition(
        "work_status_count",
        frozenset({"status"}),
        frozenset({"component_id"}),
        frozenset({"component_id"}),
        {},
        _work_status_count,
    ),
    "prediction_coverage": MetricDefinition(
        "prediction_coverage",
        frozenset({"prediction_component", "window_component"}),
        frozenset({"prediction_port", "window_port"}),
        frozenset({"prediction_component", "window_component"}),
        {
            "prediction_port": (
                "prediction_component",
                "eegle.prediction.v2",
            ),
            "window_port": ("window_component", "eegle.dense_window.v1"),
        },
        _prediction_coverage,
    ),
}

METRIC_REGISTRY: Mapping[str, MetricDefinition] = MappingProxyType(_DEFINITIONS)


def metric_definition(measure: str) -> MetricDefinition:
    try:
        return METRIC_REGISTRY[str(measure)]
    except KeyError as exc:
        raise ValueError(f"unsupported acceptance metric measure: {measure}") from exc


def evaluate_metric(metric: Mapping[str, Any] | Any, result: Any) -> MetricObservation:
    payload = metric.to_payload() if callable(getattr(metric, "to_payload", None)) else metric
    if not isinstance(payload, Mapping):
        raise TypeError("metric must be a mapping or expose to_payload()")
    definition = metric_definition(str(payload["measure"]))
    parameters = payload.get("parameters") or {}
    if not isinstance(parameters, Mapping):
        raise TypeError("metric parameters must be an object")
    return definition.evaluator(parameters, result)


def validate_protocol_metrics(
    protocol: Any,
    suite: Any,
    resolved: Mapping[str, Any],
) -> tuple[MetricSpecIssue, ...]:
    """Validate metric vocabulary, parameters, phase scope, and output ports."""

    issues: list[MetricSpecIssue] = []
    components = {value.component_id: value for value in suite.components}
    metric_indices = {
        value.metric_id: index for index, value in enumerate(protocol.metrics)
    }
    metrics = {value.metric_id: value for value in protocol.metrics}
    criteria = {value.criterion_id: value for value in protocol.acceptance}
    active_criterion_ids = {
        criterion_id
        for phase in suite.phases
        for criterion_id in phase.acceptance_criteria
    }
    active_metric_ids = {
        criteria[criterion_id].metric_id
        for criterion_id in active_criterion_ids
        if criterion_id in criteria
    }
    metric_definitions: dict[str, MetricDefinition] = {}

    for index, metric in enumerate(protocol.metrics):
        path = f"$.protocol.metrics[{index}]"
        definition = METRIC_REGISTRY.get(metric.measure)
        if definition is None:
            issues.append(
                MetricSpecIssue(
                    "metric.measure_unsupported",
                    f"{path}.measure",
                    f"unsupported acceptance metric measure: {metric.measure}",
                    {"supported": sorted(METRIC_REGISTRY)},
                )
            )
            continue
        metric_definitions[metric.metric_id] = definition
        parameters = thaw_json(metric.parameters)
        active = metric.metric_id in active_metric_ids
        missing = sorted(definition.required_parameters - set(parameters))
        unknown = sorted(set(parameters) - definition.parameters)
        for name in missing:
            issues.append(
                MetricSpecIssue(
                    "metric.parameter_required",
                    f"{path}.parameters",
                    f"metric {metric.metric_id} requires parameter {name}",
                    {"parameter": name},
                )
            )
        for name in unknown:
            issues.append(
                MetricSpecIssue(
                    "metric.parameter_unknown",
                    f"{path}.parameters.{name}",
                    f"metric {metric.metric_id} does not accept parameter {name}",
                    {"parameter": name},
                )
            )
        for name in sorted(
            definition.component_parameters & set(parameters) if active else ()
        ):
            component_id = parameters[name]
            parameter_path = f"{path}.parameters.{name}"
            if not isinstance(component_id, str) or not component_id:
                issues.append(
                    MetricSpecIssue(
                        "metric.component_type",
                        parameter_path,
                        f"metric component parameter {name} must be a component identity",
                    )
                )
            elif component_id not in components:
                issues.append(
                    MetricSpecIssue(
                        "metric.component_reference",
                        parameter_path,
                        f"metric {metric.metric_id} references unknown component {component_id}",
                        {"component_id": component_id},
                    )
                )
        if metric.measure == "work_status_count" and "status" in parameters:
            valid_statuses = {value.value for value in WorkStatus}
            if parameters["status"] not in valid_statuses:
                issues.append(
                    MetricSpecIssue(
                        "metric.work_status",
                        f"{path}.parameters.status",
                        f"unknown work status {parameters['status']}",
                        {"supported": sorted(valid_statuses)},
                    )
                )
        for port_parameter, (component_parameter, expected_type) in (
            definition.port_parameters.items()
        ):
            if not active:
                continue
            component_id = parameters.get(component_parameter)
            port_name = parameters.get(port_parameter)
            descriptor = resolved.get(component_id)
            if descriptor is None or not isinstance(component_id, str):
                continue
            output_ports = {value.name: value for value in descriptor.output_ports}
            if port_name is not None:
                parameter_path = f"{path}.parameters.{port_parameter}"
                if not isinstance(port_name, str) or port_name not in output_ports:
                    issues.append(
                        MetricSpecIssue(
                            "metric.port_reference",
                            parameter_path,
                            f"component {component_id} has no output port {port_name}",
                            {"component_id": component_id, "port": port_name},
                        )
                    )
                    continue
                if expected_type is not None and output_ports[port_name].type_id != expected_type:
                    issues.append(
                        MetricSpecIssue(
                            "metric.port_type",
                            parameter_path,
                            f"metric {metric.metric_id} requires {expected_type} at "
                            f"{component_id}.{port_name}",
                            {
                                "expected": expected_type,
                                "observed": output_ports[port_name].type_id,
                            },
                        )
                    )
            elif expected_type is not None and not any(
                value.type_id == expected_type for value in output_ports.values()
            ):
                issues.append(
                    MetricSpecIssue(
                        "metric.component_output_type",
                        f"{path}.parameters.{component_parameter}",
                        f"component {component_id} has no {expected_type} output required by "
                        f"metric {metric.metric_id}",
                        {"expected": expected_type},
                    )
                )

    for index, criterion in enumerate(protocol.acceptance):
        if isinstance(criterion.value, bool) or not isinstance(
            criterion.value, (int, float)
        ):
            issues.append(
                MetricSpecIssue(
                    "metric.acceptance_value_type",
                    f"$.protocol.acceptance[{index}].value",
                    f"criterion {criterion.criterion_id} requires a numeric expected value",
                )
            )

    for phase_index, phase in enumerate(suite.phases):
        active = set(phase.components)
        for criterion_id in phase.acceptance_criteria:
            criterion = criteria.get(criterion_id)
            if criterion is None:
                continue
            metric = metrics.get(criterion.metric_id)
            definition = metric_definitions.get(criterion.metric_id)
            if metric is None or definition is None:
                continue
            parameters = thaw_json(metric.parameters)
            for name in sorted(definition.component_parameters):
                component_id = parameters.get(name)
                if isinstance(component_id, str) and component_id not in active:
                    metric_index = metric_indices[metric.metric_id]
                    issues.append(
                        MetricSpecIssue(
                            "metric.inactive_component",
                            f"$.suite.phases[{phase_index}].acceptance_criteria",
                            f"criterion {criterion_id} uses metric component {component_id} "
                            "outside the active phase",
                            {
                                "metric_path": f"$.protocol.metrics[{metric_index}]",
                                "component_id": component_id,
                            },
                        )
                    )
    return tuple(issues)
