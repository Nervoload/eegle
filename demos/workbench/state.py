"""Typed, UI-facing Workbench state with no Qt dependency."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any


class Page(str, Enum):
    EXPERIMENTS = "experiments"
    REPLAY = "replay"
    DESIGN = "design"
    APPARATUS = "apparatus"
    BUILD = "build"
    RUN = "run"
    SESSIONS = "sessions"


ACTIVE_WORKFLOW = (
    Page.DESIGN,
    Page.APPARATUS,
    Page.BUILD,
    Page.RUN,
    Page.SESSIONS,
)


class Lifecycle(str, Enum):
    NO_PROJECT = "no_project"
    DESIGN_READY = "design_ready"
    APPARATUS_UNBOUND = "apparatus_unbound"
    DEPLOYMENT_REVIEW = "deployment_review"
    DEPLOYMENT_ACCEPTED = "deployment_accepted"
    COMPILED = "compiled"
    PREFLIGHT_WARNING = "preflight_warning"
    PREFLIGHT_READY = "preflight_ready"
    ARMED = "armed"
    RUNNING = "running"
    FINALIZING = "finalizing"
    SESSION_AVAILABLE = "session_available"
    REPLAYED = "replayed"


class EnvironmentMode(str, Enum):
    SIMULATION = "simulation"
    LIVE_LSL = "live_lsl"
    BENCH_TEST = "bench_test"


class IssueSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class TaskStatus(str, Enum):
    CLOSED = "closed"
    STARTING = "starting"
    ARMED = "armed"
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


class RunnerStatus(str, Enum):
    CLOSED = "closed"
    STARTING = "starting"
    READY = "ready"
    RUNNING = "running"
    FINALIZING = "finalizing"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UiIssue:
    code: str
    title: str
    message: str
    severity: IssueSeverity = IssueSeverity.WARNING


@dataclass(frozen=True, slots=True)
class DetectedStreamSnapshot:
    capability_id: str
    name: str
    stream_type: str
    channel_count: int
    nominal_rate_hz: float
    channel_format: str
    source_id: str
    uid: str
    hostname: str
    content_kind: str
    channel_labels: tuple[str, ...] = ()
    channel_units: tuple[str, ...] = ()
    channel_metadata_source: str | None = None
    event_kinds: tuple[str, ...] = ()

    @property
    def exact_selector(self) -> str:
        if self.uid:
            return f"uid={self.uid}"
        if self.source_id:
            return f"source_id={self.source_id}"
        return f"name={self.name}, type={self.stream_type}, host={self.hostname}"


@dataclass(frozen=True, slots=True)
class ApparatusSnapshot:
    dependency_available: bool = False
    library_version: str | None = None
    support_level: str = "unavailable"
    unavailable_reason: str | None = None
    remediation: tuple[str, ...] = ()
    scan_wait_seconds: float = 0.0
    detection_report_hash: str | None = None
    streams: tuple[DetectedStreamSnapshot, ...] = ()
    selected_eeg_capability_id: str | None = None
    selected_marker_capability_id: str | None = None
    proposal_hash: str | None = None
    deployment_hash: str | None = None
    reviewed_overlay: bool = False
    reference: str | None = None
    ground: str | None = None
    auxiliary_allocation: str | None = None

    @property
    def scan_complete(self) -> bool:
        return self.detection_report_hash is not None


@dataclass(frozen=True, slots=True)
class PreflightCheckSnapshot:
    check_id: str
    capability: str
    status: str
    summary: str


@dataclass(frozen=True, slots=True)
class BuildSnapshot:
    plan_hash: str | None = None
    lock_hash: str | None = None
    preflight_report_hash: str | None = None
    preflight_ready: bool = False
    checks: tuple[PreflightCheckSnapshot, ...] = ()
    deployment_role: str | None = None


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    status: TaskStatus = TaskStatus.CLOSED
    renderer: str | None = None
    marker_stream_name: str | None = None
    marker_source_id: str | None = None
    lsl_available: bool = False
    phase: str | None = None
    current_trial: int = 0
    total_trials: int = 0
    session_id: str | None = None
    message: str = "Task process is closed."
    participant_pseudonym: str | None = None
    timing_validated: bool = False
    measured_refresh_rate_hz: float | None = None
    task_python: str | None = None
    plan_hash: str | None = None
    behavior_summary: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class RunnerSnapshot:
    status: RunnerStatus = RunnerStatus.CLOSED
    session_id: str | None = None
    bundle_id: str | None = None
    plan_hash: str | None = None
    run_status: str | None = None
    evidence_record_count: int = 0
    terminal_reason: str | None = None
    message: str = "EEGle runner is closed."


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    session_id: str
    session_root: Path
    session_status: str
    outcome: str
    valid: bool
    bundle_id: str | None
    plan_hash: str | None
    engine_status: str | None
    terminal_reason: str | None
    evidence_record_count: int
    source_health: Mapping[str, Any]
    phase_timeline: tuple[Mapping[str, Any], ...]
    issues: tuple[Mapping[str, Any], ...]
    replay_ready: bool
    task_summary: Mapping[str, Any] | None = None
    task_reconciled: bool | None = None


@dataclass(frozen=True, slots=True)
class ReplaySnapshot:
    session_id: str | None = None
    status: str = "unavailable"
    bundle_id: str | None = None
    result_status: str | None = None
    equivalent: bool | None = None
    requested_level: str | None = None
    evaluated_level: str | None = None
    compared_record_count: int = 0
    first_divergence: Mapping[str, Any] | None = None
    issues: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    """A read-only projection assembled by the controller boundary."""

    project_root: Path
    project_id: str
    display_name: str
    statement: str
    profile_id: str
    profile_variant: str
    profile_digest: str
    design_digest: str
    design_revision: int
    protocol_hash: str
    suite_hash: str
    scientific_status: str
    session_kind: str
    logical_channels: tuple[str, ...]
    marker_kinds: tuple[str, ...]
    source_payload: Mapping[str, Any]
    explanation: Mapping[str, Any]
    session_uris: tuple[str, ...]
    compiled: bool
    plan_hash: str | None = None
    lock_hash: str | None = None

    @property
    def has_sessions(self) -> bool:
        return bool(self.session_uris)


@dataclass(frozen=True, slots=True)
class WorkbenchState:
    page: Page = Page.DESIGN
    lifecycle: Lifecycle = Lifecycle.NO_PROJECT
    environment_mode: EnvironmentMode = EnvironmentMode.LIVE_LSL
    project: ProjectSnapshot | None = None
    stale_reasons: tuple[str, ...] = ()
    issues: tuple[UiIssue, ...] = ()
    apparatus: ApparatusSnapshot = ApparatusSnapshot()
    build: BuildSnapshot = BuildSnapshot()
    task: TaskSnapshot = TaskSnapshot()
    runner: RunnerSnapshot = RunnerSnapshot()
    sessions: tuple[SessionSnapshot, ...] = ()
    selected_session_id: str | None = None
    replay: ReplaySnapshot = ReplaySnapshot()
    busy_action: str | None = None

    def navigate(self, page: Page) -> WorkbenchState:
        return replace(self, page=Page(page))

    def next_workflow_page(self) -> Page | None:
        if self.page not in ACTIVE_WORKFLOW:
            return None
        position = ACTIVE_WORKFLOW.index(self.page)
        if position >= len(ACTIVE_WORKFLOW) - 1:
            return None
        return ACTIVE_WORKFLOW[position + 1]

    @property
    def highest_status_label(self) -> str:
        if any(issue.severity == IssueSeverity.ERROR for issue in self.issues):
            return "ATTENTION REQUIRED"
        if self.lifecycle == Lifecycle.NO_PROJECT:
            return "NO PROJECT"
        if self.lifecycle in {
            Lifecycle.PREFLIGHT_READY,
            Lifecycle.ARMED,
            Lifecycle.RUNNING,
            Lifecycle.FINALIZING,
            Lifecycle.SESSION_AVAILABLE,
            Lifecycle.REPLAYED,
        }:
            return "READY"
        return "SETUP REQUIRED"

    @property
    def busy(self) -> bool:
        return self.busy_action is not None

    def page_completed(self, page: Page) -> bool:
        if self.project is None:
            return False
        if page == Page.DESIGN:
            return True
        if page == Page.APPARATUS:
            return self.lifecycle not in {
                Lifecycle.NO_PROJECT,
                Lifecycle.DESIGN_READY,
                Lifecycle.APPARATUS_UNBOUND,
                Lifecycle.DEPLOYMENT_REVIEW,
            }
        if page == Page.BUILD:
            return self.lifecycle in {
                Lifecycle.PREFLIGHT_READY,
                Lifecycle.ARMED,
                Lifecycle.RUNNING,
                Lifecycle.FINALIZING,
                Lifecycle.SESSION_AVAILABLE,
                Lifecycle.REPLAYED,
            }
        if page == Page.RUN:
            return self.lifecycle in {Lifecycle.SESSION_AVAILABLE, Lifecycle.REPLAYED}
        if page == Page.SESSIONS:
            return bool(self.sessions)
        return False

    def page_needs_attention(self, page: Page) -> bool:
        return page == Page.APPARATUS and self.lifecycle == Lifecycle.APPARATUS_UNBOUND
