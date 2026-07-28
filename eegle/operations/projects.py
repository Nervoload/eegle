"""Artifact-oriented project services shared by Python callers and the CLI."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from eegle._domain import ComponentKind
from eegle._validation import freeze_json, require_digest, require_identifier, thaw_json
from eegle.authoring import (
    AuthoredExperiment,
    DraftLoweringError,
    DraftSourceMap,
    ExperimentBuilder,
    SourceKind,
    SourceLocation,
)
from eegle.compiler import (
    CompilationError,
    ExecutionLock,
    ExecutionPlan,
    canonical_hash,
    read_lock,
    read_plan,
    write_lock,
    write_plan,
)
from eegle.operations.contracts import (
    ExitCode,
    OperationCategory,
    OperationDiagnostic,
    OperationError,
)
from eegle.operations.explanations import (
    diagnose_authoring_failure,
    diagnose_compilation_failure,
    diff_authored_experiments,
    explain_authored_experiment,
)
from eegle.operations.discovery import (
    DeploymentProposal,
    DeploymentSelection,
    DetectionReport,
    propose_deployment,
)
from eegle.operations.preflight import (
    PreflightReport,
    RehearsalReport,
    assert_rehearsal_safe,
    preflight,
    rehearsal_fault_outcomes,
)
from eegle.plugins import PluginRegistry
from eegle.recording import (
    EvidenceRecord,
    Session,
    SessionStatus,
    persist_engine_run,
)
from eegle.runtime import EngineStatus, ExecutionEngine
from eegle.specs import (
    ClockMappingBinding,
    ClockMappingStrategy,
    ComponentBindingSpec,
    DeploymentSpec,
    Placement,
    ResourceSpec,
    StorageBinding,
    StreamBinding,
)
from eegle.streams import (
    ChannelSpec,
    ContentKind,
    DenseSampleBatch,
    MissingDataPolicy,
    RateModel,
    StreamSpec,
    TimePoint,
)


PROJECT_MANIFEST_SCHEMA_ID = "eegle.project.v1"
PROJECT_RESULT_SCHEMA_ID = "eegle.project_result.v1"
PROJECT_GRAPH_SCHEMA_ID = "eegle.project_graph.v1"
PROJECT_MANIFEST_NAME = "eegle-project.json"

AUTHORING_SOURCE_URI = "authoring/experiment.json"
GENERATED_ROOT_URI = "generated"
SIMULATION_DEPLOYMENT_URI = "deployments/simulation.json"
AUTHORING_EXPLANATION_URI = "explanations/authoring.json"
DETECTION_ROOT_URI = "detections"
DEPLOYMENT_PROPOSAL_ROOT_URI = "deployments/proposals"
PREFLIGHT_ROOT_URI = "preflights"
REHEARSAL_ROOT_URI = "rehearsals"


@dataclass(frozen=True, slots=True)
class ProjectArtifact:
    role: str
    uri: str
    digest: str
    immutable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", require_identifier(self.role, "project artifact role"))
        object.__setattr__(self, "uri", _safe_relative(self.uri, "project artifact URI"))
        object.__setattr__(self, "digest", require_digest(self.digest, "project artifact digest"))
        if not isinstance(self.immutable, bool):
            raise TypeError("project artifact immutable flag must be boolean")

    def to_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "uri": self.uri,
            "digest": self.digest,
            "immutable": self.immutable,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProjectArtifact":
        return cls(
            role=str(payload["role"]),
            uri=str(payload["uri"]),
            digest=str(payload["digest"]),
            immutable=payload.get("immutable", False),
        )


@dataclass(frozen=True, slots=True)
class ProjectManifest:
    project_id: str
    artifacts: tuple[ProjectArtifact, ...]
    session_uris: tuple[str, ...] = ()
    schema: str = PROJECT_MANIFEST_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != PROJECT_MANIFEST_SCHEMA_ID:
            raise ValueError(f"unsupported project manifest schema: {self.schema}")
        object.__setattr__(self, "project_id", require_identifier(self.project_id, "project_id"))
        artifacts = tuple(sorted(self.artifacts, key=lambda value: value.role))
        if len({value.role for value in artifacts}) != len(artifacts):
            raise ValueError("project artifact roles must be unique")
        object.__setattr__(self, "artifacts", artifacts)
        sessions = tuple(_safe_relative(value, "session URI") for value in self.session_uris)
        if len(sessions) != len(set(sessions)):
            raise ValueError("project session URIs must be unique")
        object.__setattr__(self, "session_uris", sessions)

    @property
    def manifest_hash(self) -> str:
        return canonical_hash(self.content_payload())

    def content_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "project_id": self.project_id,
            "artifacts": [value.to_payload() for value in self.artifacts],
            "session_uris": list(self.session_uris),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self.content_payload()
        payload["manifest_hash"] = self.manifest_hash
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProjectManifest":
        manifest = cls(
            schema=str(payload.get("schema", PROJECT_MANIFEST_SCHEMA_ID)),
            project_id=str(payload["project_id"]),
            artifacts=tuple(
                ProjectArtifact.from_payload(value)
                for value in payload.get("artifacts", ())
            ),
            session_uris=tuple(str(value) for value in payload.get("session_uris", ())),
        )
        if payload.get("manifest_hash") != manifest.manifest_hash:
            raise ValueError("project manifest hash mismatch")
        return manifest

    def artifact(self, role: str) -> ProjectArtifact:
        selected = str(role)
        try:
            return next(value for value in self.artifacts if value.role == selected)
        except StopIteration as exc:
            raise KeyError(f"project has no {selected} artifact") from exc

    def replacing_artifacts(self, *values: ProjectArtifact) -> "ProjectManifest":
        by_role = {value.role: value for value in self.artifacts}
        by_role.update({value.role: value for value in values})
        return replace(self, artifacts=tuple(by_role.values()))

    def registering_session(self, uri: str) -> "ProjectManifest":
        normalized = _safe_relative(uri, "session URI")
        if normalized in self.session_uris:
            return self
        return replace(self, session_uris=(*self.session_uris, normalized))


@dataclass(frozen=True, slots=True)
class ExperimentProject:
    root: Path
    manifest: ProjectManifest

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())
        if not isinstance(self.manifest, ProjectManifest):
            raise TypeError("experiment project requires a ProjectManifest")

    def path_for(self, role: str) -> Path:
        return self.root / self.manifest.artifact(role).uri

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": PROJECT_RESULT_SCHEMA_ID,
            "project_root": str(self.root),
            "manifest": self.manifest.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class ProjectCompilation:
    project: ExperimentProject
    plan: ExecutionPlan
    lock: ExecutionLock
    explanation: Mapping[str, Any]
    deployment_role: str = "simulation_deployment"

    def __post_init__(self) -> None:
        object.__setattr__(self, "explanation", freeze_json(self.explanation))
        object.__setattr__(
            self,
            "deployment_role",
            require_identifier(self.deployment_role, "deployment artifact role"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.project_compilation.v1",
            "project_root": str(self.project.root),
            "plan_hash": self.plan.plan_hash,
            "lock_hash": self.lock.lock_hash,
            "plan_uri": self.project.manifest.artifact("execution_plan").uri,
            "lock_uri": self.project.manifest.artifact("execution_lock").uri,
            "explanation_uri": self.project.manifest.artifact("locked_explanation").uri,
            "deployment_role": self.deployment_role,
            "deployment_uri": self.project.manifest.artifact(self.deployment_role).uri,
            "diagnostics": thaw_json(self.explanation).get("diagnostics", []),
        }


@dataclass(frozen=True, slots=True)
class ProjectDeploymentProposal:
    project: ExperimentProject
    report: DetectionReport
    proposal: DeploymentProposal

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.project_deployment_proposal.v1",
            "project_root": str(self.project.root),
            "detection_report_hash": self.report.report_hash,
            "proposal_hash": self.proposal.proposal_hash,
            "deployment_hash": self.proposal.deployment.spec_hash,
            "detection_report_uri": self.project.manifest.artifact(
                "detection_report"
            ).uri,
            "deployment_uri": self.project.manifest.artifact(
                "deployment_proposal"
            ).uri,
            "proposal_uri": self.project.manifest.artifact(
                "deployment_proposal_provenance"
            ).uri,
            "authorization_inferred": self.proposal.authorization_inferred,
        }


@dataclass(frozen=True, slots=True)
class ProjectRun:
    session_root: Path
    session_id: str
    bundle_id: str
    plan_hash: str
    status: EngineStatus
    evidence_record_count: int
    kind: str = "run"

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_root", Path(self.session_root).expanduser().resolve())
        object.__setattr__(self, "session_id", require_identifier(self.session_id, "session_id"))
        object.__setattr__(self, "bundle_id", require_identifier(self.bundle_id, "bundle_id"))
        object.__setattr__(self, "plan_hash", require_digest(self.plan_hash, "plan_hash"))
        object.__setattr__(self, "status", EngineStatus(self.status))
        object.__setattr__(self, "kind", require_identifier(self.kind, "run kind"))

    @property
    def successful(self) -> bool:
        return self.status == EngineStatus.COMPLETE

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.project_run.v1",
            "kind": self.kind,
            "session_root": str(self.session_root),
            "session_id": self.session_id,
            "bundle_id": self.bundle_id,
            "plan_hash": self.plan_hash,
            "status": self.status.value,
            "successful": self.successful,
            "evidence_record_count": self.evidence_record_count,
        }


@dataclass(frozen=True, slots=True)
class LockedRehearsal:
    run: ProjectRun
    preflight: PreflightReport
    report: RehearsalReport

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.locked_rehearsal.v1",
            "run": self.run.to_payload(),
            "preflight": self.preflight.to_payload(),
            "report": self.report.to_payload(),
        }


def create_project(
    root: str | Path,
    *,
    project_id: str,
    template_id: str = "eegle.template.continuous_recording",
    template_version: str = "1.0.0",
    parameters: Mapping[str, Any] | None = None,
) -> ExperimentProject:
    """Create one non-overwriting, self-contained simulation project."""

    target = Path(root).expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"project directory is not empty: {target}")
    try:
        builder = ExperimentBuilder(
            draft_id=project_id,
            template_id=template_id,
            template_version=template_version,
            parameters=parameters or {},
            parameter_sources={
                str(name): SourceLocation(SourceKind.CLI, symbol=f"--set {name}")
                for name in (parameters or {})
            },
        )
        authored = builder.build()
    except DraftLoweringError as exc:
        raise _authoring_operation_error(exc, "new") from exc
    deployment = create_simulation_deployment(authored)
    target.mkdir(parents=True, exist_ok=True)
    source_path = _atomic_json(target / AUTHORING_SOURCE_URI, builder.to_source_payload())
    written = authored.write_project(target / GENERATED_ROOT_URI)
    deployment_path = _atomic_json(
        target / SIMULATION_DEPLOYMENT_URI,
        deployment.to_payload(),
    )
    explanation = explain_authored_experiment(authored).to_payload()
    explanation_path = _atomic_json(target / AUTHORING_EXPLANATION_URI, explanation)
    artifacts = (
        _artifact(target, "authoring_source", source_path),
        _artifact(target, "authoring_project", written.files["authoring-project.json"]),
        _artifact(target, "protocol", written.files["protocol.json"]),
        _artifact(target, "suite", written.files["suite.json"]),
        _artifact(
            target,
            "deployment_requirements",
            written.files["deployment-requirements.json"],
        ),
        _artifact(target, "simulation_deployment", deployment_path),
        _artifact(target, "authoring_explanation", explanation_path),
    )
    manifest = ProjectManifest(project_id, artifacts)
    _write_manifest(target, manifest)
    return ExperimentProject(target, manifest)


def open_project(root: str | Path) -> ExperimentProject:
    project = _open_project_index(root)
    target = project.root
    manifest = project.manifest
    for artifact in manifest.artifacts:
        path = target / artifact.uri
        if not path.is_file():
            raise FileNotFoundError(f"project artifact is missing: {artifact.uri}")
        if artifact.immutable and canonical_hash(_read_object(path)) != artifact.digest:
            raise ValueError(f"project artifact digest mismatch: {artifact.uri}")
    return project


def read_project_authoring(project: ExperimentProject) -> AuthoredExperiment:
    source_artifact = project.manifest.artifact("authoring_source")
    payload = _read_object(project.root / source_artifact.uri)
    template = payload.get("template")
    parameters = template.get("parameters", {}) if isinstance(template, Mapping) else {}
    source = SourceLocation(SourceKind.GENERATED, locator=source_artifact.uri)
    source_map = DraftSourceMap(
        {
            f"/template/parameters/{_escape_pointer(str(name))}": source
            for name in parameters
        },
        fallback=source,
    )
    return ExperimentBuilder.from_source_payload(payload, source_map=source_map).build()


def record_detection_report(
    root: str | Path,
    report: DetectionReport,
) -> ExperimentProject:
    """Persist one content-addressed detection report without changing a deployment."""

    if not isinstance(report, DetectionReport):
        raise TypeError("record_detection_report requires a DetectionReport")
    project = open_project(root)
    segment = report.report_hash.removeprefix("sha256:")
    report_path = _write_immutable_json(
        project.root / DETECTION_ROOT_URI / segment / "report.json",
        report.to_payload(),
    )
    manifest = project.manifest.replacing_artifacts(
        _artifact(project.root, "detection_report", report_path, immutable=True)
    )
    _write_manifest(project.root, manifest)
    return ExperimentProject(project.root, manifest)


def propose_project_deployment(
    root: str | Path,
    report: DetectionReport,
    *,
    selection: DeploymentSelection | None = None,
    proposal_id: str | None = None,
) -> ProjectDeploymentProposal:
    """Write immutable proposal evidence and a selectable DeploymentSpec.

    The proposal becomes a project artifact but never replaces the reviewed
    simulation deployment.  Compilation must explicitly name its artifact role.
    """

    project = open_project(root)
    authored = read_project_authoring(project)
    proposal = propose_deployment(
        authored,
        report,
        selection=selection,
        proposal_id=proposal_id,
    )
    report_segment = report.report_hash.removeprefix("sha256:")
    proposal_segment = proposal.proposal_hash.removeprefix("sha256:")
    report_path = _write_immutable_json(
        project.root / DETECTION_ROOT_URI / report_segment / "report.json",
        report.to_payload(),
    )
    proposal_root = project.root / DEPLOYMENT_PROPOSAL_ROOT_URI / proposal_segment
    deployment_path = _write_immutable_json(
        proposal_root / "deployment.json",
        proposal.deployment.to_payload(),
    )
    proposal_path = _write_immutable_json(
        proposal_root / "proposal.json",
        proposal.to_payload(),
    )
    manifest = project.manifest.replacing_artifacts(
        _artifact(project.root, "detection_report", report_path, immutable=True),
        _artifact(
            project.root,
            "deployment_proposal",
            deployment_path,
            immutable=True,
        ),
        _artifact(
            project.root,
            "deployment_proposal_provenance",
            proposal_path,
            immutable=True,
        ),
    )
    _write_manifest(project.root, manifest)
    return ProjectDeploymentProposal(
        ExperimentProject(project.root, manifest), report, proposal
    )


def compile_project(
    root: str | Path,
    *,
    deployment_role: str = "simulation_deployment",
) -> ProjectCompilation:
    """Lower current authoring, compile canonical specs, and write immutable outputs."""

    project = open_project(root)
    try:
        authored = read_project_authoring(project)
    except DraftLoweringError as exc:
        raise _authoring_operation_error(exc, "compile") from exc
    authored.write_project(project.root / GENERATED_ROOT_URI, overwrite=True)
    selected_role = require_identifier(deployment_role, "deployment artifact role")
    deployment = DeploymentSpec.load(project.path_for(selected_role))
    registry = _builtin_registry()
    model_manifests = None
    if selected_role != "simulation_deployment":
        try:
            registry.load_entry_points()
        except Exception as exc:
            raise _operation_error(
                "compile",
                ExitCode.UNAVAILABLE,
                "deployment.plugin_load_failed",
                OperationCategory.AVAILABILITY,
                "Installed plugin discovery failed",
                str(exc),
            ) from exc
        try:
            proposal = DeploymentProposal.from_payload(
                _read_object(project.path_for("deployment_proposal_provenance"))
            )
            report_segment = proposal.detection_report_hash.removeprefix("sha256:")
            report = DetectionReport.load(
                project.root / DETECTION_ROOT_URI / report_segment / "report.json"
            )
        except (FileNotFoundError, KeyError) as exc:
            raise _operation_error(
                "compile",
                ExitCode.INVALID_INPUT,
                "deployment.proposal_evidence_missing",
                OperationCategory.INTEGRITY,
                "Deployment proposal evidence is missing",
                "Compile a proposed deployment only while its detection and provenance artifacts are indexed.",
            ) from exc
        if proposal.detection_report_hash != report.report_hash:
            raise _operation_error(
                "compile",
                ExitCode.INTEGRITY_FAILED,
                "deployment.proposal_detection_mismatch",
                OperationCategory.INTEGRITY,
                "Deployment proposal does not match the detection report",
                "The proposal and indexed capability evidence have different identities.",
            )
        if proposal.deployment != deployment:
            raise _operation_error(
                "compile",
                ExitCode.INTEGRITY_FAILED,
                "deployment.proposal_spec_mismatch",
                OperationCategory.INTEGRITY,
                "Deployment proposal content does not match its selectable spec",
                "The proposal provenance and DeploymentSpec artifacts differ.",
            )
        model_manifests = {
            value.manifest.manifest_digest: value.manifest for value in report.models
        }
    try:
        compiled = authored.compile(
            deployment,
            registry,
            model_manifests=model_manifests,
        )
    except CompilationError as exc:
        raise diagnose_compilation_failure(exc, authored) from exc
    plan_segment = compiled.plan.plan_hash.removeprefix("sha256:")
    lock_segment = compiled.lock.lock_hash.removeprefix("sha256:")
    plan_path = _write_immutable_plan(
        project.root / "builds" / plan_segment / "execution-plan.json",
        compiled.plan,
    )
    lock_path = _write_immutable_lock(
        project.root / "locks" / f"{lock_segment}.json",
        compiled.lock,
    )
    explanation = explain_authored_experiment(authored, plan=compiled.plan).to_payload()
    authoring_explanation_path = _atomic_json(
        project.root / AUTHORING_EXPLANATION_URI,
        explain_authored_experiment(authored).to_payload(),
    )
    explanation_path = _write_immutable_json(
        project.root / "explanations" / f"{plan_segment}.json",
        explanation,
    )
    manifest = project.manifest.replacing_artifacts(
        _artifact(project.root, "authoring_source", project.path_for("authoring_source")),
        _artifact(
            project.root,
            "simulation_deployment",
            project.path_for("simulation_deployment"),
        ),
        _artifact(
            project.root,
            selected_role,
            project.path_for(selected_role),
            immutable=project.manifest.artifact(selected_role).immutable,
        ),
        _artifact(project.root, "execution_plan", plan_path, immutable=True),
        _artifact(project.root, "execution_lock", lock_path, immutable=True),
        _artifact(project.root, "locked_explanation", explanation_path, immutable=True),
        _artifact(
            project.root,
            "authoring_explanation",
            authoring_explanation_path,
        ),
        _artifact(project.root, "protocol", project.root / GENERATED_ROOT_URI / "protocol.json"),
        _artifact(project.root, "suite", project.root / GENERATED_ROOT_URI / "suite.json"),
        _artifact(
            project.root,
            "deployment_requirements",
            project.root / GENERATED_ROOT_URI / "deployment-requirements.json",
        ),
        _artifact(
            project.root,
            "authoring_project",
            project.root / GENERATED_ROOT_URI / "authoring-project.json",
        ),
    )
    _write_manifest(project.root, manifest)
    updated = ExperimentProject(project.root, manifest)
    return ProjectCompilation(
        updated,
        compiled.plan,
        compiled.lock,
        explanation,
        selected_role,
    )


def explain_project(root: str | Path) -> Mapping[str, Any]:
    try:
        project = open_project(root)
    except ValueError as exc:
        raise _operation_error(
            "explain",
            ExitCode.INTEGRITY_FAILED,
            "project.integrity",
            OperationCategory.INTEGRITY,
            "Project integrity verification failed",
            str(exc),
        ) from exc
    try:
        authored = read_project_authoring(project)
    except DraftLoweringError as exc:
        raise _authoring_operation_error(exc, "explain") from exc
    try:
        plan_path = project.path_for("execution_plan")
    except KeyError:
        plan = None
    else:
        plan = read_plan(plan_path)
    return freeze_json(explain_authored_experiment(authored, plan=plan).to_payload())


def diff_projects(before_root: str | Path, after_root: str | Path) -> Mapping[str, Any]:
    before_project = open_project(before_root)
    after_project = open_project(after_root)
    before = read_project_authoring(before_project)
    after = read_project_authoring(after_project)
    try:
        before_plan = read_plan(before_project.path_for("execution_plan"))
        after_plan = read_plan(after_project.path_for("execution_plan"))
    except KeyError:
        before_plan = None
        after_plan = None
    return freeze_json(
        diff_authored_experiments(
            before,
            after,
            before_plan=before_plan,
            after_plan=after_plan,
        ).to_payload()
    )


def graph_project(root: str | Path) -> Mapping[str, Any]:
    project = open_project(root)
    try:
        plan = read_plan(project.path_for("execution_plan"))
        lock = read_lock(project.path_for("execution_lock"))
    except KeyError as exc:
        raise _operation_error(
            "graph",
            ExitCode.INVALID_INPUT,
            "project.not_compiled",
            OperationCategory.COMPILATION,
            "Project has no locked graph",
            "Compile the project before inspecting its graph.",
        ) from exc
    try:
        lock.verify_plan(plan)
    except ValueError as exc:
        raise _operation_error(
            "graph",
            ExitCode.INTEGRITY_FAILED,
            "lock.plan_mismatch",
            OperationCategory.INTEGRITY,
            "Execution lock verification failed",
            str(exc),
        ) from exc
    if plan.graph is None:
        raise _operation_error(
            "graph",
            ExitCode.INVALID_INPUT,
            "plan.graph_missing",
            OperationCategory.COMPILATION,
            "Execution plan has no compiled graph",
            "Recompile the project with the current graph-bearing compiler.",
        )
    return freeze_json(
        {
            "schema": PROJECT_GRAPH_SCHEMA_ID,
            "project_id": project.manifest.project_id,
            "plan_hash": plan.plan_hash,
            "graph": plan.graph.to_payload(),
        }
    )


def run_project(
    root: str | Path,
    *,
    session_id: str | None = None,
    kind: str = "run",
    _registry: PluginRegistry | None = None,
    _supplemental_evidence: tuple[tuple[str, Mapping[str, Any]], ...] = (),
) -> ProjectRun:
    """Resolve a project's immutable pair, then execute only that verified pair."""

    try:
        project = _open_project_index(root)
    except ValueError as exc:
        raise _operation_error(
            kind,
            ExitCode.INTEGRITY_FAILED,
            "project.integrity",
            OperationCategory.INTEGRITY,
            "Project integrity verification failed",
            str(exc),
        ) from exc
    try:
        plan_path = project.path_for("execution_plan")
        lock_path = project.path_for("execution_lock")
    except KeyError as exc:
        raise _operation_error(
            kind,
            ExitCode.INVALID_INPUT,
            "project.not_compiled",
            OperationCategory.COMPILATION,
            "Project has no locked plan",
            "Compile the project before execution.",
        ) from exc
    identifier = session_id or _session_id(kind)
    session_root = project.root / "sessions" / identifier
    result = run_locked_plan(
        plan_path,
        lock_path,
        session_root,
        session_id=identifier,
        kind=kind,
        registry=_registry,
        supplemental_evidence=_supplemental_evidence,
    )
    session_uri = session_root.relative_to(project.root).as_posix()
    manifest = project.manifest.registering_session(session_uri)
    _write_manifest(project.root, manifest)
    return result


