"""Visit-aware acquisition pipeline for the Study 1 Dynamic SART protocol."""

from __future__ import annotations

import argparse
import copy
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from eegle.config import load_config, resolve_session_root
from eegle.devices.simulated_lsl import SimulatedEegOutlet
from eegle.hardware.neuracle import (
    NEURACLE_W64_AUX_CHANNELS,
    NEURACLE_W64_LSL_CHANNEL_COUNT,
    NEURACLE_W64_LSL_CHANNEL_TYPES,
    NEURACLE_W64_LSL_CHANNELS,
    NEURACLE_W64_PHYSIOLOGICAL_CHANNELS,
    NEURACLE_W64_TRIGGER_STATUS_CHANNEL,
)
from eegle.pipelines.dsart_recording import (
    DsartRecordingOptions,
    _accept_recording_preflight,
    _accept_post_recording_warnings,
    _complete_phase,
    _fail_phase,
    _latest_phase_result,
    _load_json,
    _now,
    _phase_is_complete,
    _probe_session_root_writable,
    _runtime_cache_root,
    _safe_token,
    _start_phase,
    _write_json_atomic,
    derive_session_seed,
    run_dsart_child_session,
    run_recording_preflight,
    run_resting_baseline,
)
from eegle.protocols.study1 import (
    STUDY1_FULL_1000_ACQUISITION_PROFILE,
    STUDY1_PROTOCOL_NAME,
    STUDY1_STANDARD_ACQUISITION_PROFILE,
    apply_study1_full_1000_profile,
    configure_study1_segment,
    deterministic_pilot_no_go_digit,
    study1_protocol_hash,
    validate_study1_config,
)
from eegle.tasks.dynamic_sart_schema import DynamicSartConfig
from eegle.tasks.dynamic_sart_sequence import build_dynamic_sart_plan, validate_dynamic_sart_plan


STUDY_SCHEMA = "eegle.study1.participant.v1"
VISIT_SCHEMA = "eegle.study1.visit.v1"
DEFAULT_CONFIG = Path("configs/study1_neuracle64.json")
VISIT_PHASES = {
    1: ("preflight", "baseline", "session1_main"),
    2: ("preflight", "baseline", "session2_main", "session2_cue_extension"),
}
SEGMENT_INDEX = {
    "session1_main": 1,
    "session2_main": 2,
    "session2_cue_extension": 3,
}
SIMULATED_NEURACLE64_STREAM_NAME = "EEGle-Neuracle64-Simulated"
SIMULATED_NEURACLE64_SOURCE_ID = "eegle-neuracle64-simulated-source"
SIMULATED_NEURACLE64_CHANNELS = NEURACLE_W64_LSL_CHANNELS


@dataclass(frozen=True)
class Study1Options:
    config_path: str | Path
    participant_id: str
    visit_number: int
    visit_id: str | None = None
    operator: str | None = None
    task_mode: str = "psychopy"
    master_seed: int = 42
    no_go_digit: int | None = None
    smoke: bool = False
    full_1000: bool = False
    include_practice: bool = False
    skip_practice: bool = False
    trials: int | None = None
    practice_trials: int | None = None
    practice_no_go_trials: int | None = None
    practice_max_rounds: int | None = None
    baseline_seconds: float | None = None
    skip_baseline: bool = False
    window_size: tuple[int, int] | None = None
    screen_index: int | None = None
    full_screen: bool | None = None
    record_eeg: bool = True
    require_eeg: bool = True
    resume: bool = False
    retry_incomplete: bool = False
    output_root: str | Path | None = None
    lsl_wait_seconds: float = 5.0
    electrode_quality_file: str | Path | None = None
    electrode_note: str | None = None
    electrodes_confirmed: bool = False
    allow_visit_interval_override: bool = False
    simulate_eeg: bool = False
    labrecorder_executable: str | Path | None = None
    preflight_only: bool = False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    options = _options_from_args(args)
    try:
        result = run_study1_visit(options)
    except Exception as exc:
        result = {
            "schema": VISIT_SCHEMA,
            "status": "failed",
            "participant_id": options.participant_id,
            "visit_number": options.visit_number,
            "error": f"{type(exc).__name__}: {exc}",
            "next_action": _exception_next_action(exc),
        }
    exit_code = 0 if result.get("status") == "completed" else 1
    result["process_exit_code"] = exit_code
    if exit_code:
        failures = list(result.get("failures") or [])
        failure_detail = result.get("failure_detail") or result.get("error")
        if failure_detail is None and failures:
            latest = failures[-1]
            failure_detail = latest.get("error") if isinstance(latest, dict) else str(latest)
        result["failure_detail"] = failure_detail or f"Study 1 status={result.get('status')}"
        result.setdefault("failed_phase", "startup_or_orchestration")
    if args.result_file:
        outcome_path = Path(args.result_file).expanduser().resolve()
        result["outcome_file"] = str(outcome_path)
        try:
            _write_json_atomic(outcome_path, result)
        except Exception as exc:
            # The console result and native exit code remain authoritative if
            # this additional launcher handshake cannot be persisted.
            result["outcome_file_error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="study1",
        description="Run one independently recorded visit of the Study 1 Dynamic SART protocol",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--participant", required=True)
    parser.add_argument("--visit", dest="visit_number", type=int, choices=[1, 2], required=True)
    parser.add_argument("--visit-id", default=None)
    parser.add_argument("--operator", default=None)
    parser.add_argument("--task-mode", choices=["psychopy", "dry-run"], default="psychopy")
    parser.add_argument("--master-seed", type=int, default=42)
    parser.add_argument(
        "--no-go-digit",
        type=int,
        choices=range(10),
        default=None,
        help="Counterbalanced participant allocation; required when creating a live participant",
    )
    parser.add_argument("--smoke", action="store_true", help="Run every visit phase with shortened block contracts")
    parser.add_argument(
        "--full-1000",
        action="store_true",
        help=(
            "Run the complete Visit 1 profile: four 250-trial sections, support trials 1-500, "
            "query trials 501-1000, and breaks after trials 250, 500, and 750"
        ),
    )
    practice_mode = parser.add_mutually_exclusive_group()
    practice_mode.add_argument("--include-practice", action="store_true")
    practice_mode.add_argument("--skip-practice", action="store_true")
    parser.add_argument(
        "--trials",
        type=int,
        default=None,
        help="Override the experimental trial count for each task segment",
    )
    parser.add_argument("--practice-trials", type=int, default=None)
    parser.add_argument("--practice-no-go-trials", type=int, default=None)
    parser.add_argument("--practice-max-rounds", type=int, default=None)
    baseline = parser.add_mutually_exclusive_group()
    baseline.add_argument(
        "--baseline-seconds",
        type=float,
        default=None,
        help="Set the duration of each resting-baseline condition in seconds",
    )
    baseline.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Record no resting-baseline session and advance directly to the task after preflight",
    )
    parser.add_argument("--window-size", type=int, nargs=2, metavar=("WIDTH", "HEIGHT"), default=None)
    parser.add_argument(
        "--screen-index",
        type=int,
        default=None,
        help="Open each PsychoPy window on this zero-based monitor; active-monitor checks follow later moves",
    )
    display_mode = parser.add_mutually_exclusive_group()
    display_mode.add_argument("--fullscreen", dest="full_screen", action="store_true")
    display_mode.add_argument("--windowed", dest="full_screen", action="store_false")
    parser.set_defaults(full_screen=None)
    parser.add_argument("--skip-eeg", action="store_true")
    parser.add_argument("--allow-missing-eeg", action="store_true")
    parser.add_argument(
        "--simulate-eeg",
        action="store_true",
        help=(
            "Publish a clearly labeled synthetic Neuracle W64 65-value LSL stream while exercising "
            "the real recorder; development-only and valid only with --task-mode dry-run"
        ),
    )
    parser.add_argument(
        "--labrecorder-executable",
        default=None,
        help="Override processes.recorder.executable for this visit",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run the full Neuracle/LSL/channel/sample/electrode/LabRecorder gate without creating a visit",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--retry-incomplete",
        action="store_true",
        help=(
            "Continue this participant's incomplete visit automatically, reusing its visit ID, "
            "skipping completed phases, and rerunning the failed phase"
        ),
    )
    parser.add_argument("--session-root", "--output-root", dest="output_root", default=None)
    parser.add_argument(
        "--result-file",
        default=None,
        help=(
            "Atomically write the final Study 1 result for an external launcher; "
            "the experiment data and visit manifest remain in the session root"
        ),
    )
    parser.add_argument("--lsl-wait", type=float, default=5.0)
    parser.add_argument("--electrode-quality-file", default=None)
    parser.add_argument("--electrode-note", default=None)
    parser.add_argument("--confirm-electrodes", action="store_true")
    parser.add_argument(
        "--allow-visit-interval-override",
        action="store_true",
        help="Pilot-only override for running Visit 2 outside the configured 2-7 day interval",
    )
    return parser


