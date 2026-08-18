"""Layout helpers used across read-only Workbench projections."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget


def label(text: str, role: str | None = None, *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if role:
        widget.setProperty("role", role)
    widget.setWordWrap(wrap)
    return widget


def key_value_rows(rows: tuple[tuple[str, str], ...]) -> QWidget:
    root = QWidget()
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    for name, value in rows:
        row = QHBoxLayout()
        row.setSpacing(14)
        left = label(name, "muted")
        right = label(value)
        right.setWordWrap(True)
        row.addWidget(left)
        row.addStretch(1)
        row.addWidget(right, 2)
        layout.addLayout(row)
    return root
