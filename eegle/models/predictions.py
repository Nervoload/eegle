"""Plan-owned canonical predictions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._domain import Lineage
from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.streams.clocks import TimePoint


PREDICTION_RECORD_SCHEMA = "eegle.prediction.v2"


@dataclass(frozen=True, slots=True)
class Prediction:
    """Canonical evidence envelope constructed only by EEGle's runtime boundary."""

    prediction_id: str
    component_id: str
    plugin_id: str
    plugin_version: str
    model_id: str
    model_version: str
    manifest_digest: str
    contract_digest: str
    result_digest: str
    role_id: str
    role_profile: str
    output_port: str
    value: Any
    produced_time: TimePoint
    available_time: TimePoint
    input_ids: tuple[str, ...]
    admitted_input_ids: tuple[str, ...]
    lineage: Lineage
    artifact_digests: Mapping[str, str]
    uncertainty: Any | None = None
    validity: Any | None = None
    abstained: bool = False
    abstention_reason: str | None = None
    comparison_group: str | None = None
    schema: str = PREDICTION_RECORD_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PREDICTION_RECORD_SCHEMA:
            raise ValueError(f"unsupported prediction schema: {self.schema}")
        for field_name in (
            "prediction_id",
            "component_id",
            "plugin_id",
            "model_id",
            "role_id",
            "role_profile",
            "output_port",
        ):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name),
            )
        if not self.plugin_version.strip() or not self.model_version.strip():
            raise ValueError("prediction plugin and model versions cannot be empty")
        object.__setattr__(
            self,
            "manifest_digest",
            require_digest(self.manifest_digest, "manifest_digest"),
        )
        object.__setattr__(
            self,
            "contract_digest",
            require_digest(self.contract_digest, "contract_digest"),
        )
        object.__setattr__(
            self,
            "result_digest",
            require_digest(self.result_digest, "result_digest"),
        )
        inputs = tuple(require_identifier(value, "input_id") for value in self.input_ids)
        admitted = tuple(
            require_identifier(value, "admitted_input_id")
            for value in self.admitted_input_ids
        )
        if not inputs or not admitted:
            raise ValueError("prediction requires model inputs and admitted inputs")
        object.__setattr__(self, "input_ids", inputs)
        object.__setattr__(self, "admitted_input_ids", admitted)
        if self.lineage.component_id != self.component_id:
            raise ValueError("prediction lineage component differs from the plan binding")
        if self.lineage.component_version != self.plugin_version:
            raise ValueError("prediction lineage version differs from the plan binding")
        if self.lineage.input_ids != inputs:
            raise ValueError("prediction input_ids must exactly match lineage input_ids")
        latest_input = self.lineage.latest_input_available_time
        if latest_input is None:
            raise ValueError("prediction lineage requires latest input availability")
        if self.produced_time.clock_id != self.available_time.clock_id:
            raise ValueError("prediction produced_time and available_time must share a clock")
        if self.available_time.seconds < self.produced_time.seconds:
            raise ValueError("prediction available_time cannot precede produced_time")
        if latest_input.clock_id != self.produced_time.clock_id:
            raise ValueError("prediction input availability must use the execution clock")
        if self.produced_time.seconds < latest_input.seconds:
            raise ValueError("prediction cannot be produced before its latest input was available")
        artifacts = {
            require_identifier(str(key), "artifact_id"): require_digest(
                str(value), "artifact_digest"
            )
            for key, value in self.artifact_digests.items()
        }
        object.__setattr__(self, "artifact_digests", freeze_json(artifacts))
        object.__setattr__(self, "value", freeze_json(self.value))
        if self.uncertainty is not None:
            object.__setattr__(self, "uncertainty", freeze_json(self.uncertainty))
        if self.validity is not None:
            object.__setattr__(self, "validity", freeze_json(self.validity))
        if self.abstained:
            if self.abstention_reason is None or not self.abstention_reason.strip():
                raise ValueError("an abstained prediction requires abstention_reason")
        elif self.abstention_reason is not None:
            raise ValueError("a non-abstained prediction cannot have abstention_reason")
        if self.comparison_group is not None:
            object.__setattr__(
                self,
                "comparison_group",
                require_identifier(self.comparison_group, "comparison_group"),
            )

    @property
    def outputs(self) -> Mapping[str, Any]:
        """Convenience view for policies consuming mapping-shaped output values."""

        if isinstance(self.value, Mapping):
            return self.value
        return {self.output_port: self.value}

    @property
    def state_digest(self) -> str | None:
        return self.lineage.state_hash

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "prediction_id": self.prediction_id,
            "component_id": self.component_id,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "manifest_digest": self.manifest_digest,
            "contract_digest": self.contract_digest,
            "result_digest": self.result_digest,
            "role_id": self.role_id,
            "role_profile": self.role_profile,
            "comparison_group": self.comparison_group,
            "output_port": self.output_port,
            "value": thaw_json(self.value),
            "uncertainty": None
            if self.uncertainty is None
            else thaw_json(self.uncertainty),
            "validity": None if self.validity is None else thaw_json(self.validity),
            "abstained": self.abstained,
            "abstention_reason": self.abstention_reason,
            "produced_time": self.produced_time.to_payload(),
            "available_time": self.available_time.to_payload(),
            "input_ids": list(self.input_ids),
            "admitted_input_ids": list(self.admitted_input_ids),
            "lineage": self.lineage.to_payload(),
            "artifact_digests": thaw_json(self.artifact_digests),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Prediction":
        return cls(
            schema=str(payload["schema"]),
            prediction_id=str(payload["prediction_id"]),
            component_id=str(payload["component_id"]),
            plugin_id=str(payload["plugin_id"]),
            plugin_version=str(payload["plugin_version"]),
            model_id=str(payload["model_id"]),
            model_version=str(payload["model_version"]),
            manifest_digest=str(payload["manifest_digest"]),
            contract_digest=str(payload["contract_digest"]),
            result_digest=str(payload["result_digest"]),
            role_id=str(payload["role_id"]),
            role_profile=str(payload["role_profile"]),
            comparison_group=None
            if payload.get("comparison_group") is None
            else str(payload["comparison_group"]),
            output_port=str(payload["output_port"]),
            value=payload.get("value"),
            uncertainty=payload.get("uncertainty"),
            validity=payload.get("validity"),
            abstained=bool(payload.get("abstained", False)),
            abstention_reason=None
            if payload.get("abstention_reason") is None
            else str(payload["abstention_reason"]),
            produced_time=TimePoint.from_payload(payload["produced_time"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            input_ids=tuple(str(value) for value in payload["input_ids"]),
            admitted_input_ids=tuple(
                str(value) for value in payload["admitted_input_ids"]
            ),
            lineage=Lineage.from_payload(payload["lineage"]),
            artifact_digests={
                str(key): str(value)
                for key, value in dict(payload.get("artifact_digests") or {}).items()
            },
        )
