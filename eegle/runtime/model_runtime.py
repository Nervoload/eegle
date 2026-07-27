"""Plan-owned runtime boundary for model results and canonical predictions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from eegle._domain import Lineage
from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash
from eegle.compiler.plan import PlannedModelBinding
from eegle.models.contracts import ModelOutputContract
from eegle.models.predictions import PREDICTION_RECORD_SCHEMA, Prediction
from eegle.models.results import ModelResult
from eegle.plugins.registry import StateBehavior
from eegle.runtime.context import RuntimeExecutionContext
from eegle.runtime.plan_runtime import RuntimeNode
from eegle.streams.clocks import TimePoint


MODEL_RESULT_DISPOSITION_SCHEMA = "eegle.model_result_disposition.v1"
MODEL_COMPARISON_SCHEMA = "eegle.model_comparison.v1"


class ModelResultDispositionStatus(str, Enum):
    EMITTED = "emitted"
    REJECTED = "rejected"
    LATE = "late"
    CANCELLED = "cancelled"


class ModelComparisonStatus(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class ModelComparison:
    comparison_id: str
    group_id: str
    output_port: str
    status: ModelComparisonStatus
    compared_time: TimePoint
    member_components: tuple[str, ...]
    prediction_ids: Mapping[str, str]
    result_digests: Mapping[str, str]
    input_ids: tuple[str, ...]
    admitted_input_ids: tuple[str, ...]
    missing_members: tuple[str, ...] = ()
    outputs_equal: bool | None = None
    reason_code: str | None = None
    schema: str = MODEL_COMPARISON_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEL_COMPARISON_SCHEMA:
            raise ValueError(f"unsupported model comparison schema: {self.schema}")
        for field_name in ("comparison_id", "group_id", "output_port"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "status", ModelComparisonStatus(self.status))
        if not isinstance(self.compared_time, TimePoint):
            raise TypeError("model comparison compared_time must be a TimePoint")
        members = tuple(
            require_identifier(value, "comparison member")
            for value in self.member_components
        )
        if len(members) < 2 or len(members) != len(set(members)):
            raise ValueError("model comparison requires at least two unique members")
        object.__setattr__(self, "member_components", members)
        predictions = {
            require_identifier(str(key), "comparison component"): require_identifier(
                str(value), "prediction_id"
            )
            for key, value in self.prediction_ids.items()
        }
        results = {
            require_identifier(str(key), "comparison component"): require_digest(
                str(value), "result_digest"
            )
            for key, value in self.result_digests.items()
        }
        if set(predictions) != set(results) or not set(predictions).issubset(members):
            raise ValueError("comparison prediction and result members must match")
        object.__setattr__(self, "prediction_ids", freeze_json(predictions))
        object.__setattr__(self, "result_digests", freeze_json(results))
        object.__setattr__(
            self,
            "input_ids",
            tuple(require_identifier(value, "input_id") for value in self.input_ids),
        )
        object.__setattr__(
            self,
            "admitted_input_ids",
            tuple(
                require_identifier(value, "admitted_input_id")
                for value in self.admitted_input_ids
            ),
        )
        if not self.input_ids or not self.admitted_input_ids:
            raise ValueError("model comparison requires input and admitted-input identity")
        missing = tuple(
            require_identifier(value, "missing comparison member")
            for value in self.missing_members
        )
        if len(missing) != len(set(missing)) or not set(missing).issubset(members):
            raise ValueError("missing comparison members must be unique planned members")
        object.__setattr__(self, "missing_members", missing)
        if self.outputs_equal is not None and not isinstance(self.outputs_equal, bool):
            raise TypeError("model comparison outputs_equal must be boolean or null")
        if self.status == ModelComparisonStatus.COMPLETE:
            if set(predictions) != set(members) or missing or self.outputs_equal is None:
                raise ValueError("complete comparison requires every member and equality result")
            if self.reason_code is not None:
                raise ValueError("complete comparison cannot declare a failure reason")
        else:
            if not missing or self.outputs_equal is not None or self.reason_code is None:
                raise ValueError("incomplete comparison requires missing members and reason")
            if set(missing) != set(members) - set(predictions):
                raise ValueError("incomplete comparison must name every missing member")
            object.__setattr__(
                self,
                "reason_code",
                require_identifier(self.reason_code, "reason_code"),
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "comparison_id": self.comparison_id,
            "group_id": self.group_id,
            "output_port": self.output_port,
            "status": self.status.value,
            "compared_time": self.compared_time.to_payload(),
            "member_components": list(self.member_components),
            "prediction_ids": thaw_json(self.prediction_ids),
            "result_digests": thaw_json(self.result_digests),
            "input_ids": list(self.input_ids),
            "admitted_input_ids": list(self.admitted_input_ids),
            "missing_members": list(self.missing_members),
            "outputs_equal": self.outputs_equal,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelComparison":
        return cls(
            schema=str(payload.get("schema", MODEL_COMPARISON_SCHEMA)),
            comparison_id=str(payload["comparison_id"]),
            group_id=str(payload["group_id"]),
            output_port=str(payload["output_port"]),
            status=ModelComparisonStatus(str(payload["status"])),
            compared_time=TimePoint.from_payload(payload["compared_time"]),
            member_components=tuple(str(value) for value in payload["member_components"]),
            prediction_ids={
                str(key): str(value)
                for key, value in dict(payload.get("prediction_ids") or {}).items()
            },
            result_digests={
                str(key): str(value)
                for key, value in dict(payload.get("result_digests") or {}).items()
            },
            input_ids=tuple(str(value) for value in payload.get("input_ids", ())),
            admitted_input_ids=tuple(
                str(value) for value in payload.get("admitted_input_ids", ())
            ),
            missing_members=tuple(
                str(value) for value in payload.get("missing_members", ())
            ),
            outputs_equal=payload.get("outputs_equal"),
            reason_code=None
            if payload.get("reason_code") is None
            else str(payload["reason_code"]),
        )


@dataclass(frozen=True, slots=True)
class ModelResultDisposition:
    disposition_id: str
    component_id: str
    output_port: str
    status: ModelResultDispositionStatus
    decided_time: TimePoint
    result_digest: str | None = None
    prediction_id: str | None = None
    reason_code: str | None = None
    schema: str = MODEL_RESULT_DISPOSITION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEL_RESULT_DISPOSITION_SCHEMA:
            raise ValueError(
                f"unsupported model result disposition schema: {self.schema}"
            )
        for field_name in ("disposition_id", "component_id", "output_port"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "status", ModelResultDispositionStatus(self.status))
        if self.result_digest is not None:
            object.__setattr__(
                self,
                "result_digest",
                require_digest(self.result_digest, "result_digest"),
            )
        if self.prediction_id is not None:
            object.__setattr__(
                self,
                "prediction_id",
                require_identifier(self.prediction_id, "prediction_id"),
            )
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                require_identifier(self.reason_code, "reason_code"),
            )
        if self.status == ModelResultDispositionStatus.EMITTED:
            if self.prediction_id is None or self.result_digest is None:
                raise ValueError("emitted model results require result and prediction identity")
        elif self.reason_code is None:
            raise ValueError("non-emitted model results require reason_code")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "disposition_id": self.disposition_id,
            "component_id": self.component_id,
            "output_port": self.output_port,
            "status": self.status.value,
            "decided_time": self.decided_time.to_payload(),
            "result_digest": self.result_digest,
            "prediction_id": self.prediction_id,
            "reason_code": self.reason_code,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelResultDisposition":
        return cls(
            schema=str(payload.get("schema", MODEL_RESULT_DISPOSITION_SCHEMA)),
            disposition_id=str(payload["disposition_id"]),
            component_id=str(payload["component_id"]),
            output_port=str(payload["output_port"]),
            status=ModelResultDispositionStatus(str(payload["status"])),
            decided_time=TimePoint.from_payload(payload["decided_time"]),
            result_digest=None
            if payload.get("result_digest") is None
            else str(payload["result_digest"]),
            prediction_id=None
            if payload.get("prediction_id") is None
            else str(payload["prediction_id"]),
            reason_code=None
            if payload.get("reason_code") is None
            else str(payload["reason_code"]),
        )


class ModelResultRejected(ValueError):
    """A plugin returned a value that cannot cross the compiled model boundary."""

    def __init__(
        self,
        message: str,
        *,
        output_port: str,
        reason_code: str,
        result_digest: str | None = None,
    ) -> None:
        super().__init__(message)
        self.output_port = output_port
        self.reason_code = reason_code
        self.result_digest = result_digest


def execute_bound_model(
    node: RuntimeNode,
    input_port: str,
    value: Any,
    context: RuntimeExecutionContext,
    *,
    input_id: str,
    admitted_input_ids: tuple[str, ...],
    input_available_time: TimePoint,
) -> Mapping[str, tuple[Prediction, ...]]:
    """Validate plugin results and add the immutable plan-owned envelope."""

    binding = node.model_binding
    if binding is None:
        raise TypeError(f"model component {node.component_id} lacks a planned binding")
    state_digest = _state_digest(node, binding)
    raw = _invoke_model(node, input_port, value, context)
    output_contracts = {
        contract.port_name: contract for contract in binding.manifest.contract.outputs
    }
    declared_ports = {port.name: port for port in node.descriptor.output_ports}
    unknown = set(raw) - set(declared_ports)
    if unknown:
        raise ModelResultRejected(
            f"model {node.component_id} returned undeclared ports: {sorted(unknown)}",
            output_port=sorted(unknown)[0],
            reason_code="undeclared_output_port",
        )
    input_ids = (require_identifier(input_id, "model input_id"),)
    admitted_input_ids = tuple(
        require_identifier(item, "admitted_input_id") for item in admitted_input_ids
    )
    if not admitted_input_ids:
        raise ValueError("bound model dispatch requires admitted input lineage")
    clock_revisions, stream_revisions = _input_revisions(value)
    clock_revisions = _merge_revisions(
        clock_revisions,
        context.clock_mapping_revisions,
        field="clock mapping",
    )
    predictions: dict[str, tuple[Prediction, ...]] = {}
    for output_port, raw_value in raw.items():
        port = declared_ports[output_port]
        contract = output_contracts.get(output_port)
        if contract is None:
            raise ModelResultRejected(
                f"model output {output_port} has no compiled scientific contract",
                output_port=output_port,
                reason_code="missing_output_contract",
            )
        if port.type_id != PREDICTION_RECORD_SCHEMA or contract.type_id != port.type_id:
            raise ModelResultRejected(
                f"model output {output_port} is not a canonical prediction port",
                output_port=output_port,
                reason_code="noncanonical_output_type",
            )
        values = _result_values(raw_value, multiple=port.multiple)
        wrapped: list[Prediction] = []
        for result in values:
            if not isinstance(result, ModelResult):
                raise ModelResultRejected(
                    f"model {node.component_id}.{output_port} returned "
                    f"{type(result).__name__}; expected ModelResult",
                    output_port=output_port,
                    reason_code="invalid_result_type",
                )
            _validate_result(result, contract, node.component_id, output_port)
            produced = TimePoint(
                context.current_time.seconds + result.completion_delay_seconds,
                context.current_time.clock_id,
            )
            lineage = Lineage(
                component_id=node.component_id,
                component_version=node.planned.plugin_version,
                input_ids=input_ids,
                state_hash=state_digest,
                latest_input_available_time=input_available_time,
                clock_mapping_revisions=clock_revisions,
                stream_revisions=stream_revisions,
            )
            wrapped.append(
                Prediction(
                    prediction_id=context.next_id("prediction"),
                    component_id=node.component_id,
                    plugin_id=binding.plugin_id,
                    plugin_version=binding.plugin_version,
                    model_id=binding.manifest.model_id,
                    model_version=binding.manifest.model_version,
                    manifest_digest=binding.manifest_digest,
                    contract_digest=binding.contract_digest,
                    result_digest=result.result_hash,
                    role_id=binding.role.role_id,
                    role_profile=binding.role.profile,
                    comparison_group=binding.comparison_group,
                    output_port=output_port,
                    value=result.value,
                    uncertainty=result.uncertainty,
                    validity=result.validity,
                    abstained=result.abstained,
                    abstention_reason=result.abstention_reason,
                    produced_time=produced,
                    available_time=produced,
                    input_ids=input_ids,
                    admitted_input_ids=admitted_input_ids,
                    lineage=lineage,
                    artifact_digests={
                        artifact.artifact_id: artifact.digest
                        for artifact in binding.artifacts
                    },
                )
            )
        predictions[output_port] = tuple(wrapped)
    return predictions


def _invoke_model(
    node: RuntimeNode,
    input_port: str,
    value: Any,
    context: RuntimeExecutionContext,
) -> Mapping[str, Any]:
    process = getattr(node.component, "process", None)
    if callable(process):
        raw = process(input_port, value, context)
        if not isinstance(raw, Mapping):
            raise ModelResultRejected(
                "model process() must return an output-port mapping",
                output_port=node.descriptor.output_ports[0].name,
                reason_code="invalid_output_mapping",
            )
        return raw
    if len(node.descriptor.output_ports) != 1:
        raise ModelResultRejected(
            f"model {node.component_id} requires process() for multiple output ports",
            output_port=node.descriptor.output_ports[0].name,
            reason_code="multiple_ports_require_process",
        )
    return {node.descriptor.output_ports[0].name: node.component.predict(value, context)}


def _state_digest(node: RuntimeNode, binding: PlannedModelBinding) -> str | None:
    behavior = node.descriptor.capabilities.state_behavior
    if behavior == StateBehavior.STATELESS:
        return None
    if behavior == StateBehavior.SNAPSHOT_RESTORE:
        snapshot = node.component.snapshot_state()
        # Freeze first so mutable or non-JSON state cannot become evidence.
        return canonical_hash(thaw_json(freeze_json(snapshot)))
    if binding.manifest.contract.state.state_affects_predictions:
        raise ModelResultRejected(
            "prediction-affecting external state requires an attestable state digest",
            output_port=binding.manifest.contract.outputs[0].port_name,
            reason_code="unattested_external_state",
        )
    return None


def _input_revisions(
    value: Any,
) -> tuple[Mapping[str, int], Mapping[str, int]]:
    lineage = getattr(value, "lineage", None)
    if isinstance(lineage, Lineage):
        return lineage.clock_mapping_revisions, lineage.stream_revisions
    return {}, {}


def _merge_revisions(
    first: Mapping[str, int],
    second: Mapping[str, int],
    *,
    field: str,
) -> dict[str, int]:
    merged = {str(key): int(value) for key, value in first.items()}
    for key, value in second.items():
        normalized = str(key)
        revision = int(value)
        if normalized in merged and merged[normalized] != revision:
            raise ValueError(f"{field} revision conflict for {normalized}")
        merged[normalized] = revision
    return merged


def _result_values(value: Any, *, multiple: bool) -> tuple[Any, ...]:
    if multiple and isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _validate_result(
    result: ModelResult,
    contract: ModelOutputContract,
    component_id: str,
    output_port: str,
) -> None:
    _validate_schema_value(
        result.value,
        contract.value_schema,
        component_id,
        output_port,
        "value",
        result.result_hash,
    )
    if result.uncertainty is not None:
        if contract.uncertainty_schema is None:
            raise ModelResultRejected(
                f"model {component_id}.{output_port} returned undeclared uncertainty",
                output_port=output_port,
                reason_code="undeclared_uncertainty",
                result_digest=result.result_hash,
            )
        _validate_schema_value(
            result.uncertainty,
            contract.uncertainty_schema,
            component_id,
            output_port,
            "uncertainty",
            result.result_hash,
        )
    if result.validity is not None:
        if contract.validity_schema is None:
            raise ModelResultRejected(
                f"model {component_id}.{output_port} returned undeclared validity",
                output_port=output_port,
                reason_code="undeclared_validity",
                result_digest=result.result_hash,
            )
        _validate_schema_value(
            result.validity,
            contract.validity_schema,
            component_id,
            output_port,
            "validity",
            result.result_hash,
        )
    if result.abstained and not contract.abstention_supported:
        raise ModelResultRejected(
            f"model {component_id}.{output_port} abstained without contract permission",
            output_port=output_port,
            reason_code="undeclared_abstention",
            result_digest=result.result_hash,
        )


def _validate_schema_value(
    value: Any,
    schema: Mapping[str, Any],
    component_id: str,
    output_port: str,
    field: str,
    result_digest: str,
) -> None:
    errors = sorted(
        Draft202012Validator(thaw_json(schema)).iter_errors(thaw_json(value)),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if not errors:
        return
    error = errors[0]
    path = ".".join(str(part) for part in error.absolute_path) or "$"
    raise ModelResultRejected(
        f"model {component_id}.{output_port} {field} failed its contract at "
        f"{path}: {error.message}",
        output_port=output_port,
        reason_code=f"invalid_{field}",
        result_digest=result_digest,
    )