def preflight_project(
    root: str | Path,
    *,
    detection_report: DetectionReport | None = None,
    registry: PluginRegistry | None = None,
    operator_confirmations: tuple[str, ...] = (),
    safe_state_reports: tuple[str, ...] = (),
    available_secret_providers: tuple[str, ...] = (),
) -> PreflightReport:
    """Verify the exact deployment that produced a project's current lock."""

    project = open_project(root)
    try:
        plan = read_plan(project.path_for("execution_plan"))
        lock = read_lock(project.path_for("execution_lock"))
    except KeyError as exc:
        raise _operation_error(
            "preflight",
            ExitCode.INVALID_INPUT,
            "project.not_compiled",
            OperationCategory.COMPILATION,
            "Project has no locked plan",
            "Compile the selected deployment before preflight.",
        ) from exc
    deployment = _deployment_for_plan(project, plan)
    observed = detection_report
    if observed is None:
        try:
            observed = DetectionReport.load(project.path_for("detection_report"))
        except KeyError:
            observed = None
    plugins = registry or _runtime_registry(plan)
    report = preflight(
        plan,
        lock,
        deployment,
        plugins,
        detection_report=observed,
        operator_confirmations=operator_confirmations,
        safe_state_reports=safe_state_reports,
        available_secret_providers=available_secret_providers,
        preflight_id=f"preflight.{plan.plan_hash.removeprefix('sha256:')[:16]}",
    )
    segment = report.report_hash.removeprefix("sha256:")
    path = _write_immutable_json(
        project.root / PREFLIGHT_ROOT_URI / segment / "report.json",
        report.to_payload(),
    )
    manifest = project.manifest.replacing_artifacts(
        _artifact(project.root, "preflight_report", path, immutable=True)
    )
    _write_manifest(project.root, manifest)
    return report


