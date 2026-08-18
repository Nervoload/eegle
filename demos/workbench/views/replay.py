"""Offline replay and equivalence surface."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel

from demos.workbench.state import ProjectSnapshot, WorkbenchState
from demos.workbench.views.base import WorkbenchPage
from demos.workbench.widgets import Card, InfoBanner


class ReplayPage(WorkbenchPage):
    session_selected = Signal(str)
    replay_requested = Signal(str)

    def __init__(self, snapshot: ProjectSnapshot | None) -> None:
        super().__init__(
            "Replay & Compare",
            "Inspect recorded evidence, run authoritative replay, and localize equivalence or first divergence.",
        )
        self.content.addWidget(
            InfoBanner(
                "Offline evidence area",
                "Replay observes an existing immutable bundle. It never mutates, finalizes, or repairs the source evidence.",
            )
        )
        selectors = Card()
        row = QHBoxLayout()
        row.addWidget(QLabel("Experiment"))
        experiment = QComboBox()
        experiment.addItem(snapshot.display_name if snapshot else "No project")
        experiment.setEnabled(False)
        row.addWidget(experiment, 1)
        row.addWidget(QLabel("Session"))
        self.session = QComboBox()
        self.session.currentIndexChanged.connect(self._selection_changed)
        row.addWidget(self.session, 1)
        selectors.layout.addLayout(row)
        self.content.addWidget(selectors)

        result = Card()
        result.layout.addWidget(QLabel("Replay result"))
        self.result_title = QLabel("Nothing replayed yet")
        self.result_title.setProperty("role", "sectionTitle")
        self.result_detail = QLabel(
            "Select a session with a published EEGle evidence bundle to evaluate integrity and equivalence."
        )
        self.result_detail.setProperty("role", "muted")
        self.result_detail.setWordWrap(True)
        result.layout.addWidget(self.result_title)
        result.layout.addWidget(self.result_detail)
        self.content.addWidget(result)
        self.content.addWidget(
            InfoBanner(
                "Model replacement unavailable",
                "A content-addressed replacement model package and compatible model observation phase are required. Study 1 remains observe-only in this demonstration.",
                tone="warning",
            )
        )
        self._state: WorkbenchState | None = None
        self.primary_requested.connect(self._replay)
        self.configure_primary("Run Replay", enabled=False, hint="Select a replay-ready session.")
        self.finish()

    def render_state(self, state: WorkbenchState) -> None:
        self._state = state
        ids = tuple(self.session.itemData(index) for index in range(self.session.count()))
        new_ids = tuple(value.session_id for value in state.sessions)
        if ids != new_ids:
            self.session.blockSignals(True)
            self.session.clear()
            for value in state.sessions:
                self.session.addItem(value.session_id, value.session_id)
            self.session.blockSignals(False)
        index = self.session.findData(state.selected_session_id)
        self.session.blockSignals(True)
        self.session.setCurrentIndex(index)
        self.session.blockSignals(False)
        selected = next(
            (value for value in state.sessions if value.session_id == state.selected_session_id),
            None,
        )
        replay = state.replay
        if replay.session_id is None:
            self.result_title.setText("Nothing replayed yet")
            self.result_detail.setText(
                "Replay will report the requested and evaluated level, compared-record count, issues, and first divergence."
            )
        else:
            verdict = "Equivalent" if replay.equivalent else "Divergence or incomplete"
            self.result_title.setText(f"{verdict} · {replay.status}")
            issue_text = "none" if not replay.issues else "; ".join(
                str(value.get("code", "issue")) for value in replay.issues
            )
            divergence = "none" if replay.first_divergence is None else str(dict(replay.first_divergence))
            self.result_detail.setText(
                "\n".join(
                    (
                        f"Session: {replay.session_id}",
                        f"Result: {replay.result_status or '—'}",
                        f"Level: {replay.requested_level or '—'} → {replay.evaluated_level or '—'}",
                        f"Compared evidence records: {replay.compared_record_count}",
                        f"Issues: {issue_text}",
                        f"First divergence: {divergence}",
                    )
                )
            )
        self.configure_primary(
            "Run Replay",
            enabled=bool(selected and selected.replay_ready and not state.busy),
            hint=(
                "Evaluate the selected immutable bundle."
                if selected and selected.replay_ready
                else "Select a replay-ready session."
            ),
        )

    def _selection_changed(self, index: int) -> None:
        value = self.session.itemData(index)
        if value:
            self.session_selected.emit(str(value))

    def _replay(self) -> None:
        value = self.session.currentData()
        if value:
            self.replay_requested.emit(str(value))
