"""Versioned protocol helpers for the Study 1 Dynamic SART experiment."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from eegle.protocols.spec import ProtocolTarget, ScientificProtocol
from eegle.tasks.dynamic_sart_schema import DynamicSartConfig
from eegle.tasks.dynamic_sart_sequence import build_dynamic_sart_plan, validate_dynamic_sart_plan


STUDY1_PROTOCOL_NAME = "study1_dynamic_sart_v1"
STUDY1_SEGMENTS = ("session1_main", "session2_main", "session2_cue_extension")
STUDY1_STANDARD_ACQUISITION_PROFILE = "proposal_standard_v1"
STUDY1_FULL_1000_ACQUISITION_PROFILE = "full_1000_support500_query500_v2"
STUDY1_ACQUISITION_PROFILES = (
    STUDY1_STANDARD_ACQUISITION_PROFILE,
    STUDY1_FULL_1000_ACQUISITION_PROFILE,
)


def apply_study1_full_1000_profile(config: dict[str, Any]) -> dict[str, Any]:
    """Return a Study 1 Visit 1 config with four leakage-safe 250-trial sections."""

    result = copy.deepcopy(config)
    study = result.setdefault("study1", {})
    study["acquisition_profile"] = STUDY1_FULL_1000_ACQUISITION_PROFILE
    segment = study.setdefault("segments", {}).setdefault("session1_main", {})
    segment.update(
        {
            "practice_enabled": True,
            "practice_ready_confirmation": True,
            "blocks": [
                {
                    "name": "full_section_1_support",
                    "phase": "support",
                    "study_segment": "session1_main",
                    "trials": 250,
                    "planned_no_go_count": 38,
                    "break_after": True,
                    "minimum_break_seconds": 30.0,
                    "maximum_break_seconds": 60.0,
                },
                {
                    "name": "full_section_2_support",
                    "phase": "support",
                    "study_segment": "session1_main",
                    "trials": 250,
                    "planned_no_go_count": 37,
                    "break_after": True,
                    "minimum_break_seconds": 30.0,
                    "maximum_break_seconds": 60.0,
                },
                {
                    "name": "full_section_3_query",
                    "phase": "query",
                    "study_segment": "session1_main",
                    "trials": 250,
                    "planned_no_go_count": 38,
                    "break_after": True,
                    "minimum_break_seconds": 30.0,
                    "maximum_break_seconds": 60.0,
                },
                {
                    "name": "full_section_4_query",
                    "phase": "query",
                    "study_segment": "session1_main",
                    "trials": 250,
                    "planned_no_go_count": 37,
                    "break_after": False,
                },
            ],
            "cue_schedule": {"enabled": False},
        }
    )
    result.setdefault("tasks", {}).setdefault("dynamic_sart", {}).update(
        {
            "minimum_go_trials_between_no_go": 0,
            "minimum_leading_go_trials": 4,
            "minimum_trailing_go_trials": 4,
            "no_go_randomization": {
                "mode": "stratified_weighted",
                "stratum_trials": 50,
                "maximum_consecutive_no_go": 2,
                "adjacent_no_go_weight": 0.10,
                "one_go_gap_weight": 0.35,
            },
        }
    )
    suite = result.setdefault("recording_suite", {})
    suite["acquisition_profile"] = STUDY1_FULL_1000_ACQUISITION_PROFILE
    return result


def study1_protocol(
    acquisition_profile: str | None = None,
) -> ScientificProtocol:
    """Return the machine-readable scientific declaration used by the acquisition pipeline."""
    full_1000 = acquisition_profile == STUDY1_FULL_1000_ACQUISITION_PROFILE
    metadata = {
        "observe_only": True,
        "allow_task_adaptation": False,
        "allow_stimulation": False,
        "visit_interval_days": [2, 7],
        "digits": list(range(10)),
        "no_go_fraction_per_block": 0.15,
        "block_trials": 250 if full_1000 else 200,
        "stimulus_seconds": 0.25,
        "post_digit_fixation_seconds": 1.35,
        "soi_seconds": 1.60,
        "intentional_jitter_seconds": 0.0,
        "segments": list(STUDY1_SEGMENTS),
    }
    if full_1000:
        metadata.update(
            {
                "acquisition_profile": STUDY1_FULL_1000_ACQUISITION_PROFILE,
                "break_after_trials": [250, 500, 750],
                "no_go_randomization": {
                    "mode": "stratified_weighted",
                    "stratum_trials": 50,
                    "maximum_consecutive_no_go": 2,
                    "adjacent_no_go_weight": 0.10,
                    "one_go_gap_weight": 0.35,
                    "minimum_leading_go_trials": 4,
                    "minimum_trailing_go_trials": 4,
                },
            }
        )
    return ScientificProtocol(
        name=STUDY1_PROTOCOL_NAME,
        task="dynamic_sart",
        primary_endpoint="cross_session_attention_lapse_prediction",
        prediction_window_seconds=(-0.75, -0.10),
        prediction_horizon="same_trial_response",
        targets=(
            ProtocolTarget(
                "attention_lapse_binary",
                positive=("slow_go_rt", "omission_error", "commission_error"),
                metadata={
                    "support_trials": 500 if full_1000 else 200,
                    "query_trials": 500 if full_1000 else 400,
                },
            ),
        ),
        splits=(
            (
                "visit1_trials_1_500_support_501_1000_query",
                "practice_excluded",
            )
            if full_1000
            else ("visit_1_visit_2", "session2_support_query", "practice_excluded")
        ),
        baselines=("behavior_only", "eeg_residual"),
        metrics=("brier_score", "auprc", "roc_auc", "ece"),
        metadata=metadata,
    )


def configure_study1_segment(
    config: dict[str, Any],
    segment_name: str,
    *,
    no_go_digit: int,
    seed: int,
    smoke: bool = False,
    include_practice: bool = False,
) -> dict[str, Any]:
    """Return a child-session config using the shared Dynamic SART engine."""
    if segment_name not in STUDY1_SEGMENTS:
        raise ValueError(f"unknown Study 1 segment {segment_name}")
    result = copy.deepcopy(config)
    study = dict(result.get("study1") or {})
    segments = dict(study.get("segments") or {})
    segment = dict(segments.get(segment_name) or {})
    if not segment:
        raise ValueError(f"study1.segments.{segment_name} is missing")
    blocks = [dict(block) for block in segment.get("blocks") or []]
    if smoke:
        blocks = _smoke_blocks(blocks, segment_name)
    task = result.setdefault("tasks", {}).setdefault("dynamic_sart", {})
    task.update(
        {
            "digits": list(range(10)),
            "no_go_digit": int(no_go_digit),
            "no_go_probability": 0.15,
            "planned_no_go_count": sum(int(block["planned_no_go_count"]) for block in blocks),
            "master_seed": int(seed),
            "blocks": blocks,
            "cue_schedule": _cue_schedule(segment, smoke=smoke),
            "allow_task_adaptation": False,
            "allow_stimulation": False,
        }
    )
    if smoke:
        # Ten-trial smoke blocks cannot use the formal 50-trial strata. Smoke
        # validates orchestration only; recorded Study 1 segments retain the
        # versioned weighted-stratified schedule below.
        task["no_go_randomization"] = {}
    task.setdefault("practice", {})["enabled"] = bool(
        segment.get("practice_enabled", False) and (include_practice or not smoke)
    )
    require_ready_confirmation = bool(
        segment.get("practice_ready_confirmation", False) and task["practice"]["enabled"]
    )
    if require_ready_confirmation:
        task["practice"]["require_ready_confirmation"] = True
    else:
        task["practice"].pop("require_ready_confirmation", None)
    suite = result.setdefault("recording_suite", {})
    suite["recipe"] = "study1"
    suite["study_segment"] = segment_name
    suite["trials_per_session"] = sum(int(block["trials"]) for block in blocks)
    suite.setdefault("session_2", {})["repeat_practice"] = bool(task["practice"]["enabled"])
    result.setdefault("study1", {})["active_segment"] = segment_name
    result["study1"]["active_segment_seed"] = int(seed)
    result["study1"]["assigned_no_go_digit"] = int(no_go_digit)
    return result


def validate_study1_config(config: dict[str, Any]) -> list[dict[str, str]]:
    """Validate proposal-defining invariants without requiring live hardware."""
    issues: list[dict[str, str]] = []
    study = dict(config.get("study1") or {})
    acquisition_profile = str(
        study.get("acquisition_profile") or STUDY1_STANDARD_ACQUISITION_PROFILE
    )
    if acquisition_profile not in STUDY1_ACQUISITION_PROFILES:
        issues.append(
            _issue(
                "fail",
                "study1.acquisition_profile must be one of "
                + ", ".join(STUDY1_ACQUISITION_PROFILES),
            )
        )
    protocol = dict(study.get("protocol") or {})
    if str(protocol.get("name")) != STUDY1_PROTOCOL_NAME:
        issues.append(_issue("fail", f"study1.protocol.name must be {STUDY1_PROTOCOL_NAME}"))
    interval = list(study.get("visit_interval_days") or [])
    if interval != [2, 7]:
        issues.append(_issue("fail", "study1.visit_interval_days must be [2, 7]"))
    eeg = dict(config.get("hardware", {}).get("eeg", {}) or {})
    if str(eeg.get("family", "")).lower() != "neuracle":
        issues.append(_issue("fail", "Study 1 hardware.eeg.family must be Neuracle"))
    if eeg.get("profile") != "neuracle64" or list(eeg.get("expected_channel_counts") or []) != [65]:
        issues.append(
            _issue(
                "fail",
                "Study 1 requires the Neuracle W64 profile transported as 65 LSL values",
            )
        )
    if float(eeg.get("expected_sample_rate_hz", 0.0)) != 1000.0:
        issues.append(_issue("fail", "Study 1 requires a 1000 Hz EEG sampling rate"))
    channel_names = list(eeg.get("expected_channel_names") or [])
    if not channel_names:
        issues.append(
            _issue(
                "warn",
                "Neuracle W64 65-value LSL names/order remain unlocked; live preflight will fail until confirmed",
            )
        )
    else:
        if len(channel_names) != 65 or channel_names[-1] != "TRIGGER_STATUS":
            issues.append(
                _issue(
                    "fail",
                    "Study 1 requires 64 physical input labels followed by TRIGGER_STATUS",
                )
            )
        if len(list(eeg.get("electrode_channel_names") or [])) != 64:
            issues.append(_issue("fail", "Study 1 requires exactly 64 physical input labels"))
        channel_types = list(eeg.get("expected_channel_types") or [])
        if len(channel_types) != 65 or str(channel_types[-1]).lower() != "stim":
            issues.append(_issue("fail", "Study 1 value 65 must have channel type stim"))
    recorder = dict(config.get("processes", {}).get("recorder", {}) or {})
    if not bool(recorder.get("enabled", False)) or recorder.get("backend") != "labrecorder_xdf":
        issues.append(_issue("fail", "Study 1 requires the managed labrecorder_xdf backend"))
    if bool(recorder.get("csv_mirror", False)):
        issues.append(_issue("fail", "Study 1 full CSV mirror must remain disabled during XDF acquisition"))
    if not bool(recorder.get("lsl_sample_heartbeat", False)):
        issues.append(_issue("fail", "Study 1 requires the non-writing live LSL sample heartbeat"))
    if bool(eeg.get("abort_on_timestamp_gap", False)):
        issues.append(_issue("fail", "Study 1 timestamp gaps must warn without aborting acquisition"))
    if not str(recorder.get("executable", "")).strip():
        issues.append(_issue("fail", "Study 1 processes.recorder.executable must be configured"))
    try:
        rcs_port = int(recorder.get("rcs_port", 0))
    except (TypeError, ValueError):
        rcs_port = 0
    if not 1 <= rcs_port <= 65535:
        issues.append(_issue("fail", "Study 1 processes.recorder.rcs_port must be between 1 and 65535"))
    cue_delivery = (
        dict(study.get("segments", {}).get("session2_cue_extension", {}))
        .get("cue_schedule", {})
        .get("delivery_mode")
    )
    if cue_delivery == "assignment_only":
        issues.append(
            _issue(
                "warn",
                "cue assignments and markers are implemented, but physical auditory delivery remains gated",
            )
        )
    base_task = dict(config.get("tasks", {}).get("dynamic_sart", {}) or {})
    if list(base_task.get("digits") or []) != list(range(10)):
        issues.append(_issue("fail", "Study 1 Dynamic SART digits must be 0 through 9"))
    if abs(float(base_task.get("stimulus_seconds", 0.0)) - 0.25) > 1e-9:
        issues.append(_issue("fail", "Study 1 digit duration must be 0.25 seconds"))
    if abs(float(base_task.get("response_window_seconds", 0.0)) - 1.60) > 1e-9:
        issues.append(_issue("fail", "Study 1 SOI must be 1.60 seconds (0.25 s digit + 1.35 s fixation)"))
    if any(
        abs(float(base_task.get(name, 0.0))) > 1e-9
        for name in ("inter_trial_jitter_min_seconds", "inter_trial_jitter_max_seconds")
    ):
        issues.append(_issue("fail", "Study 1 intentional inter-trial jitter must be disabled"))
    for name in ("soi_min_seconds", "soi_max_seconds"):
        if base_task.get(name) is not None and abs(float(base_task[name]) - 1.60) > 1e-9:
            issues.append(_issue("fail", f"Study 1 {name} must be fixed at 1.60 seconds"))
    display = dict(config.get("hardware", {}).get("display", {}) or {})
    if not bool(display.get("wait_blanking", False)):
        issues.append(_issue("fail", "Study 1 display must wait for VBlank"))
    if not bool(display.get("check_refresh_rate", False)):
        issues.append(_issue("fail", "Study 1 display refresh-rate measurement must be enabled"))
    if not bool(display.get("require_refresh_rate_match", False)):
        issues.append(_issue("fail", "Study 1 measured refresh rate must match the configured display mode"))
    if list(display.get("supported_refresh_rates_hz") or []) != [60.0, 120.0]:
        issues.append(_issue("fail", "Study 1 display modes must be restricted to measured 60 Hz or 120 Hz"))
    if float(display.get("refresh_rate_tolerance_hz", float("inf"))) > 2.0:
        issues.append(_issue("fail", "Study 1 display refresh tolerance must be no greater than 2 Hz"))
    if float(display.get("refresh_rate_stability_threshold_ms", float("inf"))) > 1.0:
        issues.append(
            _issue("fail", "Study 1 display refresh stability threshold must be no greater than 1 ms")
        )
    if int(display.get("refresh_rate_measurement_attempts", 0)) < 3:
        issues.append(_issue("fail", "Study 1 display refresh measurement must allow at least three attempts"))
    if int(display.get("refresh_rate_sample_frames", 0)) < 60:
        issues.append(_issue("fail", "Study 1 display refresh measurement must sample at least 60 frames"))
    if float(display.get("refresh_rate_window_settle_seconds", 0.0)) < 1.0:
        issues.append(
            _issue("fail", "Study 1 display refresh measurement must allow the window to settle")
        )
    if str(display.get("keyboard_backend", "")).lower() != "ptb":
        issues.append(_issue("fail", "Study 1 requires PsychoPy's asynchronous PTB keyboard backend"))
    if not bool(display.get("capture_keyboard_outside_window", False)):
        issues.append(
            _issue(
                "fail",
                "Study 1 must retain PTB keyboard responses while the task window is unfocused",
            )
        )
    for name in STUDY1_SEGMENTS:
        try:
            child = configure_study1_segment(config, name, no_go_digit=0, seed=42)
            parsed = DynamicSartConfig.from_mapping(child["tasks"]["dynamic_sart"])
            plan = build_dynamic_sart_plan(parsed)
            validate_dynamic_sart_plan(plan, parsed)
            issues.extend(_segment_issues(name, plan, acquisition_profile=acquisition_profile))
            expected_randomization = {
                "mode": "stratified_weighted",
                "stratum_trials": 50,
                "maximum_consecutive_no_go": 2,
                "adjacent_no_go_weight": 0.10,
                "one_go_gap_weight": 0.35,
            }
            if parsed.no_go_randomization != expected_randomization:
                issues.append(
                    _issue(
                        "fail",
                        "Study 1 must use the versioned weighted-stratified no-go randomization",
                    )
                )
            if (
                parsed.minimum_go_trials_between_no_go,
                parsed.minimum_leading_go_trials,
                parsed.minimum_trailing_go_trials,
            ) != (0, 4, 4):
                issues.append(
                    _issue(
                        "fail",
                        "Study 1 no-go randomization must allow rare close trials with four-go block boundaries",
                    )
                )
            if (
                acquisition_profile == STUDY1_FULL_1000_ACQUISITION_PROFILE
                and name == "session1_main"
            ):
                if not parsed.practice_enabled:
                    issues.append(_issue("fail", "full 1000 profile must enable practice"))
                if not parsed.practice_require_ready_confirmation:
                    issues.append(
                        _issue(
                            "fail",
                            "full 1000 profile must require participant confirmation after practice",
                        )
                    )
                if (
                    parsed.practice_trials_per_round,
                    parsed.practice_no_go_trials,
                    parsed.practice_max_rounds,
                ) != (30, 4, 3):
                    issues.append(
                        _issue(
                            "fail",
                            "full 1000 practice must use 30 trials, 4 no-go trials, and at most 3 rounds",
                        )
                    )
        except (KeyError, TypeError, ValueError) as exc:
            issues.append(_issue("fail", f"Study 1 segment {name} is invalid: {exc}"))
    return issues


def study1_protocol_hash(config: dict[str, Any]) -> str:
    """Hash the scientific protocol without operational acquisition settings.

    Display probes, recorder timeouts, executable paths, and resting-baseline
    duration are visit operations. They remain persisted in the visit and child
    artifacts, but changing them must not create a new participant-level
    scientific protocol identity. Exact prepared and recorded task-sequence
    hashes provide the stricter resume gate for an incomplete visit.
    """

    acquisition_profile = str(
        config.get("study1", {}).get("acquisition_profile")
        or STUDY1_STANDARD_ACQUISITION_PROFILE
    )
    study = copy.deepcopy(config.get("study1") or {})
    study.pop("visits", None)
    payload = {
        "hash_contract": "study1_scientific_protocol_v2",
        "declaration": study1_protocol(
            acquisition_profile
            if acquisition_profile == STUDY1_FULL_1000_ACQUISITION_PROFILE
            else None
        ).payload(),
        "study1": study,
        "dynamic_sart": config.get("tasks", {}).get("dynamic_sart"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def deterministic_pilot_no_go_digit(participant_id: str, master_seed: int) -> int:
    """Provide a stable dry-run assignment; scientific runs require an explicit allocation."""
    digest = hashlib.sha256(f"{master_seed}:{participant_id}:study1-no-go".encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big", signed=False) % 10


def _segment_issues(
    name: str,
    plan: dict[str, Any],
    *,
    acquisition_profile: str,
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    rows = list(plan.get("planned_trials") or [])
    if (
        acquisition_profile == STUDY1_FULL_1000_ACQUISITION_PROFILE
        and name == "session1_main"
    ):
        return _full_1000_segment_issues(plan)
    expected_trials = 400 if name == "session2_cue_extension" else 600
    if len(rows) != expected_trials:
        issues.append(_issue("fail", f"{name} must contain {expected_trials} trials"))
    for block in plan.get("planned_blocks") or []:
        if int(block.get("trials", 0)) != 200 or int(block.get("planned_no_go_count", 0)) != 30:
            issues.append(_issue("fail", f"{name} blocks must contain 200 trials and 30 no-go trials"))
            break
    if any(abs(float(row.get("planned_soi_seconds", 0.0)) - 1.60) > 1e-9 for row in rows):
        issues.append(_issue("fail", f"{name} SOIs must remain fixed at 1.60 seconds"))
    cue = dict(plan.get("cue_schedule") or {})
    if name == "session2_cue_extension":
        opportunities = list(cue.get("opportunities") or [])
        if len(opportunities) != 20:
            issues.append(_issue("fail", "session2_cue_extension must contain 20 cue opportunities"))
        for start in range(0, len(opportunities), 4):
            assignments = [row.get("assignment") for row in opportunities[start : start + 4]]
            if assignments.count("cue") != 2 or assignments.count("no_cue") != 2:
                issues.append(_issue("fail", "each cue randomization block must contain two cue and two no-cue assignments"))
                break
    elif bool(cue.get("enabled", False)):
        issues.append(_issue("fail", f"{name} must remain cue-free"))
    return issues


def _full_1000_segment_issues(plan: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    rows = list(plan.get("planned_trials") or [])
    blocks = list(plan.get("planned_blocks") or [])
    if len(rows) != 1000:
        issues.append(_issue("fail", "full 1000 session1_main must contain exactly 1000 trials"))
    if [int(block.get("trials", 0)) for block in blocks] != [250, 250, 250, 250]:
        issues.append(_issue("fail", "full 1000 session1_main must contain four 250-trial sections"))
    if [str(block.get("phase")) for block in blocks] != ["support", "support", "query", "query"]:
        issues.append(
            _issue(
                "fail",
                "full 1000 session1_main must use 500 leading support trials then 500 query trials",
            )
        )
    if [int(block.get("planned_no_go_count", 0)) for block in blocks] != [38, 37, 38, 37]:
        issues.append(
            _issue(
                "fail",
                "full 1000 sections must allocate no-go counts [38, 37, 38, 37]",
            )
        )
    if [bool(block.get("break_after", False)) for block in blocks] != [True, True, True, False]:
        issues.append(_issue("fail", "full 1000 session1_main must break after trials 250, 500, and 750"))
    for block in blocks[:3]:
        if (
            abs(float(block.get("minimum_break_seconds", 0.0)) - 30.0) > 1e-9
            or abs(float(block.get("maximum_break_seconds", 0.0)) - 60.0) > 1e-9
        ):
            issues.append(_issue("fail", "full 1000 breaks must allow continuation from 30 to 60 seconds"))
            break
    if sum(int(block.get("planned_no_go_count", 0)) for block in blocks) != 150:
        issues.append(_issue("fail", "full 1000 session1_main must contain exactly 150 no-go trials"))
    condition_sequences = [
        tuple(bool(row.get("is_no_go")) for row in rows if int(row.get("block_index", 0)) == index)
        for index in range(1, 5)
    ]
    if condition_sequences[0] == condition_sequences[2] or condition_sequences[1] == condition_sequences[3]:
        issues.append(_issue("fail", "full 1000 sections must use independently randomized no-go sequences"))
    stratum_counts = [
        [
            sum(int(bool(row.get("is_no_go"))) for row in section[start : start + 50])
            for start in range(0, len(section), 50)
        ]
        for section in (
            [row for row in rows if int(row.get("block_index", 0)) == index]
            for index in range(1, 5)
        )
    ]
    if any(sorted(counts) not in ([7, 7, 8, 8, 8], [7, 7, 7, 8, 8]) for counts in stratum_counts):
        issues.append(_issue("fail", "full 1000 sections must balance no-go trials across 50-trial strata"))
    if any(abs(float(row.get("planned_soi_seconds", 0.0)) - 1.60) > 1e-9 for row in rows):
        issues.append(_issue("fail", "full 1000 session1_main SOIs must remain fixed at 1.60 seconds"))
    if bool(dict(plan.get("cue_schedule") or {}).get("enabled", False)):
        issues.append(_issue("fail", "full 1000 session1_main must remain cue-free"))
    return issues


def _smoke_blocks(blocks: list[dict[str, Any]], segment_name: str) -> list[dict[str, Any]]:
    result = []
    for index, original in enumerate(blocks, start=1):
        block = dict(original)
        block["name"] = f"{segment_name}_smoke_{index}"
        block["trials"] = 10
        block["planned_no_go_count"] = 1
        block["minimum_break_seconds"] = 0.0
        block["maximum_break_seconds"] = 0.0
        result.append(block)
    return result


def _cue_schedule(segment: dict[str, Any], *, smoke: bool) -> dict[str, Any]:
    cue = copy.deepcopy(segment.get("cue_schedule") or {"enabled": False})
    if smoke and bool(cue.get("enabled", False)):
        cue.update(
            {
                "run_in_trials": 0,
                "opportunity_every_trials": 5,
                "randomization_block_size": 4,
                "cues_per_randomization_block": 2,
            }
        )
    return cue


def _issue(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}
