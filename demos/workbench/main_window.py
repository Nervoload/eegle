"""Native Workbench frame, navigation, and safe keyboard behavior."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from demos.workbench.controller import WorkbenchController
from demos.workbench.dialogs import NewExperimentDialog, SiteOverlayDialog
from demos.workbench.state import EnvironmentMode, Page, WorkbenchState
from demos.workbench.views import (
    ApparatusPage,
    BuildPage,
    DesignPage,
    ExperimentsPage,
    ReplayPage,
    RunPage,
    SessionsPage,
)
from demos.workbench.views.base import WorkbenchPage
from demos.workbench.widgets import EmptyState, InfoBanner, StatusIndicator
from demos.workbench.widgets.sidebar import Sidebar


class UnavailablePage(WorkbenchPage):
    def __init__(self, title: str, issue: str) -> None:
        super().__init__(title, "The prepared project is not currently available.")
        self.content.addWidget(InfoBanner("Startup issue", issue, tone="error"))
        self.content.addWidget(
            EmptyState(
                "Project required",
                "Resolve the startup issue and reopen Workbench before using this surface.",
            )
        )
        self.configure_primary("Unavailable", enabled=False, hint="No project is open.")
        self.finish()


class ContextHeader(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ContextHeader")
        self.setFixedHeight(56)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(22, 0, 24, 0)
        layout.setSpacing(9)
        self.experiment = QLabel("No active experiment")
        self.experiment.setProperty("role", "sectionTitle")
        layout.addWidget(self.experiment)
        layout.addStretch(1)
        self.environment = StatusIndicator("LIVE LSL", "blue")
        self.status = StatusIndicator("SETUP REQUIRED", "warning")
        layout.addWidget(self.environment)
        layout.addWidget(self.status)

    def render_state(self, state: WorkbenchState) -> None:
        self.experiment.setText(
            state.project.display_name if state.project is not None else "No active experiment"
        )
        self.environment.setText(state.environment_mode.value.replace("_", " ").upper())
        status = state.highest_status_label
        self.status.setText(status)
        tone = "success" if status == "READY" else "warning"
        if status == "NO PROJECT":
            tone = "neutral"
        self.status.set_tone(tone)


class WorkbenchWindow(QMainWindow):
    def __init__(self, controller: WorkbenchController) -> None:
        super().__init__()
        self.controller = controller
        self.setWindowTitle("EEGle Workbench")
        self.resize(1440, 900)
        self.setMinimumSize(1120, 720)
        root = QWidget()
        root.setObjectName("WorkbenchRoot")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.sidebar = Sidebar(controller.settings)
        layout.addWidget(self.sidebar)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        self.header = ContextHeader()
        right_layout.addWidget(self.header)
        self.stack = QStackedWidget()
        right_layout.addWidget(self.stack, 1)
        layout.addWidget(right, 1)
        self.pages: dict[Page, WorkbenchPage] = {}
        self._build_pages()
        self.sidebar.page_requested.connect(self.controller.navigate)
        self.sidebar.active_project_requested.connect(self.controller.open_active_project)
        self.sidebar.new_experiment_requested.connect(self._show_new_experiment)
        self.controller.state_changed.connect(self.render_state)
        self.render_state(self.controller.state)

    def _build_pages(self) -> None:
        snapshot = self.controller.state.project
        profile = self.controller.profile
        experiments = ExperimentsPage(snapshot)
        replay = ReplayPage(snapshot)
        replay.session_selected.connect(self.controller.select_session)
        replay.replay_requested.connect(self.controller.replay_selected_session)
        if snapshot is None:
            issue = self.controller.state.issues[0].message if self.controller.state.issues else "Unknown project error"
            active_pages = {
                Page.DESIGN: UnavailablePage("Experiment Design", issue),
                Page.APPARATUS: UnavailablePage("Apparatus", issue),
                Page.BUILD: UnavailablePage("Build Settings", issue),
                Page.RUN: UnavailablePage("Run Experiment", issue),
                Page.SESSIONS: UnavailablePage("Sessions", issue),
            }
        else:
            design = DesignPage(snapshot, profile)
            design.primary_requested.connect(self.controller.continue_workflow)
            apparatus = ApparatusPage(snapshot, profile)
            apparatus.scan_requested.connect(self.controller.scan_lsl)
            apparatus.arm_task_requested.connect(self.controller.arm_task)
            apparatus.review_requested.connect(self._review_deployment)
            apparatus.environment_requested.connect(
                lambda value: self.controller.set_environment(EnvironmentMode(value))
            )
            apparatus.continue_requested.connect(lambda: self.controller.navigate(Page.BUILD))
            apparatus.task_python_requested.connect(self._choose_task_python)
            build = BuildPage(snapshot)
            build.compile_requested.connect(self.controller.compile_and_preflight)
            build.continue_requested.connect(lambda: self.controller.navigate(Page.RUN))
            run = RunPage(snapshot, profile)
            self.controller.preview_changed.connect(run.render_preview)
            run.arm_task_requested.connect(self.controller.arm_task)
            run.start_requested.connect(self.controller.start_run)
            run.stop_requested.connect(self._confirm_stop)
            sessions = SessionsPage(snapshot)
            sessions.session_selected.connect(self.controller.select_session)
            sessions.replay_requested.connect(self.controller.replay_selected_session)
            active_pages = {
                Page.DESIGN: design,
                Page.APPARATUS: apparatus,
                Page.BUILD: build,
                Page.RUN: run,
                Page.SESSIONS: sessions,
            }
        experiments.open_project_requested.connect(self.controller.open_active_project)
        ordered = {
            Page.EXPERIMENTS: experiments,
            Page.REPLAY: replay,
            **active_pages,
        }
        for page, widget in ordered.items():
            self.pages[page] = widget
            self.stack.addWidget(widget)

    def render_state(self, state: WorkbenchState) -> None:
        self.header.render_state(state)
        self.sidebar.render_state(state)
        for page in self.pages.values():
            render = getattr(page, "render_state", None)
            if callable(render):
                render(state)
        self.stack.setCurrentWidget(self.pages[state.page])

    def _show_new_experiment(self) -> None:
        dialog = NewExperimentDialog(self)
        dialog.exec()

    def _review_deployment(self, eeg_capability_id: str, marker_capability_id: str) -> None:
        stream = next(
            (
                value
                for value in self.controller.state.apparatus.streams
                if value.capability_id == eeg_capability_id
            ),
            None,
        )
        if stream is None:
            return
        dialog = SiteOverlayDialog(stream, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self.controller.accept_live_deployment(
                eeg_capability_id,
                marker_capability_id,
                dialog.overlay(),
            )

    def _confirm_stop(self) -> None:
        answer = QMessageBox.question(
            self,
            "Stop current run?",
            "The task will abort and EEGle will register a cancelled, partial session where possible.",
            QMessageBox.StandardButton.Stop | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Stop:
            self.controller.stop_run("operator_stop")

    def _choose_task_python(self) -> None:
        selected, _filter = QFileDialog.getOpenFileName(
            self,
            "Select the dedicated PsychoPy Python interpreter",
        )
        if selected:
            self.controller.configure_task_python(Path(selected))

    def _primary_is_safe(self) -> bool:
        if QApplication.activeModalWidget() is not None:
            return False
        focus = QApplication.focusWidget()
        editable_types = (
            QLineEdit,
            QTextEdit,
            QPlainTextEdit,
            QComboBox,
            QAbstractSpinBox,
            QAbstractItemView,
        )
        if isinstance(focus, editable_types):
            return False
        page = self.pages.get(self.controller.state.page)
        return bool(page and page.primary_button.isEnabled())

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._primary_is_safe():
            page = self.pages[self.controller.state.page]
            page.primary_button.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.controller.shutdown()
        super().closeEvent(event)
