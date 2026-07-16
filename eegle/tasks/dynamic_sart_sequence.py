"""Deterministic, behavior-independent planning for Dynamic-State SART."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import replace
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
    explicit_no_go_counts = (
        None
        if smoke_test_override or config.planned_no_go_count is None
        else _distribute_no_go_count(config, blocks, config.planned_no_go_count)
    )

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
            planned_offset += config.response_window_seconds + float(trial["planned_jitter_seconds"])
            planned_trials.append(trial)
        block_payload = block.payload(block_index)
        block_payload["block_seed"] = block_seed
        block_payload["first_global_trial_index"] = global_trial - block.trials + 1
        block_payload["last_global_trial_index"] = global_trial
        block_payload["planned_no_go_count"] = sum(int(row["is_no_go"]) for row in block_trials)
        planned_blocks.append(block_payload)

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
        if no_go_positions[0] <= config.minimum_leading_go_trials:
            raise ValueError(f"dynamic_sart block {block['block_name']} violates the leading-go constraint")
        if no_go_positions[-1] > len(rows) - config.minimum_trailing_go_trials:
            raise ValueError(f"dynamic_sart block {block['block_name']} violates the trailing-go constraint")
        gap = config.minimum_go_trials_between_no_go
        if any(right - left - 1 < gap for left, right in zip(no_go_positions, no_go_positions[1:])):
            raise ValueError(f"dynamic_sart block {block['block_name']} violates no-go spacing")
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
    no_go_count = (
        max(1, int(round(block.trials * config.no_go_probability)))
        if no_go_count_override is None
        else int(no_go_count_override)
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
    no_go_positions = set(
        _spaced_positions(
            block.trials,
            no_go_count,
            config.minimum_go_trials_between_no_go,
            rng,
            config.minimum_leading_go_trials,
            config.minimum_trailing_go_trials,
        )
    )
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
        rows.append(
            {
                "schema": PLAN_SCHEMA,
                "task_name": TASK_NAME,
                "task_version": TASK_VERSION,
                "block_index": block_index,
                "block_trial_index": zero_index + 1,
                "block_name": block.name,
                "phase": block.phase,
                "regime": "standard",
                "is_practice": False,
                "master_seed": config.master_seed,
                "block_seed": block_seed,
                "digit": digit,
                "condition": "no_go" if is_no_go else "go",
                "is_no_go": is_no_go,
                "expected_action": "withhold" if is_no_go else "press",
                "planned_stimulus_seconds": config.stimulus_seconds,
                "planned_response_window_seconds": config.response_window_seconds,
                "planned_jitter_seconds": rng.uniform(
                    config.inter_trial_jitter_min_seconds,
                    config.inter_trial_jitter_max_seconds,
                ),
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
    candidates = range(first_allowed, stop)
    for _attempt in range(20000):
        positions = sorted(rng.sample(candidates, count))
        if all(right - left - 1 >= minimum_go_gap for left, right in zip(positions, positions[1:])):
            return positions
    # A deterministic evenly spaced fallback avoids an unbounded random search.
    final_allowed = stop - 1
    positions = [
        first_allowed + int(round(index * (final_allowed - first_allowed) / max(1, count - 1)))
        for index in range(count)
    ]
    if all(right - left - 1 >= minimum_go_gap for left, right in zip(positions, positions[1:])):
        return positions
    raise ValueError("dynamic_sart no-go spacing is infeasible for the requested block")


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