def _options_from_args(args: argparse.Namespace) -> Study1Options:
    return Study1Options(
        config_path=args.config,
        participant_id=str(args.participant),
        visit_number=int(args.visit_number),
        visit_id=args.visit_id,
        operator=args.operator,
        task_mode=str(args.task_mode),
        master_seed=int(args.master_seed),
        no_go_digit=args.no_go_digit,
        smoke=bool(args.smoke),
        full_1000=bool(args.full_1000),
        include_practice=bool(args.include_practice),
        skip_practice=bool(args.skip_practice),
        trials=args.trials,
        practice_trials=args.practice_trials,
        practice_no_go_trials=args.practice_no_go_trials,
        practice_max_rounds=args.practice_max_rounds,
        baseline_seconds=args.baseline_seconds,
        skip_baseline=bool(args.skip_baseline),
        window_size=None if args.window_size is None else (int(args.window_size[0]), int(args.window_size[1])),
        screen_index=args.screen_index,
        full_screen=args.full_screen,
        record_eeg=not bool(args.skip_eeg),
        require_eeg=not bool(args.allow_missing_eeg) and not bool(args.skip_eeg),
        resume=bool(args.resume),
        retry_incomplete=bool(args.retry_incomplete),
        output_root=args.output_root,
        lsl_wait_seconds=float(args.lsl_wait),
        electrode_quality_file=args.electrode_quality_file,
        electrode_note=args.electrode_note,
        electrodes_confirmed=bool(args.confirm_electrodes),
        allow_visit_interval_override=bool(args.allow_visit_interval_override),
        simulate_eeg=bool(args.simulate_eeg),
        labrecorder_executable=args.labrecorder_executable,
        preflight_only=bool(args.preflight_only),
    )


def run_study1_visit(options: Study1Options) -> dict[str, Any]:
    _validate_options(options)
    config = copy.deepcopy(load_config(options.config_path))
    if options.full_1000:
        config = apply_study1_full_1000_profile(config)
    config_issues = validate_study1_config(config)
    failures = [issue["detail"] for issue in config_issues if issue["status"] == "fail"]
    if failures:
        raise ValueError("Study 1 configuration invalid: " + "; ".join(failures))
    protocol_hash = study1_protocol_hash(config)
    if options.labrecorder_executable is not None:
        config.setdefault("processes", {}).setdefault("recorder", {})["executable"] = str(
            Path(options.labrecorder_executable).expanduser().resolve()
        )
    if options.simulate_eeg:
        _configure_simulated_eeg_rehearsal(config)
        config_issues.append(
            {
                "status": "warn",
                "detail": "synthetic EEG rehearsal enabled; generated XDF is not participant data",
            }
        )
    output_root = resolve_session_root(config, options.output_root)
    config.setdefault("runtime", {})["session_root"] = str(output_root)
    config["runtime"]["runtime_cache_dir"] = str(_runtime_cache_root(config, output_root))
    if options.window_size is not None:
        config.setdefault("hardware", {}).setdefault("display", {})["size"] = list(options.window_size)
    if options.screen_index is not None:
        config.setdefault("hardware", {}).setdefault("display", {})["screen_index"] = int(
            options.screen_index
        )
    if options.full_screen is not None:
        config.setdefault("hardware", {}).setdefault("display", {})["full_screen"] = bool(options.full_screen)
    _probe_session_root_writable(output_root)

    simulator: SimulatedEegOutlet | None = None
    if options.simulate_eeg:
        simulator = _start_simulated_eeg_rehearsal(config)
    try:
        if options.preflight_only:
            return _run_study1_preflight_only(options, config, output_root)
        return _run_configured_study1_visit(options, config, config_issues, protocol_hash, output_root)
    finally:
        if simulator is not None:
            simulator.close()


def _run_study1_preflight_only(
    options: Study1Options,
    config: dict[str, Any],
    output_root: Path,
) -> dict[str, Any]:
    _validate_live_readiness(config, options)
    check_id = _safe_token(
        options.visit_id or datetime.now().strftime("preflight-%Y%m%dT%H%M%S")
    )
    output_dir = (
        output_root
        / "system_checks"
        / "study1_neuracle64"
        / _safe_token(options.participant_id)
        / check_id
    )
    report = run_recording_preflight(
        config,
        recipe="study1",
        participant_id=options.participant_id,
        visit_id=check_id,
        phase="preflight_only",
        output_dir=output_dir,
        require_eeg=True,
        record_eeg=True,
        lsl_wait_seconds=options.lsl_wait_seconds,
        electrode_quality_file=options.electrode_quality_file,
        electrode_note=options.electrode_note,
        electrodes_confirmed=options.electrodes_confirmed,
        initial_preflight=None,
        require_display=options.task_mode == "psychopy",
    )
    if report.get("status") not in {"pass", "warning"} or report.get("failures"):
        return {
            "schema": VISIT_SCHEMA,
            "status": "failed",
            "mode": "preflight_only",
            "participant_id": options.participant_id,
            "visit_number": options.visit_number,
            "visit_id": check_id,
            "preflight_status": report.get("status"),
            "report_file": report.get("report_file"),
            "electrode_quality_file": report.get("electrode_quality_file"),
            "channel_contract": report.get("channel_contract"),
            "failures": list(report.get("failures") or [f"unexpected preflight status={report.get('status')}"]),
            "warnings": list(report.get("warnings") or []),
        }
    _accept_recording_preflight(report, _dsart_options(options, trials=10))
    return {
        "schema": VISIT_SCHEMA,
        "status": "completed",
        "mode": "preflight_only",
        "participant_id": options.participant_id,
        "visit_number": options.visit_number,
        "visit_id": check_id,
        "preflight_status": report.get("status"),
        "report_file": report.get("report_file"),
        "electrode_quality_file": report.get("electrode_quality_file"),
        "channel_contract": report.get("channel_contract"),
        "failures": list(report.get("failures") or []),
        "warnings": list(report.get("warnings") or []),
    }


