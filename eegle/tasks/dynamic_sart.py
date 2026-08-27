"""Formal Dynamic-State Sustained Attention to Response Task."""

from __future__ import annotations

import csv
import json
import math
import random
from copy import deepcopy
from pathlib import Path
from statistics import median
from time import monotonic as _system_monotonic
from time import perf_counter as _performance_counter
from time import sleep
from typing import Any, Iterable

from eegle.analysis.dynamic_sart_labels import compute_support_reference
from eegle.devices.lsl_markers import LslMarkerReceiptRecorder
from eegle.io.events import EventLogger
from eegle.lsl import LslMarkerOutlet, NullMarkerOutlet, lsl_local_clock, session_marker_source_id
from eegle.phase_progress import update_dsart_phase_status
from eegle.psychopy_display import (
    create_psychopy_window,
    measure_psychopy_refresh_rate,
    redraw_psychopy_after_resize,
    service_psychopy_static_window,
)
from eegle.psychopy_input import (
    clear_psychopy_keys,
    create_hardware_keyboard,
    poll_hardware_keyboard,
    poll_psychopy_keys,
    stop_hardware_keyboard,
)
from eegle.realtime.policy import TaskAction
from eegle.realtime.task_feedback import TaskFeedbackClient
from eegle.recording_health import RecorderHealthMonitor
from eegle.runtime import apply_pyglet_macos_notification_patch, ensure_runtime_environment
from eegle.session import SessionPaths, create_session
from eegle.tasks.base import TaskRunResult
from eegle.tasks.dynamic_sart_schema import (
    BLOCK_SCHEMA,
    KEY_EVENT_SCHEMA,
    PLAN_SCHEMA,
    PROBE_SCHEMA,
    SUMMARY_SCHEMA,
    TASK_NAME,
    TASK_VERSION,
    TRIAL_SCHEMA,
    DynamicSartConfig,
)
from eegle.tasks.dynamic_sart_sequence import (
    build_dynamic_sart_plan,
    build_dynamic_sart_practice_round,
    marker_label,
    validate_dynamic_sart_plan,
)
from eegle.telemetry import Telemetry


def _performance_counter_monotonic_offset() -> float:
    """Map the high-resolution counter onto the process monotonic origin.

    Python 3.10's Windows monotonic clock can advance in roughly 15.6 ms
    increments. The performance counter is high resolution, while this one-time
    mapping preserves compatibility with recorder timestamps that use the
    process monotonic origin.
    """

    counter_before = _performance_counter()
    reference = _system_monotonic()
    counter_after = _performance_counter()
    return reference - ((counter_before + counter_after) / 2.0)


_PERFORMANCE_COUNTER_MONOTONIC_OFFSET = _performance_counter_monotonic_offset()


def monotonic() -> float:
    """Return high-resolution monotonic time without changing persisted clock origin."""

    return _performance_counter() + _PERFORMANCE_COUNTER_MONOTONIC_OFFSET


EXPERIMENTAL_COUNTDOWN = ("5", "4", "3", "2", "1", "GO!")
PRACTICE_RETRY_CHOICE_AFTER_FAILED_ROUNDS = 2
PRACTICE_FAILURE_POLICY = {
    "schema": "eegle.dynamic_sart.practice_failure_policy.v1",
    "first_failure_action": "automatic_retry_after_feedback",
    "choice_after_failed_rounds": PRACTICE_RETRY_CHOICE_AFTER_FAILED_ROUNDS,
    "available_choices": ["retry", "proceed"],
    "retry_limit": None,
    "round_early_stop": "no_go_target_mathematically_unreachable",
    "practice_failure_terminates_task": False,
}


TRIAL_CSV_FIELDS = (
    "schema_version",
    "schema",
    "task_name",
    "task_version",
    "session_id",
    "participant_id",
    "global_trial_index",
    "block_index",
    "block_trial_index",
    "block_name",
    "phase",
    "study_segment",
    "regime",
    "is_practice",
    "sequence_id",
    "master_seed",
    "block_seed",
    "digit",
    "condition",
    "is_no_go",
    "expected_action",
    "stimulus_marker_label",
    "planned_stimulus_seconds",
    "planned_post_digit_fixation_seconds",
    "planned_response_window_seconds",
    "planned_jitter_seconds",
    "planned_soi_seconds",
    "cue_opportunity",
    "cue_opportunity_index",
    "cue_randomization_block_index",
    "cue_assignment",
    "planned_break_after_trial",
    "planned_break_minimum_seconds",
    "planned_break_maximum_seconds",
    "planned_onset_offset_seconds",
    "stimulus_onset_monotonic",
    "stimulus_onset_lsl",
    "stimulus_offset_monotonic",
    "stimulus_offset_lsl",
    "scheduled_response_window_close_monotonic",
    "scheduled_response_window_close_lsl",
    "response_window_close_monotonic",
    "response_window_close_lsl",
    "response_window_close_overshoot_seconds",
    "scheduled_next_trial_onset_monotonic",
    "actual_next_trial_onset_monotonic",
    "actual_next_trial_onset_lsl",
    "next_trial_onset_monotonic",
    "actual_stimulus_seconds",
    "actual_response_window_seconds",
    "actual_trial_duration_seconds",
    "actual_trial_duration_lsl_seconds",
    "timing_semantics_version",
    "timing_finalization_status",
    "display_timing_status",
    "expected_visual_onset_uncertainty_ms",
    "all_key_event_ids",
    "first_response_key",
    "first_response_timestamp_monotonic",
    "first_response_timestamp_lsl",
    "first_valid_response_key",
    "first_valid_response_timestamp_monotonic",
    "reaction_time_seconds",
    "response_count",
    "response_key_count",
    "primary_outcome",
    "correct",
    "commission_error",
    "omission_error",
    "premature_response",
    "too_fast_response",
    "multiple_response",
    "wrong_key_response",
    "late_response",
    "aborted",
    "invalid",
    "time_on_task_seconds",
    "time_since_break_seconds",
    "previous_trial_index",
    "trials_since_no_go",
    "probe_id",
    "probe_proximity",
    "applied_task_actions",
    "eeg_quality_join_key",
    "presented",
)


