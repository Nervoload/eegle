"""Compact non-destructive Workbench dialogs."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from demos.workbench.operations import SiteOverlay
from demos.workbench.state import DetectedStreamSnapshot
from demos.workbench.widgets import Card, InfoBanner


class NewExperimentDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Experiment — EEGle Workbench")
        self.setModal(True)
        self.resize(720, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(14)
        title = QLabel("New Experiment")
        title.setProperty("role", "pageTitle")
        subtitle = QLabel("Start from a bounded scientific preset rather than a blank programming canvas.")
        subtitle.setProperty("role", "pageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        presets = QButtonGroup(self)
        choices = (
            ("Study 1 — DSART + Neuracle 64", "Prepared, primary Workbench demonstration"),
            ("Event-locked observer", "Simple simulation/reference project"),
            ("Continuous recording", "Quick hardware diagnostic project"),
            ("Open existing EEGle project", "Select a local project directory"),
        )
        for index, (name, detail) in enumerate(choices):
            card = Card(padding=12)
            row = QHBoxLayout()
            radio = QRadioButton()
            radio.setChecked(index == 0)
            presets.addButton(radio, index)
            text = QVBoxLayout()
            text.addWidget(QLabel(name))
            helper = QLabel(detail)
            helper.setProperty("role", "muted")
            text.addWidget(helper)
            row.addWidget(radio)
            row.addLayout(text, 1)
            card.layout.addLayout(row)
            layout.addWidget(card)

        fields = Card()
        fields.layout.addWidget(QLabel("Experiment name"))
        name = QLineEdit("Study 1 — DSART")
        fields.layout.addWidget(name)
        fields.layout.addWidget(QLabel("Description (optional)"))
        description = QTextEdit()
        description.setPlaceholderText("What scientific question or observation does this project capture?")
        description.setFixedHeight(62)
        fields.layout.addWidget(description)
        fields.layout.addWidget(QLabel("Run profile"))
        profile = QComboBox()
        profile.addItems(("Demo rehearsal — demonstration only", "Full protocol — Study 1 visit"))
        fields.layout.addWidget(profile)
        layout.addWidget(fields)
        layout.addWidget(
            InfoBanner(
                "Creation is intentionally unavailable in Slice A0",
                "This surface establishes the UI contract. The prepared project already open in Workbench was created through the real EEGle project service.",
                tone="warning",
            )
        )
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        create = QPushButton("Create Experiment")
        create.setProperty("primary", True)
        create.setEnabled(False)
        create.setToolTip("Project creation for additional presets arrives in the next Slice A batch.")
        buttons.addWidget(cancel)
        buttons.addWidget(create)
        layout.addLayout(buttons)


class SiteOverlayDialog(QDialog):
    """Explicit review of the detected positional Neuracle contract."""

    def __init__(
        self,
        stream: DetectedStreamSnapshot,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.stream = stream
        self.setWindowTitle("Review Neuracle Site Overlay")
        self.setModal(True)
        self.resize(760, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(13)
        title = QLabel("Review exact Neuracle mapping")
        title.setProperty("role", "pageTitle")
        layout.addWidget(title)
        layout.addWidget(
            InfoBanner(
                "Operator review becomes deployment evidence",
                "Workbench maps the 65 detected values by position to PHYSIO_01–PHYSIO_64 plus TRIGGER_STATUS. The physical labels remain preserved; this does not rename them as electrodes.",
                tone="warning",
            )
        )
        identity = QLabel(f"{stream.name}  ·  {stream.exact_selector}")
        identity.setWordWrap(True)
        identity.setProperty("role", "muted")
        layout.addWidget(identity)
        channels = QPlainTextEdit()
        channels.setReadOnly(True)
        channels.setFixedHeight(210)
        channels.setPlainText(
            "\n".join(
                f"{index:02d}  {label}  [{unit}]"
                for index, (label, unit) in enumerate(
                    zip(stream.channel_labels, stream.channel_units),
                    start=1,
                )
            )
        )
        layout.addWidget(channels)
        fields = Card()
        fields.layout.addWidget(QLabel("Confirmed value unit"))
        self.unit = QComboBox()
        observed_units = tuple(dict.fromkeys(stream.channel_units))
        self.unit.addItems(tuple(value for value in observed_units if value != "unknown") or ("uV",))
        if self.unit.findText("uV") < 0:
            self.unit.addItem("uV")
        self.unit.setCurrentText("uV")
        fields.layout.addWidget(self.unit)
        self.reference = QLineEdit()
        self.reference.setPlaceholderText("Exact laboratory reference (required)")
        self.ground = QLineEdit()
        self.ground.setPlaceholderText("Exact laboratory ground (required)")
        self.auxiliary = QLineEdit()
        self.auxiliary.setPlaceholderText("Auxiliary / EOG allocation, or 'none' (required)")
        fields.layout.addWidget(QLabel("Reference"))
        fields.layout.addWidget(self.reference)
        fields.layout.addWidget(QLabel("Ground"))
        fields.layout.addWidget(self.ground)
        fields.layout.addWidget(QLabel("Auxiliary allocation"))
        fields.layout.addWidget(self.auxiliary)
        layout.addWidget(fields)
        self.confirm = QCheckBox(
            "I reviewed all 65 positions and confirm value 65 is TRIGGER_STATUS."
        )
        layout.addWidget(self.confirm)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.accept_button = QPushButton("Accept Proposal & Continue")
        self.accept_button.setProperty("primary", True)
        self.accept_button.setEnabled(False)
        self.accept_button.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(self.accept_button)
        layout.addLayout(buttons)
        for widget in (self.reference, self.ground, self.auxiliary):
            widget.textChanged.connect(self._sync)
        self.confirm.toggled.connect(self._sync)

    def _sync(self) -> None:
        complete = all(
            widget.text().strip()
            for widget in (self.reference, self.ground, self.auxiliary)
        )
        self.accept_button.setEnabled(
            complete
            and self.confirm.isChecked()
            and len(self.stream.channel_labels) == 65
        )

    def overlay(self) -> SiteOverlay:
        return SiteOverlay(
            channel_order=self.stream.channel_labels,
            unit=self.unit.currentText(),
            reference=self.reference.text(),
            ground=self.ground.text(),
            auxiliary_allocation=self.auxiliary.text(),
        )
