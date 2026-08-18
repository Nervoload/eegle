"""Registered session inspection surface."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QListWidget, QSplitter, QVBoxLayout, QWidget

from demos.workbench.state import ProjectSnapshot, SessionSnapshot, WorkbenchState
from demos.workbench.views.base import WorkbenchPage
from demos.workbench.widgets import Card, InfoBanner, short_digest


class SessionsPage(WorkbenchPage):
    session_selected = Signal(str)
    replay_requested = Signal(str)

    def __init__(self, snapshot: ProjectSnapshot) -> None:
        super().__init__(
            "Sessions",
            "Durable session evidence registered by the active EEGle project—not transient GUI history.",
        )
        self.content.addWidget(
            InfoBanner(
                "Authoritative inspection",
                "Each row comes from the project manifest and EEGle's read-only inspection service. Task sidecars are reconciled by exact session and task-plan hashes only.",
            )
        )
        splitter = QSplitter()
        list_card = Card()
        list_card.layout.addWidget(QLabel("Project sessions"))
        self.session_list = QListWidget()
        self.session_list.currentItemChanged.connect(self._selection_changed)
        list_card.layout.addWidget(self.session_list, 1)
        detail_card = Card()
        self.detail_root = QWidget()
        self.detail_layout = QVBoxLayout(self.detail_root)
        self.detail_layout.setContentsMargins(0, 0, 0, 0)
        self.detail_title = QLabel("No recorded sessions")
        self.detail_title.setProperty("role", "sectionTitle")
        self.detail = QLabel(
            "Compile, preflight, and run a real or simulated session before integrity, sources, timing, evidence, and replay details become available."
        )
        self.detail.setProperty("role", "muted")
        self.detail.setWordWrap(True)
        self.detail_layout.addWidget(self.detail_title)
        self.detail_layout.addWidget(self.detail)
        self.detail_layout.addStretch(1)
        detail_card.layout.addWidget(self.detail_root, 1)
        splitter.addWidget(list_card)
        splitter.addWidget(detail_card)
        splitter.setSizes((320, 700))
        splitter.setMinimumHeight(430)
        self.content.addWidget(splitter)
        self._state: WorkbenchState | None = None
        self.primary_requested.connect(self._replay)
        self.configure_primary("Replay Session", enabled=False, hint="No replay-ready session is selected.")
        self.finish()

    def render_state(self, state: WorkbenchState) -> None:
        self._state = state
        current_ids = tuple(
            self.session_list.item(index).data(256)
            for index in range(self.session_list.count())
        )
        new_ids = tuple(value.session_id for value in state.sessions)
        if current_ids != new_ids:
            self.session_list.blockSignals(True)
            self.session_list.clear()
            for session in state.sessions:
                suffix = "SIM" if ".simulation." in session.session_id else "LIVE"
                label = f"{session.session_id}\n{suffix} · {session.session_status} · {'valid' if session.valid else 'issues'}"
                self.session_list.addItem(label)
                self.session_list.item(self.session_list.count() - 1).setData(256, session.session_id)
            self.session_list.blockSignals(False)
        selected_id = state.selected_session_id
        for index in range(self.session_list.count()):
            if self.session_list.item(index).data(256) == selected_id:
                self.session_list.setCurrentRow(index)
                break
        session = self._selected(state)
        self._render_detail(session)
        self.configure_primary(
            "Replay Session",
            enabled=bool(session and session.replay_ready and not state.busy),
            hint=(
                "Run authoritative replay against this immutable bundle."
                if session and session.replay_ready
                else "Select a session with a replay-ready published bundle."
            ),
        )

    def _render_detail(self, session: SessionSnapshot | None) -> None:
        if session is None:
            self.detail_title.setText("No recorded sessions")
            self.detail.setText(
                "A complete or cancelled run will be registered and inspected here."
            )
            return
        streams = tuple(session.source_health.get("streams", ()))
        stream_lines: list[str] = []
        for stream in streams:
            counts = dict(stream.get("event_kind_counts") or {})
            marker_text = ", ".join(f"{key}: {value}" for key, value in sorted(counts.items()))
            stream_lines.append(
                f"• {stream.get('source_node_id', stream.get('stream_id', 'source'))}: "
                f"{stream.get('record_count', stream.get('observation_count', 0))} records"
                + (f" · markers [{marker_text}]" if marker_text else "")
            )
        task = "No behavioral sidecar"
        if session.task_summary is not None:
            task = (
                f"Behavioral sidecar: {session.task_summary.get('completed_trials', 0)} trials · "
                f"{'reconciled' if session.task_reconciled else 'marker mismatch'}"
            )
        issue_text = "none" if not session.issues else "; ".join(
            str(value.get("code", "issue")) for value in session.issues
        )
        self.detail_title.setText(session.session_id)
        self.detail.setText(
            "\n".join(
                (
                    f"Status: {session.session_status} / {session.outcome}",
                    f"Integrity: {'valid' if session.valid else 'issues detected'}",
                    f"Engine: {session.engine_status or '—'} · terminal reason: {session.terminal_reason or '—'}",
                    f"Plan: {short_digest(session.plan_hash)}",
                    f"Bundle: {short_digest(session.bundle_id)}",
                    f"Evidence records: {session.evidence_record_count}",
                    f"Phases: {len(session.phase_timeline)} timeline entries",
                    *stream_lines,
                    task,
                    f"Inspection issues: {issue_text}",
                )
            )
        )

    def _selected(self, state: WorkbenchState) -> SessionSnapshot | None:
        return next(
            (value for value in state.sessions if value.session_id == state.selected_session_id),
            None,
        )

    def _selection_changed(self, current, _previous) -> None:
        if current is not None:
            self.session_selected.emit(str(current.data(256)))

    def _replay(self) -> None:
        if self._state is not None and self._state.selected_session_id:
            self.replay_requested.emit(self._state.selected_session_id)
