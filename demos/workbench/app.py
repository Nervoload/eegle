"""Application entry point for EEGle Workbench."""

from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from demos.workbench.controller import WorkbenchController
from demos.workbench.main_window import WorkbenchWindow
from demos.workbench.theme import apply_theme


def create_application(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(argv if argv is not None else sys.argv)
    QCoreApplication.setOrganizationName("EEGle")
    QCoreApplication.setApplicationName("Workbench")
    apply_theme(app)
    return app


def main(argv: list[str] | None = None) -> int:
    app = create_application(argv)
    controller = WorkbenchController()
    window = WorkbenchWindow(controller)
    window.show()
    return app.exec()
