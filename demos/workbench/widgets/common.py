"""Small presentation widgets shared by Workbench pages."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def short_digest(value: str | None, length: int = 12) -> str:
    if not value:
        return "Not available"
    digest = value.removeprefix("sha256:")
    return f"{digest[:length]}…" if len(digest) > length else digest


class StatusIndicator(QWidget):
    """Compact state text with a colour-only dot and no filled label."""

    def __init__(
        self,
        text: str,
        tone: str = "neutral",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(7)
        self._dot = QFrame()
        self._dot.setFixedSize(8, 8)
        self._text = QLabel(text)
        self._text.setProperty("role", "statusText")
        layout.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._text)
        self.set_tone(tone)

    def setText(self, text: str) -> None:
        self._text.setText(text)

    def text(self) -> str:
        return self._text.text()

    def set_tone(self, tone: str) -> None:
        self._dot.setProperty("statusTone", tone)
        self._dot.style().unpolish(self._dot)
        self._dot.style().polish(self._dot)


class Card(QFrame):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        padding: int = 18,
        spacing: int = 10,
    ) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(padding, padding, padding, padding)
        self.layout.setSpacing(spacing)


class InfoBanner(QFrame):
    def __init__(
        self,
        title: str,
        message: str,
        *,
        tone: str = "blue",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("tone", tone)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        accent = QFrame()
        accent.setProperty("noticeAccent", tone)
        accent.setFixedWidth(5)
        layout.addWidget(accent)
        copy = QWidget()
        copy_layout = QVBoxLayout(copy)
        copy_layout.setContentsMargins(15, 12, 16, 12)
        copy_layout.setSpacing(4)
        self.title = QLabel(title)
        self.title.setProperty("role", "sectionTitle")
        self.message = QLabel(message)
        self.message.setProperty("role", "muted")
        self.message.setWordWrap(True)
        copy_layout.addWidget(self.title)
        copy_layout.addWidget(self.message)
        layout.addWidget(copy, 1)


class Metric(QWidget):
    def __init__(self, value: str, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        value_label = QLabel(value)
        value_label.setProperty("role", "metric")
        label_widget = QLabel(label)
        label_widget.setProperty("role", "subtle")
        layout.addWidget(value_label)
        layout.addWidget(label_widget)


class HashLabel(QWidget):
    def __init__(self, label: str, digest: str | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        name = QLabel(label)
        name.setProperty("role", "muted")
        value = QLabel(short_digest(digest))
        value.setToolTip(digest or "Not available")
        layout.addWidget(name)
        layout.addStretch(1)
        layout.addWidget(value)


class EmptyState(QFrame):
    def __init__(
        self,
        title: str,
        message: str,
        *,
        parent: QWidget | None = None,
        minimum_height: int = 190,
    ) -> None:
        super().__init__(parent)
        self.setProperty("tone", "muted")
        self.setMinimumHeight(minimum_height)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(7)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel("○")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 27px; color: #8b9097;")
        title_label = QLabel(title)
        title_label.setProperty("role", "sectionTitle")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        message_label = QLabel(message)
        message_label.setProperty("role", "muted")
        message_label.setWordWrap(True)
        message_label.setMaximumWidth(520)
        message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(icon)
        layout.addWidget(title_label)
        layout.addWidget(message_label)
        layout.addStretch(1)


class DisclosureCard(Card):
    def __init__(
        self,
        title: str,
        summary: str,
        content: QWidget,
        *,
        expanded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, padding=0, spacing=0)
        self._title = title
        self._content = content
        self._toggle = QPushButton()
        self._toggle.setProperty("disclosure", True)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(expanded)
        self._toggle.clicked.connect(self._sync)
        self.layout.addWidget(self._toggle)
        summary_label = QLabel(summary)
        summary_label.setProperty("role", "muted")
        summary_label.setWordWrap(True)
        summary_label.setContentsMargins(13, 0, 13, 12)
        self.layout.addWidget(summary_label)
        content.setContentsMargins(13, 3, 13, 14)
        self.layout.addWidget(content)
        self._sync()

    def _sync(self) -> None:
        expanded = self._toggle.isChecked()
        self._toggle.setText(f"{'⌄' if expanded else '›'}   {self._title}")
        self._content.setVisible(expanded)
