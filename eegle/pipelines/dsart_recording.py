"""Abort-safe two-session Dynamic SART raw-recording suites."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import monotonic, monotonic_ns, sleep
from typing import Any, Iterable

from eegle.analysis.dynamic_sart import analyze_dynamic_sart_session
from eegle.config import load_config, resolve_session_root
from eegle.devices.lsl_markers import LslMarkerReceiptRecorder
from eegle.devices.labrecorder_xdf import LabRecorderXdfRecorder, labrecorder_environment
from eegle.devices.xdf_integrity import validate_xdf_recording
from eegle.experiment import ForwardExperimentRunner
from eegle.feedback_manager import FeedbackManager
from eegle.hardware.profiles import expected_profile
from eegle.hardware.system import CheckResult
from eegle.io.events import EventLogger
from eegle.lsl import LslMarkerOutlet, NullMarkerOutlet, lsl_local_clock, session_marker_source_id
from eegle.preflight import run_preflight
from eegle.psychopy_audio import (
    PsychoPyAudioOutput,
    audio_output_enabled,
    play_psychopy_end_signal,
    prepare_psychopy_audio_output,
)
from eegle.psychopy_display import (
    create_psychopy_window,
    measure_psychopy_refresh_rate,
    probe_psychopy_display_and_keyboard,
    redraw_psychopy_after_resize,
)
from eegle.psychopy_input import clear_psychopy_keys, poll_psychopy_keys
from eegle.recording_health import RecorderHealthMonitor
from eegle.runtime import prepare_psychopy_runtime
from eegle.session import SessionPaths, create_session
from eegle.storage_permissions import probe_recording_storage
from eegle.telemetry import Telemetry


SUITE_SCHEMA = "eegle.dsart_recording_suite.v1"
BASELINE_SCHEMA = "eegle.dsart_resting_baseline.v1"
PREFLIGHT_SCHEMA = "eegle.dsart_recording_preflight.v1"
OVERLAP_SCHEMA = "eegle.dsart_channel_overlap.v1"
PHASE_WORKER_SCHEMA = "eegle.dsart_phase_worker.v1"
DEFAULT_CONFIGS = {
    "dsart8": Path("configs/record_dsart8.json"),
    "dsart32": Path("configs/record_dsart32.json"),
}
PHASE_ORDER = (
    "initial_preflight",
    "baseline",
    "dsart_session_1",
    "inter_session_break",
    "second_preflight",
    "dsart_session_2",
)
DSART8_CHANNELS = ("Fz", "Cz", "Pz", "C3", "C4", "P3", "P4", "Oz")
_ATOMIC_REPLACE_RETRY_DELAYS_SECONDS = (0.0, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 2.0)


@dataclass(frozen=True)
class DsartRecordingOptions:
    recipe: str
    config_path: str | Path
    participant_id: str
    visit_id: str | None = None
    operator: str | None = None
    task_mode: str = "psychopy"
    master_seed: int = 42
    session_1_seed: int | None = None
    session_2_seed: int | None = None
    trials_per_session: int = 600
    include_practice: bool = False
    baseline_seconds: float | None = None
    break_seconds: float | None = None
    window_size: tuple[int, int] | None = None
    full_screen: bool | None = None
    record_eeg: bool = True
    require_eeg: bool = True
    resume: bool = False
    output_root: str | Path | None = None
    lsl_wait_seconds: float = 5.0
    electrode_quality_file: str | Path | None = None
    electrode_note: str | None = None
    electrodes_confirmed: bool = False


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    options = _options_from_args(args)
    try:
        result = run_recording_suite(options)
    except Exception as exc:
        result = {
            "schema": SUITE_SCHEMA,
            "recipe": options.recipe,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    exit_code = 0 if result.get("status") == "completed" else 1
    result["process_exit_code"] = exit_code
    print(json.dumps(result, indent=2, sort_keys=True))
    return exit_code


def main_dsart8(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    return main(["--recipe", "dsart8", *arguments])


def main_dsart32(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    return main(["--recipe", "dsart32", *arguments])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="record-dsart",
        description="Record resting baseline plus two independent Dynamic SART sessions",
    )
    parser.add_argument("--recipe", choices=sorted(DEFAULT_CONFIGS), required=True)
    parser.add_argument("--config", default=None, help="Hardware recipe JSON; defaults from --recipe")
    parser.add_argument("--participant", required=True)
    parser.add_argument("--visit-id", default=None)
    parser.add_argument("--operator", default=None)
    parser.add_argument("--task-mode", choices=["psychopy", "dry-run"], default="psychopy")
    parser.add_argument("--master-seed", type=int, default=42)
    parser.add_argument("--session-1-seed", type=int, default=None)
    parser.add_argument("--session-2-seed", type=int, default=None)
    parser.add_argument(
        "--trials",
        type=int,
        default=600,
        help=(
            "Experimental trials per DSART session; shortened smoke runs skip participant practice "
            "unless --include-practice is supplied (minimum 10)"
        ),
    )
    parser.add_argument(
        "--include-practice",
        action="store_true",
        help="Include participant practice in a shortened smoke run; full recipe runs include it automatically",
    )
    parser.add_argument(
        "--baseline-seconds",
        type=float,
        default=None,
        help="Smoke-test override applied separately to eyes-open and eyes-closed baselines",
    )
    parser.add_argument("--break-seconds", type=float, default=None)
    parser.add_argument(
        "--window-size",
        type=int,
        nargs=2,
        metavar=("WIDTH", "HEIGHT"),
        default=None,
        help="Windowed PsychoPy launch size in pixels",
    )
    display_mode = parser.add_mutually_exclusive_group()
    display_mode.add_argument("--fullscreen", dest="full_screen", action="store_true")
    display_mode.add_argument("--windowed", dest="full_screen", action="store_false")
    parser.set_defaults(full_screen=None)
    parser.add_argument("--skip-eeg", action="store_true")
    parser.add_argument("--allow-missing-eeg", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--session-root",
        "--output-root",
        dest="output_root",
        default=None,
        help=(
            "Root for all parent-suite and child-session data; --output-root remains a compatibility alias. "
            "Use an approved writable Windows path such as $env:LOCALAPPDATA\\EEGle\\data when Documents is restricted."
        ),
    )
    parser.add_argument("--lsl-wait", type=float, default=5.0)
    parser.add_argument("--electrode-quality-file", default=None, help="Optional JSON channel quality or impedance report")
    parser.add_argument("--electrode-note", default=None)
    parser.add_argument(
        "--confirm-electrodes",
        action="store_true",
        help="Noninteractive attestation that contact/impedance was checked; otherwise each live preflight prompts",
    )
    return parser


def _options_from_args(args: argparse.Namespace) -> DsartRecordingOptions:
    config_path = args.config or DEFAULT_CONFIGS[args.recipe]
    return DsartRecordingOptions(
        recipe=str(args.recipe),
        config_path=config_path,
        participant_id=str(args.participant),
        visit_id=args.visit_id,
        operator=args.operator,
        task_mode=str(args.task_mode),
        master_seed=int(args.master_seed),
        session_1_seed=args.session_1_seed,
        session_2_seed=args.session_2_seed,
        trials_per_session=int(args.trials),
        include_practice=bool(args.include_practice),
        baseline_seconds=args.baseline_seconds,
        break_seconds=args.break_seconds,
        window_size=None if args.window_size is None else (int(args.window_size[0]), int(args.window_size[1])),
        full_screen=args.full_screen,
        record_eeg=not bool(args.skip_eeg),
        require_eeg=not bool(args.allow_missing_eeg) and not bool(args.skip_eeg),
        resume=bool(args.resume),
        output_root=args.output_root,
        lsl_wait_seconds=float(args.lsl_wait),
        electrode_quality_file=args.electrode_quality_file,
        electrode_note=args.electrode_note,
        electrodes_confirmed=bool(args.confirm_electrodes),
    )


def run_recording_suite(options: DsartRecordingOptions) -> dict[str, Any]:
    if options.recipe not in DEFAULT_CONFIGS:
        raise ValueError(f"unknown DSART recording recipe {options.recipe}")
    if not options.participant_id.strip():
        raise ValueError("--participant must be nonempty")
    if options.trials_per_session < 10:
        raise ValueError(
            "--trials must be at least 10 so smoke runs retain support/query and the configured leading/trailing go trials"
        )
    if options.baseline_seconds is not None and options.baseline_seconds < 0:
        raise ValueError("--baseline-seconds must be nonnegative")
    if options.window_size is not None and any(value <= 0 for value in options.window_size):
        raise ValueError("--window-size WIDTH and HEIGHT must be positive")
    if options.record_eeg and not options.require_eeg:
        raise ValueError(
            "DSART acquisition cannot record EEG while allowing it to be missing; "
            "remove --allow-missing-eeg, or use --skip-eeg for an explicit software-only rehearsal"
        )
    config = copy.deepcopy(load_config(options.config_path))
    suite_config = config.setdefault("recording_suite", {})
    suite_config["trials_per_session"] = int(options.trials_per_session)
    _configure_practice_policy(config, options)
    if options.baseline_seconds is not None:
        suite_config.setdefault("baseline", {}).update(
            {
                "eyes_open_seconds": float(options.baseline_seconds),
                "eyes_closed_seconds": float(options.baseline_seconds),
            }
        )
    if options.window_size is not None:
        config.setdefault("hardware", {}).setdefault("display", {})["size"] = list(options.window_size)
    if options.full_screen is not None:
        config.setdefault("hardware", {}).setdefault("display", {})["full_screen"] = bool(options.full_screen)
    output_root = resolve_session_root(config, options.output_root)
    runtime_config = config.setdefault("runtime", {})
    runtime_config["session_root"] = str(output_root)
    runtime_config["runtime_cache_dir"] = str(_runtime_cache_root(config, output_root))
    _probe_session_root_writable(output_root)
    visit_id = _resolve_visit_id(options, output_root)
    visit_dir = output_root / "recording_suites" / options.participant_id / visit_id / options.recipe
    manifest_path = visit_dir / "recording_suite.json"
    if manifest_path.exists() and not options.resume:
        raise FileExistsError(f"recording suite already exists; use --resume: {manifest_path}")
    visit_dir.mkdir(parents=True, exist_ok=True)

    config_issues = validate_recording_config(config, options.recipe)
    hard_issues = [issue for issue in config_issues if issue["status"] == "fail"]
    if hard_issues:
        raise ValueError("recording configuration invalid: " + "; ".join(issue["detail"] for issue in hard_issues))
    session_1_seed = options.session_1_seed or derive_session_seed(
        options.participant_id, visit_id, 1, options.master_seed
    )
    session_2_seed = options.session_2_seed or derive_session_seed(
        options.participant_id, visit_id, 2, options.master_seed
    )
    if session_1_seed == session_2_seed:
        raise ValueError("session-1 and session-2 seeds must differ")
    break_seconds = float(
        options.break_seconds
        if options.break_seconds is not None
        else config.get("recording_suite", {}).get("break_seconds", 600.0)
    )
    if break_seconds < 0:
        raise ValueError("--break-seconds must be nonnegative")

    if manifest_path.exists():
        manifest = _load_json(manifest_path) or {}
        _validate_resume_identity(
            manifest,
            options,
            visit_id,
            session_1_seed,
            session_2_seed,
            configuration_hash=_hash_payload(config),
        )
    else:
        manifest = _initial_manifest(
            options,
            config,
            visit_id=visit_id,
            visit_dir=visit_dir,
            session_1_seed=session_1_seed,
            session_2_seed=session_2_seed,
            break_seconds=break_seconds,
            config_issues=config_issues,
        )
        _write_json_atomic(manifest_path, manifest)

    current_preflight: dict[str, Any] | None = None
    initial_preflight: dict[str, Any] | None = None
    active_phase: str | None = None
    active_result: dict[str, Any] | None = None
    try:
        for phase in PHASE_ORDER:
            if _phase_is_complete(manifest, phase):
                if options.resume:
                    manifest.setdefault("resumed_phases", []).append({"phase": phase, "action": "skipped_completed"})
                    _write_json_atomic(manifest_path, manifest)
                if phase == "initial_preflight":
                    initial_preflight = _latest_phase_result(manifest, phase)
                    current_preflight = initial_preflight
                elif phase == "second_preflight":
                    current_preflight = _latest_phase_result(manifest, phase)
                continue

            active_phase = phase
            active_result = None
            _start_phase(manifest, phase)
            _write_json_atomic(manifest_path, manifest)
            result: dict[str, Any] | None = None
            try:
                if phase in {"initial_preflight", "second_preflight"}:
                    result = run_recording_preflight(
                        config,
                        recipe=options.recipe,
                        participant_id=options.participant_id,
                        visit_id=visit_id,
                        phase=phase,
                        output_dir=visit_dir / "preflight",
                        require_eeg=options.require_eeg,
                        record_eeg=options.record_eeg,
                        lsl_wait_seconds=options.lsl_wait_seconds,
                        electrode_quality_file=options.electrode_quality_file,
                        electrode_note=options.electrode_note,
                        electrodes_confirmed=options.electrodes_confirmed,
                        initial_preflight=initial_preflight,
                        require_display=options.task_mode == "psychopy",
                    )
                    if phase == "initial_preflight":
                        initial_preflight = result
                        manifest["initial_preflight"] = result
                    else:
                        manifest["second_preflight"] = result
                    current_preflight = result
                    if result.get("status") not in {"pass", "warning"} or result.get("failures"):
                        raise RuntimeError(f"{phase} failed; acquisition stopped")
                    _accept_recording_preflight(result, options)
                elif phase == "baseline":
                    if current_preflight is None:
                        raise RuntimeError("baseline cannot start without a completed preflight")
                    result = run_resting_baseline(
                        config,
                        options,
                        visit_id=visit_id,
                        preflight=current_preflight,
                    )
                    active_result = result
                    manifest["baseline_session_directory"] = result.get("session_dir")
                    if result.get("status") != "completed":
                        raise RuntimeError(_incomplete_phase_detail("resting baseline", result))
                    _accept_post_recording_warnings(result, options, "resting baseline")
                elif phase == "dsart_session_1":
                    result = run_dsart_child_session(
                        config,
                        options,
                        visit_id=visit_id,
                        session_index=1,
                        seed=session_1_seed,
                        preflight=current_preflight,
                    )
                    active_result = result
                    _record_dsart_attempt(manifest, 1, result)
                    if result.get("status") != "completed":
                        raise RuntimeError(_incomplete_phase_detail("DSART session 1", result))
                    _accept_post_recording_warnings(result, options, "DSART session 1")
                elif phase == "inter_session_break":
                    result = run_inter_session_break(
                        config,
                        options,
                        visit_id=visit_id,
                        visit_dir=visit_dir,
                        break_seconds=break_seconds,
                    )
                    active_result = result
                    manifest["break_start"] = result.get("start_monotonic")
                    manifest["break_end"] = result.get("end_monotonic")
                    if result.get("status") != "completed":
                        raise RuntimeError(_incomplete_phase_detail("inter-session break", result))
                elif phase == "dsart_session_2":
                    if current_preflight is None:
                        raise RuntimeError("session 2 cannot start without the second preflight")
                    result = run_dsart_child_session(
                        config,
                        options,
                        visit_id=visit_id,
                        session_index=2,
                        seed=session_2_seed,
                        preflight=current_preflight,
                    )
                    active_result = result
                    _record_dsart_attempt(manifest, 2, result)
                    if result.get("status") != "completed":
                        raise RuntimeError(_incomplete_phase_detail("DSART session 2", result))
                    _accept_post_recording_warnings(result, options, "DSART session 2")
                else:
                    raise RuntimeError(f"unsupported suite phase {phase}")
                _complete_phase(manifest, phase, result)
                _write_json_atomic(manifest_path, manifest)
                active_phase = None
                active_result = None
            except KeyboardInterrupt as exc:
                _fail_phase(manifest, phase, exc, result)
                manifest["overall_status"] = "partial" if _has_scientific_recording(manifest) else "aborted"
                manifest["status"] = manifest["overall_status"]
                manifest.setdefault("aborts", []).append(
                    {
                        "phase": phase,
                        "error": "KeyboardInterrupt: operator interrupt",
                        "failure_kind": "operator_interrupt",
                        "timestamp": _now(),
                    }
                )
                manifest["visit_end"] = _now()
                _write_json_atomic(manifest_path, manifest)
                return _public_suite_result(manifest, manifest_path)
            except Exception as exc:
                _fail_phase(manifest, phase, exc, result)
                manifest["overall_status"] = "partial" if _has_scientific_recording(manifest) else "failed"
                manifest["status"] = manifest["overall_status"]
                manifest.setdefault("aborts", []).append(
                    {
                        "phase": phase,
                        "error": f"{type(exc).__name__}: {exc}",
                        "failure_kind": "phase_failure",
                        "timestamp": _now(),
                    }
                )
                manifest["visit_end"] = _now()
                _write_json_atomic(manifest_path, manifest)
                return _public_suite_result(manifest, manifest_path)

        manifest["overall_status"] = "completed"
        manifest["status"] = "completed"
        manifest["visit_end"] = _now()
        _write_json_atomic(manifest_path, manifest)
        return _public_suite_result(manifest, manifest_path)
    except KeyboardInterrupt as exc:
        if active_phase is not None:
            _fail_phase(manifest, active_phase, exc, active_result)
            manifest.setdefault("aborts", []).append(
                {
                    "phase": active_phase,
                    "error": "KeyboardInterrupt: operator interrupt",
                    "failure_kind": "operator_interrupt",
                    "timestamp": _now(),
                }
            )
        manifest["overall_status"] = "partial" if _has_scientific_recording(manifest) else "aborted"
        manifest["status"] = manifest["overall_status"]
        manifest["visit_end"] = _now()
        _write_json_atomic(manifest_path, manifest)
        return _public_suite_result(manifest, manifest_path)
    except BaseException:
        manifest["overall_status"] = "partial" if _has_scientific_recording(manifest) else "aborted"
        manifest["status"] = manifest["overall_status"]
        manifest["visit_end"] = _now()
        _write_json_atomic(manifest_path, manifest)
        raise


def validate_recording_config(config: dict[str, Any], recipe: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    suite = dict(config.get("recording_suite", {}) or {})
    eeg = dict(config.get("hardware", {}).get("eeg", {}) or {})
    task = dict(config.get("tasks", {}).get("dynamic_sart", {}) or {})
    expected_profile_name = "enobio8_inhibition" if recipe == "dsart8" else "enobio32_dsart_wet"
    expected_count = 8 if recipe == "dsart8" else 32
    if str(suite.get("recipe")) != recipe:
        issues.append(_issue("fail", f"recording_suite.recipe must be {recipe}"))
    if str(eeg.get("profile")) != expected_profile_name:
        issues.append(_issue("fail", f"hardware.eeg.profile must be {expected_profile_name}"))
    if list(eeg.get("expected_channel_counts", [])) != [expected_count]:
        issues.append(_issue("fail", f"hardware.eeg.expected_channel_counts must be [{expected_count}]"))
    if eeg.get("raw_sample_mode") != "passthrough":
        issues.append(_issue("fail", "hardware.eeg.raw_sample_mode must be passthrough"))
    if eeg.get("recording_lsl_processing") != "source_preserving":
        issues.append(_issue("fail", "hardware.eeg.recording_lsl_processing must be source_preserving"))
    maximum_gap = float(eeg.get("maximum_timestamp_gap_seconds", 0.0))
    if maximum_gap <= 0.0 or maximum_gap > 0.1:
        issues.append(_issue("fail", "hardware.eeg.maximum_timestamp_gap_seconds must be in (0, 0.1]"))
    if not bool(eeg.get("abort_on_timestamp_gap", False)):
        issues.append(_issue("fail", "hardware.eeg.abort_on_timestamp_gap must be true"))
    if int(eeg.get("minimum_free_bytes_during_recording", 0)) < 512 * 1024 * 1024:
        issues.append(
            _issue("fail", "hardware.eeg.minimum_free_bytes_during_recording must reserve at least 512 MiB")
        )
    disk_interval = float(eeg.get("disk_check_interval_seconds", 0.0))
    if disk_interval < 1.0 or disk_interval > 5.0:
        issues.append(_issue("fail", "hardware.eeg.disk_check_interval_seconds must be between 1 and 5 seconds"))
    if float(eeg.get("expected_sample_rate_hz", 0.0)) != 500.0:
        issues.append(_issue("fail", "hardware.eeg.expected_sample_rate_hz must be 500"))
    try:
        profile = expected_profile(expected_profile_name, "Enobio")
    except KeyError as exc:
        issues.append(_issue("fail", str(exc)))
        profile = None
    mapping = dict(eeg.get("channel_number_map", {}) or {})
    if profile is not None:
        if set(mapping) != set(profile.channel_names):
            issues.append(_issue("fail", "hardware.eeg.channel_number_map labels must exactly match the selected profile"))
        numbers = list(mapping.values())
        if sorted(numbers) != list(range(1, expected_count + 1)):
            issues.append(_issue("fail", "hardware.eeg.channel_number_map must be a one-to-one mapping over device channels"))
    blocks = list(task.get("blocks", []))
    if len(blocks) != 6 or any(int(block.get("trials", 0)) != 100 for block in blocks):
        issues.append(_issue("fail", "tasks.dynamic_sart.blocks must contain six 100-trial blocks"))
    phases = [block.get("phase") for block in blocks]
    if phases != ["support", "support", "query", "query", "query", "query"]:
        issues.append(_issue("fail", "DSART phases must be support,support,query,query,query,query"))
    if int(task.get("planned_no_go_count", 0)) != 67:
        issues.append(_issue("fail", "tasks.dynamic_sart.planned_no_go_count must be 67"))
    practice = dict(task.get("practice", {}) or {})
    if abs(float(practice.get("no_go_accuracy", 0.0)) - (2.0 / 3.0)) > 1e-9:
        issues.append(_issue("fail", "tasks.dynamic_sart.practice.no_go_accuracy must represent exactly two of three no-go trials"))
    if bool(task.get("allow_task_adaptation", False)):
        issues.append(_issue("fail", "tasks.dynamic_sart.allow_task_adaptation must be false"))
    if bool(task.get("allow_stimulation", False)):
        issues.append(_issue("fail", "tasks.dynamic_sart.allow_stimulation must be false"))
    if set(task.get("response_keys", [])) != {"space"}:
        issues.append(_issue("fail", "tasks.dynamic_sart.response_keys must be exactly [space]"))
    if not {"escape", "q"}.issubset(set(task.get("escape_keys", []))):
        issues.append(_issue("fail", "tasks.dynamic_sart.escape_keys must include escape and q"))
    if float(task.get("countdown_step_seconds", 1.0)) <= 0.0:
        issues.append(_issue("fail", "tasks.dynamic_sart.countdown_step_seconds must be positive"))
    if abs(float(task.get("stimulus_seconds", 0.0)) - 0.25) > 1e-9:
        issues.append(_issue("fail", "tasks.dynamic_sart.stimulus_seconds must be 0.25"))
    if abs(float(task.get("response_window_seconds", 0.0)) - 1.60) > 1e-9:
        issues.append(
            _issue(
                "fail",
                "tasks.dynamic_sart.response_window_seconds must be 1.60 (0.25 s digit + 1.35 s fixation)",
            )
        )
    if any(
        abs(float(task.get(name, 0.0))) > 1e-9
        for name in ("inter_trial_jitter_min_seconds", "inter_trial_jitter_max_seconds")
    ):
        issues.append(_issue("fail", "DSART intentional inter-trial jitter must be disabled"))
    for name in ("soi_min_seconds", "soi_max_seconds"):
        if task.get(name) is not None and abs(float(task[name]) - 1.60) > 1e-9:
            issues.append(_issue("fail", f"tasks.dynamic_sart.{name} must be 1.60 when configured"))
    display = dict(config.get("hardware", {}).get("display", {}) or {})
    display_size = list(display.get("size", []))
    if len(display_size) != 2 or any(float(value) <= 0.0 for value in display_size):
        issues.append(_issue("fail", "hardware.display.size must contain two positive values"))
    if not bool(display.get("wait_blanking", False)):
        issues.append(_issue("fail", "hardware.display.wait_blanking must be true for VBlank synchronization"))
    if not bool(display.get("check_refresh_rate", False)):
        issues.append(_issue("fail", "hardware.display.check_refresh_rate must be true"))
    if not bool(display.get("require_refresh_rate_match", False)):
        issues.append(_issue("fail", "hardware.display.require_refresh_rate_match must be true"))
    if float(display.get("expected_refresh_rate_hz", 0.0)) <= 0.0:
        issues.append(_issue("fail", "hardware.display.expected_refresh_rate_hz must be positive"))
    marker_config = dict(config.get("hardware", {}).get("markers", {}) or {})
    if not bool(marker_config.get("required_for_realtime", False)):
        issues.append(_issue("fail", "hardware.markers.required_for_realtime must be true during acquisition"))
    realtime = dict(config.get("realtime", {}) or {})
    if bool(realtime.get("enabled", False)):
        issues.append(_issue("fail", "realtime.enabled must be false during acquisition"))
    if bool(realtime.get("inference", {}).get("enabled", False)):
        issues.append(_issue("fail", "realtime.inference.enabled must be false"))
    if bool(realtime.get("classifier", {}).get("enabled", False)):
        issues.append(_issue("fail", "realtime.classifier.enabled must be false"))
    if bool(realtime.get("capture", {}).get("enabled", False)):
        issues.append(_issue("fail", "realtime.capture.enabled must be false during acquisition"))
    if bool(realtime.get("decision_policy", {}).get("enabled", False)):
        issues.append(_issue("fail", "realtime.decision_policy.enabled must be false during acquisition"))
    if bool(realtime.get("feedback", {}).get("client", {}).get("enabled", False)):
        issues.append(_issue("fail", "realtime.feedback.client.enabled must be false during acquisition"))
    if bool(realtime.get("feedback", {}).get("allow_task_adaptation", False)):
        issues.append(_issue("fail", "realtime.feedback.allow_task_adaptation must be false"))
    if bool(realtime.get("feedback", {}).get("allow_stimulation", False)):
        issues.append(_issue("fail", "realtime.feedback.allow_stimulation must be false"))
    if bool(config.get("processes", {}).get("offline_analyzer", {}).get("enabled", False)):
        issues.append(_issue("fail", "processes.offline_analyzer.enabled must be false during acquisition"))
    for process_name in ("realtime_processor", "feedback", "dashboard"):
        if bool(config.get("processes", {}).get(process_name, {}).get("enabled", False)):
            issues.append(_issue("fail", f"processes.{process_name}.enabled must be false during acquisition"))
    recorder = dict(config.get("processes", {}).get("recorder", {}) or {})
    if not bool(recorder.get("enabled", False)) or recorder.get("backend") != "lsl_csv":
        issues.append(_issue("fail", "processes.recorder must enable the lsl_csv backend"))
    if not bool(recorder.get("csv_mirror", False)):
        issues.append(_issue("fail", "processes.recorder.csv_mirror must be true"))
    if float(suite.get("recorder_stall_timeout_seconds", 0.0)) < 1.0:
        issues.append(_issue("fail", "recording_suite.recorder_stall_timeout_seconds must be at least 1 second"))
    if str(config.get("telemetry", {}).get("file_level")) != "default":
        issues.append(_issue("fail", "telemetry.file_level must be default to keep per-stimulus telemetry off the render path"))
    epoching = dict(realtime.get("epoching", {}) or {})
    epoching_ready = (
        epoching.get("marker_prefix") == "dynamic_sart_stimulus_onset"
        and float(epoching.get("tmin_seconds", 0.0)) == -2.0
        and float(epoching.get("tmax_seconds", 0.0)) == -0.05
        and epoching.get("timebase") == "lsl"
        and not bool(epoching.get("include_practice_trials", True))
        and epoching.get("data_source") == "raw"
    )
    if not epoching_ready:
        issues.append(_issue("fail", "realtime.epoching must use the strict raw LSL DSART prestimulus contract"))
    return issues


def _normal_recipe_trial_count(config: dict[str, Any]) -> int:
    blocks = config.get("tasks", {}).get("dynamic_sart", {}).get("blocks", [])
    return sum(int(block.get("trials", 0)) for block in blocks)


def _configure_practice_policy(config: dict[str, Any], options: DsartRecordingOptions) -> dict[str, Any]:
    """Apply the explicit practice policy for formal and shortened runs."""
    task = config.setdefault("tasks", {}).setdefault("dynamic_sart", {})
    practice = task.setdefault("practice", {})
    configured_enabled = bool(practice.get("enabled", True))
    normal_trial_count = _normal_recipe_trial_count(config)
    shortened_run = int(options.trials_per_session) != normal_trial_count
    session_1_enabled = configured_enabled and (not shortened_run or bool(options.include_practice))
    repeat_practice = bool(
        config.setdefault("recording_suite", {}).setdefault("session_2", {}).get("repeat_practice", False)
    )
    practice["enabled"] = session_1_enabled
    policy = {
        "configured_enabled": configured_enabled,
        "normal_trial_count": normal_trial_count,
        "shortened_run": shortened_run,
        "include_practice_requested": bool(options.include_practice),
        "session_1_enabled": session_1_enabled,
        "session_2_enabled": session_1_enabled and repeat_practice,
    }
    config["recording_suite"]["practice_policy"] = policy
    return policy


def derive_session_seed(participant_id: str, visit_id: str, session_index: int, master_seed: int) -> int:
    digest = hashlib.sha256(
        f"{participant_id}:{visit_id}:{session_index}:{master_seed}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def run_recording_preflight(
    config: dict[str, Any],
    *,
    recipe: str,
    participant_id: str,
    visit_id: str,
    phase: str,
    output_dir: Path,
    require_eeg: bool,
    record_eeg: bool,
    lsl_wait_seconds: float,
    electrode_quality_file: str | Path | None,
    electrode_note: str | None,
    electrodes_confirmed: bool,
    initial_preflight: dict[str, Any] | None,
    require_display: bool,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    checks = run_preflight(
        config,
        lsl_wait=lsl_wait_seconds,
        require_eeg=require_eeg,
        check_eeg=record_eeg,
    )
    check_payloads = [result.__dict__ for result in checks]
    display_check = next((result for result in checks if result.name == "display_ready"), None)
    if require_display:
        dependency_ok = display_check is not None and display_check.status == "ok"
        runtime_timing: dict[str, Any] = {}
        runtime_error: str | None = None
        if dependency_ok:
            try:
                prepare_psychopy_runtime(config.get("runtime", {}).get("runtime_cache_dir", ".runtime"))
                runtime_timing = probe_psychopy_display_and_keyboard(config)
            except Exception as exc:
                runtime_error = f"{type(exc).__name__}: {exc}"
        display_ok = dependency_ok and runtime_error is None
        check_payloads.append(
            CheckResult(
                "dsart_display_contract",
                "ok" if display_ok else "fail",
                (
                    "real PsychoPy window, measured refresh, and asynchronous PTB keyboard are ready"
                    if display_ok
                    else (
                        f"live DSART display/input probe failed: {runtime_error}"
                        if runtime_error
                        else "PsychoPy is required for live DSART acquisition"
                    )
                ),
                {
                    **({} if display_check is None else dict(display_check.data)),
                    **runtime_timing,
                    "runtime_error": runtime_error,
                },
            ).__dict__
        )
        if audio_output_enabled(config):
            audio_probe = runtime_timing.get("audio_output")
            if not isinstance(audio_probe, dict):
                audio_probe = {
                    "status": "warn",
                    "detail": "audio output probe returned no result",
                    "failure_policy": "warn",
                }
            print(
                f"[audio] {audio_probe.get('detail') or 'audio output unavailable'}",
                flush=True,
            )
            audio_status = "ok" if audio_probe.get("status") == "ok" else "warn"
            check_payloads.append(
                CheckResult(
                    "audio_output",
                    audio_status,
                    str(audio_probe.get("detail") or "audio output unavailable"),
                    {**audio_probe, "operator_gate": False},
                ).__dict__
            )
    eeg = dict(config.get("hardware", {}).get("eeg", {}) or {})
    profile = expected_profile(str(eeg["profile"]), eeg.get("family"))
    expected_channel_names = list(eeg.get("expected_channel_names") or profile.channel_names)
    probe = next((result.data for result in checks if result.name == "eeg_sample_probe"), {}) or {}
    if record_eeg:
        channel_contract = assess_channel_contract(
            list(probe.get("mapped_channel_names") or []),
            expected_channel_names,
            original_names=list(probe.get("original_channel_names") or []),
            mapping_source=probe.get("channel_mapping_source"),
            mapping_version=eeg.get("mapping_version"),
            require_eeg=require_eeg,
        )
    else:
        channel_contract = {
            "status": "skip",
            "detail": "EEG channel contract skipped because recording is disabled",
            "expected_channel_order": expected_channel_names,
            "observed_channel_order": [],
            "original_device_labels": [],
            "mapping_source": None,
            "mapping_version": eeg.get("mapping_version"),
            "count_matches": None,
            "order_matches": None,
            "missing_channels": [],
            "unexpected_channels": [],
            "duplicate_channels": [],
            "skipped": True,
        }
    check_payloads.append(
        CheckResult(
            "dsart_channel_contract",
            channel_contract["status"],
            channel_contract["detail"],
            channel_contract,
        ).__dict__
    )
    sample_contract = (
        assess_sample_probe(probe, eeg, require_eeg=require_eeg)
        if record_eeg
        else {
            "status": "skip",
            "detail": "EEG sample contract skipped because recording is disabled",
            "skipped": True,
            "failures": [],
            "warnings": [],
        }
    )
    check_payloads.append(
        CheckResult(
            "dsart_sample_contract",
            sample_contract["status"],
            sample_contract["detail"],
            sample_contract,
        ).__dict__
    )
    storage = _storage_check(
        Path(config.get("runtime", {}).get("session_root", "data")),
        record_eeg=record_eeg,
    )
    check_payloads.append(storage.__dict__)
    recorder_backend = str(config.get("processes", {}).get("recorder", {}).get("backend", "lsl_csv"))
    xdf_probe: CheckResult | None = None
    if record_eeg and recorder_backend == "labrecorder_xdf":
        try:
            environment = labrecorder_environment(config)
        except Exception as exc:
            xdf_check = CheckResult(
                "labrecorder_xdf",
                "fail",
                f"managed XDF recorder is unavailable: {type(exc).__name__}: {exc}",
                {},
            )
        else:
            xdf_check = CheckResult(
                "labrecorder_xdf",
                "ok",
                "LabRecorder executable, PyXDF reader, and loopback control port are ready",
                environment,
            )
        check_payloads.append(xdf_check.__dict__)
        if xdf_check.status == "ok":
            xdf_probe = _run_xdf_preflight_probe(
                config,
                participant_id=participant_id,
                phase=phase,
                output_dir=output_dir,
                eeg_stream_identity=dict(probe.get("stream") or {}),
                prevalidated_recorder_environment=environment,
            )
            check_payloads.append(xdf_probe.__dict__)
    identity = CheckResult(
        "visit_identity",
        "ok" if participant_id.strip() and visit_id.strip() else "fail",
        f"participant={participant_id}; visit={visit_id}",
        {"participant_id": participant_id, "visit_id": visit_id, "recipe": recipe},
    )
    check_payloads.append(identity.__dict__)
    marker = _marker_preflight_check(
        config,
        participant_id,
        visit_id,
        phase,
        enabled=record_eeg,
        xdf_probe=xdf_probe,
    )
    check_payloads.append(marker.__dict__)

    electrode_path: Path | None = None
    if record_eeg:
        electrode_channel_names = list(eeg.get("electrode_channel_names") or expected_channel_names)
        electrode_report = _electrode_report(
            electrode_channel_names,
            probe,
            recipe=recipe,
            quality_file=electrode_quality_file,
            operator_note=electrode_note,
            operator_confirmed=electrodes_confirmed,
        )
        electrode_path = output_dir / f"{phase}_electrode_quality.json"
        _write_json_atomic(electrode_path, electrode_report)
        electrode_status = "ok"
        if any(row["quality_status"] == "failed" for row in electrode_report["channels"]):
            electrode_status = "fail"
        elif any(row["quality_status"] in {"warning", "unavailable"} for row in electrode_report["channels"]):
            electrode_status = "warn"
        electrode_check = CheckResult(
            "electrode_quality",
            electrode_status,
            f"electrode quality {electrode_status}; report={electrode_path}",
            {"report_file": str(electrode_path), "operator_confirmed": electrodes_confirmed},
        )
    else:
        electrode_check = CheckResult(
            "electrode_quality",
            "skip",
            "electrode quality check skipped because EEG recording is disabled",
            {"skipped": True},
        )
    check_payloads.append(electrode_check.__dict__)
    statuses = [str(item.get("status")) for item in check_payloads]
    status = "fail" if "fail" in statuses else ("warning" if "warn" in statuses else "pass")
    comparison = (
        compare_preflights(initial_preflight, probe, channel_contract)
        if phase == "second_preflight" and record_eeg
        else None
    )
    if comparison and comparison.get("status") == "fail":
        status = "fail"
    elif comparison and comparison.get("status") == "warning" and status == "pass":
        status = "warning"
    preflight_warnings = _preflight_warning_messages(check_payloads)
    nonblocking_warnings = _nonblocking_preflight_warning_messages(check_payloads)
    preflight_failures = [item["detail"] for item in check_payloads if item.get("status") == "fail"]
    if comparison and comparison.get("status") == "warning":
        comparison_warnings = list(comparison.get("warnings") or [])
        if not comparison_warnings and comparison.get("reason"):
            comparison_warnings = [comparison["reason"]]
        preflight_warnings.extend(str(item) for item in comparison_warnings)
    if comparison and comparison.get("status") == "fail":
        comparison_failures = list(comparison.get("failures") or [])
        if not comparison_failures and comparison.get("reason"):
            comparison_failures = [comparison["reason"]]
        preflight_failures.extend(str(item) for item in comparison_failures)
    report = {
        "schema": PREFLIGHT_SCHEMA,
        "phase": phase,
        "status": status,
        "participant_id": participant_id,
        "visit_id": visit_id,
        "recipe": recipe,
        "created_at": _now(),
        "acquisition_config_sha256": _acquisition_config_sha256(config),
        "acquisition_config_contract": "hardware_and_recorder_v1",
        "checks": check_payloads,
        "eeg_probe": probe,
        "channel_contract": channel_contract,
        "electrode_quality_file": None if electrode_path is None else str(electrode_path),
        "comparison_to_initial": comparison,
        "warnings": list(dict.fromkeys(preflight_warnings)),
        "nonblocking_warnings": list(dict.fromkeys(nonblocking_warnings)),
        "failures": list(dict.fromkeys(preflight_failures)),
    }
    report_path = output_dir / f"{phase}.json"
    report["report_file"] = str(report_path)
    _write_json_atomic(report_path, report)
    return report


def _preflight_warning_messages(checks: list[dict[str, Any]]) -> list[str]:
    """Flatten check-specific warning details into a concise operator list."""

    messages: list[str] = []
    for check in checks:
        if check.get("status") != "warn":
            continue
        data = dict(check.get("data") or {})
        nested = [str(item).strip() for item in data.get("warnings", []) if str(item).strip()]
        if nested:
            messages.extend(nested)
            continue
        detail = str(check.get("detail") or "").strip()
        if detail:
            messages.append(detail)
    return list(dict.fromkeys(messages))


def _nonblocking_preflight_warning_messages(checks: list[dict[str, Any]]) -> list[str]:
    """Return advisory warnings which must never gate an experiment."""

    return _preflight_warning_messages(
        [
            check
            for check in checks
            if dict(check.get("data") or {}).get("operator_gate") is False
        ]
    )


def _run_xdf_preflight_probe(
    config: dict[str, Any],
    *,
    participant_id: str,
    phase: str,
    output_dir: Path,
    eeg_stream_identity: dict[str, Any] | None = None,
    prevalidated_recorder_environment: dict[str, Any] | None = None,
) -> CheckResult:
    """Make and validate a short real XDF before a full acquisition phase."""

    probe_config = copy.deepcopy(config)
    recorder_config = dict(probe_config.get("processes", {}).get("recorder", {}) or {})
    probe_seconds = max(
        1.0,
        float(recorder_config.get("preflight_xdf_probe_seconds", 3.0)),
    )
    # This complete create_session hierarchy is nested inside the visit's
    # preflight directory. Keep its disposable path components deliberately
    # compact so LabRecorder remains below legacy Windows path limits.
    probe_root = output_dir / "xdfp"
    probe_config.setdefault("runtime", {})["session_root"] = str(probe_root.resolve())
    probe_config.setdefault("experiment", {}).update(
        {
            "experiment_id": "xdfp",
            "participant_id": participant_id,
            "task": "xdfp",
        }
    )
    paths = create_session(
        probe_config,
        task="xdfp",
        participant_id=participant_id,
        root=probe_root,
    )
    persisted_config = load_config(paths.parameters)
    outlet: LslMarkerOutlet | NullMarkerOutlet | None = None
    receipt: LslMarkerReceiptRecorder | None = None
    recorder: LabRecorderXdfRecorder | None = None
    recorder_started = False
    recorder_summary: dict[str, Any] = {}
    drain_warning: str | None = None
    try:
        outlet = _make_marker_outlet(persisted_config, paths)
        if not isinstance(outlet, LslMarkerOutlet):
            raise RuntimeError("preflight XDF probe could not create its required marker outlet")
        receipt = _start_marker_receipt_recorder(outlet, paths)
        recorder = LabRecorderXdfRecorder(
            persisted_config,
            paths,
            startup_timeout_seconds=float(recorder_config.get("startup_timeout_seconds", 20.0)),
            preferred_eeg_stream=eeg_stream_identity,
            prevalidated_environment=prevalidated_recorder_environment,
        )
        recorder_summary = recorder.start()
        recorder_started = True
        start_timestamp = lsl_local_clock()
        if start_timestamp is None:
            raise RuntimeError("LSL local clock was unavailable for the XDF preflight start marker")
        outlet.push("xdf_preflight_start", timestamp=start_timestamp)
        sleep(probe_seconds)
        end_timestamp = lsl_local_clock()
        if end_timestamp is None:
            raise RuntimeError("LSL local clock was unavailable for the XDF preflight end marker")
        outlet.push("xdf_preflight_end", timestamp=end_timestamp)
        drain_warning = _drain_emitted_markers(persisted_config, outlet, receipt)
        recorder_summary = recorder.stop(reason="preflight_probe_complete")
        recorder_started = False
        receipt_summary = receipt.stop()
        receipt = None
        outlet.close()
        outlet = None
        if recorder_summary.get("status") != "stopped":
            raise RuntimeError(
                str(recorder_summary.get("error") or "preflight XDF recorder did not stop cleanly")
            )
        if receipt_summary.get("status") != "stopped":
            raise RuntimeError(
                str(receipt_summary.get("error") or "preflight marker receipt did not stop cleanly")
            )
        validation = validate_xdf_recording(paths.root, required=True)
        failures = [str(item) for item in validation.get("failures") or []]
        warnings = [str(item) for item in validation.get("warnings") or []]
        mirror_warning = str(recorder_summary.get("csv_mirror_warning") or "").strip()
        if mirror_warning:
            warnings.append(mirror_warning)
        if drain_warning:
            warnings.append(drain_warning)
        warnings = list(dict.fromkeys(warnings))
        status = "fail" if failures else ("warn" if warnings else "ok")
        if failures:
            detail = "XDF preflight recording failed: " + "; ".join(failures[:3])
        elif warnings:
            detail = "XDF preflight recording warnings: " + "; ".join(warnings[:3])
        else:
            detail = f"{probe_seconds:g}-second XDF preflight recording passed full validation"
        return CheckResult(
            "xdf_recording_probe",
            status,
            detail,
            {
                "session_dir": str(paths.root),
                "probe_seconds": probe_seconds,
                "recorder_summary": recorder_summary,
                "validation": validation,
                "warnings": warnings,
                "failures": failures,
            },
        )
    except Exception as exc:
        return CheckResult(
            "xdf_recording_probe",
            "fail",
            f"XDF preflight recording could not be completed: {type(exc).__name__}: {exc}",
            {
                "session_dir": str(paths.root),
                "probe_seconds": probe_seconds,
                "recorder_summary": recorder_summary,
            },
        )
    finally:
        if recorder_started and recorder is not None:
            try:
                recorder.stop(reason="preflight_probe_cleanup")
            except Exception:
                pass
        if receipt is not None:
            try:
                receipt.stop()
            except Exception:
                pass
        if outlet is not None:
            try:
                outlet.close()
            except Exception:
                pass


def assess_channel_contract(
    observed_names: list[str],
    expected_names: list[str],
    *,
    original_names: list[str] | None = None,
    mapping_source: str | None = None,
    require_eeg: bool,
    mapping_version: int | None = None,
) -> dict[str, Any]:
    duplicates = sorted({name for name in observed_names if observed_names.count(name) > 1})
    missing = [name for name in expected_names if name not in observed_names]
    unexpected = [name for name in observed_names if name not in expected_names]
    count_matches = len(observed_names) == len(expected_names)
    order_matches = observed_names == expected_names
    if not observed_names:
        status = "fail" if require_eeg else "warn"
        detail = "no mapped EEG channel names were available"
    elif duplicates or missing or not count_matches:
        status = "fail"
        detail = "EEG channel count or labels do not match the configured physical profile"
    elif not order_matches:
        status = "fail"
        detail = "EEG channels are present but their order does not match the configured physical profile"
    else:
        status = "ok"
        detail = "EEG channel labels and order match the configured physical profile"
    return {
        "status": status,
        "detail": detail,
        "expected_channel_order": expected_names,
        "observed_channel_order": observed_names,
        "original_device_labels": list(original_names or []),
        "mapping_source": mapping_source,
        "mapping_version": mapping_version,
        "count_matches": count_matches,
        "order_matches": order_matches,
        "missing_channels": missing,
        "unexpected_channels": unexpected,
        "duplicate_channels": duplicates,
    }


def assess_sample_probe(
    probe: dict[str, Any],
    eeg_config: dict[str, Any],
    *,
    require_eeg: bool,
) -> dict[str, Any]:
    expected_rate = _optional_float(eeg_config.get("expected_sample_rate_hz"))
    stream = dict(probe.get("stream", {}) or {})
    quality = dict(probe.get("quality", {}) or {})
    observed_rate = _optional_float(stream.get("nominal_srate")) or _optional_float(quality.get("sample_rate_hz"))
    effective_rate = _optional_float(quality.get("effective_sample_rate_hz"))
    probe_seconds = _optional_float(probe.get("probe_seconds")) or _optional_float(eeg_config.get("sample_probe_seconds"))
    sample_count = int(probe.get("sample_count") or 0)
    expected_samples = (
        _optional_float(quality.get("expected_sample_count_from_timestamp_span"))
        or (None if expected_rate is None or probe_seconds is None else expected_rate * probe_seconds)
    )
    sample_fraction = _optional_float(
        quality.get("sample_fraction_of_expected_from_timestamp_span")
    )
    if sample_fraction is None and expected_samples:
        sample_fraction = sample_count / expected_samples
    quality_config = dict(eeg_config.get("quality_check", {}) or {})
    rate_tolerance_fraction = max(
        0.001,
        float(quality_config.get("effective_sample_rate_warning_tolerance_fraction", 0.02)),
    )
    minimum_sample_fraction = min(
        1.0,
        max(0.0, float(quality_config.get("minimum_sample_fraction_warning", 0.98))),
    )
    failures: list[str] = []
    warnings: list[str] = []
    if probe.get("status") not in {"ok", "warn"} or sample_count <= 0:
        (failures if require_eeg else warnings).append("sample probe did not receive EEG samples")
    if expected_rate is not None:
        if observed_rate is None:
            warnings.append("EEG stream did not declare a nominal sample rate")
        elif abs(observed_rate - expected_rate) >= 1.0:
            warnings.append(
                f"Nominal EEG rate is {observed_rate:g} Hz; expected {expected_rate:g} Hz"
            )
        if effective_rate is None:
            warnings.append("Measured EEG rate could not be calculated from source timestamps")
        elif abs(effective_rate - expected_rate) / expected_rate > rate_tolerance_fraction:
            warnings.append(
                f"Measured EEG rate is {effective_rate:.1f} Hz; expected about {expected_rate:g} Hz"
            )
    if eeg_config.get("recording_lsl_processing") == "source_preserving":
        correction = _optional_float(probe.get("initial_time_correction_seconds"))
        if correction is None:
            warnings.append(
                "LSL time correction is unavailable; source EEG timestamps cannot be aligned to task markers"
            )
    if sample_fraction is not None and sample_fraction < minimum_sample_fraction:
        warnings.append(f"EEG probe retained {sample_fraction:.1%} of the expected samples")
    if quality:
        invalid_rows = int(quality.get("invalid_sample_row_count") or 0)
        if invalid_rows:
            warnings.append(
                f"EEG probe received {invalid_rows} sample row(s) with the wrong channel width"
            )
        if not bool(quality.get("timestamps_finite", False)):
            warnings.append("EEG probe timestamps are missing or non-finite")
        if not bool(quality.get("timestamps_strictly_increasing", False)):
            warnings.append("EEG probe timestamps are not strictly increasing")
        gap_count = int(quality.get("timestamp_gap_warning_count") or 0)
        estimated_missing = int(quality.get("estimated_missing_samples") or 0)
        if gap_count:
            warnings.append(
                f"EEG probe found {gap_count} sampling gap(s), about {estimated_missing} missing sample(s)"
            )
        warning_channels = [str(name) for name in quality.get("warning_channels", [])]
        if warning_channels:
            warning_rows = [
                row for row in quality.get("channels", []) if row.get("status") == "warning"
            ]
            preview = ", ".join(
                f"{row.get('channel_name')} ({', '.join(row.get('warnings') or ['quality warning'])})"
                for row in warning_rows[:6]
            ) or ", ".join(warning_channels[:6])
            suffix = "" if len(warning_channels) <= 6 else f" (+{len(warning_channels) - 6} more)"
            warnings.append(
                f"Signal quality on {len(warning_channels)} channel(s): {preview}{suffix}"
            )
    elif require_eeg:
        failures.append("EEG probe did not provide timestamp-continuity diagnostics")
    status = "fail" if failures else ("warn" if warnings else "ok")
    detail = "; ".join(failures or warnings) if failures or warnings else "EEG sample rate, volume, and timestamps passed"
    return {
        "status": status,
        "detail": detail,
        "expected_sample_rate_hz": expected_rate,
        "observed_sample_rate_hz": observed_rate,
        "effective_sample_rate_hz": effective_rate,
        "probe_seconds": probe_seconds,
        "sample_count": sample_count,
        "expected_sample_count": expected_samples,
        "sample_fraction": sample_fraction,
        "timestamp_diagnostics_available": bool(quality),
        "failures": failures,
        "warnings": warnings,
    }


def _accept_recording_preflight(report: dict[str, Any], options: DsartRecordingOptions) -> None:
    """Expose the gate to the operator and persist a deliberate acceptance."""
    phase = str(report.get("phase", "preflight"))
    summary = {
        "phase": phase,
        "status": report.get("status"),
        "channel_contract": report.get("channel_contract", {}).get("status"),
        "warnings": report.get("warnings", []),
        "electrode_quality_file": report.get("electrode_quality_file"),
        "report_file": report.get("report_file"),
    }
    print(json.dumps({"dsart_recording_preflight": summary}, indent=2, sort_keys=True))
    warnings = [str(item).strip() for item in report.get("warnings", []) if str(item).strip()]
    nonblocking_warnings = {
        str(item).strip()
        for item in report.get("nonblocking_warnings", [])
        if str(item).strip()
    }
    blocking_warnings = [warning for warning in warnings if warning not in nonblocking_warnings]
    accepted_by = "software_only"
    accepted = True
    if options.task_mode == "psychopy" and options.record_eeg:
        if warnings:
            print(f"{phase}: preflight warnings")
            for index, warning in enumerate(warnings, start=1):
                advisory = (
                    " (advisory only; the run will continue)"
                    if warning in nonblocking_warnings
                    else ""
                )
                print(f"  {index}. {warning}{advisory}")
        if options.electrodes_confirmed and not blocking_warnings:
            accepted_by = "--confirm-electrodes"
        else:
            requested_actions = []
            if blocking_warnings:
                requested_actions.append("review the warnings")
            if not options.electrodes_confirmed:
                requested_actions.append("inspect cap contact/impedance")
            action_text = " and ".join(requested_actions)
            accepted = _prompt_operator_acceptance(
                f"{phase}: {action_text}. Type Y/YES to continue or N/NO to decline: "
            )
            accepted_by = "interactive_terminal"
    report["operator_acceptance"] = {
        "accepted": accepted,
        "method": accepted_by,
        "accepted_at": _now() if accepted else None,
        "operator": options.operator,
        "warning_count": len(warnings),
        "blocking_warning_count": len(blocking_warnings),
        "nonblocking_warning_count": len(nonblocking_warnings),
        "warnings_accepted": accepted and bool(blocking_warnings),
    }
    report_file = report.get("report_file")
    if report_file:
        try:
            _write_json_atomic(Path(str(report_file)), report)
        except Exception as exc:
            report.setdefault("acceptance_persistence_warnings", []).append(
                f"preflight acceptance could not be added to {report_file}: "
                f"{type(exc).__name__}: {exc}"
            )
    electrode_file = report.get("electrode_quality_file")
    if accepted and electrode_file:
        electrode_report = _load_json(Path(str(electrode_file))) or {}
        electrode_report["operator_confirmed"] = True
        electrode_report["operator_confirmation_method"] = accepted_by
        electrode_report["operator_confirmation_at"] = _now()
        try:
            _write_json_atomic(Path(str(electrode_file)), electrode_report)
        except Exception as exc:
            report.setdefault("acceptance_persistence_warnings", []).append(
                f"electrode acceptance could not be added to {electrode_file}: "
                f"{type(exc).__name__}: {exc}"
            )
    if not accepted:
        raise RuntimeError(f"{phase} electrode/contact check was not accepted by the operator")


def _accept_post_recording_warnings(
    result: dict[str, Any],
    options: DsartRecordingOptions,
    phase: str,
) -> None:
    """Show nonfatal validation warnings before advancing to another acquisition phase."""
    validation = dict(result.get("validation") or {})
    candidates = [
        *list(result.get("warnings") or []),
        *list(validation.get("warnings") or []),
    ]
    warnings = list(dict.fromkeys(str(item).strip() for item in candidates if str(item).strip()))
    if not warnings:
        return
    accepted = True
    method = "software_only"
    if options.task_mode == "psychopy" and options.record_eeg:
        print(f"{phase}: recording completed with warnings")
        for index, warning in enumerate(warnings, start=1):
            print(f"  {index}. {warning}")
        accepted = _prompt_operator_acceptance(
            f"{phase}: type Y/YES to accept these warnings or N/NO to decline: "
        )
        method = "interactive_terminal"
    acceptance = {
        "accepted": accepted,
        "method": method,
        "accepted_at": _now() if accepted else None,
        "operator": options.operator,
        "warnings": warnings,
    }
    result["operator_warning_acceptance"] = acceptance
    session_dir = result.get("session_dir")
    if session_dir:
        try:
            _write_json_atomic(
                Path(str(session_dir)) / "logs" / "post_recording_warning_acceptance.json",
                acceptance,
            )
        except Exception as exc:
            result.setdefault("warnings", []).append(
                f"warning acceptance record could not be published: {type(exc).__name__}: {exc}"
            )
    if not accepted:
        raise RuntimeError(f"{phase} warnings were not accepted; raw recording was retained")


def _prompt_operator_acceptance(prompt: str) -> bool:
    """Read an explicit, case-insensitive Y/YES or N/NO operator decision."""

    while True:
        try:
            answer = input(prompt)
        except EOFError as exc:
            raise RuntimeError(
                "operator confirmation input closed before a Y/YES or N/NO response"
            ) from exc
        normalized = answer.strip().casefold()
        if normalized in {"y", "yes"}:
            return True
        if normalized in {"n", "no"}:
            return False
        print("Please enter Y/YES to continue or N/NO to decline.")


def compare_preflights(
    initial: dict[str, Any] | None,
    second_probe: dict[str, Any],
    second_contract: dict[str, Any],
) -> dict[str, Any]:
    if not initial:
        return {"status": "warning", "reason": "initial preflight unavailable for comparison"}
    first_probe = dict(initial.get("eeg_probe", {}) or {})
    first_contract = dict(initial.get("channel_contract", {}) or {})
    first_stream = dict(first_probe.get("stream", {}) or {})
    second_stream = dict(second_probe.get("stream", {}) or {})
    first_quality = dict(first_probe.get("quality", {}) or {})
    second_quality = dict(second_probe.get("quality", {}) or {})
    first_status = {
        row.get("channel_name"): row.get("status") for row in first_quality.get("channels", [])
    }
    second_status = {
        row.get("channel_name"): row.get("status") for row in second_quality.get("channels", [])
    }
    changed_channels = [
        name
        for name in sorted(set(first_status) | set(second_status))
        if first_status.get(name) != second_status.get(name)
    ]
    first_rate = _optional_float(first_stream.get("nominal_srate"))
    second_rate = _optional_float(second_stream.get("nominal_srate"))
    rate_difference = None if first_rate is None or second_rate is None else second_rate - first_rate
    first_correction = _optional_float(first_probe.get("initial_time_correction_seconds"))
    second_correction = _optional_float(second_probe.get("initial_time_correction_seconds"))
    correction_difference = (
        None if first_correction is None or second_correction is None else second_correction - first_correction
    )
    source_changed = first_stream.get("source_id") != second_stream.get("source_id")
    failures = []
    warnings = []
    if second_contract.get("status") == "fail":
        failures.append("second channel contract failed")
    if first_contract.get("expected_channel_order") != second_contract.get("expected_channel_order"):
        failures.append("configured channel order changed between preflights")
    if rate_difference is not None and abs(rate_difference) >= 1.0:
        warnings.append("sample rate changed by at least 1 Hz after the break")
    if source_changed:
        warnings.append("EEG stream source ID changed after the break")
    if correction_difference is not None and abs(correction_difference) >= 0.002:
        warnings.append("LSL time correction changed by at least 2 ms after the break")
    if changed_channels:
        warnings.append("one or more channel quality statuses changed")
    result = {
        "status": "fail" if failures else ("warning" if warnings else "pass"),
        "initial_preflight_status": initial.get("status"),
        "second_channel_contract_status": second_contract.get("status"),
        "channels_whose_status_changed": changed_channels,
        "sample_rate_difference_hz": rate_difference,
        "initial_time_correction_seconds": first_correction,
        "second_time_correction_seconds": second_correction,
        "time_correction_difference_seconds": correction_difference,
        "stream_source_id_changed": source_changed,
        "initial_stream_source_id": first_stream.get("source_id"),
        "second_stream_source_id": second_stream.get("source_id"),
        "warnings": warnings,
        "failures": failures,
    }
    return result


def run_resting_baseline(
    config: dict[str, Any],
    options: DsartRecordingOptions,
    *,
    visit_id: str,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    _require_preflight_acquisition_config(config, preflight, phase="resting baseline")
    baseline_config = copy.deepcopy(config)
    baseline_task = "study1_baseline" if options.recipe == "study1" else "dsart_baseline"
    recorder_backend = str(
        baseline_config.get("processes", {}).get("recorder", {}).get("backend", "lsl_csv")
    )
    baseline_config.setdefault("experiment", {}).update(
        {
            "experiment_id": f"{options.recipe}_visit_{visit_id}",
            "participant_id": options.participant_id,
            "task": baseline_task,
            "components": {
                "preflight": "default",
                "task": baseline_task,
                "eeg_recorder": recorder_backend,
                "realtime_processor": "disabled",
                "feedback": "disabled",
                "analysis": "disabled",
            },
        }
    )
    _disable_nonrecording_processes(baseline_config, recorder_backend=recorder_backend)
    paths = create_session(baseline_config, task=baseline_task, participant_id=options.participant_id)
    baseline_config = load_config(paths.parameters)
    _write_json_atomic(paths.logs / "preflight.json", preflight)
    telemetry = Telemetry.from_config(baseline_config, paths, component="dsart.baseline")
    manager = FeedbackManager(baseline_config, paths, record_eeg=options.record_eeg)
    marker_outlet = _prestart_baseline_marker_outlet(
        baseline_config,
        paths,
        recorder_enabled=bool(manager.processes["recorder"]["enabled"]),
    )
    result: dict[str, Any] = {
        "schema": BASELINE_SCHEMA,
        "status": "failed",
        "mode": options.task_mode,
        "phases": [],
        "planned_duration_seconds": _planned_baseline_duration(baseline_config),
        "actual_duration_seconds": 0.0,
        "aborted": False,
    }
    lifecycle_errors: list[str] = []
    try:
        manager.start_before_task()
        result = _run_baseline_protocol(
            baseline_config,
            paths,
            mode=options.task_mode,
            record_eeg=options.record_eeg,
            telemetry=telemetry,
            marker_outlet=marker_outlet,
        )
    except KeyboardInterrupt:
        result.update(
            {
                "status": "aborted",
                "aborted": True,
                "abort_reason": "keyboard_interrupt",
                "failure_kind": "operator_interrupt",
            }
        )
    except Exception as exc:
        result.update(
            {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "failure_kind": "baseline_runtime_exception",
            }
        )
    finally:
        try:
            manager.stop_after_task()
        except Exception as exc:
            lifecycle_errors.append(f"process shutdown failed: {type(exc).__name__}: {exc}")
        try:
            if marker_outlet is not None:
                marker_outlet.close()
        except Exception as exc:
            lifecycle_errors.append(f"marker outlet shutdown failed: {type(exc).__name__}: {exc}")
    try:
        manager_summary = manager.summary()
    except Exception as exc:
        manager_summary = {
            "status": "failed",
            "processes": {},
            "notes": [f"manager summary failed: {type(exc).__name__}: {exc}"],
        }
        lifecycle_errors.extend(manager_summary["notes"])
    try:
        eeg_summary = manager.eeg_summary
    except Exception as exc:
        eeg_summary = None
        lifecycle_errors.append(f"recorder summary read failed: {type(exc).__name__}: {exc}")
    result.update(
        {
            "session_dir": str(paths.root),
            "eeg": eeg_summary,
            "processes": manager_summary,
        }
    )
    if lifecycle_errors:
        result.setdefault("warnings", []).extend(lifecycle_errors)
        result["lifecycle_errors"] = lifecycle_errors
    try:
        baseline_validation = _baseline_recording_validation(paths, result, record_eeg=options.record_eeg)
    except Exception as exc:
        baseline_validation = {
            "status": "fail",
            "failures": [
                f"baseline validation could not be completed: {type(exc).__name__}: {exc}"
            ],
            "warnings": ["raw recording was retained and must not be overwritten"],
        }
    result["validation"] = baseline_validation
    if baseline_validation["failures"]:
        result["status"] = "failed"
        result.setdefault("warnings", []).extend(baseline_validation["failures"])
    for target in (paths.events / "dsart_baseline_results.json", paths.completion_summary):
        try:
            _write_json_atomic(target, result)
        except Exception as exc:
            result.setdefault("warnings", []).append(
                f"baseline completion report could not be published to {target}: {type(exc).__name__}: {exc}; "
                "raw recording was retained"
            )
    return result


def _run_baseline_protocol(
    config: dict[str, Any],
    paths: SessionPaths,
    *,
    mode: str,
    record_eeg: bool,
    telemetry: Telemetry,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet | None = None,
) -> dict[str, Any]:
    baseline = dict(config.get("recording_suite", {}).get("baseline", {}) or {})
    eyes_open_seconds = float(baseline.get("eyes_open_seconds", 120.0))
    eyes_closed_seconds = float(baseline.get("eyes_closed_seconds", 120.0))
    if mode == "dry-run":
        return _run_baseline_dry(
            config,
            paths,
            telemetry,
            eyes_open_seconds,
            eyes_closed_seconds,
            record_eeg=record_eeg,
            marker_outlet=marker_outlet,
        )
    if mode != "psychopy":
        raise ValueError(f"unsupported baseline mode {mode}")
    return _run_baseline_psychopy(
        config,
        paths,
        telemetry,
        eyes_open_seconds,
        eyes_closed_seconds,
        record_eeg=record_eeg,
        marker_outlet=marker_outlet,
    )


def _planned_baseline_duration(config: dict[str, Any]) -> float:
    baseline = dict(config.get("recording_suite", {}).get("baseline", {}) or {})
    return float(baseline.get("eyes_open_seconds", 120.0)) + float(
        baseline.get("eyes_closed_seconds", 120.0)
    )


def _run_baseline_dry(
    config: dict[str, Any],
    paths: SessionPaths,
    telemetry: Telemetry,
    eyes_open_seconds: float,
    eyes_closed_seconds: float,
    *,
    record_eeg: bool,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet | None = None,
) -> dict[str, Any]:
    owns_marker_outlet = marker_outlet is None
    outlet: LslMarkerOutlet | NullMarkerOutlet = marker_outlet or NullMarkerOutlet("baseline dry-run")
    marker_receipt: LslMarkerReceiptRecorder | None = None
    start = monotonic()
    phases = []
    cleanup_warnings: list[str] = []
    try:
        if record_eeg:
            if marker_outlet is None:
                outlet = _make_marker_outlet(config, paths)
            marker_receipt = _start_marker_receipt_recorder(outlet, paths)
            settle_seconds = float(
                config.get("recording_rehearsal", {}).get(
                    "marker_discovery_settle_seconds",
                    0.5,
                )
            )
            if settle_seconds > 0:
                sleep(settle_seconds)
        with EventLogger(paths.behavior_csv, paths.events_jsonl, paths.triggers, telemetry, "dsart.baseline") as logger:
            current = start
            for name, duration in (("eyes_open", eyes_open_seconds), ("eyes_closed", eyes_closed_seconds)):
                phase_start = current
                phase_end = phase_start + duration
                start_label = f"dsart_baseline_{name}_start"
                end_label = f"dsart_baseline_{name}_end"
                start_lsl = _baseline_mark(
                    logger,
                    outlet,
                    start_label,
                    phase_start,
                    event_type="SYSTEM",
                    phase=name,
                )
                end_lsl = _baseline_mark(
                    logger,
                    outlet,
                    end_label,
                    phase_end,
                    event_type="SYSTEM",
                    phase=name,
                )
                phases.append(
                    _baseline_phase_result(
                        name,
                        duration,
                        phase_start,
                        phase_end,
                        start_lsl,
                        end_lsl,
                        "completed",
                        False,
                    )
                )
                current = phase_end
    finally:
        if marker_receipt is not None:
            drain_warning = _drain_emitted_markers(config, outlet, marker_receipt)
            if drain_warning:
                cleanup_warnings.append(drain_warning)
        failures = _close_resources(
            ("marker receipt recorder", marker_receipt),
            ("marker outlet", outlet if owns_marker_outlet else None),
        )
        if failures:
            raise RuntimeError("baseline dry-run cleanup failed: " + "; ".join(failures))
    result = {
        "schema": BASELINE_SCHEMA,
        "status": "completed",
        "mode": "dry-run",
        "phases": phases,
        "planned_duration_seconds": eyes_open_seconds + eyes_closed_seconds,
        "actual_duration_seconds": eyes_open_seconds + eyes_closed_seconds,
        "aborted": False,
    }
    if cleanup_warnings:
        result["warnings"] = cleanup_warnings
    return result


def _run_baseline_psychopy(
    config: dict[str, Any],
    paths: SessionPaths,
    telemetry: Telemetry,
    eyes_open_seconds: float,
    eyes_closed_seconds: float,
    *,
    record_eeg: bool = False,
    marker_outlet: LslMarkerOutlet | NullMarkerOutlet | None = None,
) -> dict[str, Any]:
    prepare_psychopy_runtime(config.get("runtime", {}).get("runtime_cache_dir", ".runtime"))
    from psychopy import event, visual

    display = dict(config.get("hardware", {}).get("display", {}) or {})
    win = None
    owns_marker_outlet = marker_outlet is None
    outlet: LslMarkerOutlet | NullMarkerOutlet | None = marker_outlet
    marker_receipt: LslMarkerReceiptRecorder | None = None
    phases = []
    aborted = False
    abort_reason: str | None = None
    operator_interrupt = False
    failure: str | None = None
    cleanup_warnings: list[str] = []
    display_timing: dict[str, Any] | None = None
    audio_output: PsychoPyAudioOutput | None = None
    audio_output_report: dict[str, Any] = {
        "status": "skip",
        "detail": "eyes-closed baseline audio cue was not requested",
    }
    audio_end_signal: dict[str, Any] = {
        "status": "skip",
        "detail": "eyes-closed baseline audio cue was not reached",
    }
    suite_config = dict(config.get("recording_suite", {}) or {})
    recorder_monitor = RecorderHealthMonitor(
        paths.process_logs / "recorder.status.json",
        required=record_eeg,
        stall_timeout_seconds=float(suite_config.get("recorder_stall_timeout_seconds", 5.0)),
    )
    try:
        win = create_psychopy_window(visual, display, title="EEGle DSART Baseline")
        display_timing = measure_psychopy_refresh_rate(win, display)
        if eyes_closed_seconds > 0.0 and audio_output_enabled(config):
            audio_output, audio_output_report = prepare_psychopy_audio_output(config)
            print(f"[audio] {audio_output_report['detail']}", flush=True)
            if audio_output_report.get("status") != "ok":
                cleanup_warnings.append(str(audio_output_report["detail"]))
        clear_psychopy_keys(event)
        if marker_outlet is None:
            outlet = _make_marker_outlet(config, paths)
        if record_eeg:
            marker_receipt = _start_marker_receipt_recorder(outlet, paths)
        with EventLogger(paths.behavior_csv, paths.events_jsonl, paths.triggers, telemetry, "dsart.baseline") as logger:
            if not _baseline_instruction(
                win,
                visual,
                event,
                "Keep your eyes open, remain still, relax, and look at the fixation point.\n\nPress SPACE to begin.",
            ):
                aborted = True
                abort_reason = "operator_abort"
            if not aborted:
                phases.append(
                    _psychopy_baseline_phase(
                        win,
                        visual,
                        event,
                        logger,
                        outlet,
                        name="eyes_open",
                        duration=eyes_open_seconds,
                        draw_fixation=True,
                        recorder_monitor=recorder_monitor,
                    )
                )
                aborted = _baseline_phase_aborted(phases[-1])
                if aborted:
                    abort_reason = str(phases[-1].get("abort_reason") or "baseline_phase_abort")
            if not aborted and not _baseline_instruction(
                win,
                visual,
                event,
                (
                    "Close your eyes, remain still, relax, and keep your eyes closed until "
                    + (
                        "you hear the end signal."
                        if audio_output is not None and audio_output_report.get("status") == "ok"
                        else "the operator tells you the interval is complete."
                    )
                    + "\n\nPress SPACE, then close your eyes."
                ),
            ):
                aborted = True
                abort_reason = "operator_abort"
            if not aborted:
                phases.append(
                    _psychopy_baseline_phase(
                        win,
                        visual,
                        event,
                        logger,
                        outlet,
                        name="eyes_closed",
                        duration=eyes_closed_seconds,
                        draw_fixation=False,
                        recorder_monitor=recorder_monitor,
                    )
                )
                aborted = _baseline_phase_aborted(phases[-1])
                if aborted:
                    abort_reason = str(phases[-1].get("abort_reason") or "baseline_phase_abort")
                if not aborted:
                    audio_end_signal = _play_baseline_end_signal(audio_output, config)
                    print(f"[audio] {audio_end_signal['detail']}", flush=True)
                    if audio_end_signal.get("status") != "played":
                        cleanup_warnings.append(str(audio_end_signal["detail"]))
            if not aborted:
                _baseline_instruction(
                    win,
                    visual,
                    event,
                    "Baseline complete. Open your eyes.\n\nPress SPACE to continue.",
                )
    except KeyboardInterrupt:
        aborted = True
        abort_reason = "keyboard_interrupt"
        operator_interrupt = True
        failure = "KeyboardInterrupt: operator interrupt"
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if marker_receipt is not None and outlet is not None:
            drain_warning = _drain_emitted_markers(config, outlet, marker_receipt)
            if drain_warning:
                cleanup_warnings.append(drain_warning)
        cleanup_warnings.extend(
            _close_resources(
                ("marker receipt recorder", marker_receipt),
                ("marker outlet", outlet if owns_marker_outlet else None),
                ("audio output", audio_output),
                ("PsychoPy window", win),
            )
        )
    actual_duration = sum(float(row.get("actual_duration_seconds") or 0.0) for row in phases)
    result = {
        "schema": BASELINE_SCHEMA,
        "status": "failed" if failure and not operator_interrupt else ("aborted" if aborted else "completed"),
        "mode": "psychopy",
        "phases": phases,
        "planned_duration_seconds": eyes_open_seconds + eyes_closed_seconds,
        "actual_duration_seconds": actual_duration,
        "aborted": aborted,
        "display_timing": display_timing,
        "audio_output": audio_output_report,
        "baseline_end_signal": audio_end_signal,
    }
    if failure:
        result["error"] = failure
    if abort_reason:
        result["abort_reason"] = abort_reason
    if cleanup_warnings:
        result["warnings"] = cleanup_warnings
    return result


def _psychopy_baseline_phase(
    win: Any,
    visual: Any,
    event_module: Any,
    logger: EventLogger,
    outlet: LslMarkerOutlet | NullMarkerOutlet,
    *,
    name: str,
    duration: float,
    draw_fixation: bool,
    recorder_monitor: RecorderHealthMonitor | None = None,
) -> dict[str, Any]:
    holder: dict[str, Any] = {}
    fixation = visual.TextStim(win, text="+", height=0.12, color="white") if draw_fixation else None
    if fixation is not None:
        fixation.draw()
    win.callOnFlip(_capture_baseline_start, holder, outlet, f"dsart_baseline_{name}_start", name)
    win.flip()
    _log_captured_baseline_start(holder, logger)
    start = float(holder["monotonic"])
    start_lsl = _optional_float(holder.get("lsl"))
    aborted = False
    abort_reason = None
    recorder_status: dict[str, Any] | None = None
    deadline = start + duration
    next_health_check = start
    while monotonic() < deadline:
        redraw_psychopy_after_resize(win, fixation)
        now = monotonic()
        if recorder_monitor is not None and now >= next_health_check:
            health = recorder_monitor.check()
            next_health_check = now + 0.25
            if health.ok and bool(getattr(health, "warning", False)):
                _baseline_mark(
                    logger,
                    outlet,
                    "dsart_baseline_recorder_warning",
                    now,
                    lsl_timestamp=lsl_local_clock(),
                    event_type="SYSTEM",
                    phase=name,
                    reason=health.reason,
                    recorder_status=health.status.get("status"),
                )
            elif not health.ok:
                aborted = True
                abort_reason = f"recorder_health_failure: {health.reason}"
                recorder_status = health.status
                _baseline_mark(
                    logger,
                    outlet,
                    "dsart_baseline_recorder_failure",
                    now,
                    lsl_timestamp=lsl_local_clock(),
                    event_type="SYSTEM",
                    phase=name,
                    reason=health.reason,
                    recorder_status=health.status.get("status"),
                )
                break
        keys = [value.name for value in poll_psychopy_keys(event_module)]
        if any(key in {"escape", "q"} for key in keys):
            aborted = True
            abort_reason = "operator_abort"
            break
        sleep(0.01)
    end = monotonic()
    end_lsl = lsl_local_clock()
    _baseline_mark(
        logger,
        outlet,
        f"dsart_baseline_{name}_end",
        end,
        lsl_timestamp=end_lsl,
        event_type="SYSTEM",
        phase=name,
        aborted=aborted,
    )
    result = _baseline_phase_result(
        name,
        duration,
        start,
        end,
        start_lsl,
        end_lsl,
        "aborted" if aborted else "completed",
        aborted,
    )
    if abort_reason:
        result["abort_reason"] = abort_reason
    if recorder_status is not None:
        result["recorder_status"] = recorder_status
    return result


def _capture_baseline_start(
    holder: dict[str, Any],
    outlet: LslMarkerOutlet | NullMarkerOutlet,
    label: str,
    phase: str,
) -> None:
    timestamp = monotonic()
    lsl_timestamp = lsl_local_clock()
    if isinstance(outlet, LslMarkerOutlet) and lsl_timestamp is None:
        raise RuntimeError("LSL local clock is unavailable for a required baseline flip marker")
    outlet.push(label, timestamp=lsl_timestamp)
    holder.update(
        {
            "monotonic": timestamp,
            "lsl": lsl_timestamp,
            "event": {
                "label": label,
                "timestamp": timestamp,
                "event_type": "SYSTEM",
                "lsl_timestamp": lsl_timestamp,
                "task": "dsart_baseline",
                "phase": phase,
                "scheduled_on_flip": True,
                "marker_stream_name": getattr(outlet, "name", None),
                "marker_stream_type": getattr(outlet, "stream_type", None),
                "marker_stream_source_id": getattr(outlet, "source_id", None),
                "marker_emit_attempted": True,
            },
        }
    )


def _log_captured_baseline_start(holder: dict[str, Any], logger: EventLogger) -> None:
    payload = dict(holder["event"])
    label = str(payload.pop("label"))
    timestamp = float(payload.pop("timestamp"))
    event_type = str(payload.pop("event_type"))
    logger.mark(label, timestamp=timestamp, event_type=event_type, **payload)


def _baseline_mark(
    logger: EventLogger,
    outlet: LslMarkerOutlet | NullMarkerOutlet,
    label: str,
    timestamp: float,
    *,
    lsl_timestamp: float | None = None,
    event_type: str = "EVENT",
    **metadata: Any,
) -> float | None:
    marker_timestamp = lsl_local_clock() if lsl_timestamp is None else lsl_timestamp
    if isinstance(outlet, LslMarkerOutlet) and marker_timestamp is None:
        raise RuntimeError("LSL local clock is unavailable for a required baseline marker")
    outlet.push(label, timestamp=marker_timestamp)
    logger.mark(
        label,
        event_type=event_type,
        timestamp=timestamp,
        lsl_timestamp=marker_timestamp,
        task="dsart_baseline",
        marker_stream_name=getattr(outlet, "name", None),
        marker_stream_type=getattr(outlet, "stream_type", None),
        marker_stream_source_id=getattr(outlet, "source_id", None),
        marker_emit_attempted=True,
        **metadata,
    )
    return marker_timestamp


def run_dsart_child_session(
    config: dict[str, Any],
    options: DsartRecordingOptions,
    *,
    visit_id: str,
    session_index: int,
    seed: int,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    if options.task_mode == "psychopy":
        return _run_dsart_child_session_isolated(
            config,
            options,
            visit_id=visit_id,
            session_index=session_index,
            seed=seed,
            preflight=preflight,
        )
    return _run_dsart_child_session_inline(
        config,
        options,
        visit_id=visit_id,
        session_index=session_index,
        seed=seed,
        preflight=preflight,
    )


def _run_dsart_child_session_isolated(
    config: dict[str, Any],
    options: DsartRecordingOptions,
    *,
    visit_id: str,
    session_index: int,
    seed: int,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    """Run each visual session in a fresh interpreter to isolate native GUI state."""
    output_root = Path(config.get("runtime", {}).get("session_root", "data")).expanduser().resolve()
    worker_dir = output_root / "recording_suites" / options.participant_id / visit_id / options.recipe / "phase_workers"
    attempt_token = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    request_path = worker_dir / f"session-{session_index}-{attempt_token}.request.json"
    result_path = worker_dir / f"session-{session_index}-{attempt_token}.result.json"
    status_path = worker_dir / f"session-{session_index}-{attempt_token}.status.json"
    request = {
        "schema": PHASE_WORKER_SCHEMA,
        "config": config,
        "options": _serialize_recording_options(options),
        "visit_id": visit_id,
        "session_index": int(session_index),
        "seed": int(seed),
        "preflight": preflight,
        "requested_at": _now(),
    }
    _write_json_atomic(request_path, request)
    command = [
        sys.executable,
        "-m",
        "eegle.pipelines.dsart_phase_worker",
        "--request",
        str(request_path),
        "--result",
        str(result_path),
        "--status",
        str(status_path),
    ]
    try:
        process = subprocess.Popen(command)
    except OSError as exc:
        return {
            "status": "failed",
            "session_index": session_index,
            "session_dir": None,
            "seed": int(seed),
            "error": f"DSART phase worker could not start: {type(exc).__name__}: {exc}",
            "failure_kind": "phase_worker_start_failure",
            "practice_status": "unknown",
            "phase_worker": {
                "mode": "fresh_python_process",
                "return_code": None,
                "request_file": str(request_path),
                "result_file": str(result_path),
                "status_file": str(status_path),
            },
        }
    print(
        f"DSART task worker started (pid {process.pid}); progress: {status_path}",
        flush=True,
    )
    last_stage: str | None = None
    while True:
        try:
            return_code = int(process.wait(timeout=1.0))
            break
        except subprocess.TimeoutExpired:
            try:
                status_payload = _load_json(status_path) or {}
            except (OSError, json.JSONDecodeError):
                # Progress reporting is diagnostic only. A transient Windows
                # reader/replace race must not interrupt the visual task.
                status_payload = {}
            stage = status_payload.get("stage")
            if stage and stage != last_stage:
                print(f"DSART task worker stage: {stage}", flush=True)
                last_stage = str(stage)
    result = _load_json(result_path)
    worker_metadata = {
        "mode": "fresh_python_process",
        "return_code": return_code,
        "request_file": str(request_path),
        "result_file": str(result_path),
        "status_file": str(status_path),
    }
    if result is None:
        return {
            "status": "failed",
            "session_index": session_index,
            "session_dir": None,
            "seed": int(seed),
            "error": f"DSART phase worker exited with code {return_code} without a result artifact",
            "failure_kind": "phase_worker_failure",
            "practice_status": "unknown",
            "phase_worker": worker_metadata,
        }
    result["phase_worker"] = worker_metadata
    if return_code != 0 and result.get("status") == "completed":
        result["status"] = "failed"
        result["error"] = f"DSART phase worker exited with code {return_code} after reporting completion"
        result["failure_kind"] = "phase_worker_status_mismatch"
    return result


def _serialize_recording_options(options: DsartRecordingOptions) -> dict[str, Any]:
    payload = asdict(options)
    for key in ("config_path", "output_root", "electrode_quality_file"):
        if payload.get(key) is not None:
            payload[key] = str(payload[key])
    if payload.get("window_size") is not None:
        payload["window_size"] = list(payload["window_size"])
    return payload


def _deserialize_recording_options(payload: dict[str, Any]) -> DsartRecordingOptions:
    values = dict(payload)
    if values.get("window_size") is not None:
        values["window_size"] = tuple(int(value) for value in values["window_size"])
    return DsartRecordingOptions(**values)


def _practice_status_from_task_summary(
    task_summary: dict[str, Any],
    *,
    practice_enabled: bool,
) -> str:
    """Translate task-level practice outcome without mislabeling operator proceed."""

    if not practice_enabled:
        return "skipped"
    if task_summary.get("practice_passed") is True:
        return "passed"
    if task_summary.get("practice_proceeded_without_passing") is True:
        return "proceeded_without_passing"
    if "practice_passed" in task_summary:
        return "not_passed" if task_summary.get("practice_trials", 0) else "not_recorded"
    # Backward compatibility for summaries written before the explicit outcome
    # fields existed: a completed task with recorded practice had necessarily
    # passed the old finite-retry gate.
    return "passed" if task_summary.get("practice_trials", 0) else "not_recorded"


def _run_dsart_child_session_inline(
    config: dict[str, Any],
    options: DsartRecordingOptions,
    *,
    visit_id: str,
    session_index: int,
    seed: int,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    _require_preflight_acquisition_config(config, preflight, phase="Dynamic SART task")
    child_config = copy.deepcopy(config)
    child_config.setdefault("runtime", {})["session_root"] = str(
        Path(child_config.get("runtime", {}).get("session_root", "data")).expanduser().resolve()
    )
    study_segment = child_config.get("recording_suite", {}).get("study_segment")
    experiment_suffix = str(study_segment or f"session_{session_index}")
    child_config.setdefault("experiment", {}).update(
        {
            "experiment_id": f"{options.recipe}_visit_{visit_id}_{experiment_suffix}",
            "participant_id": options.participant_id,
            "task": "dynamic_sart",
        }
    )
    task = child_config.setdefault("tasks", {}).setdefault("dynamic_sart", {})
    task["master_seed"] = int(seed)
    child_config.setdefault("recording_suite", {})["require_live_recorder"] = bool(options.record_eeg)
    repeat_practice = bool(
        child_config.get("recording_suite", {}).get("session_2", {}).get("repeat_practice", False)
    )
    if session_index == 2 and not repeat_practice:
        task.setdefault("practice", {})["enabled"] = False
    practice_enabled = bool(task.setdefault("practice", {}).get("enabled", True))
    _force_recording_only_contract(child_config)
    preflight_results = [
        CheckResult(
            str(row.get("name")),
            str(row.get("status")),
            str(row.get("detail", "")),
            dict(row.get("data") or {}),
        )
        for row in preflight.get("checks", [])
        if row.get("name") and row.get("status") in {"ok", "warn", "fail", "skip"}
    ]
    runner = ForwardExperimentRunner(
        child_config,
        task_name="dynamic_sart",
        task_mode=options.task_mode,
        participant_id=options.participant_id,
        trials=options.trials_per_session,
        record_eeg=options.record_eeg,
        require_eeg=options.require_eeg,
        preflight_results=preflight_results or None,
    )
    try:
        forward = runner.run()
    except Exception as exc:
        traceback_text = traceback.format_exc()
        traceback_file = None
        if runner.session_dir is not None:
            traceback_path = Path(runner.session_dir) / "logs" / "suite_exception.txt"
            try:
                _write_text_atomic(traceback_path, traceback_text)
                traceback_file = str(traceback_path)
            except Exception:
                traceback_file = None
        return {
            "status": "failed",
            "session_index": session_index,
            "session_dir": None if runner.session_dir is None else str(runner.session_dir),
            "seed": seed,
            "error": f"{type(exc).__name__}: {exc}",
            "failure_kind": "child_session_exception",
            "traceback_file": traceback_file,
            "practice_status": "skipped" if not practice_enabled else "unknown",
        }
    forward_payload = forward.as_dict()
    session_dir = Path(forward.session_dir)
    post_recording_warnings: list[str] = []
    task_summary = dict((forward_payload.get("task") or {}).get("summary") or {})
    try:
        _write_json_atomic(session_dir / "logs" / "suite_preflight.json", preflight)
    except Exception as exc:
        post_recording_warnings.append(
            f"preflight copy could not be published ({type(exc).__name__}: {exc}); raw recording was retained"
        )
    if options.recipe == "dsart32":
        try:
            write_dsart8_overlap_manifest(session_dir, child_config)
        except Exception as exc:
            post_recording_warnings.append(
                f"channel-overlap report could not be published ({type(exc).__name__}: {exc}); raw recording was retained"
            )
    validated = validate_dynamic_sart_forward_result(
        forward_payload,
        child_config,
        record_eeg=options.record_eeg,
        task_mode=options.task_mode,
    )
    sequence_manifest = dict(validated["stimulus_manifest"])
    validation = dict(validated["validation"])
    status = "completed" if not validation["failures"] else "partial"
    return {
        "status": status,
        "session_index": session_index,
        "session_dir": str(session_dir),
        "session_id": session_dir.name,
        "seed": int(seed),
        "sequence_hash": sequence_manifest.get("sequence_id"),
        "practice_status": _practice_status_from_task_summary(
            task_summary,
            practice_enabled=practice_enabled,
        ),
        "task_summary": task_summary,
        "validation": validation,
        "warnings": list(dict.fromkeys([*post_recording_warnings, *list(validation.get("warnings") or [])])),
        "raw_recording_retained": True,
        "forward": forward_payload,
    }


def validate_dynamic_sart_forward_result(
    forward_payload: dict[str, Any],
    config: dict[str, Any],
    *,
    record_eeg: bool,
    task_mode: str,
) -> dict[str, Any]:
    """Run the strict DSART completion checks used by suites and direct smoke runs."""

    session_value = forward_payload.get("session_dir")
    if not session_value:
        return {
            "stimulus_manifest": {},
            "dynamic_report": {},
            "analysis_error": None,
            "validation": {
                "status": "fail",
                "failures": ["forward experiment did not report a session directory"],
                "warnings": [],
            },
        }
    session_dir = Path(str(session_value)).expanduser().resolve()
    task_summary = dict((forward_payload.get("task") or {}).get("summary") or {})
    stimulus_manifest: dict[str, Any] = {}
    manifest_error: str | None = None
    try:
        stimulus_manifest = _load_json(session_dir / "events" / "stimulus_manifest.json") or {}
    except Exception as exc:
        manifest_error = f"{type(exc).__name__}: {exc}"
    analysis_error: str | None = None
    try:
        dynamic_report = analyze_dynamic_sart_session(session_dir, config)
    except Exception as exc:
        dynamic_report = {}
        analysis_error = f"{type(exc).__name__}: {exc}"
    try:
        validation = _child_session_validation(
            session_dir,
            task_summary,
            stimulus_manifest,
            record_eeg=record_eeg,
            task_mode=task_mode,
            dynamic_report=dynamic_report,
            analysis_error=analysis_error,
        )
    except Exception as exc:
        validation = {
            "status": "fail",
            "failures": [
                f"post-recording validation could not be completed: {type(exc).__name__}: {exc}"
            ],
            "warnings": ["raw recording was retained and must not be overwritten"],
        }
    failures = list(validation.get("failures") or [])
    warnings = list(validation.get("warnings") or [])
    if manifest_error:
        failures.append(f"stimulus manifest could not be read: {manifest_error}")
    if forward_payload.get("status") != "complete":
        failures.append(
            f"forward experiment did not complete successfully (status={forward_payload.get('status')})"
        )
    validation["failures"] = list(dict.fromkeys(str(item) for item in failures if str(item)))
    validation["warnings"] = list(dict.fromkeys(str(item) for item in warnings if str(item)))
    validation["status"] = (
        "fail"
        if validation["failures"]
        else ("warning" if validation["warnings"] else "pass")
    )
    return {
        "stimulus_manifest": stimulus_manifest,
        "dynamic_report": dynamic_report,
        "analysis_error": analysis_error,
        "validation": validation,
    }


def run_inter_session_break(
    config: dict[str, Any],
    options: DsartRecordingOptions,
    *,
    visit_id: str,
    visit_dir: Path,
    break_seconds: float,
) -> dict[str, Any]:
    events_path = visit_dir / "recording_suite_events.jsonl"
    marker_config = dict(config.get("hardware", {}).get("markers", {}) or {})
    source_id = f"eegle-dsart-visit-{_safe_token(options.participant_id)}-{_safe_token(visit_id)}"
    outlet: LslMarkerOutlet | NullMarkerOutlet | None = None
    try:
        outlet = LslMarkerOutlet(
            str(marker_config.get("lsl_stream_name", "EEGleMarkers")),
            str(marker_config.get("lsl_stream_type", "Markers")),
            source_id,
        )
    except Exception as exc:
        outlet = NullMarkerOutlet(str(exc))
        marker_setup_warning = f"inter-session break marker outlet unavailable: {type(exc).__name__}: {exc}"
    else:
        marker_setup_warning = None
    start: float | None = None
    start_lsl: float | None = None
    end: float | None = None
    end_lsl: float | None = None
    aborted = False
    cleanup_warnings: list[str] = [] if marker_setup_warning is None else [marker_setup_warning]
    try:
        start = monotonic()
        start_lsl = lsl_local_clock()
        start_marker_emitted = False
        if isinstance(outlet, LslMarkerOutlet) and start_lsl is None:
            cleanup_warnings.append("LSL local clock unavailable for the optional inter-session break start marker")
        else:
            try:
                outlet.push("dsart_inter_session_break_start", timestamp=start_lsl)
                start_marker_emitted = isinstance(outlet, LslMarkerOutlet)
            except Exception as exc:
                cleanup_warnings.append(f"inter-session break start marker failed: {type(exc).__name__}: {exc}")
        _append_jsonl_atomic_event(
            events_path,
            {
                "label": "dsart_inter_session_break_start",
                "timestamp_monotonic": start,
                "timestamp_lsl": start_lsl,
                "planned_duration_seconds": break_seconds,
                "marker_stream_source_id": getattr(outlet, "source_id", None),
                "lsl_marker_emitted": start_marker_emitted,
            },
        )
        if options.task_mode == "psychopy":
            deadline = start + break_seconds
            while monotonic() < deadline:
                sleep(min(0.25, max(0.0, deadline - monotonic())))
        end = monotonic() if options.task_mode == "psychopy" else start + break_seconds
        end_lsl = lsl_local_clock()
        end_marker_emitted = False
        if isinstance(outlet, LslMarkerOutlet) and end_lsl is None:
            cleanup_warnings.append("LSL local clock unavailable for the optional inter-session break end marker")
        else:
            try:
                outlet.push("dsart_inter_session_break_end", timestamp=end_lsl)
                end_marker_emitted = isinstance(outlet, LslMarkerOutlet)
            except Exception as exc:
                cleanup_warnings.append(f"inter-session break end marker failed: {type(exc).__name__}: {exc}")
        _append_jsonl_atomic_event(
            events_path,
            {
                "label": "dsart_inter_session_break_end",
                "timestamp_monotonic": end,
                "timestamp_lsl": end_lsl,
                "planned_duration_seconds": break_seconds,
                "actual_duration_seconds": end - start,
                "aborted": False,
                "marker_stream_source_id": getattr(outlet, "source_id", None),
                "lsl_marker_emitted": end_marker_emitted,
            },
        )
    except KeyboardInterrupt:
        aborted = True
        end = monotonic()
        end_lsl = lsl_local_clock()
        _append_jsonl_atomic_event(
            events_path,
            {
                "label": "dsart_inter_session_break_aborted",
                "timestamp_monotonic": end,
                "timestamp_lsl": end_lsl,
                "planned_duration_seconds": break_seconds,
                "actual_duration_seconds": None if start is None else end - start,
                "aborted": True,
                "marker_stream_source_id": getattr(outlet, "source_id", None),
            },
        )
        try:
            if outlet is not None:
                outlet.push("dsart_inter_session_break_aborted", timestamp=end_lsl)
        except Exception as exc:
            cleanup_warnings.append(f"break abort marker failed: {type(exc).__name__}: {exc}")
        raise
    finally:
        cleanup_warnings.extend(_close_resources(("inter-session break marker outlet", outlet)))
        if cleanup_warnings:
            _append_jsonl_atomic_event(
                events_path,
                {
                    "label": "dsart_inter_session_break_cleanup_warning",
                    "timestamp_monotonic": monotonic(),
                    "timestamp_lsl": lsl_local_clock(),
                    "warnings": cleanup_warnings,
                },
            )
    assert start is not None and end is not None
    return {
        "status": "aborted" if aborted else "completed",
        "start_monotonic": start,
        "end_monotonic": end,
        "start_lsl": start_lsl,
        "end_lsl": end_lsl,
        "planned_duration_seconds": break_seconds,
        "actual_duration_seconds": end - start,
        "events_file": str(events_path),
        "marker_stream_source_id": getattr(outlet, "source_id", None),
        "warnings": cleanup_warnings,
    }


def write_dsart8_overlap_manifest(session_dir: str | Path, config: dict[str, Any]) -> dict[str, Any]:
    root = Path(session_dir).expanduser().resolve()
    eeg = dict(config.get("hardware", {}).get("eeg", {}) or {})
    profile = expected_profile(str(eeg["profile"]), eeg.get("family"))
    full = list(profile.channel_names)
    matching = [name for name in DSART8_CHANNELS if name in full]
    missing = [name for name in DSART8_CHANNELS if name not in full]
    payload = {
        "schema": OVERLAP_SCHEMA,
        "dsart8_expected_labels": list(DSART8_CHANNELS),
        "matching_32_channel_labels": matching,
        "missing_overlap_labels": missing,
        "channel_indices_1_based": {name: full.index(name) + 1 for name in matching},
        "mapping_status": "complete" if not missing else "warning",
        "full_32_channel_order": full,
        "data_retention": "all 32 EEG channels remain in the canonical raw recording",
    }
    path = root / "events" / "dsart8_overlap_channels.json"
    _write_json_atomic(path, payload)
    payload["file"] = str(path)
    return payload


def _child_session_validation(
    session_dir: Path,
    task_summary: dict[str, Any],
    stimulus_manifest: dict[str, Any],
    *,
    record_eeg: bool,
    task_mode: str = "psychopy",
    dynamic_report: dict[str, Any] | None = None,
    analysis_error: str | None = None,
) -> dict[str, Any]:
    dynamic_report = dynamic_report or _load_json(session_dir / "reports" / "dynamic_sart_summary.json") or {}
    raw_integrity = _raw_eeg_integrity(session_dir, required=record_eeg)
    raw_metadata = raw_integrity["metadata"]
    raw_sample_contract = raw_integrity["raw_sample_contract"]
    failures = []
    warnings = []
    planned_rows = [
        row for row in list(stimulus_manifest.get("planned_trials") or []) if not bool(row.get("is_practice"))
    ]
    if planned_rows:
        expected = {
            "experimental_trials": len(planned_rows),
            "support_trials": sum(int(row.get("phase") == "support") for row in planned_rows),
            "query_trials": sum(int(row.get("phase") == "query") for row in planned_rows),
            "no_go_trial_count": sum(int(bool(row.get("is_no_go"))) for row in planned_rows),
        }
    elif stimulus_manifest:
        expected = {
            "experimental_trials": 600,
            "support_trials": 200,
            "query_trials": 400,
            "no_go_trial_count": 67,
        }
    else:
        expected = {}
        failures.append("stimulus manifest is missing; trial-plan and sequence identity cannot be validated")
    for field, value in expected.items():
        if int(task_summary.get(field, -1)) != value:
            failures.append(f"{field}={task_summary.get(field)}; expected {value}")
    if bool(task_summary.get("aborted")):
        failures.append("task reported aborted=true")
    if not bool(task_summary.get("support_complete")):
        failures.append("support reference did not complete")
    if dynamic_report:
        if dynamic_report.get("support_complete_event_count") != 1:
            failures.append("support-complete marker count is not exactly one")
        if not dynamic_report.get("marker_trial_parity", {}).get("matches", False):
            failures.append("stimulus marker/trial parity failed")
    else:
        warnings.append("post-recording DSART validation report was unavailable; raw recording was retained")
    if analysis_error:
        warnings.append(f"post-recording DSART analysis failed: {analysis_error}")
    failures.extend(raw_integrity["failures"])
    warnings.extend(raw_integrity["warnings"])
    if not record_eeg:
        warnings.append("software-only run did not record physical EEG")
    epoch_cfg = _load_json(session_dir / "parameters.json") or {}
    epoching = dict(epoch_cfg.get("realtime", {}).get("epoching", {}) or {})
    suite_config = dict(epoch_cfg.get("recording_suite", {}) or {})
    epoch_contract = dict(suite_config.get("epoch_contract", {}) or {})
    expected_tmin = float(epoch_contract.get("tmin_seconds", -2.0))
    expected_tmax = float(epoch_contract.get("tmax_seconds", -0.05))
    epoching_ready = (
        epoching.get("marker_prefix") == "dynamic_sart_stimulus_onset"
        and float(epoching.get("tmin_seconds", 0)) == expected_tmin
        and float(epoching.get("tmax_seconds", 0)) == expected_tmax
        and epoching.get("timebase") == "lsl"
        and not bool(epoching.get("include_practice_trials", True))
        and epoching.get("data_source") == "raw"
    )
    if not epoching_ready:
        failures.append("strict DSART prestimulus epoch contract is not configured")
    marker_integrity = _task_marker_integrity(
        session_dir,
        epoch_cfg,
        require_markers=record_eeg,
        require_display_flip=task_mode == "psychopy",
    )
    failures.extend(marker_integrity["failures"])
    warnings.extend(marker_integrity["warnings"])
    countdown_integrity = _countdown_event_integrity(session_dir)
    failures.extend(countdown_integrity["failures"])
    warnings.extend(countdown_integrity["warnings"])
    return {
        "status": "pass" if not failures and not warnings else ("fail" if failures else "warning"),
        "failures": failures,
        "warnings": warnings,
        "planned_trials": stimulus_manifest.get("requested_trial_override") or stimulus_manifest.get("normal_recipe_trial_count"),
        "completed_trials": task_summary.get("experimental_trials"),
        "support_trials_completed": task_summary.get("support_trials"),
        "query_trials_completed": task_summary.get("query_trials"),
        "valid_go_rt_count": task_summary.get("valid_go_rt_count"),
        "no_go_count": task_summary.get("no_go_trial_count"),
        "commission_count": task_summary.get("commission_errors"),
        "omission_count": task_summary.get("omission_errors"),
        "premature_count": task_summary.get("premature_responses"),
        "multiple_response_count": task_summary.get("multiple_responses"),
        "mean_valid_go_rt_seconds": task_summary.get("mean_valid_go_rt_seconds"),
        "median_valid_go_rt_seconds": task_summary.get("median_valid_go_rt_seconds"),
        "support_reference_status": "complete" if task_summary.get("support_complete") else "incomplete",
        "raw_eeg_status": raw_metadata.get("status", "skipped" if not record_eeg else "missing"),
        "raw_sample_contract": raw_sample_contract,
        "raw_header": raw_integrity["raw_header"],
        "raw_integrity": raw_integrity,
        "marker_count": dynamic_report.get("marker_trial_parity", {}).get("stimulus_onset_event_count"),
        "marker_integrity": marker_integrity,
        "countdown_integrity": countdown_integrity,
        "epoching_ready": epoching_ready,
        "abort_status": task_summary.get("aborted"),
    }


def _countdown_event_integrity(session_dir: Path) -> dict[str, Any]:
    path = session_dir / "events" / "events.jsonl"
    countdown_rows: list[tuple[int, str, dict[str, Any]]] = []
    practice_onsets: list[tuple[int, dict[str, Any]]] = []
    experimental_onsets: list[tuple[int, dict[str, Any]]] = []
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                family = str(row.get("label", "")).split("__", 1)[0]
                if family in {
                    "dynamic_sart_countdown_start",
                    "dynamic_sart_countdown_step",
                    "dynamic_sart_countdown_end",
                }:
                    countdown_rows.append((line_number, family, row))
                elif family == "dynamic_sart_stimulus_onset":
                    target = practice_onsets if _is_practice_stimulus_event(row) else experimental_onsets
                    target.append((line_number, row))
    families = [family for _line, family, _row in countdown_rows]
    step_values = [
        str(row.get("value"))
        for _line, family, row in countdown_rows
        if family == "dynamic_sart_countdown_step"
    ]
    failures: list[str] = []
    warnings: list[str] = []
    if families.count("dynamic_sart_countdown_start") != 1:
        failures.append("countdown-start event count is not exactly one")
    if families.count("dynamic_sart_countdown_end") != 1:
        failures.append("countdown-end event count is not exactly one")
    if step_values != ["5", "4", "3", "2", "1", "GO!"]:
        failures.append(f"countdown steps are invalid: {step_values}")
    expected_families = [
        "dynamic_sart_countdown_start",
        *(["dynamic_sart_countdown_step"] * 6),
        "dynamic_sart_countdown_end",
    ]
    if families != expected_families:
        failures.append("countdown events are not in the required start/steps/end order")
    if not experimental_onsets:
        failures.append("first experimental stimulus-onset event is missing")

    countdown_timestamps = [_finite_event_timestamp(row) for _line, _family, row in countdown_rows]
    first_experimental = experimental_onsets[0] if experimental_onsets else None
    first_experimental_timestamp = (
        None if first_experimental is None else _finite_event_timestamp(first_experimental[1])
    )
    timestamps_available = (
        len(countdown_timestamps) == len(expected_families)
        and all(value is not None for value in countdown_timestamps)
        and first_experimental_timestamp is not None
    )
    if not timestamps_available:
        failures.append("countdown/first experimental onset timestamps are missing or non-finite")
    else:
        resolved_countdown_timestamps = [float(value) for value in countdown_timestamps if value is not None]
        if any(
            later < earlier
            for earlier, later in zip(
                resolved_countdown_timestamps,
                resolved_countdown_timestamps[1:],
            )
        ):
            failures.append("countdown timestamps are not nondecreasing")
        countdown_end_timestamp = resolved_countdown_timestamps[-1]
        if countdown_end_timestamp > float(first_experimental_timestamp):
            failures.append(
                "countdown ended after the first experimental stimulus onset by "
                f"{countdown_end_timestamp - float(first_experimental_timestamp):.6f} seconds"
            )

    event_order_valid: bool | None = None
    if families.count("dynamic_sart_countdown_end") == 1 and first_experimental is not None:
        countdown_end_line = next(
            line_number
            for line_number, family, _row in countdown_rows
            if family == "dynamic_sart_countdown_end"
        )
        event_order_valid = countdown_end_line < first_experimental[0]
        if not event_order_valid:
            failures.append(
                "countdown-end event is not recorded before the first experimental stimulus onset"
            )
    return {
        "status": "fail" if failures else ("warning" if warnings else "pass"),
        "events_file": str(path),
        "step_values": step_values,
        "practice_stimulus_onset_count": len(practice_onsets),
        "experimental_stimulus_onset_count": len(experimental_onsets),
        "first_experimental_stimulus_onset_timestamp": first_experimental_timestamp,
        "countdown_end_timestamp": (
            countdown_timestamps[-1] if len(countdown_timestamps) == len(expected_families) else None
        ),
        "event_order_valid": event_order_valid,
        "failures": failures,
        "warnings": warnings,
    }


def _is_practice_stimulus_event(row: dict[str, Any]) -> bool:
    """Recognize practice trials from redundant current and legacy ledger fields."""

    metadata = dict(row.get("metadata") or {})
    practice_value = metadata.get("practice", row.get("practice"))
    if practice_value is True or str(practice_value).strip().lower() in {"1", "true", "yes"}:
        return True
    if str(metadata.get("phase") or row.get("phase") or "").strip().lower() == "practice":
        return True
    try:
        if int(row.get("trial")) < 0:
            return True
    except (TypeError, ValueError):
        pass
    label_tokens = str(row.get("label") or "").split("__")[1:]
    return "practice=1" in label_tokens


def _finite_event_timestamp(row: dict[str, Any]) -> float | None:
    timestamp = _optional_float(row.get("timestamp"))
    return timestamp if timestamp is not None and math.isfinite(timestamp) else None


def _trial_stimulus_duration(row: dict[str, Any]) -> tuple[float | None, str]:
    onset_lsl = _optional_float(row.get("stimulus_onset_lsl"))
    offset_lsl = _optional_float(row.get("stimulus_offset_lsl"))
    if onset_lsl is not None and offset_lsl is not None:
        return offset_lsl - onset_lsl, "lsl_flip"
    return _optional_float(row.get("actual_stimulus_seconds")), "high_resolution_monotonic"


def _trial_soi_duration(row: dict[str, Any]) -> tuple[float | None, str]:
    lsl_duration = _optional_float(row.get("actual_trial_duration_lsl_seconds"))
    if lsl_duration is not None:
        return lsl_duration, "lsl_flip"
    return _optional_float(row.get("actual_trial_duration_seconds")), "high_resolution_monotonic"


def _task_marker_integrity(
    session_dir: Path,
    parameters: dict[str, Any],
    *,
    require_markers: bool,
    require_display_flip: bool = True,
) -> dict[str, Any]:
    expected_source_id = parameters.get("hardware", {}).get("markers", {}).get("source_id")
    rows = []
    offset_rows = []
    display_marker_sequence: list[str] = []
    display_marker_rows: list[dict[str, Any]] = []
    path = session_dir / "events" / "events.jsonl"
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                family = str(row.get("label", "")).split("__", 1)[0]
                if family == "dynamic_sart_stimulus_onset":
                    rows.append(row)
                    display_marker_sequence.append(family)
                    display_marker_rows.append(row)
                elif family == "dynamic_sart_stimulus_offset":
                    offset_rows.append(row)
                    display_marker_sequence.append(family)
                    display_marker_rows.append(row)
    trial_records: list[dict[str, Any]] = []
    trials_path = session_dir / "events" / "dynamic_sart_trials.jsonl"
    if trials_path.exists():
        with trials_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    trial_records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    trial_rows = len(trial_records)
    missing_lsl = []
    missing_source = []
    observed_sources = set()
    onset_lsl_timestamps: list[float] = []
    onset_monotonic_timestamps: list[float] = []
    unscheduled_flip_trials = []
    for row in rows:
        trial = row.get("trial")
        metadata = dict(row.get("metadata") or {})
        lsl_timestamp = _optional_float(metadata.get("lsl_timestamp"))
        monotonic_timestamp = _optional_float(row.get("timestamp"))
        if lsl_timestamp is None:
            missing_lsl.append(trial)
        else:
            onset_lsl_timestamps.append(lsl_timestamp)
        if monotonic_timestamp is not None:
            onset_monotonic_timestamps.append(monotonic_timestamp)
        if metadata.get("scheduled_on_flip") is not True:
            unscheduled_flip_trials.append(trial)
        source_id = metadata.get("marker_stream_source_id")
        if not source_id:
            missing_source.append(trial)
        else:
            observed_sources.add(str(source_id))
    failures = []
    warnings = []
    target = failures if require_markers else warnings
    if not rows:
        target.append("no DSART stimulus-onset marker ledger rows were found")
    if len(rows) != trial_rows:
        target.append(f"stimulus marker/trial row parity failed: markers={len(rows)}, trials={trial_rows}")
    if len(offset_rows) != trial_rows:
        target.append(f"stimulus-offset marker/trial row parity failed: markers={len(offset_rows)}, trials={trial_rows}")
    expected_display_sequence = [
        family
        for _ in range(trial_rows)
        for family in ("dynamic_sart_stimulus_onset", "dynamic_sart_stimulus_offset")
    ]
    if display_marker_sequence != expected_display_sequence:
        target.append("stimulus onset/offset marker order is incomplete, duplicated, or overlapping")
    if missing_lsl:
        target.append(f"{len(missing_lsl)} stimulus markers lack an LSL timestamp")
    if missing_source:
        target.append(f"{len(missing_source)} stimulus markers lack the run-specific marker source ID")
    if require_display_flip and unscheduled_flip_trials:
        target.append(f"{len(unscheduled_flip_trials)} stimulus-onset markers were not captured on a display flip")
    invalid_offset_rows = []
    for row in offset_rows:
        metadata = dict(row.get("metadata") or {})
        if (
            _optional_float(metadata.get("lsl_timestamp")) is None
            or not metadata.get("marker_stream_source_id")
            or (require_display_flip and metadata.get("scheduled_on_flip") is not True)
            or (
                expected_source_id
                and str(metadata.get("marker_stream_source_id")) != str(expected_source_id)
            )
        ):
            invalid_offset_rows.append(row.get("trial"))
    if invalid_offset_rows:
        requirement = "flip/LSL/source" if require_display_flip else "LSL/source"
        target.append(
            f"{len(invalid_offset_rows)} stimulus-offset markers lack required {requirement} metadata"
        )
    if expected_source_id and observed_sources and observed_sources != {str(expected_source_id)}:
        target.append("stimulus marker source ID does not match the persisted run-specific source ID")
    if any(second <= first for first, second in zip(onset_lsl_timestamps, onset_lsl_timestamps[1:])):
        target.append("stimulus-onset LSL timestamps are not strictly increasing")
    if any(second <= first for first, second in zip(onset_monotonic_timestamps, onset_monotonic_timestamps[1:])):
        target.append("stimulus-onset monotonic timestamps are not strictly increasing")
    trial_indices = [row.get("global_trial_index") for row in trial_records]
    if len(set(trial_indices)) != len(trial_indices):
        target.append("DSART trial ledger contains duplicate global trial indices")
    invalid_trial_timing = []
    overlapping_trials = []
    stimulus_duration_warning_trials = []
    soi_warning_trials = []
    timing_measurement_timebases: set[str] = set()
    display_manifest = _load_json(session_dir / "events" / "stimulus_manifest.json") or {}
    display_timing = dict(display_manifest.get("display_timing") or {})
    frame_seconds = float(display_timing.get("expected_frame_interval_ms") or 0.0) / 1000.0
    timing_warning_threshold = frame_seconds * 1.5 if frame_seconds > 0.0 else None
    previous_response_close: float | None = None
    for trial_row in trial_records:
        trial_index = trial_row.get("global_trial_index")
        onset = _optional_float(trial_row.get("stimulus_onset_monotonic"))
        offset = _optional_float(trial_row.get("stimulus_offset_monotonic"))
        response_close = _optional_float(trial_row.get("response_window_close_monotonic"))
        if onset is None or offset is None or response_close is None or not (onset <= offset <= response_close):
            invalid_trial_timing.append(trial_index)
            continue
        if previous_response_close is not None and onset < previous_response_close:
            overlapping_trials.append(trial_index)
        previous_response_close = response_close
        if timing_warning_threshold is not None:
            actual_stimulus, stimulus_timebase = _trial_stimulus_duration(trial_row)
            if actual_stimulus is not None:
                timing_measurement_timebases.add(stimulus_timebase)
            planned_stimulus = _optional_float(trial_row.get("planned_stimulus_seconds"))
            if (
                actual_stimulus is not None
                and planned_stimulus is not None
                and abs(actual_stimulus - planned_stimulus) > timing_warning_threshold
            ):
                stimulus_duration_warning_trials.append(trial_index)
            actual_soi, soi_timebase = _trial_soi_duration(trial_row)
            if actual_soi is not None:
                timing_measurement_timebases.add(soi_timebase)
            planned_soi = _optional_float(trial_row.get("planned_soi_seconds"))
            if (
                actual_soi is not None
                and planned_soi is not None
                and abs(actual_soi - planned_soi) > timing_warning_threshold
            ):
                soi_warning_trials.append(trial_index)
    if invalid_trial_timing:
        target.append(f"{len(invalid_trial_timing)} trial rows have invalid onset/offset/response-close ordering")
    if overlapping_trials:
        target.append(f"{len(overlapping_trials)} trial response windows overlap the next stimulus")
    if stimulus_duration_warning_trials:
        warnings.append(
            f"{len(stimulus_duration_warning_trials)} trial(s) missed the 250 ms digit duration by more than 1.5 frames"
        )
    if soi_warning_trials:
        warnings.append(
            f"{len(soi_warning_trials)} trial(s) missed the 1.600 s SOI by more than 1.5 frames"
        )
    raw_metadata = _load_json(session_dir / "raw" / "eeg_metadata.json") or {}
    raw_first = _optional_float(raw_metadata.get("first_local_received_time"))
    raw_last = _optional_float(raw_metadata.get("last_local_received_time"))
    recorder_backend = str(
        parameters.get("processes", {}).get("recorder", {}).get("backend", "lsl_csv")
    )
    csv_marker_overlap_status = "not_checked"
    display_monotonic_timestamps = [
        value
        for value in (_optional_float(row.get("timestamp")) for row in display_marker_rows)
        if value is not None
    ]
    if require_markers and display_monotonic_timestamps:
        if recorder_backend == "labrecorder_xdf":
            csv_marker_overlap_status = "not_applicable_authoritative_xdf"
        elif raw_first is None or raw_last is None:
            csv_marker_overlap_status = "fail_missing_csv_receipt_span"
            failures.append(
                "CSV mirror lacks the required PC-local receipt-time span for marker overlap"
            )
        elif (
            raw_first > min(display_monotonic_timestamps) + 0.5
            or raw_last < max(display_monotonic_timestamps) - 0.5
        ):
            csv_marker_overlap_status = "fail_incomplete_csv_receipt_span"
            failures.append(
                "CSV mirror PC-local receipt-time span does not cover every stimulus marker"
            )
        else:
            csv_marker_overlap_status = "pass"
    marker_receipt = _marker_receipt_integrity(session_dir, display_marker_rows, required=require_markers)
    failures.extend(marker_receipt["failures"])
    warnings.extend(marker_receipt["warnings"])
    return {
        "status": "fail" if failures else ("warning" if warnings else "pass"),
        "events_file": str(path),
        "stimulus_onset_count": len(rows),
        "stimulus_offset_count": len(offset_rows),
        "trial_row_count": trial_rows,
        "marker_trial_parity": len(rows) == trial_rows,
        "expected_source_id": expected_source_id,
        "observed_source_ids": sorted(observed_sources),
        "missing_lsl_timestamp_trials": missing_lsl,
        "missing_source_id_trials": missing_source,
        "unscheduled_flip_trials": unscheduled_flip_trials,
        "invalid_stimulus_offset_trials": invalid_offset_rows,
        "invalid_trial_timing_trials": invalid_trial_timing,
        "overlapping_trial_indices": overlapping_trials,
        "stimulus_duration_warning_trials": stimulus_duration_warning_trials,
        "soi_warning_trials": soi_warning_trials,
        "timing_measurement_timebases": sorted(timing_measurement_timebases),
        "timing_warning_threshold_seconds": timing_warning_threshold,
        "onset_lsl_timestamps_strictly_increasing": not any(
            second <= first for first, second in zip(onset_lsl_timestamps, onset_lsl_timestamps[1:])
        ),
        "raw_eeg_local_receipt_monotonic_span": {"first": raw_first, "last": raw_last},
        "raw_eeg_corrected_lsl_timestamp_span": {
            "first": _optional_float(raw_metadata.get("first_lsl_timestamp")),
            "last": _optional_float(raw_metadata.get("last_lsl_timestamp")),
        },
        "csv_marker_overlap_status": csv_marker_overlap_status,
        "independent_marker_receipt": marker_receipt,
        "failures": failures,
        "warnings": warnings,
        "display_flip_required": require_display_flip,
        "transport_loopback_scope": (
            "preflight verifies receipt; task ledger verifies emitted label, display-flip timestamp, and source identity"
            if require_display_flip
            else "preflight verifies receipt; dry-run task ledger verifies emitted label, LSL timestamp, and source identity"
        ),
    }


def _marker_receipt_integrity(
    session_dir: Path,
    ledger_rows: list[dict[str, Any]],
    *,
    required: bool,
) -> dict[str, Any]:
    csv_path = session_dir / "raw" / "lsl_markers_received.csv"
    metadata_path = session_dir / "raw" / "lsl_markers_received_metadata.json"
    metadata = _load_json(metadata_path) or {}
    parameters = _load_json(session_dir / "parameters.json") or {}
    delivery_warning_seconds = max(
        0.0,
        float(
            parameters.get("recording_suite", {}).get(
                "marker_delivery_latency_warning_seconds",
                0.25,
            )
        ),
    )
    expected = []
    for row in ledger_rows:
        marker_metadata = dict(row.get("metadata") or {})
        expected.append(
            {
                "label": str(row.get("label") or ""),
                "lsl_timestamp": _optional_float(marker_metadata.get("lsl_timestamp")),
                "timestamp_advance_seconds": max(
                    0.0,
                    float(marker_metadata.get("fixed_display_latency_ms") or 0.0) / 1000.0,
                ),
            }
        )
    expected_labels = [row["label"] for row in expected]
    expected_label_set = set(expected_labels)
    received = []
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                label = str(row.get("marker_label") or "")
                if label in expected_label_set:
                    received.append(
                        {
                            "label": label,
                            "lsl_timestamp": _optional_float(row.get("lsl_timestamp")),
                            "local_received_lsl_timestamp": _optional_float(
                                row.get("local_received_lsl_timestamp")
                            ),
                        }
                    )
    failures = []
    warnings = []
    target = failures if required else warnings
    if required and metadata.get("status") != "stopped":
        warnings.append(
            "independent marker receipt recorder did not report a clean stop; exact received "
            "marker count/order/timestamps are validated separately"
        )
    if not csv_path.exists():
        target.append("independent LSL marker receipt CSV is missing")
    elif [row["label"] for row in received] != expected_labels:
        target.append("independently received marker order/count does not match the task marker ledger")
    timestamp_mismatches = []
    if len(received) == len(expected):
        for index, (emitted, observed) in enumerate(zip(expected, received), start=1):
            emitted_timestamp = emitted["lsl_timestamp"]
            observed_timestamp = observed["lsl_timestamp"]
            if (
                emitted_timestamp is None
                or observed_timestamp is None
                or abs(observed_timestamp - emitted_timestamp) > 0.000001
            ):
                timestamp_mismatches.append(index)
    if timestamp_mismatches:
        target.append(
            f"{len(timestamp_mismatches)} independently received markers do not preserve their emitted LSL timestamp"
        )
    delivery_latencies = []
    missing_receipt_times = []
    negative_latency_indices = []
    late_delivery_indices = []
    timestamp_deltas = []
    for index, (emitted, observed) in enumerate(zip(expected, received), start=1):
        emitted_timestamp = observed["lsl_timestamp"]
        received_timestamp = observed["local_received_lsl_timestamp"]
        if emitted_timestamp is None or received_timestamp is None:
            missing_receipt_times.append(index)
            continue
        timestamp_delta = received_timestamp - emitted_timestamp
        timestamp_deltas.append(timestamp_delta)
        latency = timestamp_delta + float(emitted["timestamp_advance_seconds"])
        delivery_latencies.append(latency)
        if latency < -0.001:
            negative_latency_indices.append(index)
        if latency > delivery_warning_seconds:
            late_delivery_indices.append(index)
    if missing_receipt_times:
        warnings.append(
            f"{len(missing_receipt_times)} independently received markers lack delivery-time evidence"
        )
    if negative_latency_indices:
        warnings.append(
            f"{len(negative_latency_indices)} marker receipt times remain earlier than their modeled "
            "emission timestamp after accounting for configured display latency"
        )
    if late_delivery_indices:
        warnings.append(
            f"{len(late_delivery_indices)} markers took more than "
            f"{delivery_warning_seconds:.3f} seconds to reach the independent local receipt inlet"
        )
    return {
        "status": "fail" if failures else ("warning" if warnings else "pass"),
        "required": required,
        "csv_file": str(csv_path),
        "metadata_file": str(metadata_path),
        "metadata": metadata,
        "expected_count": len(expected),
        "received_count": len(received),
        "timestamp_mismatch_indices": timestamp_mismatches,
        "receipt_minus_marker_timestamp_seconds": timestamp_deltas,
        "delivery_latency_seconds": delivery_latencies,
        "maximum_delivery_latency_seconds": max(delivery_latencies, default=None),
        "delivery_latency_warning_seconds": delivery_warning_seconds,
        "missing_delivery_time_indices": missing_receipt_times,
        "negative_delivery_latency_indices": negative_latency_indices,
        "late_delivery_indices": late_delivery_indices,
        "failures": failures,
        "warnings": warnings,
    }


def _raw_eeg_integrity(session_dir: Path, *, required: bool) -> dict[str, Any]:
    raw_path = session_dir / "raw" / "eeg.csv"
    metadata_path = session_dir / "raw" / "eeg_metadata.json"
    metadata = _load_json(metadata_path) or {}
    contract = dict(metadata.get("raw_sample_contract", {}) or {})
    stream = dict(metadata.get("stream", {}) or {})
    parameters = _load_json(session_dir / "parameters.json") or {}
    eeg_config = dict(parameters.get("hardware", {}).get("eeg", {}) or {})
    recorder_config = dict(parameters.get("processes", {}).get("recorder", {}) or {})
    recorder_backend = str(recorder_config.get("backend", "lsl_csv"))
    csv_mirror_enabled = bool(recorder_config.get("csv_mirror", False))
    csv_validation_enabled = recorder_backend != "labrecorder_xdf" or csv_mirror_enabled
    expected_channels: list[str] = []
    try:
        if eeg_config.get("profile"):
            expected_channels = list(expected_profile(str(eeg_config["profile"]), eeg_config.get("family")).channel_names)
    except KeyError:
        expected_channels = []
    header: list[str] = []
    if raw_path.exists():
        with raw_path.open("r", encoding="utf-8", newline="") as handle:
            header = next(csv.reader(handle), [])
    failures = []
    warnings = []

    def record_csv_issue(message: str) -> None:
        if recorder_backend == "labrecorder_xdf":
            warnings.append(f"CSV mirror warning: {message}")
        else:
            failures.append(message)

    if required and csv_validation_enabled:
        if not raw_path.exists():
            record_csv_issue("raw EEG CSV is missing")
        if metadata.get("status") != "stopped":
            record_csv_issue("raw EEG recorder status is not stopped")
        if int(metadata.get("sample_count") or 0) <= 0:
            record_csv_issue("raw EEG recorder did not retain any samples")
        if int(metadata.get("timestamp_gap_count") or 0) > 0:
            record_csv_issue("raw EEG contains one or more timestamp gaps above the configured acquisition limit")
        if int(metadata.get("nonmonotonic_timestamp_count") or 0) > 0:
            record_csv_issue("raw EEG contains nonmonotonic source timestamps")
        if contract.get("amplitude_samples_modified") is not False:
            record_csv_issue("raw EEG metadata does not prove amplitude pass-through")
        if list(contract.get("amplitude_transformations") or []) != []:
            record_csv_issue("raw EEG metadata reports an amplitude transformation")
        if contract.get("channel_value_order_modified") is not False:
            record_csv_issue("raw EEG metadata does not prove channel-order pass-through")
        for operation in ("filtering", "resampling", "rereferencing", "artifact_rejection"):
            if contract.get(operation) != "none":
                record_csv_issue(f"raw EEG metadata reports {operation} during acquisition")
        if contract.get("recording_timestamp_mode") != "source_preserving":
            record_csv_issue("raw EEG metadata does not prove source-preserving timestamps")
        if contract.get("source_timestamp_retained") is not True:
            record_csv_issue("raw EEG metadata does not prove original source timestamps were retained")
        if contract.get("initial_time_correction_available") is not True:
            record_csv_issue("raw EEG recorder did not obtain an LSL correction for marker alignment")
        if header[:4] != [
            "lsl_timestamp",
            "local_received_time",
            "source_lsl_timestamp",
            "lsl_time_correction_seconds",
        ]:
            record_csv_issue("raw EEG CSV does not retain corrected and original LSL timestamps")
        recorded_channels = header[4:]
        if expected_channels and recorded_channels != expected_channels:
            record_csv_issue("raw EEG CSV channel columns do not match the configured physical device order")
        if list(stream.get("channel_names") or []) != recorded_channels:
            record_csv_issue("raw EEG metadata channel order does not match the CSV header")
        if stream.get("channel_value_order_changed") is not False:
            record_csv_issue("raw EEG stream metadata does not prove that channel values stayed in source order")
        if list(stream.get("amplitude_transformations") or []) != []:
            record_csv_issue("raw EEG stream metadata reports an amplitude transformation")
        if list(stream.get("lsl_processing") or []) != []:
            record_csv_issue("raw EEG inlet applied LSL processing despite the source-preserving contract")
    result = {
        "status": "fail" if failures else ("warning" if warnings else "pass"),
        "required": required,
        "raw_file": str(raw_path),
        "metadata_file": str(metadata_path),
        "metadata": metadata,
        "raw_sample_contract": contract,
        "raw_header": header,
        "expected_channel_order": expected_channels,
        "recorded_channel_order": header[4:],
        "csv_mirror_enabled": csv_mirror_enabled,
        "csv_validation_status": "enabled" if csv_validation_enabled else "not_configured",
        "failures": failures,
        "warnings": warnings,
    }
    result["primary_format"] = "xdf" if recorder_backend == "labrecorder_xdf" else "csv"
    result["primary_file"] = (
        str(session_dir / "raw" / "recording.xdf")
        if recorder_backend == "labrecorder_xdf"
        else str(raw_path)
    )
    if recorder_backend == "labrecorder_xdf":
        xdf = validate_xdf_recording(session_dir, required=required)
        result["xdf_integrity"] = xdf
        failures.extend(list(xdf.get("failures") or []))
        warnings.extend(list(xdf.get("warnings") or []))
        if failures:
            result["status"] = "fail"
        elif warnings:
            result["status"] = "warning"
        elif xdf.get("status") == "skipped":
            result["status"] = "skipped"
            result["skipped"] = True
            result["skip_reason"] = xdf.get("skip_reason")
        else:
            result["status"] = "pass"
    return result


def _baseline_recording_validation(
    paths: SessionPaths,
    result: dict[str, Any],
    *,
    record_eeg: bool,
) -> dict[str, Any]:
    failures = []
    warnings = []
    process_summary = dict(result.get("processes") or {})
    if process_summary and process_summary.get("status") != "complete":
        notes = list(process_summary.get("notes") or [])
        detail = f": {notes[0]}" if notes else ""
        failures.append(
            f"baseline managed-process lifecycle did not complete (status={process_summary.get('status')}){detail}"
        )
    phases = list(result.get("phases") or [])
    expected_phases = ["eyes_open", "eyes_closed"]
    if [row.get("phase") for row in phases] != expected_phases:
        failures.append("baseline phases are not exactly eyes_open then eyes_closed")
    for phase in phases:
        if phase.get("completion_status") != "completed" or _baseline_phase_aborted(phase):
            failures.append(f"baseline phase {phase.get('phase')} did not complete")
        if record_eeg and (
            _optional_float(phase.get("start_lsl_timestamp")) is None
            or _optional_float(phase.get("end_lsl_timestamp")) is None
        ):
            failures.append(f"baseline phase {phase.get('phase')} lacks LSL boundary timestamps")
    parameters = _load_json(paths.parameters) or {}
    expected_source_id = parameters.get("hardware", {}).get("markers", {}).get("source_id")
    expected_labels = [
        "dsart_baseline_eyes_open_start",
        "dsart_baseline_eyes_open_end",
        "dsart_baseline_eyes_closed_start",
        "dsart_baseline_eyes_closed_end",
    ]
    marker_rows = []
    if paths.events_jsonl.exists():
        with paths.events_jsonl.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("label") in set(expected_labels):
                    marker_rows.append(row)
    observed_labels = [str(row.get("label")) for row in marker_rows]
    target = failures if record_eeg else warnings
    if observed_labels != expected_labels:
        target.append("baseline boundary marker ledger is incomplete, duplicated, or out of order")
    boundary_lsl_timestamps = []
    unscheduled_start_markers = []
    for row in marker_rows:
        metadata = dict(row.get("metadata") or {})
        lsl_timestamp = _optional_float(metadata.get("lsl_timestamp"))
        if lsl_timestamp is None:
            target.append(f"baseline marker {row.get('label')} lacks an LSL timestamp")
        else:
            boundary_lsl_timestamps.append(lsl_timestamp)
        if expected_source_id and metadata.get("marker_stream_source_id") != expected_source_id:
            target.append(f"baseline marker {row.get('label')} has the wrong source ID")
        if str(row.get("label") or "").endswith("_start") and metadata.get("scheduled_on_flip") is not True:
            unscheduled_start_markers.append(str(row.get("label") or ""))
    if unscheduled_start_markers:
        target.append("baseline start markers were not captured on their actual display flip")
    if any(
        second <= first
        for first, second in zip(boundary_lsl_timestamps, boundary_lsl_timestamps[1:])
    ):
        target.append("baseline boundary LSL timestamps are not strictly increasing")
    duration_alignment = []
    for phase in phases:
        start_monotonic = _optional_float(phase.get("start_monotonic_timestamp"))
        end_monotonic = _optional_float(phase.get("end_monotonic_timestamp"))
        start_lsl = _optional_float(phase.get("start_lsl_timestamp"))
        end_lsl = _optional_float(phase.get("end_lsl_timestamp"))
        if None in {start_monotonic, end_monotonic, start_lsl, end_lsl}:
            continue
        monotonic_duration = float(end_monotonic) - float(start_monotonic)
        lsl_duration = float(end_lsl) - float(start_lsl)
        difference = lsl_duration - monotonic_duration
        duration_alignment.append(
            {
                "phase": phase.get("phase"),
                "monotonic_duration_seconds": monotonic_duration,
                "lsl_duration_seconds": lsl_duration,
                "difference_seconds": difference,
            }
        )
        if monotonic_duration <= 0 or lsl_duration <= 0 or abs(difference) > 0.05:
            target.append(
                f"baseline phase {phase.get('phase')} has misaligned monotonic/LSL boundary timing"
            )
    marker_receipt = _marker_receipt_integrity(paths.root, marker_rows, required=record_eeg)
    failures.extend(marker_receipt["failures"])
    warnings.extend(marker_receipt["warnings"])
    raw = _raw_eeg_integrity(paths.root, required=record_eeg)
    failures.extend(raw["failures"])
    warnings.extend(raw["warnings"])
    csv_receipt_span_status = "not_required"
    if record_eeg and phases and raw.get("csv_validation_status") == "enabled":
        csv_receipt_span_status = "checked"
        raw_metadata = dict(raw.get("metadata") or {})
        raw_first = _optional_float(raw_metadata.get("first_local_received_time"))
        raw_last = _optional_float(raw_metadata.get("last_local_received_time"))
        overlap_target = warnings if raw.get("primary_format") == "xdf" else failures
        phase_starts = [
            value
            for value in (_optional_float(row.get("start_monotonic_timestamp")) for row in phases)
            if value is not None
        ]
        phase_ends = [
            value
            for value in (_optional_float(row.get("end_monotonic_timestamp")) for row in phases)
            if value is not None
        ]
        if raw_first is None or raw_last is None:
            overlap_target.append(
                "CSV mirror lacks the PC-local receipt-time span; XDF baseline overlap is "
                "validated from the authoritative XDF"
            )
        elif phase_starts and phase_ends and (
            raw_first > min(phase_starts) + 0.5
            or raw_last < max(phase_ends) - 0.5
        ):
            overlap_target.append(
                "CSV mirror PC-local receipt-time span does not cover the complete resting baseline; "
                "authoritative XDF coverage is validated separately"
            )
    elif record_eeg and raw.get("primary_format") == "xdf":
        csv_receipt_span_status = "not_applicable_authoritative_xdf"
    return {
        "status": "fail" if failures else ("warning" if warnings else "pass"),
        "failures": failures,
        "warnings": warnings,
        "expected_marker_source_id": expected_source_id,
        "marker_labels": observed_labels,
        "start_markers_scheduled_on_flip": not unscheduled_start_markers,
        "boundary_lsl_timestamps_strictly_increasing": not any(
            second <= first
            for first, second in zip(boundary_lsl_timestamps, boundary_lsl_timestamps[1:])
        ),
        "boundary_duration_alignment": duration_alignment,
        "independent_marker_receipt": marker_receipt,
        "raw_integrity": raw,
        "csv_receipt_span_status": csv_receipt_span_status,
    }


def _electrode_report(
    channel_names: Iterable[str],
    probe: dict[str, Any],
    *,
    recipe: str,
    quality_file: str | Path | None,
    operator_note: str | None,
    operator_confirmed: bool,
) -> dict[str, Any]:
    external = _load_json(Path(quality_file).expanduser().resolve()) if quality_file else {}
    external_channels = dict((external or {}).get("channels", external or {}) or {})
    signal_rows = {
        str(row.get("channel_name")): row for row in probe.get("quality", {}).get("channels", [])
    }
    channels = []
    for name in channel_names:
        supplied = external_channels.get(name)
        supplied = dict(supplied) if isinstance(supplied, dict) else ({"value": supplied} if supplied is not None else {})
        signal = signal_rows.get(name, {})
        status = str(supplied.get("status") or signal.get("status") or "unavailable")
        channels.append(
            {
                "channel_name": name,
                "quality_or_impedance_value": supplied.get("value", supplied.get("impedance_kohm")),
                "quality_status": status,
                "operator_note": supplied.get("note", operator_note),
                "timestamp": _now(),
                "finite_sample_fraction": signal.get("finite_sample_fraction"),
                "signal_units": probe.get("quality", {}).get("signal_units", "native_lsl_units"),
                "standard_deviation_native_units": signal.get("standard_deviation_native_units"),
                "peak_to_peak_native_units": signal.get("peak_to_peak_native_units"),
                "maximum_absolute_native_units": signal.get("maximum_absolute_native_units"),
                "flatline": signal.get("flatline"),
                "contact_quality_source": "operator_file" if supplied else "signal_probe" if signal else "unavailable",
            }
        )
    return {
        "schema": "eegle.dsart_electrode_quality.v1",
        "recipe": recipe,
        "created_at": _now(),
        "operator_confirmed": operator_confirmed,
        "operator_note": operator_note,
        "measurement_type": "impedance_or_contact_quality" if recipe == "dsart32" else "contact_or_signal_quality",
        "channels": channels,
    }


def _marker_preflight_check(
    config: dict[str, Any],
    participant_id: str,
    visit_id: str,
    phase: str,
    *,
    enabled: bool,
    xdf_probe: CheckResult | None,
) -> CheckResult:
    if enabled and _xdf_probe_covers_marker_loopback(xdf_probe):
        validation = dict((xdf_probe.data if xdf_probe is not None else {}).get("validation") or {})
        markers = dict(validation.get("markers") or {})
        coverage = dict(validation.get("recording_coverage") or {})
        return CheckResult(
            "marker_loopback",
            "skip",
            "standalone marker loopback omitted because the successful XDF probe already verified "
            "independent receipt, exact marker order, synchronized timestamps, and EEG/marker coverage",
            {
                "skipped": True,
                "covered_by": "xdf_recording_probe",
                "marker_sample_count": int(markers.get("sample_count") or 0),
                "recording_coverage_status": coverage.get("status"),
            },
        )
    return _standalone_marker_loopback_check(
        config,
        participant_id,
        visit_id,
        phase,
        enabled=enabled,
    )


def _xdf_probe_covers_marker_loopback(xdf_probe: CheckResult | None) -> bool:
    if xdf_probe is None or xdf_probe.status not in {"ok", "warn"}:
        return False
    validation = dict(xdf_probe.data.get("validation") or {})
    markers = dict(validation.get("markers") or {})
    coverage = dict(validation.get("recording_coverage") or {})
    timestamps = (
        _optional_float(markers.get("synchronized_first_timestamp")),
        _optional_float(markers.get("synchronized_last_timestamp")),
    )
    try:
        marker_sample_count = int(markers.get("sample_count") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        not validation.get("failures")
        and markers.get("sequence_matches_receipt") is True
        and marker_sample_count >= 2
        and all(value is not None and math.isfinite(value) for value in timestamps)
        and coverage.get("status") in {"pass", "warning"}
    )


def _standalone_marker_loopback_check(
    config: dict[str, Any],
    participant_id: str,
    visit_id: str,
    phase: str,
    *,
    enabled: bool,
) -> CheckResult:
    if not enabled:
        return CheckResult("marker_loopback", "skip", "marker loopback skipped for software-only run", {"skipped": True})
    marker_cfg = dict(config.get("hardware", {}).get("markers", {}) or {})
    source_id = f"eegle-preflight-{_safe_token(participant_id)}-{_safe_token(visit_id)}-{phase}"
    label = f"dsart_preflight_test__phase={phase}"
    outlet: LslMarkerOutlet | None = None
    inlet: Any | None = None
    try:
        import pylsl

        outlet = LslMarkerOutlet(
            str(marker_cfg.get("lsl_stream_name", "EEGleMarkers")),
            str(marker_cfg.get("lsl_stream_type", "Markers")),
            source_id,
        )
        infos = pylsl.resolve_byprop("source_id", source_id, minimum=1, timeout=2.0)
        if not infos:
            return CheckResult("marker_loopback", "fail", "run-specific marker stream was not discoverable", {"source_id": source_id})
        inlet = pylsl.StreamInlet(infos[0], max_buflen=5, recover=False)
        inlet.open_stream(timeout=1.0)
        timestamp = pylsl.local_clock()
        outlet.push(label, timestamp=timestamp)
        sample, received_timestamp = inlet.pull_sample(timeout=1.0)
        visible = bool(sample and sample[0] == label and received_timestamp)
        return CheckResult(
            "marker_loopback",
            "ok" if visible else "fail",
            "test marker observed through run-specific LSL stream" if visible else "test marker was not observed",
            {
                "label": label,
                "source_id": source_id,
                "emitted_timestamp": timestamp,
                "received_timestamp": received_timestamp,
                "timestamp_finite": bool(received_timestamp),
            },
        )
    except Exception as exc:
        return CheckResult("marker_loopback", "fail", f"marker loopback failed: {type(exc).__name__}: {exc}", {"source_id": source_id})
    finally:
        if inlet is not None:
            try:
                inlet.close_stream()
            except Exception:
                pass
        _close_resources(("preflight marker outlet", outlet))


def _storage_check(output_root: Path, *, record_eeg: bool = True) -> CheckResult:
    try:
        probe = probe_recording_storage(output_root)
        if probe.get("status") != "ok":
            return CheckResult(
                "recording_storage",
                "fail",
                "recording storage transition probe failed: " + "; ".join(probe.get("failures") or []),
                probe,
            )
        usage = shutil.disk_usage(output_root)
        if record_eeg:
            fail_below = 5 * 1024 * 1024 * 1024
            warn_below = 10 * 1024 * 1024 * 1024
        else:
            fail_below = 128 * 1024 * 1024
            warn_below = 512 * 1024 * 1024
        status = "fail" if usage.free < fail_below else ("warn" if usage.free < warn_below else "ok")
        detail = f"output writable; {usage.free / (1024 ** 3):.1f} GiB free"
        return CheckResult(
            "recording_storage",
            status,
            detail,
            {
                "output_root": str(output_root),
                "free_bytes": usage.free,
                "fail_below_bytes": fail_below,
                "warn_below_bytes": warn_below,
                "record_eeg": record_eeg,
                "transition_probe": probe,
            },
        )
    except Exception as exc:
        return CheckResult("recording_storage", "fail", f"output is not writable: {type(exc).__name__}: {exc}", {"output_root": str(output_root)})


def _runtime_cache_root(config: dict[str, Any], output_root: Path) -> Path:
    """Keep relative third-party caches on the same approved root as the visit."""

    configured = config.get("runtime", {}).get("runtime_cache_dir", ".runtime")
    candidate = Path(os.path.expandvars(str(configured))).expanduser()
    if not candidate.is_absolute():
        candidate = output_root / candidate
    return candidate.resolve()


def _probe_session_root_writable(output_root: Path) -> None:
    """Fail before acquisition unless parent and task-worker writes both work."""

    probe = probe_recording_storage(output_root)
    if probe.get("status") == "ok":
        return
    failures = "; ".join(str(value) for value in probe.get("failures") or [])
    child = dict(probe.get("child_process") or {})
    child_hint = ""
    if child.get("status") == "fail":
        child_hint = (
            " The parent process reached the root but the fresh task-worker process did not; "
            "on BeyondTrust, ask IT to review the PowerShell/Python rule and its child-process policy."
        )
    raise PermissionError(
        _session_root_error_message(output_root, failures or "unknown storage-policy failure")
        + child_hint
    )


def _session_root_error_message(output_root: Path, detail: object) -> str:
    return (
        f"DSART session root is not writable by the EEGle parent/task-worker processes: {output_root} "
        f"({detail}). Choose one approved data location with --session-root and validate it before recording; "
        "do not redirect an active visit or automatically fall back between roots."
    )


def _make_marker_outlet(config: dict[str, Any], paths: SessionPaths) -> LslMarkerOutlet | NullMarkerOutlet:
    marker_cfg = dict(config.get("hardware", {}).get("markers", {}) or {})
    try:
        return LslMarkerOutlet(
            str(marker_cfg.get("lsl_stream_name", "EEGleMarkers")),
            str(marker_cfg.get("lsl_stream_type", "Markers")),
            str(marker_cfg.get("source_id") or session_marker_source_id(paths.root)),
        )
    except Exception as exc:
        if bool(marker_cfg.get("required_for_realtime", False)):
            raise RuntimeError(f"required baseline marker outlet failed: {type(exc).__name__}: {exc}") from exc
        return NullMarkerOutlet(str(exc))


def _prestart_baseline_marker_outlet(
    config: dict[str, Any],
    paths: SessionPaths,
    *,
    recorder_enabled: bool,
) -> LslMarkerOutlet | None:
    recorder_cfg = dict(config.get("processes", {}).get("recorder", {}) or {})
    if not recorder_enabled or str(recorder_cfg.get("backend")) != "labrecorder_xdf":
        return None
    outlet = _make_marker_outlet(config, paths)
    if not isinstance(outlet, LslMarkerOutlet):
        raise RuntimeError(
            "managed XDF baseline recording requires the run-specific LSL marker outlet "
            "before LabRecorder starts"
        )
    return outlet


def _start_marker_receipt_recorder(
    outlet: LslMarkerOutlet | NullMarkerOutlet,
    paths: SessionPaths,
) -> LslMarkerReceiptRecorder:
    if not isinstance(outlet, LslMarkerOutlet):
        raise RuntimeError("independent marker receipt requires a live LSL marker outlet")
    recorder = LslMarkerReceiptRecorder(
        outlet.source_id,
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
    outlet: LslMarkerOutlet | NullMarkerOutlet,
    recorder: LslMarkerReceiptRecorder,
) -> str | None:
    """Hold cleanup until the independent inlet has received every emitted marker."""

    expected_count = getattr(outlet, "pushed_count", None)
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


def _baseline_instruction(
    win: Any,
    visual: Any,
    event_module: Any,
    text: str,
) -> bool:
    prompt = visual.TextStim(win, text=text, height=0.045, color="white", wrapWidth=1.5)
    prompt.draw()
    win.flip()
    while True:
        redraw_psychopy_after_resize(win, prompt)
        keys = [value.name for value in poll_psychopy_keys(event_module)]
        if any(key in {"escape", "q"} for key in keys):
            return False
        if "space" in keys:
            return True
        sleep(0.01)


def _play_baseline_end_signal(
    output: PsychoPyAudioOutput | None,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Play the optional cue without allowing audio to fail the baseline."""

    return play_psychopy_end_signal(output, config)