def _run_configured_study1_visit(
    options: Study1Options,
    config: dict[str, Any],
    config_issues: list[dict[str, str]],
    protocol_hash: str,
    output_root: Path,
) -> dict[str, Any]:

    participant_dir = output_root / "study1" / _safe_token(options.participant_id)
    participant_manifest_path = participant_dir / "participant_manifest.json"
    participant_manifest = _participant_manifest(
        participant_manifest_path,
        options,
        protocol_hash=protocol_hash,
    )
    assigned_no_go_digit = int(participant_manifest["no_go_digit"])
    _validate_visit_slot(participant_manifest, options)
    config.setdefault("tasks", {}).setdefault("dynamic_sart", {})["no_go_digit"] = assigned_no_go_digit
    _apply_visit_baseline(config, options)
    _validate_live_readiness(config, options)
    interval = _visit_interval_check(participant_manifest, options)

    visit_id = _resolve_visit_id(participant_dir, participant_manifest, options)
    visit_dir = participant_dir / "visits" / f"visit-{options.visit_number}" / visit_id
    visit_manifest_path = visit_dir / "visit_manifest.json"
    if visit_manifest_path.exists() and not _continues_incomplete_visit(options):
        raise FileExistsError(
            "Study 1 visit already exists. Rerun the operator script unchanged to retry an "
            f"incomplete test, or use --resume explicitly: {visit_manifest_path}"
        )
    visit_dir.mkdir(parents=True, exist_ok=True)
    visit_manifest_exists = visit_manifest_path.exists()
    manifest = _load_json(visit_manifest_path) if visit_manifest_exists else None
    if visit_manifest_exists and manifest is None:
        raise ValueError(f"Study 1 visit manifest must contain a JSON object: {visit_manifest_path}")
    if manifest is not None:
        options = _resume_options_with_persisted_task_shape(options, manifest)
    segment_seeds = {
        phase: derive_session_seed(options.participant_id, phase, SEGMENT_INDEX[phase], options.master_seed)
        for phase in VISIT_PHASES[options.visit_number]
        if phase in SEGMENT_INDEX
    }
    prepared_sequences = _prepare_visit_sequences(
        config,
        options,
        assigned_no_go_digit=assigned_no_go_digit,
        segment_seeds=segment_seeds,
    )
    if manifest is not None:
        _validate_resume(
            manifest,
            options,
            visit_id,
            protocol_hash,
            assigned_no_go_digit,
            segment_seeds,
            config,
            prepared_sequences,
        )
    else:
        manifest = _new_visit_manifest(
            options,
            visit_id,
            visit_dir,
            protocol_hash,
            assigned_no_go_digit,
            segment_seeds,
            config_issues,
            interval,
            config,
        )
        _write_json_atomic(visit_manifest_path, manifest)
    manifest.setdefault("prepared_sequence_hashes", prepared_sequences)
    _write_json_atomic(visit_manifest_path, manifest)
    _update_participant_visit(
        participant_manifest,
        participant_manifest_path,
        options.visit_number,
        visit_id,
        visit_manifest_path,
        status=str(manifest.get("status", "planned")),
    )

    active_preflight = _latest_phase_result(manifest, "preflight")
    for phase in VISIT_PHASES[options.visit_number]:
        if _phase_is_complete(manifest, phase):
            if _continues_incomplete_visit(options):
                action = "retry_skipped_completed" if options.retry_incomplete else "skipped_completed"
                manifest.setdefault("resumed_phases", []).append({"phase": phase, "action": action})
                _write_json_atomic(visit_manifest_path, manifest)
            if phase == "preflight":
                active_preflight = _latest_phase_result(manifest, phase)
            continue
        _start_phase(manifest, phase)
        _write_json_atomic(visit_manifest_path, manifest)
        result: dict[str, Any] | None = None
        try:
            dsart_options = _dsart_options(options, trials=10)
            if phase == "preflight":
                result = run_recording_preflight(
                    config,
                    recipe="study1",
                    participant_id=options.participant_id,
                    visit_id=visit_id,
                    phase=f"visit_{options.visit_number}_preflight",
                    output_dir=visit_dir / "preflight",
                    require_eeg=options.require_eeg,
                    record_eeg=options.record_eeg,
                    lsl_wait_seconds=options.lsl_wait_seconds,
                    electrode_quality_file=options.electrode_quality_file,
                    electrode_note=options.electrode_note,
                    electrodes_confirmed=options.electrodes_confirmed,
                    initial_preflight=None,
                    require_display=options.task_mode == "psychopy",
                )
                active_preflight = result
                if result.get("status") not in {"pass", "warning"} or result.get("failures"):
                    raise RuntimeError(_phase_error("Study 1 preflight", result))
                _accept_recording_preflight(result, dsart_options)
            elif phase == "baseline":
                if active_preflight is None:
                    raise RuntimeError("Study 1 baseline cannot start without preflight")
                if _baseline_is_skipped(config):
                    result = _skipped_baseline_result(config)
                else:
                    result = run_resting_baseline(
                        config,
                        dsart_options,
                        visit_id=visit_id,
                        preflight=active_preflight,
                    )
                    if result.get("status") != "completed":
                        raise RuntimeError(_phase_error("Study 1 baseline", result))
                    _accept_post_recording_warnings(result, dsart_options, "Study 1 baseline")
                    manifest.setdefault("session_directories", {})["baseline"] = result.get("session_dir")
            else:
                if active_preflight is None:
                    raise RuntimeError(f"{phase} cannot start without preflight")
                if phase == "session2_cue_extension" and options.task_mode == "psychopy":
                    delivery = config["study1"]["segments"][phase]["cue_schedule"].get("delivery_mode")
                    if delivery == "assignment_only":
                        raise RuntimeError(
                            "physical cue delivery is intentionally gated; assignment markers are implemented, "
                            "but the auditory delivery contract is not yet locked"
                        )
                child_config = _configure_study1_child(
                    config,
                    phase,
                    options,
                    no_go_digit=assigned_no_go_digit,
                    seed=segment_seeds[phase],
                )
                configured_trials = sum(
                    int(block["trials"])
                    for block in child_config["tasks"]["dynamic_sart"]["blocks"]
                )
                trials = int(options.trials or configured_trials)
                dsart_options = _dsart_options(options, trials=trials)
                result = run_dsart_child_session(
                    child_config,
                    dsart_options,
                    visit_id=visit_id,
                    session_index=SEGMENT_INDEX[phase],
                    seed=segment_seeds[phase],
                    preflight=active_preflight,
                )
                result["study_segment"] = phase
                if result.get("status") != "completed":
                    raise RuntimeError(_phase_error(phase, result))
                _accept_post_recording_warnings(result, dsart_options, phase)
                manifest.setdefault("session_directories", {})[phase] = result.get("session_dir")
                manifest.setdefault("sequence_hashes", {})[phase] = result.get("sequence_hash")
            contract_failures = _study1_phase_result_failures(phase, result, manifest)
            if contract_failures:
                raise RuntimeError(
                    f"{phase} completion contract failed: " + "; ".join(contract_failures)
                )
            _complete_phase(manifest, phase, result)
            _write_json_atomic(visit_manifest_path, manifest)
        except KeyboardInterrupt as exc:
            _fail_phase(manifest, phase, exc, result)
            manifest["status"] = "partial" if _has_recording(manifest) else "aborted"
            manifest["overall_status"] = manifest["status"]
            manifest["visit_end"] = _now()
            _write_json_atomic(visit_manifest_path, manifest)
            _update_participant_visit(
                participant_manifest,
                participant_manifest_path,
                options.visit_number,
                visit_id,
                visit_manifest_path,
                status=manifest["status"],
            )
            return _public_result(manifest, visit_manifest_path)
        except Exception as exc:
            _fail_phase(manifest, phase, exc, result)
            manifest["status"] = "partial" if _has_recording(manifest) else "failed"
            manifest["overall_status"] = manifest["status"]
            manifest.setdefault("failures", []).append(
                {"phase": phase, "error": f"{type(exc).__name__}: {exc}", "timestamp": _now()}
            )
            manifest["visit_end"] = _now()
            _write_json_atomic(visit_manifest_path, manifest)
            _update_participant_visit(
                participant_manifest,
                participant_manifest_path,
                options.visit_number,
                visit_id,
                visit_manifest_path,
                status=manifest["status"],
            )
            return _public_result(manifest, visit_manifest_path)

    completion_failures = _study1_visit_completion_failures(manifest)
    if completion_failures:
        for phase, failures in completion_failures.items():
            entry = dict(manifest.get("phases", {}).get(phase, {}) or {})
            attempts = list(entry.get("attempts") or [])
            latest_result = dict(attempts[-1].get("result") or {}) if attempts else {}
            phase_status = "partial" if latest_result.get("session_dir") else "failed"
            manifest["phases"][phase]["status"] = phase_status
            if attempts:
                manifest["phases"][phase]["attempts"][-1]["status"] = phase_status
                manifest["phases"][phase]["attempts"][-1]["completion_audit_error"] = "; ".join(failures)
            manifest.setdefault("failures", []).append(
                {
                    "phase": phase,
                    "error": "completion audit failed: " + "; ".join(failures),
                    "timestamp": _now(),
                }
            )
        manifest["status"] = "partial" if _has_recording(manifest) else "failed"
        manifest["overall_status"] = manifest["status"]
        manifest["visit_end"] = _now()
        _write_json_atomic(visit_manifest_path, manifest)
        _update_participant_visit(
            participant_manifest,
            participant_manifest_path,
            options.visit_number,
            visit_id,
            visit_manifest_path,
            status=manifest["status"],
        )
        return _public_result(manifest, visit_manifest_path)

    manifest["status"] = "completed"
    manifest["overall_status"] = "completed"
    manifest["visit_end"] = _now()
    _archive_resolved_failures(manifest, resolved_at=manifest["visit_end"])
    _write_json_atomic(visit_manifest_path, manifest)
    _update_participant_visit(
        participant_manifest,
        participant_manifest_path,
        options.visit_number,
        visit_id,
        visit_manifest_path,
        status="completed",
        completed_at=manifest["visit_end"],
    )
    return _public_result(manifest, visit_manifest_path)


