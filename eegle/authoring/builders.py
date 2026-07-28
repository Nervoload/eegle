"""Persistent typed authoring facade over exact templates and draft lowering."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from eegle._validation import freeze_json, require_digest, thaw_json
from eegle.authoring.contracts import SourceKind, SourceLocation
from eegle.authoring.drafts import (
    DeploymentRequirements,
    DraftIssue,
    DraftLoweringError,
    ExperimentDraft,
)
from eegle.authoring.provenance import AuthoringProvenance, DraftSourceMap
from eegle.authoring.schemas import (
    TEMPLATE_AUTHORING_SCHEMA_ID,
    validate_template_authoring_payload,
)
from eegle.authoring.templates import (
    BUILTIN_TEMPLATE_CATALOG,
    TemplateDefinition,
    TemplateExpansion,
    TemplateExpansionLock,
    create_template_draft,
    expand_template,
)
from eegle.compiler import canonical_hash, canonical_json_bytes, compile_suite
from eegle.plugins import PluginRegistry
from eegle.specs import DeploymentSpec, ProtocolSpec, SchemaValidationError, SuiteSpec

if TYPE_CHECKING:
    from eegle.compiler import CompilationResult
    from eegle.models import ModelManifest


AUTHORING_EXPORT_SCHEMA_ID = "eegle.authored_experiment.v1"
AUTHORING_PROJECT_SCHEMA_ID = "eegle.authoring_project.v1"


@dataclass(frozen=True, slots=True)
class WrittenAuthoringProject:
    root: Path
    files: Mapping[str, Path]
    manifest_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        normalized = {str(name): Path(path) for name, path in self.files.items()}
        object.__setattr__(self, "files", MappingProxyType(normalized))
        object.__setattr__(
            self,
            "manifest_digest",
            require_digest(self.manifest_digest, "authoring project manifest_digest"),
        )


@dataclass(frozen=True, slots=True)
class AuthoredExperiment:
    """Generated authoring artifacts; compilation still uses the normal compiler."""

    expansion: TemplateExpansion

    def __post_init__(self) -> None:
        if not isinstance(self.expansion, TemplateExpansion):
            raise TypeError("authored experiments require a TemplateExpansion")

    @property
    def draft(self) -> ExperimentDraft:
        return self.expansion.draft

    @property
    def protocol(self) -> ProtocolSpec:
        return self.expansion.lowered.protocol

    @property
    def suite(self) -> SuiteSpec:
        return self.expansion.lowered.suite

    @property
    def requirements(self) -> DeploymentRequirements:
        return self.expansion.lowered.deployment_requirements

    @property
    def provenance(self) -> AuthoringProvenance:
        return self.expansion.lowered.provenance

    @property
    def lock(self) -> TemplateExpansionLock:
        return self.expansion.lock

    @property
    def defaults(self) -> Mapping[str, Any]:
        return self.expansion.template.parameter_defaults

    def canonical_specs(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol.to_payload(),
            "suite": self.suite.to_payload(),
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": AUTHORING_EXPORT_SCHEMA_ID,
            "draft": self.draft.to_payload(),
            "template_definition": self.expansion.template.to_payload(),
            "template_expansion_lock": self.lock.to_payload(),
            "protocol": self.protocol.to_payload(),
            "suite": self.suite.to_payload(),
            "deployment_requirements": self.requirements.to_payload(),
            "authoring_provenance": self.provenance.to_payload(),
        }

    def canonical_json(self) -> str:
        return canonical_json_bytes(self.to_payload()).decode("utf-8")

    def compile(
        self,
        deployment: DeploymentSpec,
        registry: PluginRegistry,
        *,
        model_manifests: Mapping[str, ModelManifest] | None = None,
    ) -> CompilationResult:
        """Delegate unchanged canonical values to the sole compiler."""

        if not isinstance(deployment, DeploymentSpec):
            raise TypeError("compile requires a DeploymentSpec")
        if not isinstance(registry, PluginRegistry):
            raise TypeError("compile requires a PluginRegistry")
        return compile_suite(
            self.protocol,
            self.suite,
            deployment,
            registry,
            model_manifests=model_manifests,
        )

    def write_project(
        self,
        root: str | Path,
        *,
        overwrite: bool = False,
    ) -> WrittenAuthoringProject:
        """Write inspectable authoring artifacts without compiling or executing."""

        target = Path(root)
        payloads = {
            "experiment.draft.json": self.draft.to_payload(),
            "template-definition.json": self.expansion.template.to_payload(),
            "template-expansion-lock.json": self.lock.to_payload(),
            "protocol.json": self.protocol.to_payload(),
            "suite.json": self.suite.to_payload(),
            "deployment-requirements.json": self.requirements.to_payload(),
            "authoring-provenance.json": self.provenance.to_payload(),
        }
        manifest = {
            "schema": AUTHORING_PROJECT_SCHEMA_ID,
            "draft_id": self.draft.draft_id,
            "draft_revision": self.draft.revision,
            "template_manifest_digest": self.expansion.template.manifest_digest,
            "expansion_digest": self.expansion.expansion_digest,
            "files": {
                name: {
                    "schema": payload.get("schema"),
                    "canonical_digest": canonical_hash(payload),
                }
                for name, payload in sorted(payloads.items())
            },
        }
        all_payloads = {**payloads, "authoring-project.json": manifest}
        existing = sorted(name for name in all_payloads if (target / name).exists())
        if existing and not overwrite:
            raise FileExistsError(
                "authoring project files already exist: " + ", ".join(existing)
            )
        target.mkdir(parents=True, exist_ok=True)
        written: dict[str, Path] = {}
        for name, payload in sorted(all_payloads.items()):
            written[name] = _atomic_json(target / name, payload)
        return WrittenAuthoringProject(
            target,
            written,
            canonical_hash(manifest),
        )


@dataclass(frozen=True, slots=True)
class ExperimentBuilder:
    """Immutable, autocomplete-friendly parameters for one exact template."""

    draft_id: str
    template_id: str
    template_version: str = "1.0.0"
    revision: int = 1
    parameters: Mapping[str, Any] = field(default_factory=dict)
    parameter_sources: Mapping[str, SourceLocation] = field(default_factory=dict)

    def __post_init__(self) -> None:
        template = BUILTIN_TEMPLATE_CATALOG.resolve(
            self.template_id,
            self.template_version,
        )
        revision = int(self.revision)
        if revision <= 0:
            raise ValueError("builder revision must be positive")
        object.__setattr__(self, "revision", revision)
        parameters = freeze_json(self.parameters or {})
        unknown = sorted(set(parameters) - set(template.parameter_schema["properties"]))
        if unknown:
            raise ValueError(
                "unknown parameters for exact template revision: "
                + ", ".join(unknown)
            )
        object.__setattr__(self, "parameters", parameters)
        sources = dict(self.parameter_sources or {})
        if set(sources) - set(parameters):
            raise ValueError("parameter sources must reference explicit parameters")
        if not all(isinstance(value, SourceLocation) for value in sources.values()):
            raise TypeError("parameter sources must be SourceLocation values")
        object.__setattr__(self, "parameter_sources", MappingProxyType(sources))
        create_template_draft(
            template.template_id,
            template.version,
            draft_id=self.draft_id,
            parameters=parameters,
            revision=revision,
        )

    @classmethod
    def from_template(
        cls,
        draft_id: str,
        template_id: str,
        version: str = "1.0.0",
    ) -> ExperimentBuilder:
        return cls(draft_id, template_id, version)

    @classmethod
    def continuous_recording(cls, draft_id: str) -> ExperimentBuilder:
        return cls.from_template(
            draft_id,
            "eegle.template.continuous_recording",
        )

    @classmethod
    def eeg_events_recording(cls, draft_id: str) -> ExperimentBuilder:
        return cls.from_template(
            draft_id,
            "eegle.template.eeg_events_recording",
        )

    @classmethod
    def primary_shadow_comparison(
        cls,
        draft_id: str,
        *,
        model_plugin_id: str,
        model_manifest_digest: str,
    ) -> ExperimentBuilder:
        return cls.from_template(
            draft_id,
            "eegle.template.primary_shadow_comparison",
        ).model(model_plugin_id, model_manifest_digest)

    @classmethod
    def continuous_observer(
        cls,
        draft_id: str,
        *,
        model_plugin_id: str,
        model_manifest_digest: str,
    ) -> ExperimentBuilder:
        return cls.from_template(
            draft_id,
            "eegle.template.continuous_observer",
        ).model(model_plugin_id, model_manifest_digest)

    @classmethod
    def event_locked_model(
        cls,
        draft_id: str,
        *,
        model_plugin_id: str,
        model_manifest_digest: str,
    ) -> ExperimentBuilder:
        return cls.from_template(
            draft_id,
            "eegle.template.event_locked_model",
        ).model(model_plugin_id, model_manifest_digest)

    @classmethod
    def calibration_validation(
        cls,
        draft_id: str,
        *,
        model_plugin_id: str,
        model_manifest_digest: str,
    ) -> ExperimentBuilder:
        return cls.from_template(
            draft_id,
            "eegle.template.calibration_validation",
        ).model(model_plugin_id, model_manifest_digest)

    @classmethod
    def delayed_outcome_adaptation(cls, draft_id: str) -> ExperimentBuilder:
        return cls.from_template(
            draft_id,
            "eegle.template.delayed_outcome_adaptation",
        )

    @classmethod
    def simulated_closed_loop(
        cls,
        draft_id: str,
        *,
        model_plugin_id: str,
        model_manifest_digest: str,
    ) -> ExperimentBuilder:
        return cls.from_template(
            draft_id,
            "eegle.template.simulated_closed_loop",
        ).model(model_plugin_id, model_manifest_digest)

    @property
    def template(self) -> TemplateDefinition:
        return BUILTIN_TEMPLATE_CATALOG.resolve(
            self.template_id,
            self.template_version,
        )

    @property
    def defaults(self) -> Mapping[str, Any]:
        return self.template.parameter_defaults

    @property
    def required_capabilities(self) -> tuple[str, ...]:
        return self.template.required_capabilities

    @property
    def draft(self) -> ExperimentDraft:
        return create_template_draft(
            self.template_id,
            self.template_version,
            draft_id=self.draft_id,
            parameters=self.parameters,
            revision=self.revision,
        )

    @property
    def source_map(self) -> DraftSourceMap:
        locations = {
            f"/template/parameters/{_escape_pointer(name)}": source
            for name, source in self.parameter_sources.items()
        }
        return DraftSourceMap(
            locations,
            fallback=SourceLocation(
                SourceKind.PYTHON,
                symbol=f"ExperimentBuilder[{self.draft_id}]",
            ),
        )

    def with_parameter(
        self,
        name: str,
        value: Any,
        *,
        source: SourceLocation | None = None,
    ) -> ExperimentBuilder:
        if name not in self.template.parameter_schema["properties"]:
            raise ValueError(f"unknown parameter for {self.template_id}: {name}")
        parameters = {**thaw_json(self.parameters), str(name): thaw_json(freeze_json(value))}
        sources = dict(self.parameter_sources)
        sources[str(name)] = source or SourceLocation(
            SourceKind.PYTHON,
            symbol=f"ExperimentBuilder.{name}",
        )
        return replace(self, parameters=parameters, parameter_sources=sources)

    def signal(
        self,
        *,
        unit: str | None = None,
        channel_count: int | None = None,
        sample_rate_hz: float | None = None,
    ) -> ExperimentBuilder:
        values = {
            "unit": unit,
            "channel_count": channel_count,
            "sample_rate_hz": sample_rate_hz,
        }
        return self._with_present(values)

    def window(
        self,
        *,
        window_samples: int | None = None,
        step_samples: int | None = None,
        event_kind: str | None = None,
    ) -> ExperimentBuilder:
        values = {
            "window_samples": window_samples,
            "step_samples": step_samples,
            "event_kind": event_kind,
        }
        return self._with_present(values)

    def events(self, event_kind: str) -> ExperimentBuilder:
        return self.with_parameter("event_kind", event_kind)

    def model(
        self,
        plugin_id: str,
        manifest_digest: str,
        *,
        version_spec: str | None = None,
        threshold: float | None = None,
        shadow_threshold: float | None = None,
        positive_label: str | None = None,
        negative_label: str | None = None,
    ) -> ExperimentBuilder:
        values = {
            "model_plugin_id": plugin_id,
            "model_manifest_digest": manifest_digest,
            "model_version_spec": version_spec,
            "threshold": threshold,
            "shadow_threshold": shadow_threshold,
            "positive_label": positive_label,
            "negative_label": negative_label,
        }
        return self._with_present(values)

    def action(
        self,
        capability: str,
        *,
        intensity: float | None = None,
    ) -> ExperimentBuilder:
        return self._with_present(
            {
                "action_capability": capability,
                "action_intensity": intensity,
            }
        )

    def to_source_payload(self) -> dict[str, Any]:
        payload = {
            "schema": TEMPLATE_AUTHORING_SCHEMA_ID,
            "draft_id": self.draft_id,
            "revision": self.revision,
            "template": {
                "template_id": self.template_id,
                "version": self.template_version,
                "parameters": thaw_json(self.parameters),
            },
        }
        validate_template_authoring_payload(payload)
        return payload

    @classmethod
    def from_source_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        source_map: DraftSourceMap | None = None,
    ) -> ExperimentBuilder:
        validate_template_authoring_payload(payload)
        template = payload["template"]
        locations: dict[str, SourceLocation] = {}
        if source_map is not None:
            for name in template["parameters"]:
                path = f"/template/parameters/{_escape_pointer(str(name))}"
                locations[str(name)] = source_map.source_for(path)
        return cls(
            draft_id=str(payload["draft_id"]),
            revision=int(payload.get("revision", 1)),
            template_id=str(template["template_id"]),
            template_version=str(template["version"]),
            parameters=dict(template["parameters"]),
            parameter_sources=locations,
        )

    def canonical_source_json(self) -> str:
        return canonical_json_bytes(self.to_source_payload()).decode("utf-8")

    def build(self) -> AuthoredExperiment:
        try:
            expansion = expand_template(
                self.template_id,
                self.template_version,
                draft_id=self.draft_id,
                revision=self.revision,
                parameters=self.parameters,
                source_map=self.source_map,
            )
        except SchemaValidationError as exc:
            path = _parameter_error_pointer(exc.path)
            raise DraftLoweringError(
                (
                    DraftIssue(
                        "authoring.template_parameters",
                        path,
                        str(exc),
                        self.source_map.source_for(path),
                    ),
                )
            ) from exc
        return AuthoredExperiment(expansion)

    def _with_present(self, values: Mapping[str, Any | None]) -> ExperimentBuilder:
        result = self
        for name, value in values.items():
            if value is not None:
                result = result.with_parameter(name, value)
        return result


def _parameter_error_pointer(schema_path: str) -> str:
    base = "/template/parameters"
    if schema_path == "$":
        return base
    value = schema_path[1:]
    parts: list[str] = []
    while value:
        if value.startswith("."):
            value = value[1:]
            end = len(value)
            for marker in (".", "["):
                index = value.find(marker)
                if index >= 0:
                    end = min(end, index)
            parts.append(value[:end])
            value = value[end:]
        elif value.startswith("["):
            end = value.index("]")
            parts.append(value[1:end])
            value = value[end + 1 :]
        else:
            break
    return base + "".join(f"/{_escape_pointer(part)}" for part in parts)


def _escape_pointer(value: str) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> Path:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path
