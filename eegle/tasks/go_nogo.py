"""Go/No-go task for response inhibition experiments."""

from __future__ import annotations

import csv
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from eegle.io.events import EventLogger
from eegle.lsl import LslMarkerOutlet, NullMarkerOutlet, lsl_local_clock, session_marker_source_id
from eegle.realtime.policy import TaskAction, clamp
from eegle.realtime.task_feedback import FeedbackAuditRecord, TaskFeedbackClient
from eegle.runtime import apply_pyglet_macos_notification_patch, ensure_runtime_environment
from eegle.session import SessionPaths, create_session
from eegle.tasks.base import TaskRunResult
from eegle.telemetry import Telemetry


SHAPES = ("circle", "square", "triangle", "star", "hexagon")
COLORS = ("red", "blue", "green", "yellow", "magenta")


@dataclass
class AdaptiveGoNoGoState:
    no_go_probability: float
    isi_seconds: float
    visual_alpha: float = 1.0
    reward_pending: bool = False
    repeat_condition: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "no_go_probability": self.no_go_probability,
            "isi_seconds": self.isi_seconds,
            "visual_alpha": self.visual_alpha,
            "reward_pending": self.reward_pending,
            "repeat_condition": self.repeat_condition,
        }


class GoNoGoTask:
    def __init__(
        self,
        config: dict[str, Any],
        mode: str = "dry-run",
        trials: int | None = None,
        participant_id: str | None = None,
    ) -> None:
        self.config = config
        self.mode = mode
        self.participant_id = participant_id
        self.task_config = dict(config.get("tasks", {}).get("go_nogo", {}))
        if trials is not None:
            self.task_config["trials"] = trials

    def run(self, paths: SessionPaths | None = None) -> TaskRunResult:
        paths = paths or create_session(self.config, task="go_nogo", participant_id=self.participant_id)
        if self.mode == "dry-run":
            summary = self._run_dry(paths)
        elif self.mode == "psychopy":
            summary = self._run_psychopy(paths)
        else:
            raise ValueError(f"Unsupported Go/No-go mode: {self.mode}")
        return TaskRunResult("go_nogo", paths.root, self.mode, summary)

    def _run_dry(self, paths: SessionPaths) -> dict[str, Any]:
        no_go = _resolve_no_go(self.task_config)
        trials = int(self.task_config.get("trials", 40))
        stimulus_seconds = float(self.task_config.get("stimulus_seconds", 0.8))
        state = _make_adaptive_state(self.config, self.task_config)
        feedback_client = _make_task_feedback_client(self.config, paths)
        start = monotonic()
        virtual_time = start
        records: list[dict[str, Any]] = []
        telemetry = Telemetry.from_config(self.config, paths, component="task.go_nogo")
        with EventLogger(paths.behavior_csv, paths.events_jsonl, paths.triggers, telemetry, "task.go_nogo") as logger:
            logger.mark("task_start", event_type="SYSTEM", mode="dry-run", task="go_nogo")
            for index in range(1, trials + 1):
                applied_actions = []
                applied_actions.extend(_poll_and_apply_feedback(feedback_client, logger, "between_trials", index, state))
                applied_actions.extend(_poll_and_apply_feedback(feedback_client, logger, "before_trial_generation", index, state))
                stimulus = _next_adaptive_stimulus(self.task_config, no_go, state)
                onset = virtual_time
                logger.mark(_stim_label("go_nogo_stimulus_onset", index, stimulus), trial=index, **stimulus)
                presses = _simulate_presses(stimulus, onset)
                for press in presses:
                    logger.mark("button_press", trial=index, value=press["key"], rt_seconds=press["rt_seconds"])
                offset = onset + stimulus_seconds
                logger.mark(_stim_label("go_nogo_stimulus_offset", index, stimulus), trial=index, **stimulus)
                records.append(
                    _make_trial_record(
                        index,
                        stimulus,
                        start,
                        onset,
                        offset,
                        presses,
                        applied_actions,
                        state.payload(),
                    )
                )
                _write_go_nogo_outputs(paths, self.task_config, no_go, records)
                virtual_time = offset + state.isi_seconds
            logger.mark("task_end", event_type="SYSTEM", task="go_nogo")
        summary = _summary(records)
        _write_go_nogo_outputs(paths, self.task_config, no_go, records, summary)
        return summary

    def _run_psychopy(self, paths: SessionPaths) -> dict[str, Any]:
        ensure_runtime_environment(self.config.get("runtime", {}).get("runtime_cache_dir", ".runtime"))
        from psychopy import core, event, visual
        apply_pyglet_macos_notification_patch()

        display = self.config.get("hardware", {}).get("display", {})
        markers = self.config.get("hardware", {}).get("markers", {})
        marker_outlet = _make_marker_outlet(markers, paths)
        no_go = _resolve_no_go(self.task_config)
        trials = int(self.task_config.get("trials", 40))
        stimulus_seconds = float(self.task_config.get("stimulus_seconds", 0.8))
        state = _make_adaptive_state(self.config, self.task_config)
        feedback_client = _make_task_feedback_client(self.config, paths)
        response_keys = list(self.task_config.get("response_keys", ["space"]))
        escape_keys = list(dict.fromkeys([*self.task_config.get("escape_keys", ["escape"]), "q"]))
        close_keys = list(dict.fromkeys([*response_keys, "return", "enter", *escape_keys]))
        completion_auto_close = float(self.task_config.get("completion_auto_close_seconds", 30.0))
        practice_enabled = bool(self.task_config.get("practice_enabled", True))
        practice_message_min_seconds = float(self.task_config.get("practice_message_min_seconds", 1.0))
        countdown_seconds = int(self.task_config.get("countdown_seconds", 3))
        countdown_step_seconds = float(self.task_config.get("countdown_step_seconds", 1.0))
        window_size = display.get("size", [1000, 700])

        records: list[dict[str, Any]] = []
        aborted = False
        win = None
        try:
            win = visual.Window(
                fullscr=bool(display.get("full_screen", False)),
                screen=int(display.get("screen_index", 0)),
                size=tuple(window_size),
                winType=_window_backend(display),
                units=display.get("units", "height"),
                color=display.get("background_color", "black"),
                allowGUI=bool(display.get("allow_gui", True)),
            )
            display_timing = _display_timing_config(display, getattr(win, "monitorFramePeriod", None))
            telemetry = Telemetry.from_config(self.config, paths, component="task.go_nogo")
            with EventLogger(paths.behavior_csv, paths.events_jsonl, paths.triggers, telemetry, "task.go_nogo") as logger:
                understood = _show_instructions(win, visual, event, core, no_go, response_keys, escape_keys, trials, stimulus_seconds)
                if not understood:
                    aborted = True
                    _mark(logger, marker_outlet, "instruction_abort", event_type="SYSTEM", task="go_nogo")

                task_start = monotonic()
                if not aborted:
                    _mark(logger, marker_outlet, "task_start", event_type="SYSTEM", mode="psychopy", task="go_nogo")
                    if practice_enabled:
                        practice_aborted = _run_practice(
                            win,
                            visual,
                            event,
                            core,
                            logger,
                            marker_outlet,
                            no_go,
                            self.task_config,
                            response_keys,
                            escape_keys,
                            stimulus_seconds,
                            state.isi_seconds,
                            practice_message_min_seconds,
                            display_timing,
                        )
                        if practice_aborted:
                            aborted = True
                            _mark(logger, marker_outlet, "practice_abort", event_type="SYSTEM", task="go_nogo")
                    if not aborted and countdown_seconds > 0:
                        _run_countdown(win, visual, logger, marker_outlet, countdown_seconds, countdown_step_seconds, display_timing)

                    for index in range(1, trials + 1):
                        if aborted:
                            break
                        applied_actions = []
                        applied_actions.extend(_poll_and_apply_feedback(feedback_client, logger, "between_trials", index, state))
                        applied_actions.extend(_poll_and_apply_feedback(feedback_client, logger, "before_trial_generation", index, state))
                        if state.reward_pending:
                            _show_reward(win, visual, core)
                            state.reward_pending = False
                        stimulus = _next_adaptive_stimulus(self.task_config, no_go, state)
                        record, aborted = _present_stimulus(
                            win=win,
                            visual=visual,
                            event=event,
                            core=core,
                            logger=logger,
                            marker_outlet=marker_outlet,
                            stimulus=stimulus,
                            index=index,
                            task_start=task_start,
                            stimulus_seconds=stimulus_seconds,
                            visual_alpha=state.visual_alpha,
                            response_keys=response_keys,
                            escape_keys=escape_keys,
                            applied_actions=applied_actions,
                            adaptive_state=state.payload(),
                            display_timing=display_timing,
                        )
                        records.append(record)
                        _write_go_nogo_outputs(paths, self.task_config, no_go, records)
                        if aborted:
                            break
                        _safe_wait(state.isi_seconds)
                    _mark(logger, marker_outlet, "task_end", event_type="SYSTEM", task="go_nogo", aborted=aborted)

                summary = _summary(records)
                _write_go_nogo_outputs(paths, self.task_config, no_go, records, summary)
                _show_completion(win, visual, event, core, paths, summary, aborted, close_keys, completion_auto_close)
        except KeyboardInterrupt:
            aborted = True
            summary = _summary(records)
            _write_go_nogo_outputs(paths, self.task_config, no_go, records, summary)
        finally:
            marker_outlet.close()
            if win:
                win.close()

        summary = _summary(records)
        summary["aborted"] = aborted
        return summary