def _configure_simulated_eeg_rehearsal(config: dict[str, Any]) -> None:
    if len(SIMULATED_NEURACLE64_CHANNELS) != NEURACLE_W64_LSL_CHANNEL_COUNT:
        raise RuntimeError("internal simulated Neuracle W64 LSL contract must contain exactly 65 values")
    eeg = config.setdefault("hardware", {}).setdefault("eeg", {})
    eeg.update(
        {
            "expected_channel_counts": [NEURACLE_W64_LSL_CHANNEL_COUNT],
            "expected_channel_names": list(SIMULATED_NEURACLE64_CHANNELS),
            "expected_channel_types": list(NEURACLE_W64_LSL_CHANNEL_TYPES),
            "electrode_channel_names": list(NEURACLE_W64_PHYSIOLOGICAL_CHANNELS),
            "analysis_excluded_channel_names": [
                *NEURACLE_W64_AUX_CHANNELS,
                NEURACLE_W64_TRIGGER_STATUS_CHANNEL,
            ],
            "quality_excluded_channel_names": [NEURACLE_W64_TRIGGER_STATUS_CHANNEL],
            "expected_sample_rate_hz": 1000,
            "lsl_stream_type": "EEG",
            "lsl_name_patterns": [SIMULATED_NEURACLE64_STREAM_NAME.lower()],
            "allow_type_only_fallback": False,
            "required_for_run": True,
            "mapping_source": "simulated_neuracle64_rehearsal_contract",
            "reference": "CPz (rehearsal assumption; verify on live hardware)",
            "ground": "AFz (rehearsal assumption; verify on live hardware)",
            "eog_allocation": "ECG, HEOR, HEOL, VEOU, VEOL (simulated auxiliary order)",
            "embedded_trigger_channel": {
                "index": 65,
                "name": NEURACLE_W64_TRIGGER_STATUS_CHANNEL,
                "role": "reserved_trigger_status",
                "observed_without_trigger": "empty",
            },
            "simulated": True,
            "data_classification": "synthetic_rehearsal_not_participant_data",
        }
    )
    config["recording_rehearsal"] = {
        "enabled": True,
        "simulated_eeg": True,
        "not_participant_data": True,
        "stream_name": SIMULATED_NEURACLE64_STREAM_NAME,
        "source_id": SIMULATED_NEURACLE64_SOURCE_ID,
        "channel_order_source": "user-supplied Neuracle W64 rehearsal description",
        "auxiliary_order_is_assumption": True,
        "marker_discovery_settle_seconds": 2.0,
        "marker_receipt_drain_seconds": 0.5,
    }


def _start_simulated_eeg_rehearsal(config: dict[str, Any]) -> SimulatedEegOutlet:
    eeg = dict(config.get("hardware", {}).get("eeg", {}) or {})
    simulator = SimulatedEegOutlet(
        name=SIMULATED_NEURACLE64_STREAM_NAME,
        stream_type=str(eeg.get("lsl_stream_type", "EEG")),
        channel_count=NEURACLE_W64_LSL_CHANNEL_COUNT,
        sample_rate_hz=float(eeg.get("expected_sample_rate_hz", 1000.0)),
        channel_names=SIMULATED_NEURACLE64_CHANNELS,
        source_id=SIMULATED_NEURACLE64_SOURCE_ID,
    )
    simulator.start(timeout=float(eeg.get("stream_timeout_seconds", 5.0)) + 1.0)
    config.setdefault("recording_rehearsal", {})["outlet_start"] = simulator.snapshot()
    return simulator


def _recording_rehearsal_identity(config: dict[str, Any]) -> dict[str, Any] | None:
    rehearsal = config.get("recording_rehearsal")
    if not isinstance(rehearsal, dict):
        return None
    identity = copy.deepcopy(rehearsal)
    identity.pop("outlet_start", None)
    return identity