def rehearse_project(
    root: str | Path,
    *,
    session_id: str | None = None,
    operator_confirmations: Iterable[str] = (),
    safe_state_reports: Iterable[str] = (),
    available_secret_providers: Iterable[str] = (),
) -> ProjectRun:
    """Preflight and execute a simulation lock with fault-containment evidence."""

    project = _open_project_index(root)
    try:
        plan = read_plan(project.path_for("execution_plan"))
    except KeyError as exc:
        raise _operation_error(
            "rehearse",
            ExitCode.INVALID_INPUT,
            "project.not_compiled",
            OperationCategory.COMPILATION,
            "Project has no locked plan",
            "Compile the simulation project before rehearsal.",
        ) from exc
    lock = read_lock(project.path_for("execution_lock"))
    deployment = _deployment_for_plan(project, plan)
    registry = _runtime_registry(plan)
    if not deployment.resources or any(
        value.kind != "simulator" for value in deployment.resources
    ):
        raise _operation_error(
            "rehearse",
            ExitCode.UNAVAILABLE,
            "rehearsal.simulation_deployment_required",
            OperationCategory.AVAILABILITY,
            "Rehearsal requires a separately compiled simulation deployment",
            "Compile the unchanged portable intent against simulation resources before rehearsal.",
        )
    try:
        assert_rehearsal_safe(plan, registry)
    except ValueError as exc:
        raise _operation_error(
            "rehearse",
            ExitCode.REJECTED,
            "rehearsal.physical_action",
            OperationCategory.PREFLIGHT,
            "Rehearsal lock could reach physical action",
            str(exc),
        ) from exc
    preflight_report = preflight(
        plan,
        lock,
        deployment,
        registry,
        operator_confirmations=operator_confirmations,
        safe_state_reports=safe_state_reports,
        available_secret_providers=available_secret_providers,
        preflight_id=f"preflight.rehearsal.{plan.plan_hash.removeprefix('sha256:')[:12]}",
    )
    if not preflight_report.ready:
        failed = next(
            value for value in preflight_report.checks if value.status.value == "fail"
        )
        raise _operation_error(
            "rehearse",
            ExitCode.REJECTED,
            "rehearsal.preflight_failed",
            OperationCategory.AVAILABILITY,
            "Simulation preflight failed",
            failed.summary,
        )
    scenarios = rehearsal_fault_outcomes(plan, registry)
    result = run_project(
        root,
        session_id=session_id,
        kind="rehearsal",
        _registry=registry,
        _supplemental_evidence=tuple(
            ("rehearsal_fault_outcome", value.to_payload()) for value in scenarios
        ),
    )
    current = _open_project_index(root)
    report = RehearsalReport(
        rehearsal_id=f"rehearsal.{result.session_id}",
        portable_protocol_hash=plan.spec_hashes["protocol"],
        portable_suite_hash=plan.spec_hashes["suite"],
        simulation_plan_hash=plan.plan_hash,
        simulation_lock_hash=lock.lock_hash,
        preflight_report_hash=preflight_report.report_hash,
        session_id=result.session_id,
        bundle_id=result.bundle_id,
        scenarios=scenarios,
    )
    preflight_segment = preflight_report.report_hash.removeprefix("sha256:")
    preflight_path = _write_immutable_json(
        current.root / PREFLIGHT_ROOT_URI / preflight_segment / "report.json",
        preflight_report.to_payload(),
    )
    report_segment = report.report_hash.removeprefix("sha256:")
    report_path = _write_immutable_json(
        current.root / REHEARSAL_ROOT_URI / report_segment / "report.json",
        report.to_payload(),
    )
    manifest = current.manifest.replacing_artifacts(
        _artifact(current.root, "preflight_report", preflight_path, immutable=True),
        _artifact(current.root, "rehearsal_report", report_path, immutable=True),
    )
    _write_manifest(current.root, manifest)
    return result