def _present_stimulus(
    win: Any,
    visual: Any,
    event: Any,
    core: Any,
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    stimulus: dict[str, Any],
    index: int,
    task_start: float,
    stimulus_seconds: float,
    visual_alpha: float,
    response_keys: list[str],
    escape_keys: list[str],
    applied_actions: list[dict[str, Any]] | None = None,
    adaptive_state: dict[str, Any] | None = None,
    display_timing: dict[str, float] | None = None,
) -> tuple[dict[str, Any], bool]:
    _draw_stimulus(win, visual, stimulus, label="NO-GO" if stimulus["is_no_go"] else "GO", visual_alpha=visual_alpha)
    onset_holder: dict[str, float] = {}
    rt_clock = core.Clock()
    win.callOnFlip(rt_clock.reset)
    win.callOnFlip(_mark_stimulus_onset, logger, marker_outlet, index, stimulus, onset_holder, display_timing or {})
    event.clearEvents(eventType="keyboard")
    win.flip()

    presses: list[dict[str, Any]] = []
    aborted = False
    timer = core.CountdownTimer(stimulus_seconds)
    while timer.getTime() > 0:
        for key, rt in event.getKeys(timeStamped=rt_clock):
            if key in escape_keys:
                aborted = True
                _mark(logger, marker_outlet, "escape_abort", "SYSTEM", index, key=key)
                continue
            rt_from_flip = float(rt)
            display_latency_seconds = float(onset_holder.get("display_latency_seconds", 0.0))
            corrected_rt = max(0.0, rt_from_flip - display_latency_seconds)
            press_timestamp = float(onset_holder.get("flip_timestamp", onset_holder.get("timestamp", monotonic())) + rt_from_flip)
            press_lsl_timestamp = _add_optional(
                onset_holder.get("flip_lsl_timestamp", onset_holder.get("lsl_timestamp")),
                rt_from_flip,
            )
            press = {
                "key": key,
                "rt_seconds": corrected_rt,
                "rt_from_flip_seconds": rt_from_flip,
                "timestamp_monotonic": press_timestamp,
                "is_response_key": key in response_keys,
            }
            presses.append(press)
            _mark(
                logger,
                marker_outlet,
                "button_press",
                "EVENT",
                index,
                value=key,
                rt_seconds=corrected_rt,
                rt_from_flip_seconds=rt_from_flip,
                is_response_key=key in response_keys,
                _event_monotonic_timestamp=press_timestamp,
                _event_lsl_timestamp=press_lsl_timestamp,
            )
        _safe_wait(0.002)

    win.callOnFlip(_mark_stimulus_offset, logger, marker_outlet, index, stimulus, onset_holder, display_timing or {})
    win.flip()
    onset = onset_holder.get("timestamp", monotonic())
    offset = onset_holder.get("offset_timestamp", monotonic())
    return (
        _make_trial_record(
            index,
            stimulus,
            task_start,
            onset,
            offset,
            presses,
            applied_actions,
            adaptive_state,
            onset_lsl_timestamp=onset_holder.get("lsl_timestamp"),
            offset_lsl_timestamp=onset_holder.get("offset_lsl_timestamp"),
        ),
        aborted,
    )


