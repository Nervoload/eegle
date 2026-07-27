"""Modality-neutral scientific contracts for model artifacts.

These records describe what a model artifact consumes, produces, and owns.
Executable construction and runtime capabilities belong to ``PluginDescriptor``;
placement and local resources belong to deployment; exact joins belong to the
compiled plan.  Keeping those authorities separate prevents model manifests
from becoming another plugin or deployment configuration system.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from eegle._domain import EquivalenceLevel, ExecutionMode
from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.compiler.lock import canonical_hash


MODEL_CONTRACT_SCHEMA = "eegle.model_contract.v1"
MODEL_INPUT_CONTRACT_SCHEMA = "eegle.model_input_contract.v1"
MODEL_OUTPUT_CONTRACT_SCHEMA = "eegle.model_output_contract.v1"
MODEL_STATE_CONTRACT_SCHEMA = "eegle.model_state_contract.v1"
PREPROCESSING_REQUIREMENT_SCHEMA = "eegle.preprocessing_requirement.v1"


def _validate_json_schema(schema: Mapping[str, Any], field_name: str) -> None:
    try:
        Draft202012Validator.check_schema(thaw_json(schema))
    except SchemaError as exc:
        raise ValueError(f"invalid {field_name}: {exc.message}") from exc


class PreprocessingOwnership(str, Enum):
    """The sole owner of one preprocessing operation or prohibition."""

    UPSTREAM = "upstream"
    MODEL_INTERNAL = "model_internal"
    ARTIFACT_PREPARED = "artifact_prepared"
    FORBIDDEN = "forbidden"


class ModelStateBehavior(str, Enum):
    STATELESS = "stateless"
    SNAPSHOT_RESTORE = "snapshot_restore"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class PreprocessingRequirement:
    requirement_id: str
    operation: str
    ownership: PreprocessingOwnership
    parameters: Mapping[str, Any] = field(default_factory=dict)
    required_lineage: tuple[str, ...] = ()
    schema: str = PREPROCESSING_REQUIREMENT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PREPROCESSING_REQUIREMENT_SCHEMA:
            raise ValueError(f"unsupported preprocessing requirement schema: {self.schema}")
        object.__setattr__(
            self,
            "requirement_id",
            require_identifier(self.requirement_id, "requirement_id"),
        )
        object.__setattr__(self, "operation", require_identifier(self.operation, "operation"))
        object.__setattr__(self, "ownership", PreprocessingOwnership(self.ownership))
        lineage = tuple(
            require_identifier(value, "required_lineage") for value in self.required_lineage
        )
        if len(lineage) != len(set(lineage)):
            raise ValueError("preprocessing required_lineage entries must be unique")
        object.__setattr__(self, "required_lineage", lineage)
        object.__setattr__(self, "parameters", freeze_json(self.parameters or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "requirement_id": self.requirement_id,
            "operation": self.operation,
            "ownership": self.ownership.value,
            "parameters": thaw_json(self.parameters),
            "required_lineage": list(self.required_lineage),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PreprocessingRequirement":
        return cls(
            schema=str(payload.get("schema", PREPROCESSING_REQUIREMENT_SCHEMA)),
            requirement_id=str(payload["requirement_id"]),
            operation=str(payload["operation"]),
            ownership=PreprocessingOwnership(str(payload["ownership"])),
            parameters=dict(payload.get("parameters") or {}),
            required_lineage=tuple(str(value) for value in payload.get("required_lineage", ())),
        )


@dataclass(frozen=True, slots=True)
class ModelInputContract:
    """Scientific requirements for one declared plugin input port.

    ``requirements`` is canonical structured data interpreted by compiler
    semantic passes. It can describe channel or feature identities, units, rate
    models, window support, missingness, or other schema-specific constraints
    without assuming that every input is a dense EEG epoch.
    """

    port_name: str
    type_id: str
    requirements: Mapping[str, Any] = field(default_factory=dict)
    preprocessing: tuple[PreprocessingRequirement, ...] = ()
    schema: str = MODEL_INPUT_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEL_INPUT_CONTRACT_SCHEMA:
            raise ValueError(f"unsupported model input contract schema: {self.schema}")
        object.__setattr__(self, "port_name", require_identifier(self.port_name, "port_name"))
        object.__setattr__(self, "type_id", require_identifier(self.type_id, "type_id"))
        requirement_ids = tuple(value.requirement_id for value in self.preprocessing)
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("preprocessing requirement identities must be unique per input")
        object.__setattr__(self, "requirements", freeze_json(self.requirements or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "port_name": self.port_name,
            "type_id": self.type_id,
            "requirements": thaw_json(self.requirements),
            "preprocessing": [value.to_payload() for value in self.preprocessing],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelInputContract":
        return cls(
            schema=str(payload.get("schema", MODEL_INPUT_CONTRACT_SCHEMA)),
            port_name=str(payload["port_name"]),
            type_id=str(payload["type_id"]),
            requirements=dict(payload.get("requirements") or {}),
            preprocessing=tuple(
                PreprocessingRequirement.from_payload(value)
                for value in payload.get("preprocessing", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelOutputContract:
    """Schema-bound model output without assuming a learning-problem taxonomy."""

    port_name: str
    type_id: str
    value_schema: Mapping[str, Any]
    uncertainty_schema: Mapping[str, Any] | None = None
    validity_schema: Mapping[str, Any] | None = None
    abstention_supported: bool = False
    schema: str = MODEL_OUTPUT_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEL_OUTPUT_CONTRACT_SCHEMA:
            raise ValueError(f"unsupported model output contract schema: {self.schema}")
        object.__setattr__(self, "port_name", require_identifier(self.port_name, "port_name"))
        object.__setattr__(self, "type_id", require_identifier(self.type_id, "type_id"))
        value_schema = freeze_json(self.value_schema)
        _validate_json_schema(value_schema, "model value schema")
        object.__setattr__(self, "value_schema", value_schema)
        if self.uncertainty_schema is not None:
            uncertainty_schema = freeze_json(self.uncertainty_schema)
            _validate_json_schema(uncertainty_schema, "model uncertainty schema")
            object.__setattr__(
                self,
                "uncertainty_schema",
                uncertainty_schema,
            )
        if self.validity_schema is not None:
            validity_schema = freeze_json(self.validity_schema)
            _validate_json_schema(validity_schema, "model validity schema")
            object.__setattr__(self, "validity_schema", validity_schema)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "port_name": self.port_name,
            "type_id": self.type_id,
            "value_schema": thaw_json(self.value_schema),
            "uncertainty_schema": None
            if self.uncertainty_schema is None
            else thaw_json(self.uncertainty_schema),
            "validity_schema": None
            if self.validity_schema is None
            else thaw_json(self.validity_schema),
            "abstention_supported": self.abstention_supported,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelOutputContract":
        uncertainty = payload.get("uncertainty_schema")
        validity = payload.get("validity_schema")
        return cls(
            schema=str(payload.get("schema", MODEL_OUTPUT_CONTRACT_SCHEMA)),
            port_name=str(payload["port_name"]),
            type_id=str(payload["type_id"]),
            value_schema=dict(payload.get("value_schema") or {}),
            uncertainty_schema=None if uncertainty is None else dict(uncertainty),
            validity_schema=None if validity is None else dict(validity),
            abstention_supported=bool(payload.get("abstention_supported", False)),
        )


@dataclass(frozen=True, slots=True)
class ModelStateContract:
    behavior: ModelStateBehavior = ModelStateBehavior.STATELESS
    state_schema_id: str | None = None
    initial_state_required: bool = False
    adaptation_supported: bool = False
    state_affects_predictions: bool = False
    replay_equivalence: EquivalenceLevel = EquivalenceLevel.BITWISE
    schema: str = MODEL_STATE_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEL_STATE_CONTRACT_SCHEMA:
            raise ValueError(f"unsupported model state contract schema: {self.schema}")
        object.__setattr__(self, "behavior", ModelStateBehavior(self.behavior))
        object.__setattr__(
            self,
            "replay_equivalence",
            EquivalenceLevel(self.replay_equivalence),
        )
        if self.state_schema_id is not None:
            object.__setattr__(
                self,
                "state_schema_id",
                require_identifier(self.state_schema_id, "state_schema_id"),
            )
        if self.behavior == ModelStateBehavior.STATELESS:
            if self.state_schema_id is not None:
                raise ValueError("stateless model state cannot declare state_schema_id")
            if self.initial_state_required or self.adaptation_supported or self.state_affects_predictions:
                raise ValueError("stateless model state cannot require or mutate prediction state")
        elif self.state_schema_id is None:
            raise ValueError("stateful model state requires state_schema_id")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "behavior": self.behavior.value,
            "state_schema_id": self.state_schema_id,
            "initial_state_required": self.initial_state_required,
            "adaptation_supported": self.adaptation_supported,
            "state_affects_predictions": self.state_affects_predictions,
            "replay_equivalence": self.replay_equivalence.value,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelStateContract":
        return cls(
            schema=str(payload.get("schema", MODEL_STATE_CONTRACT_SCHEMA)),
            behavior=ModelStateBehavior(str(payload.get("behavior", "stateless"))),
            state_schema_id=None
            if payload.get("state_schema_id") is None
            else str(payload["state_schema_id"]),
            initial_state_required=bool(payload.get("initial_state_required", False)),
            adaptation_supported=bool(payload.get("adaptation_supported", False)),
            state_affects_predictions=bool(payload.get("state_affects_predictions", False)),
            replay_equivalence=EquivalenceLevel(
                str(payload.get("replay_equivalence", EquivalenceLevel.BITWISE.value))
            ),
        )


@dataclass(frozen=True, slots=True)
class ModelContract:
    inputs: tuple[ModelInputContract, ...]
    outputs: tuple[ModelOutputContract, ...]
    state: ModelStateContract = ModelStateContract()
    supported_modes: frozenset[ExecutionMode] = frozenset({ExecutionMode.CAUSAL})
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema: str = MODEL_CONTRACT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != MODEL_CONTRACT_SCHEMA:
            raise ValueError(f"unsupported model contract schema: {self.schema}")
        if not self.inputs:
            raise ValueError("model contract requires at least one input")
        if not self.outputs:
            raise ValueError("model contract requires at least one output")
        for values, label in ((self.inputs, "input"), (self.outputs, "output")):
            names = tuple(value.port_name for value in values)
            if len(names) != len(set(names)):
                raise ValueError(f"model {label} port names must be unique")
        modes = frozenset(ExecutionMode(value) for value in self.supported_modes)
        if not modes:
            raise ValueError("model contract requires at least one execution mode")
        object.__setattr__(self, "supported_modes", modes)
        object.__setattr__(self, "metadata", freeze_json(self.metadata or {}))

    @property
    def contract_digest(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "inputs": [value.to_payload() for value in self.inputs],
            "outputs": [value.to_payload() for value in self.outputs],
            "state": self.state.to_payload(),
            "supported_modes": sorted(value.value for value in self.supported_modes),
            "metadata": thaw_json(self.metadata),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["contract_digest"] = self.contract_digest
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelContract":
        value = cls(
            schema=str(payload.get("schema", MODEL_CONTRACT_SCHEMA)),
            inputs=tuple(ModelInputContract.from_payload(item) for item in payload["inputs"]),
            outputs=tuple(
                ModelOutputContract.from_payload(item) for item in payload["outputs"]
            ),
            state=ModelStateContract.from_payload(payload.get("state") or {}),
            supported_modes=frozenset(
                ExecutionMode(str(item)) for item in payload.get("supported_modes", ("causal",))
            ),
            metadata=dict(payload.get("metadata") or {}),
        )
        expected = payload.get("contract_digest")
        if expected is None:
            raise ValueError("model contract payload requires contract_digest")
        if expected != value.contract_digest:
            raise ValueError("model contract digest mismatch")
        return value