class DynamicSartTask:
    """Digit SART with immutable planning and explicit support/query phases."""

    def __init__(
        self,
        config: dict[str, Any],
        mode: str = "dry-run",
        trials: int | None = None,
        participant_id: str | None = None,
    ) -> None:
        self.config = config
        self.mode = mode
        self.trial_override = trials
        self.participant_id = participant_id
        self.task_config = DynamicSartConfig.from_mapping(config.get("tasks", {}).get(TASK_NAME, {}))

    def run(
        self,
        paths: SessionPaths | None = None,
        *,
        marker_outlet: LslMarkerOutlet | NullMarkerOutlet | None = None,
    ) -> TaskRunResult:
        paths = paths or create_session(self.config, task=TASK_NAME, participant_id=self.participant_id)
        plan = build_dynamic_sart_plan(self.task_config, trial_override=self.trial_override)
        validate_dynamic_sart_plan(plan, self.task_config)
        if self.mode == "dry-run":
            summary = self._run_dry(paths, plan, marker_outlet=marker_outlet)
        elif self.mode == "psychopy":
            summary = self._run_psychopy(paths, plan, marker_outlet=marker_outlet)
        else:
            raise ValueError(f"Unsupported Dynamic SART mode: {self.mode}")
        return TaskRunResult(TASK_NAME, paths.root, self.mode, summary)

    def _run_dry(
        self,
        paths: SessionPaths,
        plan: dict[str, Any],
        *,
        marker_outlet: LslMarkerOutlet | NullMarkerOutlet | None = None,
    ) -> dict[str, Any]:
        participant = _participant_id(paths, self.participant_id)
        telemetry = Telemetry.from_config(self.config, paths, component="task.dynamic_sart")
        owns_marker_outlet = marker_outlet is None
        active_marker_outlet: LslMarkerOutlet | NullMarkerOutlet = (
            marker_outlet or NullMarkerOutlet("dynamic_sart dry-run")
        )
        marker_receipt: LslMarkerReceiptRecorder | None = None
        require_live_recorder = bool(
            self.config.get("recording_suite", {}).get("require_live_recorder", False)
        )
        virtual_time = monotonic()
        task_start = virtual_time
        aborted = False
        abort_reason = None
        support_complete = False
        support_reference: dict[str, Any] | None = None
        experimental_completed = 0
        store = DynamicSartArtifactStore(paths, plan, self.task_config, participant)
        feedback_client = _make_task_feedback_client(self.config, paths)
        primary_error: BaseException | None = None
        summary: dict[str, Any] | None = None
        try:
            if require_live_recorder:
                if marker_outlet is None:
                    active_marker_outlet = _make_marker_outlet(
                        dict(self.config.get("hardware", {}).get("markers", {}) or {}),
                        paths,
                    )
                marker_receipt = _start_marker_receipt_recorder(active_marker_outlet, paths)
                settle_seconds = float(
                    self.config.get("recording_rehearsal", {}).get(
                        "marker_discovery_settle_seconds",
                        0.5,
                    )
                )
                if settle_seconds > 0:
                    sleep(settle_seconds)
            with EventLogger(paths.behavior_csv, paths.events_jsonl, paths.triggers, telemetry, "task.dynamic_sart") as logger:
                _emit(logger, active_marker_outlet, marker_label("task_start"), event_type="SYSTEM", timestamp=virtual_time, mode="dry-run", task=TASK_NAME)
                practice_passed = not self.task_config.practice_enabled
                if self.task_config.practice_enabled:
                    _emit(logger, active_marker_outlet, marker_label("practice_start"), event_type="SYSTEM", timestamp=virtual_time, task=TASK_NAME)
                    for round_index, practice_plan in enumerate(plan["practice_rounds"], start=1):
                        round_records = []
                        for trial in practice_plan:
                            record, virtual_time = _simulate_trial(
                                trial,
                                self.task_config,
                                store,
                                logger,
                                active_marker_outlet,
                                task_start,
                                virtual_time,
                                plan_trial_count=len(plan["planned_trials"]),
                                force_practice_correct=True,
                            )
                            round_records.append(record)
                        criteria = practice_criteria(round_records, self.task_config, comprehension_confirmed=True)
                        store.append_block(_block_result(practice_plan, round_records, criteria=criteria))
                        if criteria["passed"]:
                            practice_passed = True
                            break
                    _emit(
                        logger,
                        active_marker_outlet,
                        marker_label("practice_end"),
                        event_type="SYSTEM",
                        timestamp=virtual_time,
                        task=TASK_NAME,
                        passed=practice_passed,
                    )
                if not practice_passed:
                    aborted = True
                    abort_reason = "practice_criteria_not_met"

                if not aborted:
                    virtual_time = _run_dry_countdown(
                        logger,
                        active_marker_outlet,
                        virtual_time,
                        self.task_config.countdown_step_seconds,
                    )

                blocks = list(plan["planned_blocks"])
                trials_by_block = {
                    int(block["block_index"]): [
                        trial for trial in plan["planned_trials"] if int(trial["block_index"]) == int(block["block_index"])
                    ]
                    for block in blocks
                }
                final_support_block = max(int(block["block_index"]) for block in blocks if block["phase"] == "support")
                abort_after = _optional_int(self.task_config.dry_run.get("abort_after_trial"))
                last_break_monotonic = task_start
                for block in blocks:
                    if aborted:
                        break
                    block_index = int(block["block_index"])
                    _emit(
                        logger,
                        active_marker_outlet,
                        marker_label("block_start", block=block_index, phase=block["phase"]),
                        event_type="SYSTEM",
                        timestamp=virtual_time,
                        block_index=block_index,
                        block_name=block["block_name"],
                        phase=block["phase"],
                    )
                    block_records = []
                    for trial in trials_by_block[block_index]:
                        action_audits = _poll_and_audit_dynamic_sart_feedback(
                            feedback_client,
                            logger,
                            trial_index=int(trial["global_trial_index"]),
                            block_index=block_index,
                        )
                        record, virtual_time = _simulate_trial(
                            trial,
                            self.task_config,
                            store,
                            logger,
                            active_marker_outlet,
                            task_start,
                            virtual_time,
                            plan_trial_count=len(plan["planned_trials"]),
                            applied_task_actions=action_audits,
                            last_break_monotonic=last_break_monotonic,
                        )
                        block_records.append(record)
                        if bool(record.get("probe_after")):
                            virtual_time = _record_dry_probe(
                                store,
                                logger,
                                active_marker_outlet,
                                record,
                                self.task_config,
                                virtual_time,
                            )
                        experimental_completed += 1
                        if abort_after is not None and experimental_completed >= abort_after:
                            aborted = True
                            abort_reason = "dry_run_abort_after_trial"
                            break
                    _emit(
                        logger,
                        active_marker_outlet,
                        marker_label("block_end", block=block_index, phase=block["phase"]),
                        event_type="SYSTEM",
                        timestamp=virtual_time,
                        block_index=block_index,
                        block_name=block["block_name"],
                        phase=block["phase"],
                        completed_trials=len(block_records),
                    )
                    store.append_block(_block_result(trials_by_block[block_index], block_records))
                    if not aborted and block_index == final_support_block:
                        support_reference = _complete_support(
                            store,
                            self.task_config,
                            virtual_time,
                            logger,
                            active_marker_outlet,
                        )
                        support_complete = True
                    if not aborted and bool(block.get("break_after")):
                        break_minimum = float(block.get("minimum_break_seconds", 0.0))
                        break_maximum = float(block.get("maximum_break_seconds", break_minimum))
                        _emit(
                            logger,
                            active_marker_outlet,
                            marker_label("break_start", block=block_index),
                            event_type="SYSTEM",
                            timestamp=virtual_time,
                            minimum_break_seconds=break_minimum,
                            maximum_break_seconds=break_maximum,
                        )
                        virtual_time += break_minimum
                        _emit(
                            logger,
                            active_marker_outlet,
                            marker_label("break_end", block=block_index),
                            event_type="SYSTEM",
                            timestamp=virtual_time,
                            minimum_break_seconds=break_minimum,
                            maximum_break_seconds=break_maximum,
                            actual_break_seconds=break_minimum,
                            break_end_reason="dry_run_minimum_elapsed",
                        )
                        last_break_monotonic = virtual_time

                if aborted:
                    _emit(
                        logger,
                        active_marker_outlet,
                        marker_label("abort"),
                        event_type="SYSTEM",
                        timestamp=virtual_time,
                        reason=abort_reason,
                        completed_experimental_trials=experimental_completed,
                    )
                _emit(
                    logger,
                    active_marker_outlet,
                    marker_label("task_end"),
                    event_type="SYSTEM",
                    timestamp=virtual_time,
                    task=TASK_NAME,
                    aborted=aborted,
                )
            summary = summarize_dynamic_sart(
                store.records,
                aborted=aborted,
                abort_reason=abort_reason,
                support_complete=support_complete,
                planned_experimental_trials=len(plan["planned_trials"]),
            )
            store.finalize(summary, aborted=aborted, abort_reason=abort_reason)
            return summary
        except Exception as exc:
            primary_error = exc
            abort_reason = f"exception:{type(exc).__name__}"
            summary = summarize_dynamic_sart(
                store.records,
                aborted=True,
                abort_reason=abort_reason,
                support_complete=support_complete,
                planned_experimental_trials=len(plan["planned_trials"]),
            )
            try:
                store.finalize(summary, aborted=True, abort_reason=abort_reason)
            except Exception as finalize_exc:
                exc.add_note(f"Additional DSART finalization error: {finalize_exc}")
            raise
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if marker_receipt is not None:
                drain_warning = _drain_emitted_markers(
                    self.config,
                    active_marker_outlet,
                    marker_receipt,
                )
                if drain_warning:
                    _attach_cleanup_warnings(summary, telemetry, [drain_warning])
            critical_cleanup_errors = _close_task_resources(
                ("marker receipt recorder", marker_receipt),
                ("artifact store", store),
            )
            cleanup_warnings = _close_task_resources(
                ("marker outlet", active_marker_outlet if owns_marker_outlet else None)
            )
            _attach_cleanup_warnings(summary, telemetry, cleanup_warnings)
            if critical_cleanup_errors:
                detail = "; ".join(critical_cleanup_errors)
                if primary_error is not None:
                    primary_error.add_note(f"Additional DSART cleanup error: {detail}")
                else:
                    raise RuntimeError(f"DSART cleanup failed: {detail}")

    def _run_psychopy(
        self,
        paths: SessionPaths,
        plan: dict[str, Any],
        *,
        marker_outlet: LslMarkerOutlet | NullMarkerOutlet | None = None,
    ) -> dict[str, Any]:
        ensure_runtime_environment(self.config.get("runtime", {}).get("runtime_cache_dir", ".runtime"))
        from psychopy import visual
        from psychopy.hardware import keyboard as keyboard_module

        apply_pyglet_macos_notification_patch()
        participant = _participant_id(paths, self.participant_id)
        display = dict(self.config.get("hardware", {}).get("display", {}))
        markers = dict(self.config.get("hardware", {}).get("markers", {}))
        telemetry = Telemetry.from_config(self.config, paths, component="task.dynamic_sart")
        owns_marker_outlet = marker_outlet is None
        marker_receipt: LslMarkerReceiptRecorder | None = None
        store: DynamicSartArtifactStore | None = None
        win = None
        hardware_keyboard = None
        aborted = False
        abort_reason = None
        support_complete = False
        support_reference = None
        practice_passed = not self.task_config.practice_enabled
        practice_proceeded_without_passing = False
        practice_rounds_completed = 0
        task_start = monotonic()
        primary_error: BaseException | None = None
        summary: dict[str, Any] | None = None
        suite_config = dict(self.config.get("recording_suite", {}) or {})
        recorder_monitor = RecorderHealthMonitor(
            paths.process_logs / "recorder.status.json",
            required=bool(suite_config.get("require_live_recorder", False)),
            stall_timeout_seconds=float(suite_config.get("recorder_stall_timeout_seconds", 5.0)),
        )
        try:
            if marker_outlet is None:
                marker_outlet = _make_marker_outlet(markers, paths)
            if recorder_monitor.required:
                marker_receipt = _start_marker_receipt_recorder(marker_outlet, paths)
            store = DynamicSartArtifactStore(paths, plan, self.task_config, participant)
            feedback_client = _make_task_feedback_client(self.config, paths)
            update_dsart_phase_status("creating_psychopy_window", session_dir=str(paths.root))
            win = create_psychopy_window(visual, display, title="EEGle Dynamic SART")
            update_dsart_phase_status("measuring_display_refresh", session_dir=str(paths.root))
            timing = measure_psychopy_refresh_rate(win, display)
            store.set_display_timing(timing)
            digit_stimulus = visual.TextStim(win, text="", height=0.22, color="white")
            fixation_stimulus = visual.TextStim(win, text="+", height=0.08, color="white")
            update_dsart_phase_status("starting_ptb_keyboard", session_dir=str(paths.root))
            hardware_keyboard = create_hardware_keyboard(
                keyboard_module,
                backend=str(display.get("keyboard_backend", "ptb")),
            )
            keyboard = PersistentKeyboardCollector(
                hardware_keyboard,
                hardware_keyboard.clock,
                store,
                self.task_config.response_keys,
                self.task_config.escape_keys,
                hardware=True,
            )
            update_dsart_phase_status("ptb_keyboard_ready", session_dir=str(paths.root))
            with EventLogger(
                paths.behavior_csv,
                paths.events_jsonl,
                paths.triggers,
                telemetry,
                "task.dynamic_sart",
                flush_each_event=False,
            ) as logger:
                keyboard.logger = logger
                task_start = monotonic()
                _emit(logger, marker_outlet, marker_label("task_start"), event_type="SYSTEM", mode="psychopy")
                if not _recorder_health_gate(recorder_monitor, logger, marker_outlet):
                    aborted = True
                    abort_reason = "recorder_health_failure"
                update_dsart_phase_status(
                    "awaiting_instruction_key",
                    session_dir=str(paths.root),
                    expected_key="space",
                )
                understood = False if aborted else _show_screen(
                    win,
                    visual,
                    keyboard,
                    (
                        f"A digit from {min(self.task_config.digits)} to {max(self.task_config.digits)} "
                        "will appear in the centre of the screen.\n\n"
                        f"Press SPACE for every digit except {self.task_config.no_go_digit}.\n"
                        f"When {self.task_config.no_go_digit} appears, do not press.\n\n"
                        "Respond quickly while remaining accurate.\n\nPress SPACE to continue."
                    ),
                    state="INSTRUCTIONS",
                    allowed_continue=self.task_config.response_keys,
                )
                update_dsart_phase_status(
                    "instruction_screen_complete",
                    session_dir=str(paths.root),
                    continued=bool(understood),
                )
                if not aborted and not understood:
                    aborted = True
                    abort_reason = "instruction_abort"

                if not aborted and self.task_config.practice_enabled:
                    update_dsart_phase_status("practice", session_dir=str(paths.root))
                    _emit(logger, marker_outlet, marker_label("practice_start"), event_type="SYSTEM")
                    practice_passed = False
                    practice_proceeded_without_passing = False
                    practice_rounds = list(plan["practice_rounds"])
                    practice_round_index = 1
                    while not aborted and not practice_passed and not practice_proceeded_without_passing:
                        if practice_round_index <= len(practice_rounds):
                            practice_plan = practice_rounds[practice_round_index - 1]
                        else:
                            practice_plan = build_dynamic_sart_practice_round(
                                self.task_config,
                                practice_round_index,
                                sequence_id=str(plan["sequence_id"]),
                            )
                            store.ensure_practice_round_planned(practice_plan)
                        round_records = []
                        practice_premature: list[dict[str, Any]] = []
                        practice_early_stop: dict[str, Any] | None = None
                        planned_no_go_trials = sum(
                            int(bool(row.get("is_no_go"))) for row in practice_plan
                        )
                        for trial_index, trial in enumerate(practice_plan):
                            if not _recorder_health_gate(recorder_monitor, logger, marker_outlet, trial=trial):
                                aborted = True
                                abort_reason = "recorder_health_failure"
                                break
                            record, trial_aborted, practice_premature, _ = _present_psychopy_trial(
                                win,
                                visual,
                                keyboard,
                                logger,
                                marker_outlet,
                                store,
                                trial,
                                self.task_config,
                                timing,
                                task_start,
                                premature_events=practice_premature,
                                next_trial=None,
                                digit_stimulus=digit_stimulus,
                                fixation_stimulus=fixation_stimulus,
                            )
                            if trial_aborted:
                                if record is not None:
                                    round_records.append(record)
                                aborted = True
                                abort_reason = "escape_abort"
                                break
                            if record is not None:
                                round_records.append(record)
                                feedback_text = "Correct" if record["correct"] else _practice_feedback(record, self.task_config.no_go_digit)
                                feedback_ok = _show_timed_text(
                                    win,
                                    visual,
                                    keyboard,
                                    feedback_text,
                                    self.task_config.practice_feedback_seconds,
                                )
                                if not feedback_ok:
                                    aborted = True
                                    abort_reason = "escape_abort"
                                    break
                                no_go_status = practice_no_go_target_status(
                                    round_records,
                                    practice_plan[trial_index + 1 :],
                                    self.task_config,
                                    planned_no_go_trials=planned_no_go_trials,
                                )
                                if not no_go_status["reachable"]:
                                    practice_early_stop = no_go_status
                                    _emit(
                                        logger,
                                        marker_outlet,
                                        marker_label(
                                            "practice_round_early_stop",
                                            round=practice_round_index,
                                            reason="no_go_target_unreachable",
                                        ),
                                        event_type="SYSTEM",
                                        practice_round=practice_round_index,
                                        reason="no_go_target_unreachable",
                                        completed_trials=len(round_records),
                                        planned_trials=len(practice_plan),
                                        no_go_target=no_go_status,
                                    )
                                    break
                        criteria = practice_criteria(
                            round_records,
                            self.task_config,
                            comprehension_confirmed=True,
                            planned_no_go_trials=planned_no_go_trials,
                        )
                        if practice_early_stop is not None:
                            criteria.update(
                                {
                                    "ended_early": True,
                                    "early_stop_reason": "no_go_target_unreachable",
                                    "completed_trial_count": len(round_records),
                                    "planned_trial_count": len(practice_plan),
                                    "unpresented_trial_count": len(practice_plan) - len(round_records),
                                    "no_go_target_status": practice_early_stop,
                                }
                            )
                        practice_rounds_completed = practice_round_index
                        store.append_block(_block_result(practice_plan, round_records, criteria=criteria))
                        if aborted or criteria["passed"]:
                            practice_passed = criteria["passed"]
                            break
                        if practice_round_index < PRACTICE_RETRY_CHOICE_AFTER_FAILED_ROUNDS:
                            continued = _show_screen(
                                win,
                                visual,
                                keyboard,
                                _practice_status_text(
                                    criteria,
                                    self.task_config,
                                    will_repeat=True,
                                ),
                                state="PRACTICE_FEEDBACK",
                                allowed_continue=self.task_config.response_keys,
                            )
                            if not continued:
                                aborted = True
                                abort_reason = "practice_abort"
                                break
                            practice_round_index += 1
                            continue
                        decision = _show_practice_failure_choice(
                            win,
                            visual,
                            keyboard,
                            criteria,
                            self.task_config,
                            practice_round_index,
                        )
                        _emit(
                            logger,
                            marker_outlet,
                            marker_label(
                                "practice_decision",
                                round=practice_round_index,
                                action=decision,
                            ),
                            event_type="SYSTEM",
                            practice_round=practice_round_index,
                            decision=decision,
                            criteria=criteria,
                        )
                        if decision == "abort":
                            aborted = True
                            abort_reason = "practice_abort"
                            break
                        if decision == "proceed":
                            practice_proceeded_without_passing = True
                            break
                        practice_round_index += 1
                    _emit(
                        logger,
                        marker_outlet,
                        marker_label("practice_end"),
                        event_type="SYSTEM",
                        passed=practice_passed,
                        proceeded_without_passing=practice_proceeded_without_passing,
                        rounds_completed=practice_round_index,
                    )
                    if (
                        not aborted
                        and (practice_passed or practice_proceeded_without_passing)
                        and self.task_config.practice_require_ready_confirmation
                    ):
                        _emit(
                            logger,
                            marker_outlet,
                            marker_label("practice_ready_start"),
                            event_type="SYSTEM",
                        )
                        practice_ready = _show_practice_ready(
                            win,
                            visual,
                            keyboard,
                            self.task_config,
                            passed=practice_passed,
                        )
                        _emit(
                            logger,
                            marker_outlet,
                            marker_label("practice_ready_end"),
                            event_type="SYSTEM",
                            continued=practice_ready,
                        )
                        if not practice_ready:
                            aborted = True
                            abort_reason = "practice_ready_abort"

                if not aborted:
                    update_dsart_phase_status("countdown", session_dir=str(paths.root))
                    countdown_ok, countdown_reason = _run_psychopy_countdown(
                        win,
                        visual,
                        keyboard,
                        logger,
                        marker_outlet,
                        timing,
                        recorder_monitor,
                        self.task_config.countdown_step_seconds,
                    )
                    if not countdown_ok:
                        aborted = True
                        abort_reason = countdown_reason

                blocks = list(plan["planned_blocks"])
                final_support_block = max(int(block["block_index"]) for block in blocks if block["phase"] == "support")
                premature_for_next: list[dict[str, Any]] = []
                preflipped_onset: dict[str, Any] | None = None
                last_break_monotonic = task_start
                for block in blocks:
                    if aborted:
                        break
                    block_index = int(block["block_index"])
                    update_dsart_phase_status(
                        "experimental_block",
                        session_dir=str(paths.root),
                        block_index=block_index,
                        block_count=len(blocks),
                    )
                    block_trials = [row for row in plan["planned_trials"] if int(row["block_index"]) == block_index]
                    _emit(
                        logger,
                        marker_outlet,
                        marker_label("block_start", block=block_index, phase=block["phase"]),
                        event_type="SYSTEM",
                        block_index=block_index,
                        phase=block["phase"],
                    )
                    block_records = []
                    for local_index, trial in enumerate(block_trials):
                        # A preflipped trial was already health-gated before its onset. Checking it
                        # here would happen after the stimulus was visible and could leave an onset
                        # marker without a matching trial record if the check failed.
                        if preflipped_onset is None and not _recorder_health_gate(
                            recorder_monitor,
                            logger,
                            marker_outlet,
                            trial=trial,
                        ):
                            aborted = True
                            abort_reason = "recorder_health_failure"
                            break
                        next_trial = (
                            block_trials[local_index + 1]
                            if local_index + 1 < len(block_trials)
                            and not bool(trial.get("probe_after"))
                            and not feedback_client.enabled
                            else None
                        )
                        recorder_failed_before_next = False
                        if next_trial is not None and not _recorder_health_gate(
                            recorder_monitor,
                            logger,
                            marker_outlet,
                            trial=next_trial,
                        ):
                            # Finish and persist the current, already-started trial, but do not show
                            # the next digit on its boundary frame.
                            next_trial = None
                            recorder_failed_before_next = True
                        action_audits = _poll_and_audit_dynamic_sart_feedback(
                            feedback_client,
                            logger,
                            trial_index=int(trial["global_trial_index"]),
                            block_index=block_index,
                        )
                        record, trial_aborted, premature_for_next, preflipped_onset = _present_psychopy_trial(
                            win,
                            visual,
                            keyboard,
                            logger,
                            marker_outlet,
                            store,
                            trial,
                            self.task_config,
                            timing,
                            task_start,
                            premature_events=premature_for_next,
                            next_trial=next_trial,
                            preflipped_onset=preflipped_onset,
                            digit_stimulus=digit_stimulus,
                            fixation_stimulus=fixation_stimulus,
                            applied_task_actions=action_audits,
                            last_break_monotonic=last_break_monotonic,
                        )
                        if trial_aborted:
                            if record is not None:
                                block_records.append(record)
                            aborted = True
                            abort_reason = "escape_abort"
                            break
                        if record is not None:
                            block_records.append(record)
                            if bool(record.get("probe_after")):
                                probe_ok = _present_psychopy_probe(
                                    win,
                                    visual,
                                    keyboard,
                                    logger,
                                    marker_outlet,
                                    store,
                                    record,
                                    self.task_config,
                                )
                                if not probe_ok:
                                    aborted = True
                                    abort_reason = "probe_abort"
                                    break
                                preflipped_onset = None
                        if recorder_failed_before_next:
                            aborted = True
                            abort_reason = "recorder_health_failure"
                            break
                    timing_warning = _block_timing_warning(block_records, timing)
                    if timing_warning is not None:
                        _emit(
                            logger,
                            marker_outlet,
                            marker_label("timing_warning", block=block_index),
                            event_type="SYSTEM",
                            block_index=block_index,
                            **timing_warning,
                        )
                    _emit(
                        logger,
                        marker_outlet,
                        marker_label("block_end", block=block_index, phase=block["phase"]),
                        event_type="SYSTEM",
                        block_index=block_index,
                        phase=block["phase"],
                        completed_trials=len(block_records),
                    )
                    store.append_block(_block_result(block_trials, block_records))
                    logger.flush()
                    if not aborted and block_index == final_support_block:
                        support_reference = _complete_support(store, self.task_config, monotonic(), logger, marker_outlet)
                        support_complete = True
                    if not aborted and bool(block.get("break_after")):
                        break_minimum = float(block.get("minimum_break_seconds", 30.0))
                        break_maximum = float(block.get("maximum_break_seconds", break_minimum))
                        break_started = monotonic()
                        _emit(
                            logger,
                            marker_outlet,
                            marker_label("break_start", block=block_index),
                            event_type="SYSTEM",
                            timestamp=break_started,
                            minimum_break_seconds=break_minimum,
                            maximum_break_seconds=break_maximum,
                        )
                        continued, break_result = _show_bounded_break(
                            win,
                            visual,
                            keyboard,
                            break_minimum,
                            break_maximum,
                            started_at=break_started,
                        )
                        _emit(
                            logger,
                            marker_outlet,
                            marker_label("break_end", block=block_index),
                            event_type="SYSTEM",
                            timestamp=float(break_result["ended_at_monotonic"]),
                            **break_result,
                        )
                        last_break_monotonic = monotonic()
                        if not continued:
                            aborted = True
                            abort_reason = "break_abort"

                if aborted:
                    _emit(logger, marker_outlet, marker_label("abort"), event_type="SYSTEM", reason=abort_reason)
                _emit(logger, marker_outlet, marker_label("task_end"), event_type="SYSTEM", aborted=aborted)
                summary = summarize_dynamic_sart(
                    store.records,
                    aborted=aborted,
                    abort_reason=abort_reason,
                    support_complete=support_complete,
                    planned_experimental_trials=len(plan["planned_trials"]),
                )
                summary.update(
                    {
                        "practice_passed": practice_passed,
                        "practice_proceeded_without_passing": practice_proceeded_without_passing,
                        "practice_rounds_completed": practice_rounds_completed,
                        "practice_failure_policy": deepcopy(PRACTICE_FAILURE_POLICY),
                    }
                )
                store.finalize(summary, aborted=aborted, abort_reason=abort_reason)
                update_dsart_phase_status(
                    "completion_screen",
                    session_dir=str(paths.root),
                    aborted=aborted,
                )
                _show_completion(win, visual, keyboard, paths, summary, self.task_config.completion_auto_close_seconds)
                return summary
        except KeyboardInterrupt:
            aborted = True
            abort_reason = "keyboard_interrupt"
            summary = summarize_dynamic_sart(
                store.records,
                aborted=True,
                abort_reason=abort_reason,
                support_complete=support_complete,
                planned_experimental_trials=len(plan["planned_trials"]),
            )
            store.finalize(summary, aborted=True, abort_reason=abort_reason)
            return summary
        except Exception as exc:
            primary_error = exc
            aborted = True
            abort_reason = f"exception:{type(exc).__name__}"
            if store is not None:
                summary = summarize_dynamic_sart(
                    store.records,
                    aborted=True,
                    abort_reason=abort_reason,
                    support_complete=support_complete,
                    planned_experimental_trials=len(plan["planned_trials"]),
                )
                try:
                    store.finalize(summary, aborted=True, abort_reason=abort_reason)
                except Exception as finalize_exc:
                    exc.add_note(f"Additional DSART finalization error: {finalize_exc}")
            raise
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            update_dsart_phase_status("task_cleanup", session_dir=str(paths.root))
            drain_warnings = []
            if marker_receipt is not None and marker_outlet is not None:
                drain_warning = _drain_emitted_markers(
                    self.config,
                    marker_outlet,
                    marker_receipt,
                )
                if drain_warning:
                    drain_warnings.append(drain_warning)
            critical_cleanup_errors = _close_task_resources(
                ("marker receipt recorder", marker_receipt),
                ("artifact store", store),
            )
            cleanup_warnings = _close_task_resources(
                ("marker outlet", marker_outlet if owns_marker_outlet else None),
                ("hardware keyboard", _HardwareKeyboardCleanup(hardware_keyboard)),
                ("PsychoPy window", win),
            )
            _attach_cleanup_warnings(summary, telemetry, [*drain_warnings, *cleanup_warnings])
            if critical_cleanup_errors:
                detail = "; ".join(critical_cleanup_errors)
                if primary_error is not None:
                    primary_error.add_note(f"Additional DSART cleanup error: {detail}")
                else:
                    raise RuntimeError(f"DSART cleanup failed: {detail}")


