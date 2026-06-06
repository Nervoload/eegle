"""Task registry for reproduction targets."""

from __future__ import annotations

from reproduce.tasks.base import TaskSpec

TASK_SPECS: dict[str, TaskSpec] = {
    "pvt": TaskSpec(
        name="pvt",
        display_name="Psychomotor Vigilance Task",
        status="scaffolded",
        description="Reaction-time vigilance task with randomized foreperiods.",
        closed_loop_ready=True,
        notes=("First target for end-to-end LSL markers, behavior logging, and latency checks.",),
    ),
    "go_nogo": TaskSpec(
        name="go_nogo",
        display_name="Go/No-go",
        status="scaffolded",
        description="Response inhibition task with configurable Go/No-go stimulus split and no-go rule.",
        closed_loop_ready=True,
        notes=("Records exact stimulus onset/offset markers and all button presses for EEG alignment.",),
    ),
    "n_back": TaskSpec(
        name="n_back",
        display_name="N-back",
        status="planned",
        description="Working-memory updating task with configurable n and target probability.",
        closed_loop_ready=False,
        notes=("Implement after PVT logging and marker timing are validated.",),
    ),
    "sternberg": TaskSpec(
        name="sternberg",
        display_name="Sternberg Working Memory",
        status="planned",
        description="Memory set encoding, retention, and probe recognition task.",
        closed_loop_ready=False,
    ),
    "anti_vea": TaskSpec(
        name="anti_vea",
        display_name="ANTI-Vea",
        status="planned",
        description="Attention networks plus vigilance components for sustained attention.",
        closed_loop_ready=False,
    ),
}


def get_task_spec(task_name: str) -> TaskSpec:
    try:
        return TASK_SPECS[task_name]
    except KeyError as exc:
        known = ", ".join(sorted(TASK_SPECS))
        raise KeyError(f"Unknown task '{task_name}'. Known tasks: {known}") from exc


def list_task_specs() -> list[TaskSpec]:
    return [TASK_SPECS[name] for name in sorted(TASK_SPECS)]
