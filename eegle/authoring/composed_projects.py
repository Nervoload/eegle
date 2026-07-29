"""Composed results, compilation handoff, and non-overwriting project export."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Mapping

from eegle._validation import require_digest
from eegle.authoring.composition import ExperimentDesign
from eegle.authoring.drafts import DeploymentRequirements, LoweredExperiment
from eegle.authoring.provenance import AuthoringProvenance
from eegle.compiler import canonical_hash, compile_suite
from eegle.plugins import PluginRegistry
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec

if TYPE_CHECKING:
    from eegle.compiler import CompilationResult
    from eegle.models import ModelManifest


COMPOSED_EXPERIMENT_SCHEMA_ID = "eegle.composed_experiment.v1"
COMPOSED_PROJECT_SCHEMA_ID = "eegle.composed_authoring_project.v1"


@dataclass(frozen=True, slots=True)
class WrittenComposedProject:
    root: Path
    files: Mapping[str, Path]
    manifest_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "files", MappingProxyType({str(k): Path(v) for k, v in self.files.items()}))
        object.__setattr__(self, "manifest_digest", require_digest(self.manifest_digest, "manifest_digest"))


@dataclass(frozen=True, slots=True)
class ComposedExperiment:
    design: ExperimentDesign
    lowered: LoweredExperiment

    def __post_init__(self) -> None:
        if not isinstance(self.design, ExperimentDesign) or not isinstance(self.lowered, LoweredExperiment):
            raise TypeError("composed experiments require a design and lowered result")

    @property
    def protocol(self) -> ProtocolSpec:
        return self.lowered.protocol

    @property
    def suite(self) -> SuiteSpec:
        return self.lowered.suite

    @property
    def requirements(self) -> DeploymentRequirements:
        return self.lowered.deployment_requirements

    @property
    def provenance(self) -> AuthoringProvenance:
        return self.lowered.provenance

    def canonical_specs(self) -> dict[str, Any]:
        return {"protocol": self.protocol.to_payload(), "suite": self.suite.to_payload()}

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": COMPOSED_EXPERIMENT_SCHEMA_ID,
            "design": self.design.to_payload(),
            "protocol": self.protocol.to_payload(),
            "suite": self.suite.to_payload(),
            "deployment_requirements": self.requirements.to_payload(),
            "authoring_provenance": self.provenance.to_payload(),
        }

    def compile(
        self,
        deployment: DeploymentSpec,
        registry: PluginRegistry,
        *,
        model_manifests: Mapping[str, "ModelManifest"] | None = None,
    ) -> "CompilationResult":
        return compile_suite(
            self.protocol,
            self.suite,
            deployment,
            registry,
            model_manifests=model_manifests,
        )

    def write_project(self, root: str | Path, *, overwrite: bool = False) -> WrittenComposedProject:
        target = Path(root)
        payloads = {
            "experiment.design.json": self.design.to_payload(),
            "protocol.json": self.protocol.to_payload(),
            "suite.json": self.suite.to_payload(),
            "deployment-requirements.json": self.requirements.to_payload(),
            "authoring-provenance.json": self.provenance.to_payload(),
        }
        manifest = {
            "schema": COMPOSED_PROJECT_SCHEMA_ID,
            "experiment_id": self.design.experiment_id,
            "revision": self.design.revision,
            "design_digest": self.design.design_digest,
            "files": {
                name: {"schema": payload.get("schema"), "canonical_digest": canonical_hash(payload)}
                for name, payload in sorted(payloads.items())
            },
        }
        all_payloads = {**payloads, "authoring-project.json": manifest}
        existing = sorted(name for name in all_payloads if (target / name).exists())
        if existing and not overwrite:
            raise FileExistsError("composed authoring project files already exist: " + ", ".join(existing))
        target.mkdir(parents=True, exist_ok=True)
        files = {name: _atomic_json(target / name, payload) for name, payload in sorted(all_payloads.items())}
        return WrittenComposedProject(target, files, canonical_hash(manifest))


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


__all__ = [
    "COMPOSED_EXPERIMENT_SCHEMA_ID",
    "COMPOSED_PROJECT_SCHEMA_ID",
    "ComposedExperiment",
    "WrittenComposedProject",
]

