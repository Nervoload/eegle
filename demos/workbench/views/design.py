"""Read-only projection of the prepared ExperimentDesign."""

from __future__ import annotations

import json
from collections.abc import Mapping

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from demos.workbench.profile import Study1Profile
from demos.workbench.state import ProjectSnapshot
from demos.workbench.views.base import WorkbenchPage
from demos.workbench.views.helpers import key_value_rows
from demos.workbench.widgets import (
    Card,
    DisclosureCard,
    EmptyState,
    HashLabel,
    Metric,
)


class DesignPage(WorkbenchPage):
    def __init__(self, snapshot: ProjectSnapshot, profile: Study1Profile) -> None:
        super().__init__(
            "Experiment Design",
            "Portable scientific intent, before selecting a local stream, device, path, or permission.",
        )
        selected = profile.variant(snapshot.profile_variant)
        top = Card()
        header = QHBoxLayout()
        identity = QVBoxLayout()
        title = QLabel(snapshot.display_name)
        title.setProperty("role", "sectionTitle")
        statement = QLabel(snapshot.statement)
        statement.setProperty("role", "muted")
        statement.setWordWrap(True)
        identity.addWidget(title)
        identity.addWidget(statement)
        header.addLayout(identity, 1)
        top.layout.addLayout(header)
        hashes = QHBoxLayout()
        hashes.addWidget(Metric(str(snapshot.design_revision), "Design revision"))
        hashes.addSpacing(26)
        hashes.addWidget(Metric("65", "Logical values"))
        hashes.addSpacing(26)
        hashes.addWidget(Metric("600", "Full-protocol trials"))
        hashes.addStretch(1)
        top.layout.addLayout(hashes)
        top.layout.addWidget(HashLabel("Design", snapshot.design_digest))
        top.layout.addWidget(HashLabel("Protocol", snapshot.protocol_hash))
        self.content.addWidget(top)
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self._overview(profile, selected), "Overview")
        tabs.addTab(self._structure(selected), "Structure")
        tabs.addTab(
            EmptyState(
                "Compiled graph not available",
                "The graph is an ExecutionPlan projection. Accept an apparatus deployment and compile before showing it.",
                minimum_height=360,
            ),
            "Graph",
        )
        source = QPlainTextEdit()
        source.setReadOnly(True)
        source.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        source.setPlainText(json.dumps(snapshot.source_payload, indent=2, ensure_ascii=False))
        source.setMinimumHeight(420)
        tabs.addTab(source, "Source JSON")
        self.content.addWidget(tabs)
        self.configure_primary(
            "Continue to Apparatus  →",
            enabled=True,
            hint="The design is read-only in this UI foundation; continue to local binding.",
        )
        self.finish()

    def _overview(self, profile: Study1Profile, selected: Mapping[str, object]) -> QWidget:
        task = profile.payload["task"]
        signal = profile.payload["signal"]
        processing = profile.payload["processing"]
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(14, 16, 14, 16)
        layout.setSpacing(11)
        blocks = selected["blocks"]
        total = sum(int(block["trials"]) for block in blocks)
        cards = (
            DisclosureCard(
                "Protocol structure",
                "Visit 1 baseline, practice, one support block, and two query blocks.",
                key_value_rows(
                    (
                        ("Rehearsal baseline", "15 s eyes open + 15 s eyes closed"),
                        ("Practice", "10 trials · one rehearsal round"),
                        ("Main task", f"3 blocks · {total} trials"),
                    )
                ),
                expanded=True,
            ),
            DisclosureCard(
                "Task parameters",
                "Digits 0–9, participant-specific No-Go allocation, and fixed task timing.",
                key_value_rows(
                    (
                        ("Stimulus", f"{float(task['stimulus_seconds']):.2f} s"),
                        ("Onset interval", f"{float(task['stimulus_onset_interval_seconds']):.1f} s fixed"),
                        ("Response", "Space · minimum valid RT 0.10 s"),
                        ("No-Go schedule", "15% · 3 planned per rehearsal block"),
                    )
                ),
            ),
            DisclosureCard(
                "Signals and events",
                "One logical EEG source and one sparse marker stream.",
                key_value_rows(
                    (
                        ("EEG", f"65 values · {int(signal['nominal_rate_hz'])} Hz · {signal['unit']}"),
                        ("Physiological path", "64 neutral positions"),
                        ("Reserved value", "TRIGGER_STATUS · excluded from physiology"),
                        ("Markers", "EEGleMarkers · exact source pending detection"),
                    )
                ),
            ),
            DisclosureCard(
                "Processing and windows",
                "Source-preserving recording with one causal prestimulus window.",
                key_value_rows(
                    (
                        ("Processing", "Identity / source-preserving"),
                        (
                            "Causal window",
                            f"{float(processing['window_start_seconds']):.2f} to {float(processing['window_end_seconds']):.2f} s",
                        ),
                        ("Event", str(processing["event_kind"])),
                    )
                ),
            ),
            DisclosureCard(
                "Models and policy",
                "No active model or action is required for this first live slice.",
                key_value_rows(
                    (
                        ("Observer model", "Not selected"),
                        ("Policy", "Observe-only"),
                        ("Task adaptation", "Disabled"),
                        ("Stimulation", "Disabled"),
                    )
                ),
            ),
            DisclosureCard(
                "Recording and acceptance",
                "Record EEG, markers, execution capture, and semantic evidence.",
                key_value_rows(
                    (
                        ("Execution capture", "Required"),
                        ("Semantic evidence", "Required"),
                        ("Archival raw", "External reference"),
                        ("Apparatus acceptance", "Pending exact site overlay"),
                    )
                ),
            ),
        )
        for card in cards:
            layout.addWidget(card)
        layout.addStretch(1)
        return root

    def _structure(self, selected: Mapping[str, object]) -> QWidget:
        selected = dict(selected)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)
        baseline = selected["baseline"]
        stages = [
            ("01", "Eyes open baseline", f"{int(baseline['eyes_open_seconds'])} seconds"),
            ("02", "Eyes closed baseline", f"{int(baseline['eyes_closed_seconds'])} seconds"),
            ("03", "Practice", f"{selected['practice']['trials_per_round']} trials"),
        ]
        for index, block in enumerate(selected["blocks"], start=4):
            stages.append(
                (
                    f"{index:02d}",
                    f"{str(block['phase']).title()} block",
                    f"{block['trials']} trials · {block['planned_no_go_count']} No-Go",
                )
            )
        for number, title, detail in stages:
            card = Card(padding=14)
            row = QHBoxLayout()
            stage_number = QLabel(number)
            stage_number.setProperty("role", "stageNumber")
            stage_number.setFixedWidth(30)
            row.addWidget(stage_number)
            text = QVBoxLayout()
            text.addWidget(QLabel(title))
            subtitle = QLabel(detail)
            subtitle.setProperty("role", "muted")
            text.addWidget(subtitle)
            row.addLayout(text, 1)
            card.layout.addLayout(row)
            layout.addWidget(card)
        note = QLabel(
            "EEGle currently executes this external task as one valid observation phase; these task stages remain task-owned presentation structure."
        )
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)
        return root
