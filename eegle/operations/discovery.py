"""Reproducible capability detection and review-only deployment proposals.

Detection records evidence about an environment.  Proposal generation may use
that evidence to construct a site-local ``DeploymentSpec``, but it never edits
portable protocol or suite values and it never treats provider presence as an
authorization grant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from eegle._domain import ComponentKind, ExecutionMode
from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.authoring import (
    AuthoredExperiment,
    AuthoringOrigin,
    ComposedExperiment,
    DeploymentRequirement,
    DeploymentRequirementKind,
    SourceKind,
    SourceLocation,
)
from eegle.compiler import canonical_hash
from eegle.compiler.graph import contract_issues
from eegle.models import ModelManifest
from eegle.operations.contracts import (
    ExitCode,
    OperationCategory,
    OperationDiagnostic,
    OperationError,
)
from eegle.plugins import PluginDescriptor, PluginRegistry
from eegle.specs import (
    AuthorizationProviderBindingSpec,
    ClockMappingBinding,
    ComponentBindingSpec,
    DeploymentSpec,
    ModelArtifactBindingSpec,
    PermissionGrant,
    Placement,
    ResourceSpec,
    SecretReference,
    SignalContract,
    StorageBinding,
    StreamBinding,
)
from eegle.streams import ContentKind, StreamSpec

DETECTION_REPORT_SCHEMA_ID = "eegle.detection_report.v1"
DEPLOYMENT_PROPOSAL_SCHEMA_ID = "eegle.deployment_proposal.v1"
MODEL_MANIFEST_ENTRY_POINT_GROUP = "eegle.models"

_CREDENTIAL_KEYS = {
    "api_key",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
}


@dataclass(frozen=True, slots=True)
class PluginObservation:
    descriptor: Mapping[str, Any]
    descriptor_hash: str
    source: SourceLocation

    def __post_init__(self) -> None:
        value = freeze_json(self.descriptor)
        object.__setattr__(self, "descriptor", value)
        object.__setattr__(
            self, "descriptor_hash", require_digest(self.descriptor_hash, "descriptor_hash")
        )
        if canonical_hash(thaw_json(value)) != self.descriptor_hash:
            raise ValueError("plugin observation descriptor hash mismatch")
        _require_detection_source(self.source)
        require_identifier(str(value["plugin_id"]), "plugin_id")
        Version(str(value["version"]))

    @property
    def plugin_id(self) -> str:
        return str(self.descriptor["plugin_id"])

    @property
    def version(self) -> str:
        return str(self.descriptor["version"])

    @property
    def kind(self) -> ComponentKind:
        return ComponentKind(str(self.descriptor["kind"]))

    def to_payload(self) -> dict[str, Any]:
        return {
            "descriptor": thaw_json(self.descriptor),
            "descriptor_hash": self.descriptor_hash,
            "source": self.source.to_payload(),
        }

    @classmethod
    def from_descriptor(
        cls,
        descriptor: PluginDescriptor,
        *,
        source: SourceLocation | None = None,
    ) -> "PluginObservation":
        return cls(
            descriptor.to_payload(),
            descriptor.descriptor_hash,
            source
            or SourceLocation(
                SourceKind.DETECTION,
                locator=descriptor.distribution or "python-environment",
                symbol=f"{descriptor.plugin_id}@{descriptor.version}",
            ),
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PluginObservation":
        return cls(
            dict(payload["descriptor"]),
            str(payload["descriptor_hash"]),
            SourceLocation.from_payload(payload["source"]),
        )


@dataclass(frozen=True, slots=True)
class ModelObservation:
    manifest: ModelManifest
    source: SourceLocation

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, ModelManifest):
            raise TypeError("model observation requires a ModelManifest")
        _require_detection_source(self.source)

    @property
    def observation_id(self) -> str:
        return self.manifest.manifest_digest

    def to_payload(self) -> dict[str, Any]:
        return {"manifest": self.manifest.to_payload(), "source": self.source.to_payload()}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ModelObservation":
        return cls(
            ModelManifest.from_payload(payload["manifest"]),
            SourceLocation.from_payload(payload["source"]),
        )


@dataclass(frozen=True, slots=True)
class SourceCapability:
    capability_id: str
    resource_id: str
    resource_kind: str
    stream: StreamSpec
    contract: SignalContract
    plugin_id: str
    plugin_version: str
    selector: Mapping[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict)
    placement: Placement = Placement.IN_PROCESS
    endpoint_id: str | None = None
    required_secret_aliases: tuple[str, ...] = ()
    source: SourceLocation = field(
        default_factory=lambda: SourceLocation(SourceKind.DETECTION, locator="provided")
    )

    def __post_init__(self) -> None:
        for field_name in ("capability_id", "resource_id", "resource_kind", "plugin_id"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(str(getattr(self, field_name)), field_name),
            )
        Version(self.plugin_version)
        if not isinstance(self.stream, StreamSpec):
            raise TypeError("source capability requires a StreamSpec")
        if not isinstance(self.contract, SignalContract):
            raise TypeError("source capability requires a SignalContract")
        observed = signal_contract_from_stream(self.stream)
        if contract_issues(observed, self.contract):
            raise ValueError("source capability contract exceeds its observed StreamSpec")
        capabilities = tuple(
            sorted(require_identifier(value, "capability") for value in self.capabilities)
        )
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("source capabilities must be unique")
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "selector", freeze_json(self.selector))
        object.__setattr__(self, "config", freeze_json(self.config))
        _reject_credential_literals(self.selector, "$.selector")
        _reject_credential_literals(self.config, "$.config")
        object.__setattr__(self, "placement", Placement(self.placement))
        if self.placement == Placement.IN_PROCESS and self.endpoint_id is not None:
            raise ValueError("in-process source capability cannot declare endpoint_id")
        if self.placement != Placement.IN_PROCESS and self.endpoint_id is None:
            raise ValueError("proxy source capability requires endpoint_id")
        if self.endpoint_id is not None:
            object.__setattr__(
                self, "endpoint_id", require_identifier(self.endpoint_id, "endpoint_id")
            )
        aliases = tuple(
            sorted(
                require_identifier(value, "required secret alias")
                for value in self.required_secret_aliases
            )
        )
        if len(aliases) != len(set(aliases)):
            raise ValueError("required secret aliases must be unique")
        object.__setattr__(self, "required_secret_aliases", aliases)
        _require_detection_source(self.source)

    def resource(self) -> ResourceSpec:
        return ResourceSpec(
            self.resource_id,
            self.resource_kind,
            thaw_json(self.selector),
            self.capabilities,
            self.contract,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "resource_id": self.resource_id,
            "resource_kind": self.resource_kind,
            "stream": self.stream.to_payload(),
            "contract": self.contract.to_payload(),
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "selector": thaw_json(self.selector),
            "capabilities": list(self.capabilities),
            "config": thaw_json(self.config),
            "placement": self.placement.value,
            "endpoint_id": self.endpoint_id,
            "required_secret_aliases": list(self.required_secret_aliases),
            "source": self.source.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SourceCapability":
        return cls(
            capability_id=str(payload["capability_id"]),
            resource_id=str(payload["resource_id"]),
            resource_kind=str(payload["resource_kind"]),
            stream=StreamSpec.from_payload(payload["stream"]),
            contract=SignalContract.from_payload(payload["contract"]),
            plugin_id=str(payload["plugin_id"]),
            plugin_version=str(payload["plugin_version"]),
            selector=dict(payload.get("selector") or {}),
            capabilities=tuple(str(value) for value in payload.get("capabilities", ())),
            config=dict(payload.get("config") or {}),
            placement=Placement(str(payload.get("placement", Placement.IN_PROCESS.value))),
            endpoint_id=None
            if payload.get("endpoint_id") is None
            else str(payload["endpoint_id"]),
            required_secret_aliases=tuple(
                str(value) for value in payload.get("required_secret_aliases", ())
            ),
            source=SourceLocation.from_payload(payload["source"]),
        )


@dataclass(frozen=True, slots=True)
class StorageCapability:
    capability_id: str
    binding: StorageBinding
    capabilities: tuple[str, ...] = ()
    source: SourceLocation = field(
        default_factory=lambda: SourceLocation(SourceKind.DETECTION, locator="provided")
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capability_id", require_identifier(self.capability_id, "capability_id")
        )
        if not isinstance(self.binding, StorageBinding):
            raise TypeError("storage capability requires a StorageBinding")
        values = tuple(sorted(require_identifier(value, "capability") for value in self.capabilities))
        if len(values) != len(set(values)):
            raise ValueError("storage capabilities must be unique")
        object.__setattr__(self, "capabilities", values)
        _require_detection_source(self.source)

    def to_payload(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "binding": self.binding.to_payload(),
            "capabilities": list(self.capabilities),
            "source": self.source.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StorageCapability":
        return cls(
            str(payload["capability_id"]),
            StorageBinding.from_payload(payload["binding"]),
            tuple(str(value) for value in payload.get("capabilities", ())),
            SourceLocation.from_payload(payload["source"]),
        )


@dataclass(frozen=True, slots=True)
class ClockCapability:
    capability_id: str
    binding: ClockMappingBinding
    source: SourceLocation = field(
        default_factory=lambda: SourceLocation(SourceKind.DETECTION, locator="provided")
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capability_id", require_identifier(self.capability_id, "capability_id")
        )
        if not isinstance(self.binding, ClockMappingBinding):
            raise TypeError("clock capability requires a ClockMappingBinding")
        _require_detection_source(self.source)

    def to_payload(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "binding": self.binding.to_payload(),
            "source": self.source.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ClockCapability":
        return cls(
            str(payload["capability_id"]),
            ClockMappingBinding.from_payload(payload["binding"]),
            SourceLocation.from_payload(payload["source"]),
        )


@dataclass(frozen=True, slots=True)
class PlacementCapability:
    capability_id: str
    plugin_id: str
    plugin_version: str
    placement: Placement
    endpoint_id: str | None = None
    capabilities: tuple[str, ...] = ()
    required_secret_aliases: tuple[str, ...] = ()
    source: SourceLocation = field(
        default_factory=lambda: SourceLocation(SourceKind.DETECTION, locator="provided")
    )

    def __post_init__(self) -> None:
        for field_name in ("capability_id", "plugin_id"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(str(getattr(self, field_name)), field_name),
            )
        Version(self.plugin_version)
        object.__setattr__(self, "placement", Placement(self.placement))
        if self.placement == Placement.IN_PROCESS and self.endpoint_id is not None:
            raise ValueError("in-process placement cannot declare endpoint_id")
        if self.placement != Placement.IN_PROCESS and self.endpoint_id is None:
            raise ValueError("proxy placement requires endpoint_id")
        if self.endpoint_id is not None:
            object.__setattr__(
                self, "endpoint_id", require_identifier(self.endpoint_id, "endpoint_id")
            )
        for field_name, label in (
            ("capabilities", "capability"),
            ("required_secret_aliases", "required secret alias"),
        ):
            values = tuple(
                sorted(require_identifier(value, label) for value in getattr(self, field_name))
            )
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
            object.__setattr__(self, field_name, values)
        _require_detection_source(self.source)

    def to_payload(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "placement": self.placement.value,
            "endpoint_id": self.endpoint_id,
            "capabilities": list(self.capabilities),
            "required_secret_aliases": list(self.required_secret_aliases),
            "source": self.source.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PlacementCapability":
        return cls(
            capability_id=str(payload["capability_id"]),
            plugin_id=str(payload["plugin_id"]),
            plugin_version=str(payload["plugin_version"]),
            placement=Placement(str(payload["placement"])),
            endpoint_id=None
            if payload.get("endpoint_id") is None
            else str(payload["endpoint_id"]),
            capabilities=tuple(str(value) for value in payload.get("capabilities", ())),
            required_secret_aliases=tuple(
                str(value) for value in payload.get("required_secret_aliases", ())
            ),
            source=SourceLocation.from_payload(payload["source"]),
        )


@dataclass(frozen=True, slots=True)
class AuthorizationProviderCapability:
    capability_id: str
    provider_id: str
    plugin_id: str
    plugin_version: str
    capabilities: tuple[str, ...]
    config: Mapping[str, Any] = field(default_factory=dict)
    required_secret_aliases: tuple[str, ...] = ()
    source: SourceLocation = field(
        default_factory=lambda: SourceLocation(SourceKind.DETECTION, locator="provided")
    )

    def __post_init__(self) -> None:
        for field_name in ("capability_id", "provider_id", "plugin_id"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(str(getattr(self, field_name)), field_name),
            )
        Version(self.plugin_version)
        values = tuple(sorted(require_identifier(value, "capability") for value in self.capabilities))
        if not values:
            raise ValueError("authorization provider capability must declare capabilities")
        if len(values) != len(set(values)):
            raise ValueError("authorization capabilities must be unique")
        object.__setattr__(self, "capabilities", values)
        object.__setattr__(self, "config", freeze_json(self.config))
        _reject_credential_literals(self.config, "$.config")
        aliases = tuple(
            sorted(
                require_identifier(value, "required secret alias")
                for value in self.required_secret_aliases
            )
        )
        if len(aliases) != len(set(aliases)):
            raise ValueError("required secret aliases must be unique")
        object.__setattr__(self, "required_secret_aliases", aliases)
        _require_detection_source(self.source)

    def to_payload(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "provider_id": self.provider_id,
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "capabilities": list(self.capabilities),
            "config": thaw_json(self.config),
            "required_secret_aliases": list(self.required_secret_aliases),
            "source": self.source.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AuthorizationProviderCapability":
        return cls(
            capability_id=str(payload["capability_id"]),
            provider_id=str(payload["provider_id"]),
            plugin_id=str(payload["plugin_id"]),
            plugin_version=str(payload["plugin_version"]),
            capabilities=tuple(str(value) for value in payload.get("capabilities", ())),
            config=dict(payload.get("config") or {}),
            required_secret_aliases=tuple(
                str(value) for value in payload.get("required_secret_aliases", ())
            ),
            source=SourceLocation.from_payload(payload["source"]),
        )


@dataclass(frozen=True, slots=True)
class DetectionReport:
    detection_id: str
    observed_at: str
    plugins: tuple[PluginObservation, ...]
    models: tuple[ModelObservation, ...] = ()
    sources: tuple[SourceCapability, ...] = ()
    storage: tuple[StorageCapability, ...] = ()
    clocks: tuple[ClockCapability, ...] = ()
    placements: tuple[PlacementCapability, ...] = ()
    authorization_providers: tuple[AuthorizationProviderCapability, ...] = ()
    schema: str = DETECTION_REPORT_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != DETECTION_REPORT_SCHEMA_ID:
            raise ValueError(f"unsupported detection report schema: {self.schema}")
        object.__setattr__(
            self, "detection_id", require_identifier(self.detection_id, "detection_id")
        )
        _parse_utc(self.observed_at)
        orders = (
            ("plugins", lambda value: (value.plugin_id, Version(value.version))),
            ("models", lambda value: (value.manifest.model_id, Version(value.manifest.model_version))),
            ("sources", lambda value: value.capability_id),
            ("storage", lambda value: value.capability_id),
            ("clocks", lambda value: value.capability_id),
            ("placements", lambda value: value.capability_id),
            ("authorization_providers", lambda value: value.capability_id),
        )
        for field_name, key in orders:
            object.__setattr__(self, field_name, tuple(sorted(getattr(self, field_name), key=key)))
        _require_unique(
            (f"{value.plugin_id}@{value.version}" for value in self.plugins),
            "plugin observation",
        )
        _require_unique((value.observation_id for value in self.models), "model observation")
        _require_unique(
            (
                f"{value.manifest.model_id}@{value.manifest.model_version}"
                for value in self.models
            ),
            "model coordinate",
        )
        for field_name in (
            "sources",
            "storage",
            "clocks",
            "placements",
            "authorization_providers",
        ):
            _require_unique(
                (value.capability_id for value in getattr(self, field_name)),
                f"{field_name} capability",
            )

    @property
    def report_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "detection_id": self.detection_id,
            "observed_at": self.observed_at,
            "plugins": [value.to_payload() for value in self.plugins],
            "models": [value.to_payload() for value in self.models],
            "sources": [value.to_payload() for value in self.sources],
            "storage": [value.to_payload() for value in self.storage],
            "clocks": [value.to_payload() for value in self.clocks],
            "placements": [value.to_payload() for value in self.placements],
            "authorization_providers": [
                value.to_payload() for value in self.authorization_providers
            ],
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["report_hash"] = self.report_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DetectionReport":
        report = cls(
            schema=str(payload["schema"]),
            detection_id=str(payload["detection_id"]),
            observed_at=str(payload["observed_at"]),
            plugins=tuple(PluginObservation.from_payload(value) for value in payload.get("plugins", ())),
            models=tuple(ModelObservation.from_payload(value) for value in payload.get("models", ())),
            sources=tuple(SourceCapability.from_payload(value) for value in payload.get("sources", ())),
            storage=tuple(StorageCapability.from_payload(value) for value in payload.get("storage", ())),
            clocks=tuple(ClockCapability.from_payload(value) for value in payload.get("clocks", ())),
            placements=tuple(PlacementCapability.from_payload(value) for value in payload.get("placements", ())),
            authorization_providers=tuple(
                AuthorizationProviderCapability.from_payload(value)
                for value in payload.get("authorization_providers", ())
            ),
        )
        if payload.get("report_hash") != report.report_hash:
            raise ValueError("detection report hash mismatch")
        return report

    @classmethod
    def load(cls, path: str | Path) -> "DetectionReport":
        return cls.from_payload(_read_object(path))


@dataclass(frozen=True, slots=True)
class DeploymentSelection:
    source_capabilities: Mapping[str, str] = field(default_factory=dict)
    storage_capabilities: Mapping[str, str] = field(default_factory=dict)
    clock_capabilities: Mapping[str, str] = field(default_factory=dict)
    placement_capabilities: Mapping[str, str] = field(default_factory=dict)
    component_plugins: Mapping[str, str] = field(default_factory=dict)
    component_secret_refs: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    secrets: tuple[SecretReference, ...] = ()
    model_artifacts: tuple[ModelArtifactBindingSpec, ...] = ()
    authorization_providers: tuple[AuthorizationProviderBindingSpec, ...] = ()
    permissions: tuple[PermissionGrant, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "source_capabilities",
            "storage_capabilities",
            "clock_capabilities",
            "placement_capabilities",
            "component_plugins",
        ):
            values = {
                require_identifier(str(key), f"{field_name} key"): require_identifier(
                    str(value), f"{field_name} value"
                )
                for key, value in getattr(self, field_name).items()
            }
            object.__setattr__(self, field_name, MappingProxyType(values))
        refs: dict[str, Mapping[str, str]] = {}
        for component_id, aliases in self.component_secret_refs.items():
            refs[require_identifier(str(component_id), "component_id")] = MappingProxyType(
                {
                    require_identifier(str(alias), "secret alias"): require_identifier(
                        str(secret_id), "secret_id"
                    )
                    for alias, secret_id in aliases.items()
                }
            )
        object.__setattr__(self, "component_secret_refs", MappingProxyType(refs))
        for field_name, identity in (
            ("secrets", lambda value: value.secret_id),
            (
                "model_artifacts",
                lambda value: f"{value.manifest_digest}:{value.artifact_id}",
            ),
            ("authorization_providers", lambda value: value.provider_id),
            ("permissions", lambda value: value.permission_id),
        ):
            values = tuple(getattr(self, field_name))
            _require_unique((identity(value) for value in values), field_name)
            object.__setattr__(self, field_name, values)


@dataclass(frozen=True, slots=True)
class DeploymentDecision:
    target: str
    observation_id: str | None
    origin: AuthoringOrigin
    explicit_selection: bool
    source: SourceLocation
    requirement_id: str | None = None

    def __post_init__(self) -> None:
        if not self.target.startswith("$"):
            raise ValueError("deployment decision target must be a canonical path")
        if self.observation_id is not None:
            object.__setattr__(
                self,
                "observation_id",
                require_identifier(self.observation_id, "observation_id"),
            )
        object.__setattr__(self, "origin", AuthoringOrigin(self.origin))
        if self.requirement_id is not None:
            object.__setattr__(
                self,
                "requirement_id",
                require_identifier(self.requirement_id, "requirement_id"),
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "observation_id": self.observation_id,
            "origin": self.origin.value,
            "explicit_selection": self.explicit_selection,
            "source": self.source.to_payload(),
            "requirement_id": self.requirement_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DeploymentDecision":
        return cls(
            target=str(payload["target"]),
            observation_id=None
            if payload.get("observation_id") is None
            else str(payload["observation_id"]),
            origin=AuthoringOrigin(str(payload["origin"])),
            explicit_selection=bool(payload["explicit_selection"]),
            source=SourceLocation.from_payload(payload["source"]),
            requirement_id=None
            if payload.get("requirement_id") is None
            else str(payload["requirement_id"]),
        )


@dataclass(frozen=True, slots=True)
class DeploymentProposal:
    proposal_id: str
    suite_hash: str
    requirements_hash: str
    detection_report_hash: str
    deployment: DeploymentSpec
    decisions: tuple[DeploymentDecision, ...]
    authorization_inferred: bool = False
    schema: str = DEPLOYMENT_PROPOSAL_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != DEPLOYMENT_PROPOSAL_SCHEMA_ID:
            raise ValueError(f"unsupported deployment proposal schema: {self.schema}")
        object.__setattr__(
            self, "proposal_id", require_identifier(self.proposal_id, "proposal_id")
        )
        for field_name in ("suite_hash", "requirements_hash", "detection_report_hash"):
            object.__setattr__(
                self,
                field_name,
                require_digest(getattr(self, field_name), field_name),
            )
        if not isinstance(self.deployment, DeploymentSpec):
            raise TypeError("deployment proposal requires a DeploymentSpec")
        decisions = tuple(sorted(self.decisions, key=lambda value: value.target))
        _require_unique((value.target for value in decisions), "deployment decision target")
        object.__setattr__(self, "decisions", decisions)
        if self.authorization_inferred:
            raise ValueError("deployment proposals cannot infer authorization")

    @property
    def proposal_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "proposal_id": self.proposal_id,
            "suite_hash": self.suite_hash,
            "requirements_hash": self.requirements_hash,
            "detection_report_hash": self.detection_report_hash,
            "deployment": self.deployment.to_payload(),
            "decisions": [value.to_payload() for value in self.decisions],
            "authorization_inferred": False,
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["proposal_hash"] = self.proposal_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DeploymentProposal":
        proposal = cls(
            schema=str(payload["schema"]),
            proposal_id=str(payload["proposal_id"]),
            suite_hash=str(payload["suite_hash"]),
            requirements_hash=str(payload["requirements_hash"]),
            detection_report_hash=str(payload["detection_report_hash"]),
            deployment=DeploymentSpec.from_payload(payload["deployment"]),
            decisions=tuple(
                DeploymentDecision.from_payload(value)
                for value in payload.get("decisions", ())
            ),
            authorization_inferred=bool(payload.get("authorization_inferred", False)),
        )
        if payload.get("proposal_hash") != proposal.proposal_hash:
            raise ValueError("deployment proposal hash mismatch")
        return proposal


def detect_capabilities(
    *,
    registry: PluginRegistry | None = None,
    include_entry_points: bool = True,
    models: Iterable[ModelObservation | ModelManifest] = (),
    sources: Iterable[SourceCapability] = (),
    storage: Iterable[StorageCapability] = (),
    clocks: Iterable[ClockCapability] = (),
    placements: Iterable[PlacementCapability] = (),
    authorization_providers: Iterable[AuthorizationProviderCapability] = (),
    detection_id: str | None = None,
    observed_at: str | None = None,
) -> DetectionReport:
    """Inspect installed plugins and combine independently supplied detectors.

    Hardware transports provide the typed observations; the base package does
    not probe optional transports or reinterpret their vendor metadata.
    """

    captured = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    _parse_utc(captured)
    identifier = detection_id or (
        "detection." + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    discovered = registry or PluginRegistry()
    if registry is None:
        discovered.register_builtins()
    if include_entry_points:
        try:
            discovered.load_entry_points()
        except Exception as exc:
            raise OperationError(
                "detect",
                ExitCode.UNAVAILABLE,
                (
                    OperationDiagnostic(
                        "detection.plugin_load_failed",
                        OperationCategory.AVAILABILITY,
                        "Installed plugin discovery failed",
                        str(exc),
                    ),
                ),
            ) from exc
    plugin_values = tuple(PluginObservation.from_descriptor(value) for value in discovered.descriptors())
    provided_models = tuple(
        value
        if isinstance(value, ModelObservation)
        else ModelObservation(
            value,
            SourceLocation(
                SourceKind.DETECTION,
                locator="provided-model-manifest",
                symbol=value.manifest_digest,
            ),
        )
        for value in models
    )
    installed_models: tuple[ModelObservation, ...] = ()
    if include_entry_points:
        try:
            installed_models = _load_model_entry_points()
        except Exception as exc:
            raise OperationError(
                "detect",
                ExitCode.UNAVAILABLE,
                (
                    OperationDiagnostic(
                        "detection.model_load_failed",
                        OperationCategory.AVAILABILITY,
                        "Installed model discovery failed",
                        str(exc),
                    ),
                ),
            ) from exc
    model_by_digest = {value.observation_id: value for value in installed_models}
    model_by_digest.update({value.observation_id: value for value in provided_models})
    model_values = tuple(model_by_digest.values())
    supplied_placements = tuple(placements)
    existing = {(value.plugin_id, value.plugin_version) for value in supplied_placements}
    in_process = tuple(
        PlacementCapability(
            capability_id=f"placement.in_process.{value.plugin_id}.{value.version}",
            plugin_id=value.plugin_id,
            plugin_version=value.version,
            placement=Placement.IN_PROCESS,
            source=SourceLocation(
                SourceKind.DETECTION,
                locator=value.source.locator,
                symbol=f"{value.plugin_id}@{value.version}",
            ),
        )
        for value in plugin_values
        if (value.plugin_id, value.version) not in existing
    )
    return DetectionReport(
        identifier,
        captured,
        plugin_values,
        model_values,
        tuple(sources),
        tuple(storage),
        tuple(clocks),
        (*supplied_placements, *in_process),
        tuple(authorization_providers),
    )


def propose_deployment(
    authored: AuthoredExperiment | ComposedExperiment,
    report: DetectionReport,
    *,
    selection: DeploymentSelection | None = None,
    proposal_id: str | None = None,
) -> DeploymentProposal:
    """Generate a reviewable deployment while preserving portable intent."""

    if not isinstance(authored, (AuthoredExperiment, ComposedExperiment)):
        raise TypeError("deployment proposal requires an authored experiment")
    if not isinstance(report, DetectionReport):
        raise TypeError("deployment proposal requires a DetectionReport")
    selected = selection or DeploymentSelection()
    suite_hash = canonical_hash(authored.suite.to_payload())
    requirements_hash = authored.requirements.requirements_hash
    plugins = {(value.plugin_id, value.version): value for value in report.plugins}
    components = {value.component_id: value for value in authored.suite.components}
    streams = {value.stream_id: value for value in authored.suite.streams}
    execution_mode = authored.protocol.execution_mode
    decisions: list[DeploymentDecision] = []
    bindings: dict[str, ComponentBindingSpec] = {}
    resources: dict[str, ResourceSpec] = {}
    stream_bindings: dict[str, StreamBinding] = {}
    storage_bindings: dict[str, StorageBinding] = {}
    clock_bindings: dict[tuple[str, str], ClockMappingBinding] = {}

    requirements_by_kind: dict[DeploymentRequirementKind, list[DeploymentRequirement]] = {}
    for requirement in authored.requirements.requirements:
        requirements_by_kind.setdefault(requirement.kind, []).append(requirement)

    source_requirements = requirements_by_kind.get(DeploymentRequirementKind.SOURCE_BINDING, [])
    source_components: set[str] = set()
    for requirement in source_requirements:
        assert requirement.component_id is not None
        assert requirement.stream_id is not None
        component = components[requirement.component_id]
        logical = streams[requirement.stream_id]
        candidates = tuple(
            value
            for value in report.sources
            if not contract_issues(value.contract, logical.contract)
            and (logical.modality is None or value.stream.modality == logical.modality)
            and set(requirement.required_capabilities).issubset(
                {value.resource_kind, *value.capabilities}
            )
            and _source_plugin_compatible(value, component, plugins, execution_mode)
        )
        chosen, explicit = _select_capability(
            "source",
            requirement,
            candidates,
            selected.source_capabilities.get(requirement.requirement_id)
            or selected.source_capabilities.get(requirement.component_id),
        )
        source_components.add(component.component_id)
        secret_refs = dict(selected.component_secret_refs.get(component.component_id, {}))
        _require_secret_aliases(chosen.required_secret_aliases, secret_refs, component.component_id)
        source_config = thaw_json(chosen.config)
        observed_stream = source_config.get("stream_spec")
        if isinstance(observed_stream, Mapping):
            projected_stream = thaw_json(observed_stream)
            metadata = dict(projected_stream.get("metadata") or {})
            metadata["deployment_observation"] = {
                "stream_id": projected_stream.get("stream_id"),
                "clock_id": projected_stream.get("clock_id"),
                "capability_id": chosen.capability_id,
            }
            projected_stream["stream_id"] = logical.stream_id
            if logical.clock_id is not None:
                projected_stream["clock_id"] = logical.clock_id
            projected_stream["metadata"] = metadata
            source_config["stream_spec"] = projected_stream
        binding = ComponentBindingSpec(
            component.component_id,
            chosen.plugin_id,
            f"=={chosen.plugin_version}",
            source_config,
            chosen.placement,
            chosen.endpoint_id,
            (chosen.resource_id,),
            secret_refs,
        )
        bindings[component.component_id] = binding
        resource = chosen.resource()
        existing = resources.get(resource.resource_id)
        if existing is not None and existing != resource:
            _proposal_error(
                ExitCode.REJECTED,
                "deployment.resource_conflict",
                "Detected resource has conflicting descriptions",
                f"Resource {resource.resource_id} was selected with incompatible observations.",
                details={"resource_id": resource.resource_id},
            )
        resources[resource.resource_id] = resource
        stream_bindings[requirement.stream_id] = StreamBinding(
            requirement.stream_id, resource.resource_id
        )
        decisions.append(
            _decision(
                f"$.component_bindings.{component.component_id}",
                chosen.capability_id,
                chosen.source,
                requirement.requirement_id,
                explicit,
            )
        )
        decisions.append(
            _decision(
                f"$.stream_bindings.{requirement.stream_id}",
                chosen.capability_id,
                chosen.source,
                requirement.requirement_id,
                explicit,
            )
        )

    model_requirements = {
        value.component_id: value
        for value in requirements_by_kind.get(DeploymentRequirementKind.MODEL_ARTIFACT, [])
    }
    for component in authored.suite.components:
        if component.component_id in source_components:
            continue
        descriptor = _select_component_plugin(
            component,
            execution_mode,
            report,
            selected.component_plugins.get(component.component_id),
            model_requirements.get(component.component_id),
        )
        placement_candidates = tuple(
            value
            for value in report.placements
            if value.plugin_id == descriptor.plugin_id
            and value.plugin_version == descriptor.version
        )
        placement, explicit = _select_placement(
            component.component_id,
            placement_candidates,
            selected.placement_capabilities.get(component.component_id),
        )
        secret_refs = dict(selected.component_secret_refs.get(component.component_id, {}))
        _require_secret_aliases(
            placement.required_secret_aliases, secret_refs, component.component_id
        )
        bindings[component.component_id] = ComponentBindingSpec(
            component.component_id,
            descriptor.plugin_id,
            f"=={descriptor.version}",
            thaw_json(component.config),
            placement.placement,
            placement.endpoint_id,
            (),
            secret_refs,
        )
        decisions.append(
            _decision(
                f"$.component_bindings.{component.component_id}",
                placement.capability_id,
                placement.source,
                None,
                explicit,
            )
        )

    for requirement in requirements_by_kind.get(DeploymentRequirementKind.STORAGE, []):
        assert requirement.storage_kind is not None
        candidates = tuple(
            value
            for value in report.storage
            if value.binding.kind == requirement.storage_kind
            and set(requirement.required_capabilities).issubset(set(value.capabilities))
        )
        chosen, explicit = _select_capability(
            "storage",
            requirement,
            candidates,
            selected.storage_capabilities.get(requirement.requirement_id),
        )
        storage_bindings[chosen.binding.storage_id] = chosen.binding
        decisions.append(
            _decision(
                f"$.storage.{chosen.binding.storage_id}",
                chosen.capability_id,
                chosen.source,
                requirement.requirement_id,
                explicit,
            )
        )

    for requirement in requirements_by_kind.get(DeploymentRequirementKind.CLOCK_MAPPING, []):
        assert requirement.source_clock is not None
        assert requirement.target_clock is not None
        candidates = tuple(
            value
            for value in report.clocks
            if value.binding.source_clock == requirement.source_clock
            and value.binding.target_clock == requirement.target_clock
        )
        chosen, explicit = _select_capability(
            "clock",
            requirement,
            candidates,
            selected.clock_capabilities.get(requirement.requirement_id),
        )
        pair = (chosen.binding.source_clock, chosen.binding.target_clock)
        existing_clock = clock_bindings.get(pair)
        if existing_clock is not None:
            if existing_clock != chosen.binding:
                _proposal_error(
                    ExitCode.REJECTED,
                    "deployment.clock_conflict",
                    "Detected clock mappings conflict",
                    f"Multiple requirements selected incompatible mappings for {pair[0]} to {pair[1]}.",
                )
            continue
        clock_bindings[pair] = chosen.binding
        decisions.append(
            _decision(
                f"$.clock_mappings.{pair[0]}->{pair[1]}",
                chosen.capability_id,
                chosen.source,
                requirement.requirement_id,
                explicit,
            )
        )

    _validate_model_materializations(model_requirements.values(), report, selected.model_artifacts)
    _validate_reviewed_authorization(
        requirements_by_kind.get(DeploymentRequirementKind.AUTHORIZATION, []),
        report,
        selected.authorization_providers,
        selected.permissions,
        decisions,
    )
    secret_ids = {value.secret_id for value in selected.secrets}
    for component_id, refs in selected.component_secret_refs.items():
        unknown = sorted(set(refs.values()) - secret_ids)
        if unknown:
            _proposal_error(
                ExitCode.REJECTED,
                "deployment.secret_reference",
                "Secret reference is not declared",
                f"Component {component_id} references unknown secret identities: {', '.join(unknown)}.",
                details={"component_id": component_id, "secret_ids": unknown},
            )

    deployment = DeploymentSpec(
        deployment_id=(
            "deployment.proposal."
            + report.report_hash.removeprefix("sha256:")[:16]
        ),
        suite_id=authored.suite.suite_id,
        component_bindings=tuple(bindings[key] for key in sorted(bindings)),
        resources=tuple(resources[key] for key in sorted(resources)),
        stream_bindings=tuple(stream_bindings[key] for key in sorted(stream_bindings)),
        storage=tuple(storage_bindings[key] for key in sorted(storage_bindings)),
        permissions=tuple(sorted(selected.permissions, key=lambda value: value.permission_id)),
        authorization_providers=tuple(
            sorted(selected.authorization_providers, key=lambda value: value.provider_id)
        ),
        secrets=tuple(sorted(selected.secrets, key=lambda value: value.secret_id)),
        clock_mappings=tuple(clock_bindings[key] for key in sorted(clock_bindings)),
        model_artifacts=tuple(
            sorted(
                selected.model_artifacts,
                key=lambda value: (value.manifest_digest, value.artifact_id),
            )
        ),
    )
    if canonical_hash(authored.suite.to_payload()) != suite_hash:
        raise RuntimeError("deployment generation mutated the portable suite")
    return DeploymentProposal(
        proposal_id
        or f"proposal.{report.report_hash.removeprefix('sha256:')[:16]}",
        suite_hash,
        requirements_hash,
        report.report_hash,
        deployment,
        tuple(decisions),
    )


def read_model_observations(paths: Iterable[str | Path]) -> tuple[ModelObservation, ...]:
    """Read exact model manifests from explicitly named files or directories."""

    files: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_dir():
            files.extend(sorted(value for value in path.glob("*.json") if value.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(path)
    values: list[ModelObservation] = []
    for path in sorted(set(files)):
        manifest = ModelManifest.from_payload(_read_object(path))
        values.append(
            ModelObservation(
                manifest,
                SourceLocation(
                    SourceKind.DETECTION,
                    locator=str(path),
                    symbol=manifest.manifest_digest,
                ),
            )
        )
    return tuple(values)


def _load_model_entry_points(
    group: str = MODEL_MANIFEST_ENTRY_POINT_GROUP,
) -> tuple[ModelObservation, ...]:
    discovered = metadata.entry_points()
    if hasattr(discovered, "select"):
        entries = tuple(discovered.select(group=group))
    else:  # pragma: no cover - compatibility with older importlib.metadata
        entries = tuple(discovered.get(group, ()))
    values: list[ModelObservation] = []
    for entry in sorted(entries, key=lambda value: (value.name, value.value)):
        loaded = entry.load()
        materialized = loaded() if callable(loaded) else loaded
        if isinstance(materialized, ModelManifest):
            manifests = (materialized,)
        elif isinstance(materialized, Iterable) and not isinstance(
            materialized, (str, bytes, Mapping)
        ):
            manifests = tuple(materialized)
            if not all(isinstance(value, ModelManifest) for value in manifests):
                raise TypeError(
                    "model entry point iterable must contain only ModelManifest values"
                )
        else:
            raise TypeError(
                "model entry point must load a ModelManifest or iterable of manifests"
            )
        distribution = getattr(getattr(entry, "dist", None), "name", None)
        for manifest in manifests:
            values.append(
                ModelObservation(
                    manifest,
                    SourceLocation(
                        SourceKind.DETECTION,
                        locator=str(distribution or "python-environment"),
                        symbol=f"{entry.name}:{manifest.manifest_digest}",
                    ),
                )
            )
    return tuple(values)


def read_capability_observations(path: str | Path) -> Mapping[str, tuple[Any, ...]]:
    """Read transport/provider detector output without importing that integration."""

    payload = _read_object(path)
    allowed = {"sources", "storage", "clocks", "placements", "authorization_providers"}
    unknown = sorted(set(payload) - allowed - {"schema"})
    if unknown:
        raise ValueError("unknown capability observation fields: " + ", ".join(unknown))
    return MappingProxyType(
        {
            "sources": tuple(SourceCapability.from_payload(value) for value in payload.get("sources", ())),
            "storage": tuple(StorageCapability.from_payload(value) for value in payload.get("storage", ())),
            "clocks": tuple(ClockCapability.from_payload(value) for value in payload.get("clocks", ())),
            "placements": tuple(PlacementCapability.from_payload(value) for value in payload.get("placements", ())),
            "authorization_providers": tuple(
                AuthorizationProviderCapability.from_payload(value)
                for value in payload.get("authorization_providers", ())
            ),
        }
    )


def signal_contract_from_stream(stream: StreamSpec) -> SignalContract:
    """Project observed channel/rate/unit facts into the compiler contract type."""

    type_id = {
        ContentKind.DENSE_SAMPLES: "eegle.dense_sample_batch.v1",
        ContentKind.SPARSE_EVENTS: "eegle.sparse_event_batch.v1",
        ContentKind.METADATA: "eegle.metadata_event.v1",
    }[stream.content_kind]
    units = {value.channel_id: value.unit for value in stream.channels}
    unique_units = set(units.values())
    event_kinds = (
        tuple(str(value) for value in stream.metadata.get("event_kinds", ()))
        if stream.content_kind == ContentKind.SPARSE_EVENTS
        else ()
    )
    rate_model = (
        "irregular"
        if stream.content_kind == ContentKind.SPARSE_EVENTS
        else stream.rate_model.value
    )
    missing_data_policy = (
        "explicit_validity"
        if stream.missing_data_policy.value == "validity_mask"
        else stream.missing_data_policy.value
    )
    return SignalContract(
        type_id=type_id,
        unit=next(iter(unique_units)) if len(unique_units) == 1 else None,
        channel_count=len(stream.channels) or None,
        nominal_rate_hz=stream.sample_rate_hz,
        content_kind=stream.content_kind.value,
        rate_model=rate_model,
        channel_ids=tuple(value.channel_id for value in stream.channels),
        units=units,
        event_kinds=event_kinds,
        missing_data_policy=missing_data_policy,
        layout="samples_by_channels"
        if stream.content_kind == ContentKind.DENSE_SAMPLES
        else None,
    )


def _source_plugin_compatible(
    source: SourceCapability,
    component: Any,
    plugins: Mapping[tuple[str, str], PluginObservation],
    mode: ExecutionMode,
) -> bool:
    plugin = plugins.get((source.plugin_id, source.plugin_version))
    if plugin is None or plugin.kind != ComponentKind.SOURCE:
        return False
    if component.plugin_id is not None and component.plugin_id != plugin.plugin_id:
        return False
    if component.version_spec is not None and Version(plugin.version) not in SpecifierSet(
        component.version_spec
    ):
        return False
    capabilities = plugin.descriptor["capabilities"]
    if mode.value not in capabilities["supported_modes"]:
        return False
    if not set(capabilities.get("resources", ())).issubset(
        {source.resource_kind, *source.capabilities}
    ):
        return False
    return True


def _select_component_plugin(
    component: Any,
    mode: ExecutionMode,
    report: DetectionReport,
    explicit_plugin_id: str | None,
    model_requirement: DeploymentRequirement | None,
) -> PluginObservation:
    plugin_id = explicit_plugin_id or component.plugin_id
    version_spec = component.version_spec
    if plugin_id is None and model_requirement is not None:
        assert model_requirement.manifest_digest is not None
        models = [
            value.manifest
            for value in report.models
            if value.manifest.manifest_digest == model_requirement.manifest_digest
        ]
        if not models:
            _proposal_error(
                ExitCode.UNAVAILABLE,
                "deployment.model_unavailable",
                "Required model was not detected",
                f"No detected model has digest {model_requirement.manifest_digest}.",
                details={"manifest_digest": model_requirement.manifest_digest},
            )
        implementations = models[0].implementations
        possible = sorted(
            {
                value.plugin_id
                for value in implementations
                if any(
                    plugin.plugin_id == value.plugin_id
                    and Version(plugin.version) in SpecifierSet(value.version_spec)
                    for plugin in report.plugins
                )
            }
        )
        if len(possible) != 1:
            _match_error("plugin", component.component_id, possible)
        plugin_id = possible[0]
        version_spec = next(
            value.version_spec for value in implementations if value.plugin_id == plugin_id
        )
    if plugin_id is None:
        _proposal_error(
            ExitCode.UNAVAILABLE,
            "deployment.plugin_unavailable",
            "Component has no executable plugin",
            f"Component {component.component_id} has no portable or selected plugin identity.",
            details={"component_id": component.component_id},
        )
    if component.plugin_id is not None and plugin_id != component.plugin_id:
        _proposal_error(
            ExitCode.REJECTED,
            "deployment.plugin_conflict",
            "Selected plugin conflicts with portable intent",
            f"Component {component.component_id} requires {component.plugin_id}, not {plugin_id}.",
            details={"component_id": component.component_id},
        )
    candidates = [
        value
        for value in report.plugins
        if value.plugin_id == plugin_id
        and value.kind == component.kind
        and Version(value.version) in SpecifierSet(version_spec or "")
        and mode.value in value.descriptor["capabilities"]["supported_modes"]
    ]
    if not candidates:
        _proposal_error(
            ExitCode.UNAVAILABLE,
            "deployment.plugin_unavailable",
            "Required plugin was not detected",
            f"No compatible {plugin_id} plugin was detected for {component.component_id}.",
            details={"component_id": component.component_id, "plugin_id": plugin_id},
        )
    return max(candidates, key=lambda value: Version(value.version))


def _select_placement(
    component_id: str,
    candidates: Sequence[PlacementCapability],
    selected: str | None,
) -> tuple[PlacementCapability, bool]:
    if selected is not None:
        matching = [value for value in candidates if value.capability_id == selected]
        if not matching:
            _proposal_error(
                ExitCode.REJECTED,
                "deployment.selection_invalid",
                "Selected placement is not compatible",
                f"Placement {selected} cannot execute component {component_id}.",
                details={
                    "component_id": component_id,
                    "selected": selected,
                    "candidates": [value.capability_id for value in candidates],
                },
            )
        return matching[0], True
    if len(candidates) != 1:
        _match_error("placement", component_id, [value.capability_id for value in candidates])
    return candidates[0], False


def _select_capability(
    kind: str,
    requirement: DeploymentRequirement,
    candidates: Sequence[Any],
    selected: str | None,
) -> tuple[Any, bool]:
    if selected is not None:
        matching = [value for value in candidates if value.capability_id == selected]
        if not matching:
            _proposal_error(
                ExitCode.REJECTED,
                "deployment.selection_invalid",
                f"Selected {kind} capability is not compatible",
                f"{selected} does not satisfy {requirement.requirement_id}.",
                details={
                    "requirement_id": requirement.requirement_id,
                    "selected": selected,
                    "candidates": [value.capability_id for value in candidates],
                },
            )
        return matching[0], True
    if len(candidates) != 1:
        _match_error(kind, requirement.requirement_id, [value.capability_id for value in candidates])
    return candidates[0], False


def _match_error(kind: str, target: str, candidates: Sequence[str]) -> None:
    ordered = sorted(candidates)
    if not ordered:
        _proposal_error(
            ExitCode.UNAVAILABLE,
            f"deployment.{kind}_unavailable",
            f"No compatible {kind} capability was detected",
            f"No detected {kind} capability satisfies {target}.",
            details={"target": target, "candidates": []},
        )
    _proposal_error(
        ExitCode.REJECTED,
        f"deployment.{kind}_ambiguous",
        f"Multiple compatible {kind} capabilities were detected",
        f"Select one exact capability for {target}: {', '.join(ordered)}.",
        details={"target": target, "candidates": ordered},
    )


def _validate_model_materializations(
    requirements: Iterable[DeploymentRequirement],
    report: DetectionReport,
    bindings: tuple[ModelArtifactBindingSpec, ...],
) -> None:
    detected = {value.manifest.manifest_digest: value.manifest for value in report.models}
    materialized = {(value.manifest_digest, value.artifact_id) for value in bindings}
    for requirement in requirements:
        assert requirement.manifest_digest is not None
        manifest = detected.get(requirement.manifest_digest)
        if manifest is None:
            _proposal_error(
                ExitCode.UNAVAILABLE,
                "deployment.model_unavailable",
                "Required model was not detected",
                f"No detected model has digest {requirement.manifest_digest}.",
                details={"requirement_id": requirement.requirement_id},
            )
        missing = sorted(
            value.artifact_id
            for value in manifest.artifacts
            if (manifest.manifest_digest, value.artifact_id) not in materialized
        )
        if missing:
            _proposal_error(
                ExitCode.UNAVAILABLE,
                "deployment.model_artifact_unavailable",
                "Model artifact materialization was not selected",
                f"Select site-local artifact bindings for: {', '.join(missing)}.",
                details={
                    "requirement_id": requirement.requirement_id,
                    "manifest_digest": manifest.manifest_digest,
                    "artifact_ids": missing,
                },
            )


def _validate_reviewed_authorization(
    requirements: Sequence[DeploymentRequirement],
    report: DetectionReport,
    providers: tuple[AuthorizationProviderBindingSpec, ...],
    permissions: tuple[PermissionGrant, ...],
    decisions: list[DeploymentDecision],
) -> None:
    if not requirements:
        if providers or permissions:
            _proposal_error(
                ExitCode.REJECTED,
                "deployment.authorization_extraneous",
                "Authorization was supplied without a portable requirement",
                "Remove provider and permission records that are not required by the suite.",
            )
        return
    provider_by_id = {value.provider_id: value for value in providers}
    observations = {value.provider_id: value for value in report.authorization_providers}
    for requirement in requirements:
        assert requirement.component_id is not None
        assert requirement.action_capability is not None
        grants = [
            value
            for value in permissions
            if value.capability == requirement.action_capability
            and requirement.component_id in value.component_ids
        ]
        if len(grants) != 1:
            _proposal_error(
                ExitCode.REJECTED,
                "deployment.authorization_review_required",
                "Explicit authorization review is required",
                f"Supply one reviewed permission grant for {requirement.requirement_id}; detected providers are evidence, not authority.",
                details={
                    "requirement_id": requirement.requirement_id,
                    "detected_provider_ids": sorted(observations),
                },
            )
        grant = grants[0]
        provider = provider_by_id.get(grant.authorization_ref)
        observation = observations.get(grant.authorization_ref)
        if provider is None or observation is None:
            _proposal_error(
                ExitCode.REJECTED,
                "deployment.authorization_provider_unreviewed",
                "Authorization provider is not both detected and selected",
                f"Provider {grant.authorization_ref} requires an exact reviewed binding.",
                details={"provider_id": grant.authorization_ref},
            )
        if (
            provider.plugin_id != observation.plugin_id
            or Version(observation.plugin_version) not in SpecifierSet(provider.version_spec or "")
            or requirement.action_capability not in observation.capabilities
        ):
            _proposal_error(
                ExitCode.REJECTED,
                "deployment.authorization_provider_mismatch",
                "Reviewed authorization does not match detected capability",
                f"Provider {provider.provider_id} does not prove {requirement.action_capability}.",
                details={"provider_id": provider.provider_id},
            )
        if observation.required_secret_aliases:
            _proposal_error(
                ExitCode.REJECTED,
                "deployment.authorization_secret_reference_required",
                "Authorization provider secret references are unresolved",
                "This provider declares secret aliases that cannot be satisfied by literal configuration.",
                details={
                    "provider_id": provider.provider_id,
                    "aliases": list(observation.required_secret_aliases),
                },
            )
        decisions.append(
            DeploymentDecision(
                f"$.permissions.{grant.permission_id}",
                observation.capability_id,
                AuthoringOrigin.USER_EXPLICIT,
                True,
                observation.source,
                requirement.requirement_id,
            )
        )


def _require_secret_aliases(
    required: Sequence[str], refs: Mapping[str, str], component_id: str
) -> None:
    missing = sorted(set(required) - set(refs))
    if missing:
        _proposal_error(
            ExitCode.REJECTED,
            "deployment.secret_reference_required",
            "Required secret references were not selected",
            f"Component {component_id} requires secret aliases: {', '.join(missing)}.",
            details={"component_id": component_id, "aliases": missing},
        )


def _decision(
    target: str,
    observation_id: str,
    source: SourceLocation,
    requirement_id: str | None,
    explicit: bool,
) -> DeploymentDecision:
    return DeploymentDecision(
        target,
        observation_id,
        AuthoringOrigin.USER_EXPLICIT if explicit else AuthoringOrigin.DETECTION_PROPOSAL,
        explicit,
        source,
        requirement_id,
    )


def _proposal_error(
    exit_code: ExitCode,
    code: str,
    title: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> None:
    raise OperationError(
        "deployment.propose",
        exit_code,
        (
            OperationDiagnostic(
                code,
                OperationCategory.AVAILABILITY
                if exit_code == ExitCode.UNAVAILABLE
                else OperationCategory.PREFLIGHT,
                title,
                message,
                details=details or {},
            ),
        ),
    )


def _require_detection_source(source: SourceLocation) -> None:
    if not isinstance(source, SourceLocation) or source.kind != SourceKind.DETECTION:
        raise ValueError("capability observations require detection provenance")


def _reject_credential_literals(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _CREDENTIAL_KEYS:
                raise ValueError(
                    f"credential literal field {path}.{key} is forbidden; use SecretReference"
                )
            _reject_credential_literals(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_credential_literals(nested, f"{path}[{index}]")


def _require_unique(values: Iterable[str], label: str) -> None:
    items = tuple(values)
    if len(items) != len(set(items)):
        raise ValueError(f"{label} identities must be unique")


def _parse_utc(value: str) -> datetime:
    normalized = str(value)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("observed_at must be an ISO-8601 UTC timestamp")
    return parsed


def _read_object(path: str | Path) -> Mapping[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON object: {source}") from exc
    if not isinstance(payload, Mapping):
        raise TypeError(f"{source} must contain a JSON object")
    return payload


__all__ = [
    "AuthorizationProviderCapability",
    "ClockCapability",
    "DETECTION_REPORT_SCHEMA_ID",
    "DEPLOYMENT_PROPOSAL_SCHEMA_ID",
    "DeploymentDecision",
    "DeploymentProposal",
    "DeploymentSelection",
    "DetectionReport",
    "ModelObservation",
    "MODEL_MANIFEST_ENTRY_POINT_GROUP",
    "PlacementCapability",
    "PluginObservation",
    "SourceCapability",
    "StorageCapability",
    "detect_capabilities",
    "propose_deployment",
    "read_capability_observations",
    "read_model_observations",
    "signal_contract_from_stream",
]
