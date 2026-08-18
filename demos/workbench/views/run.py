"""Study 1 operator surface for controlled live and simulation runs."""

from __future__ import annotations

from pyqtgraph import GraphicsLayoutWidget, mkPen
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from demos.workbench.profile import Study1Profile
from demos.workbench.state import (
    EnvironmentMode,
    ProjectSnapshot,
    RunnerStatus,
    TaskStatus,
    WorkbenchState,
)
from demos.workbench.views.base import WorkbenchPage
from demos.workbench.views.helpers import key_value_rows
from demos.workbench.widgets import Card, EmptyState, InfoBanner, short_digest


class RunPage(WorkbenchPage):
    arm_task_requested = Signal(str)
    start_requested = Signal(str, bool, bool)
    stop_requested = Signal()

    def __init__(self, snapshot: ProjectSnapshot, profile: Study1Profile) -> None:
        super().__init__(
            "Run Experiment",
            "A calm operator surface before, during, and immediately after acquisition.",
        )
        self.snapshot = snapshot
        selected = profile.variant(snapshot.profile_variant)
        self.main_trials = sum(int(block["trials"]) for block in selected["blocks"])
        self.practice_trials = int(selected["practice"]["trials_per_round"])
        self.readiness = InfoBanner(
            "Live session needs current evidence",
            "Arm PsychoPy, detect and accept the exact EEG/marker identities, compile, and pass a fresh preflight immediately before Start.",
            tone="warning",
        )
        self.content.addWidget(self.readiness)

        summary = Card()
        header = QHBoxLayout()
        title = QLabel("Final run summary")
        title.setProperty("role", "sectionTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.mode_label = QLabel("LIVE LSL")
        self.mode_label.setProperty("role", "eyebrow")
        header.addWidget(self.mode_label)
        summary.layout.addLayout(header)
        summary.layout.addWidget(
            key_value_rows(
                (
                    ("Project", snapshot.project_id),
                    ("Task", f"Dynamic SART · {self.practice_trials} practice + {self.main_trials} main trials"),
                    ("Baseline", "15 s eyes open + 15 s eyes closed"),
                    ("Evidence destination", str(snapshot.project_root / "sessions")),
                    ("Scientific behavior", "Observe-only; no feedback or model input labels"),
                )
            )
        )
        self.plan_line = QLabel("Immutable plan · Not compiled")
        self.binding_line = QLabel("EEG / markers · Unbound")
        for value in (self.plan_line, self.binding_line):
            value.setProperty("role", "muted")
            summary.layout.addWidget(value)
        self.content.addWidget(summary)

        operator = Card()
        operator.layout.addWidget(QLabel("Operator confirmation"))
        form = QFormLayout()
        self.participant = QLineEdit()
        self.participant.setPlaceholderText("Required for live task arming")
        self.participant.setText("DEMO-001")
        self.participant.setToolTip("Memory-only; never written to Workbench settings or logs.")
        self.participant.textChanged.connect(self._sync_primary)
        form.addRow("Participant pseudonym", self.participant)
        self.timing_ack = QCheckBox(
            "Acknowledge display refresh warning for this demonstration"
        )
        self.timing_ack.toggled.connect(self._sync_primary)
        form.addRow("", self.timing_ack)
        self.warning_ack = QCheckBox(
            "Confirm demonstration-only, observe-only status and reviewed laboratory warnings"
        )
        self.warning_ack.toggled.connect(self._sync_primary)
        form.addRow("", self.warning_ack)
        operator.layout.addLayout(form)
        self.operator_note = QLabel(
            "The pseudonym stays in memory until task launch; behavioral output stores only its hash."
        )
        self.operator_note.setProperty("role", "muted")
        self.operator_note.setWordWrap(True)
        operator.layout.addWidget(self.operator_note)
        self.content.addWidget(operator)

        grid_root = QWidget()
        grid = QGridLayout(grid_root)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(14)
        monitor = Card()
        monitor.layout.addWidget(QLabel("READ-ONLY PREVIEW — visualization only"))
        self.monitor_empty = EmptyState(
            "No preview stream is open",
            "No waveform is invented. The independent preview remains empty until a selected live inlet supplies current samples.",
            minimum_height=205,
        )
        monitor.layout.addWidget(self.monitor_empty)
        self.preview_plot = GraphicsLayoutWidget()
        self.preview_plot.setMinimumHeight(270)
        self.preview_plot.hide()
        self.preview_curves = []
        colors = ("#2463EB", "#0F766E", "#7C3AED", "#B45309")
        for index in range(8):
            plot = self.preview_plot.addPlot(row=index, col=0)
            plot.hideAxis("bottom" if index < 7 else "left")
            if index == 7:
                plot.setLabel("bottom", "Seconds")
            plot.setMouseEnabled(x=False, y=False)
            curve = plot.plot(pen=mkPen(colors[index % len(colors)], width=1))
            self.preview_curves.append((plot, curve))
        monitor.layout.addWidget(self.preview_plot)
        self.preview_status = QLabel("Preview · closed")
        self.preview_status.setProperty("role", "muted")
        self.preview_status.setWordWrap(True)
        monitor.layout.addWidget(self.preview_status)
        grid.addWidget(monitor, 0, 0, 1, 2)

        task_card = Card()
        task_card.layout.addWidget(QLabel("Task progress"))
        self.task_state = QLabel("State · Closed")
        self.task_phase = QLabel("Phase · —")
        self.task_trial = QLabel(f"Trial · 0 / {self.main_trials}")
        self.task_timing = QLabel("Display timing · Not measured")
        for value in (self.task_state, self.task_phase, self.task_trial, self.task_timing):
            value.setProperty("role", "muted")
            task_card.layout.addWidget(value)

        health = Card()
        health.layout.addWidget(QLabel("Execution lifecycle"))
        self.preflight_state = QLabel("Preflight · Pending")
        self.marker_state = QLabel("Marker outlet · Closed")
        self.runner_state = QLabel("EEGle runner · Closed")
        self.result_state = QLabel("Session · Not started")
        for value in (self.preflight_state, self.marker_state, self.runner_state, self.result_state):
            value.setProperty("role", "muted")
            value.setWordWrap(True)
            health.layout.addWidget(value)
        grid.addWidget(task_card, 1, 0)
        grid.addWidget(health, 1, 1)
        self.content.addWidget(grid_root)

        self.stop_button = QPushButton("Stop & keep partial session")
        self.stop_button.setProperty("destructive", True)
        self.stop_button.clicked.connect(self.stop_requested)
        self.action_bar.layout().insertWidget(
            self.action_bar.layout().count() - 1,
            self.stop_button,
        )
        self.stop_button.hide()
        self._state: WorkbenchState | None = None
        self.primary_requested.connect(self._primary)
        self.configure_primary("Start unavailable", enabled=False, hint="Complete Build first.")
        self.finish()

    def render_preview(self, payload: dict[str, object]) -> None:
        kind = str(payload.get("type", ""))
        if kind == "status":
            status = str(payload.get("status", "unknown"))
            message = str(payload.get("message", ""))
            self.preview_status.setText(
                f"Preview · {status}" + (f" · {message}" if message else "")
            )
            if status in {"closed", "disconnected", "interrupted"}:
                self.preview_plot.hide()
                self.monitor_empty.show()
            return
        if kind != "preview":
            return
        times = tuple(payload.get("times") or ())
        channels = tuple(payload.get("channels") or ())
        labels = tuple(payload.get("labels") or ())
        if not times or not channels:
            return
        for index, (plot, curve) in enumerate(self.preview_curves):
            if index < len(channels):
                curve.setData(times, channels[index])
                plot.setTitle(str(labels[index]) if index < len(labels) else f"Position {index + 1}")
        events = tuple(payload.get("events") or ())
        event_text = ", ".join(str(value.get("kind", "event")) for value in events[-6:])
        rate = float(payload.get("effective_rate_hz") or 0.0)
        self.preview_status.setText(
            f"Preview · connected · effective {rate:.1f} samples/s"
            + (f" · recent markers: {event_text}" if event_text else " · no task markers yet")
        )
        self.monitor_empty.hide()
        self.preview_plot.show()

    def render_state(self, state: WorkbenchState) -> None:
        self._state = state
        simulation = state.environment_mode == EnvironmentMode.SIMULATION
        self.mode_label.setText("SIMULATION" if simulation else "LIVE LSL")
        self.plan_line.setText("Immutable plan · " + short_digest(state.build.plan_hash))
        self.binding_line.setText(
            "Source · deterministic simulation deployment"
            if simulation
            else "EEG / markers · "
            + ("reviewed live deployment" if state.apparatus.reviewed_overlay else "unbound")
        )
        self.readiness.title.setText(
            "Deterministic simulation rehearsal"
            if simulation
            else "Live session needs current evidence"
        )
        self.readiness.message.setText(
            "This uses EEGle's standard simulation deployment and the same runner, inspection, and replay flow. It makes no Neuracle, PsychoPy, or marker-synchronization claim."
            if simulation
            else "Arm PsychoPy, detect and accept the exact EEG/marker identities, compile, and pass a fresh preflight immediately before Start."
        )
        self.participant.setEnabled(not simulation and state.runner.status in {RunnerStatus.CLOSED, RunnerStatus.FAILED, RunnerStatus.COMPLETED, RunnerStatus.CANCELLED})
        self.timing_ack.setVisible(not simulation)
        self.warning_ack.setVisible(not simulation)
        self.operator_note.setText(
            "Participant identity is not used by deterministic simulation."
            if simulation
            else "The pseudonym stays in memory until task launch; behavioral output stores only its hash."
        )
        task = state.task
        if task.participant_pseudonym and task.status in {
            TaskStatus.STARTING,
            TaskStatus.ARMED,
            TaskStatus.RUNNING,
        }:
            self.participant.blockSignals(True)
            self.participant.setText(task.participant_pseudonym)
            self.participant.blockSignals(False)
        if simulation:
            self.task_state.setText("External task · Not used by simulation")
            self.task_phase.setText("Phase · Engine-driven deterministic rehearsal")
            self.task_trial.setText("Trial · No PsychoPy timing claim")
            self.task_timing.setText("Display timing · Not evaluated")
        else:
            self.task_state.setText(f"State · {task.status.value.replace('_', ' ').title()}")
            self.task_phase.setText(f"Phase · {task.phase or '—'}")
            self.task_trial.setText(f"Trial · {task.current_trial} / {task.total_trials or self.main_trials}")
            refresh = "—" if task.measured_refresh_rate_hz is None else f"{task.measured_refresh_rate_hz:.2f} Hz"
            self.task_timing.setText(
                f"Display timing · {'Validated' if task.timing_validated else 'Unvalidated'} · {refresh}"
            )
        self.preflight_state.setText(
            "Preflight · Ready" if state.build.preflight_ready else "Preflight · Pending / blocked"
        )
        self.marker_state.setText(
            "Marker outlet · Not used by simulation"
            if simulation
            else (
                f"Marker outlet · {task.marker_stream_name}"
                if task.lsl_available
                else "Marker outlet · Unavailable"
            )
        )
        runner = state.runner
        self.runner_state.setText(
            f"EEGle runner · {runner.status.value.replace('_', ' ').title()} · {runner.message}"
        )
        self.result_state.setText(
            "Session · "
            + (
                f"{runner.run_status or 'unknown'} · {runner.evidence_record_count} evidence records · reason {runner.terminal_reason or '—'}"
                if runner.status in {RunnerStatus.COMPLETED, RunnerStatus.CANCELLED}
                else "Not finalized"
            )
        )
        active = runner.status in {
            RunnerStatus.STARTING,
            RunnerStatus.READY,
            RunnerStatus.RUNNING,
            RunnerStatus.FINALIZING,
            RunnerStatus.CANCELLING,
        }
        self.stop_button.setVisible(active)
        self.stop_button.setEnabled(runner.status not in {RunnerStatus.FINALIZING, RunnerStatus.CANCELLING})
        self._sync_primary()

    def _sync_primary(self) -> None:
        state = self._state
        if state is None:
            return
        runner = state.runner.status
        if runner in {
            RunnerStatus.STARTING,
            RunnerStatus.READY,
            RunnerStatus.RUNNING,
            RunnerStatus.FINALIZING,
            RunnerStatus.CANCELLING,
        }:
            self.configure_primary("Run in progress", enabled=False, hint=state.runner.message)
            return
        if state.environment_mode == EnvironmentMode.SIMULATION:
            self.configure_primary(
                "Start Simulation",
                enabled=state.build.preflight_ready and not state.busy,
                hint=(
                    "Run the deterministic rehearsal through the supervised EEGle runner."
                    if state.build.preflight_ready
                    else "Compile and pass simulation preflight first."
                ),
            )
            return
        task = state.task
        participant = self.participant.text().strip()
        if task.status in {TaskStatus.CLOSED, TaskStatus.COMPLETED, TaskStatus.ABORTED, TaskStatus.FAILED}:
            self.configure_primary(
                "Arm PsychoPy Task",
                enabled=bool(participant) and not state.busy,
                hint="Open the task and its marker outlet before the final LSL scan.",
            )
            return
        ready = (
            task.status == TaskStatus.ARMED
            and task.renderer == "psychopy"
            and task.lsl_available
            and participant == task.participant_pseudonym
            and state.apparatus.reviewed_overlay
            and state.build.preflight_ready
            and (task.timing_validated or self.timing_ack.isChecked())
            and self.warning_ack.isChecked()
        )
        self.configure_primary(
            "Start Live Recording",
            enabled=ready and not state.busy,
            hint=(
                "Fresh identity and preflight checks run once more before EEGle starts the task."
                if ready
                else "Use the same participant, arm PsychoPy, accept exact streams, and pass preflight."
            ),
        )

    def _primary(self) -> None:
        if self._state is None:
            return
        if self._state.environment_mode == EnvironmentMode.LIVE_LSL and self._state.task.status in {
            TaskStatus.CLOSED,
            TaskStatus.COMPLETED,
            TaskStatus.ABORTED,
            TaskStatus.FAILED,
        }:
            self.arm_task_requested.emit(self.participant.text().strip())
            return
        self.start_requested.emit(
            self.participant.text().strip(),
            self.timing_ack.isChecked(),
            self.warning_ack.isChecked(),
        )