def _validate_options(options: Study1Options) -> None:
    if not options.participant_id.strip():
        raise ValueError("--participant must be nonempty")
    if options.visit_number not in VISIT_PHASES:
        raise ValueError("--visit must be 1 or 2")
    if options.record_eeg and not options.require_eeg:
        raise ValueError("Study 1 cannot record EEG while allowing the required stream to be missing")
    if options.simulate_eeg and options.task_mode != "dry-run":
        raise ValueError("--simulate-eeg is development-only and requires --task-mode dry-run")
    if options.simulate_eeg and not options.record_eeg:
        raise ValueError("--simulate-eeg cannot be combined with --skip-eeg")
    if options.simulate_eeg and not options.require_eeg:
        raise ValueError("--simulate-eeg cannot be combined with --allow-missing-eeg")
    if options.preflight_only and (not options.record_eeg or not options.require_eeg):
        raise ValueError("--preflight-only requires EEG; do not combine it with --skip-eeg or --allow-missing-eeg")
    if options.preflight_only and options.resume:
        raise ValueError("--preflight-only cannot be combined with --resume")
    if options.preflight_only and options.retry_incomplete:
        raise ValueError("--preflight-only cannot be combined with --retry-incomplete")
    if options.preflight_only and options.simulate_eeg:
        raise ValueError("--preflight-only is for the physical EEG system and cannot use --simulate-eeg")
    if options.full_1000 and options.visit_number != 1:
        raise ValueError("--full-1000 is a Visit 1 acquisition profile")
    if options.full_1000 and options.smoke:
        raise ValueError("--full-1000 cannot be combined with --smoke")
    if options.full_1000 and options.preflight_only:
        raise ValueError("--full-1000 cannot be combined with --preflight-only")
    if options.resume and options.retry_incomplete:
        raise ValueError("choose either --resume or --retry-incomplete, not both")
    if options.include_practice and options.skip_practice:
        raise ValueError("choose either --include-practice or --skip-practice, not both")
    if options.baseline_seconds is not None and options.skip_baseline:
        raise ValueError("choose either --baseline-seconds or --skip-baseline, not both")
    if options.trials is not None and options.trials < 10:
        raise ValueError("--trials must be at least 10")
    if options.practice_trials is not None and options.practice_trials < 10:
        raise ValueError(
            "--practice-trials must be at least 10 so the leading/trailing go constraints fit"
        )
    if options.practice_no_go_trials is not None and options.practice_no_go_trials < 1:
        raise ValueError("--practice-no-go-trials must be at least 1")
    effective_practice_trials = options.practice_trials or 30
    if (
        options.practice_no_go_trials is not None
        and options.practice_no_go_trials >= effective_practice_trials
    ):
        raise ValueError("--practice-no-go-trials must be less than --practice-trials")
    if options.practice_max_rounds is not None and options.practice_max_rounds < 1:
        raise ValueError("--practice-max-rounds must be at least 1")
    if options.baseline_seconds is not None and options.baseline_seconds < 0:
        raise ValueError("--baseline-seconds must be nonnegative")
    if options.window_size is not None and any(value <= 0 for value in options.window_size):
        raise ValueError("--window-size values must be positive")
    if options.screen_index is not None and options.screen_index < 0:
        raise ValueError("--screen-index must be nonnegative")


def _participant_manifest(
    path: Path,
    options: Study1Options,
    *,
    protocol_hash: str,
) -> dict[str, Any]:
    existing = _load_json(path)
    if existing is not None:
        mismatches = []
        if existing.get("participant_id") != options.participant_id:
            mismatches.append("participant_id")
        if existing.get("protocol_hash") != protocol_hash:
            expected_profile = (
                STUDY1_FULL_1000_ACQUISITION_PROFILE
                if options.full_1000
                else STUDY1_STANDARD_ACQUISITION_PROFILE
            )
            visit = dict(existing.get("visits", {}).get(str(options.visit_number)) or {})
            compatible_incomplete_retry = bool(
                _continues_incomplete_visit(options)
                and visit
                and visit.get("status") != "completed"
                and existing.get("protocol_name") == STUDY1_PROTOCOL_NAME
                and existing.get("acquisition_profile") == expected_profile
            )
            if compatible_incomplete_retry:
                event = {
                    "recorded_protocol_hash": existing.get("protocol_hash"),
                    "current_protocol_hash": protocol_hash,
                    "reason": "incomplete_visit_retry_with_matching_versioned_acquisition_profile",
                    "timestamp": _now(),
                }
                events = existing.setdefault("protocol_hash_compatibility_events", [])
                if not any(
                    row.get("recorded_protocol_hash") == event["recorded_protocol_hash"]
                    and row.get("current_protocol_hash") == event["current_protocol_hash"]
                    for row in events
                ):
                    events.append(event)
                    _write_json_atomic(path, existing)
            else:
                mismatches.append("protocol_hash")
        if int(existing.get("master_seed", -1)) != options.master_seed:
            mismatches.append("master_seed")
        if options.no_go_digit is not None and int(existing.get("no_go_digit", -1)) != options.no_go_digit:
            mismatches.append("no_go_digit")
        if mismatches:
            raise ValueError("participant manifest does not match this run: " + ", ".join(mismatches))
        return existing
    if options.visit_number != 1:
        raise FileNotFoundError("Visit 1 must create the Study 1 participant manifest before Visit 2")
    if options.no_go_digit is None:
        if options.record_eeg and not options.simulate_eeg:
            raise ValueError("--no-go-digit is required when creating a live Study 1 participant")
        no_go_digit = deterministic_pilot_no_go_digit(options.participant_id, options.master_seed)
        allocation_method = "deterministic_dry_run"
    else:
        no_go_digit = int(options.no_go_digit)
        allocation_method = "explicit_counterbalance_allocation"
    manifest = {
        "schema": STUDY_SCHEMA,
        "protocol_name": STUDY1_PROTOCOL_NAME,
        "protocol_hash": protocol_hash,
        "acquisition_profile": (
            STUDY1_FULL_1000_ACQUISITION_PROFILE
            if options.full_1000
            else STUDY1_STANDARD_ACQUISITION_PROFILE
        ),
        "participant_id": options.participant_id,
        "master_seed": options.master_seed,
        "no_go_digit": no_go_digit,
        "allocation_method": allocation_method,
        "created_at": _now(),
        "visits": {},
    }
    _write_json_atomic(path, manifest)
    return manifest


def _apply_visit_baseline(config: dict[str, Any], options: Study1Options) -> None:
    visits = dict(config.get("study1", {}).get("visits") or {})
    visit = dict(visits.get(str(options.visit_number)) or {})
    baseline = dict(visit.get("baseline") or config.get("recording_suite", {}).get("baseline") or {})
    if options.skip_baseline:
        baseline = {
            "eyes_open_seconds": 0.0,
            "eyes_closed_seconds": 0.0,
            "skipped": True,
            "skip_reason": "operator_requested",
        }
    elif options.smoke and options.baseline_seconds is None:
        baseline = {"eyes_open_seconds": 0.0, "eyes_closed_seconds": 0.0}
    elif options.baseline_seconds is not None:
        baseline = {
            "eyes_open_seconds": float(options.baseline_seconds),
            "eyes_closed_seconds": float(options.baseline_seconds),
        }
    config.setdefault("recording_suite", {})["baseline"] = baseline


def _baseline_is_skipped(config: dict[str, Any]) -> bool:
    baseline = dict(config.get("recording_suite", {}).get("baseline", {}) or {})
    return bool(baseline.get("skipped", False))