def _run_practice(
    win: Any,
    visual: Any,
    event: Any,
    core: Any,
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    no_go: dict[str, str],
    config: dict[str, Any],
    response_keys: list[str],
    escape_keys: list[str],
    stimulus_seconds: float,
    isi_seconds: float,
    message_min_seconds: float,
    display_timing: dict[str, float],
) -> bool:
    practice_no_go = {"shape": no_go["shape"], "color": no_go["color"], "is_no_go": True}
    practice_go = _random_go_stimulus(no_go, config)
    continue_keys = list(dict.fromkeys([*response_keys, *escape_keys]))

    if not _show_practice_message(
        win,
        visual,
        event,
        core,
        "Practice Example 1\n\nNO-GO means do not press.\n\nWatch the next stimulus and keep your hands still.\n\nPress SPACE when ready.",
        continue_keys,
        response_keys,
        message_min_seconds,
    ):
        return True
    _, aborted = _present_stimulus(
        win,
        visual,
        event,
        core,
        logger,
        marker_outlet,
        practice_no_go,
        -2,
        monotonic(),
        stimulus_seconds,
        1.0,
        response_keys,
        escape_keys,
        display_timing=display_timing,
    )
    if aborted:
        return True
    _safe_wait(isi_seconds)

    if not _show_practice_message(
        win,
        visual,
        event,
        core,
        "Practice Example 2\n\nGO means press SPACE as quickly as you can.\n\nThe stimulus will stay on screen even after you press.\n\nPress SPACE when ready.",
        continue_keys,
        response_keys,
        message_min_seconds,
    ):
        return True
    _, aborted = _present_stimulus(
        win,
        visual,
        event,
        core,
        logger,
        marker_outlet,
        practice_go,
        -1,
        monotonic(),
        stimulus_seconds,
        1.0,
        response_keys,
        escape_keys,
        display_timing=display_timing,
    )
    if aborted:
        return True
    _safe_wait(isi_seconds)

    return not _show_practice_message(
        win,
        visual,
        event,
        core,
        "Practice complete\n\nThe main task starts next.\n\nPress SPACE to begin.",
        continue_keys,
        response_keys,
        message_min_seconds,
    )


def _run_countdown(
    win: Any,
    visual: Any,
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    seconds: int,
    step_seconds: float,
    display_timing: dict[str, float],
) -> None:
    for value in range(seconds, 0, -1):
        text = visual.TextStim(win, text=f"{value}", height=0.16, color="white", wrapWidth=1.5)
        text.draw()
        win.callOnFlip(
            _mark,
            logger,
            marker_outlet,
            f"countdown_{value}",
            "SYSTEM",
            None,
            None,
            task="go_nogo",
            _scheduled_on_flip=True,
            _display_timing=display_timing,
        )
        win.flip()
        _safe_wait(step_seconds)
    text = visual.TextStim(win, text="Go", height=0.11, color="white", wrapWidth=1.5)
    text.draw()
    win.callOnFlip(
        _mark,
        logger,
        marker_outlet,
        "countdown_go",
        "SYSTEM",
        None,
        None,
        task="go_nogo",
        _scheduled_on_flip=True,
        _display_timing=display_timing,
    )
    win.flip()
    _safe_wait(min(step_seconds, 0.5))