def _baseline_phase_result(
    name: str,
    planned: float,
    start: float,
    end: float,
    start_lsl: float | None,
    end_lsl: float | None,
    status: str,
    aborted: bool,
) -> dict[str, Any]:
    return {
        "phase": name,
        "planned_duration_seconds": planned,
        "actual_duration_seconds": end - start,
        "start_monotonic_timestamp": start,
        "end_monotonic_timestamp": end,
        "start_lsl_timestamp": start_lsl,
        "end_lsl_timestamp": end_lsl,
        "completion_status": status,
        "aborted": bool(aborted),
    }


def _baseline_phase_aborted(phase: dict[str, Any]) -> bool:
    """Read the v1 field while remaining compatible with early rehearsal artifacts."""
    if "aborted" in phase:
        return bool(phase.get("aborted"))
    return bool(phase.get("abort_status", False))


def _close_resources(*resources: tuple[str, Any]) -> list[str]:
    """Close every resource without allowing one cleanup error to mask another failure."""
    warnings: list[str] = []
    for name, resource in resources:
        if resource is None:
            continue
        try:
            resource.close()
        except Exception as exc:
            warnings.append(f"{name} cleanup failed: {type(exc).__name__}: {exc}")
    return warnings


def _disable_nonrecording_processes(
    config: dict[str, Any],
    *,
    recorder_backend: str | None = None,
) -> None:
    processes = config.setdefault("processes", {})
    recorder = processes.setdefault("recorder", {})
    backend = str(recorder_backend or recorder.get("backend") or "lsl_csv")
    recorder.update({"enabled": True, "backend": backend})
    processes.setdefault("realtime_processor", {}).update({"enabled": False, "backend": "disabled"})
    processes.setdefault("feedback", {}).update({"enabled": False, "backend": "disabled"})
    processes.setdefault("dashboard", {}).update({"enabled": False})
    processes.setdefault("offline_analyzer", {}).update({"enabled": False, "backend": "disabled"})
    config.setdefault("realtime", {})["enabled"] = False