class DynamicSartArtifactStore:
    """Buffer timing-path writes and checkpoint them at block boundaries."""

    def __init__(
        self,
        paths: SessionPaths,
        plan: dict[str, Any],
        config: DynamicSartConfig,
        participant_id: str,
    ) -> None:
        self.paths = paths
        self.plan = plan
        self.config = config
        self.participant_id = participant_id
        self.records: list[dict[str, Any]] = []
        self.key_events: list[dict[str, Any]] = []
        self.trials_jsonl_path = paths.events / "dynamic_sart_trials.jsonl"
        self.trials_csv_path = paths.events / "dynamic_sart_trials.csv"
        self.key_events_path = paths.events / "dynamic_sart_key_events.jsonl"
        self.blocks_path = paths.events / "dynamic_sart_blocks.csv"
        self.results_path = paths.events / "dynamic_sart_results.json"
        self.reference_path = paths.events / "dynamic_sart_support_reference.json"
        self.probes_path = paths.events / "dynamic_sart_probes.jsonl"
        self.manifest_path = paths.events / "stimulus_manifest.json"
        self._trial_jsonl = None
        self._key_jsonl = None
        self._probes_jsonl = None
        self._trial_csv = None
        self._blocks_csv = None
        try:
            self._trial_jsonl = self.trials_jsonl_path.open("w", encoding="utf-8")
            self._key_jsonl = self.key_events_path.open("w", encoding="utf-8")
            self._probes_jsonl = self.probes_path.open("w", encoding="utf-8")
            self._trial_csv = self.trials_csv_path.open("w", encoding="utf-8", newline="")
            self._trial_writer = csv.DictWriter(self._trial_csv, fieldnames=list(TRIAL_CSV_FIELDS))
            self._trial_writer.writeheader()
            self._blocks_csv = self.blocks_path.open("w", encoding="utf-8", newline="")
            self._block_fields = [
                "schema",
                "block_index",
                "block_name",
                "phase",
                "planned_trials",
                "completed_trials",
                "correct_trials",
                "start_trial",
                "end_trial",
                "criteria",
            ]
            self._block_writer = csv.DictWriter(self._blocks_csv, fieldnames=self._block_fields)
            self._block_writer.writeheader()
            self.manifest = self._initial_manifest()
            _write_json_atomic(self.manifest_path, self.manifest)
        except BaseException:
            _close_task_resources(("partial DSART artifact store", self))
            raise

    def _initial_manifest(self) -> dict[str, Any]:
        practice = [deepcopy(row) for round_rows in self.plan["practice_rounds"] for row in round_rows]
        experimental = [deepcopy(row) for row in self.plan["planned_trials"]]
        planned = [*practice, *experimental]
        for row in planned:
            row["stimulus_marker_label"] = marker_label("stimulus_onset", row)
        mutable = [{**deepcopy(row), "presented": False, "completion_status": "planned", "abort_status": None} for row in planned]
        return {
            "schema_version": 1,
            "schema": PLAN_SCHEMA,
            "task": TASK_NAME,
            "task_name": TASK_NAME,
            "task_version": TASK_VERSION,
            "participant_id": self.participant_id,
            "session_id": self.paths.root.name,
            "master_seed": self.plan["master_seed"],
            "block_seeds": self.plan["block_seeds"],
            "sequence_id": self.plan["sequence_id"],
            "configuration_hash": self.plan["configuration_hash"],
            "planned_blocks": self.plan["planned_blocks"],
            "planned_trials": planned,
            "planned_digits": list(self.config.digits),
            "planned_conditions": ["go", "no_go"],
            "planned_jitter": [row.get("planned_jitter_seconds") for row in experimental],
            "smoke_test_override": self.plan["smoke_test_override"],
            "requested_trial_override": self.plan["requested_trial_override"],
            "normal_recipe_trial_count": self.plan["normal_recipe_trial_count"],
            "practice_failure_policy": deepcopy(PRACTICE_FAILURE_POLICY),
            "trials": mutable,
            "support_complete": False,
            "aborted": False,
        }

    def set_display_timing(self, timing: dict[str, Any]) -> None:
        self.manifest["display_timing"] = deepcopy(timing)
        _write_json_atomic(self.manifest_path, self.manifest)

    def ensure_practice_round_planned(self, practice_plan: list[dict[str, Any]]) -> None:
        """Add an on-demand retry round to the manifest before it is presented."""

        if not practice_plan:
            raise ValueError("practice retry plan must contain trials")
        round_index = int(practice_plan[0]["practice_round"])
        if any(
            int(row.get("practice_round") or 0) == round_index
            for row in self.manifest["planned_trials"]
            if bool(row.get("is_practice"))
        ):
            return
        planned = [deepcopy(row) for row in practice_plan]
        for row in planned:
            row["stimulus_marker_label"] = marker_label("stimulus_onset", row)
        mutable = [
            {
                **deepcopy(row),
                "presented": False,
                "completion_status": "planned",
                "abort_status": None,
            }
            for row in planned
        ]
        planned_insert = next(
            (
                index
                for index, row in enumerate(self.manifest["planned_trials"])
                if not bool(row.get("is_practice"))
            ),
            len(self.manifest["planned_trials"]),
        )
        trial_insert = next(
            (
                index
                for index, row in enumerate(self.manifest["trials"])
                if not bool(row.get("is_practice"))
            ),
            len(self.manifest["trials"]),
        )
        self.manifest["planned_trials"][planned_insert:planned_insert] = planned
        self.manifest["trials"][trial_insert:trial_insert] = mutable
        extended = self.manifest.setdefault("runtime_extended_practice_rounds", [])
        if round_index not in extended:
            extended.append(round_index)
        _write_json_atomic(self.manifest_path, self.manifest)

    def append_trial(self, record: dict[str, Any]) -> None:
        immutable = deepcopy(record)
        self.records.append(immutable)
        self._trial_jsonl.write(json.dumps(immutable, sort_keys=True) + "\n")
        self._trial_writer.writerow({field: _csv_value(immutable.get(field)) for field in TRIAL_CSV_FIELDS})
        target = next(
            (
                row
                for row in self.manifest["trials"]
                if int(row.get("global_trial_index")) == int(immutable["global_trial_index"])
            ),
            None,
        )
        if target is not None:
            target.update(deepcopy(immutable))
            target["completion_status"] = (
                "aborted" if bool(immutable.get("aborted")) else "complete"
            )
            target["abort_status"] = immutable.get("abort_reason")
            if bool(immutable.get("aborted")):
                # Escape is an exceptional terminal boundary; persist its manifest
                # state immediately rather than waiting for the normal block checkpoint.
                _write_json_atomic(self.manifest_path, self.manifest)

    def append_key_event(self, event: dict[str, Any]) -> None:
        immutable = deepcopy(event)
        self.key_events.append(immutable)
        self._key_jsonl.write(json.dumps(immutable, sort_keys=True) + "\n")

    def append_probe(self, probe: dict[str, Any]) -> None:
        self._probes_jsonl.write(json.dumps(deepcopy(probe), sort_keys=True) + "\n")

    def append_block(self, result: dict[str, Any]) -> None:
        self._block_writer.writerow({field: _csv_value(result.get(field)) for field in self._block_fields})
        self.checkpoint()
        self.manifest["last_checkpoint"] = {
            "completed_record_count": len(self.records),
            "last_completed_trial_index": (
                None if not self.records else self.records[-1].get("global_trial_index")
            ),
            "block_index": result.get("block_index"),
            "block_name": result.get("block_name"),
        }
        _write_json_atomic(self.manifest_path, self.manifest)

    def checkpoint(self) -> None:
        """Flush task ledgers only at a non-stimulus block boundary."""

        for handle in (
            self._trial_jsonl,
            self._key_jsonl,
            self._probes_jsonl,
            self._trial_csv,
            self._blocks_csv,
        ):
            handle.flush()

    def write_support_reference(self, reference: dict[str, Any]) -> None:
        _write_json_atomic(self.reference_path, reference)
        self.manifest["support_complete"] = True
        self.manifest["support_reference_hash"] = reference.get("reference_hash")
        _write_json_atomic(self.manifest_path, self.manifest)

    def finalize(self, summary: dict[str, Any], *, aborted: bool, abort_reason: str | None) -> None:
        self.manifest["aborted"] = aborted
        self.manifest["abort_reason"] = abort_reason
        self.manifest["presented_trial_count"] = len(self.records)
        self.manifest["completed_experimental_trial_count"] = sum(
            int(
                not row.get("is_practice")
                and not row.get("aborted")
                and not row.get("invalid")
            )
            for row in self.records
        )
        _write_json_atomic(self.manifest_path, self.manifest)
        _write_json_atomic(
            self.results_path,
            {
                "schema": SUMMARY_SCHEMA,
                "task": TASK_NAME,
                "settings": self.config.payload(),
                "sequence_id": self.plan["sequence_id"],
                "summary": summary,
                "aborted": aborted,
                "abort_reason": abort_reason,
                "artifact_files": {
                    "trials_jsonl": str(self.trials_jsonl_path),
                    "trials_csv": str(self.trials_csv_path),
                    "key_events_jsonl": str(self.key_events_path),
                    "blocks_csv": str(self.blocks_path),
                    "support_reference_json": str(self.reference_path),
                    "probes_jsonl": str(self.probes_path),
                },
            },
        )

    def close(self) -> None:
        failures = []
        for handle in (self._trial_jsonl, self._key_jsonl, self._probes_jsonl, self._trial_csv, self._blocks_csv):
            if handle is None:
                continue
            try:
                if not handle.closed:
                    handle.close()
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")
        if failures:
            raise RuntimeError("one or more DSART artifact files did not close: " + "; ".join(failures))


