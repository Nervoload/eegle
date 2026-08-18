"""Functional LSL discovery and explicit Study 1 apparatus review."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from demos.workbench.profile import Study1Profile
from demos.workbench.state import (
    EnvironmentMode,
    ProjectSnapshot,
    TaskStatus,
    WorkbenchState,
)
from demos.workbench.views.base import WorkbenchPage
from demos.workbench.widgets import Card, EmptyState, InfoBanner


class ApparatusPage(WorkbenchPage):
    scan_requested = Signal()
    arm_task_requested = Signal(str)
    review_requested = Signal(str, str)
    environment_requested = Signal(str)
    continue_requested = Signal()
    task_python_requested = Signal()

    def __init__(self, snapshot: ProjectSnapshot, profile: Study1Profile) -> None:
        super().__init__(
            "Apparatus",
            "Bind portable experiment requirements to the laboratory resources that actually exist now.",
        )
        self.snapshot = snapshot
        self.profile = profile
        self._state: WorkbenchState | None = None
        modes = Card()
        row = QHBoxLayout()
        title = QVBoxLayout()
        label = QLabel("Execution environment")
        label.setProperty("role", "sectionTitle")
        self.environment_hint = QLabel("Live LSL selected · waiting for current detection evidence.")
        self.environment_hint.setProperty("role", "muted")
        title.addWidget(label)
        title.addWidget(self.environment_hint)
        row.addLayout(title, 1)
        self.environment = QComboBox()
        self.environment.addItem("Live LSL", EnvironmentMode.LIVE_LSL.value)
        self.environment.addItem("Simulation", EnvironmentMode.SIMULATION.value)
        self.environment.addItem("Bench test · unavailable", EnvironmentMode.BENCH_TEST.value)
        model_item = self.environment.model().item(2)
        if model_item is not None:
            model_item.setEnabled(False)
        self.environment.activated.connect(
            lambda index: self.environment_requested.emit(
                str(self.environment.itemData(index))
            )
        )
        row.addWidget(self.environment)
        modes.layout.addLayout(row)
        self.content.addWidget(modes)

        self.live_banner = InfoBanner(
                "Arm the external DSART task first",
                "Opening PsychoPy creates the task-owned marker stream before final detection. The Qt renderer remains a presentation-only fallback and cannot satisfy recorded Start.",
                tone="blue",
            )
        self.content.addWidget(self.live_banner)

        task = Card()
        task_row = QHBoxLayout()
        task_text = QVBoxLayout()
        task_title = QLabel("Study 1 task environment")
        task_title.setProperty("role", "sectionTitle")
        self.task_status = QLabel("Closed · no marker outlet")
        self.task_status.setProperty("role", "muted")
        self.task_status.setWordWrap(True)
        task_text.addWidget(task_title)
        task_text.addWidget(self.task_status)
        task_row.addLayout(task_text, 1)
        self.participant = QLineEdit()
        self.participant.setPlaceholderText("Participant pseudonym")
        self.participant.setText("DEMO-001")
        self.participant.setToolTip("Memory-only; this preference is not persisted.")
        task_row.addWidget(self.participant)
        self.python_button = QPushButton("Task Python…")
        self.python_button.clicked.connect(self.task_python_requested)
        task_row.addWidget(self.python_button)
        self.arm_button = QPushButton("Open DSART Task")
        self.arm_button.clicked.connect(
            lambda: self.arm_task_requested.emit(self.participant.text().strip())
        )
        task_row.addWidget(self.arm_button)
        task.layout.addLayout(task_row)
        self.task_card = task
        self.content.addWidget(task)

        streams = Card()
        stream_header = QHBoxLayout()
        heading = QLabel("Detected LSL streams")
        heading.setProperty("role", "sectionTitle")
        stream_header.addWidget(heading)
        stream_header.addStretch(1)
        self.scan_button = QPushButton("Scan for streams")
        self.scan_button.clicked.connect(self.scan_requested)
        stream_header.addWidget(self.scan_button)
        streams.layout.addLayout(stream_header)
        self.stream_table = QTableWidget(0, 5)
        self.stream_table.setHorizontalHeaderLabels(
            ("Stream", "Kind", "Values", "Rate", "Exact identity")
        )
        self.stream_table.verticalHeader().setVisible(False)
        self.stream_table.horizontalHeader().setSectionResizeMode(
            0, self.stream_table.horizontalHeader().ResizeMode.Stretch
        )
        self.stream_table.horizontalHeader().setSectionResizeMode(
            4, self.stream_table.horizontalHeader().ResizeMode.Stretch
        )
        self.stream_table.setMinimumHeight(190)
        self.stream_empty = EmptyState(
            "No scan has been run",
            "Start Neuracle acquisition, open the task, then scan. Only current LSL observations appear here.",
            minimum_height=190,
        )
        streams.layout.addWidget(self.stream_empty)
        streams.layout.addWidget(self.stream_table)
        self.stream_table.hide()
        self.streams_card = streams
        self.content.addWidget(streams)

        binding_title = QLabel("LOGICAL-TO-PHYSICAL BINDING")
        binding_title.setProperty("role", "eyebrow")
        self.content.addWidget(binding_title)
        self.binding_table = QTableWidget(4, 3)
        self.binding_table.setHorizontalHeaderLabels(
            ("Experiment requirement", "Selected capability", "Compatibility")
        )
        requirements = (
            "signal.eeg · 65 values · 1000 Hz",
            "event.markers · DSART event kinds",
            "device.clock → boundary.clock",
            "evidence storage",
        )
        self.eeg_combo = QComboBox()
        self.marker_combo = QComboBox()
        self.eeg_combo.currentIndexChanged.connect(self._sync_primary)
        self.marker_combo.currentIndexChanged.connect(self._sync_primary)
        for row_index, requirement in enumerate(requirements):
            item = QTableWidgetItem(requirement)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.binding_table.setItem(row_index, 0, item)
        self.binding_table.setCellWidget(0, 1, self.eeg_combo)
        self.binding_table.setCellWidget(1, 1, self.marker_combo)
        self._set_readonly_item(0, 2, "Pending scan")
        self._set_readonly_item(1, 2, "Pending task + scan")
        self._set_readonly_item(2, 1, "Unbound")
        self._set_readonly_item(2, 2, "Pending LSL mapping")
        self._set_readonly_item(3, 1, str(snapshot.project_root / "sessions"))
        self._set_readonly_item(3, 2, "Local project path")
        self.binding_table.horizontalHeader().setSectionResizeMode(
            0, self.binding_table.horizontalHeader().ResizeMode.Stretch
        )
        self.binding_table.horizontalHeader().setSectionResizeMode(
            1, self.binding_table.horizontalHeader().ResizeMode.Stretch
        )
        self.binding_table.verticalHeader().setVisible(False)
        self.binding_table.setMinimumHeight(205)
        self.content.addWidget(self.binding_table)

        self.review_banner = InfoBanner(
                "Site review is required before compilation",
                "The review sheet preserves detected physical labels while explicitly mapping their positions to PHYSIO_01–PHYSIO_64 and TRIGGER_STATUS. Reference, ground, auxiliary allocation, unit, and selector must be confirmed.",
                tone="warning",
            )
        self.content.addWidget(self.review_banner)
        self.primary_requested.connect(self._emit_review)
        self.configure_primary(
            "Review Deployment",
            enabled=False,
            hint="Detect one compatible Neuracle stream and the armed EEGleMarkers stream first.",
        )
        self.finish()

    def _set_readonly_item(self, row: int, column: int, text: str) -> None:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.binding_table.setItem(row, column, item)

    def render_state(self, state: WorkbenchState) -> None:
        self._state = state
        simulation = state.environment_mode == EnvironmentMode.SIMULATION
        index = self.environment.findData(state.environment_mode.value)
        self.environment.blockSignals(True)
        self.environment.setCurrentIndex(max(index, 0))
        self.environment.blockSignals(False)
        self.live_banner.setVisible(not simulation)
        self.task_card.setVisible(not simulation)
        self.streams_card.setVisible(not simulation)
        self.binding_table.setVisible(not simulation)
        self.review_banner.setVisible(not simulation)
        if simulation:
            self.environment_hint.setText(
                "Deterministic simulation selected · no hardware or task-timing claim"
            )
            self.configure_primary(
                "Continue to Build  →",
                enabled=not state.busy,
                hint="Compile the prepared simulation deployment.",
            )
            return
        apparatus = state.apparatus
        task = state.task
        self.scan_button.setEnabled(not state.busy)
        self.scan_button.setText(state.busy_action or "Scan for streams")
        self.arm_button.setEnabled(
            not state.busy
            and task.status
            in {
                TaskStatus.CLOSED,
                TaskStatus.COMPLETED,
                TaskStatus.ABORTED,
                TaskStatus.FAILED,
            }
        )
        self.arm_button.setText(
            "Task Open" if task.status in {TaskStatus.ARMED, TaskStatus.RUNNING} else "Open DSART Task"
        )
        self.python_button.setEnabled(self.arm_button.isEnabled())
        self.task_status.setText(
            f"{task.status.value.replace('_', ' ').title()} · {task.message}"
        )
        if apparatus.scan_complete:
            version = apparatus.library_version or "unknown"
            digest = apparatus.detection_report_hash or ""
            self.environment_hint.setText(
                f"Detection {digest[:19]}… · pylsl {version} · {len(apparatus.streams)} stream(s)"
            )
            self._render_streams(state)
        self._sync_primary()

    def _render_streams(self, state: WorkbenchState) -> None:
        streams = state.apparatus.streams
        self.stream_empty.setVisible(not streams)
        self.stream_table.setVisible(bool(streams))
        self.stream_table.setRowCount(len(streams))
        for row, stream in enumerate(streams):
            values = (
                stream.name,
                stream.stream_type,
                str(stream.channel_count),
                "irregular" if not stream.nominal_rate_hz else f"{stream.nominal_rate_hz:g} Hz",
                stream.exact_selector,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.stream_table.setItem(row, column, item)
        self.eeg_combo.blockSignals(True)
        self.marker_combo.blockSignals(True)
        self.eeg_combo.clear()
        self.marker_combo.clear()
        self.eeg_combo.addItem("Select detected EEG stream…", None)
        self.marker_combo.addItem("Select armed marker stream…", None)
        for stream in streams:
            if stream.content_kind == "dense_samples":
                self.eeg_combo.addItem(
                    f"{stream.name} · {stream.channel_count} @ {stream.nominal_rate_hz:g} Hz",
                    stream.capability_id,
                )
            if stream.content_kind == "sparse_events":
                self.marker_combo.addItem(stream.name, stream.capability_id)
        self._select_data(self.eeg_combo, state.apparatus.selected_eeg_capability_id)
        self._select_data(self.marker_combo, state.apparatus.selected_marker_capability_id)
        self.eeg_combo.blockSignals(False)
        self.marker_combo.blockSignals(False)
        eeg = self._selected_stream(state, self.eeg_combo.currentData())
        marker = self._selected_stream(state, self.marker_combo.currentData())
        self.binding_table.item(0, 2).setText(
            "65 values · 1000 Hz"
            if eeg and eeg.channel_count == 65 and eeg.nominal_rate_hz == 1000
            else "Incompatible or unselected"
        )
        self.binding_table.item(1, 2).setText(
            "Armed task identity"
            if marker and marker.name == self.profile.payload["markers"]["stream_name"]
            else "Incompatible or unselected"
        )
        self.binding_table.item(2, 1).setText("LSL online estimate")
        self.binding_table.item(2, 2).setText("Detected")

    @staticmethod
    def _select_data(combo: QComboBox, value: str | None) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(max(index, 0))

    @staticmethod
    def _selected_stream(state: WorkbenchState, capability_id: object):
        return next(
            (
                value
                for value in state.apparatus.streams
                if value.capability_id == capability_id
            ),
            None,
        )

    def _sync_primary(self) -> None:
        state = self._state
        if state is not None and state.environment_mode == EnvironmentMode.SIMULATION:
            self.configure_primary(
                "Continue to Build  →",
                enabled=not state.busy,
                hint="Compile the prepared simulation deployment.",
            )
            return
        eeg = None if state is None else self._selected_stream(state, self.eeg_combo.currentData())
        marker = (
            None
            if state is None
            else self._selected_stream(state, self.marker_combo.currentData())
        )
        enabled = bool(
            state is not None
            and not state.busy
            and eeg is not None
            and eeg.channel_count == 65
            and eeg.nominal_rate_hz == 1000.0
            and marker is not None
            and marker.name == self.profile.payload["markers"]["stream_name"]
        )
        self.configure_primary(
            "Review Deployment",
            enabled=enabled,
            hint=(
                "Review the exact 65-value overlay and accept the proposal."
                if enabled
                else "Detect one compatible Neuracle stream and the armed EEGleMarkers stream first."
            ),
        )

    def _emit_review(self) -> None:
        if self._state is not None and self._state.environment_mode == EnvironmentMode.SIMULATION:
            self.continue_requested.emit()
            return
        eeg = self.eeg_combo.currentData()
        marker = self.marker_combo.currentData()
        if eeg and marker:
            self.review_requested.emit(str(eeg), str(marker))