def _skipped_baseline_result(config: dict[str, Any]) -> dict[str, Any]:
    baseline = copy.deepcopy(config.get("recording_suite", {}).get("baseline", {}) or {})
    return {
        "schema": "eegle.dsart_resting_baseline.v1",
        "status": "skipped",
        "skip_reason": str(baseline.get("skip_reason") or "operator_requested"),
        "baseline": baseline,
        "session_dir": None,
        "planned_duration_seconds": 0.0,
        "actual_duration_seconds": 0.0,
        "phases": [],
        "processes": {"status": "skipped", "processes": {}, "notes": []},
        "validation": {"status": "skipped", "failures": [], "warnings": []},
    }


def _validate_live_readiness(config: dict[str, Any], options: Study1Options) -> None:
    missing = []
    if options.record_eeg and not options.simulate_eeg:
        eeg = dict(config.get("hardware", {}).get("eeg", {}) or {})
        if len(list(eeg.get("expected_channel_names") or [])) != NEURACLE_W64_LSL_CHANNEL_COUNT:
            missing.append("the exact 65-value LSL names/order")
        if len(list(eeg.get("electrode_channel_names") or [])) != len(
            NEURACLE_W64_PHYSIOLOGICAL_CHANNELS
        ):
            missing.append("the exact 64 physical-input names/order")
        for key in ("reference", "ground", "eog_allocation"):
            if str(eeg.get(key, "")).startswith("pending") or not str(eeg.get(key, "")).strip():
                missing.append(key)
    if options.visit_number == 2 and options.task_mode == "psychopy":
        cue = dict(config.get("study1", {}).get("segments", {}).get("session2_cue_extension", {}).get("cue_schedule") or {})
        if cue.get("delivery_mode") == "assignment_only":
            missing.append("physical auditory cue delivery")
    if missing:
        raise ValueError("live Study 1 hardware contract is not locked: " + ", ".join(missing))


def _validate_visit_slot(participant: dict[str, Any], options: Study1Options) -> None:
    existing = dict(participant.get("visits", {}).get(str(options.visit_number)) or {})
    if not existing:
        return
    status = str(existing.get("status") or "unknown")
    if options.retry_incomplete:
        if status == "completed":
            raise FileExistsError(
                f"Study 1 Visit {options.visit_number} is already completed for participant "
                f"{options.participant_id!r}. Use a new participant/test ID for a new run."
            )
        if options.visit_id is not None and existing.get("visit_id") != _safe_token(options.visit_id):
            raise ValueError(
                "--visit-id does not match this participant's incomplete visit. Omit --visit-id "
                f"to retry automatically, or use {existing.get('visit_id')!r}."
            )
        return
    if not options.resume:
        raise FileExistsError(
            f"Study 1 Visit {options.visit_number} already has status={status}. Use "
            "--retry-incomplete to rerun the failed phase, or --resume with the original visit ID."
        )
    if options.visit_id is not None and existing.get("visit_id") != _safe_token(options.visit_id):
        raise ValueError("--visit-id does not match the participant manifest visit slot")


def _visit_interval_check(participant: dict[str, Any], options: Study1Options) -> dict[str, Any]:
    if options.visit_number == 1:
        return {"status": "not_applicable", "days": None, "override": False}
    existing_visit_two = dict(participant.get("visits", {}).get("2") or {})
    if _continues_incomplete_visit(options) and existing_visit_two.get("manifest_file"):
        existing_manifest = _load_json(Path(str(existing_visit_two["manifest_file"]))) or {}
        recorded = dict(existing_manifest.get("visit_interval") or {})
        if recorded:
            return {**recorded, "resume_reused_original_gate": True}
    visit_one = dict(participant.get("visits", {}).get("1") or {})
    if visit_one.get("status") != "completed":
        raise ValueError("Visit 2 requires a completed Visit 1")
    completed_at = visit_one.get("completed_at")
    if not completed_at:
        raise ValueError("Visit 1 completion timestamp is missing")
    elapsed_days = (datetime.now() - datetime.fromisoformat(str(completed_at))).total_seconds() / 86400.0
    valid = 2.0 <= elapsed_days <= 7.0
    if not valid and not options.allow_visit_interval_override:
        raise ValueError(f"Visit 2 is {elapsed_days:.2f} days after Visit 1; expected 2-7 days")
    return {
        "status": "pass" if valid else "override",
        "days": elapsed_days,
        "override": bool(not valid and options.allow_visit_interval_override),
    }


