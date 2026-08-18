"""Minimal, demo-only Study 1 Dynamic SART presentation.

This module intentionally owns presentation, responses, behavioral files, and
task marker publication only. EEGle remains the sole EEG recording authority.
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from demos.workbench.profile import Study1Profile


@dataclass(frozen=True, slots=True)
class TaskTrial:
    trial: int
    block_index: int
    block_name: str
    phase: str
    block_trial: int
    digit: int
    is_no_go: bool
    practice: bool = False
    practice_round: int = 0


@dataclass(frozen=True, slots=True)
class TaskPlan:
    schema: str
    variant: str
    scientific_status: str
    participant_pseudonym: str
    no_go_digit: int
    master_seed: int
    stimulus_seconds: float
    onset_interval_seconds: float
    minimum_valid_rt_seconds: float
    baseline_open_seconds: float
    baseline_closed_seconds: float
    practice_rounds: tuple[tuple[TaskTrial, ...], ...]
    practice_go_accuracy: float
    practice_no_go_accuracy: float
    practice_max_anticipatory_response_rate: float
    practice_feedback_seconds: float
    trials: tuple[TaskTrial, ...]
    break_after_blocks: Mapping[int, float]
    plan_hash: str

    @property
    def total_trials(self) -> int:
        return len(self.trials)

    @property
    def practice_trials(self) -> tuple[TaskTrial, ...]:
        return self.practice_rounds[0] if self.practice_rounds else ()


def build_task_plan(
    profile: Study1Profile,
    *,
    participant_pseudonym: str,
    variant: str | None = None,
) -> TaskPlan:
    """Build a deterministic, behavior-independent DSART plan."""

    pseudonym = participant_pseudonym.strip()
    if not pseudonym:
        raise ValueError("a participant pseudonym is required")
    variant_name = variant or profile.default_variant
    selected = profile.variant(variant_name)
    task = profile.payload["task"]
    master_seed = int(task["master_seed"])
    no_go_digit = _no_go_digit(
        pseudonym,
        tuple(int(value) for value in task["digits"]),
        master_seed,
    )
    practice = selected["practice"]
    practice_rounds = tuple(
        tuple(
            _make_trials(
                pseudonym,
                master_seed,
                no_go_digit,
                digits=tuple(int(value) for value in task["digits"]),
                block_index=0,
                block_name=f"practice_round_{round_index}",
                phase="practice",
                count=int(practice["trials_per_round"]),
                no_go_count=int(practice["no_go_trials"]),
                minimum_leading=int(task["minimum_leading_go_trials"]),
                minimum_gap=int(task["minimum_go_trials_between_no_go"]),
                practice=True,
                practice_round=round_index,
                trial_offset=0,
            )
        )
        for round_index in range(1, int(practice["max_rounds"]) + 1)
    )
    trials: list[TaskTrial] = []
    breaks: dict[int, float] = {}
    for block_index, block in enumerate(selected["blocks"], start=1):
        rows = _make_trials(
            pseudonym,
            master_seed,
            no_go_digit,
            digits=tuple(int(value) for value in task["digits"]),
            block_index=block_index,
            block_name=str(block["name"]),
            phase=str(block["phase"]),
            count=int(block["trials"]),
            no_go_count=int(block["planned_no_go_count"]),
            minimum_leading=int(task["minimum_leading_go_trials"]),
            minimum_gap=int(task["minimum_go_trials_between_no_go"]),
            practice=False,
            trial_offset=len(trials),
        )
        trials.extend(rows)
        duration = float(block.get("break_after_seconds", 0.0))
        if duration > 0:
            breaks[block_index] = duration
    basis = {
        "schema": "eegle.workbench.study1_dsart_plan.v1",
        "variant": variant_name,
        "scientific_status": str(selected["scientific_status"]),
        "participant_pseudonym_hash": hashlib.sha256(pseudonym.encode()).hexdigest(),
        "no_go_digit": no_go_digit,
        "master_seed": master_seed,
        "stimulus_seconds": float(task["stimulus_seconds"]),
        "onset_interval_seconds": float(task["stimulus_onset_interval_seconds"]),
        "minimum_valid_rt_seconds": float(task["minimum_valid_rt_seconds"]),
        "baseline_open_seconds": float(selected["baseline"]["eyes_open_seconds"]),
        "baseline_closed_seconds": float(selected["baseline"]["eyes_closed_seconds"]),
        "practice_go_accuracy": float(practice["go_accuracy"]),
        "practice_no_go_accuracy": float(practice["no_go_accuracy"]),
        "practice_max_anticipatory_response_rate": float(
            practice["max_anticipatory_response_rate"]
        ),
        "practice_feedback_seconds": float(practice["feedback_seconds"]),
        "practice": [
            [_trial_payload(value) for value in round_rows]
            for round_rows in practice_rounds
        ],
        "trials": [_trial_payload(value) for value in trials],
        "break_after_blocks": breaks,
    }
    plan_hash = "sha256:" + hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return TaskPlan(
        schema=str(basis["schema"]),
        variant=variant_name,
        scientific_status=str(selected["scientific_status"]),
        participant_pseudonym=pseudonym,
        no_go_digit=no_go_digit,
        master_seed=master_seed,
        stimulus_seconds=float(task["stimulus_seconds"]),
        onset_interval_seconds=float(task["stimulus_onset_interval_seconds"]),
        minimum_valid_rt_seconds=float(task["minimum_valid_rt_seconds"]),
        baseline_open_seconds=float(selected["baseline"]["eyes_open_seconds"]),
        baseline_closed_seconds=float(selected["baseline"]["eyes_closed_seconds"]),
        practice_rounds=practice_rounds,
        practice_go_accuracy=float(practice["go_accuracy"]),
        practice_no_go_accuracy=float(practice["no_go_accuracy"]),
        practice_max_anticipatory_response_rate=float(
            practice["max_anticipatory_response_rate"]
        ),
        practice_feedback_seconds=float(practice["feedback_seconds"]),
        trials=tuple(trials),
        break_after_blocks=breaks,
        plan_hash=plan_hash,
    )


def marker_label(kind: str, **metadata: Any) -> str:
    """Encode only operational identifiers; labels stay in behavioral output."""

    values = {key: value for key, value in metadata.items() if value is not None}
    if not values:
        return kind
    suffix = "__".join(f"{key}={value}" for key, value in values.items())
    return f"{kind}__{suffix}"


def _no_go_digit(pseudonym: str, digits: tuple[int, ...], master_seed: int) -> int:
    digest = hashlib.sha256(
        f"study1|visit1|{master_seed}|{pseudonym}".encode()
    ).digest()
    return digits[int.from_bytes(digest[:8], "big") % len(digits)]


def _make_trials(
    pseudonym: str,
    master_seed: int,
    no_go_digit: int,
    *,
    digits: tuple[int, ...],
    block_index: int,
    block_name: str,
    phase: str,
    count: int,
    no_go_count: int,
    minimum_leading: int,
    minimum_gap: int,
    practice: bool,
    practice_round: int = 0,
    trial_offset: int,
) -> list[TaskTrial]:
    seed_text = f"{pseudonym}|{master_seed}|{block_index}|{block_name}"
    rng = random.Random(int.from_bytes(hashlib.sha256(seed_text.encode()).digest()[:8], "big"))
    first_position = minimum_leading + 1
    compressed_last = count - minimum_gap * (no_go_count - 1)
    candidates = list(range(first_position, compressed_last + 1))
    if len(candidates) < no_go_count:
        raise ValueError(f"{block_name} cannot satisfy the prepared No-Go spacing")
    compressed = sorted(rng.sample(candidates, no_go_count))
    positions = {
        value + index * minimum_gap for index, value in enumerate(compressed)
    }
    go_digits = [value for value in digits if value != no_go_digit]
    rng.shuffle(go_digits)
    cursor = 0
    rows: list[TaskTrial] = []
    for block_trial in range(1, count + 1):
        is_no_go = block_trial in positions
        if is_no_go:
            digit = no_go_digit
        else:
            if cursor and cursor % len(go_digits) == 0:
                rng.shuffle(go_digits)
            digit = go_digits[cursor % len(go_digits)]
            cursor += 1
        rows.append(
            TaskTrial(
                trial=trial_offset + block_trial,
                block_index=block_index,
                block_name=block_name,
                phase=phase,
                block_trial=block_trial,
                digit=digit,
                is_no_go=is_no_go,
                practice=practice,
                practice_round=practice_round,
            )
        )
    return rows


def _trial_payload(value: TaskTrial) -> dict[str, Any]:
    return {
        "trial": value.trial,
        "block_index": value.block_index,
        "block_name": value.block_name,
        "phase": value.phase,
        "block_trial": value.block_trial,
        "digit": value.digit,
        "is_no_go": value.is_no_go,
        "practice": value.practice,
        "practice_round": value.practice_round,
    }


class _TaskAbort(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def run_psychopy_presentation(
    plan: TaskPlan,
    session_root: Path,
    *,
    publish_marker: Callable[[str], None],
    publish_message: Callable[[Mapping[str, Any]], None],
    command_queue: Any,
    display: Mapping[str, Any],
) -> int:
    """Run the timing-capable DSART renderer in its isolated task process."""

    from psychopy import core, visual
    from psychopy.hardware import keyboard

    expected_rate = float(display["expected_refresh_rate_hz"])
    tolerance = float(display["refresh_rate_tolerance_hz"])
    screen_index = int(display.get("screen_index", 0))
    full_screen = bool(display.get("full_screen", True))
    window = visual.Window(
        size=(1280, 800),
        fullscr=full_screen,
        screen=screen_index,
        color=(-0.92, -0.92, -0.92),
        units="height",
        allowGUI=not full_screen,
    )
    digit = visual.TextStim(window, text="+", color=(0.9, 0.9, 0.9), height=0.22)
    instruction = visual.TextStim(
        window,
        text="",
        color=(0.9, 0.9, 0.9),
        height=0.045,
        wrapWidth=1.25,
    )
    keys = keyboard.Keyboard()
    records: list[dict[str, Any]] = []
    aborted_reason: str | None = None

    try:
        try:
            measured_rate = window.getActualFrameRate(
                nIdentical=20,
                nMaxFrames=120,
                nWarmUpFrames=20,
                threshold=1,
            )
        except Exception:  # noqa: BLE001 - PsychoPy backends expose backend-specific failures
            measured_rate = None
        timing_validated = measured_rate is not None and abs(measured_rate - expected_rate) <= tolerance
        publish_message(
            {
                "type": "armed",
                "renderer": "psychopy",
                "timing_validated": timing_validated,
                "total_trials": plan.total_trials,
                "display": {
                    "screen_index": screen_index,
                    "full_screen": full_screen,
                    "expected_refresh_rate_hz": expected_rate,
                    "measured_refresh_rate_hz": measured_rate,
                    "refresh_rate_tolerance_hz": tolerance,
                },
            }
        )

        _wait_for_start(
            window,
            instruction,
            command_queue,
            text=(
                "Study 1 — Dynamic SART\n\n"
                f"No-Go digit: {plan.no_go_digit}\n\n"
                "Task armed. Waiting for EEGle Workbench."
            ),
        )
        window.callOnFlip(publish_marker, "dynamic_sart_task_start")
        instruction.text = "Session starting"
        instruction.draw()
        window.flip()
        publish_message({"type": "state", "state": "running"})

        publish_message({"type": "phase", "phase": "baseline_open"})
        _timed_screen(
            window,
            instruction,
            keys,
            command_queue,
            "Baseline — keep your eyes open and look at the fixation cross.",
            plan.baseline_open_seconds,
            fixation=True,
        )
        publish_message({"type": "phase", "phase": "baseline_closed"})
        _timed_screen(
            window,
            instruction,
            keys,
            command_queue,
            "Baseline — close your eyes until the next instruction.",
            plan.baseline_closed_seconds,
        )

        publish_marker("dynamic_sart_practice_start")
        for round_index, rows in enumerate(plan.practice_rounds, start=1):
            publish_message(
                {"type": "phase", "phase": "practice", "round": round_index}
            )
            round_records = _run_psychopy_trials(
                window,
                digit,
                keys,
                command_queue,
                plan,
                rows,
                publish_marker,
                publish_message,
            )
            records.extend(round_records)
            metrics = _practice_metrics(round_records, plan.minimum_valid_rt_seconds)
            passed = (
                metrics["go_accuracy"] >= plan.practice_go_accuracy
                and metrics["no_go_accuracy"] >= plan.practice_no_go_accuracy
                and metrics["anticipatory_response_rate"]
                <= plan.practice_max_anticipatory_response_rate
            )
            publish_message(
                {
                    "type": "practice_result",
                    "round": round_index,
                    "passed": passed,
                    "metrics": metrics,
                }
            )
            if passed or round_index == len(plan.practice_rounds):
                break
            _timed_screen(
                window,
                instruction,
                keys,
                command_queue,
                "Practice will repeat. Respond to every digit except your No-Go digit.",
                plan.practice_feedback_seconds,
            )
        publish_marker("dynamic_sart_practice_end")

        by_block: dict[int, list[TaskTrial]] = {}
        for row in plan.trials:
            by_block.setdefault(row.block_index, []).append(row)
        for block_index in sorted(by_block):
            rows = tuple(by_block[block_index])
            phase = rows[0].phase
            publish_marker(
                marker_label(
                    "dynamic_sart_block_start",
                    block=block_index,
                    phase=phase,
                )
            )
            publish_message({"type": "phase", "phase": phase})
            records.extend(
                _run_psychopy_trials(
                    window,
                    digit,
                    keys,
                    command_queue,
                    plan,
                    rows,
                    publish_marker,
                    publish_message,
                )
            )
            publish_marker(
                marker_label(
                    "dynamic_sart_block_end",
                    block=block_index,
                    phase=phase,
                )
            )
            if phase == "support":
                publish_marker("dynamic_sart_support_complete")
            break_seconds = float(plan.break_after_blocks.get(block_index, 0.0))
            if break_seconds:
                publish_marker("dynamic_sart_break_start")
                publish_message({"type": "phase", "phase": "break"})
                _timed_screen(
                    window,
                    instruction,
                    keys,
                    command_queue,
                    "Rehearsal break",
                    break_seconds,
                )
                publish_marker("dynamic_sart_break_end")

        window.callOnFlip(publish_marker, "dynamic_sart_task_end")
        instruction.text = "Rehearsal complete"
        instruction.draw()
        window.flip()
        summary = _write_behavior(session_root, plan, records, aborted=False)
        publish_message({"type": "completed", "behavior_summary": summary})
        core.wait(0.35)
        return 0
    except _TaskAbort as exc:
        aborted_reason = exc.reason
        publish_marker(marker_label("dynamic_sart_abort", reason=exc.reason))
        publish_marker("dynamic_sart_task_end")
        summary = _write_behavior(session_root, plan, records, aborted=True)
        publish_message(
            {
                "type": "aborted",
                "reason": exc.reason,
                "behavior_summary": summary,
            }
        )
        return 2
    finally:
        if aborted_reason is None:
            _consume_abort_command(command_queue)
        window.close()


def _wait_for_start(window: Any, instruction: Any, commands: Any, *, text: str) -> None:
    instruction.text = text
    while True:
        command = _next_command(commands)
        if command == "start":
            return
        if command is not None:
            raise _TaskAbort(command)
        instruction.draw()
        window.flip()


def _timed_screen(
    window: Any,
    instruction: Any,
    keys: Any,
    commands: Any,
    text: str,
    duration: float,
    *,
    fixation: bool = False,
) -> None:
    from psychopy import core, visual

    timer = core.Clock()
    cross = visual.TextStim(window, text="+", color=(0.9, 0.9, 0.9), height=0.08)
    while timer.getTime() < duration:
        reason = _next_command(commands)
        if reason is not None:
            raise _TaskAbort(reason)
        pressed = keys.getKeys(keyList=["escape", "q"], waitRelease=False, clear=True)
        if pressed:
            raise _TaskAbort("escape")
        if fixation:
            cross.draw()
        else:
            instruction.text = text
            instruction.draw()
        window.flip()


def _run_psychopy_trials(
    window: Any,
    digit: Any,
    keys: Any,
    commands: Any,
    plan: TaskPlan,
    rows: tuple[TaskTrial, ...],
    publish_marker: Callable[[str], None],
    publish_message: Callable[[Mapping[str, Any]], None],
) -> list[dict[str, Any]]:
    from psychopy import core, visual

    fixation = visual.TextStim(window, text="+", color=(0.9, 0.9, 0.9), height=0.08)
    values: list[dict[str, Any]] = []
    for row in rows:
        reason = _next_command(commands)
        if reason is not None:
            raise _TaskAbort(reason)
        digit.text = str(row.digit)
        trial_clock = core.Clock()
        keys.clearEvents()
        window.callOnFlip(trial_clock.reset)
        window.callOnFlip(keys.clock.reset)
        window.callOnFlip(
            publish_marker,
            marker_label(
                "dynamic_sart_stimulus_onset",
                trial=row.trial,
                block=row.block_index,
                phase=row.phase,
                practice=int(row.practice),
                practice_round=row.practice_round or None,
            ),
        )
        digit.draw()
        window.flip()
        publish_message(
            {
                "type": "trial",
                "current": row.trial if not row.practice else 0,
                "total": plan.total_trials,
                "practice": row.practice,
                "practice_round": row.practice_round,
            }
        )
        response_time: float | None = None
        offset_sent = False
        while trial_clock.getTime() < plan.onset_interval_seconds:
            reason = _next_command(commands)
            if reason is not None:
                raise _TaskAbort(reason)
            elapsed = trial_clock.getTime()
            if elapsed < plan.stimulus_seconds:
                digit.draw()
            else:
                fixation.draw()
                if not offset_sent:
                    window.callOnFlip(
                        publish_marker,
                        marker_label("dynamic_sart_stimulus_offset", trial=row.trial),
                    )
                    offset_sent = True
            window.flip()
            for pressed in keys.getKeys(
                keyList=["space", "escape", "q"],
                waitRelease=False,
                clear=True,
            ):
                if pressed.name in {"escape", "q"}:
                    raise _TaskAbort("escape")
                if pressed.name == "space" and response_time is None:
                    raw_rt = getattr(pressed, "rt", None)
                    if raw_rt is not None:
                        response_time = float(raw_rt)
        if not offset_sent:
            publish_marker(marker_label("dynamic_sart_stimulus_offset", trial=row.trial))
        valid = response_time is not None and response_time >= plan.minimum_valid_rt_seconds
        correct = (row.is_no_go and not valid) or (not row.is_no_go and valid)
        values.append(
            {
                "record": True,
                **_trial_payload(row),
                "reaction_time_seconds": response_time,
                "valid_response": valid,
                "correct": correct,
            }
        )
        publish_marker(marker_label("dynamic_sart_response_window_close", trial=row.trial))
        publish_marker(marker_label("dynamic_sart_trial_complete", trial=row.trial))
    return values


def _practice_metrics(
    records: list[dict[str, Any]],
    minimum_valid_rt_seconds: float,
) -> dict[str, float]:
    go = [value for value in records if not value["is_no_go"]]
    no_go = [value for value in records if value["is_no_go"]]
    anticipatory = [
        value
        for value in records
        if value.get("reaction_time_seconds") is not None
        and float(value["reaction_time_seconds"]) < minimum_valid_rt_seconds
    ]
    return {
        "go_accuracy": sum(bool(value["correct"]) for value in go) / max(1, len(go)),
        "no_go_accuracy": sum(bool(value["correct"]) for value in no_go)
        / max(1, len(no_go)),
        "anticipatory_response_rate": len(anticipatory) / max(1, len(records)),
    }


def _next_command(commands: Any) -> str | None:
    while not commands.empty():
        value = commands.get_nowait()
        name = str(value.get("command", ""))
        if name == "start":
            return "start"
        if name in {"abort", "stop", "disconnect"}:
            return str(value.get("reason") or "operator")
    return None


def _consume_abort_command(commands: Any) -> None:
    while not commands.empty():
        commands.get_nowait()


def run_qt_presentation(
    plan: TaskPlan,
    session_root: Path,
    *,
    publish_marker: Callable[[str], None],
    publish_message: Callable[[Mapping[str, Any]], None],
    allow_local_start: bool = True,
    command_queue: Any | None = None,
) -> int:
    """Open the immediate demo renderer in an isolated Qt process.

    The window is explicitly labelled as a presentation preview. A live Study 1
    acceptance still requires the PsychoPy renderer and display preflight.
    """

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import (
        QApplication,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    class TaskWindow(QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("Study 1 — Dynamic SART")
            self.resize(980, 680)
            self.setStyleSheet(
                "QWidget { background: #111827; color: #f8fafc; }"
                "QLabel { color: #f8fafc; }"
                "QPushButton { min-height: 38px; padding: 0 18px; background: #2563eb;"
                " color: white; border: none; border-radius: 8px; }"
            )
            layout = QVBoxLayout(self)
            layout.setContentsMargins(42, 32, 42, 32)
            self.preview_label = QLabel("DEMO REHEARSAL · QT PRESENTATION PREVIEW")
            self.preview_label.setStyleSheet("color: #d7dbe2; font-weight: 700;")
            self.status = QLabel("Task armed — waiting for Workbench")
            self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.status.setStyleSheet("font-size: 22px;")
            self.digit = QLabel("+")
            self.digit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.digit.setStyleSheet("font-size: 150px; font-weight: 600;")
            self.detail = QLabel(
                f"Participant {plan.participant_pseudonym}  ·  No-Go digit {plan.no_go_digit}  ·  "
                f"{plan.total_trials} rehearsal trials"
            )
            self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.detail.setStyleSheet("color: #cbd5e1;")
            buttons = QHBoxLayout()
            buttons.addStretch(1)
            self.start_button = QPushButton("Start rehearsal preview")
            self.start_button.setVisible(allow_local_start)
            self.start_button.clicked.connect(self.start)
            self.abort_button = QPushButton("Abort")
            self.abort_button.setStyleSheet("background: #374151;")
            self.abort_button.clicked.connect(lambda: self.abort("operator"))
            buttons.addWidget(self.start_button)
            buttons.addWidget(self.abort_button)
            layout.addWidget(self.preview_label)
            layout.addStretch(1)
            layout.addWidget(self.status)
            layout.addWidget(self.digit, 1)
            layout.addWidget(self.detail)
            layout.addStretch(1)
            layout.addLayout(buttons)
            self._stage = "armed"
            self._rows: list[TaskTrial] = []
            self._cursor = 0
            self._current: TaskTrial | None = None
            self._onset = 0.0
            self._responses: list[dict[str, Any]] = []
            self._ended_blocks: set[int] = set()
            self._trial_timer = QTimer(self)
            self._trial_timer.setSingleShot(True)
            self._trial_timer.timeout.connect(self._finish_trial)
            self._stimulus_timer = QTimer(self)
            self._stimulus_timer.setSingleShot(True)
            self._stimulus_timer.timeout.connect(lambda: self.digit.setText("+"))
            self._phase_timer = QTimer(self)
            self._phase_timer.setSingleShot(True)
            self._phase_timer.timeout.connect(self._advance_phase)
            self._command_timer = QTimer(self)
            self._command_timer.timeout.connect(self._poll_commands)
            self._command_timer.start(50)

        def _poll_commands(self) -> None:
            if command_queue is None:
                return
            while not command_queue.empty():
                command = command_queue.get_nowait()
                name = str(command.get("command", ""))
                if name == "start":
                    self.start()
                elif name in {"abort", "stop"}:
                    self.abort(str(command.get("reason", "operator")))

        def start(self) -> None:
            if self._stage != "armed":
                return
            self.start_button.setEnabled(False)
            publish_marker("dynamic_sart_task_start")
            publish_message({"type": "state", "state": "running"})
            self._stage = "baseline_open"
            self.status.setText("Baseline · eyes open")
            self.digit.setText("+")
            publish_message({"type": "phase", "phase": "baseline_open"})
            self._phase_timer.start(round(plan.baseline_open_seconds * 1000))

        def _advance_phase(self) -> None:
            if self._stage == "baseline_open":
                self._stage = "baseline_closed"
                self.status.setText("Baseline · eyes closed")
                publish_message({"type": "phase", "phase": "baseline_closed"})
                self._phase_timer.start(round(plan.baseline_closed_seconds * 1000))
                return
            if self._stage == "baseline_closed":
                self._stage = "practice"
                self._rows = list(plan.practice_trials)
                self._cursor = 0
                publish_marker("dynamic_sart_practice_start")
                publish_message({"type": "phase", "phase": "practice"})
                self._next_trial()
                return
            if self._stage == "break":
                publish_marker("dynamic_sart_break_end")
                self._stage = "experiment"
                self._next_trial()

        def _next_trial(self) -> None:
            if self._cursor >= len(self._rows):
                if self._stage == "practice":
                    publish_marker("dynamic_sart_practice_end")
                    self._stage = "experiment"
                    self._rows = list(plan.trials)
                    self._cursor = 0
                    self._next_trial()
                    return
                self._complete()
                return
            row = self._rows[self._cursor]
            previous = self._rows[self._cursor - 1] if self._cursor else None
            if (
                self._stage == "experiment"
                and previous is not None
                and previous.block_index != row.block_index
                and previous.block_index not in self._ended_blocks
            ):
                publish_marker(
                    marker_label(
                        "dynamic_sart_block_end",
                        block=previous.block_index,
                        phase=previous.phase,
                    )
                )
                self._ended_blocks.add(previous.block_index)
                if previous.phase == "support":
                    publish_marker("dynamic_sart_support_complete")
                break_seconds = float(plan.break_after_blocks.get(previous.block_index, 0.0))
                if break_seconds:
                    self._stage = "break"
                    self.status.setText(f"Rehearsal break · {break_seconds:g} seconds")
                    publish_marker("dynamic_sart_break_start")
                    publish_message({"type": "phase", "phase": "break"})
                    self._phase_timer.start(round(break_seconds * 1000))
                    return
            if self._stage == "experiment" and (
                previous is None or previous.block_index != row.block_index
            ):
                publish_marker(
                    marker_label(
                        "dynamic_sart_block_start",
                        block=row.block_index,
                        phase=row.phase,
                    )
                )
                publish_message({"type": "phase", "phase": row.phase})
            self._current = row
            self._onset = monotonic()
            self.status.setText(
                f"{'Practice' if row.practice else row.phase.title()} · "
                f"trial {row.block_trial}"
            )
            self.digit.setText(str(row.digit))
            publish_marker(
                marker_label(
                    "dynamic_sart_stimulus_onset",
                    trial=row.trial,
                    block=row.block_index,
                    phase=row.phase,
                    practice=int(row.practice),
                )
            )
            publish_message(
                {
                    "type": "trial",
                    "current": row.trial if not row.practice else 0,
                    "total": plan.total_trials,
                    "practice": row.practice,
                }
            )
            self._stimulus_timer.start(round(plan.stimulus_seconds * 1000))
            self._trial_timer.start(round(plan.onset_interval_seconds * 1000))

        def _finish_trial(self) -> None:
            row = self._current
            if row is None:
                return
            matching = [value for value in self._responses if value["trial"] == row.trial and value["practice"] == row.practice]
            rt = matching[0]["rt_seconds"] if matching else None
            valid_press = rt is not None and rt >= plan.minimum_valid_rt_seconds
            correct = (row.is_no_go and not valid_press) or (not row.is_no_go and valid_press)
            self._responses.append(
                {
                    "record": True,
                    **_trial_payload(row),
                    "reaction_time_seconds": rt,
                    "valid_response": valid_press,
                    "correct": correct,
                }
            )
            publish_marker(marker_label("dynamic_sart_stimulus_offset", trial=row.trial))
            publish_marker(marker_label("dynamic_sart_response_window_close", trial=row.trial))
            publish_marker(marker_label("dynamic_sart_trial_complete", trial=row.trial))
            self._cursor += 1
            self._current = None
            self._next_trial()

        def _complete(self) -> None:
            if self._rows:
                last = self._rows[-1]
                if last.block_index not in self._ended_blocks:
                    publish_marker(
                        marker_label(
                            "dynamic_sart_block_end",
                            block=last.block_index,
                            phase=last.phase,
                        )
                    )
                    self._ended_blocks.add(last.block_index)
            publish_marker("dynamic_sart_task_end")
            self._stage = "completed"
            self.status.setText("Rehearsal complete")
            self.digit.setText("✓")
            summary = _write_behavior(session_root, plan, self._responses, aborted=False)
            publish_message({"type": "completed", "behavior_summary": summary})

        def abort(self, reason: str) -> None:
            if self._stage in {"completed", "aborted"}:
                return
            self._stage = "aborted"
            self._trial_timer.stop()
            self._stimulus_timer.stop()
            self._phase_timer.stop()
            publish_marker(marker_label("dynamic_sart_abort", reason=reason))
            publish_marker("dynamic_sart_task_end")
            summary = _write_behavior(session_root, plan, self._responses, aborted=True)
            publish_message({"type": "aborted", "reason": reason, "behavior_summary": summary})
            self.close()

        def keyPressEvent(self, event: QKeyEvent) -> None:
            if event.key() == Qt.Key.Key_Space and self._current is not None:
                self._responses.append(
                    {
                        "trial": self._current.trial,
                        "practice": self._current.practice,
                        "rt_seconds": monotonic() - self._onset,
                    }
                )
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Q):
                self.abort("escape")
                event.accept()
                return
            super().keyPressEvent(event)

        def closeEvent(self, event: Any) -> None:
            if self._stage not in {"completed", "aborted"}:
                self.abort("window_closed")
            event.accept()

    app = QApplication.instance() or QApplication(["eegle-workbench-study1-task"])
    window = TaskWindow()
    window.show()
    publish_message(
        {
            "type": "armed",
            "renderer": "qt_preview",
            "timing_validated": False,
            "total_trials": plan.total_trials,
        }
    )
    return app.exec()


def _write_behavior(
    session_root: Path,
    plan: TaskPlan,
    records: list[dict[str, Any]],
    *,
    aborted: bool,
) -> dict[str, Any]:
    session_root.mkdir(parents=True, exist_ok=True)
    completed = [value for value in records if value.get("record")]
    csv_path = session_root / "study1_dsart_trials.csv"
    fields = (
        "trial",
        "block_index",
        "block_name",
        "phase",
        "block_trial",
        "digit",
        "is_no_go",
        "practice",
        "practice_round",
        "reaction_time_seconds",
        "valid_response",
        "correct",
    )
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for value in completed:
            writer.writerow({key: value.get(key) for key in fields})
    practice_rows = [value for value in completed if value.get("practice")]
    main_rows = [value for value in completed if not value.get("practice")]
    csv_digest = "sha256:" + hashlib.sha256(csv_path.read_bytes()).hexdigest()
    summary = {
        "schema": "eegle.workbench.study1_dsart_behavior.v1",
        "variant": plan.variant,
        "scientific_status": plan.scientific_status,
        "session_id": session_root.name,
        "plan_hash": plan.plan_hash,
        "participant_pseudonym_hash": hashlib.sha256(
            plan.participant_pseudonym.encode()
        ).hexdigest(),
        "aborted": aborted,
        "completed_trials": len(completed),
        "completed_practice_trials": len(practice_rows),
        "completed_main_trials": len(main_rows),
        "correct_trials": sum(int(bool(value.get("correct"))) for value in completed),
        "trials_csv_digest": csv_digest,
        "trials_csv": str(csv_path),
    }
    summary["summary_hash"] = "sha256:" + hashlib.sha256(
        json.dumps(summary, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    summary_path = session_root / "study1_dsart_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**summary, "summary_json": str(summary_path)}