def _show_instructions(
    win: Any,
    visual: Any,
    event: Any,
    core: Any,
    no_go: dict[str, str],
    response_keys: list[str],
    escape_keys: list[str],
    trials: int,
    stimulus_seconds: float,
) -> bool:
    response_text = " / ".join(key.upper() for key in response_keys)
    escape_text = " / ".join(key.upper() for key in escape_keys)
    text = visual.TextStim(
        win,
        text=(
            "Go/No-go Task\n\n"
            f"Press {response_text} for every GO stimulus.\n"
            "Do not press for the NO-GO stimulus.\n\n"
            f"NO-GO stimulus: {no_go['color'].upper()} {no_go['shape'].upper()}\n\n"
            f"Each stimulus stays on screen for {stimulus_seconds:.1f} seconds.\n"
            "The stimulus will not disappear early if you press.\n"
            "All button presses are recorded, including multiple presses.\n\n"
            f"This run has {trials} main stimuli after a short practice.\n\n"
            f"Press {response_text} when you understand and are ready.\n"
            f"Press {escape_text} to abort."
        ),
        height=0.034,
        color="white",
        wrapWidth=1.55,
    )
    text.draw()
    win.flip()
    pressed = _wait_for_key(event, core, list(dict.fromkeys([*response_keys, *escape_keys])), max_wait=None)
    return bool(pressed and pressed[0] in response_keys)


def _show_completion(
    win: Any,
    visual: Any,
    event: Any,
    core: Any,
    paths: SessionPaths,
    summary: dict[str, Any],
    aborted: bool,
    close_keys: list[str],
    max_wait: float,
) -> None:
    text = visual.TextStim(
        win,
        text=(
            f"Go/No-go {'aborted' if aborted else 'complete'}\n\n"
            f"Accuracy: {summary.get('accuracy', 0.0):.1%}\n"
            f"Mean Go RT: {summary.get('mean_go_rt_seconds') or 0.0:.3f} s\n\n"
            f"Data saved to:\n{paths.root}\n\n"
            "Press SPACE, RETURN, ESC, or Q to close."
        ),
        height=0.035,
        color="white",
        wrapWidth=1.55,
    )
    text.draw()
    win.flip()
    _wait_for_key(event, core, close_keys, max_wait=max_wait)


def _show_reward(win: Any, visual: Any, core: Any) -> None:
    text = visual.TextStim(win, text="Nice", height=0.055, color="white", wrapWidth=1.5)
    text.draw()
    win.flip()
    core.wait(0.25)


def _show_practice_message(
    win: Any,
    visual: Any,
    event: Any,
    core: Any,
    text_value: str,
    key_list: list[str],
    response_keys: list[str],
    min_seconds: float,
) -> bool:
    text = visual.TextStim(win, text=text_value, height=0.04, color="white", wrapWidth=1.5)
    text.draw()
    win.flip()
    pressed = _wait_for_key(event, core, key_list, max_wait=None, min_wait=min_seconds)
    return bool(pressed and pressed[0] in response_keys)


def _draw_stimulus(win: Any, visual: Any, stimulus: dict[str, Any], label: str, visual_alpha: float = 1.0) -> None:
    _draw_shape(win, visual, stimulus["shape"], stimulus["color"], visual_alpha)
    visual.TextStim(win, text=label, height=0.045, color="white", pos=(0, -0.24)).draw()


def _draw_shape(win: Any, visual: Any, shape: Any, color: Any, visual_alpha: float = 1.0) -> None:
    shape = _normalize_shape_name(shape)
    if shape == "circle":
        visual.Circle(win, radius=0.12, fillColor=color, lineColor=color, opacity=visual_alpha).draw()
    elif shape == "square":
        visual.Rect(win, width=0.22, height=0.22, fillColor=color, lineColor=color, opacity=visual_alpha).draw()
    elif shape == "triangle":
        visual.Polygon(win, edges=3, radius=0.15, fillColor=color, lineColor=color, opacity=visual_alpha).draw()
    elif shape == "hexagon":
        visual.Polygon(win, edges=6, radius=0.14, fillColor=color, lineColor=color, opacity=visual_alpha).draw()
    elif shape == "star":
        visual.ShapeStim(win, vertices=_star_vertices(0.15, 0.065), fillColor=color, lineColor=color, opacity=visual_alpha).draw()
    elif shape == "x":
        visual.TextStim(win, text="X", height=0.24, color=color, bold=True, opacity=visual_alpha).draw()
    else:
        visual.TextStim(win, text=str(shape).upper(), height=0.14, color=color, opacity=visual_alpha).draw()


def _window_backend(display: dict[str, Any]) -> str:
    return str(display.get("win_type", "pyglet"))


