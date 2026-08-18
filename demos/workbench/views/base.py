"""Common scrollable page shell with a persistent action area."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


class WorkbenchPage(QWidget):
    primary_requested = Signal()

    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.content = QVBoxLayout(self.body)
        self.content.setContentsMargins(36, 30, 36, 30)
        self.content.setSpacing(16)
        heading = QLabel(title)
        heading.setProperty("role", "pageTitle")
        description = QLabel(subtitle)
        description.setProperty("role", "pageSubtitle")
        description.setWordWrap(True)
        self.content.addWidget(heading)
        self.content.addWidget(description)
        self.content.addSpacing(2)
        scroll.setWidget(self.body)
        outer.addWidget(scroll, 1)

        self.action_bar = QFrame()
        self.action_bar.setObjectName("ActionBar")
        action_layout = QHBoxLayout(self.action_bar)
        action_layout.setContentsMargins(28, 13, 28, 13)
        self.action_hint = QLabel()
        self.action_hint.setProperty("role", "muted")
        self.action_hint.setWordWrap(True)
        self.primary_button = QPushButton()
        self.primary_button.setProperty("primary", True)
        self.primary_button.clicked.connect(self.primary_requested)
        action_layout.addWidget(self.action_hint, 1)
        action_layout.addWidget(self.primary_button)
        outer.addWidget(self.action_bar)

    def configure_primary(self, text: str, *, enabled: bool, hint: str) -> None:
        self.primary_button.setText(text)
        self.primary_button.setEnabled(enabled)
        self.primary_button.setToolTip("" if enabled else hint)
        self.action_hint.setText(hint)

    def finish(self) -> None:
        self.content.addStretch(1)
