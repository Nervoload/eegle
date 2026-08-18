"""Light visual system for the Workbench demo."""

from __future__ import annotations

from PySide6.QtGui import QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication

COLORS = {
    "shell": "#f6f6f3",
    "sidebar": "#f0f1ee",
    "surface": "#ffffff",
    "surface_alt": "#fafaf8",
    "border": "#dedfdb",
    "border_strong": "#cfd1cc",
    "text": "#17191c",
    "muted": "#676c74",
    "subtle": "#8b9097",
    "blue": "#0758e6",
    "blue_hover": "#004bc8",
    "green": "#16845b",
    "orange": "#ff5a1f",
    "red": "#e63225",
}


def apply_theme(app: QApplication) -> None:
    app.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont))
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(COLORS["shell"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(COLORS["surface"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(COLORS["surface_alt"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(COLORS["surface"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(COLORS["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(COLORS["blue"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    app.setStyleSheet(_STYLE)


_STYLE = f"""
* {{
    color: {COLORS['text']};
    font-size: 13px;
}}
QMainWindow, QWidget#WorkbenchRoot {{
    background: {COLORS['shell']};
}}
QFrame#Sidebar {{
    background: {COLORS['sidebar']};
    border-right: 1px solid {COLORS['border']};
}}
QFrame#ContextHeader {{
    background: rgba(255, 255, 255, 0.78);
    border-bottom: 1px solid {COLORS['border']};
}}
QFrame#ActionBar {{
    background: {COLORS['surface_alt']};
    border-top: 1px solid {COLORS['border']};
}}
QFrame[card="true"] {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 10px;
}}
QFrame[tone="blue"] {{
    background: #f8f9fb;
    border: 1px solid #cbd3df;
    border-radius: 9px;
}}
QFrame[tone="warning"] {{
    background: #fbf9f8;
    border: 1px solid {COLORS['orange']};
    border-radius: 9px;
}}
QFrame[tone="error"] {{
    background: #fbf9f9;
    border: 1px solid {COLORS['red']};
    border-radius: 9px;
}}
QFrame[noticeAccent="blue"] {{ background: {COLORS['blue']}; border: none; }}
QFrame[noticeAccent="warning"] {{ background: {COLORS['orange']}; border: none; }}
QFrame[noticeAccent="error"] {{ background: {COLORS['red']}; border: none; }}
QFrame[tone="muted"] {{
    background: {COLORS['surface_alt']};
    border: 1px dashed {COLORS['border_strong']};
    border-radius: 10px;
}}
QLabel[role="wordmark"] {{
    font-size: 18px;
    font-weight: 650;
    letter-spacing: 0.2px;
}}
QLabel[role="eyebrow"] {{
    color: {COLORS['muted']};
    font-size: 11px;
    font-weight: 650;
    letter-spacing: 0.8px;
}}
QLabel[role="pageTitle"] {{
    font-size: 25px;
    font-weight: 650;
}}
QLabel[role="pageSubtitle"] {{
    color: {COLORS['muted']};
    font-size: 14px;
}}
QLabel[role="sectionTitle"] {{
    font-size: 15px;
    font-weight: 650;
}}
QLabel[role="muted"] {{ color: {COLORS['muted']}; }}
QLabel[role="subtle"] {{ color: {COLORS['subtle']}; font-size: 12px; }}
QLabel[role="metric"] {{ font-size: 19px; font-weight: 650; }}
QLabel[role="statusText"] {{ color: #30343a; font-size: 12px; font-weight: 650; }}
QFrame[statusTone="blue"] {{ background: {COLORS['blue']}; border: none; border-radius: 4px; }}
QFrame[statusTone="success"] {{ background: {COLORS['green']}; border: none; border-radius: 4px; }}
QFrame[statusTone="warning"] {{ background: {COLORS['orange']}; border: none; border-radius: 4px; }}
QFrame[statusTone="error"] {{ background: {COLORS['red']}; border: none; border-radius: 4px; }}
QFrame[statusTone="neutral"] {{ background: #686d74; border: none; border-radius: 4px; }}
QLabel[role="stageNumber"] {{ color: #5f646b; font-size: 13px; font-weight: 700; }}
QPushButton {{
    min-height: 34px;
    padding: 0 13px;
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border_strong']};
    border-radius: 8px;
}}
QPushButton:hover:enabled {{ background: #f4f6f8; border-color: #bfc4ca; }}
QPushButton:pressed:enabled {{ background: #edf0f3; }}
QPushButton:disabled {{ color: #9a9ea4; background: #f2f3f1; border-color: {COLORS['border']}; }}
QPushButton[primary="true"] {{
    color: white;
    background: {COLORS['blue']};
    border-color: {COLORS['blue']};
    font-weight: 650;
    padding: 0 17px;
}}
QPushButton[primary="true"]:hover:enabled {{ background: {COLORS['blue_hover']}; }}
QPushButton[primary="true"]:disabled {{ color: #9098a2; background: #e0e4e8; border-color: #e0e4e8; }}
QPushButton[nav="true"] {{
    text-align: left;
    border: none;
    background: transparent;
    min-height: 36px;
    padding: 0 10px;
}}
QPushButton[nav="true"]:hover {{ background: rgba(30, 35, 40, 0.055); }}
QPushButton[nav="true"]:checked {{ background: #e0e3df; font-weight: 650; }}
QPushButton[groupHeader="true"] {{
    text-align: left;
    border: none;
    background: transparent;
    min-height: 34px;
    padding: 0 8px;
}}
QPushButton[groupHeader="true"]:hover {{ background: rgba(30, 35, 40, 0.05); }}
QFrame#Sidebar QLabel, QFrame#Sidebar QPushButton {{ font-size: 16px; }}
QFrame#Sidebar QLabel[role="wordmark"] {{ font-size: 23px; }}
QFrame#Sidebar QLabel[role="eyebrow"] {{ font-size: 14px; }}
QFrame#Sidebar QLabel[role="muted"], QFrame#Sidebar QLabel[role="subtle"] {{ font-size: 15px; }}
QFrame#Sidebar QPushButton[nav="true"] {{ min-height: 45px; padding: 0 12px; }}
QFrame#Sidebar QPushButton[groupHeader="true"] {{ min-height: 43px; padding: 0 10px; }}
QPushButton[disclosure="true"] {{
    text-align: left;
    min-height: 42px;
    border: none;
    border-radius: 9px;
    background: transparent;
    padding: 0 12px;
    font-weight: 650;
}}
QPushButton[disclosure="true"]:hover {{ background: #f7f8f6; }}
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border_strong']};
    border-radius: 7px;
    padding: 7px 9px;
    selection-background-color: {COLORS['blue']};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border-color: {COLORS['blue']};
}}
QTabWidget::pane {{
    border: 1px solid {COLORS['border']};
    border-radius: 9px;
    background: {COLORS['surface']};
    top: -1px;
}}
QTabBar::tab {{
    color: {COLORS['muted']};
    padding: 9px 13px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {COLORS['text']}; border-bottom-color: {COLORS['blue']}; }}
QHeaderView::section {{
    background: {COLORS['surface_alt']};
    color: {COLORS['muted']};
    padding: 8px;
    border: none;
    border-bottom: 1px solid {COLORS['border']};
    font-weight: 650;
}}
QTableWidget {{
    background: {COLORS['surface']};
    border: 1px solid {COLORS['border']};
    border-radius: 8px;
    gridline-color: {COLORS['border']};
}}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ width: 10px; background: transparent; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #c9ccc8; border-radius: 4px; min-height: 28px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QSplitter::handle {{ background: {COLORS['border']}; width: 1px; }}
"""
