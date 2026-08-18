"""Live compilation and preflight projection."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QTableWidget, QTableWidgetItem

from demos.workbench.state import (
    EnvironmentMode,
    Lifecycle,
    ProjectSnapshot,
    WorkbenchState,
)
from demos.workbench.views.base import WorkbenchPage
from demos.workbench.widgets import Card, HashLabel, InfoBanner, short_digest


class BuildPage(WorkbenchPage):
    compile_requested = Signal()
    continue_requested = Signal()

    def __init__(self, snapshot: ProjectSnapshot) -> None:
        super().__init__(
            "Build Settings",
            "Turn the saved design and an accepted deployment into one immutable, checked execution.",
        )
        self.build_banner = InfoBanner(
                "Build follows explicit deployment acceptance",
                "Compile uses the reviewed deployment_proposal artifact. Preflight then checks that exact lock against the current LSL detection report.",
                tone="blue",
            )
        self.content.addWidget(self.build_banner)
        config = Card()
        config.layout.addWidget(QLabel("Build configuration"))
        config.layout.addWidget(HashLabel("Design revision", snapshot.design_digest))
        config.layout.addWidget(HashLabel("Portable protocol", snapshot.protocol_hash))
        config.layout.addWidget(HashLabel("Portable suite", snapshot.suite_hash))
        self.deployment_hash = QLabel("Live deployment · Not accepted")
        self.deployment_hash.setProperty("role", "muted")
        config.layout.addWidget(self.deployment_hash)
        self.content.addWidget(config)

        compile_card = Card()
        compile_card.layout.addWidget(QLabel("Compile"))
        self.compile_lines: list[QLabel] = []
        for title in (
            "Portable design resolved",
            "Reviewed deployment selected",
            "Plugins and contracts validated",
            "ExecutionPlan and ExecutionLock",
        ):
            line = QLabel(f"○  {title}")
            line.setProperty("role", "muted")
            line.setWordWrap(True)
            compile_card.layout.addWidget(line)
            self.compile_lines.append(line)
        self.plan_hash = QLabel("Plan · Not compiled")
        self.lock_hash = QLabel("Lock · Not compiled")
        self.plan_hash.setProperty("role", "muted")
        self.lock_hash.setProperty("role", "muted")
        compile_card.layout.addWidget(self.plan_hash)
        compile_card.layout.addWidget(self.lock_hash)
        self.content.addWidget(compile_card)

        preflight_title = QLabel("PREFLIGHT")
        preflight_title.setProperty("role", "eyebrow")
        self.content.addWidget(preflight_title)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("Check", "Status", "Detail"))
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            0, self.table.horizontalHeader().ResizeMode.Stretch
        )
        self.table.horizontalHeader().setSectionResizeMode(
            2, self.table.horizontalHeader().ResizeMode.Stretch
        )
        self.table.setMinimumHeight(300)
        self.content.addWidget(self.table)
        self.primary_requested.connect(self._primary)
        self.configure_primary(
            "Compile & Preflight",
            enabled=False,
            hint="Accept a deployment proposal on Apparatus before compiling.",
        )
        self.finish()
        self._state: WorkbenchState | None = None

    def render_state(self, state: WorkbenchState) -> None:
        self._state = state
        simulation = state.environment_mode == EnvironmentMode.SIMULATION
        accepted = simulation or state.apparatus.proposal_hash is not None
        compiled = state.build.plan_hash is not None and state.build.lock_hash is not None
        self.build_banner.title.setText(
            "Prepared deterministic simulation"
            if simulation
            else "Build follows explicit deployment acceptance"
        )
        self.build_banner.message.setText(
            "Compile selects simulation_deployment and runs the standard EEGle preflight. It does not validate Neuracle, PsychoPy timing, or marker synchronization."
            if simulation
            else "Compile uses the reviewed deployment_proposal artifact. Preflight then checks that exact lock against the current LSL detection report."
        )
        self.deployment_hash.setText(
            "Simulation deployment · prepared project artifact"
            if simulation
            else "Live deployment · " + short_digest(state.apparatus.deployment_hash)
        )
        states = (True, accepted, compiled, compiled)
        for line, complete in zip(self.compile_lines, states):
            text = line.text().lstrip("○✓ ")
            line.setText(f"{'✓' if complete else '○'}  {text}")
        self.plan_hash.setText("Plan · " + short_digest(state.build.plan_hash))
        self.lock_hash.setText("Lock · " + short_digest(state.build.lock_hash))
        if state.build.checks:
            self.table.setRowCount(len(state.build.checks))
            for row, check in enumerate(state.build.checks):
                values = (check.capability, check.status.title(), check.summary)
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.table.setItem(row, column, item)
        else:
            pending = (
                "Execution lock integrity",
                "EEG and marker source identity",
                "Clock mapping",
                "Plugins and evidence storage",
            )
            self.table.setRowCount(len(pending))
            for row, name in enumerate(pending):
                detail = (
                    "Requires a compiled simulation plan"
                    if simulation
                    else "Requires a compiled live plan"
                )
                for column, value in enumerate((name, "Pending", detail)):
                    item = QTableWidgetItem(value)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.table.setItem(row, column, item)
        ready = state.build.preflight_ready
        self.configure_primary(
            "Continue to Run  →" if ready else (state.busy_action or "Compile & Preflight"),
            enabled=(ready or accepted) and not state.busy,
            hint=(
                "Preflight is ready; continue to the operator surface."
                if ready
                else (
                    "Compile the prepared deterministic simulation deployment."
                    if simulation
                    else "Accept a deployment proposal on Apparatus before compiling."
                )
            ),
        )

    def _primary(self) -> None:
        if self._state is None:
            return
        if self._state.lifecycle == Lifecycle.PREFLIGHT_READY:
            self.continue_requested.emit()
        else:
            self.compile_requested.emit()
