"""Qt controller owning Workbench state and the EEGle service boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

from demos.workbench.operations import (
    AcceptedDeployment,
    BuildResult,
    LiveRecheckResult,
    SiteOverlay,
    WorkbenchDetection,
    WorkbenchOperations,
)
from demos.workbench.platform_support import (
    discovery_remediation as _discovery_remediation,
)
from demos.workbench.preview import PreviewManager
from demos.workbench.profile import Study1Profile, load_study1_profile
from demos.workbench.project import bootstrap_study1_project
from demos.workbench.runner_process import RunnerProcess
from demos.workbench.settings import WorkbenchSettings
from demos.workbench.state import (
    ApparatusSnapshot,
    BuildSnapshot,
    EnvironmentMode,
    IssueSeverity,
    Lifecycle,
    Page,
    ProjectSnapshot,
    ReplaySnapshot,
    RunnerSnapshot,
    RunnerStatus,
    SessionSnapshot,
    TaskSnapshot,
    TaskStatus,
    UiIssue,
    WorkbenchState,
)
from demos.workbench.task_process import TaskProcess
from eegle.operations import OperationError


class _WorkerSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(object)


class _Worker(QRunnable):
    def __init__(self, operation: Callable[[], Any]) -> None:
        super().__init__()
        self.operation = operation
        self.signals = _WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            value = self.operation()
        except Exception as exc:  # noqa: BLE001 - worker boundary reports UI failure
            self.signals.failed.emit(exc)
        else:
            self.signals.succeeded.emit(value)


class WorkbenchController(QObject):
    """Own all mutations and keep views away from EEGle execution services."""

    state_changed = Signal(object)
    preview_changed = Signal(object)

    def __init__(
        self,
        *,
        project_root: Path | None = None,
        profile: Study1Profile | None = None,
        settings: WorkbenchSettings | None = None,
        operations: WorkbenchOperations | None = None,
        task_process: TaskProcess | None = None,
        runner_process: RunnerProcess | None = None,
        preview_manager: PreviewManager | None = None,
        thread_pool: QThreadPool | None = None,
    ) -> None:
        super().__init__()
        self.profile = profile or load_study1_profile()
        self.settings = settings or WorkbenchSettings()
        self.operations = operations or WorkbenchOperations()
        self.task_process = task_process or TaskProcess(self)
        self.runner_process = runner_process or RunnerProcess(self)
        self.preview_manager = preview_manager or PreviewManager(self)
        self.thread_pool = thread_pool or QThreadPool.globalInstance()
        self._detection: WorkbenchDetection | None = None
        self._accepted: AcceptedDeployment | None = None
        self._workers: set[_Worker] = set()
        self.task_process.message_received.connect(self._handle_task_message)
        self.runner_process.message_received.connect(self._handle_runner_message)
        self.preview_manager.payload.connect(self.preview_changed)
        try:
            result = bootstrap_study1_project(self.profile, project_root=project_root)
        except Exception as exc:  # noqa: BLE001 - startup failure must render in shell
            self.state = WorkbenchState(
                page=Page.EXPERIMENTS,
                lifecycle=Lifecycle.NO_PROJECT,
                issues=(
                    UiIssue(
                        "workbench.project_unavailable",
                        "Prepared project could not be opened",
                        str(exc),
                        IssueSeverity.ERROR,
                    ),
                ),
            )
        else:
            self.settings.record_project(result.project.root)
            sessions = self._safe_initial_sessions(result.snapshot.project_root)
            self.state = WorkbenchState(
                page=Page.DESIGN,
                lifecycle=(Lifecycle.SESSION_AVAILABLE if sessions else Lifecycle.APPARATUS_UNBOUND),
                project=result.snapshot,
                stale_reasons=(("Prepared profile digest changed",) if result.issues else ()),
                issues=result.issues,
                task=self._closed_task_snapshot(),
                sessions=sessions,
                selected_session_id=sessions[0].session_id if sessions else None,
            )

    def _safe_initial_sessions(self, project_root: Path) -> tuple[SessionSnapshot, ...]:
        try:
            return self.operations.inspect_project_sessions(project_root)
        except Exception:  # noqa: BLE001 - project inspection is a startup boundary
            return ()

    def navigate(self, page: Page) -> None:
        selected = Page(page)
        if selected != self.state.page:
            self.state = self.state.navigate(selected)
            self._emit()

    def continue_workflow(self) -> None:
        target = self.state.next_workflow_page()
        if target is not None:
            self.navigate(target)

    def open_active_project(self) -> None:
        if self.state.project is not None:
            self.navigate(Page.DESIGN)

    def set_environment(self, mode: EnvironmentMode) -> None:
        selected = EnvironmentMode(mode)
        if selected == EnvironmentMode.BENCH_TEST or selected == self.state.environment_mode:
            return
        if self.runner_process.running:
            self._append_issue(
                "workbench.environment_run_active",
                "Stop the active run before switching environments",
                "Environment changes require the separately confirmed Stop action so the partial session can finish registering.",
            )
            return
        if self.task_process.running:
            self.task_process.abort("environment_changed")
        self.preview_manager.stop()
        self._detection = None
        self._accepted = None
        self.state = replace(
            self.state,
            page=Page.APPARATUS,
            lifecycle=(Lifecycle.DEPLOYMENT_ACCEPTED if selected == EnvironmentMode.SIMULATION else Lifecycle.APPARATUS_UNBOUND),
            environment_mode=selected,
            apparatus=ApparatusSnapshot(),
            build=BuildSnapshot(),
            task=self._closed_task_snapshot(),
            runner=RunnerSnapshot(),
            replay=ReplaySnapshot(),
            busy_action=None,
            issues=tuple(
                issue for issue in self.state.issues
                if not issue.code.startswith(("workbench.lsl", "workbench.operation"))
            ),
        )
        self._emit()

    def arm_task(self, participant_pseudonym: str) -> None:
        project = self.state.project
        participant = participant_pseudonym.strip()
        if project is None or self.state.environment_mode != EnvironmentMode.LIVE_LSL or self.task_process.running or not participant:
            return
        task_python = self.settings.task_python()
        renderer = "psychopy" if task_python is not None else "qt_preview"
        session_id = self._session_id("live")
        self.state = replace(
            self.state,
            task=TaskSnapshot(
                status=TaskStatus.STARTING,
                renderer=renderer,
                session_id=session_id,
                total_trials=self._total_trials(),
                message=(
                    "Opening PsychoPy and arming the task marker outlet…"
                    if task_python is not None
                    else "Opening the non-recording Qt fallback; configure the task Python environment for live Start…"
                ),
                participant_pseudonym=participant,
                task_python=None if task_python is None else str(task_python),
            ),
        )
        self._emit()
        self.task_process.launch(
            project.project_root,
            session_id=session_id,
            participant=participant,
            variant=project.profile_variant,
            controlled_start=True,
            renderer=renderer,
            python_executable=task_python,
        )

    def configure_task_python(self, path: Path) -> None:
        value = Path(path).expanduser().resolve()
        if not value.is_file() or self.task_process.running:
            return
        self.settings.set_task_python(value)
        self.state = replace(self.state, task=self._closed_task_snapshot())
        self._emit()

    def arm_task_preview(self) -> None:
        """Compatibility action for older page tests; never qualifies live Start."""
        self.arm_task("DEMO-001")

    def scan_lsl(self) -> None:
        project = self.state.project
        if project is None or self.state.busy or self.state.environment_mode != EnvironmentMode.LIVE_LSL:
            return
        self._run_operation(
            "Scanning LSL",
            lambda: self.operations.scan_lsl(project.project_root),
            self._scan_succeeded,
        )

    def accept_live_deployment(
        self,
        eeg_capability_id: str,
        marker_capability_id: str,
        overlay: SiteOverlay,
    ) -> None:
        project = self.state.project
        if project is None or self._detection is None or self.state.busy:
            return
        self.state = replace(self.state, lifecycle=Lifecycle.DEPLOYMENT_REVIEW)
        self._emit()
        self._run_operation(
            "Creating reviewed deployment",
            lambda: self.operations.accept_live_deployment(
                project.project_root,
                self.profile,
                self._detection,
                eeg_capability_id=eeg_capability_id,
                marker_capability_id=marker_capability_id,
                overlay=overlay,
            ),
            self._deployment_succeeded,
        )

    def compile_and_preflight(self) -> None:
        project = self.state.project
        simulation = self.state.environment_mode == EnvironmentMode.SIMULATION
        if project is None or self.state.busy or (not simulation and self._accepted is None):
            return
        report = None if simulation else self._accepted.report
        self._run_operation(
            "Compiling and checking preflight",
            lambda: self.operations.compile_and_preflight(project.project_root, report, simulation=simulation),
            self._build_succeeded,
        )

    def start_run(
        self,
        participant_pseudonym: str,
        timing_acknowledged: bool = False,
        warnings_acknowledged: bool = False,
    ) -> None:
        project = self.state.project
        if project is None or self.state.busy or not self.state.build.preflight_ready:
            return
        if self.state.environment_mode == EnvironmentMode.SIMULATION:
            self._launch_runner(self._session_id("simulation"))
            return
        participant = participant_pseudonym.strip()
        task = self.state.task
        eligible = (
            task.status == TaskStatus.ARMED
            and task.renderer == "psychopy"
            and task.lsl_available
            and participant
            and participant == task.participant_pseudonym
            and (task.timing_validated or timing_acknowledged)
            and warnings_acknowledged
            and self._accepted is not None
        )
        if not eligible:
            self._append_issue(
                "workbench.run_prerequisites",
                "Live Start prerequisites changed",
                "Arm PsychoPy for this participant, detect both exact streams, accept the overlay, and complete a ready preflight.",
            )
            return
        self._run_operation(
            "Refreshing live identity and preflight",
            lambda: self.operations.recheck_live_deployment(project.project_root, self.profile, self._accepted),
            self._live_recheck_succeeded,
        )

    def stop_run(self, reason: str = "operator_stop") -> None:
        if self.task_process.running:
            self.task_process.abort(reason)
        if self.runner_process.running:
            self.runner_process.cancel(reason)
            self.state = replace(
                self.state,
                lifecycle=Lifecycle.FINALIZING,
                runner=replace(
                    self.state.runner,
                    status=RunnerStatus.CANCELLING,
                    message="Cancelling EEGle and finalizing a partial session…",
                ),
            )
            self._emit()

    def select_session(self, session_id: str) -> None:
        if any(value.session_id == session_id for value in self.state.sessions):
            self.state = replace(self.state, selected_session_id=session_id)
            self._emit()

    def replay_selected_session(self, session_id: str | None = None) -> None:
        selected = session_id or self.state.selected_session_id
        session = next((value for value in self.state.sessions if value.session_id == selected), None)
        if session is None or not session.replay_ready or self.state.busy:
            return
        self._run_operation(
            "Replaying recorded evidence",
            lambda: self.operations.replay_session(session),
            self._replay_succeeded,
        )

    def shutdown(self) -> None:
        self.preview_manager.stop()
        self.task_process.shutdown()
        self.runner_process.shutdown()

    def _live_recheck_succeeded(self, value: LiveRecheckResult) -> None:
        self._detection = value.detection
        self._accepted = value.accepted
        self._start_preview(value.accepted)
        if not value.preflight.ready:
            self.state = replace(
                self.state,
                lifecycle=Lifecycle.PREFLIGHT_WARNING,
                page=Page.APPARATUS,
                build=replace(
                    self.state.build,
                    preflight_ready=False,
                    preflight_report_hash=value.preflight.report_hash,
                    checks=value.checks,
                ),
            )
            self._append_issue(
                "workbench.live_recheck_failed",
                "Fresh live preflight is not ready",
                "The current streams no longer satisfy the compiled deployment. Review Apparatus again.",
            )
            return
        self.state = replace(
            self.state,
            build=replace(
                self.state.build,
                preflight_ready=True,
                preflight_report_hash=value.preflight.report_hash,
                checks=value.checks,
            ),
        )
        self._launch_runner(self.state.task.session_id or self._session_id("live"))

    def _launch_runner(self, session_id: str) -> None:
        project = self.state.project
        if project is None or self.runner_process.running:
            return
        self.state = replace(
            self.state,
            page=Page.RUN,
            lifecycle=Lifecycle.ARMED,
            runner=RunnerSnapshot(
                status=RunnerStatus.STARTING,
                session_id=session_id,
                plan_hash=self.state.build.plan_hash,
                message="Validating the immutable EEGle plan and lock…",
            ),
        )
        self._emit()
        self.runner_process.launch(project.project_root, session_id=session_id, mode=self.state.environment_mode.value)

    def _run_operation(self, label: str, operation: Callable[[], Any], on_success: Callable[[Any], None]) -> None:
        self.state = replace(self.state, busy_action=label)
        self._emit()
        worker = _Worker(operation)
        self._workers.add(worker)

        def succeeded(value: Any) -> None:
            self._workers.discard(worker)
            self.state = replace(self.state, busy_action=None)
            on_success(value)

        def failed(exc: Exception) -> None:
            self._workers.discard(worker)
            self._operation_failed(exc)

        worker.signals.succeeded.connect(succeeded)
        worker.signals.failed.connect(failed)
        self.thread_pool.start(worker)

    def _scan_succeeded(self, value: WorkbenchDetection) -> None:
        self._detection = value
        selected_eeg = next((item.capability_id for item in value.streams if item.content_kind == "dense_samples" and item.channel_count == 65 and item.nominal_rate_hz == 1000.0), None)
        selected_markers = next((item.capability_id for item in value.streams if item.content_kind == "sparse_events" and item.name == self.profile.payload["markers"]["stream_name"]), None)
        apparatus = ApparatusSnapshot(
            dependency_available=value.lsl.support.dependency_available,
            library_version=value.lsl.support.library_version,
            support_level=value.lsl.support.support_level.value,
            unavailable_reason=value.lsl.support.unavailable_reason,
            remediation=tuple(value.lsl.support.remediation),
            scan_wait_seconds=value.wait_seconds,
            detection_report_hash=value.report.report_hash,
            streams=value.streams,
            selected_eeg_capability_id=selected_eeg,
            selected_marker_capability_id=selected_markers,
        )
        issues = tuple(issue for issue in self.state.issues if not issue.code.startswith("workbench.lsl"))
        if not value.lsl.support.dependency_available:
            issues += (
                UiIssue(
                    "workbench.lsl_dependency_missing",
                    "Live LSL dependency could not be loaded",
                    "\n".join(
                        (
                            value.lsl.support.unavailable_reason
                            or "pylsl could not be imported.",
                            *value.lsl.support.remediation,
                        )
                    ),
                    IssueSeverity.ERROR,
                ),
            )
        elif not value.streams:
            issues += (
                UiIssue(
                    "workbench.lsl_no_streams",
                    "No LSL streams were detected",
                    "\n".join(
                        (
                            (
                                f"pylsl {value.lsl.support.library_version or 'unknown'}"
                                f" resolved no streams in {value.wait_seconds:g} s."
                            ),
                            "Start Neuracle acquisition and arm the DSART task, then scan again.",
                            *_discovery_remediation(),
                        )
                    ),
                ),
            )
        self.state = replace(self.state, lifecycle=Lifecycle.APPARATUS_UNBOUND, apparatus=apparatus, build=BuildSnapshot(), issues=issues)
        self._emit()

    def _deployment_succeeded(self, value: AcceptedDeployment) -> None:
        self._accepted = value
        self._start_preview(value)
        proposal = value.result.proposal
        self.state = replace(
            self.state,
            lifecycle=Lifecycle.DEPLOYMENT_ACCEPTED,
            page=Page.BUILD,
            apparatus=replace(
                self.state.apparatus,
                selected_eeg_capability_id=value.eeg_capability_id.removesuffix(".reviewed"),
                selected_marker_capability_id=value.marker_capability_id.removesuffix(".reviewed"),
                proposal_hash=proposal.proposal_hash,
                deployment_hash=proposal.deployment.spec_hash,
                reviewed_overlay=True,
                reference=value.overlay.reference,
                ground=value.overlay.ground,
                auxiliary_allocation=value.overlay.auxiliary_allocation,
            ),
        )
        self._emit()

    def _build_succeeded(self, value: BuildResult) -> None:
        self.state = replace(
            self.state,
            lifecycle=Lifecycle.PREFLIGHT_READY if value.preflight.ready else Lifecycle.PREFLIGHT_WARNING,
            build=BuildSnapshot(
                plan_hash=value.compilation.plan.plan_hash,
                lock_hash=value.compilation.lock.lock_hash,
                preflight_report_hash=value.preflight.report_hash,
                preflight_ready=value.preflight.ready,
                checks=value.checks,
                deployment_role=value.deployment_role,
            ),
            page=Page.RUN if value.preflight.ready else Page.BUILD,
        )
        self._emit()

    def _replay_succeeded(self, value: ReplaySnapshot) -> None:
        self.state = replace(self.state, lifecycle=Lifecycle.REPLAYED, page=Page.REPLAY, replay=value)
        self._emit()

    def _operation_failed(self, exc: Exception) -> None:
        details = "\n".join(f"{value.title}: {value.message}" for value in exc.diagnostics) if isinstance(exc, OperationError) else str(exc)
        lifecycle = Lifecycle.APPARATUS_UNBOUND if self.state.lifecycle == Lifecycle.DEPLOYMENT_REVIEW else self.state.lifecycle
        self.state = replace(
            self.state,
            busy_action=None,
            lifecycle=lifecycle,
            issues=(*self.state.issues, UiIssue("workbench.operation_failed", "Workbench operation failed", details or type(exc).__name__, IssueSeverity.ERROR)),
        )
        self._emit()

    def _handle_runner_message(self, payload: dict[str, Any]) -> None:
        kind = str(payload.get("type", ""))
        runner = self.state.runner
        if kind == "ready":
            runner = replace(runner, status=RunnerStatus.READY, plan_hash=str(payload.get("plan_hash") or runner.plan_hash or "") or None, message="EEGle runner is ready; starting the controlled session…")
            self.state = replace(self.state, runner=runner)
            self._emit()
            self.runner_process.start_run()
            return
        if kind == "state":
            phase = str(payload.get("state", ""))
            status = {"running": RunnerStatus.RUNNING, "finalizing": RunnerStatus.FINALIZING, "cancelling": RunnerStatus.CANCELLING}.get(phase, runner.status)
            self.state = replace(self.state, runner=replace(runner, status=status, message=f"EEGle runner: {phase}."), lifecycle=Lifecycle.RUNNING if phase == "running" else Lifecycle.FINALIZING)
            self._emit()
            if phase == "running" and self.state.environment_mode == EnvironmentMode.LIVE_LSL:
                self.task_process.start_task()
            return
        if kind == "completed":
            run = dict(payload.get("project_run") or {})
            run_status = str(run.get("status") or "unknown")
            cancelled = run_status == "cancelled"
            self.state = replace(
                self.state,
                runner=replace(
                    runner,
                    status=RunnerStatus.CANCELLED if cancelled else RunnerStatus.COMPLETED,
                    bundle_id=None if run.get("bundle_id") is None else str(run["bundle_id"]),
                    run_status=run_status,
                    evidence_record_count=int(run.get("evidence_record_count", 0)),
                    terminal_reason=None if run.get("terminal_reason") is None else str(run["terminal_reason"]),
                    message="Partial session registered after cancellation." if cancelled else "Session finalized and registered.",
                ),
            )
            self._refresh_sessions()
            return
        if kind == "failed":
            error = dict(payload.get("error") or {})
            if self.task_process.running:
                self.task_process.abort("runner_failed")
            message = str(error.get("message", "EEGle runner failed."))
            detail = str(error.get("traceback") or "").strip()
            self.state = replace(self.state, runner=replace(runner, status=RunnerStatus.FAILED, message=message), lifecycle=Lifecycle.FINALIZING)
            self._append_issue(
                str(error.get("code", "workbench.runner_failed")),
                "EEGle runner failed",
                f"{message}\n\n{detail}" if detail else message,
                IssueSeverity.ERROR,
            )
            return
        if kind == "warning":
            self._append_issue(str(payload.get("code", "workbench.runner_warning")), "EEGle runner warning", str(payload.get("message", "Unknown runner warning")))

    def _handle_task_message(self, payload: dict[str, Any]) -> None:
        kind = str(payload.get("type", ""))
        task = self.state.task
        if kind == "armed":
            marker = dict(payload.get("marker_stream") or {})
            display = dict(payload.get("display") or {})
            measured = payload.get(
                "measured_refresh_rate_hz",
                display.get("measured_refresh_rate_hz"),
            )
            task = replace(
                task,
                status=TaskStatus.ARMED,
                renderer=str(payload.get("renderer", task.renderer or "unknown")),
                marker_stream_name=str(marker.get("name", "")) or None,
                marker_source_id=str(marker.get("source_id", "")) or None,
                lsl_available=bool(payload.get("lsl_available", False)),
                timing_validated=bool(payload.get("timing_validated", False)),
                measured_refresh_rate_hz=None if measured is None else float(measured),
                plan_hash=str(payload.get("plan_hash") or "") or None,
                total_trials=int(payload.get("total_trials", task.total_trials)),
                message="PsychoPy is armed; marker outlet and display timing are ready." if payload.get("lsl_available") and payload.get("timing_validated") else "Task is armed, but live prerequisites still need attention.",
            )
        elif kind == "state" and payload.get("state") == "running":
            task = replace(task, status=TaskStatus.RUNNING, message="DSART rehearsal is running.")
        elif kind == "phase":
            task = replace(task, phase=str(payload.get("phase", "")) or None)
        elif kind == "trial":
            task = replace(task, current_trial=int(payload.get("current", 0)), total_trials=int(payload.get("total", task.total_trials)))
        elif kind == "completed":
            task = replace(task, status=TaskStatus.COMPLETED, message="DSART rehearsal completed; draining trailing markers…", behavior_summary=dict(payload.get("behavior_summary") or {}))
            self.state = replace(self.state, task=task, lifecycle=Lifecycle.FINALIZING, runner=replace(self.state.runner, status=RunnerStatus.FINALIZING, message="Draining the final task marker before graceful completion…"))
            self._emit()
            QTimer.singleShot(500, lambda: self.runner_process.complete("task_complete"))
            return
        elif kind == "aborted":
            task = replace(task, status=TaskStatus.ABORTED, message=f"Task aborted: {payload.get('reason', 'unknown')}", behavior_summary=dict(payload.get("behavior_summary") or {}))
            if self.runner_process.running and self.state.runner.status not in {RunnerStatus.CANCELLING, RunnerStatus.COMPLETED, RunnerStatus.CANCELLED}:
                self.runner_process.cancel("task_aborted")
        elif kind == "failed":
            error = dict(payload.get("error") or {})
            task = replace(task, status=TaskStatus.FAILED, message=str(error.get("message", "Task process failed.")))
            if self.runner_process.running:
                self.runner_process.cancel("task_failed")
        elif kind == "warning":
            self._append_issue(str(payload.get("code", "workbench.task_warning")), "Task process warning", str(payload.get("message", "Unknown task warning")))
            return
        self.state = replace(self.state, task=task)
        self._emit()

    def _refresh_sessions(self) -> None:
        project = self.state.project
        if project is not None:
            self._run_operation(
                "Refreshing project and inspecting session",
                lambda: self._load_project_sessions(project.project_root),
                self._sessions_refreshed,
            )

    def _load_project_sessions(
        self,
        project_root: Path,
    ) -> tuple[ProjectSnapshot, tuple[SessionSnapshot, ...]]:
        refreshed = bootstrap_study1_project(
            self.profile,
            project_root=project_root,
        )
        sessions = self.operations.inspect_project_sessions(project_root)
        return refreshed.snapshot, sessions

    def _sessions_refreshed(
        self,
        value: tuple[ProjectSnapshot, tuple[SessionSnapshot, ...]],
    ) -> None:
        snapshot, sessions = value
        self.preview_manager.stop()
        selected = self.state.runner.session_id
        if not any(value.session_id == selected for value in sessions):
            selected = sessions[0].session_id if sessions else None
        self.state = replace(
            self.state,
            project=snapshot,
            lifecycle=Lifecycle.SESSION_AVAILABLE if sessions else self.state.lifecycle,
            page=Page.SESSIONS,
            sessions=sessions,
            selected_session_id=selected,
        )
        self._emit()

    def _append_issue(self, code: str, title: str, message: str, severity: IssueSeverity = IssueSeverity.WARNING) -> None:
        self.state = replace(self.state, issues=(*self.state.issues, UiIssue(code, title, message, severity)))
        self._emit()

    def _total_trials(self) -> int:
        project = self.state.project
        return 0 if project is None else sum(int(value["trials"]) for value in self.profile.variant(project.profile_variant)["blocks"])

    def _closed_task_snapshot(self) -> TaskSnapshot:
        task_python = self.settings.task_python()
        return TaskSnapshot(
            task_python=None if task_python is None else str(task_python),
            message=(
                f"PsychoPy task environment: {task_python}"
                if task_python is not None
                else "PsychoPy task environment not configured; Qt fallback is presentation-only."
            ),
        )

    def _start_preview(self, accepted: AcceptedDeployment) -> None:
        by_id = {value.capability_id: value for value in accepted.report.sources}
        eeg = by_id.get(accepted.eeg_capability_id)
        markers = by_id.get(accepted.marker_capability_id)
        if eeg is None or markers is None:
            return
        self.preview_manager.start(
            eeg.config,
            markers.config,
            tuple(self.profile.logical_channels[:8]),
        )

    @staticmethod
    def _session_id(kind: str) -> str:
        return f"study1.{kind}." + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")

    def _emit(self) -> None:
        self.state_changed.emit(self.state)