def _resolve_visit_id(
    participant_dir: Path,
    participant: dict[str, Any],
    options: Study1Options,
) -> str:
    if options.retry_incomplete:
        existing = dict(participant.get("visits", {}).get(str(options.visit_number)) or {})
        if existing and str(existing.get("status")) != "completed":
            existing_id = str(existing.get("visit_id") or "").strip()
            if not existing_id:
                raise ValueError("incomplete participant visit is missing its visit ID")
            return _safe_token(existing_id)
    if options.visit_id:
        return _safe_token(options.visit_id)
    visit_root = participant_dir / "visits" / f"visit-{options.visit_number}"
    if options.resume:
        candidates = sorted(
            (
                path
                for path in visit_root.glob("*/visit_manifest.json")
                if (_load_json(path) or {}).get("status") != "completed"
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0].parent.name
        raise FileNotFoundError("--resume could not find an incomplete Study 1 visit")
    return datetime.now().strftime(f"visit-{options.visit_number}-%Y%m%dT%H%M%S")


def _task_override_identity(options: Study1Options) -> dict[str, int | bool | None]:
    return {
        "experimental_trials": options.trials,
        "skip_practice": bool(options.skip_practice),
        "practice_trials_per_round": options.practice_trials,
        "practice_no_go_trials": options.practice_no_go_trials,
        "practice_max_rounds": options.practice_max_rounds,
    }


def _resume_options_with_persisted_task_shape(
    options: Study1Options,
    manifest: dict[str, Any],
) -> Study1Options:
    """Make the original task shape authoritative for a resumed visit.

    Baseline controls intentionally remain operator-selectable because a
    completed baseline is skipped by the phase ledger and an incomplete one may
    be retried or explicitly skipped. Experimental and practice shape cannot
    drift between task attempts, so resume reloads it from the visit manifest.
    """

    recorded = manifest.get("task_overrides")
    if not isinstance(recorded, dict):
        return options
    return replace(
        options,
        include_practice=bool(manifest.get("include_practice", options.include_practice)),
        skip_practice=bool(recorded.get("skip_practice", options.skip_practice)),
        trials=recorded.get("experimental_trials"),
        practice_trials=recorded.get("practice_trials_per_round"),
        practice_no_go_trials=recorded.get("practice_no_go_trials"),
        practice_max_rounds=recorded.get("practice_max_rounds"),
    )


def _new_visit_manifest(
    options: Study1Options,
    visit_id: str,
    visit_dir: Path,
    protocol_hash: str,
    no_go_digit: int,
    segment_seeds: dict[str, int],
    config_issues: list[dict[str, str]],
    interval: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    phases = VISIT_PHASES[options.visit_number]
    return {
        "schema": VISIT_SCHEMA,
        "protocol_name": STUDY1_PROTOCOL_NAME,
        "protocol_hash": protocol_hash,
        "participant_id": options.participant_id,
        "visit_number": options.visit_number,
        "visit_id": visit_id,
        "visit_start": _now(),
        "visit_end": None,
        "status": "planned",
        "overall_status": "planned",
        "operator": options.operator or os.environ.get("USER") or os.environ.get("USERNAME") or "unspecified",
        "no_go_digit": no_go_digit,
        "master_seed": options.master_seed,
        "segment_seeds": segment_seeds,
        "task_mode": options.task_mode,
        "record_eeg": options.record_eeg,
        "require_eeg": options.require_eeg,
        "simulate_eeg": options.simulate_eeg,
        "recording_rehearsal": _recording_rehearsal_identity(config),
        "smoke": options.smoke,
        "full_1000": options.full_1000,
        "acquisition_profile": str(
            config.get("study1", {}).get("acquisition_profile")
            or STUDY1_STANDARD_ACQUISITION_PROFILE
        ),
        "include_practice": options.include_practice,
        "skip_practice": options.skip_practice,
        "task_overrides": _task_override_identity(options),
        "baseline": copy.deepcopy(config.get("recording_suite", {}).get("baseline", {})),
        "window_size": list(config.get("hardware", {}).get("display", {}).get("size", [1000, 700])),
        "configured_screen_index": int(
            config.get("hardware", {}).get("display", {}).get("screen_index", 0)
        ),
        "full_screen": bool(config.get("hardware", {}).get("display", {}).get("full_screen", False)),
        "visit_interval": interval,
        "warnings": [issue["detail"] for issue in config_issues if issue["status"] == "warn"],
        "failures": [],
        "resumed_phases": [],
        "session_directories": {},
        "sequence_hashes": {},
        "visit_directory": str(visit_dir),
        "config_path": str(Path(options.config_path).expanduser().resolve()),
        "phase_order": list(phases),
        "phases": {phase: {"status": "planned", "attempts": []} for phase in phases},
    }


def _validate_resume(
    manifest: dict[str, Any],
    options: Study1Options,
    visit_id: str,
    protocol_hash: str,
    no_go_digit: int,
    segment_seeds: dict[str, int],
    config: dict[str, Any],
    prepared_sequences: dict[str, str],
) -> None:
    expected = {
        "participant_id": options.participant_id,
        "visit_number": options.visit_number,
        "visit_id": visit_id,
        "no_go_digit": no_go_digit,
        "master_seed": options.master_seed,
        "segment_seeds": segment_seeds,
        "task_mode": options.task_mode,
        "record_eeg": options.record_eeg,
        "require_eeg": options.require_eeg,
        "simulate_eeg": options.simulate_eeg,
        "recording_rehearsal": _recording_rehearsal_identity(config),
        "smoke": options.smoke,
        "include_practice": options.include_practice,
        "window_size": list(config.get("hardware", {}).get("display", {}).get("size", [1000, 700])),
        "full_screen": bool(config.get("hardware", {}).get("display", {}).get("full_screen", False)),
    }
    if options.full_1000:
        expected.update(
            {
                "full_1000": True,
                "acquisition_profile": STUDY1_FULL_1000_ACQUISITION_PROFILE,
            }
        )
    mismatches = [key for key, value in expected.items() if manifest.get(key) != value]
    recorded_overrides = manifest.get("task_overrides")
    if isinstance(recorded_overrides, dict) and recorded_overrides != _task_override_identity(options):
        mismatches.append("task_overrides")
    if manifest.get("protocol_hash") != protocol_hash:
        expected_profile = (
            STUDY1_FULL_1000_ACQUISITION_PROFILE
            if options.full_1000
            else STUDY1_STANDARD_ACQUISITION_PROFILE
        )
        if (
            manifest.get("protocol_name") != STUDY1_PROTOCOL_NAME
            or manifest.get("acquisition_profile") != expected_profile
        ):
            mismatches.append("protocol_hash")
    for phase, recorded_hash in dict(manifest.get("prepared_sequence_hashes") or {}).items():
        if prepared_sequences.get(phase) != recorded_hash:
            mismatches.append(f"prepared_sequence_hashes.{phase}")
    for phase, recorded_hash in dict(manifest.get("sequence_hashes") or {}).items():
        if prepared_sequences.get(phase) != recorded_hash:
            mismatches.append(f"sequence_hashes.{phase}")
    if mismatches:
        raise ValueError(
            "resume identity does not match existing Study 1 visit: "
            + ", ".join(dict.fromkeys(mismatches))
        )


def _prepare_visit_sequences(
    config: dict[str, Any],
    options: Study1Options,
    *,
    assigned_no_go_digit: int,
    segment_seeds: dict[str, int],
) -> dict[str, str]:
    """Build every actual seeded segment before any recorder process starts."""

    prepared: dict[str, str] = {}
    for phase, seed in segment_seeds.items():
        child = _configure_study1_child(
            config,
            phase,
            options,
            no_go_digit=assigned_no_go_digit,
            seed=seed,
        )
        parsed = DynamicSartConfig.from_mapping(child["tasks"]["dynamic_sart"])
        # The child runner always supplies its effective trial count explicitly.
        # Include that same value in the plan hash basis so the prepared and
        # recorded sequence identities are exactly comparable.
        plan = build_dynamic_sart_plan(
            parsed,
            trial_override=int(options.trials or parsed.normal_recipe_trial_count),
        )
        validate_dynamic_sart_plan(plan, parsed)
        prepared[phase] = str(plan["sequence_id"])
    return prepared


def _configure_study1_child(
    config: dict[str, Any],
    phase: str,
    options: Study1Options,
    *,
    no_go_digit: int,
    seed: int,
) -> dict[str, Any]:
    child = configure_study1_segment(
        config,
        phase,
        no_go_digit=no_go_digit,
        seed=seed,
        smoke=options.smoke,
        include_practice=options.include_practice,
    )
    practice = child.setdefault("tasks", {}).setdefault("dynamic_sart", {}).setdefault(
        "practice", {}
    )
    if options.skip_practice:
        practice["enabled"] = False
        practice.pop("require_ready_confirmation", None)
    if options.practice_trials is not None:
        practice["trials_per_round"] = int(options.practice_trials)
    if options.practice_no_go_trials is not None:
        practice["no_go_trials"] = int(options.practice_no_go_trials)
    elif options.practice_trials is not None:
        practice["no_go_trials"] = min(
            int(options.practice_trials) - 1,
            max(1, round(int(options.practice_trials) * 0.15)),
        )
    if options.practice_max_rounds is not None:
        practice["max_rounds"] = int(options.practice_max_rounds)
    return child


def _dsart_options(options: Study1Options, *, trials: int) -> DsartRecordingOptions:
    return DsartRecordingOptions(
        recipe="study1",
        config_path=options.config_path,
        participant_id=options.participant_id,
        visit_id=options.visit_id,
        operator=options.operator,
        task_mode=options.task_mode,
        master_seed=options.master_seed,
        trials_per_session=int(trials),
        include_practice=options.include_practice,
        baseline_seconds=options.baseline_seconds,
        window_size=options.window_size,
        full_screen=options.full_screen,
        record_eeg=options.record_eeg,
        require_eeg=options.require_eeg,
        resume=options.resume,
        output_root=options.output_root,
        lsl_wait_seconds=options.lsl_wait_seconds,
        electrode_quality_file=options.electrode_quality_file,
        electrode_note=options.electrode_note,
        electrodes_confirmed=options.electrodes_confirmed,
    )


def _update_participant_visit(
    participant: dict[str, Any],
    path: Path,
    visit_number: int,
    visit_id: str,
    manifest_path: Path,
    *,
    status: str,
    completed_at: str | None = None,
) -> None:
    entry = participant.setdefault("visits", {}).setdefault(str(visit_number), {})
    entry.update(
        {
            "visit_id": visit_id,
            "manifest_file": str(manifest_path),
            "status": status,
            "updated_at": _now(),
        }
    )
    if completed_at is not None:
        entry["completed_at"] = completed_at
    _write_json_atomic(path, participant)


def _phase_error(label: str, result: dict[str, Any]) -> str:
    detail = result.get("error") or result.get("abort_reason")
    if detail is None:
        failures = list(result.get("failures") or [])
        if not failures:
            failures = list(dict(result.get("validation") or {}).get("failures") or [])
        detail = failures[0] if failures else f"status={result.get('status')}"
    return f"{label} did not complete: {detail}"


def _study1_phase_result_failures(
    phase: str,
    result: dict[str, Any],
    manifest: dict[str, Any],
) -> list[str]:
    """Reject contradictory or incomplete phase artifacts before advancing."""

    failures: list[str] = []
    if phase == "preflight":
        if result.get("status") not in {"pass", "warning"}:
            failures.append(f"preflight status is {result.get('status')}; expected pass or warning")
        if result.get("failures"):
            failures.append("preflight contains one or more active failures")
        if not bool(dict(result.get("operator_acceptance") or {}).get("accepted")):
            failures.append("preflight lacks an accepted operator decision")
        return failures

    if phase == "baseline" and result.get("status") == "skipped":
        if result.get("skip_reason") != "operator_requested":
            failures.append("skipped baseline lacks the explicit operator-requested reason")
        if result.get("session_dir") is not None:
            failures.append("skipped baseline unexpectedly created a session directory")
        return failures

    if result.get("status") != "completed":
        failures.append(f"phase result status is {result.get('status')}; expected completed")
    session_value = result.get("session_dir")
    if not session_value:
        failures.append("phase did not report a retained session directory")
    elif not Path(str(session_value)).exists():
        failures.append(f"reported session directory does not exist: {session_value}")
    validation = dict(result.get("validation") or {})
    if not validation:
        failures.append("phase did not publish post-recording validation")
    else:
        if validation.get("status") not in {"pass", "warning"}:
            failures.append(f"post-recording validation status is {validation.get('status')}")
        if validation.get("failures"):
            failures.append("post-recording validation contains one or more active failures")

    if phase == "baseline":
        processes = dict(result.get("processes") or {})
        if processes.get("status") != "complete":
            failures.append(f"baseline managed-process status is {processes.get('status')}; expected complete")
        return failures

    forward = dict(result.get("forward") or {})
    if forward.get("status") != "complete":
        failures.append(f"forward task/recorder status is {forward.get('status')}; expected complete")
    expected_hash = dict(manifest.get("prepared_sequence_hashes") or {}).get(phase)
    if not expected_hash:
        failures.append("prepared sequence identity is missing from the visit manifest")
    elif result.get("sequence_hash") != expected_hash:
        failures.append(
            f"recorded sequence identity {result.get('sequence_hash')} does not match prepared identity {expected_hash}"
        )
    if manifest.get("task_mode") == "psychopy":
        worker = dict(result.get("phase_worker") or {})
        if worker.get("return_code") != 0:
            failures.append(
                f"isolated PsychoPy phase worker return code is {worker.get('return_code')}; expected 0"
            )
    if phase == "session1_main" and manifest.get("include_practice"):
        if result.get("practice_status") != "passed":
            failures.append(
                f"required participant practice status is {result.get('practice_status')}; expected passed"
            )
    return failures


def _study1_visit_completion_failures(manifest: dict[str, Any]) -> dict[str, list[str]]:
    failures: dict[str, list[str]] = {}
    for phase in list(manifest.get("phase_order") or []):
        phase_failures: list[str] = []
        if not _phase_is_complete(manifest, phase):
            phase_failures.append(
                f"phase ledger status is {manifest.get('phases', {}).get(phase, {}).get('status')}; expected completed"
            )
        result = _latest_phase_result(manifest, phase)
        if result is None:
            phase_failures.append("completed phase has no completed result artifact")
        else:
            phase_failures.extend(_study1_phase_result_failures(phase, result, manifest))
        if phase_failures:
            failures[phase] = list(dict.fromkeys(phase_failures))
    return failures


def _continues_incomplete_visit(options: Study1Options) -> bool:
    return bool(options.resume or options.retry_incomplete)


def _has_recording(manifest: dict[str, Any]) -> bool:
    if manifest.get("session_directories") or _phase_is_complete(manifest, "baseline"):
        return True
    for phase in dict(manifest.get("phases") or {}).values():
        for attempt in list(dict(phase or {}).get("attempts") or []):
            if dict(attempt.get("result") or {}).get("session_dir"):
                return True
    return False


def _archive_resolved_failures(manifest: dict[str, Any], *, resolved_at: str) -> None:
    """Keep retry history without reporting old failures as active on success."""

    active = list(manifest.get("failures") or [])
    if not active:
        return
    resolved = manifest.setdefault("resolved_failures", [])
    for failure in active:
        resolved.append(
            {
                **dict(failure),
                "resolved_at": resolved_at,
                "resolution": "visit_completed_after_retry",
            }
        )
    manifest["failures"] = []


def _public_result(manifest: dict[str, Any], path: Path) -> dict[str, Any]:
    result = {
        "schema": manifest.get("schema"),
        "status": manifest.get("status"),
        "participant_id": manifest.get("participant_id"),
        "visit_number": manifest.get("visit_number"),
        "visit_id": manifest.get("visit_id"),
        "manifest_file": str(path),
        "acquisition_profile": manifest.get("acquisition_profile"),
        "no_go_digit": manifest.get("no_go_digit"),
        "session_directories": manifest.get("session_directories"),
        "sequence_hashes": manifest.get("sequence_hashes"),
        "failures": manifest.get("failures"),
        "resolved_failures": manifest.get("resolved_failures", []),
    }
    failures = list(manifest.get("failures") or [])
    if result["status"] != "completed" and failures:
        latest = dict(failures[-1])
        failed_phase = str(latest.get("phase") or "unknown")
        result["failed_phase"] = failed_phase
        result["failure_detail"] = latest.get("error")
        attempts = list(
            dict(manifest.get("phases", {}).get(failed_phase, {}) or {}).get("attempts") or []
        )
        latest_result = dict(attempts[-1].get("result") or {}) if attempts else {}
        if latest_result.get("session_dir"):
            result["retained_session_directory"] = latest_result.get("session_dir")
        result["next_action"] = (
            "Correct the reported failure, then rerun the same Windows operator command. "
            "EEGle will reuse this incomplete visit, skip completed phases, and rerun the failed phase."
        )
    return result


def _exception_next_action(exc: BaseException) -> str:
    detail = str(exc)
    if "already completed" in detail:
        return "Use a new participant/test ID for a new run; completed visits are immutable."
    if "already has status" in detail or "already exists" in detail:
        return (
            "Rerun with --retry-incomplete to reuse the incomplete visit automatically, "
            "or use --resume with its original visit ID."
        )
    return "Correct the reported error and rerun the same command."


if __name__ == "__main__":
    raise SystemExit(main())