def run_locked_plan(
    plan_path: str | Path,
    lock_path: str | Path,
    session_root: str | Path,
    *,
    session_id: str,
    kind: str = "run",
    registry: PluginRegistry | None = None,
    supplemental_evidence: tuple[tuple[str, Mapping[str, Any]], ...] = (),
) -> ProjectRun:
    """Run a verified plan/lock pair without reading authoring or project state."""

    try:
        plan = read_plan(plan_path)
        lock = read_lock(lock_path)
        lock.verify_plan(plan)
    except (TypeError, ValueError) as exc:
        raise _operation_error(
            kind,
            ExitCode.INTEGRITY_FAILED,
            "lock.plan_mismatch",
            OperationCategory.INTEGRITY,
            "Execution lock verification failed",
            str(exc),
        ) from exc
    registry = registry or _runtime_registry(plan)
    session = Session.create(
        session_root,
        session_id=session_id,
        metadata={
            "operation": kind,
            "plan_hash": plan.plan_hash,
            "lock_hash": lock.lock_hash,
        },
    )
    try:
        engine = ExecutionEngine.from_plan(plan, registry)
        result = engine.run()
        if supplemental_evidence:
            records = list(result.evidence)
            emitted = records[-1].emitted_time
            for record_type, payload in supplemental_evidence:
                sequence = len(records)
                records.append(
                    EvidenceRecord(
                        record_id=f"{record_type}.{sequence}",
                        record_type=record_type,
                        sequence=sequence,
                        emitted_time=emitted,
                        payload=payload,
                    )
                )
            result = replace(result, evidence=tuple(records))
        streams = _streams_from_plan(plan)
        bundle = persist_engine_run(session, result, plan=plan, streams=streams)
        session.finalize(_session_status(result.status))
    except Exception:
        if session.status == SessionStatus.OPEN:
            session.finalize(SessionStatus.FAILED)
        raise
    return ProjectRun(
        session.root,
        session.session_id,
        bundle.bundle_id,
        plan.plan_hash,
        result.status,
        len(result.evidence),
        kind,
    )


