"""Recent and prepared experiments page."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton

from demos.workbench.state import ProjectSnapshot, WorkbenchState
from demos.workbench.views.base import WorkbenchPage
from demos.workbench.widgets import Card, EmptyState, InfoBanner


class ExperimentsPage(WorkbenchPage):
    open_project_requested = Signal()

    def __init__(self, snapshot: ProjectSnapshot | None) -> None:
        super().__init__(
            "Experiments",
            "Open prepared projects and keep related experiment revisions together without inventing a registry.",
        )
        if snapshot is None:
            self.content.addWidget(
                EmptyState(
                    "No project is available",
                    "Workbench could not open the prepared Study 1 project. Review the startup issue before continuing.",
                )
            )
            self.configure_primary("Open project", enabled=False, hint="No readable project is available.")
            self.finish()
            return

        self.content.addWidget(
            InfoBanner(
                "Local projects stay local",
                "Recents store only project paths and UI preferences. Scientific authority remains in each EEGle project.",
            )
        )
        group = QLabel("STUDY 1 — DSART")
        group.setProperty("role", "eyebrow")
        self.content.addWidget(group)
        card = Card()
        top = QHBoxLayout()
        title = QLabel(snapshot.display_name)
        title.setProperty("role", "sectionTitle")
        top.addWidget(title)
        top.addStretch(1)
        card.layout.addLayout(top)
        statement = QLabel(snapshot.statement)
        statement.setProperty("role", "muted")
        statement.setWordWrap(True)
        card.layout.addWidget(statement)
        path = QLabel(str(snapshot.project_root))
        path.setProperty("role", "subtle")
        path.setWordWrap(True)
        card.layout.addWidget(path)
        metadata = QHBoxLayout()
        self.design_status = QLabel("Design ready")
        metadata.addWidget(self.design_status)
        metadata.addWidget(QLabel("•"))
        self.lifecycle_status = QLabel("Apparatus unbound")
        metadata.addWidget(self.lifecycle_status)
        metadata.addWidget(QLabel("•"))
        self.session_status = QLabel(
            "No sessions"
            if not snapshot.has_sessions
            else f"{len(snapshot.session_uris)} session(s)"
        )
        metadata.addWidget(self.session_status)
        metadata.addStretch(1)
        open_button = QPushButton("Open")
        open_button.clicked.connect(self.open_project_requested)
        metadata.addWidget(open_button)
        card.layout.addLayout(metadata)
        self.content.addWidget(card)

        references = QLabel("REFERENCE EXPERIMENTS")
        references.setProperty("role", "eyebrow")
        self.content.addWidget(references)
        self.content.addWidget(
            EmptyState(
                "No additional recents",
                "Event-locked observer and continuous-recording presets will appear here after their project-creation slice.",
                minimum_height=150,
            )
        )
        self.configure_primary(
            "Open Study 1",
            enabled=True,
            hint="Open the prepared project on Experiment Design.",
        )
        self.primary_requested.connect(self.open_project_requested)
        self.finish()

    def render_state(self, state: WorkbenchState) -> None:
        if not hasattr(self, "session_status"):
            return
        self.lifecycle_status.setText(
            state.lifecycle.value.replace("_", " ").title()
        )
        self.session_status.setText(
            "No sessions"
            if not state.sessions
            else f"{len(state.sessions)} session(s)"
        )