def _close_task_resources(*resources: tuple[str, Any]) -> list[str]:
    failures = []
    for name, resource in resources:
        if resource is None:
            continue
        try:
            resource.close()
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    return failures


class _HardwareKeyboardCleanup:
    """Adapt PsychoPy's public ``stop`` lifecycle to task resource cleanup."""

    def __init__(self, keyboard: Any | None) -> None:
        self.keyboard = keyboard

    def close(self) -> None:
        stop_hardware_keyboard(self.keyboard)


def _attach_cleanup_warnings(
    summary: dict[str, Any] | None,
    telemetry: Telemetry,
    warnings: list[str],
) -> None:
    if not warnings:
        return
    if summary is not None:
        summary.setdefault("warnings", []).extend(warnings)
    telemetry.emit(
        "task.cleanup_warning",
        level="default",
        message="DSART completed with non-critical cleanup warnings",
        metadata={"warnings": warnings},
    )


class PersistentKeyboardCollector:
    """Drain one persistent keyboard queue without per-trial clearing."""

    _STATIC_CHECKPOINT_STATES = {
        "INSTRUCTIONS",
        "PRACTICE_DECISION",
        "PRACTICE_READY",
        "PRACTICE_FEEDBACK",
        "BREAK",
        "COMPLETE",
    }

    def __init__(
        self,
        event_module: Any,
        clock: Any,
        store: DynamicSartArtifactStore,
        response_keys: Iterable[str],
        escape_keys: Iterable[str],
        *,
        hardware: bool = False,
    ) -> None:
        self.event_module = event_module
        self.hardware = bool(hardware)
        self.clock = clock
        self.store = store
        self.response_keys = {str(key).strip().lower() for key in response_keys}
        self.escape_keys = {str(key).strip().lower() for key in escape_keys}
        self.sequence = 0
        self.logger: EventLogger | None = None
        self.clock.reset()
        elapsed = float(self.clock.getTime())
        self.origin_monotonic = monotonic() - elapsed
        current_lsl = lsl_local_clock()
        self.origin_lsl = None if current_lsl is None else current_lsl - elapsed
        if self.hardware:
            self.event_module.clearEvents()
        else:
            clear_psychopy_keys(self.event_module)

    def poll(
        self,
        *,
        task_state: str,
        assigned_trial: int | None = None,
        assigned_block: int | None = None,
        assigned_phase: str | None = None,
        premature: bool = False,
        late: bool = False,
    ) -> list[dict[str, Any]]:
        rows = []
        values = (
            poll_hardware_keyboard(self.event_module)
            if self.hardware
            else poll_psychopy_keys(self.event_module, clock=self.clock)
        )
        for value in values:
            self.sequence += 1
            keyboard_time = _optional_float(value.rt)
            timestamp = monotonic() if keyboard_time is None else self.origin_monotonic + keyboard_time
            lsl_timestamp = None if self.origin_lsl is None or keyboard_time is None else self.origin_lsl + keyboard_time
            key = value.name
            row = {
                "schema": KEY_EVENT_SCHEMA,
                "event_id": f"key-{self.sequence:08d}",
                "key": key,
                "timestamp_monotonic": timestamp,
                "timestamp_lsl_if_available": lsl_timestamp,
                "keyboard_time": keyboard_time,
                "keyboard_backend": "psychopy.hardware.keyboard.Keyboard" if self.hardware else "psychopy.event",
                "hardware_timestamped": self.hardware and keyboard_time is not None,
                "task_state": task_state,
                "assigned_trial": assigned_trial,
                "assigned_block": assigned_block,
                "assigned_phase": assigned_phase,
                "is_response_key": key in self.response_keys,
                "is_escape_key": key in self.escape_keys,
                # Kept for backward-readable key ledgers. Trial scoring derives
                # timing classes from the captured key-down timestamp itself.
                "is_premature": premature and key in self.response_keys,
                "is_late": late and key in self.response_keys,
            }
            self.store.append_key_event(row)
            if self.logger is not None:
                self.logger.mark(
                    "dynamic_sart_key_event",
                    event_type="EVENT",
                    timestamp=timestamp,
                    trial=assigned_trial,
                    value=key,
                    **{field: data for field, data in row.items() if field not in {"key", "timestamp_monotonic", "assigned_trial"}},
                )
            rows.append(row)
        if rows and task_state in self._STATIC_CHECKPOINT_STATES:
            # These screens have no stimulus deadline. Persist the exact key
            # receipt before advancing so a later native freeze cannot erase
            # the last successful keyboard operation from the artifacts.
            self.store.checkpoint()
            if self.logger is not None:
                self.logger.flush()
        return rows