def rehearse_locked_plan(
    plan_path: str | Path,
    lock_path: str | Path,
    deployment: DeploymentSpec,
    session_root: str | Path,
    *,
    session_id: str,
    registry: PluginRegistry,
    live_plan_hash: str | None = None,
    operator_confirmations: Iterable[str] = (),
    safe_state_reports: Iterable[str] = (),
    available_secret_providers: Iterable[str] = (),
) -> LockedRehearsal:
    """Rehearse any compiled reference plan with explicitly supplied plugins."""

    plan = read_plan(plan_path)
    lock = read_lock(lock_path)
    if plan.spec_hashes.get("deployment") != deployment.spec_hash:
        raise ValueError("rehearsal deployment does not match the compiled plan")
    if not deployment.resources or any(value.kind != "simulator" for value in deployment.resources):
        raise ValueError("rehearsal requires only simulation resources")
    assert_rehearsal_safe(plan, registry)
    preflight_report = preflight(
        plan,
        lock,
        deployment,
        registry,
        operator_confirmations=operator_confirmations,
        safe_state_reports=safe_state_reports,
        available_secret_providers=available_secret_providers,
        preflight_id=f"preflight.rehearsal.{plan.plan_hash.removeprefix('sha256:')[:12]}",
    )
    if not preflight_report.ready:
        first = next(value for value in preflight_report.checks if value.status.value == "fail")
        raise ValueError(f"rehearsal preflight failed: {first.summary}")
    scenarios = rehearsal_fault_outcomes(plan, registry)
    run = run_locked_plan(
        plan_path,
        lock_path,
        session_root,
        session_id=session_id,
        kind="rehearsal",
        registry=registry,
        supplemental_evidence=tuple(
            ("rehearsal_fault_outcome", value.to_payload()) for value in scenarios
        ),
    )
    report = RehearsalReport(
        rehearsal_id=f"rehearsal.{session_id}",
        portable_protocol_hash=plan.spec_hashes["protocol"],
        portable_suite_hash=plan.spec_hashes["suite"],
        simulation_plan_hash=plan.plan_hash,
        simulation_lock_hash=lock.lock_hash,
        preflight_report_hash=preflight_report.report_hash,
        session_id=run.session_id,
        bundle_id=run.bundle_id,
        scenarios=scenarios,
        live_plan_hash=live_plan_hash,
    )
    return LockedRehearsal(run, preflight_report, report)


