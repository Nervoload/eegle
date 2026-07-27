"""Typed graph dispatch and versioned-value routing helpers."""

from __future__ import annotations

from typing import Any, Iterable, Mapping, MutableMapping

from eegle._domain import ComponentKind
from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.runtime.context import RuntimeExecutionContext
from eegle.runtime.model_runtime import execute_bound_model
from eegle.runtime.plan_runtime import RuntimeNode
from eegle.streams.clocks import TimePoint
from eegle.streams.packets import DenseSampleBatch, MetadataEvent, SparseEventBatch
from eegle.streams.synthetic import packet_available_time


def dispatch_component(
    node: RuntimeNode,
    input_port: str,
    value: Any,
    context: RuntimeExecutionContext,
    component_state: MutableMapping[str, dict[str, Any]],
    *,
    input_id: str | None = None,
    admitted_input_ids: tuple[str, ...] = (),
) -> Mapping[str, tuple[Any, ...]]:
    validate_input_port(node, input_port, value)
    if node.plugin.kind == ComponentKind.MODEL and node.model_binding is not None:
        if input_id is None:
            raise ValueError("bound model dispatch requires the exact graph input identity")
        return execute_bound_model(
            node,
            input_port,
            value,
            context,
            input_id=input_id,
            admitted_input_ids=admitted_input_ids,
            input_available_time=available_time(value, context.current_time),
        )
    process = getattr(node.component, "process", None)
    if callable(process):
        raw = process(input_port, value, context)
        if not isinstance(raw, Mapping):
            raise TypeError("graph node process() must return an output-port mapping")
        return normalize_outputs(node, raw)
    kind = node.plugin.kind
    if kind == ComponentKind.TRANSFORM:
        result = node.component.update(value, context)
    elif kind == ComponentKind.WINDOW:
        result = tuple(node.component.update(value, context))
    elif kind == ComponentKind.QUALITY:
        result = node.component.evaluate(value, context)
    elif kind == ComponentKind.MODEL:
        result = node.component.predict(value, context)
    elif kind == ComponentKind.OUTCOME:
        result = tuple(node.component.update(value, context))
        declared_uses = set(node.planned.outcome_uses)
        for outcome in result:
            observed_uses = {
                getattr(item, "value", str(item))
                for item in getattr(outcome, "permitted_uses", frozenset())
            }
            if not observed_uses.issubset(declared_uses):
                raise PermissionError(
                    f"outcome resolver {node.component_id} emitted undeclared uses: "
                    f"{sorted(observed_uses - declared_uses)}"
                )
    elif kind == ComponentKind.ADAPTER:
        _require_outcome_use(node, value)
        state = component_state.setdefault(node.component_id, {})
        result = node.component.update(value, state, context)
    elif kind == ComponentKind.POLICY:
        _require_outcome_use(node, value)
        state = component_state.setdefault(node.component_id, {})
        result = node.component.decide(value, state, context)
    elif kind == ComponentKind.ACTUATOR:
        raise RuntimeError("actuator ingress must pass through the action broker")
    elif kind == ComponentKind.ARTIFACT:
        result = node.component.produce(value, context)
    elif kind == ComponentKind.SINK:
        node.component.append(value)
        return {}
    else:
        raise TypeError(f"component kind {kind.value} cannot consume graph input")
    if not node.descriptor.output_ports:
        if result is not None:
            raise TypeError(
                f"component {node.component_id} returned output without a declared port"
            )
        return {}
    if len(node.descriptor.output_ports) != 1:
        raise TypeError(
            f"component {node.component_id} requires process() for multiple output ports"
        )
    return normalize_outputs(node, {node.descriptor.output_ports[0].name: result})


def _require_outcome_use(node: RuntimeNode, value: Any) -> None:
    required_use = node.planned.required_outcome_use
    if required_use is None:
        return
    permitted_uses = {
        getattr(item, "value", str(item))
        for item in getattr(value, "permitted_uses", frozenset())
    }
    if required_use not in permitted_uses:
        raise PermissionError(
            f"component {node.component_id} requires outcome use {required_use}"
        )


def normalize_outputs(
    node: RuntimeNode,
    raw: Mapping[str, Any],
) -> Mapping[str, tuple[Any, ...]]:
    declared = {value.name: value for value in node.descriptor.output_ports}
    unknown = set(raw) - set(declared)
    if unknown:
        raise ValueError(
            f"component {node.component_id} returned undeclared ports: {sorted(unknown)}"
        )
    normalized: dict[str, tuple[Any, ...]] = {}
    for port_name, value in raw.items():
        if value is None:
            normalized[port_name] = ()
            continue
        port = declared[port_name]
        if port.multiple and is_output_iterable(value):
            values = tuple(value)
        else:
            values = (value,)
        for item in values:
            observed = value_type(item)
            if observed != port.type_id:
                raise TypeError(
                    f"component {node.component_id}.{port_name} emitted {observed}; "
                    f"expected {port.type_id}"
                )
        normalized[port_name] = values
    return normalized


def validate_input_port(node: RuntimeNode, port_name: str, value: Any) -> None:
    port = next(
        (
            value_port
            for value_port in node.descriptor.input_ports
            if value_port.name == port_name
        ),
        None,
    )
    if port is None:
        raise ValueError(f"component {node.component_id} has no input port {port_name}")
    observed = value_type(value)
    if observed != port.type_id:
        raise TypeError(
            f"component {node.component_id}.{port_name} received {observed}; "
            f"expected {port.type_id}"
        )


def value_payload(value: Any) -> Any:
    to_payload = getattr(value, "to_payload", None)
    return to_payload() if callable(to_payload) else thaw_json(freeze_json(value))


def value_type(value: Any) -> str:
    schema = getattr(value, "schema", None)
    if schema is None:
        payload = value_payload(value)
        if isinstance(payload, Mapping):
            schema = payload.get("schema")
    if not isinstance(schema, str):
        raise TypeError(f"value {type(value).__name__} does not expose a schema identity")
    return require_identifier(schema, "value schema")


def value_id(value: Any) -> str:
    for field_name in (
        "batch_id",
        "event_id",
        "window_id",
        "request_id",
        "authorization_request_id",
        "cancellation_id",
        "decision_id",
        "prediction_id",
        "outcome_id",
        "transition_id",
        "command_id",
        "receipt_id",
        "publication_id",
        "record_id",
    ):
        observed = getattr(value, field_name, None)
        if isinstance(observed, str):
            return require_identifier(observed, "value_id")
    return f"value.{canonical_hash(value_payload(value))[-32:]}"


def available_time(value: Any, fallback: TimePoint) -> TimePoint:
    if isinstance(value, (DenseSampleBatch, SparseEventBatch, MetadataEvent)):
        return packet_available_time(value)
    for field_name in (
        "available_time",
        "decided_time",
        "transition_time",
        "observed_time",
        "emitted_time",
    ):
        observed = getattr(value, field_name, None)
        if isinstance(observed, TimePoint):
            return observed
    return fallback


def sequence_key(value: Any) -> int:
    return int(getattr(value, "sequence_start", 0))


def is_output_iterable(value: Any) -> bool:
    if callable(getattr(value, "to_payload", None)):
        return False
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping))