def _force_recording_only_contract(config: dict[str, Any]) -> None:
    realtime = config.setdefault("realtime", {})
    realtime["enabled"] = False
    realtime.setdefault("capture", {})["enabled"] = False
    realtime.setdefault("inference", {})["enabled"] = False
    realtime.setdefault("classifier", {})["enabled"] = False
    realtime.setdefault("decision_policy", {}).update(
        {"kind": "observe_only", "enabled": False, "allow_task_adaptation": False, "allow_stimulation": False}
    )
    realtime.setdefault("feedback", {}).update(
        {"mode": "observe_only", "allow_task_adaptation": False, "allow_stimulation": False}
    )
    realtime["feedback"].setdefault("client", {}).update({"enabled": False, "backend": "disabled"})
    config.setdefault("tasks", {}).setdefault("dynamic_sart", {}).update(
        {"allow_task_adaptation": False, "allow_stimulation": False}
    )
    processes = config.setdefault("processes", {})
    processes.setdefault("realtime_processor", {}).update({"enabled": False, "backend": "disabled"})
    processes.setdefault("feedback", {}).update({"enabled": False, "backend": "disabled"})
    processes.setdefault("dashboard", {}).update({"enabled": False})
    processes.setdefault("offline_analyzer", {}).update({"enabled": False, "backend": "disabled"})