def create_simulation_deployment(authored: AuthoredExperiment) -> DeploymentSpec:
    """Create the bounded base-wheel deployment used by the first simulation."""

    if authored.expansion.template.template_id != "eegle.template.continuous_recording":
        raise _operation_error(
            "new",
            ExitCode.UNAVAILABLE,
            "simulation.template_scope",
            OperationCategory.AVAILABILITY,
            "Automatic simulation binding is currently recording-only",
            "Use the continuous-recording template; broader deployment generation is owned by P7-008/P7-009.",
        )
    suite = authored.suite
    if len(suite.streams) != 1:
        raise ValueError("base simulation requires exactly one logical stream")
    logical = suite.streams[0]
    contract = logical.contract
    if (
        contract.type_id != "eegle.dense_sample_batch.v1"
        or contract.channel_count is None
        or contract.nominal_rate_hz is None
        or contract.unit is None
    ):
        raise ValueError("base simulation requires a complete regular dense signal contract")
    channels = tuple(
        ChannelSpec(
            channel_id=f"channel.simulation.{index + 1}",
            kind=logical.modality or "signal",
            unit=contract.unit,
            name=f"SIM{index + 1}",
        )
        for index in range(contract.channel_count)
    )
    stream = StreamSpec(
        stream_id=logical.stream_id,
        revision=1,
        modality=logical.modality or "signal",
        content_kind=ContentKind.DENSE_SAMPLES,
        rate_model=RateModel.REGULAR,
        clock_id=logical.clock_id or "device.clock",
        channels=channels,
        sample_rate_hz=contract.nominal_rate_hz,
        sample_dtype="float64",
        missing_data_policy=MissingDataPolicy.FORBID,
        metadata={"fixture": "eegle.base_first_simulation.v1"},
    )
    values = np.asarray(
        [
            [float(sample) + float(channel) / 10.0 for channel in range(len(channels))]
            for sample in range(8)
        ],
        dtype=np.float64,
    )
    execution_clock_id = str(suite.clock_policy["execution_clock_id"])
    packet = DenseSampleBatch(
        batch_id="batch.simulation.1",
        stream_id=stream.stream_id,
        stream_revision=stream.revision,
        sequence_start=0,
        channel_ids=tuple(value.channel_id for value in channels),
        values=values,
        received_time=TimePoint(0.099, execution_clock_id),
        available_time=TimePoint(0.1, execution_clock_id),
        first_sample_time=TimePoint(0.0, stream.clock_id),
        sample_period_seconds=1.0 / contract.nominal_rate_hz,
    )
    source_ids = {
        value.component_id
        for value in suite.components
        if value.kind == ComponentKind.SOURCE
    }
    if len(source_ids) != 1:
        raise ValueError("base simulation requires exactly one source component")
    source_id = next(iter(source_ids))
    resource_id = "resource.simulation.neural"
    bindings: list[ComponentBindingSpec] = []
    for component in suite.components:
        if component.component_id == source_id:
            bindings.append(
                ComponentBindingSpec(
                    component_id=component.component_id,
                    plugin_id="eegle.sources.packet_sequence_dense",
                    version_spec="~=0.1.0",
                    placement=Placement.IN_PROCESS,
                    resource_ids=(resource_id,),
                    config={
                        "stream_spec": stream.to_payload(),
                        "packets": [packet.to_payload()],
                    },
                )
            )
        else:
            if component.plugin_id is None:
                raise ValueError(f"component {component.component_id} has no executable plugin")
            bindings.append(
                ComponentBindingSpec(
                    component_id=component.component_id,
                    plugin_id=component.plugin_id,
                    version_spec=component.version_spec,
                    placement=Placement.IN_PROCESS,
                    config=thaw_json(component.config),
                )
            )
    clocks = ()
    if stream.clock_id != execution_clock_id:
        clocks = (
            ClockMappingBinding(
                source_clock=stream.clock_id,
                target_clock=execution_clock_id,
                strategy=ClockMappingStrategy.DECLARED_AFFINE,
                maximum_uncertainty_seconds=0.001,
            ),
        )
    return DeploymentSpec(
        deployment_id=f"deployment.simulation.{authored.draft.draft_id}",
        suite_id=suite.suite_id,
        component_bindings=tuple(bindings),
        resources=(
            ResourceSpec(
                resource_id=resource_id,
                kind="simulator",
                selector={"generator": "packet_sequence", "fixture": "base_first_simulation"},
                capabilities=("deterministic",),
                contract=contract,
            ),
        ),
        stream_bindings=(StreamBinding(stream.stream_id, resource_id),),
        storage=(
            StorageBinding(
                "storage.evidence",
                "evidence",
                f"memory://eegle/{authored.draft.draft_id}/simulation",
            ),
        ),
        clock_mappings=clocks,
    )