def _make_adaptive_state(config: dict[str, Any], task_config: dict[str, Any]) -> AdaptiveGoNoGoState:
    policy_bounds = config.get("realtime", {}).get("decision_policy", {}).get("bounds", {})
    no_go_probability = float(task_config.get("no_go_probability", 0.3))
    isi_seconds = float(task_config.get("isi_seconds", 0.7))
    visual_alpha = float(task_config.get("visual_alpha", 1.0))
    no_go_bounds = policy_bounds.get("no_go_probability", [0.0, 1.0])
    isi_bounds = policy_bounds.get("isi_seconds", [0.0, 10.0])
    alpha_bounds = policy_bounds.get("visual_alpha", [0.0, 1.0])
    return AdaptiveGoNoGoState(
        no_go_probability=clamp(no_go_probability, float(no_go_bounds[0]), float(no_go_bounds[1])),
        isi_seconds=clamp(isi_seconds, float(isi_bounds[0]), float(isi_bounds[1])),
        visual_alpha=clamp(visual_alpha, float(alpha_bounds[0]), float(alpha_bounds[1])),
    )


def _make_task_feedback_client(config: dict[str, Any], paths: SessionPaths) -> TaskFeedbackClient:
    feedback_config = config.get("realtime", {}).get("feedback", {})
    client_config = dict(feedback_config.get("client", {}))
    if "enabled" not in client_config:
        client_config["enabled"] = bool(feedback_config.get("allow_task_adaptation", False))
    if "backend" not in client_config:
        client_config["backend"] = feedback_config.get("emitter", "disabled")
    return TaskFeedbackClient(client_config, default_jsonl_path=paths.realtime_feedback_jsonl)


def _poll_and_apply_feedback(
    client: TaskFeedbackClient,
    logger: EventLogger,
    boundary: str,
    trial_index: int,
    state: AdaptiveGoNoGoState,
) -> list[dict[str, Any]]:
    actions = client.poll(boundary, trial_index)
    _log_feedback_audit(logger, client.audit_records, trial_index)
    applied = []
    for action in actions:
        result = _apply_task_action(action, state)
        payload = {**action.to_payload(), "application": result, "adaptive_state": state.payload()}
        applied.append(payload)
        logger.mark(
            "feedback_action_applied" if result["status"] == "applied" else "feedback_action_rejected",
            event_type="SYSTEM",
            trial=trial_index,
            value=action.action,
            action=payload,
        )
    return applied


def _log_feedback_audit(logger: EventLogger, records: list[FeedbackAuditRecord], trial_index: int) -> None:
    for record in records:
        if record.status == "pending":
            continue
        logger.mark(
            f"feedback_action_{record.status}",
            event_type="SYSTEM",
            trial=trial_index,
            value=record.reason,
            audit=record.to_payload(),
        )


def _apply_task_action(action: TaskAction, state: AdaptiveGoNoGoState) -> dict[str, Any]:
    params = action.parameters
    if action.action == "observe_only":
        return {"status": "applied", "state_changed": False}
    if action.action == "increase_no_go_probability":
        low = float(params.get("min", 0.0))
        high = float(params.get("max", 1.0))
        delta = float(params.get("delta", action.value if action.value is not None else 0.0))
        before = state.no_go_probability
        state.no_go_probability = clamp(before + delta, low, high)
        return {"status": "applied", "field": "no_go_probability", "before": before, "after": state.no_go_probability}
    if action.action == "adjust_isi":
        low = float(params.get("min", 0.0))
        high = float(params.get("max", 10.0))
        delta = float(params.get("delta", action.value if action.value is not None else 0.0))
        before = state.isi_seconds
        state.isi_seconds = clamp(before + delta, low, high)
        return {"status": "applied", "field": "isi_seconds", "before": before, "after": state.isi_seconds}
    if action.action == "set_visual_alpha":
        if action.value is None:
            return {"status": "rejected", "reason": "set_visual_alpha requires a value"}
        low = float(params.get("min", 0.0))
        high = float(params.get("max", 1.0))
        before = state.visual_alpha
        state.visual_alpha = clamp(float(action.value), low, high)
        return {"status": "applied", "field": "visual_alpha", "before": before, "after": state.visual_alpha}
    if action.action == "repeat_condition":
        condition = str(params.get("condition") or action.value or "")
        if condition not in {"go", "no_go", "nogo"}:
            return {"status": "rejected", "reason": f"unsupported repeat condition {condition}"}
        state.repeat_condition = "no_go" if condition == "nogo" else condition
        return {"status": "applied", "field": "repeat_condition", "after": state.repeat_condition}
    if action.action == "show_reward":
        state.reward_pending = True
        return {"status": "applied", "field": "reward_pending", "after": True}
    return {"status": "rejected", "reason": f"unsupported action {action.action}"}


def _next_adaptive_stimulus(config: dict[str, Any], no_go: dict[str, str], state: AdaptiveGoNoGoState) -> dict[str, Any]:
    condition = state.repeat_condition
    state.repeat_condition = None
    if condition == "no_go":
        return {"shape": no_go["shape"], "color": no_go["color"], "is_no_go": True}
    if condition == "go":
        return _random_go_stimulus(no_go, config)
    if random.random() < state.no_go_probability:
        return {"shape": no_go["shape"], "color": no_go["color"], "is_no_go": True}
    return _random_go_stimulus(no_go, config)


def _star_vertices(outer: float, inner: float) -> list[tuple[float, float]]:
    vertices = []
    for idx in range(10):
        radius = outer if idx % 2 == 0 else inner
        angle = math.pi / 2 + idx * math.pi / 5
        vertices.append((math.cos(angle) * radius, math.sin(angle) * radius))
    return vertices