def _initial_manifest(
    options: DsartRecordingOptions,
    config: dict[str, Any],
    *,
    visit_id: str,
    visit_dir: Path,
    session_1_seed: int,
    session_2_seed: int,
    break_seconds: float,
    config_issues: list[dict[str, str]],
) -> dict[str, Any]:
    hardware = dict(config.get("hardware", {}).get("eeg", {}) or {})
    practice_policy = copy.deepcopy(config.get("recording_suite", {}).get("practice_policy", {}))
    normal_trial_count = int(practice_policy.get("normal_trial_count", _normal_recipe_trial_count(config)))
    smoke_test_override = bool(
        options.trials_per_session != normal_trial_count
        or options.baseline_seconds is not None
        or break_seconds != 600.0
    )
    return {
        "schema": SUITE_SCHEMA,
        "recipe": options.recipe,
        "participant_id": options.participant_id,
        "visit_id": visit_id,
        "hardware_profile": hardware.get("profile"),
        "channel_mapping_source": hardware.get("mapping_source"),
        "channel_mapping_version": hardware.get("mapping_version"),
        "operator": options.operator or os.environ.get("USER") or os.environ.get("USERNAME") or "unspecified",
        "visit_start": _now(),
        "visit_end": None,
        "overall_status": "planned",
        "status": "planned",
        "initial_preflight": None,
        "baseline_session_directory": None,
        "dsart_session_1_directory": None,
        "break_start": None,
        "break_end": None,
        "second_preflight": None,
        "dsart_session_2_directory": None,
        "session_1_seed": session_1_seed,
        "session_2_seed": session_2_seed,
        "trials_per_session": int(options.trials_per_session),
        "include_practice": bool(options.include_practice),
        "practice_policy": practice_policy,
        "task_mode": options.task_mode,
        "record_eeg": bool(options.record_eeg),
        "require_eeg": bool(options.require_eeg),
        "baseline_seconds_per_phase": (
            None if options.baseline_seconds is None else float(options.baseline_seconds)
        ),
        "window_size": list(config.get("hardware", {}).get("display", {}).get("size", [1000, 700])),
        "full_screen": bool(config.get("hardware", {}).get("display", {}).get("full_screen", False)),
        "smoke_test_override": smoke_test_override,
        "session_1_sequence_hash": None,
        "session_2_sequence_hash": None,
        "visit_master_seed": options.master_seed,
        "break_seconds": break_seconds,
        "warnings": [issue["detail"] for issue in config_issues if issue["status"] == "warn"],
        "aborts": [],
        "resumed_phases": [],
        "partial_recordings": [],
        "software_version": _software_version(),
        "configuration_hashes": {"recording_recipe": _hash_payload(config)},
        "config_path": str(Path(options.config_path).expanduser().resolve()),
        "session_root": str(config.get("runtime", {}).get("session_root", "data")),
        "visit_directory": str(visit_dir),
        "phase_order": list(PHASE_ORDER),
        "phases": {phase: {"status": "planned", "attempts": []} for phase in PHASE_ORDER},
        "scientific_limitations": [
            "hardware and participant are confounded across the two planned participants",
            "this technical pilot cannot establish hardware superiority, cross-participant generalization, or EEG incremental validity",
            *(
                ["smoke-test duration overrides are not a scientific DSART recording"]
                if smoke_test_override
                else []
            ),
        ],
    }


