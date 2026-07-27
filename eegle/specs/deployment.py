"""Site-local deployment bindings for portable suites."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit

from eegle._validation import (
    freeze_json,
    require_digest,
    require_finite,
    require_identifier,
    thaw_json,
)
from eegle.compiler.lock import canonical_hash
from eegle.specs.schemas import validate_payload
from eegle.specs.suite import SignalContract


DEPLOYMENT_SPEC_SCHEMA = "eegle.deployment_spec.v1"
_JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
_SECRET_KEYS = {
    "api_key",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
}


class Placement(str, Enum):
    IN_PROCESS = "in_process"
    SUBPROCESS_PROXY = "subprocess_proxy"
    EXTERNAL_PROXY = "external_proxy"


class ClockMappingStrategy(str, Enum):
    IDENTITY = "identity"
    DECLARED_AFFINE = "declared_affine"
    ONLINE_ESTIMATED = "online_estimated"
    RECORDED_REPLAY = "recorded_replay"
    POSTHOC = "posthoc"


class AuthorizationFailureDisposition(str, Enum):
    OBSERVE_ONLY = "observe_only"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    resource_id: str
    kind: str
    selector: Mapping[str, Any]
    capabilities: tuple[str, ...] = ()
    contract: SignalContract | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "resource_id", require_identifier(self.resource_id, "resource_id")
        )
        object.__setattr__(self, "kind", require_identifier(self.kind, "resource kind"))
        object.__setattr__(self, "selector", freeze_json(self.selector))
        capabilities = tuple(
            require_identifier(value, "resource capability") for value in self.capabilities
        )
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("resource capabilities must be unique")
        object.__setattr__(self, "capabilities", capabilities)

    def to_payload(self) -> dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "kind": self.kind,
            "selector": thaw_json(self.selector),
            "capabilities": list(self.capabilities),
            "contract": None if self.contract is None else self.contract.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ResourceSpec":
        return cls(
            resource_id=str(payload["resource_id"]),
            kind=str(payload["kind"]),
            selector=dict(payload["selector"]),
            capabilities=tuple(str(value) for value in payload.get("capabilities", ())),
            contract=None
            if payload.get("contract") is None
            else SignalContract.from_payload(payload["contract"]),
        )


@dataclass(frozen=True, slots=True)
class StreamBinding:
    stream_id: str
    resource_id: str
    selector: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(self, "stream_id", require_identifier(self.stream_id, "stream_id"))
        object.__setattr__(
            self, "resource_id", require_identifier(self.resource_id, "resource_id")
        )
        object.__setattr__(self, "selector", freeze_json(self.selector or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "resource_id": self.resource_id,
            "selector": thaw_json(self.selector),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StreamBinding":
        return cls(
            stream_id=str(payload["stream_id"]),
            resource_id=str(payload["resource_id"]),
            selector=dict(payload.get("selector") or {}),
        )


@dataclass(frozen=True, slots=True)
class ComponentBindingSpec:
    component_id: str
    plugin_id: str | None = None
    version_spec: str | None = None
    config: Mapping[str, Any] = None  # type: ignore[assignment]
    placement: Placement = Placement.IN_PROCESS
    endpoint_id: str | None = None
    resource_ids: tuple[str, ...] = ()
    secret_refs: Mapping[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component_id", require_identifier(self.component_id, "component_id")
        )
        if self.plugin_id is not None:
            object.__setattr__(
                self, "plugin_id", require_identifier(self.plugin_id, "plugin_id")
            )
        if self.version_spec is not None and not self.version_spec.strip():
            raise ValueError("version_spec cannot be empty")
        object.__setattr__(self, "config", freeze_json(self.config or {}))
        object.__setattr__(self, "placement", Placement(self.placement))
        if self.placement == Placement.IN_PROCESS and self.endpoint_id is not None:
            raise ValueError("in-process component cannot declare endpoint_id")
        if self.placement != Placement.IN_PROCESS and self.endpoint_id is None:
            raise ValueError("proxy component requires endpoint_id")
        if self.endpoint_id is not None:
            object.__setattr__(
                self, "endpoint_id", require_identifier(self.endpoint_id, "endpoint_id")
            )
        resources = tuple(require_identifier(value, "resource_id") for value in self.resource_ids)
        if len(resources) != len(set(resources)):
            raise ValueError("component resource identities must be unique")
        object.__setattr__(self, "resource_ids", resources)
        refs = {
            require_identifier(str(alias), "secret alias"): require_identifier(
                str(secret_id), "secret_id"
            )
            for alias, secret_id in (self.secret_refs or {}).items()
        }
        object.__setattr__(self, "secret_refs", freeze_json(refs))

    def to_payload(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "plugin_id": self.plugin_id,
            "version_spec": self.version_spec,
            "config": thaw_json(self.config),
            "placement": self.placement.value,
            "endpoint_id": self.endpoint_id,
            "resource_ids": list(self.resource_ids),
            "secret_refs": thaw_json(self.secret_refs),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ComponentBindingSpec":
        return cls(
            component_id=str(payload["component_id"]),
            plugin_id=None if payload.get("plugin_id") is None else str(payload["plugin_id"]),
            version_spec=None
            if payload.get("version_spec") is None
            else str(payload["version_spec"]),
            config=dict(payload.get("config") or {}),
            placement=Placement(str(payload.get("placement", "in_process"))),
            endpoint_id=None
            if payload.get("endpoint_id") is None
            else str(payload["endpoint_id"]),
            resource_ids=tuple(str(value) for value in payload.get("resource_ids", ())),
            secret_refs={
                str(key): str(value)
                for key, value in dict(payload.get("secret_refs") or {}).items()
            },
        )


@dataclass(frozen=True, slots=True)
class ModelArtifactBindingSpec:
    """Site-local materialization of one manifest-declared logical artifact."""

    manifest_digest: str
    artifact_id: str
    uri: str
    digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "manifest_digest",
            require_digest(self.manifest_digest, "manifest_digest"),
        )
        object.__setattr__(
            self, "artifact_id", require_identifier(self.artifact_id, "artifact_id")
        )
        object.__setattr__(self, "digest", require_digest(self.digest))
        _validate_site_uri(self.uri, "model artifact uri")

    def to_payload(self) -> dict[str, Any]:
        return {
            "manifest_digest": self.manifest_digest,
            "artifact_id": self.artifact_id,
            "uri": self.uri,
            "digest": self.digest,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelArtifactBindingSpec":
        return cls(
            manifest_digest=str(payload["manifest_digest"]),
            artifact_id=str(payload["artifact_id"]),
            uri=str(payload["uri"]),
            digest=str(payload["digest"]),
        )


@dataclass(frozen=True, slots=True)
class StorageBinding:
    storage_id: str
    kind: str
    uri: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "storage_id", require_identifier(self.storage_id, "storage_id"))
        object.__setattr__(self, "kind", require_identifier(self.kind, "storage kind"))
        _validate_site_uri(self.uri, "storage uri")

    def to_payload(self) -> dict[str, Any]:
        return {"storage_id": self.storage_id, "kind": self.kind, "uri": self.uri}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StorageBinding":
        return cls(
            storage_id=str(payload["storage_id"]),
            kind=str(payload["kind"]),
            uri=str(payload["uri"]),
        )


@dataclass(frozen=True, slots=True)
class SecretReference:
    secret_id: str
    provider: str
    key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "secret_id", require_identifier(self.secret_id, "secret_id"))
        object.__setattr__(self, "provider", require_identifier(self.provider, "secret provider"))
        if not self.key.strip():
            raise ValueError("secret reference key cannot be empty")

    def to_payload(self) -> dict[str, Any]:
        return {"secret_id": self.secret_id, "provider": self.provider, "key": self.key}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SecretReference":
        return cls(
            secret_id=str(payload["secret_id"]),
            provider=str(payload["provider"]),
            key=str(payload["key"]),
        )


@dataclass(frozen=True, slots=True)
class ActionParameterConstraint:
    minimum: float | None = None
    maximum: float | None = None
    allowed_values: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        minimum = (
            None if self.minimum is None else require_finite(self.minimum, "minimum")
        )
        maximum = (
            None if self.maximum is None else require_finite(self.maximum, "maximum")
        )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("action parameter minimum cannot exceed maximum")
        values = tuple(freeze_json(value) for value in self.allowed_values)
        value_hashes = tuple(canonical_hash(value) for value in values)
        if len(value_hashes) != len(set(value_hashes)):
            raise ValueError("allowed action parameter values must be unique")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)
        object.__setattr__(self, "allowed_values", values)

    def to_payload(self) -> dict[str, Any]:
        return {
            "minimum": self.minimum,
            "maximum": self.maximum,
            "allowed_values": [thaw_json(value) for value in self.allowed_values],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ActionParameterConstraint":
        return cls(
            minimum=None if payload.get("minimum") is None else float(payload["minimum"]),
            maximum=None if payload.get("maximum") is None else float(payload["maximum"]),
            allowed_values=tuple(payload.get("allowed_values", ())),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationProviderBindingSpec:
    provider_id: str
    plugin_id: str
    version_spec: str | None = None
    config: Mapping[str, Any] = None  # type: ignore[assignment]
    failure_disposition: AuthorizationFailureDisposition = (
        AuthorizationFailureDisposition.OBSERVE_ONLY
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", require_identifier(self.provider_id, "provider_id"))
        object.__setattr__(self, "plugin_id", require_identifier(self.plugin_id, "plugin_id"))
        if self.version_spec is not None and not self.version_spec.strip():
            raise ValueError("authorization provider version_spec cannot be empty")
        object.__setattr__(self, "config", freeze_json(self.config or {}))
        object.__setattr__(
            self,
            "failure_disposition",
            AuthorizationFailureDisposition(self.failure_disposition),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "plugin_id": self.plugin_id,
            "version_spec": self.version_spec,
            "config": thaw_json(self.config),
            "failure_disposition": self.failure_disposition.value,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AuthorizationProviderBindingSpec":
        return cls(
            provider_id=str(payload["provider_id"]),
            plugin_id=str(payload["plugin_id"]),
            version_spec=None
            if payload.get("version_spec") is None
            else str(payload["version_spec"]),
            config=dict(payload.get("config") or {}),
            failure_disposition=AuthorizationFailureDisposition(
                str(payload.get("failure_disposition", "observe_only"))
            ),
        )


@dataclass(frozen=True, slots=True)
class PermissionGrant:
    permission_id: str
    capability: str
    component_ids: tuple[str, ...]
    authorization_ref: str
    operator_confirmation: bool = False
    parameter_constraints: Mapping[str, ActionParameterConstraint] = None  # type: ignore[assignment]
    allow_unlisted_parameters: bool = False
    maximum_decision_delay_seconds: float | None = None
    maximum_delivery_delay_seconds: float | None = None
    maximum_request_ttl_seconds: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "permission_id", require_identifier(self.permission_id, "permission_id")
        )
        object.__setattr__(
            self, "capability", require_identifier(self.capability, "permission capability")
        )
        components = tuple(
            require_identifier(value, "permission component") for value in self.component_ids
        )
        if not components:
            raise ValueError("permission grant requires at least one component")
        if len(components) != len(set(components)):
            raise ValueError("permission component identities must be unique")
        object.__setattr__(self, "component_ids", components)
        object.__setattr__(
            self,
            "authorization_ref",
            require_identifier(self.authorization_ref, "authorization_ref"),
        )
        constraints = {
            require_identifier(str(key), "action parameter"): (
                value
                if isinstance(value, ActionParameterConstraint)
                else ActionParameterConstraint.from_payload(value)
            )
            for key, value in (self.parameter_constraints or {}).items()
        }
        object.__setattr__(self, "parameter_constraints", MappingProxyType(constraints))
        for field in (
            "maximum_decision_delay_seconds",
            "maximum_delivery_delay_seconds",
            "maximum_request_ttl_seconds",
        ):
            value = getattr(self, field)
            if value is not None:
                normalized = require_finite(value, field)
                if normalized < 0:
                    raise ValueError(f"{field} cannot be negative")
                object.__setattr__(self, field, normalized)

    def to_payload(self) -> dict[str, Any]:
        return {
            "permission_id": self.permission_id,
            "capability": self.capability,
            "component_ids": list(self.component_ids),
            "authorization_ref": self.authorization_ref,
            "operator_confirmation": self.operator_confirmation,
            "parameter_constraints": {
                key: value.to_payload()
                for key, value in self.parameter_constraints.items()
            },
            "allow_unlisted_parameters": self.allow_unlisted_parameters,
            "maximum_decision_delay_seconds": self.maximum_decision_delay_seconds,
            "maximum_delivery_delay_seconds": self.maximum_delivery_delay_seconds,
            "maximum_request_ttl_seconds": self.maximum_request_ttl_seconds,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PermissionGrant":
        return cls(
            permission_id=str(payload["permission_id"]),
            capability=str(payload["capability"]),
            component_ids=tuple(str(value) for value in payload["component_ids"]),
            authorization_ref=str(payload["authorization_ref"]),
            operator_confirmation=bool(payload.get("operator_confirmation", False)),
            parameter_constraints={
                str(key): ActionParameterConstraint.from_payload(value)
                for key, value in dict(payload.get("parameter_constraints") or {}).items()
            },
            allow_unlisted_parameters=bool(payload.get("allow_unlisted_parameters", False)),
            maximum_decision_delay_seconds=None
            if payload.get("maximum_decision_delay_seconds") is None
            else float(payload["maximum_decision_delay_seconds"]),
            maximum_delivery_delay_seconds=None
            if payload.get("maximum_delivery_delay_seconds") is None
            else float(payload["maximum_delivery_delay_seconds"]),
            maximum_request_ttl_seconds=None
            if payload.get("maximum_request_ttl_seconds") is None
            else float(payload["maximum_request_ttl_seconds"]),
        )


@dataclass(frozen=True, slots=True)
class ClockMappingBinding:
    source_clock: str
    target_clock: str
    strategy: ClockMappingStrategy
    maximum_uncertainty_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_clock", require_identifier(self.source_clock, "source_clock")
        )
        object.__setattr__(
            self, "target_clock", require_identifier(self.target_clock, "target_clock")
        )
        object.__setattr__(self, "strategy", ClockMappingStrategy(self.strategy))
        uncertainty = require_finite(
            self.maximum_uncertainty_seconds, "maximum_uncertainty_seconds"
        )
        if uncertainty < 0:
            raise ValueError("maximum_uncertainty_seconds cannot be negative")
        object.__setattr__(self, "maximum_uncertainty_seconds", uncertainty)

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_clock": self.source_clock,
            "target_clock": self.target_clock,
            "strategy": self.strategy.value,
            "maximum_uncertainty_seconds": self.maximum_uncertainty_seconds,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ClockMappingBinding":
        return cls(
            source_clock=str(payload["source_clock"]),
            target_clock=str(payload["target_clock"]),
            strategy=ClockMappingStrategy(str(payload["strategy"])),
            maximum_uncertainty_seconds=float(payload["maximum_uncertainty_seconds"]),
        )


@dataclass(frozen=True, slots=True)
class DeploymentSpec:
    deployment_id: str
    suite_id: str
    component_bindings: tuple[ComponentBindingSpec, ...]
    resources: tuple[ResourceSpec, ...] = ()
    stream_bindings: tuple[StreamBinding, ...] = ()
    storage: tuple[StorageBinding, ...] = ()
    permissions: tuple[PermissionGrant, ...] = ()
    authorization_providers: tuple[AuthorizationProviderBindingSpec, ...] = ()
    secrets: tuple[SecretReference, ...] = ()
    clock_mappings: tuple[ClockMappingBinding, ...] = ()
    model_artifacts: tuple[ModelArtifactBindingSpec, ...] = ()
    schema: str = DEPLOYMENT_SPEC_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != DEPLOYMENT_SPEC_SCHEMA:
            raise ValueError(f"unsupported deployment schema: {self.schema}")
        object.__setattr__(
            self, "deployment_id", require_identifier(self.deployment_id, "deployment_id")
        )
        object.__setattr__(self, "suite_id", require_identifier(self.suite_id, "suite_id"))
        _require_unique(
            (value.component_id for value in self.component_bindings), "component binding"
        )
        _require_unique((value.resource_id for value in self.resources), "resource")
        _require_unique((value.stream_id for value in self.stream_bindings), "stream binding")
        _require_unique((value.storage_id for value in self.storage), "storage")
        _require_unique((value.permission_id for value in self.permissions), "permission")
        _require_unique(
            (value.provider_id for value in self.authorization_providers),
            "authorization provider",
        )
        _require_unique((value.secret_id for value in self.secrets), "secret reference")
        _require_unique(
            (
                (value.manifest_digest, value.artifact_id)
                for value in self.model_artifacts
            ),
            "model artifact binding",
        )
        _require_unique(
            (
                (value.source_clock, value.target_clock)
                for value in self.clock_mappings
            ),
            "clock mapping",
        )
        payload = self.to_payload()
        _reject_secret_literals(payload, path="$")

    @property
    def spec_hash(self) -> str:
        return canonical_hash(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "deployment_id": self.deployment_id,
            "suite_id": self.suite_id,
            "component_bindings": [value.to_payload() for value in self.component_bindings],
            "resources": [value.to_payload() for value in self.resources],
            "stream_bindings": [value.to_payload() for value in self.stream_bindings],
            "storage": [value.to_payload() for value in self.storage],
            "permissions": [value.to_payload() for value in self.permissions],
            "authorization_providers": [
                value.to_payload() for value in self.authorization_providers
            ],
            "secrets": [value.to_payload() for value in self.secrets],
            "clock_mappings": [value.to_payload() for value in self.clock_mappings],
            "model_artifacts": [value.to_payload() for value in self.model_artifacts],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DeploymentSpec":
        validate_payload(payload, DEPLOYMENT_JSON_SCHEMA)
        return cls(
            schema=str(payload.get("schema", DEPLOYMENT_SPEC_SCHEMA)),
            deployment_id=str(payload["deployment_id"]),
            suite_id=str(payload["suite_id"]),
            component_bindings=tuple(
                ComponentBindingSpec.from_payload(value)
                for value in payload.get("component_bindings", ())
            ),
            resources=tuple(ResourceSpec.from_payload(value) for value in payload.get("resources", ())),
            stream_bindings=tuple(
                StreamBinding.from_payload(value)
                for value in payload.get("stream_bindings", ())
            ),
            storage=tuple(StorageBinding.from_payload(value) for value in payload.get("storage", ())),
            permissions=tuple(
                PermissionGrant.from_payload(value) for value in payload.get("permissions", ())
            ),
            authorization_providers=tuple(
                AuthorizationProviderBindingSpec.from_payload(value)
                for value in payload.get("authorization_providers", ())
            ),
            secrets=tuple(
                SecretReference.from_payload(value) for value in payload.get("secrets", ())
            ),
            clock_mappings=tuple(
                ClockMappingBinding.from_payload(value)
                for value in payload.get("clock_mappings", ())
            ),
            model_artifacts=tuple(
                ModelArtifactBindingSpec.from_payload(value)
                for value in payload.get("model_artifacts", ())
            ),
        )

    @classmethod
    def load(cls, path: str | Path) -> "DeploymentSpec":
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, Mapping):
            raise TypeError(f"{path} must contain a JSON object")
        return cls.from_payload(value)


def _reject_secret_literals(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SECRET_KEYS:
                raise ValueError(f"{path}.{key}: literal secret fields are forbidden")
            _reject_secret_literals(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_secret_literals(item, path=f"{path}[{index}]")


def _validate_site_uri(uri: str, field: str) -> None:
    parsed = urlsplit(uri)
    if not parsed.scheme:
        raise ValueError(f"{field} must be explicit and include a URI scheme")
    if len(parsed.scheme) == 1 and len(uri) >= 3 and uri[1] == ":":
        raise ValueError(f"{field} cannot be an implicit Windows drive path")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} cannot contain credentials")


def _require_unique(values: Any, label: str) -> None:
    materialized = tuple(values)
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{label} identities must be unique")


_RESOURCE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["resource_id", "kind", "selector"],
    "properties": {
        "resource_id": {"type": "string"},
        "kind": {"type": "string"},
        "selector": {"type": "object"},
        "capabilities": {"type": "array", "items": {"type": "string"}},
        "contract": {
            "type": ["object", "null"],
            "properties": {
                "type_id": {"type": "string"},
                "unit": {"type": "string"},
                "channel_count": {"type": "integer", "minimum": 1},
                "minimum_channels": {"type": "integer", "minimum": 1},
                "maximum_channels": {"type": "integer", "minimum": 1},
                "nominal_rate_hz": {"type": "number", "exclusiveMinimum": 0},
                "minimum_rate_hz": {"type": "number", "exclusiveMinimum": 0},
                "maximum_rate_hz": {"type": "number", "exclusiveMinimum": 0},
                "window_samples": {"type": "integer", "minimum": 1},
                "minimum_window_samples": {"type": "integer", "minimum": 1},
                "content_kind": {"type": "string", "minLength": 1},
                "rate_model": {"type": "string", "minLength": 1},
                "channel_ids": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "required_channel_ids": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "feature_ids": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "required_feature_ids": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "units": {
                    "type": "object",
                    "additionalProperties": {"type": "string", "minLength": 1},
                },
                "event_kinds": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "required_event_kinds": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "missing_data_policy": {"type": "string", "minLength": 1},
                "layout": {"type": "string", "minLength": 1},
                "window_duration_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
                "minimum_duration_seconds": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
            },
            "required": ["type_id"],
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}
_COMPONENT_BINDING_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["component_id"],
    "properties": {
        "component_id": {"type": "string"},
        "plugin_id": {"type": ["string", "null"]},
        "version_spec": {"type": ["string", "null"]},
        "config": {"type": "object"},
        "placement": {"enum": [value.value for value in Placement]},
        "endpoint_id": {"type": ["string", "null"]},
        "resource_ids": {"type": "array", "items": {"type": "string"}},
        "secret_refs": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "additionalProperties": False,
}
_ACTION_PARAMETER_CONSTRAINT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "minimum": {"type": ["number", "null"]},
        "maximum": {"type": ["number", "null"]},
        "allowed_values": {"type": "array"},
    },
    "additionalProperties": False,
}
_AUTHORIZATION_PROVIDER_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "required": ["provider_id", "plugin_id"],
    "properties": {
        "provider_id": {"type": "string", "minLength": 1},
        "plugin_id": {"type": "string", "minLength": 1},
        "version_spec": {"type": ["string", "null"]},
        "config": {"type": "object"},
        "failure_disposition": {
            "enum": [value.value for value in AuthorizationFailureDisposition]
        },
    },
    "additionalProperties": False,
}
DEPLOYMENT_JSON_SCHEMA: Mapping[str, Any] = {
    "$schema": _JSON_SCHEMA,
    "type": "object",
    "required": ["schema", "deployment_id", "suite_id", "component_bindings"],
    "properties": {
        "schema": {"const": DEPLOYMENT_SPEC_SCHEMA},
        "deployment_id": {"type": "string", "minLength": 1},
        "suite_id": {"type": "string", "minLength": 1},
        "component_bindings": {"type": "array", "items": _COMPONENT_BINDING_SCHEMA},
        "resources": {"type": "array", "items": _RESOURCE_SCHEMA},
        "stream_bindings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["stream_id", "resource_id"],
                "properties": {
                    "stream_id": {"type": "string"},
                    "resource_id": {"type": "string"},
                    "selector": {"type": "object"},
                },
                "additionalProperties": False,
            },
        },
        "storage": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["storage_id", "kind", "uri"],
                "properties": {
                    "storage_id": {"type": "string"},
                    "kind": {"type": "string"},
                    "uri": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
        },
        "permissions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["permission_id", "capability", "component_ids", "authorization_ref"],
                "properties": {
                    "permission_id": {"type": "string"},
                    "capability": {"type": "string"},
                    "component_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    "authorization_ref": {"type": "string"},
                    "operator_confirmation": {"type": "boolean"},
                    "parameter_constraints": {
                        "type": "object",
                        "additionalProperties": _ACTION_PARAMETER_CONSTRAINT_SCHEMA,
                    },
                    "allow_unlisted_parameters": {"type": "boolean"},
                    "maximum_decision_delay_seconds": {
                        "type": ["number", "null"],
                        "minimum": 0,
                    },
                    "maximum_delivery_delay_seconds": {
                        "type": ["number", "null"],
                        "minimum": 0,
                    },
                    "maximum_request_ttl_seconds": {
                        "type": ["number", "null"],
                        "minimum": 0,
                    },
                },
                "additionalProperties": False,
            },
        },
        "authorization_providers": {
            "type": "array",
            "items": _AUTHORIZATION_PROVIDER_SCHEMA,
        },
        "secrets": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["secret_id", "provider", "key"],
                "properties": {
                    "secret_id": {"type": "string"},
                    "provider": {"type": "string"},
                    "key": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
        },
        "clock_mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source_clock", "target_clock", "strategy", "maximum_uncertainty_seconds"],
                "properties": {
                    "source_clock": {"type": "string"},
                    "target_clock": {"type": "string"},
                    "strategy": {"enum": [value.value for value in ClockMappingStrategy]},
                    "maximum_uncertainty_seconds": {"type": "number", "minimum": 0},
                },
                "additionalProperties": False,
            },
        },
        "model_artifacts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["manifest_digest", "artifact_id", "uri", "digest"],
                "properties": {
                    "manifest_digest": {"type": "string", "minLength": 1},
                    "artifact_id": {"type": "string", "minLength": 1},
                    "uri": {"type": "string", "minLength": 1},
                    "digest": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}