def _make_sequence(config: dict[str, Any], no_go: dict[str, str]) -> list[dict[str, Any]]:
    trials = int(config.get("trials", 40))
    no_go_probability = float(config.get("no_go_probability", 0.3))
    no_go_count = max(1, round(trials * no_go_probability))
    go_count = max(0, trials - no_go_count)
    sequence = [_random_go_stimulus(no_go, config) for _ in range(go_count)]
    sequence.extend({"shape": no_go["shape"], "color": no_go["color"], "is_no_go": True} for _ in range(no_go_count))
    random.shuffle(sequence)
    return sequence


def _random_go_stimulus(no_go: dict[str, str], config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    shapes = _shape_options(config)
    colors = _color_options(config)
    combos = [(shape, color) for shape in shapes for color in colors if not (shape == no_go["shape"] and color == no_go["color"])]
    shape, color = random.choice(combos)
    return {"shape": shape, "color": color, "is_no_go": False}


def _resolve_no_go(config: dict[str, Any]) -> dict[str, str]:
    no_go_config = dict(config.get("no_go", {}))
    randomize = bool(no_go_config.get("randomize", False))
    shape = no_go_config.get("shape", "x")
    color = no_go_config.get("color", "white")
    if randomize:
        shape = random.choice(_shape_options(config))
        color = random.choice(_color_options(config))
    return {"shape": _normalize_shape_name(shape), "color": str(color)}


def _shape_options(config: dict[str, Any]) -> tuple[str, ...]:
    return tuple(_normalize_shape_name(shape) for shape in config.get("shapes", SHAPES))


def _normalize_shape_name(shape: Any) -> str:
    return str(shape).strip().lower()


def _color_options(config: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(color) for color in config.get("colors", COLORS))


def _make_trial_record(
    index: int,
    stimulus: dict[str, Any],
    task_start: float,
    onset: float,
    offset: float,
    presses: list[dict[str, Any]],
    applied_actions: list[dict[str, Any]] | None = None,
    adaptive_state: dict[str, Any] | None = None,
    onset_lsl_timestamp: float | None = None,
    offset_lsl_timestamp: float | None = None,
) -> dict[str, Any]:
    non_escape_presses = [press for press in presses if press["key"] not in {"escape", "q"}]
    first_press = non_escape_presses[0] if non_escape_presses else None
    first_rt = first_press["rt_seconds"] if first_press else None
    correct = bool((stimulus["is_no_go"] and first_press is None) or ((not stimulus["is_no_go"]) and first_press is not None))
    stimulus_id = _stimulus_id(index, stimulus)
    return {
        "stimulus_number": index,
        "stimulus_id": stimulus_id,
        "stimulus": {
            "id": stimulus_id,
            "kind": "geometry",
            "shape": stimulus["shape"],
            "color": stimulus["color"],
            "is_no_go": bool(stimulus["is_no_go"]),
            "label": "NO-GO" if stimulus["is_no_go"] else "GO",
            "asset_path": None,
            "asset_sha256": None,
        },
        "shape": stimulus["shape"],
        "color": stimulus["color"],
        "is_no_go": bool(stimulus["is_no_go"]),
        "stimulus_onset_monotonic": onset,
        "stimulus_offset_monotonic": offset,
        "stimulus_onset_lsl_timestamp": onset_lsl_timestamp,
        "stimulus_offset_lsl_timestamp": offset_lsl_timestamp,
        "timestamp_since_start": onset - task_start,
        "actual_duration_seconds": offset - onset,
        "presses": presses,
        "button_press_count": len(non_escape_presses),
        "reaction_time_seconds": first_rt,
        "correct_press": int(correct),
        "applied_actions": list(applied_actions or []),
        "adaptive_state": dict(adaptive_state or {}),
    }


def _write_go_nogo_outputs(
    paths: SessionPaths,
    config: dict[str, Any],
    no_go: dict[str, str],
    records: list[dict[str, Any]],
    summary: dict[str, Any] | None = None,
) -> None:
    summary = summary or _summary(records)
    json_path = paths.events / "go_nogo_results.json"
    csv_path = paths.events / "go_nogo_trials.csv"
    manifest_path = paths.events / "stimulus_manifest.json"
    payload = {
        "task": "go_nogo",
        "settings": config,
        "no_go_stimulus": no_go,
        "summary": summary,
        "trials": records,
    }
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    _write_stimulus_manifest(manifest_path, config, no_go, records)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "stimulus_number",
                "is_no_go",
                "reaction_time",
                "correct_press",
                "rolling_average_accuracy",
                "button_press_count",
                "timestamp_since_start",
            ]
        )
        correct_so_far = 0
        for row_idx, record in enumerate(records, start=1):
            correct_so_far += int(record["correct_press"])
            writer.writerow(
                [
                    record["stimulus_number"],
                    int(record["is_no_go"]),
                    "" if record["reaction_time_seconds"] is None else f"{record['reaction_time_seconds']:.6f}",
                    int(record["correct_press"]),
                    f"{correct_so_far / row_idx:.6f}",
                    record["button_press_count"],
                    f"{record['timestamp_since_start']:.6f}",
                ]
            )