def score_dynamic_sart_trial(
    planned: dict[str, Any],
    key_events: Iterable[dict[str, Any]],
    config: DynamicSartConfig,
    *,
    session_id: str,
    participant_id: str,
    task_start_monotonic: float,
    stimulus_onset_monotonic: float,
    stimulus_onset_lsl: float | None,
    stimulus_offset_monotonic: float,
    stimulus_offset_lsl: float | None,
    scheduled_response_window_close_monotonic: float,
    scheduled_response_window_close_lsl: float | None,
    response_window_close_monotonic: float,
    response_window_close_lsl: float | None,
    scheduled_next_trial_onset_monotonic: float | None,
    display_timing: dict[str, Any] | None = None,
    time_since_break_seconds: float | None = None,
    previous_trial_index: int | None = None,
    trials_since_no_go: int | None = None,
    applied_task_actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    events = sorted((dict(row) for row in key_events), key=lambda row: float(row.get("timestamp_monotonic", 0.0)))
    in_window = [
        row
        for row in events
        if stimulus_onset_monotonic
        <= float(row.get("timestamp_monotonic", -math.inf))
        <= scheduled_response_window_close_monotonic
        and not bool(row.get("is_escape_key"))
    ]
    response_events = [row for row in in_window if bool(row.get("is_response_key"))]
    first_response = in_window[0] if in_window else None
    first_response_key = str(first_response.get("key")) if first_response else None
    first_response_timestamp = _optional_float(first_response.get("timestamp_monotonic")) if first_response else None
    first_response_lsl = _optional_float(first_response.get("timestamp_lsl_if_available")) if first_response else None
    valid_response_events = [
        row
        for row in response_events
        if float(row["timestamp_monotonic"]) - stimulus_onset_monotonic >= config.minimum_valid_rt_seconds
    ]
    first_valid = valid_response_events[0] if valid_response_events else None
    first_valid_timestamp = _optional_float(first_valid.get("timestamp_monotonic")) if first_valid else None
    reaction_time = None if first_valid_timestamp is None else first_valid_timestamp - stimulus_onset_monotonic
    has_response = bool(response_events)
    is_no_go = bool(planned["is_no_go"])
    if is_no_go and has_response:
        outcome = "commission_error"
    elif is_no_go:
        outcome = "correct_no_go"
    elif has_response:
        outcome = "correct_go"
    else:
        outcome = "omission_error"
    correct = outcome in {"correct_go", "correct_no_go"}
    too_fast = any(
        float(row["timestamp_monotonic"]) - stimulus_onset_monotonic < config.minimum_valid_rt_seconds
        for row in response_events
    )
    timing = dict(display_timing or {})
    return {
        **deepcopy(planned),
        "schema_version": 3,
        "schema": TRIAL_SCHEMA,
        "task_name": TASK_NAME,
        "task_version": TASK_VERSION,
        "session_id": session_id,
        "participant_id": participant_id,
        "stimulus_marker_label": marker_label("stimulus_onset", planned),
        "stimulus_onset_monotonic": stimulus_onset_monotonic,
        "stimulus_onset_lsl": stimulus_onset_lsl,
        "stimulus_offset_monotonic": stimulus_offset_monotonic,
        "stimulus_offset_lsl": stimulus_offset_lsl,
        "scheduled_response_window_close_monotonic": scheduled_response_window_close_monotonic,
        "scheduled_response_window_close_lsl": scheduled_response_window_close_lsl,
        "response_window_close_monotonic": response_window_close_monotonic,
        "response_window_close_lsl": response_window_close_lsl,
        "response_window_close_overshoot_seconds": max(
            0.0,
            response_window_close_monotonic - scheduled_response_window_close_monotonic,
        ),
        "scheduled_next_trial_onset_monotonic": scheduled_next_trial_onset_monotonic,
        "actual_next_trial_onset_monotonic": None,
        "actual_next_trial_onset_lsl": None,
        # Retained as a backward-readable alias, but never populated with a schedule.
        "next_trial_onset_monotonic": None,
        "actual_stimulus_seconds": stimulus_offset_monotonic - stimulus_onset_monotonic,
        "actual_response_window_seconds": response_window_close_monotonic - stimulus_onset_monotonic,
        "actual_trial_duration_seconds": None,
        "actual_trial_duration_lsl_seconds": None,
        "timing_semantics_version": 2,
        "timing_finalization_status": "awaiting_next_stimulus_flip",
        "display_timing_status": timing.get("status", "modeled"),
        "expected_visual_onset_uncertainty_ms": timing.get("expected_visual_onset_uncertainty_ms"),
        "all_key_event_ids": [row.get("event_id") for row in events],
        "first_response_key": first_response_key,
        "first_response_timestamp_monotonic": first_response_timestamp,
        "first_response_timestamp_lsl": first_response_lsl,
        "first_valid_response_key": str(first_valid.get("key")) if first_valid else None,
        "first_valid_response_timestamp_monotonic": first_valid_timestamp,
        "reaction_time_seconds": reaction_time,
        "response_count": len(in_window),
        "response_key_count": len(response_events),
        "primary_outcome": outcome,
        "correct": correct,
        "commission_error": outcome == "commission_error",
        "omission_error": outcome == "omission_error",
        "premature_response": any(
            bool(row.get("is_response_key"))
            and float(row.get("timestamp_monotonic", math.inf)) < stimulus_onset_monotonic
            for row in events
        ),
        "too_fast_response": too_fast,
        "multiple_response": len(response_events) > 1,
        "wrong_key_response": any(not bool(row.get("is_response_key")) for row in in_window),
        "late_response": any(
            bool(row.get("is_response_key"))
            and float(row.get("timestamp_monotonic", -math.inf)) > scheduled_response_window_close_monotonic
            for row in events
        ),
        "aborted": False,
        "invalid": False,
        "time_on_task_seconds": stimulus_onset_monotonic - task_start_monotonic,
        "time_since_break_seconds": time_since_break_seconds,
        "previous_trial_index": previous_trial_index,
        "trials_since_no_go": trials_since_no_go,
        "probe_id": planned.get("probe_id"),
        "probe_proximity": planned.get("probe_proximity"),
        "applied_task_actions": list(applied_task_actions or []),
        "eeg_quality_join_key": f"dynamic_sart:{planned['global_trial_index']}",
        "presented": True,
    }


def practice_criteria(
    records: Iterable[dict[str, Any]],
    config: DynamicSartConfig,
    *,
    comprehension_confirmed: bool,
    planned_no_go_trials: int | None = None,
) -> dict[str, Any]:
    rows = list(records)
    go = [row for row in rows if row.get("condition") == "go"]
    no_go = [row for row in rows if row.get("condition") == "no_go"]
    correct_go_count = sum(int(bool(row.get("correct"))) for row in go)
    correct_no_go_count = sum(int(bool(row.get("correct"))) for row in no_go)
    go_accuracy = correct_go_count / len(go) if go else 0.0
    no_go_accuracy = correct_no_go_count / len(no_go) if no_go else 0.0
    required_correct_go_count = _required_correct_count(config.practice_go_accuracy, len(go))
    planned_no_go_count = max(
        len(no_go),
        int(config.practice_no_go_trials if planned_no_go_trials is None else planned_no_go_trials),
    )
    required_correct_no_go_count = _required_correct_count(
        config.practice_no_go_accuracy,
        planned_no_go_count,
    )
    anticipatory_rate = sum(int(bool(row.get("premature_response"))) for row in rows) / len(rows) if rows else 1.0
    passed = (
        correct_go_count >= required_correct_go_count
        and correct_no_go_count >= required_correct_no_go_count
        and len(no_go) >= planned_no_go_count
        and anticipatory_rate <= config.practice_max_anticipatory_rate
        and comprehension_confirmed
    )
    return {
        "passed": passed,
        "go_accuracy": go_accuracy,
        "no_go_accuracy": no_go_accuracy,
        "go_correct_count": correct_go_count,
        "go_trial_count": len(go),
        "go_required_correct_count": required_correct_go_count,
        "no_go_correct_count": correct_no_go_count,
        "no_go_trial_count": len(no_go),
        "planned_no_go_trial_count": planned_no_go_count,
        "no_go_required_correct_count": required_correct_no_go_count,
        "anticipatory_response_rate": anticipatory_rate,
        "maximum_anticipatory_response_rate": config.practice_max_anticipatory_rate,
        "response_rule_comprehension_confirmed": comprehension_confirmed,
    }


def practice_no_go_target_status(
    records: Iterable[dict[str, Any]],
    remaining_trials: Iterable[dict[str, Any]],
    config: DynamicSartConfig,
    *,
    planned_no_go_trials: int | None = None,
) -> dict[str, Any]:
    """Report whether the no-go practice target remains mathematically reachable."""

    rows = list(records)
    remaining = list(remaining_trials)
    presented_no_go = [row for row in rows if row.get("condition") == "no_go"]
    remaining_no_go_count = sum(
        int(bool(row.get("is_no_go")) or row.get("condition") == "no_go")
        for row in remaining
    )
    total_no_go_count = max(
        len(presented_no_go) + remaining_no_go_count,
        int(config.practice_no_go_trials if planned_no_go_trials is None else planned_no_go_trials),
    )
    required_correct_count = _required_correct_count(
        config.practice_no_go_accuracy,
        total_no_go_count,
    )
    correct_count = sum(int(bool(row.get("correct"))) for row in presented_no_go)
    maximum_possible_correct_count = correct_count + remaining_no_go_count
    return {
        "reachable": maximum_possible_correct_count >= required_correct_count,
        "correct_no_go_count": correct_count,
        "presented_no_go_count": len(presented_no_go),
        "remaining_no_go_count": remaining_no_go_count,
        "planned_no_go_count": total_no_go_count,
        "required_correct_no_go_count": required_correct_count,
        "maximum_possible_correct_no_go_count": maximum_possible_correct_count,
    }


def _required_correct_count(threshold: float, trial_count: int) -> int:
    if trial_count <= 0:
        return 0
    return min(trial_count, int(math.ceil(float(threshold) * trial_count - 1e-9)))


def _practice_status_text(
    criteria: dict[str, Any],
    config: DynamicSartConfig,
    *,
    will_repeat: bool,
) -> str:
    heading = (
        "Practice will repeat."
        if will_repeat
        else "Practice did not meet the target criteria."
    )
    next_step = (
        f"Remember: press SPACE for every digit except {config.no_go_digit}.\n\nPress SPACE to repeat practice."
        if will_repeat
        else (
            "Choose the next step:\n\n"
            "1 — Retry practice\n"
            "2 — Proceed to the main SART task\n\n"
            "Press ESCAPE only if you need to stop the task."
        )
    )
    early_stop = (
        "This practice round ended early because the no-go target could no longer be reached.\n\n"
        if bool(criteria.get("ended_early"))
        else ""
    )
    planned_no_go_count = int(
        criteria.get("planned_no_go_trial_count", criteria.get("no_go_trial_count", 0))
    )
    return (
        f"{heading}\n\n{early_stop}"
        f"Go responses: {int(criteria.get('go_correct_count', 0))}/{int(criteria.get('go_trial_count', 0))} correct "
        f"(need {int(criteria.get('go_required_correct_count', 0))}).\n"
        f"No-go withholding: {int(criteria.get('no_go_correct_count', 0))}/{int(criteria.get('no_go_trial_count', 0))} correct so far "
        f"(need {int(criteria.get('no_go_required_correct_count', 0))} of {planned_no_go_count} planned).\n"
        f"Premature-response rate: {float(criteria.get('anticipatory_response_rate', 0.0)):.1%} "
        f"(maximum {float(criteria.get('maximum_anticipatory_response_rate', 0.0)):.1%}).\n\n"
        f"{next_step}"
    )


def _show_practice_failure_choice(
    win: Any,
    visual: Any,
    keyboard: "PersistentKeyboardCollector",
    criteria: dict[str, Any],
    config: DynamicSartConfig,
    round_index: int,
) -> str:
    """Offer retry/proceed after the second and every later failed round."""

    prompt = visual.TextStim(
        win,
        text=(
            f"Practice round {round_index} did not meet the target criteria.\n\n"
            + _practice_status_text(criteria, config, will_repeat=False)
        ),
        height=0.045,
        color="white",
        wrapWidth=1.5,
    )
    prompt.draw()
    win.flip()
    retry_keys = {"1", "num_1", "num1", "r"}
    proceed_keys = {"2", "num_2", "num2", "p"}
    while True:
        service_psychopy_static_window(win, prompt)
        rows = keyboard.poll(task_state="PRACTICE_DECISION")
        if any(row["is_escape_key"] for row in rows):
            return "abort"
        keys = {str(row.get("key") or "").strip().lower() for row in rows}
        if keys & retry_keys:
            return "retry"
        if keys & proceed_keys:
            return "proceed"
        sleep(0.01)


def _show_practice_ready(
    win: Any,
    visual: Any,
    keyboard: "PersistentKeyboardCollector",
    config: DynamicSartConfig,
    *,
    passed: bool = True,
) -> bool:
    return _show_screen(
        win,
        visual,
        keyboard,
        (
            (
                "Practice complete.\n\n"
                if passed
                else "Practice section complete. You chose to proceed.\n\n"
            )
            + "The main task will begin after a short countdown.\n\n"
            "Press SPACE when you are ready to begin."
        ),
        state="PRACTICE_READY",
        allowed_continue=config.response_keys,
    )


def audit_dynamic_sart_action(action: TaskAction) -> dict[str, Any]:
    accepted = action.action == "observe_only"
    return {
        "status": "accepted" if accepted else "rejected",
        "reason": "observe-only action audited without task change" if accepted else "dynamic_sart rejects task-changing actions",
        "action": action.to_payload(),
        "applied": False,
    }


def _make_task_feedback_client(config: dict[str, Any], paths: SessionPaths) -> TaskFeedbackClient:
    feedback_config = dict(config.get("realtime", {}).get("feedback", {}) or {})
    client_config = dict(feedback_config.get("client", {}) or {})
    client_config.setdefault("enabled", False)
    client_config.setdefault("backend", feedback_config.get("emitter", "disabled"))
    return TaskFeedbackClient(client_config, default_jsonl_path=paths.realtime_feedback_jsonl)


def _poll_and_audit_dynamic_sart_feedback(
    client: TaskFeedbackClient,
    logger: EventLogger,
    *,
    trial_index: int,
    block_index: int,
) -> list[dict[str, Any]]:
    actions = client.poll("between_trials", trial_index, block_index)
    for record in client.audit_records:
        if record.status in {"pending", "accepted"}:
            continue
        logger.mark(
            f"dynamic_sart_feedback_action_{record.status}",
            event_type="SYSTEM",
            trial=trial_index,
            value=record.reason,
            audit=record.to_payload(),
            task=TASK_NAME,
        )
    audits = []
    for action in actions:
        audit = audit_dynamic_sart_action(action)
        audits.append(audit)
        logger.mark(
            f"dynamic_sart_feedback_action_{audit['status']}",
            event_type="SYSTEM",
            trial=trial_index,
            value=action.action,
            audit=audit,
            task=TASK_NAME,
        )
    return audits


def summarize_dynamic_sart(
    records: Iterable[dict[str, Any]],
    *,
    aborted: bool,
    abort_reason: str | None,
    support_complete: bool,
    planned_experimental_trials: int,
) -> dict[str, Any]:
    rows = list(records)
    experimental = [row for row in rows if not bool(row.get("is_practice"))]
    completed_experimental = [
        row
        for row in experimental
        if not bool(row.get("aborted")) and not bool(row.get("invalid"))
    ]
    practice = [row for row in rows if bool(row.get("is_practice"))]
    go = [row for row in completed_experimental if row.get("condition") == "go"]
    no_go = [row for row in completed_experimental if row.get("condition") == "no_go"]
    valid_rts = [float(row["reaction_time_seconds"]) for row in go if row.get("reaction_time_seconds") is not None and not row.get("too_fast_response")]
    return {
        "schema": SUMMARY_SCHEMA,
        "task": TASK_NAME,
        "task_version": TASK_VERSION,
        "planned_experimental_trials": planned_experimental_trials,
        "experimental_trials": len(completed_experimental),
        "presented_experimental_trials": len(experimental),
        "aborted_experimental_trials": len(experimental) - len(completed_experimental),
        "practice_trials": len(practice),
        "support_trials": sum(int(row.get("phase") == "support") for row in completed_experimental),
        "query_trials": sum(int(row.get("phase") == "query") for row in completed_experimental),
        "support_complete": support_complete,
        "accuracy": (
            sum(int(bool(row.get("correct"))) for row in completed_experimental)
            / len(completed_experimental)
            if completed_experimental
            else 0.0
        ),
        "go_accuracy": sum(int(bool(row.get("correct"))) for row in go) / len(go) if go else None,
        "no_go_accuracy": sum(int(bool(row.get("correct"))) for row in no_go) / len(no_go) if no_go else None,
        "valid_go_rt_count": len(valid_rts),
        "mean_valid_go_rt_seconds": sum(valid_rts) / len(valid_rts) if valid_rts else None,
        "median_valid_go_rt_seconds": median(valid_rts) if valid_rts else None,
        "go_trial_count": len(go),
        "no_go_trial_count": len(no_go),
        "commission_errors": sum(int(bool(row.get("commission_error"))) for row in experimental),
        "omission_errors": sum(int(bool(row.get("omission_error"))) for row in experimental),
        "premature_responses": sum(int(bool(row.get("premature_response"))) for row in experimental),
        "too_fast_responses": sum(int(bool(row.get("too_fast_response"))) for row in experimental),
        "multiple_responses": sum(int(bool(row.get("multiple_response"))) for row in experimental),
        "wrong_key_responses": sum(int(bool(row.get("wrong_key_response"))) for row in experimental),
        "aborted": aborted,
        "abort_reason": abort_reason,
        "partial_run": aborted or len(completed_experimental) != planned_experimental_trials,
    }


def _simulate_trial(
    planned: dict[str, Any],
    config: DynamicSartConfig,
    store: DynamicSartArtifactStore,
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    task_start: float,
    onset: float,
    *,
    plan_trial_count: int,
    force_practice_correct: bool = False,
    applied_task_actions: list[dict[str, Any]] | None = None,
    last_break_monotonic: float | None = None,
) -> tuple[dict[str, Any], float]:
    stimulus_offset = onset + config.stimulus_seconds
    response_close = onset + config.response_window_seconds
    next_onset = onset + float(planned["planned_soi_seconds"])
    rng = random.Random(int(planned["block_seed"]) ^ abs(int(planned["global_trial_index"])) * 7919)
    events: list[dict[str, Any]] = []
    response_specs: list[tuple[str, float]] = []
    if not force_practice_correct and rng.random() < 0.02:
        events.append(_sim_key_event(store, logger, planned, "space", onset - 0.04, premature=True))
    _emit_planned_cue(logger, marker_outlet, planned, timestamp=onset)
    onset_label = marker_label("stimulus_onset", planned)
    _emit(logger, marker_outlet, onset_label, timestamp=onset, trial=int(planned["global_trial_index"]), **_marker_metadata(planned))
    drift = max(0.0, int(planned.get("global_trial_index", 0))) / max(1, plan_trial_count)
    state_shift = 0.08 if drift >= 0.65 else 0.0
    if planned["is_no_go"]:
        commission = False if force_practice_correct else rng.random() < 0.10 + 0.08 * drift
        if commission:
            rt = max(0.05, min(config.response_window_seconds - 0.02, rng.gauss(0.34 + state_shift, 0.07)))
            response_specs.append(("space", onset + rt))
    else:
        omission = False if force_practice_correct else rng.random() < 0.02 + 0.04 * drift
        if not omission:
            rt = max(0.04, min(config.response_window_seconds - 0.02, rng.gauss(0.38 + 0.12 * drift + state_shift, 0.06)))
            if not force_practice_correct and rng.random() < 0.015:
                rt = config.minimum_valid_rt_seconds * 0.65
            if not force_practice_correct and rng.random() < 0.015:
                response_specs.append(("x", onset + max(0.02, rt - 0.04)))
            response_specs.append(("space", onset + rt))
            if not force_practice_correct and rng.random() < 0.04:
                second = min(response_close - 0.005, onset + rt + rng.uniform(0.04, 0.15))
                response_specs.append(("space", second))
    response_specs.sort(key=lambda item: item[1])
    before_offset = [item for item in response_specs if item[1] <= stimulus_offset]
    after_offset = [item for item in response_specs if item[1] > stimulus_offset]
    events.extend(_sim_key_event(store, logger, planned, key, timestamp) for key, timestamp in before_offset)
    _emit(
        logger,
        marker_outlet,
        marker_label("stimulus_offset", planned),
        timestamp=stimulus_offset,
        trial=int(planned["global_trial_index"]),
        **_marker_metadata(planned),
    )
    events.extend(_sim_key_event(store, logger, planned, key, timestamp) for key, timestamp in after_offset)
    _emit(
        logger,
        marker_outlet,
        marker_label("response_window_close", planned),
        timestamp=response_close,
        trial=int(planned["global_trial_index"]),
        **_marker_metadata(planned),
    )
    previous_experimental = max((int(row["global_trial_index"]) for row in store.records if not row.get("is_practice")), default=None)
    record = score_dynamic_sart_trial(
        planned,
        events,
        config,
        session_id=store.paths.root.name,
        participant_id=store.participant_id,
        task_start_monotonic=task_start,
        stimulus_onset_monotonic=onset,
        stimulus_onset_lsl=None,
        stimulus_offset_monotonic=stimulus_offset,
        stimulus_offset_lsl=None,
        scheduled_response_window_close_monotonic=response_close,
        scheduled_response_window_close_lsl=None,
        response_window_close_monotonic=response_close,
        response_window_close_lsl=None,
        scheduled_next_trial_onset_monotonic=next_onset,
        display_timing={"status": "virtual", "expected_visual_onset_uncertainty_ms": 0.0},
        time_since_break_seconds=None if last_break_monotonic is None else onset - last_break_monotonic,
        previous_trial_index=previous_experimental,
        trials_since_no_go=_trials_since_no_go(store.records),
        applied_task_actions=applied_task_actions,
    )
    store.append_trial(record)
    _emit(
        logger,
        marker_outlet,
        marker_label("trial_complete", planned),
        timestamp=response_close,
        trial=int(planned["global_trial_index"]),
        primary_outcome=record["primary_outcome"],
        correct=record["correct"],
        reaction_time_seconds=record["reaction_time_seconds"],
        **_marker_metadata(planned),
    )
    return record, next_onset


def _sim_key_event(
    store: DynamicSartArtifactStore,
    logger: EventLogger,
    planned: dict[str, Any],
    key: str,
    timestamp: float,
    *,
    premature: bool = False,
) -> dict[str, Any]:
    event_id = f"key-{len(store.key_events) + 1:08d}"
    row = {
        "schema": KEY_EVENT_SCHEMA,
        "event_id": event_id,
        "key": key,
        "timestamp_monotonic": timestamp,
        "timestamp_lsl_if_available": None,
        "keyboard_time": None,
        "task_state": "INTERTRIAL_INTERVAL" if premature else "STIMULUS_VISIBLE",
        "assigned_trial": int(planned["global_trial_index"]),
        "assigned_block": int(planned["block_index"]),
        "assigned_phase": planned["phase"],
        "is_response_key": key in store.config.response_keys,
        "is_escape_key": key in store.config.escape_keys,
        "is_premature": premature,
        "is_late": False,
    }
    store.append_key_event(row)
    logger.mark(
        "dynamic_sart_key_event",
        event_type="EVENT",
        timestamp=timestamp,
        trial=int(planned["global_trial_index"]),
        value=key,
        event_id=event_id,
        task_state=row["task_state"],
        phase=planned["phase"],
        practice=bool(planned["is_practice"]),
        is_response_key=row["is_response_key"],
        is_premature=premature,
    )
    return row


def _record_dry_probe(
    store: DynamicSartArtifactStore,
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    anchor: dict[str, Any],
    config: DynamicSartConfig,
    onset: float,
) -> float:
    probe_id = str(anchor["probe_id"])
    response_timestamp = onset + min(0.2, config.thought_probe_max_seconds)
    selected_key = "1"
    selected_response = config.thought_probe_choices[0]
    _emit(
        logger,
        marker_outlet,
        marker_label("probe_onset", v=1, probe=probe_id),
        timestamp=onset,
        event_type="SYSTEM",
        trial=int(anchor["global_trial_index"]),
        probe_id=probe_id,
    )
    event = {
        "schema": KEY_EVENT_SCHEMA,
        "event_id": f"key-{len(store.key_events) + 1:08d}",
        "key": selected_key,
        "timestamp_monotonic": response_timestamp,
        "timestamp_lsl_if_available": None,
        "keyboard_time": None,
        "task_state": "PROBE",
        "assigned_trial": None,
        "assigned_block": int(anchor["block_index"]),
        "assigned_phase": str(anchor["phase"]),
        "is_response_key": False,
        "is_escape_key": False,
        "is_premature": False,
        "is_late": False,
    }
    store.append_key_event(event)
    logger.mark(
        "dynamic_sart_key_event",
        timestamp=response_timestamp,
        value=selected_key,
        event_id=event["event_id"],
        task_state="PROBE",
        task=TASK_NAME,
    )
    _emit(
        logger,
        marker_outlet,
        marker_label("probe_response", v=1, probe=probe_id),
        timestamp=response_timestamp,
        event_type="SYSTEM",
        trial=int(anchor["global_trial_index"]),
        value=selected_response,
        probe_id=probe_id,
    )
    store.append_probe(
        _probe_record(
            anchor,
            config,
            onset=onset,
            offset=response_timestamp,
            selected_key=selected_key,
            selected_response=selected_response,
        )
    )
    return response_timestamp


def _present_psychopy_probe(
    win: Any,
    visual: Any,
    keyboard: PersistentKeyboardCollector,
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    store: DynamicSartArtifactStore,
    anchor: dict[str, Any],
    config: DynamicSartConfig,
) -> bool:
    choices = list(config.thought_probe_choices)
    choice_lines = "\n".join(f"{index}. {choice.replace('_', ' ')}" for index, choice in enumerate(choices, start=1))
    prompt = visual.TextStim(
        win,
        text=f"{config.thought_probe_question}\n\n{choice_lines}",
        height=0.045,
        color="white",
        wrapWidth=1.5,
    )
    probe_id = str(anchor["probe_id"])
    holder: dict[str, Any] = {}
    prompt.draw()
    win.callOnFlip(
        _capture_probe_onset,
        holder,
        logger,
        marker_outlet,
        probe_id,
        int(anchor["global_trial_index"]),
    )
    win.flip()
    onset = float(holder["onset"])
    deadline = onset + config.thought_probe_max_seconds
    selected_key = None
    selected_response = None
    while monotonic() < deadline:
        service_psychopy_static_window(win, prompt)
        rows = keyboard.poll(
            task_state="PROBE",
            assigned_block=int(anchor["block_index"]),
            assigned_phase=str(anchor["phase"]),
        )
        if any(row["is_escape_key"] for row in rows):
            return False
        for row in rows:
            key = str(row["key"])
            if key.isdigit() and 1 <= int(key) <= len(choices):
                selected_key = key
                selected_response = choices[int(key) - 1]
                break
        if selected_response is not None:
            break
        sleep(0.005)
    offset = monotonic()
    _emit(
        logger,
        marker_outlet,
        marker_label("probe_response", v=1, probe=probe_id),
        event_type="SYSTEM",
        timestamp=offset,
        trial=int(anchor["global_trial_index"]),
        value=selected_response or "no_response",
        probe_id=probe_id,
    )
    store.append_probe(
        _probe_record(
            anchor,
            config,
            onset=onset,
            offset=offset,
            selected_key=selected_key,
            selected_response=selected_response,
        )
    )
    return True


def _capture_probe_onset(
    holder: dict[str, Any],
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    probe_id: str,
    trial_index: int,
) -> None:
    onset = monotonic()
    holder["onset"] = onset
    _emit(
        logger,
        marker_outlet,
        marker_label("probe_onset", v=1, probe=probe_id),
        event_type="SYSTEM",
        timestamp=onset,
        trial=trial_index,
        probe_id=probe_id,
        scheduled_on_flip=True,
    )


def _probe_record(
    anchor: dict[str, Any],
    config: DynamicSartConfig,
    *,
    onset: float,
    offset: float,
    selected_key: str | None,
    selected_response: str | None,
) -> dict[str, Any]:
    trial_index = int(anchor["global_trial_index"])
    window = config.thought_probe_exclusion_window_trials
    return {
        "schema": PROBE_SCHEMA,
        "probe_id": anchor.get("probe_id"),
        "after_trial_index": trial_index,
        "block_index": int(anchor["block_index"]),
        "phase": anchor.get("phase"),
        "question": config.thought_probe_question,
        "response_choices": list(config.thought_probe_choices),
        "onset_monotonic": onset,
        "offset_monotonic": offset,
        "selected_key": selected_key,
        "selected_response": selected_response,
        "response_time_seconds": None if selected_response is None else offset - onset,
        "neighboring_trials": list(range(max(1, trial_index - window), trial_index + window + 1)),
        "exclusion_window_trials": window,
    }


def _present_psychopy_trial(
    win: Any,
    visual: Any,
    keyboard: PersistentKeyboardCollector,
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    store: DynamicSartArtifactStore,
    planned: dict[str, Any],
    config: DynamicSartConfig,
    timing: dict[str, Any],
    task_start: float,
    *,
    premature_events: list[dict[str, Any]],
    next_trial: dict[str, Any] | None,
    preflipped_onset: dict[str, Any] | None = None,
    digit_stimulus: Any | None = None,
    fixation_stimulus: Any | None = None,
    applied_task_actions: list[dict[str, Any]] | None = None,
    last_break_monotonic: float | None = None,
) -> tuple[dict[str, Any] | None, bool, list[dict[str, Any]], dict[str, Any] | None]:
    digit = digit_stimulus or visual.TextStim(win, text="", height=0.22, color="white")
    fixation = fixation_stimulus or visual.TextStim(win, text="+", height=0.08, color="white")
    holder: dict[str, Any] = preflipped_onset or {}
    if preflipped_onset is None:
        _set_stimulus_text(digit, str(planned["digit"]))
        _emit_planned_cue(logger, marker_outlet, planned)
        digit.draw()
        win.callOnFlip(
            _capture_flip_event,
            holder,
            marker_outlet,
            marker_label("stimulus_onset", planned),
            planned,
            timing,
            "onset",
        )
        win.flip()
        _log_captured_flip_event(holder, logger, "onset")
    onset = float(holder["onset_monotonic"])
    onset_lsl = _optional_float(holder.get("onset_lsl"))
    onset_flip = float(holder["onset_event"]["flip_monotonic"])
    nominal_rate = float(timing.get("nominal_refresh_rate_hz") or 60.0)
    stimulus_frames = int(timing.get("stimulus_frame_count") or round(config.stimulus_seconds * nominal_rate))
    soi_frames = int(timing.get("soi_frame_count") or round(config.response_window_seconds * nominal_rate))
    frame_period = 1.0 / nominal_rate
    flip_request_lead = frame_period * 0.75
    scheduled_offset_flip = onset_flip + (stimulus_frames * frame_period)
    scheduled_boundary_flip = onset_flip + (soi_frames * frame_period)
    scheduled_response_close = onset + (soi_frames * frame_period)
    scheduled_response_close_lsl = (
        None if onset_lsl is None else onset_lsl + (soi_frames * frame_period)
    )
    events = list(premature_events)
    if any(row["is_escape_key"] for row in events):
        record = _append_aborted_psychopy_trial(
            logger,
            marker_outlet,
            store,
            planned,
            config,
            timing,
            task_start,
            holder,
            events,
            abort_stage="preflipped_stimulus_onset",
            scheduled_response_close=scheduled_response_close,
            scheduled_response_close_lsl=scheduled_response_close_lsl,
            applied_task_actions=applied_task_actions,
            last_break_monotonic=last_break_monotonic,
        )
        return record, True, [], None
    while monotonic() < scheduled_offset_flip - flip_request_lead:
        redraw_psychopy_after_resize(win, digit)
        polled = keyboard.poll(
            task_state="STIMULUS_VISIBLE",
            assigned_trial=int(planned["global_trial_index"]),
            assigned_block=int(planned["block_index"]),
            assigned_phase=str(planned["phase"]),
        )
        events.extend(polled)
        if any(row["is_escape_key"] for row in polled):
            record = _append_aborted_psychopy_trial(
                logger,
                marker_outlet,
                store,
                planned,
                config,
                timing,
                task_start,
                holder,
                events,
                abort_stage="stimulus_visible",
                scheduled_response_close=scheduled_response_close,
                scheduled_response_close_lsl=scheduled_response_close_lsl,
                applied_task_actions=applied_task_actions,
                last_break_monotonic=last_break_monotonic,
            )
            return record, True, [], None
        sleep(0.002)
    fixation.draw()
    win.callOnFlip(_capture_flip_event, holder, marker_outlet, marker_label("stimulus_offset", planned), planned, timing, "offset")
    win.flip()
    _log_captured_flip_event(holder, logger, "offset")
    if next_trial is not None:
        _set_stimulus_text(digit, str(next_trial["digit"]))
        _emit_planned_cue(logger, marker_outlet, next_trial)
    while monotonic() < scheduled_boundary_flip - flip_request_lead:
        redraw_psychopy_after_resize(win, fixation)
        polled = keyboard.poll(
            task_state="RESPONSE_WINDOW_MASK",
            assigned_trial=int(planned["global_trial_index"]),
            assigned_block=int(planned["block_index"]),
            assigned_phase=str(planned["phase"]),
        )
        events.extend(polled)
        if any(row["is_escape_key"] for row in polled):
            record = _append_aborted_psychopy_trial(
                logger,
                marker_outlet,
                store,
                planned,
                config,
                timing,
                task_start,
                holder,
                events,
                abort_stage="response_window",
                scheduled_response_close=scheduled_response_close,
                scheduled_response_close_lsl=scheduled_response_close_lsl,
                applied_task_actions=applied_task_actions,
                last_break_monotonic=last_break_monotonic,
            )
            return record, True, [], None
        sleep(0.002)
    boundary: dict[str, Any] = {}
    (digit if next_trial is not None else fixation).draw()
    win.callOnFlip(
        _capture_response_close_event,
        boundary,
        marker_outlet,
        marker_label("response_window_close", planned),
        planned,
        timing,
        scheduled_response_close,
        scheduled_response_close_lsl,
    )
    next_holder: dict[str, Any] | None = None
    if next_trial is not None:
        next_holder = {}
        win.callOnFlip(
            _capture_flip_event,
            next_holder,
            marker_outlet,
            marker_label("stimulus_onset", next_trial),
            next_trial,
            timing,
            "onset",
        )
    win.flip()
    _log_captured_response_close_event(boundary, logger)
    if next_holder is not None:
        _log_captured_flip_event(next_holder, logger, "onset")
    response_close = float(boundary["response_close_monotonic"])
    response_close_lsl = _optional_float(boundary.get("response_close_lsl"))

    boundary_rows = keyboard.poll(
        task_state="STIMULUS_VISIBLE" if next_trial is not None else "RESPONSE_WINDOW_CLOSE",
        assigned_trial=(
            int(next_trial["global_trial_index"])
            if next_trial is not None
            else int(planned["global_trial_index"])
        ),
        assigned_block=(
            int(next_trial["block_index"])
            if next_trial is not None
            else int(planned["block_index"])
        ),
        assigned_phase=(
            str(next_trial["phase"])
            if next_trial is not None
            else str(planned["phase"])
        ),
    )
    next_onset = None if next_holder is None else float(next_holder["onset_monotonic"])
    upcoming = []
    for row in boundary_rows:
        if next_onset is not None and float(row["timestamp_monotonic"]) >= next_onset:
            upcoming.append(row)
        else:
            events.append(row)
    if any(row["is_escape_key"] for row in events):
        record = _append_aborted_psychopy_trial(
            logger,
            marker_outlet,
            store,
            planned,
            config,
            timing,
            task_start,
            holder,
            events,
            abort_stage="response_window_boundary",
            scheduled_response_close=scheduled_response_close,
            scheduled_response_close_lsl=scheduled_response_close_lsl,
            applied_task_actions=applied_task_actions,
            last_break_monotonic=last_break_monotonic,
        )
        return record, True, upcoming, next_holder
    previous_experimental = max((int(row["global_trial_index"]) for row in store.records if not row.get("is_practice")), default=None)
    record = score_dynamic_sart_trial(
        planned,
        events,
        config,
        session_id=store.paths.root.name,
        participant_id=store.participant_id,
        task_start_monotonic=task_start,
        stimulus_onset_monotonic=onset,
        stimulus_onset_lsl=onset_lsl,
        stimulus_offset_monotonic=float(holder["offset_monotonic"]),
        stimulus_offset_lsl=_optional_float(holder.get("offset_lsl")),
        scheduled_response_window_close_monotonic=scheduled_response_close,
        scheduled_response_window_close_lsl=scheduled_response_close_lsl,
        response_window_close_monotonic=response_close,
        response_window_close_lsl=response_close_lsl,
        scheduled_next_trial_onset_monotonic=onset + float(planned["planned_soi_seconds"]),
        display_timing=timing,
        time_since_break_seconds=None if last_break_monotonic is None else onset - last_break_monotonic,
        previous_trial_index=previous_experimental,
        trials_since_no_go=_trials_since_no_go(store.records),
        applied_task_actions=applied_task_actions,
    )
    if next_holder is not None:
        record["actual_next_trial_onset_monotonic"] = next_onset
        record["actual_next_trial_onset_lsl"] = _optional_float(next_holder.get("onset_lsl"))
        record["next_trial_onset_monotonic"] = next_onset
        record["actual_trial_duration_seconds"] = (
            None if next_onset is None else next_onset - onset
        )
        next_lsl = _optional_float(next_holder.get("onset_lsl"))
        record["actual_trial_duration_lsl_seconds"] = (
            None if next_lsl is None or onset_lsl is None else next_lsl - onset_lsl
        )
        record["timing_finalization_status"] = "observed_next_stimulus_flip"
    store.append_trial(record)
    _emit(
        logger,
        marker_outlet,
        marker_label("trial_complete", planned),
        timestamp=response_close,
        lsl_timestamp=response_close_lsl,
        trial=int(planned["global_trial_index"]),
        primary_outcome=record["primary_outcome"],
        correct=record["correct"],
        reaction_time_seconds=record["reaction_time_seconds"],
        **_marker_metadata(planned),
    )
    return record, False, upcoming, next_holder


def _append_aborted_psychopy_trial(
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    store: DynamicSartArtifactStore,
    planned: dict[str, Any],
    config: DynamicSartConfig,
    timing: dict[str, Any],
    task_start: float,
    holder: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    abort_stage: str,
    scheduled_response_close: float,
    scheduled_response_close_lsl: float | None,
    applied_task_actions: list[dict[str, Any]] | None,
    last_break_monotonic: float | None,
) -> dict[str, Any]:
    """Persist a presented Escape-interrupted trial without scoring it as behavior."""

    escape = next(row for row in events if bool(row.get("is_escape_key")))
    abort_monotonic = float(escape["timestamp_monotonic"])
    abort_lsl = _optional_float(escape.get("timestamp_lsl_if_available"))
    onset = float(holder["onset_monotonic"])
    onset_lsl = _optional_float(holder.get("onset_lsl"))
    offset = _optional_float(holder.get("offset_monotonic"))
    offset_lsl = _optional_float(holder.get("offset_lsl"))
    previous_experimental = max(
        (
            int(row["global_trial_index"])
            for row in store.records
            if not row.get("is_practice")
        ),
        default=None,
    )
    record = score_dynamic_sart_trial(
        planned,
        events,
        config,
        session_id=store.paths.root.name,
        participant_id=store.participant_id,
        task_start_monotonic=task_start,
        stimulus_onset_monotonic=onset,
        stimulus_onset_lsl=onset_lsl,
        stimulus_offset_monotonic=offset if offset is not None else abort_monotonic,
        stimulus_offset_lsl=offset_lsl,
        scheduled_response_window_close_monotonic=scheduled_response_close,
        scheduled_response_window_close_lsl=scheduled_response_close_lsl,
        response_window_close_monotonic=abort_monotonic,
        response_window_close_lsl=abort_lsl,
        scheduled_next_trial_onset_monotonic=onset + float(planned["planned_soi_seconds"]),
        display_timing=timing,
        time_since_break_seconds=(
            None if last_break_monotonic is None else onset - last_break_monotonic
        ),
        previous_trial_index=previous_experimental,
        trials_since_no_go=_trials_since_no_go(store.records),
        applied_task_actions=applied_task_actions,
    )
    record.update(
        {
            "stimulus_offset_monotonic": offset,
            "stimulus_offset_lsl": offset_lsl,
            "response_window_close_monotonic": None,
            "response_window_close_lsl": None,
            "response_window_close_overshoot_seconds": None,
            "actual_stimulus_seconds": None if offset is None else offset - onset,
            "actual_response_window_seconds": None,
            "timing_finalization_status": f"aborted_{abort_stage}",
            "primary_outcome": "aborted",
            "correct": False,
            "commission_error": False,
            "omission_error": False,
            "aborted": True,
            "invalid": True,
            "abort_reason": "escape_abort",
            "abort_stage": abort_stage,
        }
    )
    store.append_trial(record)
    _emit(
        logger,
        marker_outlet,
        marker_label("trial_aborted", planned),
        timestamp=abort_monotonic,
        lsl_timestamp=abort_lsl,
        trial=int(planned["global_trial_index"]),
        reason="escape_abort",
        abort_stage=abort_stage,
        **_marker_metadata(planned),
    )
    return record


def _set_stimulus_text(stimulus: Any, value: str) -> None:
    setter = getattr(stimulus, "setText", None)
    if callable(setter):
        setter(value)
    else:
        stimulus.text = value


def _observed_stimulus_duration(row: dict[str, Any]) -> tuple[float | None, str]:
    onset_lsl = _optional_float(row.get("stimulus_onset_lsl"))
    offset_lsl = _optional_float(row.get("stimulus_offset_lsl"))
    if onset_lsl is not None and offset_lsl is not None:
        return offset_lsl - onset_lsl, "lsl_flip"
    return _optional_float(row.get("actual_stimulus_seconds")), "high_resolution_monotonic"


def _observed_soi_duration(row: dict[str, Any]) -> tuple[float | None, str]:
    lsl_duration = _optional_float(row.get("actual_trial_duration_lsl_seconds"))
    if lsl_duration is not None:
        return lsl_duration, "lsl_flip"
    return _optional_float(row.get("actual_trial_duration_seconds")), "high_resolution_monotonic"


def _block_timing_warning(
    records: Iterable[dict[str, Any]],
    timing: dict[str, Any],
) -> dict[str, Any] | None:
    frame_seconds = float(timing.get("expected_frame_interval_ms") or 0.0) / 1000.0
    if frame_seconds <= 0.0:
        return None
    threshold = frame_seconds * 1.5
    rows = list(records)
    stimulus_observations = [_observed_stimulus_duration(row) for row in rows]
    soi_observations = [_observed_soi_duration(row) for row in rows]
    stimulus_errors = [
        abs(actual - float(row.get("planned_stimulus_seconds") or 0.0))
        for row, (actual, _source) in zip(rows, stimulus_observations)
        if actual is not None
    ]
    soi_errors = [
        abs(actual - float(row.get("planned_soi_seconds") or 0.0))
        for row, (actual, _source) in zip(rows, soi_observations)
        if actual is not None
    ]
    affected = sum(error > threshold for error in stimulus_errors) + sum(
        error > threshold for error in soi_errors
    )
    if affected <= 0:
        return None
    return {
        "warning": True,
        "affected_timing_measurements": affected,
        "allowed_error_seconds": threshold,
        "maximum_stimulus_error_seconds": max(stimulus_errors, default=0.0),
        "maximum_soi_error_seconds": max(soi_errors, default=0.0),
        "measurement_timebases": sorted(
            {
                source
                for actual, source in (*stimulus_observations, *soi_observations)
                if actual is not None
            }
        ),
        "action": "recording continues; review the timing report after acquisition",
    }


def _capture_response_close_event(
    holder: dict[str, Any],
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    label: str,
    planned: dict[str, Any],
    timing: dict[str, Any],
    scheduled_monotonic: float,
    scheduled_lsl: float | None,
) -> None:
    flip_monotonic = monotonic()
    flip_lsl = lsl_local_clock()
    latency = float(timing.get("fixed_display_latency_ms", 0.0)) / 1000.0
    modeled_monotonic = flip_monotonic + latency
    modeled_lsl = None if flip_lsl is None else flip_lsl + latency
    if isinstance(marker_outlet, LslMarkerOutlet) and modeled_lsl is None:
        raise RuntimeError("LSL local clock is unavailable for a required response-close marker")
    marker_outlet.push(label, timestamp=modeled_lsl)
    holder["response_close_monotonic"] = modeled_monotonic
    holder["response_close_lsl"] = modeled_lsl
    holder["event"] = {
        "label": label,
        "timestamp": modeled_monotonic,
        "trial": int(planned["global_trial_index"]),
        "lsl_timestamp": modeled_lsl,
        "scheduled_on_flip": True,
        "flip_monotonic": flip_monotonic,
        "flip_lsl_timestamp": flip_lsl,
        "scheduled_response_window_close_monotonic": scheduled_monotonic,
        "scheduled_response_window_close_lsl": scheduled_lsl,
        "response_window_close_overshoot_seconds": max(0.0, modeled_monotonic - scheduled_monotonic),
        **_marker_metadata(planned),
    }


def _log_captured_response_close_event(holder: dict[str, Any], logger: EventLogger) -> None:
    payload = dict(holder["event"])
    label = str(payload.pop("label"))
    timestamp = float(payload.pop("timestamp"))
    trial = int(payload.pop("trial"))
    logger.mark(label, timestamp=timestamp, trial=trial, **payload)


def _capture_flip_event(
    holder: dict[str, Any],
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    label: str,
    planned: dict[str, Any],
    timing: dict[str, Any],
    kind: str,
) -> None:
    flip_monotonic = monotonic()
    flip_lsl = lsl_local_clock()
    latency = float(timing.get("fixed_display_latency_ms", 0.0)) / 1000.0
    modeled_monotonic = flip_monotonic + latency
    modeled_lsl = None if flip_lsl is None else flip_lsl + latency
    if isinstance(marker_outlet, LslMarkerOutlet) and modeled_lsl is None:
        raise RuntimeError("LSL local clock is unavailable for a required display-flip marker")
    marker_outlet.push(label, timestamp=modeled_lsl)
    holder[f"{kind}_monotonic"] = modeled_monotonic
    holder[f"{kind}_lsl"] = modeled_lsl
    holder[f"{kind}_event"] = {
        "label": label,
        "timestamp": modeled_monotonic,
        "trial": int(planned["global_trial_index"]),
        "lsl_timestamp": modeled_lsl,
        "scheduled_on_flip": True,
        "flip_monotonic": flip_monotonic,
        "flip_lsl_timestamp": flip_lsl,
        "expected_visual_onset_monotonic": modeled_monotonic,
        "expected_visual_onset_lsl_timestamp": modeled_lsl,
        "fixed_display_latency_ms": timing.get("fixed_display_latency_ms"),
        "expected_visual_onset_uncertainty_ms": timing.get("expected_visual_onset_uncertainty_ms"),
        "photodiode_verification_enabled": timing.get("photodiode_verification_enabled", False),
        "timing_model": "psychopy_callOnFlip_plus_fixed_display_latency",
        "task": TASK_NAME,
        "marker_stream_name": getattr(marker_outlet, "name", None),
        "marker_stream_type": getattr(marker_outlet, "stream_type", None),
        "marker_stream_source_id": getattr(marker_outlet, "source_id", None),
        "marker_emit_attempted": True,
        **_marker_metadata(planned),
    }


def _capture_countdown_flip(
    holder: dict[str, Any],
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    label: str,
    display_value: str,
    countdown_index: int,
    timing: dict[str, Any],
) -> None:
    flip_monotonic = monotonic()
    flip_lsl = lsl_local_clock()
    latency = float(timing.get("fixed_display_latency_ms", 0.0)) / 1000.0
    modeled_monotonic = flip_monotonic + latency
    modeled_lsl = None if flip_lsl is None else flip_lsl + latency
    if isinstance(marker_outlet, LslMarkerOutlet) and modeled_lsl is None:
        raise RuntimeError("LSL local clock is unavailable for a required countdown display marker")
    marker_outlet.push(label, timestamp=modeled_lsl)
    holder["event"] = {
        "label": label,
        "timestamp": modeled_monotonic,
        "lsl_timestamp": modeled_lsl,
        "value": display_value,
        "countdown_index": int(countdown_index),
        "countdown_value": display_value,
        "scheduled_on_flip": True,
        "flip_monotonic": flip_monotonic,
        "flip_lsl_timestamp": flip_lsl,
        "expected_visual_onset_monotonic": modeled_monotonic,
        "expected_visual_onset_lsl_timestamp": modeled_lsl,
        "fixed_display_latency_ms": timing.get("fixed_display_latency_ms"),
        "expected_visual_onset_uncertainty_ms": timing.get("expected_visual_onset_uncertainty_ms"),
        "photodiode_verification_enabled": timing.get("photodiode_verification_enabled", False),
        "timing_model": "psychopy_callOnFlip_plus_fixed_display_latency",
        "task": TASK_NAME,
        "task_state": "COUNTDOWN",
        "marker_stream_name": getattr(marker_outlet, "name", None),
        "marker_stream_type": getattr(marker_outlet, "stream_type", None),
        "marker_stream_source_id": getattr(marker_outlet, "source_id", None),
        "marker_emit_attempted": True,
    }


def _log_captured_countdown_flip(holder: dict[str, Any], logger: EventLogger) -> None:
    payload = dict(holder["event"])
    label = str(payload.pop("label"))
    timestamp = float(payload.pop("timestamp"))
    logger.mark(label, timestamp=timestamp, **payload)


def _log_captured_flip_event(holder: dict[str, Any], logger: EventLogger, kind: str) -> None:
    payload = dict(holder[f"{kind}_event"])
    label = str(payload.pop("label"))
    timestamp = float(payload.pop("timestamp"))
    trial = int(payload.pop("trial"))
    logger.mark(label, timestamp=timestamp, trial=trial, **payload)


def _complete_support(
    store: DynamicSartArtifactStore,
    config: DynamicSartConfig,
    timestamp: float,
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
) -> dict[str, Any]:
    reference = compute_support_reference(
        store.records,
        minimum_valid_rt_seconds=config.minimum_valid_rt_seconds,
        response_window_seconds=config.response_window_seconds,
        created_at_support_boundary=timestamp,
    )
    store.write_support_reference(reference)
    _emit(
        logger,
        marker_outlet,
        marker_label("support_complete"),
        event_type="SYSTEM",
        timestamp=timestamp,
        task=TASK_NAME,
        reference_hash=reference["reference_hash"],
        valid_support_go_count=reference["valid_support_go_count"],
    )
    return reference


def _recorder_health_gate(
    monitor: RecorderHealthMonitor,
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    *,
    trial: dict[str, Any] | None = None,
) -> bool:
    health = monitor.check()
    if health.ok:
        if bool(getattr(health, "warning", False)):
            _emit(
                logger,
                marker_outlet,
                marker_label("recorder_warning", trial),
                event_type="SYSTEM",
                trial=None if trial is None else int(trial.get("global_trial_index", 0)),
                reason=health.reason,
                recorder_status=health.status.get("status"),
                recorder_summary=_compact_recorder_summary(health.status.get("summary")),
            )
        return True
    _emit(
        logger,
        marker_outlet,
        marker_label("recorder_failure", trial),
        event_type="SYSTEM",
        trial=None if trial is None else int(trial.get("global_trial_index", 0)),
        reason=health.reason,
        recorder_status=health.status.get("status"),
        recorder_summary=_compact_recorder_summary(health.status.get("summary")),
    )
    return False


def _compact_recorder_summary(summary: Any) -> dict[str, Any]:
    """Retain actionable health evidence without serializing recorder metadata mid-trial."""

    payload = dict(summary or {})
    csv_mirror = dict(payload.get("csv_mirror") or {})
    heartbeat = dict(payload.get("lsl_sample_heartbeat") or {})
    return {
        "status": payload.get("status"),
        "primary_format": payload.get("primary_format"),
        "sample_count": payload.get("sample_count"),
        "xdf_growth_status": payload.get("xdf_growth_status"),
        "xdf_size_bytes": payload.get("xdf_size_bytes"),
        "error": payload.get("error"),
        "csv_mirror": {
            "status": csv_mirror.get("status"),
            "sample_count": csv_mirror.get("sample_count"),
            "error": csv_mirror.get("error"),
        },
        "lsl_sample_heartbeat": {
            "status": heartbeat.get("status"),
            "sample_count": heartbeat.get("sample_count"),
            "error": heartbeat.get("error"),
        },
    }


def _emit(
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    label: str,
    *,
    event_type: str = "EVENT",
    timestamp: float | None = None,
    lsl_timestamp: float | None = None,
    trial: int | None = None,
    value: str | None = None,
    **metadata: Any,
) -> Any:
    monotonic_timestamp = monotonic() if timestamp is None else float(timestamp)
    marker_timestamp = lsl_local_clock() if lsl_timestamp is None else lsl_timestamp
    if isinstance(marker_outlet, LslMarkerOutlet) and marker_timestamp is None:
        raise RuntimeError("LSL local clock is unavailable for a required DSART marker")
    marker_outlet.push(label, timestamp=marker_timestamp)
    metadata.setdefault("task", TASK_NAME)
    metadata.setdefault("marker_stream_name", getattr(marker_outlet, "name", None))
    metadata.setdefault("marker_stream_type", getattr(marker_outlet, "stream_type", None))
    metadata.setdefault("marker_stream_source_id", getattr(marker_outlet, "source_id", None))
    metadata.setdefault("marker_emit_attempted", True)
    return logger.mark(
        label,
        event_type=event_type,
        timestamp=monotonic_timestamp,
        trial=trial,
        value=value,
        lsl_timestamp=marker_timestamp,
        **metadata,
    )


def _run_dry_countdown(
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    timestamp: float,
    step_seconds: float,
) -> float:
    _emit(
        logger,
        marker_outlet,
        marker_label("countdown_start"),
        event_type="SYSTEM",
        timestamp=timestamp,
        countdown_steps=list(EXPERIMENTAL_COUNTDOWN),
    )
    current = float(timestamp)
    for index, display_value in enumerate(EXPERIMENTAL_COUNTDOWN, start=1):
        _emit(
            logger,
            marker_outlet,
            marker_label("countdown_step", step=index, value=display_value.rstrip("!")),
            event_type="EVENT",
            timestamp=current,
            value=display_value,
            countdown_index=index,
            countdown_value=display_value,
            scheduled_on_flip=False,
        )
        current += float(step_seconds)
    _emit(
        logger,
        marker_outlet,
        marker_label("countdown_end"),
        event_type="SYSTEM",
        timestamp=current,
    )
    return current


def _run_psychopy_countdown(
    win: Any,
    visual: Any,
    keyboard: PersistentKeyboardCollector,
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    timing: dict[str, Any],
    recorder_monitor: RecorderHealthMonitor,
    step_seconds: float,
) -> tuple[bool, str | None]:
    _emit(
        logger,
        marker_outlet,
        marker_label("countdown_start"),
        event_type="SYSTEM",
        countdown_steps=list(EXPERIMENTAL_COUNTDOWN),
    )
    for index, display_value in enumerate(EXPERIMENTAL_COUNTDOWN, start=1):
        if not _recorder_health_gate(recorder_monitor, logger, marker_outlet):
            return False, "recorder_health_failure"
        prompt = visual.TextStim(
            win,
            text=display_value,
            height=0.18 if display_value != "GO!" else 0.14,
            color="white",
        )
        holder: dict[str, Any] = {}
        label = marker_label("countdown_step", step=index, value=display_value.rstrip("!"))
        win.callOnFlip(
            _capture_countdown_flip,
            holder,
            marker_outlet,
            label,
            display_value,
            index,
            timing,
        )
        prompt.draw()
        win.flip()
        _log_captured_countdown_flip(holder, logger)
        deadline = monotonic() + float(step_seconds)
        while monotonic() < deadline:
            service_psychopy_static_window(win, prompt)
            rows = keyboard.poll(task_state="COUNTDOWN")
            if any(row["is_escape_key"] for row in rows):
                return False, "escape_abort"
            sleep(0.005)
    _emit(
        logger,
        marker_outlet,
        marker_label("countdown_end"),
        event_type="SYSTEM",
    )
    return True, None


def _show_screen(
    win: Any,
    visual: Any,
    keyboard: PersistentKeyboardCollector,
    text: str,
    *,
    state: str,
    allowed_continue: Iterable[str],
) -> bool:
    allowed = set(allowed_continue)
    prompt = visual.TextStim(win, text=text, height=0.045, color="white", wrapWidth=1.5)
    prompt.draw()
    win.flip()
    while True:
        service_psychopy_static_window(win, prompt)
        rows = keyboard.poll(task_state=state)
        if any(row["is_escape_key"] for row in rows):
            return False
        if any(row["key"] in allowed for row in rows):
            return True
        sleep(0.01)


def _show_bounded_break(
    win: Any,
    visual: Any,
    keyboard: PersistentKeyboardCollector,
    minimum_seconds: float,
    maximum_seconds: float,
    *,
    started_at: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    minimum_seconds = max(0.0, float(minimum_seconds))
    maximum_seconds = max(minimum_seconds, float(maximum_seconds))
    started = monotonic() if started_at is None else float(started_at)
    if maximum_seconds == minimum_seconds:
        instruction = f"Please rest for {minimum_seconds:g} seconds. The task will then continue automatically."
    else:
        instruction = (
            f"Please rest for at least {minimum_seconds:g} seconds.\n"
            f"After that, press SPACE when ready. The task resumes by {maximum_seconds:g} seconds."
        )
    prompt = visual.TextStim(
        win,
        text=f"Break\n\nMaintain your setup.\n\n{instruction}",
        height=0.05,
        color="white",
        wrapWidth=1.5,
    )
    prompt.draw()
    win.flip()
    minimum_deadline = started + minimum_seconds
    maximum_deadline = started + maximum_seconds
    ignored_early_continue_presses = 0
    while monotonic() < maximum_deadline:
        service_psychopy_static_window(win, prompt)
        rows = keyboard.poll(task_state="BREAK")
        if any(row["is_escape_key"] for row in rows):
            ended = monotonic()
            return False, {
                "minimum_break_seconds": minimum_seconds,
                "maximum_break_seconds": maximum_seconds,
                "actual_break_seconds": max(0.0, ended - started),
                "ended_at_monotonic": ended,
                "break_end_reason": "escape_abort",
                "ignored_early_continue_presses": ignored_early_continue_presses,
            }
        if any(row["is_response_key"] for row in rows):
            if monotonic() < minimum_deadline:
                ignored_early_continue_presses += 1
            else:
                ended = monotonic()
                return True, {
                    "minimum_break_seconds": minimum_seconds,
                    "maximum_break_seconds": maximum_seconds,
                    "actual_break_seconds": max(0.0, ended - started),
                    "ended_at_monotonic": ended,
                    "break_end_reason": "participant_continue_after_minimum",
                    "ignored_early_continue_presses": ignored_early_continue_presses,
                }
        sleep(0.01)
    ended = monotonic()
    return True, {
        "minimum_break_seconds": minimum_seconds,
        "maximum_break_seconds": maximum_seconds,
        "actual_break_seconds": max(0.0, ended - started),
        "ended_at_monotonic": ended,
        "break_end_reason": "maximum_elapsed",
        "ignored_early_continue_presses": ignored_early_continue_presses,
    }


def _show_timed_text(
    win: Any,
    visual: Any,
    keyboard: PersistentKeyboardCollector,
    text: str,
    seconds: float,
) -> bool:
    prompt = visual.TextStim(win, text=text, height=0.06, color="white")
    prompt.draw()
    win.flip()
    deadline = monotonic() + max(0.0, seconds)
    while monotonic() < deadline:
        service_psychopy_static_window(win, prompt)
        rows = keyboard.poll(task_state="PRACTICE_FEEDBACK")
        if any(row["is_escape_key"] for row in rows):
            return False
        sleep(0.005)
    return True


def _show_completion(
    win: Any,
    visual: Any,
    keyboard: PersistentKeyboardCollector,
    paths: SessionPaths,
    summary: dict[str, Any],
    max_wait: float,
) -> None:
    prompt = visual.TextStim(
        win,
        text=_completion_text(paths, summary),
        height=0.04,
        color="white",
        wrapWidth=1.6,
    )
    prompt.draw()
    win.flip()
    deadline = monotonic() + max(0.0, max_wait)
    while monotonic() < deadline:
        service_psychopy_static_window(win, prompt)
        rows = keyboard.poll(task_state="COMPLETE")
        if any(row["is_response_key"] or row["is_escape_key"] for row in rows):
            return
        sleep(0.02)


def _completion_text(paths: SessionPaths, summary: dict[str, Any]) -> str:
    completed = int(summary.get("experimental_trials") or 0)
    planned = int(summary.get("planned_experimental_trials") or completed)
    if not bool(summary.get("aborted")):
        return (
            "Dynamic SART complete\n\n"
            f"Recorded {completed} experimental trials.\n"
            f"Session: {paths.root}\n\nPress SPACE to close."
        )
    reason = str(summary.get("abort_reason") or "unknown")
    reason_text = {
        "practice_criteria_not_met": "Practice criteria were not met; the experimental session did not start.",
        "escape_abort": "The task was stopped with Escape.",
        "keyboard_interrupt": "The task was interrupted by the operator.",
        "recorder_health_failure": "The EEG recorder stopped or became unhealthy.",
    }.get(reason, f"The task stopped early ({reason}).")
    return (
        "Dynamic SART did not complete\n\n"
        f"{reason_text}\n"
        f"Recorded {completed} of {planned} experimental trials.\n"
        f"Session: {paths.root}\n\nPress SPACE to close."
    )


def _make_marker_outlet(markers: dict[str, Any], paths: SessionPaths) -> LslMarkerOutlet | NullMarkerOutlet:
    try:
        return LslMarkerOutlet(
            name=str(markers.get("lsl_stream_name", "EEGleMarkers")),
            stream_type=str(markers.get("lsl_stream_type", "Markers")),
            source_id=str(markers.get("source_id") or session_marker_source_id(paths.root)),
        )
    except Exception as exc:
        if bool(markers.get("required_for_realtime", False)):
            raise RuntimeError(f"required LSL marker outlet could not be created: {type(exc).__name__}: {exc}") from exc
        return NullMarkerOutlet(f"{type(exc).__name__}: {exc}")


def _start_marker_receipt_recorder(
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    paths: SessionPaths,
) -> LslMarkerReceiptRecorder:
    if not isinstance(marker_outlet, LslMarkerOutlet):
        raise RuntimeError("independent marker receipt requires a live LSL marker outlet")
    recorder = LslMarkerReceiptRecorder(
        marker_outlet.source_id,
        paths.raw / "lsl_markers_received.csv",
        paths.raw / "lsl_markers_received_metadata.json",
    )
    recorder.start()
    recorder.wait_until_ready()
    summary = recorder.snapshot()
    if summary.get("status") != "recording":
        recorder.stop()
        raise RuntimeError(
            "independent LSL marker receipt did not start: "
            + str(summary.get("error") or summary.get("status"))
        )
    return recorder


def _drain_emitted_markers(
    config: dict[str, Any],
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    recorder: LslMarkerReceiptRecorder,
) -> str | None:
    """Wait for transport receipt before closing the task's marker subscriber."""

    expected_count = getattr(marker_outlet, "pushed_count", None)
    if not isinstance(expected_count, int):
        return None
    timeout = max(
        0.0,
        float(config.get("recording_suite", {}).get("marker_receipt_timeout_seconds", 2.0)),
    )
    try:
        if recorder.wait_for_count(expected_count, timeout=timeout):
            return None
        observed = int(recorder.snapshot().get("received_count") or 0)
        return (
            "marker receipt drain timed out before shutdown: "
            f"received {observed} of {expected_count} emitted markers within {timeout:.3f} seconds"
        )
    except Exception as exc:
        return f"marker receipt drain failed: {type(exc).__name__}: {exc}"


def _practice_feedback(record: dict[str, Any], no_go_digit: int) -> str:
    if record["primary_outcome"] == "commission_error":
        return f"Do not press for {no_go_digit}."
    if record["primary_outcome"] == "omission_error":
        return "Press SPACE for go digits."
    return "Check the response rule."


def _block_result(
    planned: list[dict[str, Any]],
    completed: list[dict[str, Any]],
    *,
    criteria: dict[str, Any] | None = None,
) -> dict[str, Any]:
    first = planned[0] if planned else {}
    return {
        "schema": BLOCK_SCHEMA,
        "block_index": first.get("block_index"),
        "block_name": first.get("block_name"),
        "phase": first.get("phase"),
        "planned_trials": len(planned),
        "completed_trials": sum(
            int(not row.get("aborted") and not row.get("invalid"))
            for row in completed
        ),
        "correct_trials": sum(int(bool(row.get("correct"))) for row in completed),
        "start_trial": first.get("global_trial_index"),
        "end_trial": planned[-1].get("global_trial_index") if planned else None,
        "criteria": criteria,
    }


def _marker_metadata(planned: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": planned.get("phase"),
        "study_segment": planned.get("study_segment"),
        "practice": bool(planned.get("is_practice")),
        "condition": planned.get("condition"),
        "digit": planned.get("digit"),
        "block": planned.get("block_index"),
        "sequence_id": planned.get("sequence_id"),
        "cue_opportunity": bool(planned.get("cue_opportunity", False)),
        "cue_opportunity_index": planned.get("cue_opportunity_index"),
        "cue_randomization_block_index": planned.get("cue_randomization_block_index"),
        "cue_assignment": planned.get("cue_assignment"),
    }


def _emit_planned_cue(
    logger: EventLogger,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet,
    planned: dict[str, Any],
    *,
    timestamp: float | None = None,
) -> None:
    """Publish immutable cue assignment markers without claiming physical delivery."""
    if not bool(planned.get("cue_opportunity", False)):
        return
    metadata = _marker_metadata(planned)
    trial = int(planned["global_trial_index"])
    _emit(
        logger,
        marker_outlet,
        marker_label("cue_opportunity", planned),
        timestamp=timestamp,
        trial=trial,
        **metadata,
    )
    _emit(
        logger,
        marker_outlet,
        marker_label("cue_assigned", planned),
        timestamp=timestamp,
        trial=trial,
        value=str(planned.get("cue_assignment")),
        delivery_status="not_attempted",
        **metadata,
    )


def _next_experimental_trial(rows: list[dict[str, Any]], current: int) -> dict[str, Any] | None:
    return next((row for row in rows if int(row["global_trial_index"]) == current + 1), None)


def _trials_since_no_go(records: list[dict[str, Any]]) -> int | None:
    experimental = [row for row in records if not row.get("is_practice")]
    for distance, row in enumerate(reversed(experimental), start=1):
        if bool(row.get("is_no_go")):
            return distance
    return None


def _participant_id(paths: SessionPaths, explicit: str | None) -> str:
    if explicit:
        return explicit
    try:
        with paths.manifest.open("r", encoding="utf-8") as handle:
            return str(json.load(handle).get("participant_id", "unknown-participant"))
    except (FileNotFoundError, json.JSONDecodeError):
        return "unknown-participant"


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
