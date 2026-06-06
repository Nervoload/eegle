"""Psychomotor vigilance task scaffold."""

from __future__ import annotations

import random
from time import monotonic
from typing import Any

from reproduce.io.events import EventLogger
from reproduce.lsl import LslMarkerOutlet, NullMarkerOutlet
from reproduce.runtime import apply_pyglet_macos_notification_patch, ensure_runtime_environment
from reproduce.session import SessionPaths, create_session
from reproduce.tasks.base import TaskRunResult
from reproduce.telemetry import Telemetry


class PVTTask:
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
        self.task_config = dict(config.get("tasks", {}).get("pvt", {}))
        if trials is not None:
            self.task_config["trials"] = trials

    def run(self, paths: SessionPaths | None = None) -> TaskRunResult:
        paths = paths or create_session(self.config, task="pvt", participant_id=self.participant_id)
        if self.mode == "dry-run":
            summary = self._run_dry(paths)
        elif self.mode == "psychopy":
            summary = self._run_psychopy(paths)
        else:
            raise ValueError(f"Unsupported PVT mode: {self.mode}")
        return TaskRunResult("pvt", paths.root, self.mode, summary)

    def _run_dry(self, paths: SessionPaths) -> dict[str, Any]:
        trials = int(self.task_config.get("trials", 20))
        deadline = float(self.task_config.get("response_deadline_seconds", 2.0))
        min_rt = float(self.task_config.get("minimum_valid_rt_seconds", 0.1))
        rts: list[float] = []
        misses = 0
        too_fast = 0
        premature = 0
        telemetry = Telemetry.from_config(self.config, paths, component="task.pvt")
        with EventLogger(paths.behavior_csv, paths.events_jsonl, paths.triggers, telemetry, "task.pvt") as logger:
            logger.mark("task_start", event_type="SYSTEM", mode="dry-run")
            for trial in range(trials):
                foreperiod = random.uniform(
                    float(self.task_config.get("foreperiod_min_seconds", 2.0)),
                    float(self.task_config.get("foreperiod_max_seconds", 10.0)),
                )
                logger.mark("trial_start", trial=trial, foreperiod_seconds=foreperiod)
                logger.mark("fixation_onset", trial=trial)
                if random.random() < 0.03:
                    premature += 1
                    logger.mark("premature_response", trial=trial, value="", foreperiod_seconds=foreperiod)
                logger.mark("target_onset", trial=trial, foreperiod_seconds=foreperiod)
                if random.random() < 0.08:
                    misses += 1
                    logger.mark("miss", trial=trial, value="")
                else:
                    rt = random.uniform(0.05, min(0.75, deadline))
                    response_class = "too_fast" if rt < min_rt else "valid"
                    if response_class == "too_fast":
                        too_fast += 1
                        logger.mark("too_fast_response", trial=trial, value=f"{rt:.6f}", rt_seconds=rt, minimum_valid_rt_seconds=min_rt)
                    rts.append(rt)
                    logger.mark("response", trial=trial, value=f"{rt:.6f}", rt_seconds=rt, response_class=response_class)
                logger.mark("trial_end", trial=trial)
            logger.mark("task_end", event_type="SYSTEM")
        return {
            "trials": trials,
            "responses": len(rts),
            "misses": misses,
            "premature_responses": premature,
            "too_fast_responses": too_fast,
            "mean_rt_seconds": _mean(rts),
        }

    def _run_psychopy(self, paths: SessionPaths) -> dict[str, Any]:
        ensure_runtime_environment(self.config.get("runtime", {}).get("runtime_cache_dir", ".runtime"))
        from psychopy import core, event, visual
        apply_pyglet_macos_notification_patch()

        display = self.config.get("hardware", {}).get("display", {})
        markers = self.config.get("hardware", {}).get("markers", {})
        marker_outlet = _make_marker_outlet(markers)
        trials = int(self.task_config.get("trials", 20))
        fixation_seconds = float(self.task_config.get("fixation_seconds", 1.0))
        foreperiod_min = float(self.task_config.get("foreperiod_min_seconds", 2.0))
        foreperiod_max = float(self.task_config.get("foreperiod_max_seconds", 10.0))
        deadline_seconds = float(self.task_config.get("response_deadline_seconds", 2.0))
        min_rt = float(self.task_config.get("minimum_valid_rt_seconds", 0.1))
        feedback_seconds = float(self.task_config.get("feedback_seconds", 0.5))
        target_radius = float(self.task_config.get("target_radius", 0.09))
        completion_auto_close = float(self.task_config.get("completion_auto_close_seconds", 30.0))
        instruction_required = bool(self.task_config.get("instruction_required", True))
        response_keys = list(self.task_config.get("response_keys", ["space"]))
        escape_keys = list(dict.fromkeys([*self.task_config.get("escape_keys", ["escape"]), "q"]))
        close_keys = list(dict.fromkeys([*response_keys, "return", "enter", *escape_keys]))
        keys = response_keys + escape_keys

        rts: list[float] = []
        misses = 0
        premature = 0
        too_fast = 0
        aborted = False

        win = None
        try:
            win = visual.Window(
                fullscr=bool(display.get("full_screen", False)),
                screen=int(display.get("screen_index", 0)),
                size=tuple(display.get("size", [1000, 700])),
                winType=_window_backend(display),
                units=display.get("units", "height"),
                color=display.get("background_color", "black"),
                allowGUI=bool(display.get("allow_gui", True)),
            )
            fixation = visual.TextStim(win, text="+", height=0.09, color="white")
            target = visual.Circle(win, radius=target_radius, fillColor="white", lineColor="white", pos=(0, 0))
            target_label = visual.TextStim(win, text="PRESS", height=0.045, color="white", pos=(0, -0.18))
            feedback = visual.TextStim(win, text="", height=0.05, color="white")
            photodiode = visual.Rect(win, width=0.08, height=0.08, pos=(0.45, -0.45), fillColor="white", lineColor="white")

            telemetry = Telemetry.from_config(self.config, paths, component="task.pvt")
            with EventLogger(paths.behavior_csv, paths.events_jsonl, paths.triggers, telemetry, "task.pvt") as logger:
                if instruction_required:
                    understood = _show_instruction_screen(
                        win,
                        visual,
                        event,
                        core,
                        trials=trials,
                        foreperiod_min=foreperiod_min,
                        foreperiod_max=foreperiod_max,
                        deadline_seconds=deadline_seconds,
                        response_keys=response_keys,
                        escape_keys=escape_keys,
                    )
                    if not understood:
                        aborted = True
                        _mark(logger, marker_outlet, "instruction_abort", event_type="SYSTEM")

                if not aborted:
                    _mark(logger, marker_outlet, "task_start", event_type="SYSTEM", mode="psychopy")
                    for trial in range(trials):
                        foreperiod = random.uniform(foreperiod_min, foreperiod_max)
                        fixation.draw()
                        win.callOnFlip(_mark, logger, marker_outlet, "trial_start", "EVENT", trial, foreperiod_seconds=foreperiod)
                        win.callOnFlip(_mark, logger, marker_outlet, "fixation_onset", "EVENT", trial)
                        win.flip()
                        core.wait(fixation_seconds)

                        event.clearEvents(eventType="keyboard")
                        foreperiod_timer = core.CountdownTimer(foreperiod)
                        premature_flagged = False
                        while foreperiod_timer.getTime() > 0:
                            pressed = event.getKeys(keyList=keys)
                            if not pressed:
                                core.wait(0.005)
                                continue
                            key = pressed[0]
                            if key in escape_keys:
                                aborted = True
                                _mark(logger, marker_outlet, "escape_abort", "SYSTEM", trial)
                                break
                            if key in response_keys and not premature_flagged:
                                premature_flagged = True
                                premature += 1
                                _mark(logger, marker_outlet, "premature_response", "EVENT", trial, key=key, foreperiod_seconds=foreperiod)
                                event.clearEvents(eventType="keyboard")
                        if aborted:
                            break

                        target.draw()
                        target_label.draw()
                        if display.get("photodiode_patch", False):
                            photodiode.draw()
                        rt_clock = core.Clock()
                        event.clearEvents(eventType="keyboard")
                        win.callOnFlip(rt_clock.reset)
                        win.callOnFlip(_mark, logger, marker_outlet, "target_onset", "EVENT", trial)
                        win.flip()

                        response_rt = None
                        timer = core.CountdownTimer(deadline_seconds)
                        while timer.getTime() > 0:
                            pressed = event.getKeys(keyList=keys, timeStamped=rt_clock)
                            if not pressed:
                                core.wait(0.002)
                                continue
                            key, key_time = pressed[0]
                            if key in escape_keys:
                                aborted = True
                                _mark(logger, marker_outlet, "escape_abort", "SYSTEM", trial)
                                break
                            if key in response_keys:
                                response_rt = float(key_time)
                                response_class = "too_fast" if response_rt < min_rt else "valid"
                                if response_class == "too_fast":
                                    too_fast += 1
                                    _mark(
                                        logger,
                                        marker_outlet,
                                        "too_fast_response",
                                        "EVENT",
                                        trial,
                                        value=f"{response_rt:.6f}",
                                        rt_seconds=response_rt,
                                        minimum_valid_rt_seconds=min_rt,
                                    )
                                rts.append(response_rt)
                                _mark(
                                    logger,
                                    marker_outlet,
                                    "response",
                                    "EVENT",
                                    trial,
                                    value=f"{response_rt:.6f}",
                                    rt_seconds=response_rt,
                                    response_class=response_class,
                                )
                                break
                        if aborted:
                            break
                        if response_rt is None:
                            misses += 1
                            _mark(logger, marker_outlet, "miss", "EVENT", trial)

                        feedback.text = "Miss" if response_rt is None else f"{response_rt * 1000:.0f} ms"
                        feedback.draw()
                        win.callOnFlip(_mark, logger, marker_outlet, "trial_end", "EVENT", trial)
                        win.flip()
                        core.wait(feedback_seconds)
                    _mark(logger, marker_outlet, "task_end", event_type="SYSTEM", aborted=aborted)

                completion = visual.TextStim(
                    win,
                    text=(
                        f"Task {'aborted' if aborted else 'complete'}\n\n"
                        f"Data saved to:\n{paths.root}\n\n"
                        "Press SPACE, RETURN, ESC, or Q to close."
                    ),
                    height=0.035,
                    color="white",
                    wrapWidth=1.6,
                )
                completion.draw()
                win.flip()
                _wait_for_key(event, core, close_keys, max_wait=completion_auto_close)
        except KeyboardInterrupt:
            aborted = True
        finally:
            marker_outlet.close()
            if win:
                win.close()

        return {
            "trials": trials,
            "responses": len(rts),
            "misses": misses,
            "premature_responses": premature,
            "too_fast_responses": too_fast,
            "mean_rt_seconds": _mean(rts),
            "aborted": aborted,
        }


