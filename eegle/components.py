"""Experiment component contracts.

The concrete components are intentionally lightweight. The important design
choice is that preflight, task, device recording, realtime processing,
feedback, and analysis are selected independently by config.
"""

from __future__ import annotations

from typing import Protocol

from eegle.hardware.system import CheckResult
from eegle.session import SessionPaths
from eegle.tasks.base import TaskRunResult


class PreflightComponent(Protocol):
    def run(self) -> list[CheckResult]:
        ...


class DeviceRecorderComponent(Protocol):
    def start(self) -> None:
        ...

    def stop(self) -> dict[str, object]:
        ...


class TaskComponent(Protocol):
    def run(self, paths: SessionPaths | None = None) -> TaskRunResult:
        ...


class AnalysisComponent(Protocol):
    def run(self, paths: SessionPaths) -> dict[str, object]:
        ...


class ManagedProcessComponent(Protocol):
    def start(self) -> None:
        ...

    def stop(self) -> dict[str, object]:
        ...