def _write_stimulus_manifest(
    path: Path,
    config: dict[str, Any],
    no_go: dict[str, str],
    records: list[dict[str, Any]],
) -> None:
    payload = {
        "schema_version": 1,
        "task": "go_nogo",
        "stimulus_source": "generated_geometry",
        "settings": {
            "shapes": list(_shape_options(config)),
            "colors": list(_color_options(config)),
            "stimulus_seconds": config.get("stimulus_seconds"),
            "isi_seconds": config.get("isi_seconds"),
        },
        "no_go_stimulus": no_go,
        "trials": [
            {
                "trial": record["stimulus_number"],
                "stimulus_id": record["stimulus_id"],
                "stimulus": record["stimulus"],
                "onset_monotonic": record["stimulus_onset_monotonic"],
                "offset_monotonic": record["stimulus_offset_monotonic"],
                "onset_lsl_timestamp": record.get("stimulus_onset_lsl_timestamp"),
                "offset_lsl_timestamp": record.get("stimulus_offset_lsl_timestamp"),
                "timestamp_since_start": record["timestamp_since_start"],
                "actual_duration_seconds": record["actual_duration_seconds"],
                "applied_actions": record.get("applied_actions", []),
                "adaptive_state": record.get("adaptive_state", {}),
                "response": {
                    "reaction_time_seconds": record["reaction_time_seconds"],
                    "button_press_count": record["button_press_count"],
                    "correct_press": record["correct_press"],
                    "presses": record["presses"],
                },
            }
            for record in records
        ],
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "trials": 0,
            "accuracy": 0.0,
            "mean_go_rt_seconds": None,
            "commission_errors": 0,
            "omission_errors": 0,
            "start_monotonic": None,
            "finish_monotonic": None,
            "duration_seconds": None,
        }
    correct = sum(int(record["correct_press"]) for record in records)
    go_rts = [
        record["reaction_time_seconds"]
        for record in records
        if not record["is_no_go"] and record["reaction_time_seconds"] is not None
    ]
    commission_errors = sum(1 for record in records if record["is_no_go"] and record["button_press_count"] > 0)
    omission_errors = sum(1 for record in records if (not record["is_no_go"]) and record["button_press_count"] == 0)
    return {
        "trials": len(records),
        "accuracy": correct / len(records),
        "mean_go_rt_seconds": sum(go_rts) / len(go_rts) if go_rts else None,
        "commission_errors": commission_errors,
        "omission_errors": omission_errors,
        "button_presses": sum(record["button_press_count"] for record in records),
        "start_monotonic": records[0]["stimulus_onset_monotonic"],
        "finish_monotonic": records[-1]["stimulus_offset_monotonic"],
        "duration_seconds": records[-1]["stimulus_offset_monotonic"] - records[0]["stimulus_onset_monotonic"],
    }


def _simulate_presses(stimulus: dict[str, Any], onset: float) -> list[dict[str, Any]]:
    should_press = random.random() < (0.85 if not stimulus["is_no_go"] else 0.12)
    if not should_press:
        return []
    rt = random.uniform(0.18, 0.65)
    presses = [{"key": "space", "rt_seconds": rt, "timestamp_monotonic": onset + rt, "is_response_key": True}]
    if random.random() < 0.08:
        second_rt = presses[0]["rt_seconds"] + random.uniform(0.05, 0.2)
        presses.append({"key": "space", "rt_seconds": second_rt, "timestamp_monotonic": onset + second_rt, "is_response_key": True})
    return presses


def _mark_stimulus_onset(
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    index: int,
    stimulus: dict[str, Any],
    holder: dict[str, float],
    display_timing: dict[str, float],
) -> None:
    record = _mark(
        logger,
        marker_outlet,
        _stim_label("go_nogo_stimulus_onset", index, stimulus),
        "EVENT",
        index,
        **stimulus,
        _scheduled_on_flip=True,
        _display_timing=display_timing,
    )
    holder["timestamp"] = record.timestamp
    holder["flip_timestamp"] = float(record.metadata.get("flip_monotonic", record.timestamp))
    holder["display_latency_seconds"] = float(record.metadata.get("fixed_display_latency_ms", 0.0)) / 1000.0
    lsl_timestamp = record.metadata.get("lsl_timestamp")
    if lsl_timestamp is not None:
        holder["lsl_timestamp"] = float(lsl_timestamp)
    flip_lsl_timestamp = record.metadata.get("flip_lsl_timestamp")
    if flip_lsl_timestamp is not None:
        holder["flip_lsl_timestamp"] = float(flip_lsl_timestamp)


def _mark_stimulus_offset(
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    index: int,
    stimulus: dict[str, Any],
    holder: dict[str, float],
    display_timing: dict[str, float],
) -> None:
    record = _mark(
        logger,
        marker_outlet,
        _stim_label("go_nogo_stimulus_offset", index, stimulus),
        "EVENT",
        index,
        **stimulus,
        _scheduled_on_flip=True,
        _display_timing=display_timing,
    )
    holder["offset_timestamp"] = record.timestamp
    lsl_timestamp = record.metadata.get("lsl_timestamp")
    if lsl_timestamp is not None:
        holder["offset_lsl_timestamp"] = float(lsl_timestamp)


def _stim_label(prefix: str, index: int, stimulus: dict[str, Any]) -> str:
    condition = "nogo" if stimulus["is_no_go"] else "go"
    return f"{prefix}_{index}_{condition}_{stimulus['shape']}_{stimulus['color']}"


def _stimulus_id(index: int, stimulus: dict[str, Any]) -> str:
    condition = "nogo" if stimulus["is_no_go"] else "go"
    return f"go_nogo:{index}:{condition}:{stimulus['shape']}:{stimulus['color']}"


