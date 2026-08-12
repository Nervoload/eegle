"""Versioned schemas and validated configuration for Dynamic-State SART."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


TASK_NAME = "dynamic_sart"
TASK_VERSION = "1.2"
PLAN_SCHEMA = "eegle.dynamic_sart.plan.v3"
TRIAL_SCHEMA = "eegle.dynamic_sart.trial.v3"
KEY_EVENT_SCHEMA = "eegle.dynamic_sart.key_event.v1"
BLOCK_SCHEMA = "eegle.dynamic_sart.block.v3"
SUPPORT_REFERENCE_SCHEMA = "eegle.dynamic_sart.support_reference.v2"
LABELS_SCHEMA = "eegle.dynamic_sart.labels.v2"
LABEL_CONTRACT_SCHEMA = "eegle.dynamic_sart.label_contract.v2"
SUMMARY_SCHEMA = "eegle.dynamic_sart.summary.v2"
PROBE_SCHEMA = "eegle.dynamic_sart.probe.v1"

PHASES = ("practice", "support", "query")
EXPERIMENTAL_PHASES = ("support", "query")
PRIMARY_OUTCOMES = (
    "correct_go",
    "correct_no_go",
    "commission_error",
    "omission_error",
    "aborted",
    "invalid",
)


@dataclass(frozen=True)
class DynamicSartBlock:
    name: str
    phase: str
    trials: int
    break_after: bool = False
    minimum_break_seconds: float = 30.0
    maximum_break_seconds: float = 30.0
    study_segment: str | None = None
    planned_no_go_count: int | None = None

    def payload(self, index: int) -> dict[str, Any]:
        return {
            "schema": BLOCK_SCHEMA,
            "block_index": index,
            "block_name": self.name,
            "phase": self.phase,
            "trials": self.trials,
            "break_after": self.break_after,
            "minimum_break_seconds": self.minimum_break_seconds,
            "maximum_break_seconds": self.maximum_break_seconds,
            "study_segment": self.study_segment,
            "planned_no_go_count": self.planned_no_go_count,
        }


@dataclass(frozen=True)
class DynamicSartConfig:
    digits: tuple[int, ...]
    no_go_digit: int
    response_keys: tuple[str, ...]
    escape_keys: tuple[str, ...]
    stimulus_seconds: float
    response_window_seconds: float
    inter_trial_jitter_min_seconds: float
    inter_trial_jitter_max_seconds: float
    soi_min_seconds: float | None
    soi_max_seconds: float | None
    minimum_valid_rt_seconds: float
    no_go_probability: float
    planned_no_go_count: int | None
    minimum_go_trials_between_no_go: int
    minimum_leading_go_trials: int
    minimum_trailing_go_trials: int
    master_seed: int
    blocks: tuple[DynamicSartBlock, ...]
    practice_enabled: bool
    practice_trials_per_round: int
    practice_no_go_trials: int
    practice_max_rounds: int
    practice_go_accuracy: float
    practice_no_go_accuracy: float
    practice_max_anticipatory_rate: float
    practice_feedback_seconds: float
    thought_probes_enabled: bool
    thought_probe_question: str
    thought_probe_choices: tuple[str, ...]
    thought_probe_after_trials: tuple[int, ...]
    thought_probe_max_seconds: float
    thought_probe_exclusion_window_trials: int
    allow_task_adaptation: bool
    allow_stimulation: bool
    countdown_step_seconds: float
    completion_auto_close_seconds: float
    cue_schedule: dict[str, Any]
    dry_run: dict[str, Any]

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> "DynamicSartConfig":
        raw = dict(value or {})
        practice = dict(raw.get("practice") or {})
        thought_probes = dict(raw.get("thought_probes") or {})
        blocks_value = raw.get("blocks")
        if blocks_value is None:
            blocks_value = [
                {"name": "support_1", "phase": "support", "trials": 160},
                {"name": "support_2", "phase": "support", "trials": 160, "break_after": True},
                {"name": "query_1", "phase": "query", "trials": 160},
                {"name": "query_2", "phase": "query", "trials": 160, "break_after": True},
                {"name": "query_3", "phase": "query", "trials": 160},
                {"name": "query_4", "phase": "query", "trials": 160},
            ]
        parsed_blocks = []
        for index, item in enumerate(blocks_value, start=1):
            legacy_break_seconds = float(item.get("break_seconds", raw.get("break_seconds", 30.0)))
            parsed_blocks.append(
                DynamicSartBlock(
                    name=str(item.get("name", f"block_{index}")),
                    phase=str(item.get("phase", "query")),
                    trials=int(item.get("trials", 0)),
                    break_after=bool(item.get("break_after", False)),
                    minimum_break_seconds=float(item.get("minimum_break_seconds", legacy_break_seconds)),
                    maximum_break_seconds=float(item.get("maximum_break_seconds", legacy_break_seconds)),
                    study_segment=(
                        None if item.get("study_segment") is None else str(item.get("study_segment"))
                    ),
                    planned_no_go_count=(
                        None if item.get("planned_no_go_count") is None else int(item["planned_no_go_count"])
                    ),
                )
            )
        blocks = tuple(parsed_blocks)
        config = cls(
            digits=tuple(int(item) for item in raw.get("digits", range(1, 10))),
            no_go_digit=int(raw.get("no_go_digit", 3)),
            response_keys=tuple(str(item) for item in raw.get("response_keys", ["space"])),
            escape_keys=tuple(str(item) for item in raw.get("escape_keys", ["escape", "q"])),
            stimulus_seconds=float(raw.get("stimulus_seconds", 0.25)),
            response_window_seconds=float(raw.get("response_window_seconds", 1.60)),
            inter_trial_jitter_min_seconds=float(raw.get("inter_trial_jitter_min_seconds", 0.0)),
            inter_trial_jitter_max_seconds=float(raw.get("inter_trial_jitter_max_seconds", 0.0)),
            soi_min_seconds=(None if raw.get("soi_min_seconds") is None else float(raw["soi_min_seconds"])),
            soi_max_seconds=(None if raw.get("soi_max_seconds") is None else float(raw["soi_max_seconds"])),
            minimum_valid_rt_seconds=float(raw.get("minimum_valid_rt_seconds", 0.10)),
            no_go_probability=float(raw.get("no_go_probability", 1.0 / 9.0)),
            planned_no_go_count=(
                None if raw.get("planned_no_go_count") is None else int(raw["planned_no_go_count"])
            ),
            minimum_go_trials_between_no_go=int(raw.get("minimum_go_trials_between_no_go", 2)),
            minimum_leading_go_trials=int(raw.get("minimum_leading_go_trials", 4)),
            minimum_trailing_go_trials=int(raw.get("minimum_trailing_go_trials", 0)),
            master_seed=int(raw.get("master_seed", 42)),
            blocks=blocks,
            practice_enabled=bool(practice.get("enabled", True)),
            practice_trials_per_round=int(practice.get("trials_per_round", 27)),
            practice_no_go_trials=int(practice.get("no_go_trials", 3)),
            practice_max_rounds=int(practice.get("max_rounds", 3)),
            practice_go_accuracy=float(practice.get("go_accuracy", 0.80)),
            practice_no_go_accuracy=float(practice.get("no_go_accuracy", 0.6666666667)),
            practice_max_anticipatory_rate=float(practice.get("max_anticipatory_response_rate", 0.15)),
            practice_feedback_seconds=float(practice.get("feedback_seconds", 0.35)),
            thought_probes_enabled=bool(thought_probes.get("enabled", raw.get("thought_probes_enabled", False))),
            thought_probe_question=str(
                thought_probes.get("question", "Where was your attention just before this question?")
            ),
            thought_probe_choices=tuple(
                str(item) for item in thought_probes.get("response_choices", ["on_task", "mind_wandering", "uncertain"])
            ),
            thought_probe_after_trials=tuple(int(item) for item in thought_probes.get("after_trials", [])),
            thought_probe_max_seconds=float(thought_probes.get("max_seconds", 10.0)),
            thought_probe_exclusion_window_trials=int(thought_probes.get("exclusion_window_trials", 2)),
            allow_task_adaptation=bool(raw.get("allow_task_adaptation", False)),
            allow_stimulation=bool(raw.get("allow_stimulation", False)),
            countdown_step_seconds=float(raw.get("countdown_step_seconds", 1.0)),
            completion_auto_close_seconds=float(raw.get("completion_auto_close_seconds", 30.0)),
            cue_schedule=dict(raw.get("cue_schedule") or {}),
            dry_run=dict(raw.get("dry_run") or {}),
        )
        validate_dynamic_sart_config(config)
        return config

    @property
    def normal_recipe_trial_count(self) -> int:
        return sum(block.trials for block in self.blocks)

    @property
    def post_digit_fixation_seconds(self) -> float:
        """Planned fixation duration between digit offset and the next onset."""

        return self.response_window_seconds - self.stimulus_seconds

    def payload(self) -> dict[str, Any]:
        return {
            "digits": list(self.digits),
            "no_go_digit": self.no_go_digit,
            "response_keys": list(self.response_keys),
            "escape_keys": list(self.escape_keys),
            "stimulus_seconds": self.stimulus_seconds,
            "post_digit_fixation_seconds": self.post_digit_fixation_seconds,
            "response_window_seconds": self.response_window_seconds,
            "inter_trial_jitter_min_seconds": self.inter_trial_jitter_min_seconds,
            "inter_trial_jitter_max_seconds": self.inter_trial_jitter_max_seconds,
            "soi_min_seconds": self.soi_min_seconds,
            "soi_max_seconds": self.soi_max_seconds,
            "minimum_valid_rt_seconds": self.minimum_valid_rt_seconds,
            "no_go_probability": self.no_go_probability,
            "planned_no_go_count": self.planned_no_go_count,
            "minimum_go_trials_between_no_go": self.minimum_go_trials_between_no_go,
            "minimum_leading_go_trials": self.minimum_leading_go_trials,
            "minimum_trailing_go_trials": self.minimum_trailing_go_trials,
            "master_seed": self.master_seed,
            "blocks": [block.payload(index) for index, block in enumerate(self.blocks, start=1)],
            "practice": {
                "enabled": self.practice_enabled,
                "trials_per_round": self.practice_trials_per_round,
                "no_go_trials": self.practice_no_go_trials,
                "max_rounds": self.practice_max_rounds,
                "go_accuracy": self.practice_go_accuracy,
                "no_go_accuracy": self.practice_no_go_accuracy,
                "max_anticipatory_response_rate": self.practice_max_anticipatory_rate,
                "feedback_seconds": self.practice_feedback_seconds,
            },
            "thought_probes": {
                "enabled": self.thought_probes_enabled,
                "question": self.thought_probe_question,
                "response_choices": list(self.thought_probe_choices),
                "after_trials": list(self.thought_probe_after_trials),
                "max_seconds": self.thought_probe_max_seconds,
                "exclusion_window_trials": self.thought_probe_exclusion_window_trials,
            },
            "allow_task_adaptation": self.allow_task_adaptation,
            "allow_stimulation": self.allow_stimulation,
            "countdown_step_seconds": self.countdown_step_seconds,
            "completion_auto_close_seconds": self.completion_auto_close_seconds,
            "cue_schedule": dict(self.cue_schedule),
            "dry_run": dict(self.dry_run),
        }


def validate_dynamic_sart_config(config: DynamicSartConfig) -> None:
    if not config.digits:
        raise ValueError("tasks.dynamic_sart.digits must be nonempty")
    if len(set(config.digits)) != len(config.digits):
        raise ValueError("tasks.dynamic_sart.digits must contain unique values")
    if config.no_go_digit not in config.digits:
        raise ValueError("tasks.dynamic_sart.no_go_digit must belong to digits")
    if len(config.digits) < 2:
        raise ValueError("tasks.dynamic_sart.digits must leave at least one go digit")
    if not config.response_keys:
        raise ValueError("tasks.dynamic_sart.response_keys must be nonempty")
    if not config.escape_keys:
        raise ValueError("tasks.dynamic_sart.escape_keys must be nonempty")
    durations = {
        "stimulus_seconds": config.stimulus_seconds,
        "response_window_seconds": config.response_window_seconds,
        "inter_trial_jitter_min_seconds": config.inter_trial_jitter_min_seconds,
        "inter_trial_jitter_max_seconds": config.inter_trial_jitter_max_seconds,
        "minimum_valid_rt_seconds": config.minimum_valid_rt_seconds,
    }
    for name, number in durations.items():
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"tasks.dynamic_sart.{name} must be finite and nonnegative")
    if config.response_window_seconds < config.stimulus_seconds:
        raise ValueError("tasks.dynamic_sart.response_window_seconds must be at least stimulus_seconds")
    if config.minimum_valid_rt_seconds >= config.response_window_seconds:
        raise ValueError("tasks.dynamic_sart.minimum_valid_rt_seconds must be less than response_window_seconds")
    if config.inter_trial_jitter_min_seconds > config.inter_trial_jitter_max_seconds:
        raise ValueError("tasks.dynamic_sart jitter minimum must not exceed jitter maximum")
    if (config.soi_min_seconds is None) != (config.soi_max_seconds is None):
        raise ValueError("tasks.dynamic_sart soi_min_seconds and soi_max_seconds must be configured together")
    if config.soi_min_seconds is not None and config.soi_max_seconds is not None:
        if not math.isfinite(config.soi_min_seconds) or not math.isfinite(config.soi_max_seconds):
            raise ValueError("tasks.dynamic_sart SOI bounds must be finite")
        if config.soi_min_seconds < config.response_window_seconds:
            raise ValueError("tasks.dynamic_sart soi_min_seconds must be at least response_window_seconds")
        if config.soi_max_seconds < config.soi_min_seconds:
            raise ValueError("tasks.dynamic_sart soi_max_seconds must be at least soi_min_seconds")
    if not 0.0 < config.no_go_probability < 1.0:
        raise ValueError("tasks.dynamic_sart.no_go_probability must be between zero and one")
    if config.minimum_go_trials_between_no_go < 0:
        raise ValueError("tasks.dynamic_sart.minimum_go_trials_between_no_go must be nonnegative")
    if config.minimum_leading_go_trials < 0:
        raise ValueError("tasks.dynamic_sart.minimum_leading_go_trials must be nonnegative")
    if config.minimum_trailing_go_trials < 0:
        raise ValueError("tasks.dynamic_sart.minimum_trailing_go_trials must be nonnegative")
    if not config.blocks or any(block.trials < 1 for block in config.blocks):
        raise ValueError("tasks.dynamic_sart.blocks must each contain at least one trial")
    if config.planned_no_go_count is not None:
        total_trials = sum(block.trials for block in config.blocks)
        if not 1 <= config.planned_no_go_count < total_trials:
            raise ValueError("tasks.dynamic_sart.planned_no_go_count must leave both go and no-go trials")
        if config.planned_no_go_count < len(config.blocks):
            raise ValueError("tasks.dynamic_sart.planned_no_go_count must allocate at least one no-go trial per block")
        maximum_total = sum(_maximum_no_go_count(block, config) for block in config.blocks)
        if config.planned_no_go_count > maximum_total:
            raise ValueError("tasks.dynamic_sart.planned_no_go_count is infeasible for the block spacing constraints")
    for block in config.blocks:
        requested = (
            int(block.planned_no_go_count)
            if block.planned_no_go_count is not None
            else max(1, int(round(block.trials * config.no_go_probability)))
        )
        if block.break_after:
            for name, seconds in (
                ("minimum_break_seconds", block.minimum_break_seconds),
                ("maximum_break_seconds", block.maximum_break_seconds),
            ):
                if not math.isfinite(seconds) or seconds < 0:
                    raise ValueError(f"tasks.dynamic_sart.blocks[{block.name}].{name} must be finite and nonnegative")
            if block.maximum_break_seconds < block.minimum_break_seconds:
                raise ValueError(
                    f"tasks.dynamic_sart.blocks[{block.name}].maximum_break_seconds must be at least minimum_break_seconds"
                )
        maximum = _maximum_no_go_count(block, config)
        if maximum < 1:
            raise ValueError(
                f"tasks.dynamic_sart.blocks[{block.name}] is too short for the leading/trailing go constraints"
            )
        if requested > maximum:
            raise ValueError(
                f"tasks.dynamic_sart.blocks[{block.name}] no-go count is infeasible for the spacing constraint"
            )
        if requested < 1 or requested >= block.trials:
            raise ValueError(
                f"tasks.dynamic_sart.blocks[{block.name}].planned_no_go_count must leave go and no-go trials"
            )
    names = [block.name for block in config.blocks]
    if len(names) != len(set(names)):
        raise ValueError("tasks.dynamic_sart block names must be unique")
    invalid_phases = [block.phase for block in config.blocks if block.phase not in EXPERIMENTAL_PHASES]
    if invalid_phases:
        raise ValueError(f"tasks.dynamic_sart block phase must be support or query; got {invalid_phases[0]}")
    if not any(block.phase == "support" for block in config.blocks):
        raise ValueError("tasks.dynamic_sart.blocks must contain at least one support block")
    if not any(block.phase == "query" for block in config.blocks):
        raise ValueError("tasks.dynamic_sart.blocks must contain at least one query block")
    phases = [block.phase for block in config.blocks]
    if "support" in phases[phases.index("query") :]:
        raise ValueError("tasks.dynamic_sart support blocks must all precede query blocks")
    if config.practice_enabled:
        if config.practice_trials_per_round < 2:
            raise ValueError("tasks.dynamic_sart.practice.trials_per_round must be at least 2")
        if not 1 <= config.practice_no_go_trials < config.practice_trials_per_round:
            raise ValueError("tasks.dynamic_sart.practice.no_go_trials must leave both go and no-go practice trials")
        if config.practice_max_rounds < 1:
            raise ValueError("tasks.dynamic_sart.practice.max_rounds must be at least 1")
        practice_block = DynamicSartBlock("practice", "practice", config.practice_trials_per_round)
        practice_maximum = _maximum_no_go_count(practice_block, config)
        if config.practice_no_go_trials > practice_maximum:
            raise ValueError("tasks.dynamic_sart.practice.no_go_trials is infeasible for the spacing constraint")
        for name, threshold in (
            ("go_accuracy", config.practice_go_accuracy),
            ("no_go_accuracy", config.practice_no_go_accuracy),
            ("max_anticipatory_response_rate", config.practice_max_anticipatory_rate),
        ):
            if not 0.0 <= threshold <= 1.0:
                raise ValueError(f"tasks.dynamic_sart.practice.{name} must be between zero and one")
    if config.thought_probes_enabled:
        if not config.thought_probe_question.strip():
            raise ValueError("tasks.dynamic_sart.thought_probes.question must be nonempty")
        if len(config.thought_probe_choices) < 2 or len(set(config.thought_probe_choices)) != len(config.thought_probe_choices):
            raise ValueError("tasks.dynamic_sart.thought_probes.response_choices must contain at least two unique choices")
        if not config.thought_probe_after_trials or any(value < 1 for value in config.thought_probe_after_trials):
            raise ValueError("tasks.dynamic_sart.thought_probes.after_trials must contain positive trial indices")
        if any(value > config.normal_recipe_trial_count for value in config.thought_probe_after_trials):
            raise ValueError("tasks.dynamic_sart.thought_probes.after_trials must fall within the experimental plan")
        if not math.isfinite(config.thought_probe_max_seconds) or config.thought_probe_max_seconds <= 0:
            raise ValueError("tasks.dynamic_sart.thought_probes.max_seconds must be positive")
        if config.thought_probe_exclusion_window_trials < 0:
            raise ValueError("tasks.dynamic_sart.thought_probes.exclusion_window_trials must be nonnegative")
    if config.allow_task_adaptation:
        raise ValueError("tasks.dynamic_sart.allow_task_adaptation must remain false")
    if config.allow_stimulation:
        raise ValueError("tasks.dynamic_sart.allow_stimulation must remain false")
    if not math.isfinite(config.countdown_step_seconds) or config.countdown_step_seconds <= 0:
        raise ValueError("tasks.dynamic_sart.countdown_step_seconds must be finite and positive")
    _validate_cue_schedule(config)


def _validate_cue_schedule(config: DynamicSartConfig) -> None:
    cue = dict(config.cue_schedule)
    if not bool(cue.get("enabled", False)):
        return
    interval = int(cue.get("opportunity_every_trials", 0))
    run_in = int(cue.get("run_in_trials", 0))
    block_size = int(cue.get("randomization_block_size", 0))
    cues_per_block = int(cue.get("cues_per_randomization_block", -1))
    if interval < 1:
        raise ValueError("tasks.dynamic_sart.cue_schedule.opportunity_every_trials must be positive")
    if run_in < 0:
        raise ValueError("tasks.dynamic_sart.cue_schedule.run_in_trials must be nonnegative")
    if block_size < 1:
        raise ValueError("tasks.dynamic_sart.cue_schedule.randomization_block_size must be positive")
    if not 0 <= cues_per_block <= block_size:
        raise ValueError(
            "tasks.dynamic_sart.cue_schedule.cues_per_randomization_block must fit the randomization block"
        )
    opportunities = max(0, (config.normal_recipe_trial_count - run_in) // interval)
    if opportunities == 0:
        raise ValueError("tasks.dynamic_sart.cue_schedule does not create any cue opportunities")
    if opportunities % block_size:
        raise ValueError(
            "tasks.dynamic_sart.cue_schedule opportunity count must be divisible by randomization_block_size"
        )


def _maximum_no_go_count(block: DynamicSartBlock, config: DynamicSartConfig) -> int:
    eligible = block.trials - config.minimum_leading_go_trials - config.minimum_trailing_go_trials
    if eligible <= 0:
        return 0
    return (eligible + config.minimum_go_trials_between_no_go) // (
        config.minimum_go_trials_between_no_go + 1
    )
