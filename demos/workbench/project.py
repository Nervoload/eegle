"""Project bootstrap and read-only projection for Workbench."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from demos.workbench.profile import Study1Profile, build_study1_design
from demos.workbench.state import IssueSeverity, ProjectSnapshot, UiIssue
from eegle.authoring import ExperimentDesign
from eegle.compiler import canonical_hash
from eegle.operations import (
    ExperimentProject,
    create_project,
    explain_project,
    open_project,
)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    project: ExperimentProject
    snapshot: ProjectSnapshot
    issues: tuple[UiIssue, ...] = ()


def default_project_root() -> Path:
    repository_root = Path(__file__).resolve().parents[2]
    return repository_root / "data" / "workbench" / "projects" / "study1-neuracle64-demo"


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"project artifact must contain a JSON object: {path}")
    return payload


def _annotation(payload: Mapping[str, Any], name: str, default: str = "") -> str:
    study = payload.get("study")
    annotations = study.get("annotations") if isinstance(study, Mapping) else None
    if not isinstance(annotations, Mapping):
        return default
    return str(annotations.get(name, default))


def _artifact_digest(project: ExperimentProject, role: str) -> str | None:
    try:
        return project.manifest.artifact(role).digest
    except KeyError:
        return None


def project_snapshot(
    project: ExperimentProject,
    profile: Study1Profile,
) -> ProjectSnapshot:
    source_path = project.path_for("authoring_source")
    payload = _read_json_object(source_path)
    design = ExperimentDesign.from_payload(payload)
    explanation = explain_project(project.root)
    hashes = explanation.get("canonical_hashes", {})
    if not isinstance(hashes, Mapping):
        hashes = {}
    roles = {artifact.role for artifact in project.manifest.artifacts}
    return ProjectSnapshot(
        project_root=project.root,
        project_id=project.manifest.project_id,
        display_name=profile.display_name,
        statement=design.study.statement,
        profile_id=_annotation(payload, "workbench_profile_id", str(profile.payload["profile_id"])),
        profile_variant=_annotation(payload, "workbench_profile_variant", profile.default_variant),
        profile_digest=_annotation(payload, "workbench_profile_digest"),
        design_digest=canonical_hash(payload),
        design_revision=design.revision,
        protocol_hash=str(hashes.get("protocol", "")),
        suite_hash=str(hashes.get("suite", "")),
        scientific_status=_annotation(payload, "scientific_status", "unknown"),
        session_kind=_annotation(payload, "session_kind", "unknown"),
        logical_channels=tuple(design.signals[0].channels) if design.signals else (),
        marker_kinds=tuple(design.events[0].event_kinds) if design.events else (),
        source_payload=payload,
        explanation=explanation,
        session_uris=tuple(project.manifest.session_uris),
        compiled={"execution_plan", "execution_lock"}.issubset(roles),
        plan_hash=_artifact_digest(project, "execution_plan"),
        lock_hash=_artifact_digest(project, "execution_lock"),
    )


def bootstrap_study1_project(
    profile: Study1Profile,
    *,
    project_root: Path | None = None,
) -> BootstrapResult:
    """Create the prepared project once, then reopen it without overwriting."""

    root = (project_root or default_project_root()).expanduser().resolve()
    if not root.exists() or not any(root.iterdir()):
        design = build_study1_design(profile)
        project = create_project(root, project_id=profile.project_id, design=design)
    else:
        project = open_project(root)
    snapshot = project_snapshot(project, profile)
    issues: list[UiIssue] = []
    if snapshot.profile_digest != profile.digest:
        issues.append(
            UiIssue(
                "workbench.profile_stale",
                "Prepared profile changed",
                "The existing project was created from a different profile digest. "
                "Workbench has left it unchanged; create a new project revision before rebuilding.",
                IssueSeverity.WARNING,
            )
        )
    return BootstrapResult(project, snapshot, tuple(issues))
