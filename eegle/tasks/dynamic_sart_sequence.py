"""Deterministic, behavior-independent planning for Dynamic-State SART."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import replace
from functools import lru_cache
from typing import Any

from eegle.tasks.dynamic_sart_schema import PLAN_SCHEMA, TASK_NAME, TASK_VERSION, DynamicSartBlock, DynamicSartConfig


def build_dynamic_sart_plan(
    config: DynamicSartConfig,
    *,
    trial_override: int | None = None,
) -> dict[str, Any]:
    blocks = config.blocks
    smoke_test_override = trial_override is not None and int(trial_override) != config.normal_recipe_trial_count
    if smoke_test_override:
        blocks = _smoke_blocks(int(trial_override), config)
    explicit_no_go_counts = _explicit_no_go_counts(config, blocks, smoke_test_override=smoke_test_override)

    configuration_hash = _hash_payload(config.payload())
    planned_blocks: list[dict[str, Any]] = []
    planned_trials: list[dict[str, Any]] = []
    block_seeds: dict[str, int] = {}
    global_trial = 0
    planned_offset = 0.0
    for block_index, block in enumerate(blocks, start=1):
        block_seed = _derived_seed(config.master_seed, block_index, block.name)
        block_seeds[str(block_index)] = block_seed
        block_trials = _build_block_trials(
            config,
            block,
            block_index,
            block_seed,
            no_go_count_override=None if explicit_no_go_counts is None else explicit_no_go_counts[block_index - 1],
        )
        if block_trials:
            block_trials[-1]["planned_break_after_trial"] = bool(block.break_after)
            block_trials[-1]["planned_break_minimum_seconds"] = (
                block.minimum_break_seconds if block.break_after else None
            )
            block_trials[-1]["planned_break_maximum_seconds"] = (
                block.maximum_break_seconds if block.break_after else None
            )
        for trial in block_trials:
            global_trial += 1
            trial["global_trial_index"] = global_trial
            trial["trial"] = global_trial
            trial["planned_onset_offset_seconds"] = planned_offset
            planned_offset += float(trial["planned_soi_seconds"])
            planned_trials.append(trial)
        block_payload = block.payload(block_index)
        block_payload["block_seed"] = block_seed
        block_payload["first_global_trial_index"] = global_trial - block.trials + 1
        block_payload["last_global_trial_index"] = global_trial
        block_payload["planned_no_go_count"] = sum(int(row["is_no_go"]) for row in block_trials)
        planned_blocks.append(block_payload)

    cue_schedule = _apply_cue_schedule(config, planned_trials, smoke_test_override=smoke_test_override)
    probe_trials = sorted(
        value for value in set(config.thought_probe_after_trials) if config.thought_probes_enabled and value <= len(planned_trials)
    )
    probe_ids = {trial_index: f"probe-{index:03d}" for index, trial_index in enumerate(probe_trials, start=1)}
    for row in planned_trials:
        trial_index = int(row["global_trial_index"])
        nearest = min(probe_trials, key=lambda value: abs(value - trial_index)) if probe_trials else None
        proximity = None if nearest is None else trial_index - nearest
        row["probe_after"] = trial_index in probe_ids
        row["probe_id"] = probe_ids.get(trial_index)
        row["probe_proximity"] = (
            proximity
            if proximity is not None and abs(proximity) <= config.thought_probe_exclusion_window_trials
            else None
        )

    practice_rounds = _build_practice_rounds(config)
    sequence_basis = {
        "schema": PLAN_SCHEMA,
        "task_name": TASK_NAME,
        "task_version": TASK_VERSION,
        "master_seed": config.master_seed,
        "configuration_hash": configuration_hash,
        "block_seeds": block_seeds,
        "planned_blocks": planned_blocks,
        "planned_trials": planned_trials,
        "practice_rounds": practice_rounds,
        "smoke_test_override": smoke_test_override,
        "requested_trial_override": int(trial_override) if trial_override is not None else None,
        "normal_recipe_trial_count": config.normal_recipe_trial_count,
        "planned_no_go_count": sum(int(row["is_no_go"]) for row in planned_trials),
        "cue_schedule": cue_schedule,
    }
    sequence_id = _hash_payload(sequence_basis)
    for row in planned_trials:
        row["sequence_id"] = sequence_id
    for round_rows in practice_rounds:
        for row in round_rows:
            row["sequence_id"] = sequence_id
    return {**sequence_basis, "sequence_id": sequence_id}


def validate_dynamic_sart_plan(plan: dict[str, Any], config: DynamicSartConfig) -> None:
    trials = list(plan.get("planned_trials") or [])
    if not trials:
        raise ValueError("dynamic_sart plan contains no experimental trials")
    phases = {str(row.get("phase")) for row in trials}
    if not {"support", "query"}.issubset(phases):
        raise ValueError("dynamic_sart plan must contain support and query trials")
    if (
        not bool(plan.get("smoke_test_override"))
        and config.planned_no_go_count is not None
        and sum(int(bool(row.get("is_no_go"))) for row in trials) != config.planned_no_go_count
    ):
        raise ValueError("dynamic_sart plan does not match planned_no_go_count")
    for block in plan.get("planned_blocks") or []:
        rows = [row for row in trials if int(row["block_index"]) == int(block["block_index"])]
        if len(rows) != int(block["trials"]):
            raise ValueError(f"dynamic_sart block {block['block_name']} trial count does not match its contract")
        no_go_positions = [int(row["block_trial_index"]) for row in rows if bool(row["is_no_go"])]
        if not no_go_positions:
            raise ValueError(f"dynamic_sart block {block['block_name']} has no no-go trial")
        configured_count = block.get("planned_no_go_count")
        if configured_count is not None and len(no_go_positions) != int(configured_count):
            raise ValueError(f"dynamic_sart block {block['block_name']} no-go count does not match its contract")
        if no_go_positions[0] <= config.minimum_leading_go_trials:
            raise ValueError(f"dynamic_sart block {block['block_name']} violates the leading-go constraint")
        if no_go_positions[-1] > len(rows) - config.minimum_trailing_go_trials:
            raise ValueError(f"dynamic_sart block {block['block_name']} violates the trailing-go constraint")
        gap = config.minimum_go_trials_between_no_go
        if any(right - left - 1 < gap for left, right in zip(no_go_positions, no_go_positions[1:])):
            raise ValueError(f"dynamic_sart block {block['block_name']} violates no-go spacing")
        _validate_no_go_randomization_block(rows, config, block)
        go_counts: dict[int, int] = {}
        for row in rows:
            if not bool(row["is_no_go"]):
                digit = int(row["digit"])
                go_counts[digit] = go_counts.get(digit, 0) + 1
        if go_counts and max(go_counts.values()) - min(go_counts.values()) > 1:
            raise ValueError(f"dynamic_sart block {block['block_name']} does not balance go digits")


def marker_label(family: str, trial: dict[str, Any] | None = None, **metadata: Any) -> str:
    prefix = family if family.startswith("dynamic_sart_") else f"dynamic_sart_{family}"
    fields: dict[str, Any] = {}
    if trial is not None:
        fields.update(
            {
                "v": 1,
                "trial": trial.get("global_trial_index"),
                "condition": trial.get("condition"),
                "digit": trial.get("digit"),
                "block": trial.get("block_index"),
                "phase": trial.get("phase"),
                "segment": trial.get("study_segment"),
                "cue_opportunity": 1 if bool(trial.get("cue_opportunity", False)) else None,
                "cue_assignment": trial.get("cue_assignment"),
                "practice": int(bool(trial.get("is_practice", False))),
            }
        )
    fields.update(metadata)
    if not fields:
        return prefix
    tokens = [f"{key}={_marker_token(value)}" for key, value in fields.items() if value is not None]
    return prefix + "__" + "__".join(tokens)


def _build_block_trials(
    config: DynamicSartConfig,
    block: DynamicSartBlock,
    block_index: int,
    block_seed: int,
    no_go_count_override: int | None = None,
) -> list[dict[str, Any]]:
    rng = random.Random(block_seed)
    no_go_count = int(
        block.planned_no_go_count
        if no_go_count_override is None and block.planned_no_go_count is not None
        else (
            max(1, int(round(block.trials * config.no_go_probability)))
            if no_go_count_override is None
            else no_go_count_override
        )
    )
    maximum = _maximum_spaced_events(
        block.trials,
        config.minimum_go_trials_between_no_go,
        config.minimum_leading_go_trials,
        config.minimum_trailing_go_trials,
    )
    if no_go_count > maximum:
        raise ValueError(
            f"tasks.dynamic_sart block {block.name} requests {no_go_count} no-go trials, "
            f"but spacing allows at most {maximum}"
        )
    no_go_positions_list, stratum_counts = _no_go_positions(
        block.trials,
        no_go_count,
        config,
        rng,
    )
    no_go_positions = set(no_go_positions_list)
    go_digits = [digit for digit in config.digits if digit != config.no_go_digit]
    go_count = block.trials - no_go_count
    assigned_go_digits: list[int] = []
    while len(assigned_go_digits) < go_count:
        cycle = list(go_digits)
        rng.shuffle(cycle)
        assigned_go_digits.extend(cycle)
    assigned_go_digits = assigned_go_digits[:go_count]
    go_cursor = 0
    rows = []
    for zero_index in range(block.trials):
        is_no_go = zero_index in no_go_positions
        digit = config.no_go_digit if is_no_go else assigned_go_digits[go_cursor]
        if not is_no_go:
            go_cursor += 1
        if config.soi_min_seconds is None:
            planned_jitter = rng.uniform(
                config.inter_trial_jitter_min_seconds,
                config.inter_trial_jitter_max_seconds,
            )
            planned_soi = config.response_window_seconds + planned_jitter
        else:
            planned_soi = rng.uniform(config.soi_min_seconds, config.soi_max_seconds)
            planned_jitter = planned_soi - config.response_window_seconds
        rows.append(
            {
                "schema": PLAN_SCHEMA,
                "task_name": TASK_NAME,
                "task_version": TASK_VERSION,
                "block_index": block_index,
                "block_trial_index": zero_index + 1,
                "block_name": block.name,
                "phase": block.phase,
                "study_segment": block.study_segment,
                "regime": "standard",
                "is_practice": False,
                "master_seed": config.master_seed,
                "block_seed": block_seed,
                "digit": digit,
                "condition": "no_go" if is_no_go else "go",
                "is_no_go": is_no_go,
                "expected_action": "withhold" if is_no_go else "press",
                "no_go_randomization_mode": str(
                    config.no_go_randomization.get("mode", "uniform_constrained")
                ),
                "no_go_stratum_index": (
                    None
                    if stratum_counts is None
                    else zero_index // int(config.no_go_randomization["stratum_trials"]) + 1
                ),
                "planned_no_go_count_in_stratum": (
                    None
                    if stratum_counts is None
                    else stratum_counts[
                        zero_index // int(config.no_go_randomization["stratum_trials"])
                    ]
                ),
                "planned_stimulus_seconds": config.stimulus_seconds,
                "planned_post_digit_fixation_seconds": config.post_digit_fixation_seconds,
                "planned_response_window_seconds": config.response_window_seconds,
                "planned_jitter_seconds": planned_jitter,
                "planned_soi_seconds": planned_soi,
                "cue_opportunity": False,
                "cue_opportunity_index": None,
                "cue_randomization_block_index": None,
                "cue_assignment": None,
                "planned_break_after_trial": False,
                "planned_break_minimum_seconds": None,
                "planned_break_maximum_seconds": None,
            }
        )
    return rows


def _build_practice_rounds(config: DynamicSartConfig) -> list[list[dict[str, Any]]]:
    if not config.practice_enabled:
        return []
    rounds = []
    negative_index = -1
    practice_probability = config.practice_no_go_trials / config.practice_trials_per_round
    for round_index in range(1, config.practice_max_rounds + 1):
        block = DynamicSartBlock(
            name=f"practice_{round_index}",
            phase="practice",
            trials=config.practice_trials_per_round,
        )
        practice_config = replace(config, no_go_probability=practice_probability)
        seed = _derived_seed(config.master_seed, -round_index, block.name)
        rows = _build_block_trials(practice_config, block, -round_index, seed)
        for row in rows:
            row.update(
                {
                    "global_trial_index": negative_index,
                    "trial": negative_index,
                    "phase": "practice",
                    "is_practice": True,
                    "practice_round": round_index,
                    "planned_onset_offset_seconds": None,
                }
            )
            negative_index -= 1
        rounds.append(rows)
    return rounds


def _smoke_blocks(trials: int, config: DynamicSartConfig) -> tuple[DynamicSartBlock, ...]:
    minimum_per_phase = config.minimum_leading_go_trials + config.minimum_trailing_go_trials + 1
    minimum_trials = minimum_per_phase * 2
    if trials < minimum_trials:
        raise ValueError(
            f"dynamic_sart --trials smoke override must be at least {minimum_trials} to preserve "
            "support/query and the configured leading/trailing go trials"
        )
    support = trials // 2
    query = trials - support
    return (
        DynamicSartBlock("support_smoke", "support", support),
        DynamicSartBlock("query_smoke", "query", query),
    )


def _explicit_no_go_counts(
    config: DynamicSartConfig,
    blocks: tuple[DynamicSartBlock, ...],
    *,
    smoke_test_override: bool,
) -> list[int] | None:
    if smoke_test_override:
        return None
    block_counts = [block.planned_no_go_count for block in blocks]
    if any(count is not None for count in block_counts):
        if any(count is None for count in block_counts):
            raise ValueError("dynamic_sart block-level planned_no_go_count must be configured for every block")
        counts = [int(count) for count in block_counts if count is not None]
        if config.planned_no_go_count is not None and sum(counts) != config.planned_no_go_count:
            raise ValueError("dynamic_sart block-level no-go counts do not match planned_no_go_count")
        return counts
    if config.planned_no_go_count is None:
        return None
    return _distribute_no_go_count(config, blocks, config.planned_no_go_count)


def _apply_cue_schedule(
    config: DynamicSartConfig,
    trials: list[dict[str, Any]],
    *,
    smoke_test_override: bool,
) -> dict[str, Any]:
    cue = dict(config.cue_schedule)
    if not bool(cue.get("enabled", False)) or smoke_test_override:
        return {"enabled": False, "opportunities": []}
    interval = int(cue["opportunity_every_trials"])
    run_in = int(cue.get("run_in_trials", 0))
    block_size = int(cue["randomization_block_size"])
    cues_per_block = int(cue["cues_per_randomization_block"])
    opportunity_trials = list(range(run_in + interval, len(trials) + 1, interval))
    rng = random.Random(_derived_seed(config.master_seed, 0, "cue_schedule"))
    assignments: list[str] = []
    for _start in range(0, len(opportunity_trials), block_size):
        block = ["cue"] * cues_per_block + ["no_cue"] * (block_size - cues_per_block)
        rng.shuffle(block)
        assignments.extend(block)
    opportunities = []
    for opportunity_index, (trial_index, assignment) in enumerate(
        zip(opportunity_trials, assignments),
        start=1,
    ):
        randomization_block = (opportunity_index - 1) // block_size + 1
        row = trials[trial_index - 1]
        row.update(
            {
                "cue_opportunity": True,
                "cue_opportunity_index": opportunity_index,
                "cue_randomization_block_index": randomization_block,
                "cue_assignment": assignment,
            }
        )
        opportunities.append(
            {
                "opportunity_index": opportunity_index,
                "trial_index": trial_index,
                "randomization_block_index": randomization_block,
                "assignment": assignment,
            }
        )
    return {
        "enabled": True,
        "run_in_trials": run_in,
        "opportunity_every_trials": interval,
        "randomization_block_size": block_size,
        "cues_per_randomization_block": cues_per_block,
        "opportunities": opportunities,
    }


def _spaced_positions(
    length: int,
    count: int,
    minimum_go_gap: int,
    rng: random.Random,
    minimum_leading_go_trials: int = 0,
    minimum_trailing_go_trials: int = 0,
) -> list[int]:
    if count <= 0:
        return []
    first_allowed = minimum_leading_go_trials
    stop = length - minimum_trailing_go_trials
    eligible = stop - first_allowed
    compressed_length = eligible - minimum_go_gap * (count - 1)
    if compressed_length < count:
        raise ValueError("dynamic_sart no-go spacing is infeasible for the requested block")
    compressed = sorted(rng.sample(range(compressed_length), count))
    return [
        first_allowed + position + index * minimum_go_gap
        for index, position in enumerate(compressed)
    ]


def _no_go_positions(
    length: int,
    count: int,
    config: DynamicSartConfig,
    rng: random.Random,
) -> tuple[list[int], list[int] | None]:
    randomization = dict(config.no_go_randomization)
    mode = str(randomization.get("mode", "uniform_constrained"))
    if mode == "uniform_constrained":
        return (
            _spaced_positions(
                length,
                count,
                config.minimum_go_trials_between_no_go,
                rng,
                config.minimum_leading_go_trials,
                config.minimum_trailing_go_trials,
            ),
            None,
        )
    if mode != "stratified_weighted":
        raise ValueError(f"unsupported dynamic_sart no-go randomization mode {mode}")
    stratum_trials = int(randomization["stratum_trials"])
    stratum_lengths = [
        min(stratum_trials, length - start)
        for start in range(0, length, stratum_trials)
    ]
    if length % stratum_trials == 0:
        # Preserve the established deterministic full-session sequence exactly.
        base, extra = divmod(count, len(stratum_lengths))
        counts = [base + 1] * extra + [base] * (len(stratum_lengths) - extra)
        rng.shuffle(counts)
    else:
        counts = _proportional_stratum_counts(stratum_lengths, count, rng)
    positions = _weighted_stratified_positions(
        length=length,
        stratum_lengths=stratum_lengths,
        stratum_no_go_counts=counts,
        minimum_go_gap=config.minimum_go_trials_between_no_go,
        minimum_leading_go_trials=config.minimum_leading_go_trials,
        minimum_trailing_go_trials=config.minimum_trailing_go_trials,
        maximum_consecutive_no_go=int(randomization["maximum_consecutive_no_go"]),
        adjacent_no_go_weight=float(randomization["adjacent_no_go_weight"]),
        one_go_gap_weight=float(randomization["one_go_gap_weight"]),
        rng=rng,
    )
    return positions, counts


def _weighted_stratified_positions(
    *,
    length: int,
    stratum_lengths: list[int],
    stratum_no_go_counts: list[int],
    minimum_go_gap: int,
    minimum_leading_go_trials: int,
    minimum_trailing_go_trials: int,
    maximum_consecutive_no_go: int,
    adjacent_no_go_weight: float,
    one_go_gap_weight: float,
    rng: random.Random,
) -> list[int]:
    """Sample an exact-count schedule while softly discouraging close no-go trials."""

    no_go_weights = (adjacent_no_go_weight, one_go_gap_weight, 1.0)
    stratum_starts: list[int] = []
    stratum_stops: list[int] = []
    cursor = 0
    for stratum_length in stratum_lengths:
        stratum_starts.append(cursor)
        cursor += stratum_length
        stratum_stops.append(cursor)
    if cursor != length or len(stratum_lengths) != len(stratum_no_go_counts):
        raise ValueError("dynamic_sart weighted no-go strata do not cover the block")

    def stratum_for_trial(trial_index: int) -> int:
        return next(
            index
            for index, stop in enumerate(stratum_stops)
            if trial_index < stop
        )

    @lru_cache(maxsize=None)
    def suffix_weight(
        trial_index: int,
        used_in_stratum: int,
        go_since_no_go: int,
        consecutive_no_go: int,
    ) -> float:
        if trial_index == length:
            return 1.0 if used_in_stratum == stratum_no_go_counts[-1] else 0.0
        stratum_index = stratum_for_trial(trial_index)
        local_index = trial_index - stratum_starts[stratum_index]
        required = stratum_no_go_counts[stratum_index]
        remaining_slots = stratum_lengths[stratum_index] - local_index
        remaining_no_go = required - used_in_stratum
        if remaining_no_go < 0 or remaining_no_go > remaining_slots:
            return 0.0

        next_trial = trial_index + 1
        boundary = next_trial == stratum_stops[stratum_index]
        go_used = used_in_stratum
        go_branch = 0.0
        if not boundary or go_used == required:
            next_used = 0 if boundary and next_trial < length else go_used
            go_branch = suffix_weight(
                next_trial,
                next_used,
                min(2, go_since_no_go + 1),
                0,
            )

        no_go_branch = 0.0
        no_go_allowed = (
            trial_index >= minimum_leading_go_trials
            and trial_index < length - minimum_trailing_go_trials
            and used_in_stratum < required
            and go_since_no_go >= minimum_go_gap
            and consecutive_no_go < maximum_consecutive_no_go
        )
        if no_go_allowed:
            no_go_used = used_in_stratum + 1
            if not boundary or no_go_used == required:
                next_used = 0 if boundary and next_trial < length else no_go_used
                no_go_branch = no_go_weights[min(2, go_since_no_go)] * suffix_weight(
                    next_trial,
                    next_used,
                    0,
                    consecutive_no_go + 1,
                )
        return go_branch + no_go_branch

    total = suffix_weight(0, 0, 2, 0)
    if total <= 0.0:
        raise ValueError("dynamic_sart weighted no-go randomization constraints are infeasible")

    positions: list[int] = []
    trial_index = 0
    used_in_stratum = 0
    go_since_no_go = 2
    consecutive_no_go = 0
    while trial_index < length:
        stratum_index = stratum_for_trial(trial_index)
        required = stratum_no_go_counts[stratum_index]
        next_trial = trial_index + 1
        boundary = next_trial == stratum_stops[stratum_index]

        go_weight = 0.0
        if not boundary or used_in_stratum == required:
            next_used = 0 if boundary and next_trial < length else used_in_stratum
            go_weight = suffix_weight(
                next_trial,
                next_used,
                min(2, go_since_no_go + 1),
                0,
            )

        no_go_weight = 0.0
        no_go_used = used_in_stratum + 1
        no_go_allowed = (
            trial_index >= minimum_leading_go_trials
            and trial_index < length - minimum_trailing_go_trials
            and used_in_stratum < required
            and go_since_no_go >= minimum_go_gap
            and consecutive_no_go < maximum_consecutive_no_go
        )
        if no_go_allowed and (not boundary or no_go_used == required):
            next_used = 0 if boundary and next_trial < length else no_go_used
            no_go_weight = no_go_weights[min(2, go_since_no_go)] * suffix_weight(
                next_trial,
                next_used,
                0,
                consecutive_no_go + 1,
            )

        choose_no_go = no_go_weight > 0.0 and rng.random() * (go_weight + no_go_weight) >= go_weight
        if choose_no_go:
            positions.append(trial_index)
            used_in_stratum = no_go_used
            go_since_no_go = 0
            consecutive_no_go += 1
        else:
            go_since_no_go = min(2, go_since_no_go + 1)
            consecutive_no_go = 0
        trial_index = next_trial
        if boundary and trial_index < length:
            used_in_stratum = 0
    return positions


def _proportional_stratum_counts(
    stratum_lengths: list[int],
    count: int,
    rng: random.Random,
) -> list[int]:
    """Allocate exact no-go counts without overloading a short final stratum."""

    total_length = sum(stratum_lengths)
    if total_length <= 0:
        raise ValueError("dynamic_sart no-go strata must contain trials")
    exact = [count * length / total_length for length in stratum_lengths]
    counts = [int(value) for value in exact]
    remaining = count - sum(counts)
    tie_breakers = list(range(len(stratum_lengths)))
    rng.shuffle(tie_breakers)
    tie_rank = {index: rank for rank, index in enumerate(tie_breakers)}
    order = sorted(
        range(len(stratum_lengths)),
        key=lambda index: (-(exact[index] - counts[index]), tie_rank[index]),
    )
    for index in order[:remaining]:
        counts[index] += 1
    return counts


def _validate_no_go_randomization_block(
    rows: list[dict[str, Any]],
    config: DynamicSartConfig,
    block: dict[str, Any],
) -> None:
    randomization = dict(config.no_go_randomization)
    if str(randomization.get("mode", "uniform_constrained")) != "stratified_weighted":
        return
    maximum_consecutive = int(randomization["maximum_consecutive_no_go"])
    run = 0
    for row in rows:
        run = run + 1 if bool(row["is_no_go"]) else 0
        if run > maximum_consecutive:
            raise ValueError(
                f"dynamic_sart block {block['block_name']} violates the consecutive no-go limit"
            )
    strata: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        stratum_index = int(row.get("no_go_stratum_index") or 0)
        strata.setdefault(stratum_index, []).append(row)
    for stratum_index, stratum_rows in strata.items():
        planned_count = stratum_rows[0].get("planned_no_go_count_in_stratum")
        actual_count = sum(int(bool(row["is_no_go"])) for row in stratum_rows)
        if planned_count is None or actual_count != int(planned_count):
            raise ValueError(
                f"dynamic_sart block {block['block_name']} stratum {stratum_index} "
                "does not match its planned no-go count"
            )


def _maximum_spaced_events(
    length: int,
    minimum_go_gap: int,
    minimum_leading_go_trials: int = 0,
    minimum_trailing_go_trials: int = 0,
) -> int:
    eligible = length - minimum_leading_go_trials - minimum_trailing_go_trials
    if eligible <= 0:
        return 0
    return (eligible + minimum_go_gap) // (minimum_go_gap + 1)


def _distribute_no_go_count(
    config: DynamicSartConfig,
    blocks: tuple[DynamicSartBlock, ...],
    total: int,
) -> list[int]:
    capacities = [
        _maximum_spaced_events(
            block.trials,
            config.minimum_go_trials_between_no_go,
            config.minimum_leading_go_trials,
            config.minimum_trailing_go_trials,
        )
        for block in blocks
    ]
    counts = [0 for _block in blocks]
    order = list(range(len(blocks)))
    random.Random(_derived_seed(config.master_seed, 0, "no_go_distribution")).shuffle(order)
    remaining = int(total)
    while remaining > 0:
        progressed = False
        for index in order:
            if counts[index] >= capacities[index]:
                continue
            minimum = min(counts[candidate] for candidate in order if counts[candidate] < capacities[candidate])
            if counts[index] > minimum:
                continue
            counts[index] += 1
            remaining -= 1
            progressed = True
            if remaining == 0:
                break
        if not progressed:
            raise ValueError("dynamic_sart planned_no_go_count cannot be distributed across blocks")
    if any(count < 1 for count in counts):
        raise ValueError("dynamic_sart planned_no_go_count must allocate at least one no-go trial per block")
    return counts


def _derived_seed(master_seed: int, block_index: int, name: str) -> int:
    digest = hashlib.sha256(f"{master_seed}:{block_index}:{name}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _marker_token(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value).strip().replace(" ", "-").replace("__", "-").replace("=", "-")
