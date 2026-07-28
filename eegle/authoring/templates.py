"""Versioned declarative templates as clients of draft lowering."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from packaging.version import InvalidVersion, Version

from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.authoring.contracts import CanonicalArtifact
from eegle.authoring.drafts import (
    DEPLOYMENT_REQUIREMENTS_SCHEMA_ID,
    DeploymentRequirementKind,
    DraftIssue,
    DraftLoweringError,
    ExperimentDraft,
    LoweredExperiment,
    TemplateSelection,
)
from eegle.authoring.provenance import DraftSourceMap
from eegle.authoring.schemas import EXPERIMENT_DRAFT_SCHEMA_ID
from eegle.compiler.lock import canonical_hash
from eegle.specs.protocol import PROTOCOL_SPEC_SCHEMA
from eegle.specs.suite import SUITE_SPEC_SCHEMA


TEMPLATE_EXPANSION_LOCK_SCHEMA_ID = "eegle.template_expansion_lock.v1"
TEMPLATE_DEFINITION_SCHEMA_ID = "eegle.template_definition.v1"
_JSON_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$"
_DIGEST = r"^sha256:[0-9a-f]{64}$"


class TemplateProfile(str, Enum):
    CONTINUOUS_RECORDING = "continuous_recording"
    EEG_EVENTS_RECORDING = "eeg_events_recording"
    CONTINUOUS_OBSERVER = "continuous_observer"
    EVENT_LOCKED_MODEL = "event_locked_model"
    PRIMARY_SHADOW_COMPARISON = "primary_shadow_comparison"
    CALIBRATION_VALIDATION = "calibration_validation"
    DELAYED_OUTCOME_ADAPTATION = "delayed_outcome_adaptation"
    SIMULATED_CLOSED_LOOP = "simulated_closed_loop"


class TemplateMigrationPolicy(str, Enum):
    EXPLICIT_REEXPANSION = "explicit_reexpansion"


@dataclass(frozen=True, slots=True)
class TemplateParameterTarget:
    artifact: CanonicalArtifact
    path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact", CanonicalArtifact(self.artifact))
        if self.path and not self.path.startswith("/"):
            raise ValueError("template parameter target must be a JSON pointer")

    def to_payload(self) -> dict[str, str]:
        return {"artifact": self.artifact.value, "path": self.path}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TemplateParameterTarget":
        return cls(
            artifact=CanonicalArtifact(str(payload["artifact"])),
            path=str(payload["path"]),
        )


@dataclass(frozen=True, slots=True)
class TemplateDefinition:
    template_id: str
    version: str
    title: str
    profile: TemplateProfile
    parameter_schema: Mapping[str, Any]
    parameter_defaults: Mapping[str, Any]
    scientific_defaults: Mapping[str, Any]
    parameter_targets: Mapping[str, tuple[TemplateParameterTarget, ...]]
    generated_schemas: Mapping[str, str]
    required_capabilities: tuple[str, ...]
    deployment_requirement_kinds: tuple[DeploymentRequirementKind, ...]
    synthetic_fixture: str
    synthetic_expansion_digest: str
    migration_policy: TemplateMigrationPolicy = (
        TemplateMigrationPolicy.EXPLICIT_REEXPANSION
    )
    implementation_revision: str = "template-lowering-v1"
    schema: str = TEMPLATE_DEFINITION_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != TEMPLATE_DEFINITION_SCHEMA_ID:
            raise ValueError(f"unsupported template definition schema: {self.schema}")
        object.__setattr__(
            self,
            "template_id",
            require_identifier(self.template_id, "template_id"),
        )
        try:
            Version(self.version)
        except InvalidVersion as exc:
            raise ValueError("template version must be a valid PEP 440 version") from exc
        if not self.title.strip():
            raise ValueError("template title cannot be empty")
        object.__setattr__(self, "profile", TemplateProfile(self.profile))
        object.__setattr__(
            self,
            "migration_policy",
            TemplateMigrationPolicy(self.migration_policy),
        )
        object.__setattr__(
            self,
            "implementation_revision",
            require_identifier(self.implementation_revision, "implementation_revision"),
        )
        object.__setattr__(self, "parameter_schema", freeze_json(self.parameter_schema))
        from eegle.specs.schemas import validate_schema

        validate_schema(self.parameter_schema)
        object.__setattr__(self, "parameter_defaults", freeze_json(self.parameter_defaults))
        object.__setattr__(self, "scientific_defaults", freeze_json(self.scientific_defaults))
        if set(self.scientific_defaults) - set(self.parameter_defaults):
            raise ValueError("scientific defaults must also be parameter defaults")
        if any(
            self.scientific_defaults[key] != self.parameter_defaults[key]
            for key in self.scientific_defaults
        ):
            raise ValueError("scientific defaults must match their parameter defaults")
        schema_parameters = set(self.parameter_schema["properties"])
        if set(self.parameter_defaults) - schema_parameters:
            raise ValueError("parameter defaults must be declared by the parameter schema")
        targets: dict[str, tuple[TemplateParameterTarget, ...]] = {}
        for parameter, values in self.parameter_targets.items():
            name = require_identifier(str(parameter), "template parameter")
            materialized = tuple(values)
            if not all(isinstance(value, TemplateParameterTarget) for value in materialized):
                raise TypeError("parameter targets must be TemplateParameterTarget values")
            targets[name] = materialized
        if set(targets) - schema_parameters:
            raise ValueError("parameter targets must be declared by the parameter schema")
        object.__setattr__(self, "parameter_targets", MappingProxyType(targets))
        generated = {
            require_identifier(str(key), "generated artifact"): require_identifier(
                str(value), "generated schema"
            )
            for key, value in self.generated_schemas.items()
        }
        required_generated = {"draft", "protocol", "suite", "deployment_requirements"}
        if set(generated) != required_generated:
            raise ValueError("template generated_schemas must name all authoring outputs")
        object.__setattr__(self, "generated_schemas", MappingProxyType(generated))
        capabilities = tuple(
            sorted(
                require_identifier(value, "required capability")
                for value in self.required_capabilities
            )
        )
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("template capabilities must be unique")
        if not capabilities:
            raise ValueError("templates must declare required capabilities")
        object.__setattr__(self, "required_capabilities", capabilities)
        kinds = tuple(
            sorted(
                {DeploymentRequirementKind(value) for value in self.deployment_requirement_kinds},
                key=lambda value: value.value,
            )
        )
        if not kinds:
            raise ValueError("templates must declare deployment requirement kinds")
        object.__setattr__(self, "deployment_requirement_kinds", kinds)
        object.__setattr__(
            self,
            "synthetic_fixture",
            require_identifier(self.synthetic_fixture, "synthetic_fixture"),
        )
        object.__setattr__(
            self,
            "synthetic_expansion_digest",
            require_digest(
                self.synthetic_expansion_digest,
                "synthetic_expansion_digest",
            ),
        )
        _validate_parameters(self, thaw_json(self.parameter_defaults), require_complete=False)

    @property
    def manifest_digest(self) -> str:
        return canonical_hash(self.to_payload())

    def effective_parameters(self, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        explicit = thaw_json(freeze_json(parameters))
        _validate_parameters(self, explicit, require_complete=False)
        effective = {**thaw_json(self.parameter_defaults), **explicit}
        _validate_parameters(self, effective, require_complete=True)
        return freeze_json(effective)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "template_id": self.template_id,
            "version": self.version,
            "title": self.title,
            "profile": self.profile.value,
            "parameter_schema": thaw_json(self.parameter_schema),
            "parameter_defaults": thaw_json(self.parameter_defaults),
            "scientific_defaults": thaw_json(self.scientific_defaults),
            "parameter_targets": {
                key: [value.to_payload() for value in values]
                for key, values in sorted(self.parameter_targets.items())
            },
            "generated_schemas": dict(sorted(self.generated_schemas.items())),
            "required_capabilities": list(self.required_capabilities),
            "deployment_requirement_kinds": [
                value.value for value in self.deployment_requirement_kinds
            ],
            "synthetic_fixture": self.synthetic_fixture,
            "synthetic_expansion_digest": self.synthetic_expansion_digest,
            "migration_policy": self.migration_policy.value,
            "implementation_revision": self.implementation_revision,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TemplateDefinition":
        return cls(
            schema=str(payload["schema"]),
            template_id=str(payload["template_id"]),
            version=str(payload["version"]),
            title=str(payload["title"]),
            profile=TemplateProfile(str(payload["profile"])),
            parameter_schema=dict(payload["parameter_schema"]),
            parameter_defaults=dict(payload["parameter_defaults"]),
            scientific_defaults=dict(payload["scientific_defaults"]),
            parameter_targets={
                str(key): tuple(
                    TemplateParameterTarget.from_payload(value)
                    for value in values
                )
                for key, values in payload["parameter_targets"].items()
            },
            generated_schemas=dict(payload["generated_schemas"]),
            required_capabilities=tuple(payload["required_capabilities"]),
            deployment_requirement_kinds=tuple(
                DeploymentRequirementKind(str(value))
                for value in payload["deployment_requirement_kinds"]
            ),
            synthetic_fixture=str(payload["synthetic_fixture"]),
            synthetic_expansion_digest=str(payload["synthetic_expansion_digest"]),
            migration_policy=TemplateMigrationPolicy(str(payload["migration_policy"])),
            implementation_revision=str(payload["implementation_revision"]),
        )


class TemplateCatalog:
    """An exact-version catalog; it deliberately has no implicit latest lookup."""

    def __init__(self, templates: Iterable[TemplateDefinition]) -> None:
        values = tuple(sorted(templates, key=lambda value: (value.template_id, value.version)))
        by_key = {(value.template_id, value.version): value for value in values}
        if len(by_key) != len(values):
            raise ValueError("template identities and versions must be unique")
        self._templates = values
        self._by_key = MappingProxyType(by_key)

    @property
    def templates(self) -> tuple[TemplateDefinition, ...]:
        return self._templates

    def resolve(self, template_id: str, version: str) -> TemplateDefinition:
        key = (str(template_id), str(version))
        try:
            return self._by_key[key]
        except KeyError as exc:
            raise KeyError(
                f"unknown exact template revision: {template_id}@{version}"
            ) from exc

    def with_template(self, template: TemplateDefinition) -> "TemplateCatalog":
        if not isinstance(template, TemplateDefinition):
            raise TypeError("catalog additions must be TemplateDefinition values")
        existing = self._by_key.get((template.template_id, template.version))
        if existing is not None:
            if existing.manifest_digest != template.manifest_digest:
                raise ValueError(
                    "an existing template revision cannot be replaced with new content"
                )
            return self
        return TemplateCatalog((*self.templates, template))


@dataclass(frozen=True, slots=True)
class TemplateExpansionLock:
    template_id: str
    template_version: str
    template_manifest_digest: str
    explicit_parameters_digest: str
    effective_parameters_digest: str
    draft_digest: str
    protocol_digest: str
    suite_digest: str
    deployment_requirements_digest: str
    schema: str = TEMPLATE_EXPANSION_LOCK_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != TEMPLATE_EXPANSION_LOCK_SCHEMA_ID:
            raise ValueError(f"unsupported template expansion lock schema: {self.schema}")
        object.__setattr__(
            self,
            "template_id",
            require_identifier(self.template_id, "template_id"),
        )
        if not self.template_version.strip():
            raise ValueError("template_version cannot be empty")
        for field in (
            "template_manifest_digest",
            "explicit_parameters_digest",
            "effective_parameters_digest",
            "draft_digest",
            "protocol_digest",
            "suite_digest",
            "deployment_requirements_digest",
        ):
            object.__setattr__(self, field, require_digest(getattr(self, field), field))

    @property
    def expansion_digest(self) -> str:
        return canonical_hash(
            {
                "schema": self.schema,
                "template_id": self.template_id,
                "template_version": self.template_version,
                "explicit_parameters_digest": self.explicit_parameters_digest,
                "effective_parameters_digest": self.effective_parameters_digest,
                "protocol_digest": self.protocol_digest,
                "suite_digest": self.suite_digest,
                "deployment_requirements_digest": (
                    self.deployment_requirements_digest
                ),
            }
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "template_id": self.template_id,
            "template_version": self.template_version,
            "template_manifest_digest": self.template_manifest_digest,
            "explicit_parameters_digest": self.explicit_parameters_digest,
            "effective_parameters_digest": self.effective_parameters_digest,
            "draft_digest": self.draft_digest,
            "protocol_digest": self.protocol_digest,
            "suite_digest": self.suite_digest,
            "deployment_requirements_digest": self.deployment_requirements_digest,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TemplateExpansionLock":
        return cls(
            schema=str(payload["schema"]),
            template_id=str(payload["template_id"]),
            template_version=str(payload["template_version"]),
            template_manifest_digest=str(payload["template_manifest_digest"]),
            explicit_parameters_digest=str(payload["explicit_parameters_digest"]),
            effective_parameters_digest=str(payload["effective_parameters_digest"]),
            draft_digest=str(payload["draft_digest"]),
            protocol_digest=str(payload["protocol_digest"]),
            suite_digest=str(payload["suite_digest"]),
            deployment_requirements_digest=str(
                payload["deployment_requirements_digest"]
            ),
        )


@dataclass(frozen=True, slots=True)
class TemplateExpansion:
    template: TemplateDefinition
    draft: ExperimentDraft
    lowered: LoweredExperiment
    effective_parameters: Mapping[str, Any]
    lock: TemplateExpansionLock

    def __post_init__(self) -> None:
        object.__setattr__(self, "effective_parameters", freeze_json(self.effective_parameters))
        if self.draft.template is None:
            raise ValueError("template expansion requires an exact draft selection")
        if self.draft.template.template_id != self.template.template_id:
            raise ValueError("draft selection does not match the template identity")
        if self.draft.template.version != self.template.version:
            raise ValueError("draft selection does not match the template version")
        if self.draft.template.manifest_digest != self.template.manifest_digest:
            raise ValueError("draft selection does not match the template manifest")
        if self.lock.template_manifest_digest != self.template.manifest_digest:
            raise ValueError("template expansion lock does not match the template manifest")
        if self.lock.template_id != self.template.template_id:
            raise ValueError("template expansion lock does not match the template identity")
        if self.lock.template_version != self.template.version:
            raise ValueError("template expansion lock does not match the template version")
        if self.lock.draft_digest != self.draft.draft_digest:
            raise ValueError("template expansion lock does not match the draft")
        if self.lock.explicit_parameters_digest != canonical_hash(
            self.draft.template.parameters
        ):
            raise ValueError("template expansion lock does not match explicit parameters")
        if self.lock.effective_parameters_digest != canonical_hash(
            self.effective_parameters
        ):
            raise ValueError("template expansion lock does not match the parameters")
        if self.lock.protocol_digest != self.lowered.protocol.spec_hash:
            raise ValueError("template expansion lock does not match the protocol")
        if self.lock.suite_digest != self.lowered.suite.spec_hash:
            raise ValueError("template expansion lock does not match the suite")
        if (
            self.lock.deployment_requirements_digest
            != self.lowered.deployment_requirements.requirements_hash
        ):
            raise ValueError(
                "template expansion lock does not match deployment requirements"
            )

    @property
    def expansion_digest(self) -> str:
        return self.lock.expansion_digest


def expand_template(
    template_id: str,
    version: str,
    *,
    draft_id: str,
    parameters: Mapping[str, Any] | None = None,
    revision: int = 1,
    source_map: DraftSourceMap | None = None,
    catalog: TemplateCatalog | None = None,
) -> TemplateExpansion:
    """Expand an exact template revision through the sole draft lowerer."""

    selected_catalog = catalog or BUILTIN_TEMPLATE_CATALOG
    template = selected_catalog.resolve(template_id, version)
    draft = create_template_draft(
        template_id,
        version,
        draft_id=draft_id,
        parameters=parameters,
        revision=revision,
        catalog=selected_catalog,
    )
    explicit = thaw_json(draft.template.parameters)
    effective = template.effective_parameters(explicit)
    lowered = _lower_template_draft(
        draft,
        source_map or DraftSourceMap({}),
        catalog=selected_catalog,
    )
    lock = TemplateExpansionLock(
        template_id=template.template_id,
        template_version=template.version,
        template_manifest_digest=template.manifest_digest,
        explicit_parameters_digest=canonical_hash(explicit),
        effective_parameters_digest=canonical_hash(effective),
        draft_digest=draft.draft_digest,
        protocol_digest=lowered.protocol.spec_hash,
        suite_digest=lowered.suite.spec_hash,
        deployment_requirements_digest=(
            lowered.deployment_requirements.requirements_hash
        ),
    )
    return TemplateExpansion(
        template=template,
        draft=draft,
        lowered=lowered,
        effective_parameters=effective,
        lock=lock,
    )


def create_template_draft(
    template_id: str,
    version: str,
    *,
    draft_id: str,
    parameters: Mapping[str, Any] | None = None,
    revision: int = 1,
    catalog: TemplateCatalog | None = None,
) -> ExperimentDraft:
    """Create a non-executable draft locked to one exact template manifest."""

    selected_catalog = catalog or BUILTIN_TEMPLATE_CATALOG
    template = selected_catalog.resolve(template_id, version)
    explicit = thaw_json(freeze_json(parameters or {}))
    return ExperimentDraft(
        draft_id=draft_id,
        revision=revision,
        intent={
            "study": {
                "template_id": template.template_id,
                "template_version": template.version,
            }
        },
        template=TemplateSelection(
            template_id=template.template_id,
            version=template.version,
            manifest_digest=template.manifest_digest,
            parameters=explicit,
        ),
    )


def _lower_template_draft(
    draft: ExperimentDraft,
    sources: DraftSourceMap,
    *,
    catalog: TemplateCatalog | None = None,
) -> LoweredExperiment:
    if draft.template is None:
        raise TypeError("template lowering requires an exact template selection")
    selected_catalog = catalog or BUILTIN_TEMPLATE_CATALOG
    try:
        template = selected_catalog.resolve(
            draft.template.template_id,
            draft.template.version,
        )
    except KeyError as exc:
        raise DraftLoweringError(
            (
                DraftIssue(
                    "authoring.template_revision",
                    "/template/version",
                    str(exc),
                    sources.source_for("/template/version"),
                ),
            )
        ) from exc
    if template.manifest_digest != draft.template.manifest_digest:
        raise DraftLoweringError(
            (
                DraftIssue(
                    "authoring.template_manifest",
                    "/template/manifest_digest",
                    "selected template content does not match the locked manifest digest",
                    sources.source_for("/template/manifest_digest"),
                ),
            )
        )
    try:
        effective = template.effective_parameters(draft.template.parameters)
    except (TypeError, ValueError) as exc:
        raise DraftLoweringError(
            (
                DraftIssue(
                    "authoring.template_parameters",
                    "/template/parameters",
                    str(exc),
                    sources.source_for("/template/parameters"),
                ),
            )
        ) from exc

    from eegle.authoring._template_profiles import build_template_profile

    return build_template_profile(
        template,
        draft,
        effective,
        explicit_parameters=frozenset(draft.template.parameters),
        sources=sources,
    )


def _validate_parameters(
    template: TemplateDefinition,
    parameters: Mapping[str, Any],
    *,
    require_complete: bool,
) -> None:
    from eegle.specs.schemas import validate_payload

    schema = thaw_json(template.parameter_schema)
    if not require_complete:
        schema["required"] = []
    validate_payload(parameters, schema)


def _target(artifact: CanonicalArtifact, path: str) -> TemplateParameterTarget:
    return TemplateParameterTarget(artifact, path)


def _parameter_schema(*, model: bool = False, action: bool = False) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "unit": {"type": "string", "minLength": 1},
        "channel_count": {"type": "integer", "minimum": 1},
        "sample_rate_hz": {"type": "number", "exclusiveMinimum": 0},
        "window_samples": {"type": "integer", "minimum": 1},
        "step_samples": {"type": "integer", "minimum": 1},
        "event_kind": {"type": "string", "pattern": _IDENTIFIER},
        "threshold": {"type": "number"},
        "shadow_threshold": {"type": "number"},
        "positive_label": {"type": "string", "minLength": 1},
        "negative_label": {"type": "string", "minLength": 1},
    }
    required: list[str] = []
    if model:
        properties.update(
            {
                "model_plugin_id": {"type": "string", "pattern": _IDENTIFIER},
                "model_manifest_digest": {"type": "string", "pattern": _DIGEST},
                "model_version_spec": {"type": "string", "minLength": 1},
            }
        )
        required.extend(("model_plugin_id", "model_manifest_digest"))
    if action:
        properties.update(
            {
                "action_capability": {"type": "string", "pattern": _IDENTIFIER},
                "action_intensity": {"type": "number", "minimum": 0, "maximum": 1},
            }
        )
    return {
        "$schema": _JSON_SCHEMA,
        "type": "object",
        "required": required,
        "properties": properties,
        "additionalProperties": False,
    }


def _definition(
    profile: TemplateProfile,
    title: str,
    *,
    model: bool = False,
    action: bool = False,
    capabilities: tuple[str, ...],
    requirement_kinds: tuple[DeploymentRequirementKind, ...],
    targets: Mapping[str, tuple[TemplateParameterTarget, ...]],
    synthetic_expansion_digest: str = "sha256:" + "0" * 64,
) -> TemplateDefinition:
    defaults: dict[str, Any] = {
        "unit": "uV",
        "channel_count": 2,
        "sample_rate_hz": 100.0,
        "window_samples": 4,
        "step_samples": 2,
        "event_kind": "stimulus.target",
        "threshold": 0.0,
        "shadow_threshold": 0.5,
        "positive_label": "positive",
        "negative_label": "negative",
    }
    scientific = dict(defaults)
    if model:
        defaults["model_version_spec"] = "~=0.1.0"
    if action:
        defaults.update(
            action_capability="simulated.feedback",
            action_intensity=0.25,
        )
        scientific.update(
            action_capability="simulated.feedback",
            action_intensity=0.25,
        )
    used = set(targets) | ({"model_plugin_id", "model_manifest_digest"} if model else set())
    used |= {"model_version_spec"} if model else set()
    schema = _parameter_schema(model=model, action=action)
    schema["properties"] = {
        key: value for key, value in schema["properties"].items() if key in used
    }
    parameter_defaults = {key: value for key, value in defaults.items() if key in used}
    scientific_defaults = {
        key: value for key, value in scientific.items() if key in used
    }
    return TemplateDefinition(
        template_id=f"eegle.template.{profile.value}",
        version="1.0.0",
        title=title,
        profile=profile,
        parameter_schema=schema,
        parameter_defaults=parameter_defaults,
        scientific_defaults=scientific_defaults,
        parameter_targets=targets,
        generated_schemas={
            "draft": EXPERIMENT_DRAFT_SCHEMA_ID,
            "protocol": PROTOCOL_SPEC_SCHEMA,
            "suite": SUITE_SPEC_SCHEMA,
            "deployment_requirements": DEPLOYMENT_REQUIREMENTS_SCHEMA_ID,
        },
        required_capabilities=capabilities,
        deployment_requirement_kinds=requirement_kinds,
        synthetic_fixture=f"phase7.fixture.{profile.value}.v1",
        synthetic_expansion_digest=synthetic_expansion_digest,
    )


_SOURCE_STORAGE = (
    DeploymentRequirementKind.SOURCE_BINDING,
    DeploymentRequirementKind.STORAGE,
    DeploymentRequirementKind.CLOCK_MAPPING,
)
_MODEL_REQUIREMENTS = (*_SOURCE_STORAGE, DeploymentRequirementKind.MODEL_ARTIFACT)


def _stream_targets(index: int = 0) -> dict[str, tuple[TemplateParameterTarget, ...]]:
    base = f"/streams/{index}/contract"
    return {
        "unit": (_target(CanonicalArtifact.SUITE, f"{base}/unit"),),
        "channel_count": (
            _target(CanonicalArtifact.SUITE, f"{base}/channel_count"),
        ),
        "sample_rate_hz": (
            _target(CanonicalArtifact.SUITE, f"{base}/nominal_rate_hz"),
        ),
    }


def _model_targets(*indices: int) -> dict[str, tuple[TemplateParameterTarget, ...]]:
    return {
        "model_plugin_id": tuple(
            _target(CanonicalArtifact.SUITE, f"/components/{index}/plugin_id")
            for index in indices
        ),
        "model_version_spec": tuple(
            _target(CanonicalArtifact.SUITE, f"/components/{index}/version_spec")
            for index in indices
        ),
        "model_manifest_digest": tuple(
            _target(CanonicalArtifact.SUITE, f"/model_uses/{index}/manifest_digest")
            for index in range(len(indices))
        ),
        "threshold": tuple(
            _target(CanonicalArtifact.SUITE, f"/components/{index}/config/threshold")
            for index in indices[:1]
        ),
        "positive_label": tuple(
            _target(
                CanonicalArtifact.SUITE,
                f"/components/{index}/config/positive_label",
            )
            for index in indices
        ),
        "negative_label": tuple(
            _target(
                CanonicalArtifact.SUITE,
                f"/components/{index}/config/negative_label",
            )
            for index in indices
        ),
    }


BUILTIN_TEMPLATE_CATALOG = TemplateCatalog(
    (
        _definition(
            TemplateProfile.CONTINUOUS_RECORDING,
            "Continuous EEG recording",
            synthetic_expansion_digest="sha256:f35122f4ccf6d671e82608f84752a8b227441e7182f7eb05bc78440058339932",
            capabilities=("source:dense", "storage:evidence"),
            requirement_kinds=_SOURCE_STORAGE,
            targets=_stream_targets(),
        ),
        _definition(
            TemplateProfile.EEG_EVENTS_RECORDING,
            "EEG plus event recording",
            synthetic_expansion_digest="sha256:43b1d7129653185bec1e78ec132398916b6cd3f91ac95de13432442fea51e833",
            capabilities=("source:dense", "source:sparse", "storage:evidence"),
            requirement_kinds=_SOURCE_STORAGE,
            targets={
                **_stream_targets(),
                "event_kind": (
                    _target(
                        CanonicalArtifact.SUITE,
                        "/streams/1/contract/event_kinds/0",
                    ),
                ),
            },
        ),
        _definition(
            TemplateProfile.CONTINUOUS_OBSERVER,
            "Continuous model observer",
            synthetic_expansion_digest="sha256:4ba5bbe3104cf77555e11cd2d4438ca9e21913ee6b86be16baba93122c4c86cf",
            model=True,
            capabilities=("model:manifest", "source:dense", "storage:evidence"),
            requirement_kinds=_MODEL_REQUIREMENTS,
            targets={
                **_stream_targets(),
                **_model_targets(3),
                "window_samples": (
                    _target(CanonicalArtifact.SUITE, "/components/2/config/window_samples"),
                ),
                "step_samples": (
                    _target(CanonicalArtifact.SUITE, "/components/2/config/step_samples"),
                ),
                "threshold": (
                    _target(CanonicalArtifact.SUITE, "/components/3/config/threshold"),
                ),
            },
        ),
        _definition(
            TemplateProfile.EVENT_LOCKED_MODEL,
            "Event-locked model observer",
            synthetic_expansion_digest="sha256:305cc6a05d7f48a4c32e3ed8d16c84979ee928d350d890d963c375bb09d94321",
            model=True,
            capabilities=("model:manifest", "source:dense", "source:sparse"),
            requirement_kinds=_MODEL_REQUIREMENTS,
            targets={
                **_stream_targets(),
                **_model_targets(3),
                "event_kind": (
                    _target(CanonicalArtifact.SUITE, "/streams/1/contract/event_kinds/0"),
                    _target(CanonicalArtifact.SUITE, "/components/2/config/event_kinds/0"),
                ),
            },
        ),
        _definition(
            TemplateProfile.PRIMARY_SHADOW_COMPARISON,
            "Primary and shadow model comparison",
            synthetic_expansion_digest="sha256:0571dcb0751f3be66cec6b9de83db30200cf6ff66cfeca28bc513796ab946d9e",
            model=True,
            capabilities=("model:manifest", "source:dense", "source:sparse"),
            requirement_kinds=_MODEL_REQUIREMENTS,
            targets={
                **_stream_targets(),
                **_model_targets(3, 4),
                "event_kind": (
                    _target(CanonicalArtifact.SUITE, "/streams/1/contract/event_kinds/0"),
                    _target(CanonicalArtifact.SUITE, "/components/2/config/event_kinds/0"),
                ),
                "shadow_threshold": (
                    _target(CanonicalArtifact.SUITE, "/components/4/config/threshold"),
                ),
            },
        ),
        _definition(
            TemplateProfile.CALIBRATION_VALIDATION,
            "Calibration followed by locked validation",
            synthetic_expansion_digest="sha256:d4d641c14612b25f01bc3e60ff8759b80b9a7f44d86d9eeb8036e4822c85e08d",
            model=True,
            capabilities=("artifact:calibration", "model:manifest", "source:dense"),
            requirement_kinds=_MODEL_REQUIREMENTS,
            targets={
                **_stream_targets(),
                **_model_targets(4),
                "window_samples": (
                    _target(CanonicalArtifact.SUITE, "/components/3/config/window_samples"),
                    _target(CanonicalArtifact.SUITE, "/components/3/config/step_samples"),
                ),
            },
        ),
        _definition(
            TemplateProfile.DELAYED_OUTCOME_ADAPTATION,
            "Delayed-outcome adaptation",
            synthetic_expansion_digest="sha256:348bca171d79d72b714f23fbc982f82c7e55e8b44eb7689bfd630e617450ec0c",
            capabilities=("outcome:delayed", "source:sparse", "state:checkpoint"),
            requirement_kinds=_SOURCE_STORAGE,
            targets={
                "event_kind": (
                    _target(CanonicalArtifact.SUITE, "/streams/0/contract/event_kinds/0"),
                    _target(CanonicalArtifact.SUITE, "/components/1/config/event_kind"),
                ),
            },
        ),
        _definition(
            TemplateProfile.SIMULATED_CLOSED_LOOP,
            "Simulated closed-loop action",
            synthetic_expansion_digest="sha256:5521966d3a1e8f2aac3d37f7d0eacf8fc11039af61213032223f82b1d6471161",
            model=True,
            action=True,
            capabilities=(
                "authorization:simulation",
                "model:manifest",
                "source:dense",
                "source:sparse",
            ),
            requirement_kinds=(
                *_MODEL_REQUIREMENTS,
                DeploymentRequirementKind.AUTHORIZATION,
            ),
            targets={
                **_stream_targets(),
                **_model_targets(3),
                "event_kind": (
                    _target(CanonicalArtifact.SUITE, "/streams/1/contract/event_kinds/0"),
                    _target(CanonicalArtifact.SUITE, "/components/2/config/event_kinds/0"),
                ),
                "action_capability": (
                    _target(CanonicalArtifact.SUITE, "/components/4/config/capability"),
                    _target(CanonicalArtifact.SUITE, "/components/5/action_capabilities/0"),
                ),
                "action_intensity": (
                    _target(CanonicalArtifact.SUITE, "/components/4/config/parameters/intensity"),
                ),
            },
        ),
    )
)


def builtin_templates() -> tuple[TemplateDefinition, ...]:
    return BUILTIN_TEMPLATE_CATALOG.templates