def _builtin_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register_builtins()
    return registry


def _runtime_registry(plan: ExecutionPlan) -> PluginRegistry:
    """Load only installed executable descriptors needed by a locked plan."""

    registry = _builtin_registry()
    missing = {
        (value.plugin_id, value.version)
        for value in plan.plugins
        if not any(
            descriptor.plugin_id == value.plugin_id
            and descriptor.version == value.version
            for descriptor in registry.descriptors()
        )
    }
    if missing:
        registry.load_entry_points()
    return registry


def _deployment_for_plan(
    project: ExperimentProject,
    plan: ExecutionPlan,
) -> DeploymentSpec:
    expected = plan.spec_hashes.get("deployment")
    candidates: list[tuple[str, DeploymentSpec]] = []
    for artifact in project.manifest.artifacts:
        if artifact.role not in {"simulation_deployment", "deployment_proposal"}:
            continue
        try:
            deployment = DeploymentSpec.load(project.root / artifact.uri)
        except (KeyError, TypeError, ValueError):
            continue
        if deployment.spec_hash == expected:
            candidates.append((artifact.role, deployment))
    if len(candidates) != 1:
        raise _operation_error(
            "preflight",
            ExitCode.INTEGRITY_FAILED,
            "deployment.locked_binding_missing",
            OperationCategory.INTEGRITY,
            "The plan's compiled deployment cannot be resolved exactly",
            f"Expected one indexed deployment with digest {expected}; found {len(candidates)}.",
        )
    return candidates[0][1]


