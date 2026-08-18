"""ChatGPT/Obsidian-inspired Workbench navigation."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from demos.workbench.settings import WorkbenchSettings
from demos.workbench.state import ACTIVE_WORKFLOW, Page, WorkbenchState

_PAGE_LABELS = {
    Page.DESIGN: "Design",
    Page.APPARATUS: "Apparatus",
    Page.BUILD: "Build Settings",
    Page.RUN: "Run Experiment",
    Page.SESSIONS: "Sessions",
}

_ASSET_ROOT = Path(__file__).resolve().parent.parent / "assets"


class SidebarGroupButton(QPushButton):
    """Sidebar section button with a native chevron pinned to the right."""

    def __init__(self, text: str, icon_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("groupHeader", True)
        self.setAccessibleName(text)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(11, 0, 9, 0)
        layout.setSpacing(11)
        self._icon = QLabel()
        self._icon.setFixedSize(27, 27)
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(str(icon_path)).scaled(
            QSize(25, 25),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._icon.setPixmap(pixmap)
        self._label = QLabel(text)
        self._chevron = QLabel()
        self._chevron.setFixedSize(22, 22)
        self._chevron.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for child in (self._icon, self._label, self._chevron):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._label, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        layout.addWidget(self._chevron, 0, Qt.AlignmentFlag.AlignVCenter)

    def set_expanded(self, expanded: bool) -> None:
        standard = (
            QStyle.StandardPixmap.SP_ArrowDown
            if expanded
            else QStyle.StandardPixmap.SP_ArrowRight
        )
        self._chevron.setPixmap(self.style().standardIcon(standard).pixmap(18, 18))


class Sidebar(QFrame):
    page_requested = Signal(object)
    new_experiment_requested = Signal()
    active_project_requested = Signal()

    def __init__(self, settings: WorkbenchSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(304)
        self._settings = settings
        self._nav_buttons: dict[Page, QPushButton] = {}
        self._panel_animations: dict[str, QPropertyAnimation] = {}
        self._animation_targets: dict[str, bool] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 15, 14, 14)
        layout.setSpacing(5)

        brand = QWidget()
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(8, 1, 8, 10)
        brand_layout.setSpacing(10)
        brand_icon = QLabel()
        brand_icon.setFixedSize(54, 38)
        brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_icon.setPixmap(
            QPixmap(str(_ASSET_ROOT / "eeglelogo.png")).scaled(
                QSize(54, 38),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        wordmark = QLabel("eegle")
        wordmark.setProperty("role", "wordmark")
        brand_layout.addWidget(brand_icon)
        brand_layout.addWidget(wordmark)
        brand_layout.addStretch(1)
        layout.addWidget(brand)

        self.new_button = QPushButton("New Experiment")
        self.new_button.setObjectName("NewExperimentButton")
        self.new_button.setProperty("nav", True)
        self.new_button.setIcon(QIcon(str(_ASSET_ROOT / "writing.png")))
        self.new_button.setIconSize(QSize(27, 27))
        self.new_button.clicked.connect(self.new_experiment_requested)
        layout.addWidget(self.new_button)

        self._experiments_header = SidebarGroupButton(
            "Experiments", _ASSET_ROOT / "florence-flask.png"
        )
        self._experiments_header.clicked.connect(self._toggle_experiments)
        layout.addWidget(self._experiments_header)
        self._experiments_panel = QFrame()
        experiments_layout = QVBoxLayout(self._experiments_panel)
        experiments_layout.setContentsMargins(14, 0, 0, 5)
        experiments_layout.setSpacing(2)
        active = QPushButton("Study 1 — DSART")
        active.setProperty("nav", True)
        active.clicked.connect(self.active_project_requested)
        experiments_layout.addWidget(active)
        all_experiments = QPushButton("All experiments")
        all_experiments.setProperty("nav", True)
        all_experiments.clicked.connect(lambda: self.page_requested.emit(Page.EXPERIMENTS))
        experiments_layout.addWidget(all_experiments)
        layout.addWidget(self._experiments_panel)

        self._replay_header = SidebarGroupButton("Replay", _ASSET_ROOT / "replay.png")
        self._replay_header.clicked.connect(self._toggle_replay)
        layout.addWidget(self._replay_header)
        self._replay_panel = QFrame()
        replay_layout = QVBoxLayout(self._replay_panel)
        replay_layout.setContentsMargins(14, 0, 0, 5)
        replay_layout.setSpacing(2)
        recent = QLabel("No recent sessions")
        recent.setProperty("role", "subtle")
        recent.setContentsMargins(10, 7, 0, 7)
        replay_layout.addWidget(recent)
        open_replay = QPushButton("Replay & Compare")
        open_replay.setProperty("nav", True)
        open_replay.clicked.connect(lambda: self.page_requested.emit(Page.REPLAY))
        replay_layout.addWidget(open_replay)
        layout.addWidget(self._replay_panel)

        divider = QFrame()
        divider.setFixedHeight(1)
        divider.setStyleSheet("background: #d9dad6; margin: 10px 8px 11px 8px;")
        layout.addWidget(divider)

        section = QLabel("ACTIVE EXPERIMENT")
        section.setProperty("role", "eyebrow")
        section.setContentsMargins(9, 0, 0, 4)
        layout.addWidget(section)
        for page in ACTIVE_WORKFLOW:
            button = QPushButton()
            button.setCheckable(True)
            button.setProperty("nav", True)
            button.clicked.connect(lambda checked=False, selected=page: self.page_requested.emit(selected))
            layout.addWidget(button)
            self._nav_buttons[page] = button
        layout.addStretch(1)

        mode = QFrame()
        mode_layout = QVBoxLayout(mode)
        mode_layout.setContentsMargins(8, 9, 8, 5)
        mode_layout.setSpacing(2)
        local = QLabel("Local demonstration")
        local.setProperty("role", "muted")
        safety = QLabel("Observe-only · no stimulation")
        safety.setProperty("role", "subtle")
        mode_layout.addWidget(local)
        mode_layout.addWidget(safety)
        layout.addWidget(mode)

        self._experiments_expanded = settings.sidebar_expanded("experiments", True)
        self._replay_expanded = settings.sidebar_expanded("replay", False)
        self._set_initial_panel_state(self._experiments_panel, self._experiments_expanded)
        self._set_initial_panel_state(self._replay_panel, self._replay_expanded)
        self._sync_group_headers()

    def _toggle_experiments(self) -> None:
        self._experiments_expanded = not self._experiments_expanded
        self._animate_panel(
            "experiments", self._experiments_panel, self._experiments_expanded
        )
        self._settings.set_sidebar_expanded("experiments", self._experiments_expanded)
        self._sync_group_headers()

    def _toggle_replay(self) -> None:
        self._replay_expanded = not self._replay_expanded
        self._animate_panel("replay", self._replay_panel, self._replay_expanded)
        self._settings.set_sidebar_expanded("replay", self._replay_expanded)
        self._sync_group_headers()

    def _sync_group_headers(self) -> None:
        self._experiments_header.set_expanded(self._experiments_expanded)
        self._replay_header.set_expanded(self._replay_expanded)

    @staticmethod
    def _set_initial_panel_state(panel: QFrame, expanded: bool) -> None:
        panel.setVisible(expanded)
        panel.setMaximumHeight(16777215 if expanded else 0)

    def _animate_panel(self, key: str, panel: QFrame, expanded: bool) -> None:
        animation = self._panel_animations.get(key)
        if animation is None:
            animation = QPropertyAnimation(panel, b"maximumHeight", self)
            animation.setDuration(190)
            animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
            animation.finished.connect(lambda selected=key: self._finish_animation(selected))
            self._panel_animations[key] = animation
        animation.stop()
        self._animation_targets[key] = expanded
        if expanded:
            panel.setVisible(True)
            panel.setMaximumHeight(0)
            panel.layout().activate()
            start, end = 0, panel.sizeHint().height()
        else:
            start, end = panel.height(), 0
        animation.setStartValue(start)
        animation.setEndValue(end)
        animation.start()

    def _finish_animation(self, key: str) -> None:
        panel = self._experiments_panel if key == "experiments" else self._replay_panel
        expanded = self._animation_targets.get(key, False)
        panel.setMaximumHeight(16777215 if expanded else 0)
        panel.setVisible(expanded)

    def render_state(self, state: WorkbenchState) -> None:
        for page, button in self._nav_buttons.items():
            if state.page_completed(page):
                marker = "✓"
            elif state.page_needs_attention(page):
                marker = "●"
            else:
                marker = "○"
            button.setText(f"{marker}   {_PAGE_LABELS[page]}")
            button.setChecked(state.page == page)