def _record_dsart_attempt(manifest: dict[str, Any], session_index: int, result: dict[str, Any]) -> None:
    key = f"dsart_session_{session_index}_directory"
    hash_key = f"session_{session_index}_sequence_hash"
    if result.get("status") == "completed":
        manifest[key] = result.get("session_dir")
        manifest[hash_key] = result.get("sequence_hash")
    elif result.get("session_dir"):
        manifest.setdefault("partial_recordings", []).append(
            {
                "phase": f"dsart_session_{session_index}",
                "session_dir": result.get("session_dir"),
                "status": result.get("status"),
                "seed": result.get("seed"),
                "error": result.get("error"),
            }
        )


def _incomplete_phase_detail(label: str, result: dict[str, Any]) -> str:
    detail = result.get("error") or result.get("abort_reason")
    if not detail:
        validation = dict(result.get("validation") or {})
        failures = list(validation.get("failures") or [])
        detail = failures[0] if failures else f"status={result.get('status', 'unknown')}"
    return f"{label} did not complete: {detail}"


def _start_phase(manifest: dict[str, Any], phase: str) -> None:
    entry = manifest["phases"][phase]
    entry["status"] = "running"
    entry["attempts"].append({"started_at": _now(), "status": "running"})
    manifest["overall_status"] = "running"
    manifest["status"] = "running"


