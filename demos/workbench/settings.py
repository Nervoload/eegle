"""Non-scientific Workbench preferences."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSettings


class WorkbenchSettings:
    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings or QSettings("EEGle", "Workbench")

    def recent_projects(self) -> tuple[Path, ...]:
        values = self._settings.value("recent_projects", [], list)
        return tuple(Path(str(value)) for value in values if str(value).strip())

    def record_project(self, path: Path) -> None:
        normalized = str(path.expanduser().resolve())
        values = [normalized]
        values.extend(str(value) for value in self.recent_projects() if str(value) != normalized)
        self._settings.setValue("recent_projects", values[:8])

    def sidebar_expanded(self, group: str, default: bool) -> bool:
        return bool(self._settings.value(f"sidebar/{group}_expanded", default, bool))

    def set_sidebar_expanded(self, group: str, expanded: bool) -> None:
        self._settings.setValue(f"sidebar/{group}_expanded", bool(expanded))

    def task_python(self) -> Path | None:
        configured = os.environ.get("EEGLE_WORKBENCH_TASK_PYTHON") or self._settings.value(
            "task/python", "", str
        )
        if configured:
            path = Path(str(configured)).expanduser()
            return path.resolve() if path.is_file() else None
        repository = Path(__file__).resolve().parents[2]
        candidates = (
            repository / ".venv-workbench-task" / "bin" / "python",
            repository / ".venv-workbench-task" / "Scripts" / "python.exe",
        )
        return next((value.resolve() for value in candidates if value.is_file()), None)

    def set_task_python(self, path: Path | None) -> None:
        self._settings.setValue(
            "task/python",
            "" if path is None else str(path.expanduser().resolve()),
        )
