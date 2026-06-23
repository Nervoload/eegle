"""Base task abstractions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class TaskSpec:
    name: str
    display_name: str
    status: str
    description: str
    closed_loop_ready: bool = False
    notes: tuple[str, ...] = ()


@dataclass
class TaskRunResult:
    task: str
    session_dir: Path
    mode: str
    summary: dict[str, Any] = field(default_factory=dict)


class RunnableTask(Protocol):
    def run(self) -> TaskRunResult:
        ...