def _complete_phase(manifest: dict[str, Any], phase: str, result: dict[str, Any]) -> None:
    entry = manifest["phases"][phase]
    entry["status"] = "completed"
    attempt = entry["attempts"][-1]
    attempt.update({"status": "completed", "completed_at": _now(), "result": result})


def _fail_phase(
    manifest: dict[str, Any],
    phase: str,
    exc: BaseException,
    result: dict[str, Any] | None,
) -> None:
    entry = manifest["phases"][phase]
    entry["status"] = "partial" if result and result.get("session_dir") else "failed"
    attempt = entry["attempts"][-1]
    attempt.update(
        {
            "status": entry["status"],
            "completed_at": _now(),
            "error": f"{type(exc).__name__}: {exc}",
            "result": result,
        }
    )


def _phase_is_complete(manifest: dict[str, Any], phase: str) -> bool:
    return manifest.get("phases", {}).get(phase, {}).get("status") == "completed"


def _latest_phase_result(manifest: dict[str, Any], phase: str) -> dict[str, Any] | None:
    attempts = manifest.get("phases", {}).get(phase, {}).get("attempts", [])
    for attempt in reversed(attempts):
        if attempt.get("status") == "completed" and isinstance(attempt.get("result"), dict):
            return dict(attempt["result"])
    return None