def _open_project_index(root: str | Path) -> ExperimentProject:
    target = Path(root).expanduser().resolve()
    payload = _read_object(target / PROJECT_MANIFEST_NAME)
    return ExperimentProject(target, ProjectManifest.from_payload(payload))


def _streams_from_plan(plan: ExecutionPlan) -> tuple[StreamSpec, ...]:
    streams: list[StreamSpec] = []
    for component in plan.components:
        payload = component.config.get("stream_spec")
        if isinstance(payload, Mapping):
            streams.append(StreamSpec.from_payload(payload))
    if not streams:
        raise ValueError("execution plan contains no persistable source stream specifications")
    return tuple(streams)


def _session_status(status: EngineStatus) -> SessionStatus:
    if status == EngineStatus.COMPLETE:
        return SessionStatus.COMPLETE
    if status == EngineStatus.FAILED:
        return SessionStatus.FAILED
    return SessionStatus.PARTIAL


def _session_id(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"session.{require_identifier(kind, 'run kind')}.{stamp}"


def _artifact(
    root: Path,
    role: str,
    path: Path,
    *,
    immutable: bool = False,
) -> ProjectArtifact:
    resolved_root = root.expanduser().resolve()
    resolved = path.expanduser().resolve()
    uri = resolved.relative_to(resolved_root).as_posix()
    return ProjectArtifact(role, uri, canonical_hash(_read_object(resolved)), immutable)


def _write_manifest(root: Path, manifest: ProjectManifest) -> Path:
    return _atomic_json(root / PROJECT_MANIFEST_NAME, manifest.to_payload())


def _read_object(path: str | Path) -> Mapping[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON object: {source}") from exc
    if not isinstance(payload, Mapping):
        raise TypeError(f"{source} must contain a JSON object")
    return payload


def _atomic_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def _write_immutable_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    target = Path(path)
    if target.exists():
        if _read_object(target) != payload:
            raise FileExistsError(f"immutable artifact already exists with other content: {target}")
        return target
    return _atomic_json(target, payload)


def _write_immutable_plan(path: str | Path, plan: ExecutionPlan) -> Path:
    target = Path(path)
    if target.exists():
        existing = read_plan(target)
        if existing != plan:
            raise FileExistsError(f"immutable execution plan already exists: {target}")
        return target
    return write_plan(target, plan)


def _write_immutable_lock(path: str | Path, lock: ExecutionLock) -> Path:
    target = Path(path)
    if target.exists():
        existing = read_lock(target)
        if existing != lock:
            raise FileExistsError(f"immutable execution lock already exists: {target}")
        return target
    return write_lock(target, lock)


def _safe_relative(value: str, field: str) -> str:
    path = Path(str(value))
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"{field} must be a safe relative path")
    return path.as_posix()


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _operation_error(
    operation: str,
    exit_code: ExitCode,
    code: str,
    category: OperationCategory,
    title: str,
    message: str,
) -> OperationError:
    return OperationError(
        operation,
        exit_code,
        (OperationDiagnostic(code, category, title, message),),
    )


def _authoring_operation_error(
    error: DraftLoweringError,
    operation: str,
) -> OperationError:
    diagnosed = diagnose_authoring_failure(error)
    return OperationError(operation, diagnosed.exit_code, diagnosed.diagnostics)