def _show_instruction_screen(
    win: Any,
    visual: Any,
    event: Any,
    core: Any,
    trials: int,
    foreperiod_min: float,
    foreperiod_max: float,
    deadline_seconds: float,
    response_keys: list[str],
    escape_keys: list[str],
) -> bool:
    response_text = " / ".join(key.upper() for key in response_keys)
    escape_text = " / ".join(key.upper() for key in escape_keys)
    instructions = visual.TextStim(
        win,
        text=(
            "Psychomotor Vigilance Task\n\n"
            "Keep your eyes on the center of the screen.\n\n"
            "A white target will appear after a random wait.\n"
            f"Press {response_text} as quickly as you can when the target appears.\n\n"
            "Do not press before the target appears. Early presses will be flagged but the task will continue.\n"
            f"If no response is made within {deadline_seconds:.1f} seconds, the trial is marked as a miss.\n\n"
            f"This run has {trials} trials. The random wait is {foreperiod_min:.1f}-{foreperiod_max:.1f} seconds.\n\n"
            f"Press {response_text} when you understand and are ready.\n"
            f"Press {escape_text} to abort."
        ),
        height=0.036,
        color="white",
        wrapWidth=1.55,
    )
    instructions.draw()
    win.flip()
    pressed = _wait_for_key(event, core, list(dict.fromkeys([*response_keys, *escape_keys])), max_wait=None)
    return bool(pressed and pressed[0] in response_keys)


def _wait_for_key(event: Any, core: Any, key_list: list[str], max_wait: float | None) -> list[str]:
    event.clearEvents(eventType="keyboard")
    timer = core.CountdownTimer(max_wait) if max_wait is not None else None
    while timer is None or timer.getTime() > 0:
        pressed = event.getKeys(keyList=key_list)
        if pressed:
            return pressed
        core.wait(0.02)
    return []


def _window_backend(display: dict[str, Any]) -> str:
    return str(display.get("win_type", "pyglet"))


def _make_marker_outlet(markers: dict[str, Any]) -> LslMarkerOutlet | NullMarkerOutlet:
    try:
        return LslMarkerOutlet(
            name=markers.get("lsl_stream_name", "ClosedLoopMarkers"),
            stream_type=markers.get("lsl_stream_type", "Markers"),
        )
    except Exception:
        return NullMarkerOutlet()


def _mark(
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    label: str,
    event_type: str = "EVENT",
    trial: int | None = None,
    value: str | None = None,
    **metadata: Any,
) -> None:
    timestamp = monotonic()
    logger.mark(label, event_type=event_type, timestamp=timestamp, trial=trial, value=value, **metadata)
    marker_outlet.push(label, timestamp=timestamp)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)