def _resolve_visit_id(options: DsartRecordingOptions, output_root: Path) -> str:
    if options.visit_id:
        return _safe_token(options.visit_id)
    if options.resume:
        base = output_root / "recording_suites" / options.participant_id
        candidates = sorted(
            (
                path
                for path in base.glob(f"*/{options.recipe}/recording_suite.json")
                if (_load_json(path) or {}).get("overall_status") != "completed"
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0].parent.parent.name
        raise FileNotFoundError("--resume could not find an incomplete visit; provide --visit-id")
    return datetime.now().strftime("visit-%Y%m%dT%H%M%S")


def _validate_resume_identity(
    manifest: dict[str, Any],
    options: DsartRecordingOptions,
    visit_id: str,
    seed_1: int,
    seed_2: int,
    *,
    configuration_hash: str,
) -> None:
    expected = {
        "recipe": options.recipe,
        "participant_id": options.participant_id,
        "visit_id": visit_id,
        "session_1_seed": seed_1,
        "session_2_seed": seed_2,
        "trials_per_session": int(options.trials_per_session),
        "include_practice": bool(options.include_practice),
        "task_mode": options.task_mode,
        "record_eeg": bool(options.record_eeg),
        "require_eeg": bool(options.require_eeg),
        "baseline_seconds_per_phase": (
            None if options.baseline_seconds is None else float(options.baseline_seconds)
        ),
        "window_size": list(options.window_size) if options.window_size is not None else [1000, 700],
        "full_screen": (
            bool(options.full_screen) if options.full_screen is not None else bool(manifest.get("full_screen", False))
        ),
    }
    mismatches = [key for key, value in expected.items() if manifest.get(key) != value]
    recorded_hash = dict(manifest.get("configuration_hashes") or {}).get("recording_recipe")
    if recorded_hash != configuration_hash:
        mismatches.append("configuration_hashes.recording_recipe")
    if mismatches:
        raise ValueError("resume identity does not match existing visit: " + ", ".join(mismatches))


def _has_scientific_recording(manifest: dict[str, Any]) -> bool:
    return bool(
        manifest.get("baseline_session_directory")
        or manifest.get("dsart_session_1_directory")
        or manifest.get("dsart_session_2_directory")
        or manifest.get("partial_recordings")
    )


def _public_suite_result(manifest: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    return {
        "schema": manifest.get("schema"),
        "status": manifest.get("overall_status"),
        "recipe": manifest.get("recipe"),
        "participant_id": manifest.get("participant_id"),
        "visit_id": manifest.get("visit_id"),
        "manifest_file": str(manifest_path),
        "session_root": manifest.get("session_root"),
        "baseline_session_directory": manifest.get("baseline_session_directory"),
        "dsart_session_1_directory": manifest.get("dsart_session_1_directory"),
        "dsart_session_2_directory": manifest.get("dsart_session_2_directory"),
        "session_1_seed": manifest.get("session_1_seed"),
        "session_2_seed": manifest.get("session_2_seed"),
        "trials_per_session": manifest.get("trials_per_session"),
        "include_practice": manifest.get("include_practice"),
        "practice_policy": manifest.get("practice_policy"),
        "task_mode": manifest.get("task_mode"),
        "record_eeg": manifest.get("record_eeg"),
        "require_eeg": manifest.get("require_eeg"),
        "baseline_seconds_per_phase": manifest.get("baseline_seconds_per_phase"),
        "window_size": manifest.get("window_size"),
        "full_screen": manifest.get("full_screen"),
        "smoke_test_override": manifest.get("smoke_test_override"),
        "session_1_sequence_hash": manifest.get("session_1_sequence_hash"),
        "session_2_sequence_hash": manifest.get("session_2_sequence_hash"),
        "warnings": manifest.get("warnings", []),
        "aborts": manifest.get("aborts", []),
        "partial_recordings": manifest.get("partial_recordings", []),
    }


def _issue(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def _append_jsonl_atomic_event(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
        handle.flush()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{monotonic_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        _replace_atomic_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{monotonic_ns()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            if text and not text.endswith("\n"):
                handle.write("\n")
        _replace_atomic_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_atomic_file(source: Path, target: Path) -> None:
    """Publish a suite artifact despite short-lived Windows file locks."""

    last_error: OSError | None = None
    for delay in _ATOMIC_REPLACE_RETRY_DELAYS_SECONDS:
        if delay:
            sleep(delay)
        try:
            source.replace(target)
            return
        except OSError as exc:
            if not _is_transient_replace_error(exc):
                raise
            last_error = exc
    if last_error is not None:
        raise last_error


def _is_transient_replace_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload if isinstance(payload, dict) else None


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _acquisition_config_sha256(config: dict[str, Any]) -> str:
    """Identify the EEG/display/marker/recorder contract checked by preflight.

    Optional operator audio is excluded: losing or changing a speaker must not
    invalidate a completed baseline or prevent task resume.
    """

    hardware = copy.deepcopy(config.get("hardware", {}))
    hardware.pop("audio", None)
    return _hash_payload(
        {
            "hardware": hardware,
            "recorder": config.get("processes", {}).get("recorder", {}),
        }
    )


def _require_preflight_acquisition_config(
    config: dict[str, Any],
    preflight: dict[str, Any],
    *,
    phase: str,
) -> None:
    """Prevent a phase from silently using a different acquisition contract."""

    checked = str(preflight.get("acquisition_config_sha256") or "").strip()
    if not checked:
        # Retain compatibility with resumable preflight reports created before
        # this provenance field was introduced.
        return
    current = _acquisition_config_sha256(config)
    compatible_operational_upgrade = checked in _operational_hardware_upgrade_hashes(config)
    if current != checked and not compatible_operational_upgrade:
        raise RuntimeError(
            f"{phase} acquisition configuration changed after preflight; rerun preflight "
            "before recording"
        )


def _operational_hardware_upgrade_hashes(config: dict[str, Any]) -> set[str]:
    """Recognize narrowly scoped timing/audio safety upgrades after preflight.

    Every visual phase measures its own live window, so these fields change how
    that local measurement is performed rather than the EEG/marker/recorder
    acquisition contract.  Removing only this exact allowlist reconstructs the
    v1 hash of an otherwise identical preflight; all other drift stays fatal.
    """

    compatible: list[dict[str, Any]] = []
    legacy = copy.deepcopy(config)
    display = legacy.setdefault("hardware", {}).setdefault("display", {})
    removed = False
    for name in (
        "refresh_rate_window_settle_seconds",
        "refresh_rate_retry_settle_seconds",
        "refresh_rate_warmup_frames",
        "refresh_rate_sample_frames",
    ):
        removed = display.pop(name, None) is not None or removed
    if removed:
        compatible.append(legacy)
    current_display = config.get("hardware", {}).get("display", {})
    if bool(current_display.get("full_screen", False)):
        windowed = copy.deepcopy(config)
        windowed["hardware"]["display"]["full_screen"] = False
        compatible.append(windowed)
        if removed:
            legacy_windowed = copy.deepcopy(legacy)
            legacy_windowed["hardware"]["display"]["full_screen"] = False
            compatible.append(legacy_windowed)
    return {_acquisition_config_sha256(candidate) for candidate in compatible}


def _safe_token(value: str) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_"} else "-" for character in str(value)).strip("-")


def _software_version() -> str:
    try:
        return version("eegle")
    except PackageNotFoundError:
        return "source-checkout"


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


if __name__ == "__main__":
    raise SystemExit(main())