def _wait_for_key(event: Any, core: Any, key_list: list[str], max_wait: float | None, min_wait: float = 0.0) -> list[str]:
    event.clearEvents(eventType="keyboard")
    start = monotonic()
    timer = core.CountdownTimer(max_wait) if max_wait is not None else None
    while timer is None or timer.getTime() > 0:
        pressed = event.getKeys(keyList=key_list)
        if pressed and monotonic() - start >= min_wait:
            return pressed
        _safe_wait(0.02)
    return []


def _safe_wait(seconds: float) -> None:
    """Avoid PsychoPy core.wait's macOS/pyglet event pump issue."""
    sleep(max(0.0, seconds))


def _make_marker_outlet(markers: dict[str, Any], paths: SessionPaths) -> LslMarkerOutlet | NullMarkerOutlet:
    try:
        return LslMarkerOutlet(
            name=markers.get("lsl_stream_name", "EEGleMarkers"),
            stream_type=markers.get("lsl_stream_type", "Markers"),
            source_id=markers.get("source_id") or session_marker_source_id(paths.root),
        )
    except Exception as exc:
        if bool(markers.get("required_for_realtime", False)):
            raise RuntimeError(f"required LSL marker outlet could not be created: {type(exc).__name__}: {exc}") from exc
        return NullMarkerOutlet(f"{type(exc).__name__}: {exc}")


def _mark(
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    label: str,
    event_type: str = "EVENT",
    trial: int | None = None,
    value: str | None = None,
    **metadata: Any,
) -> Any:
    enriched_metadata = dict(metadata)
    scheduled_on_flip = bool(enriched_metadata.pop("_scheduled_on_flip", False))
    display_timing = dict(enriched_metadata.pop("_display_timing", {}) or {})
    explicit_monotonic = enriched_metadata.pop("_event_monotonic_timestamp", None)
    explicit_lsl = enriched_metadata.pop("_event_lsl_timestamp", None)
    flip_monotonic = monotonic()
    flip_lsl_timestamp = lsl_local_clock()
    display_latency_seconds = float(display_timing.get("fixed_display_latency_ms", 0.0)) / 1000.0 if scheduled_on_flip else 0.0
    timestamp = float(explicit_monotonic) if explicit_monotonic is not None else flip_monotonic + display_latency_seconds
    lsl_timestamp = float(explicit_lsl) if explicit_lsl is not None else _add_optional(flip_lsl_timestamp, display_latency_seconds)
    marker_push_started = monotonic()
    marker_outlet.push(label, timestamp=lsl_timestamp)
    marker_push_latency_ms = (monotonic() - marker_push_started) * 1000.0
    if lsl_timestamp is not None:
        enriched_metadata["lsl_timestamp"] = lsl_timestamp
    enriched_metadata["marker_push_latency_ms"] = marker_push_latency_ms
    if scheduled_on_flip:
        enriched_metadata.update(
            {
                "scheduled_on_flip": True,
                "flip_monotonic": flip_monotonic,
                "flip_lsl_timestamp": flip_lsl_timestamp,
                "expected_visual_onset_monotonic": timestamp,
                "expected_visual_onset_lsl_timestamp": lsl_timestamp,
                "fixed_display_latency_ms": float(display_timing.get("fixed_display_latency_ms", 0.0)),
                "expected_refresh_rate_hz": float(display_timing.get("expected_refresh_rate_hz", 60.0)),
                "measured_refresh_rate_hz": float(display_timing.get("measured_refresh_rate_hz", display_timing.get("expected_refresh_rate_hz", 60.0))),
                "expected_frame_interval_ms": float(display_timing.get("expected_frame_interval_ms", 1000.0 / 60.0)),
                "expected_visual_onset_uncertainty_ms": float(display_timing.get("expected_visual_onset_uncertainty_ms", 1000.0 / 120.0)),
                "photodiode_verification_enabled": bool(display_timing.get("photodiode_verification_enabled", False)),
                "timing_model": "psychopy_callOnFlip_plus_fixed_display_latency",
            }
        )
    record = logger.mark(label, event_type=event_type, timestamp=timestamp, trial=trial, value=value, **enriched_metadata)
    return record


def _display_timing_config(display: dict[str, Any], measured_frame_period: Any = None) -> dict[str, float]:
    expected_refresh_hz = max(1.0, float(display.get("expected_refresh_rate_hz", 60.0)))
    try:
        measured_period = float(measured_frame_period)
    except (TypeError, ValueError):
        measured_period = 0.0
    measured_refresh_hz = 1.0 / measured_period if measured_period > 0 else expected_refresh_hz
    return {
        "expected_refresh_rate_hz": expected_refresh_hz,
        "measured_refresh_rate_hz": measured_refresh_hz,
        "expected_frame_interval_ms": 1000.0 / measured_refresh_hz,
        "expected_visual_onset_uncertainty_ms": 500.0 / measured_refresh_hz,
        "fixed_display_latency_ms": float(display.get("fixed_display_latency_ms", 0.0)),
        "photodiode_verification_enabled": float(bool(display.get("photodiode_patch", False))),
    }


def _add_optional(value: Any, offset: float) -> float | None:
    return None if value is None else float(value) + float(offset)
