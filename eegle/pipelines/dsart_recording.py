"""Abort-safe two-session Dynamic SART raw-recording suites."""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
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
from eegle.devices.labrecorder_xdf import labrecorder_environment
from eegle.devices.xdf_integrity import validate_xdf_recording
from eegle.experiment import ForwardExperimentRunner
from eegle.feedback_manager import FeedbackManager
from eegle.hardware.profiles import expected_profile
from eegle.hardware.system import CheckResult
from eegle.io.events import EventLogger
from eegle.lsl import LslMarkerOutlet, NullMarkerOutlet, lsl_local_clock, session_marker_source_id
from eegle.preflight import run_preflight
from eegle.psychopy_input import clear_psychopy_keys, poll_psychopy_keys
from eegle.recording_health import RecorderHealthMonitor
from eegle.runtime import prepare_psychopy_runtime
from eegle.session import SessionPaths, create_session
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
_ATOMIC_REPLACE_RETRY_DELAYS_SECONDS = (0.0, 0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8)


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
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "completed" else 1


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
        help="Windowed PsychoPy size in pixels; DSART recipes are not full-screen by default",
    )
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
                    if result["status"] == "fail":
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
    display = dict(config.get("hardware", {}).get("display", {}) or {})
    if bool(display.get("full_screen", True)):
        issues.append(_issue("fail", "hardware.display.full_screen must be false for DSART recording"))
    display_size = list(display.get("size", []))
    if len(display_size) != 2 or any(float(value) <= 0.0 for value in display_size):
        issues.append(_issue("fail", "hardware.display.size must contain two positive values"))
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
        display_ok = display_check is not None and display_check.status == "ok"
        check_payloads.append(
            CheckResult(
                "dsart_display_contract",
                "ok" if display_ok else "fail",
                "PsychoPy display dependency is ready" if display_ok else "PsychoPy is required for live DSART acquisition",
                {} if display_check is None else dict(display_check.data),
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
    identity = CheckResult(
        "visit_identity",
        "ok" if participant_id.strip() and visit_id.strip() else "fail",
        f"participant={participant_id}; visit={visit_id}",
        {"participant_id": participant_id, "visit_id": visit_id, "recipe": recipe},
    )
    check_payloads.append(identity.__dict__)
    marker = _marker_loopback_check(config, participant_id, visit_id, phase, enabled=record_eeg)
    check_payloads.append(marker.__dict__)

    electrode_path: Path | None = None
    if record_eeg:
        electrode_report = _electrode_report(
            expected_channel_names,
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
    report = {
        "schema": PREFLIGHT_SCHEMA,
        "phase": phase,
        "status": status,
        "participant_id": participant_id,
        "visit_id": visit_id,
        "recipe": recipe,
        "created_at": _now(),
        "checks": check_payloads,
        "eeg_probe": probe,
        "channel_contract": channel_contract,
        "electrode_quality_file": None if electrode_path is None else str(electrode_path),
        "comparison_to_initial": comparison,
        "warnings": [item["detail"] for item in check_payloads if item.get("status") == "warn"],
        "failures": [item["detail"] for item in check_payloads if item.get("status") == "fail"],
    }
    report_path = output_dir / f"{phase}.json"
    report["report_file"] = str(report_path)
    _write_json_atomic(report_path, report)
    return report


def assess_channel_contract(
    observed_names: list[str],
    expected_names: list[str],
    *,
    original_names: list[str] | None = None,
    mapping_source: str | None = None,
    require_eeg: bool,
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
        "mapping_version": 1,
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
    probe_seconds = _optional_float(probe.get("probe_seconds")) or _optional_float(eeg_config.get("sample_probe_seconds"))
    sample_count = int(probe.get("sample_count") or 0)
    expected_samples = None if expected_rate is None or probe_seconds is None else expected_rate * probe_seconds
    sample_fraction = None if not expected_samples else sample_count / expected_samples
    failures: list[str] = []
    warnings: list[str] = []
    if probe.get("status") not in {"ok", "warn"} or sample_count <= 0:
        (failures if require_eeg else warnings).append("sample probe did not receive EEG samples")
    if expected_rate is not None:
        if observed_rate is None:
            (failures if require_eeg else warnings).append("EEG stream did not declare a sample rate")
        elif abs(observed_rate - expected_rate) >= 1.0:
            (failures if require_eeg else warnings).append(
                f"EEG sample rate is {observed_rate:g} Hz; expected {expected_rate:g} Hz"
            )
    if eeg_config.get("recording_lsl_processing") == "source_preserving":
        correction = _optional_float(probe.get("initial_time_correction_seconds"))
        if correction is None:
            (failures if require_eeg else warnings).append(
                "LSL time correction is unavailable; source EEG timestamps cannot be aligned to task markers"
            )
    if sample_fraction is not None:
        if sample_fraction < 0.5:
            (failures if require_eeg else warnings).append(
                f"sample probe received only {sample_fraction:.0%} of the nominal sample count"
            )
        elif sample_fraction < 0.8:
            warnings.append(f"sample probe received {sample_fraction:.0%} of the nominal sample count")
    if quality:
        if not bool(quality.get("timestamps_finite", False)):
            (failures if require_eeg else warnings).append("EEG probe timestamps are missing or non-finite")
        if not bool(quality.get("timestamps_strictly_increasing", False)):
            (failures if require_eeg else warnings).append("EEG probe timestamps are not strictly increasing")
        maximum_gap = _optional_float(quality.get("maximum_timestamp_gap_seconds"))
        if maximum_gap is not None and expected_rate and maximum_gap > 5.0 / expected_rate:
            warnings.append(f"EEG probe maximum timestamp gap was {maximum_gap:.6f} seconds")
    elif require_eeg:
        failures.append("EEG probe did not provide timestamp-continuity diagnostics")
    status = "fail" if failures else ("warn" if warnings else "ok")
    detail = "; ".join(failures or warnings) if failures or warnings else "EEG sample rate, volume, and timestamps passed"
    return {
        "status": status,
        "detail": detail,
        "expected_sample_rate_hz": expected_rate,
        "observed_sample_rate_hz": observed_rate,
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
    accepted_by = "software_only"
    accepted = True
    if options.task_mode == "psychopy" and options.record_eeg:
        if options.electrodes_confirmed:
            accepted_by = "--confirm-electrodes"
        else:
            try:
                answer = input(
                    f"{phase}: inspect NIC contact/impedance and the report above. "
                    "Type YES to accept the electrodes and continue: "
                )
            except EOFError:
                answer = ""
            accepted = answer.strip() == "YES"
            accepted_by = "interactive_terminal"
    report["operator_acceptance"] = {
        "accepted": accepted,
        "method": accepted_by,
        "accepted_at": _now() if accepted else None,
        "operator": options.operator,
    }
    report_file = report.get("report_file")
    if report_file:
        _write_json_atomic(Path(str(report_file)), report)
    electrode_file = report.get("electrode_quality_file")
    if accepted and electrode_file:
        electrode_report = _load_json(Path(str(electrode_file))) or {}
        electrode_report["operator_confirmed"] = True
        electrode_report["operator_confirmation_method"] = accepted_by
        electrode_report["operator_confirmation_at"] = _now()
        _write_json_atomic(Path(str(electrode_file)), electrode_report)
    if not accepted:
        raise RuntimeError(f"{phase} electrode/contact check was not accepted by the operator")


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
        failures.append("sample rate changed by at least 1 Hz")
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
        if options.record_eeg:
            result["status"] = "failed"
            result.setdefault("failure_kind", "recorder_lifecycle_failure")
    try:
        baseline_validation = _baseline_recording_validation(paths, result, record_eeg=options.record_eeg)
    except Exception as exc:
        baseline_validation = {
            "status": "fail",
            "failures": [f"baseline validation failed: {type(exc).__name__}: {exc}"],
            "warnings": [],
        }
    result["validation"] = baseline_validation
    if baseline_validation["failures"]:
        result["status"] = "failed"
        result.setdefault("warnings", []).extend(baseline_validation["failures"])
    try:
        _write_json_atomic(paths.events / "dsart_baseline_results.json", result)
        _write_json_atomic(paths.completion_summary, result)
    except Exception as exc:
        result["status"] = "failed"
        result["failure_kind"] = "post_recording_metadata_failure"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result.setdefault("warnings", []).append(
            "baseline raw data were retained, but one or more completion metadata files could not be published"
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
            drain_seconds = float(
                config.get("recording_rehearsal", {}).get(
                    "marker_receipt_drain_seconds",
                    0.25,
                )
            )
            if drain_seconds > 0:
                sleep(drain_seconds)
        failures = _close_resources(
            ("marker receipt recorder", marker_receipt),
            ("marker outlet", outlet if owns_marker_outlet else None),
        )
        if failures:
            raise RuntimeError("baseline dry-run cleanup failed: " + "; ".join(failures))
    return {
        "schema": BASELINE_SCHEMA,
        "status": "completed",
        "mode": "dry-run",
        "phases": phases,
        "planned_duration_seconds": eyes_open_seconds + eyes_closed_seconds,
        "actual_duration_seconds": eyes_open_seconds + eyes_closed_seconds,
        "aborted": False,
    }


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
    suite_config = dict(config.get("recording_suite", {}) or {})
    recorder_monitor = RecorderHealthMonitor(
        paths.process_logs / "recorder.status.json",
        required=record_eeg,
        stall_timeout_seconds=float(suite_config.get("recorder_stall_timeout_seconds", 5.0)),
    )
    try:
        win = visual.Window(
            fullscr=bool(display.get("full_screen", False)),
            screen=int(display.get("screen_index", 0)),
            size=tuple(display.get("size", [1000, 700])),
            winType=str(display.get("win_type", "pyglet")),
            units=str(display.get("units", "height")),
            color=display.get("background_color", "black"),
            allowGUI=bool(display.get("allow_gui", True)),
        )
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
                "Close your eyes, remain still, relax, and keep your eyes closed until you hear the end signal.\n\nPress SPACE, then close your eyes.",
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
                _play_baseline_end_signal()
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
        cleanup_warnings.extend(
            _close_resources(
                ("marker receipt recorder", marker_receipt),
                ("marker outlet", outlet if owns_marker_outlet else None),
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
    if draw_fixation:
        visual.TextStim(win, text="+", height=0.12, color="white").draw()
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
        now = monotonic()
        if recorder_monitor is not None and now >= next_health_check:
            health = recorder_monitor.check()
            next_health_check = now + 0.25
            if not health.ok:
                aborted = True
                abort_reason = "recorder_health_failure"
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
    ]
    completed = subprocess.run(command, check=False)
    result = _load_json(result_path)
    worker_metadata = {
        "mode": "fresh_python_process",
        "return_code": int(completed.returncode),
        "request_file": str(request_path),
        "result_file": str(result_path),
    }
    if result is None:
        return {
            "status": "failed",
            "session_index": session_index,
            "session_dir": None,
            "seed": int(seed),
            "error": f"DSART phase worker exited with code {completed.returncode} without a result artifact",
            "failure_kind": "phase_worker_failure",
            "practice_status": "unknown",
            "phase_worker": worker_metadata,
        }
    result["phase_worker"] = worker_metadata
    if completed.returncode != 0 and result.get("status") == "completed":
        result["status"] = "failed"
        result["error"] = f"DSART phase worker exited with code {completed.returncode} after reporting completion"
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


def _run_dsart_child_session_inline(
    config: dict[str, Any],
    options: DsartRecordingOptions,
    *,
    visit_id: str,
    session_index: int,
    seed: int,
    preflight: dict[str, Any],
) -> dict[str, Any]:
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
    try:
        _write_json_atomic(session_dir / "logs" / "suite_preflight.json", preflight)
        task_summary = dict((forward_payload.get("task") or {}).get("summary") or {})
        sequence_manifest = _load_json(session_dir / "events" / "stimulus_manifest.json") or {}
        if options.recipe == "dsart32":
            write_dsart8_overlap_manifest(session_dir, child_config)
    except Exception as exc:
        return {
            "status": "partial",
            "session_index": session_index,
            "session_dir": str(session_dir),
            "session_id": session_dir.name,
            "seed": int(seed),
            "error": f"{type(exc).__name__}: {exc}",
            "failure_kind": "post_recording_metadata_failure",
            "practice_status": "skipped" if not practice_enabled else "unknown",
            "raw_recording_retained": True,
        }
    analysis_error = None
    try:
        dynamic_report = analyze_dynamic_sart_session(session_dir, child_config)
    except Exception as exc:
        dynamic_report = {}
        analysis_error = f"{type(exc).__name__}: {exc}"
    try:
        validation = _child_session_validation(
            session_dir,
            task_summary,
            sequence_manifest,
            record_eeg=options.record_eeg,
            task_mode=options.task_mode,
            dynamic_report=dynamic_report,
            analysis_error=analysis_error,
        )
    except Exception as exc:
        return {
            "status": "partial",
            "session_index": session_index,
            "session_dir": str(session_dir),
            "session_id": session_dir.name,
            "seed": int(seed),
            "error": f"{type(exc).__name__}: {exc}",
            "failure_kind": "post_recording_validation_failure",
            "practice_status": "skipped" if not practice_enabled else "unknown",
            "raw_recording_retained": True,
        }
    status = "completed" if not validation["failures"] else "partial"
    return {
        "status": status,
        "session_index": session_index,
        "session_dir": str(session_dir),
        "session_id": session_dir.name,
        "seed": int(seed),
        "sequence_hash": sequence_manifest.get("sequence_id"),
        "practice_status": (
            "skipped"
            if not practice_enabled
            else ("passed" if task_summary.get("practice_trials", 0) else "not_recorded")
        ),
        "task_summary": task_summary,
        "validation": validation,
        "forward": forward_payload,
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
    expected = (
        {
            "experimental_trials": len(planned_rows),
            "support_trials": sum(int(row.get("phase") == "support") for row in planned_rows),
            "query_trials": sum(int(row.get("phase") == "query") for row in planned_rows),
            "no_go_trial_count": sum(int(bool(row.get("is_no_go"))) for row in planned_rows),
        }
        if planned_rows
        else {
            "experimental_trials": 600,
            "support_trials": 200,
            "query_trials": 400,
            "no_go_trial_count": 67,
        }
    )
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
    rows = []
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
                if family in {
                    "dynamic_sart_countdown_start",
                    "dynamic_sart_countdown_step",
                    "dynamic_sart_countdown_end",
                    "dynamic_sart_stimulus_onset",
                }:
                    rows.append((family, row))
    families = [family for family, _row in rows]
    step_values = [str(row.get("value")) for family, row in rows if family == "dynamic_sart_countdown_step"]
    failures = []
    if families.count("dynamic_sart_countdown_start") != 1:
        failures.append("countdown-start event count is not exactly one")
    if families.count("dynamic_sart_countdown_end") != 1:
        failures.append("countdown-end event count is not exactly one")
    if step_values != ["5", "4", "3", "2", "1", "GO!"]:
        failures.append(f"countdown steps are invalid: {step_values}")
    if "dynamic_sart_countdown_end" in families and "dynamic_sart_stimulus_onset" in families:
        if families.index("dynamic_sart_countdown_end") > families.index("dynamic_sart_stimulus_onset"):
            failures.append("countdown did not finish before the first stimulus onset")
    return {
        "status": "fail" if failures else "pass",
        "events_file": str(path),
        "step_values": step_values,
        "failures": failures,
    }


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
    if invalid_trial_timing:
        target.append(f"{len(invalid_trial_timing)} trial rows have invalid onset/offset/response-close ordering")
    if overlapping_trials:
        target.append(f"{len(overlapping_trials)} trial response windows overlap the next stimulus")
    raw_metadata = _load_json(session_dir / "raw" / "eeg_metadata.json") or {}
    raw_first = _optional_float(raw_metadata.get("first_lsl_timestamp"))
    raw_last = _optional_float(raw_metadata.get("last_lsl_timestamp"))
    if require_markers and onset_lsl_timestamps:
        if raw_first is None or raw_last is None:
            failures.append("raw EEG metadata lacks the timestamp span needed to verify marker overlap")
        elif raw_first > min(onset_lsl_timestamps) or raw_last < max(onset_lsl_timestamps):
            failures.append("raw EEG timestamp span does not cover every stimulus-onset marker")
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
        "onset_lsl_timestamps_strictly_increasing": not any(
            second <= first for first, second in zip(onset_lsl_timestamps, onset_lsl_timestamps[1:])
        ),
        "raw_eeg_timestamp_span": {"first": raw_first, "last": raw_last},
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
    expected = []
    for row in ledger_rows:
        marker_metadata = dict(row.get("metadata") or {})
        expected.append(
            {
                "label": str(row.get("label") or ""),
                "lsl_timestamp": _optional_float(marker_metadata.get("lsl_timestamp")),
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
                        }
                    )
    failures = []
    warnings = []
    target = failures if required else warnings
    if required and metadata.get("status") != "stopped":
        failures.append("independent marker receipt recorder status is not stopped")
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
    return {
        "status": "fail" if failures else ("warning" if warnings else "pass"),
        "required": required,
        "csv_file": str(csv_path),
        "metadata_file": str(metadata_path),
        "metadata": metadata,
        "expected_count": len(expected),
        "received_count": len(received),
        "timestamp_mismatch_indices": timestamp_mismatches,
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
    if required:
        if not raw_path.exists():
            failures.append("raw EEG CSV is missing")
        if metadata.get("status") != "stopped":
            failures.append("raw EEG recorder status is not stopped")
        if int(metadata.get("sample_count") or 0) <= 0:
            failures.append("raw EEG recorder did not retain any samples")
        if int(metadata.get("timestamp_gap_count") or 0) > 0:
            failures.append("raw EEG contains one or more timestamp gaps above the configured acquisition limit")
        if int(metadata.get("nonmonotonic_timestamp_count") or 0) > 0:
            failures.append("raw EEG contains nonmonotonic source timestamps")
        if contract.get("amplitude_samples_modified") is not False:
            failures.append("raw EEG metadata does not prove amplitude pass-through")
        if list(contract.get("amplitude_transformations") or []) != []:
            failures.append("raw EEG metadata reports an amplitude transformation")
        if contract.get("channel_value_order_modified") is not False:
            failures.append("raw EEG metadata does not prove channel-order pass-through")
        for operation in ("filtering", "resampling", "rereferencing", "artifact_rejection"):
            if contract.get(operation) != "none":
                failures.append(f"raw EEG metadata reports {operation} during acquisition")
        if contract.get("recording_timestamp_mode") != "source_preserving":
            failures.append("raw EEG metadata does not prove source-preserving timestamps")
        if contract.get("source_timestamp_retained") is not True:
            failures.append("raw EEG metadata does not prove original source timestamps were retained")
        if contract.get("initial_time_correction_available") is not True:
            failures.append("raw EEG recorder did not obtain an LSL correction for marker alignment")
        if header[:4] != [
            "lsl_timestamp",
            "local_received_time",
            "source_lsl_timestamp",
            "lsl_time_correction_seconds",
        ]:
            failures.append("raw EEG CSV does not retain corrected and original LSL timestamps")
        recorded_channels = header[4:]
        if expected_channels and recorded_channels != expected_channels:
            failures.append("raw EEG CSV channel columns do not match the configured physical device order")
        if list(stream.get("channel_names") or []) != recorded_channels:
            failures.append("raw EEG metadata channel order does not match the CSV header")
        if stream.get("channel_value_order_changed") is not False:
            failures.append("raw EEG stream metadata does not prove that channel values stayed in source order")
        if list(stream.get("amplitude_transformations") or []) != []:
            failures.append("raw EEG stream metadata reports an amplitude transformation")
        if list(stream.get("lsl_processing") or []) != []:
            failures.append("raw EEG inlet applied LSL processing despite the source-preserving contract")
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
        "failures": failures,
        "warnings": warnings,
    }
    recorder_backend = str(
        parameters.get("processes", {}).get("recorder", {}).get("backend", "lsl_csv")
    )
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
    expected_labels = {
        "dsart_baseline_eyes_open_start",
        "dsart_baseline_eyes_open_end",
        "dsart_baseline_eyes_closed_start",
        "dsart_baseline_eyes_closed_end",
    }
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
                if row.get("label") in expected_labels:
                    marker_rows.append(row)
    observed_labels = [str(row.get("label")) for row in marker_rows]
    target = failures if record_eeg else warnings
    if set(observed_labels) != expected_labels or len(observed_labels) != len(expected_labels):
        target.append("baseline boundary marker ledger is incomplete or duplicated")
    for row in marker_rows:
        metadata = dict(row.get("metadata") or {})
        if _optional_float(metadata.get("lsl_timestamp")) is None:
            target.append(f"baseline marker {row.get('label')} lacks an LSL timestamp")
        if expected_source_id and metadata.get("marker_stream_source_id") != expected_source_id:
            target.append(f"baseline marker {row.get('label')} has the wrong source ID")
    marker_receipt = _marker_receipt_integrity(paths.root, marker_rows, required=record_eeg)
    failures.extend(marker_receipt["failures"])
    warnings.extend(marker_receipt["warnings"])
    raw = _raw_eeg_integrity(paths.root, required=record_eeg)
    failures.extend(raw["failures"])
    warnings.extend(raw["warnings"])
    if record_eeg and phases:
        raw_metadata = dict(raw.get("metadata") or {})
        raw_first = _optional_float(raw_metadata.get("first_lsl_timestamp"))
        raw_last = _optional_float(raw_metadata.get("last_lsl_timestamp"))
        phase_starts = [
            value
            for value in (_optional_float(row.get("start_lsl_timestamp")) for row in phases)
            if value is not None
        ]
        phase_ends = [
            value
            for value in (_optional_float(row.get("end_lsl_timestamp")) for row in phases)
            if value is not None
        ]
        if raw_first is None or raw_last is None:
            failures.append("raw EEG metadata lacks the timestamp span needed to verify baseline overlap")
        elif phase_starts and phase_ends and (raw_first > min(phase_starts) or raw_last < max(phase_ends)):
            failures.append("raw EEG timestamp span does not cover the complete resting baseline")
    return {
        "status": "fail" if failures else ("warning" if warnings else "pass"),
        "failures": failures,
        "warnings": warnings,
        "expected_marker_source_id": expected_source_id,
        "marker_labels": observed_labels,
        "independent_marker_receipt": marker_receipt,
        "raw_integrity": raw,
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


def _marker_loopback_check(
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
        output_root.mkdir(parents=True, exist_ok=True)
        probe = output_root / ".eegle_dsart_write_probe"
        with probe.open("w", encoding="utf-8") as handle:
            handle.write("ok\n")
        probe.unlink(missing_ok=True)
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
    """Fail before acquisition when the selected suite root cannot be updated."""

    probe_dir = output_root / ".eegle_dsart_write_probe"
    probe_file = probe_dir / f"{os.getpid()}.json"
    try:
        _write_json_atomic(probe_file, {"probe": 1})
        _write_json_atomic(probe_file, {"probe": 2})
    except OSError as exc:
        message = _session_root_error_message(output_root, exc)
        if isinstance(exc, PermissionError):
            raise PermissionError(message) from exc
        raise OSError(message) from exc
    finally:
        try:
            probe_file.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            probe_dir.rmdir()
        except OSError:
            pass


def _session_root_error_message(output_root: Path, exc: OSError) -> str:
    return (
        f"DSART session root is not writable by the current Python process: {output_root} "
        f"({type(exc).__name__}: {exc}). Choose an approved data location with --session-root, "
        "for example $env:LOCALAPPDATA\\EEGle\\data on Windows, then rerun the same dsart8 or dsart32 command."
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
        keys = [value.name for value in poll_psychopy_keys(event_module)]
        if any(key in {"escape", "q"} for key in keys):
            return False
        if "space" in keys:
            return True
        sleep(0.01)


def _play_baseline_end_signal() -> None:
    try:
        from psychopy import sound

        tone = sound.Sound("C", secs=0.5)
        tone.play()
        sleep(0.55)
    except Exception:
        return


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
