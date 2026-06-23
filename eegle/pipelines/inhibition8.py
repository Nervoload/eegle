"""Observe-only Inhibition8 full experiment pipeline."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from eegle.analysis.html_summary import generate_experiment_html_report
from eegle.analysis.inhibition8 import replay_realtime_session, run_feature_behavior_analysis
from eegle.analysis.reanalysis import rerun_alpha_analysis
from eegle.analysis.reports import analyze_session
from eegle.config import load_config
from eegle.experiment import ForwardExperimentRunner
from eegle.hardware.system import CheckResult
from eegle.preflight import run_preflight, write_preflight_report
from eegle.runtime import ensure_runtime_environment
from eegle.telemetry import apply_cli_telemetry_overrides


DEFAULT_INHIBITION8_CONFIG = Path("configs/forward_go_nogo_inhibition8.json")
MAX_INHIBITION8_TRIALS = 100


@dataclass(frozen=True)
class Inhibition8PipelineOptions:
    config_path: str | Path = DEFAULT_INHIBITION8_CONFIG
    participant_id: str | None = None
    task_mode: str = "psychopy"
    trials: int = MAX_INHIBITION8_TRIALS
    record_eeg: bool = True
    require_eeg: bool = True
    lsl_wait_seconds: float = 5.0
    quiet: bool = False
    log_level: str | None = None
    trace: bool = False
    max_raw_points: int = 120000
    max_alpha_points: int = 12000


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "full":
        return cmd_full(args)
    if args.command == "reanalyze":
        return cmd_reanalyze(args)
    parser.error(f"unknown command {args.command}")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inhibition8",
        description="Observe-only Inhibition8 closed-loop Go/No-go pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    full = subparsers.add_parser(
        "full",
        help="Run calibration, observe-only Go/No-go, causal replay, exploratory analysis, and HTML summary",
    )
    full.add_argument("--config", default=str(DEFAULT_INHIBITION8_CONFIG), help="Pipeline config JSON")
    full.add_argument("--participant", default=None, help="Participant/session id override")
    full.add_argument("--trials", type=int, default=MAX_INHIBITION8_TRIALS, help="Main Go/No-go trial count, maximum 100")
    full.add_argument("--task-mode", choices=["psychopy", "dry-run"], default="psychopy")
    full.add_argument("--skip-eeg", action="store_true", help="Development mode: skip EEG recorder and realtime LSL")
    full.add_argument("--allow-missing-eeg", action="store_true", help="Warn instead of failing when setup checks find no EEG stream")
    full.add_argument("--lsl-wait", type=float, default=5.0, help="Seconds to wait during the Enobio8 LSL setup check")
    full.add_argument("--max-raw-points", type=int, default=120000, help="Maximum raw EEG points embedded in HTML replay")
    full.add_argument("--max-alpha-points", type=int, default=12000, help="Maximum alpha windows embedded in HTML plots")
    full.add_argument("--log-level", choices=["quiet", "default", "realtime", "debug"], default=None)
    full.add_argument("--trace", action="store_true")
    full.add_argument("--quiet", action="store_true")
    reanalyze = subparsers.add_parser(
        "reanalyze",
        help="Rerun posterior-alpha calibration, alpha validation, analysis, and HTML for an existing session",
    )
    reanalyze.add_argument("--config", default=str(DEFAULT_INHIBITION8_CONFIG), help="Fallback config JSON when session parameters are missing")
    reanalyze.add_argument("--session-dir", required=True, help="Existing session directory to reprocess")
    reanalyze.add_argument("--no-update-parameters", action="store_true", help="Do not write recalibrated alpha settings back to parameters.json")
    reanalyze.add_argument("--no-html", action="store_true", help="Skip regenerating reports/experiment_summary.html")
    reanalyze.add_argument("--max-raw-points", type=int, default=120000, help="Maximum raw EEG points embedded in HTML replay")
    reanalyze.add_argument("--max-alpha-points", type=int, default=12000, help="Maximum alpha windows embedded in HTML plots")
    return parser


def cmd_full(args: argparse.Namespace) -> int:
    try:
        options = _options_from_args(args)
    except ValueError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2, sort_keys=True))
        return 2
    try:
        summary = run_full_pipeline(options)
    except Exception as exc:
        payload = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "pipeline": "inhibition8.full",
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1
    print(json.dumps(_compact_summary(summary), indent=2, sort_keys=True))
    return 0 if summary.get("status") in {"complete", "degraded"} else 1


def cmd_reanalyze(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    try:
        summary = rerun_alpha_analysis(
            args.session_dir,
            config,
            update_parameters=not bool(args.no_update_parameters),
            write_html=not bool(args.no_html),
            max_raw_points=int(args.max_raw_points),
            max_alpha_points=int(args.max_alpha_points),
        )
        replay = replay_realtime_session(args.session_dir)
        behavior = run_feature_behavior_analysis(args.session_dir, config.get("analysis", {}).get("inhibition8", {}))
        publication = analyze_session(args.session_dir)
        html = None if bool(args.no_html) else generate_experiment_html_report(
            args.session_dir,
            max_raw_points=int(args.max_raw_points),
            max_alpha_points=int(args.max_alpha_points),
        )
        summary["realtime_feature_replay"] = replay
        summary["exploratory_feature_behavior"] = behavior
        summary["publication_analysis"] = publication
        summary["html_report"] = html
        if replay.get("status") != "pass":
            summary["status"] = "analytically_invalid"
        _write_reanalysis_summary(args.session_dir, summary)
    except Exception as exc:
        payload = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "pipeline": "inhibition8.reanalyze"}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") == "ok" else 1


def run_full_pipeline(options: Inhibition8PipelineOptions) -> dict[str, Any]:
    """Run the complete inhibition8 sequence strictly in order."""
    if options.trials < 1 or options.trials > MAX_INHIBITION8_TRIALS:
        raise ValueError(f"inhibition8 full supports 1-{MAX_INHIBITION8_TRIALS} main trials; got {options.trials}")
    config = load_config(options.config_path)
    config = _apply_pipeline_telemetry_options(config, options)
    contract_issues = _config_contract_issues(config)
    if contract_issues:
        raise ValueError("inhibition8 observe-only contract violated: " + ", ".join(contract_issues))
    cache_root = ensure_runtime_environment(config.get("runtime", {}).get("runtime_cache_dir", ".runtime"))

    started_at = datetime.now().isoformat(timespec="seconds")
    pipeline_steps: list[dict[str, Any]] = []
    preflight_report = cache_root / "inhibition8" / "preflight_inhibition8_full.json"

    _step_start("1/6 preflight", options)
    setup_checks = run_preflight(config, lsl_wait=options.lsl_wait_seconds, require_eeg=options.require_eeg)
    preflight_report.parent.mkdir(parents=True, exist_ok=True)
    write_preflight_report(setup_checks, preflight_report)
    setup_check_payload = _preflight_payload(setup_checks, preflight_report)
    pipeline_steps.append({"step": "preflight", **setup_check_payload})
    _print_preflight(setup_checks, preflight_report, options)
    if any(result.status == "fail" for result in setup_checks):
        summary = {
            "schema_version": 1,
            "pipeline": "inhibition8.full",
            "status": "failed",
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "steps": pipeline_steps,
            "failure_step": "preflight",
            "reason": "setup_check_failed",
        }
        _print_step("Pipeline stopped before calibration because preflight failed.", options)
        return summary

    _step_start("2/6 calibration suite", options)
    _print_step("Posterior alpha calibration will run before the main Go/No-go task.", options)
    _step_start("3/6 observe-only experiment", options)
    _print_step(f"Main Go/No-go task configured for {options.trials} stimuli.", options)
    forward = ForwardExperimentRunner(
        config,
        task_name="go_nogo",
        task_mode=options.task_mode,
        participant_id=options.participant_id,
        trials=options.trials,
        record_eeg=options.record_eeg,
        require_eeg=options.require_eeg,
        calibration_suite="posterior_alpha",
        preflight_results=setup_checks,
    ).run()
    forward_payload = forward.as_dict()
    calibration_issues = [] if forward.calibration is not None else ["calibration_result_missing"]
    forward_issues = _forward_validity_issues(forward_payload, record_eeg=options.record_eeg, expected_trials=options.trials)
    pipeline_steps.append(
        {
            "step": "calibration",
            "status": "failed" if calibration_issues else "complete",
            "session_dir": str(forward.session_dir),
            "calibration_status": None if forward.calibration is None else forward.calibration.get("status"),
            "issues": calibration_issues,
        }
    )
    pipeline_steps.append(
        {
            "step": "observe_only_experiment",
            "status": "failed" if forward_issues else "complete",
            "session_dir": str(forward.session_dir),
            "task_trials": None if forward.task is None else forward.task.summary.get("trials"),
            "issues": forward_issues,
        }
    )

    _step_start("4/6 causal replay validation", options)
    if options.record_eeg:
        replay_summary = replay_realtime_session(
            forward.session_dir,
            tolerance=float(config.get("analysis", {}).get("inhibition8", {}).get("replay_feature_tolerance", 1e-9)),
        )
    else:
        replay_summary = {"status": "skipped", "reason": "development_run_without_eeg"}
        _write_json_file(
            Path(forward.session_dir) / "reports" / "realtime_features" / "replay_summary.json",
            replay_summary,
        )
    replay_issues = [] if replay_summary.get("status") in {"pass", "skipped"} else [f"causal_replay_{replay_summary.get('status', 'missing')}"]
    pipeline_steps.append({"step": "causal_replay", "status": "failed" if replay_issues else "complete", "summary": replay_summary, "issues": replay_issues})

    _step_start("5/6 publication and exploratory analysis", options)
    behavior_summary = run_feature_behavior_analysis(
        forward.session_dir,
        config.get("analysis", {}).get("inhibition8", {}),
    ) if options.record_eeg else {"status": "skipped", "reason": "development_run_without_eeg"}
    if not options.record_eeg:
        _write_json_file(
            Path(forward.session_dir) / "reports" / "realtime_features" / "behavior_feature_summary.json",
            behavior_summary,
        )
    analysis_summary = analyze_session(forward.session_dir)
    pipeline_steps.append(
        {
            "step": "publication_and_exploratory_analysis",
            "status": "complete",
            "publication_summary_file": str(Path(forward.session_dir) / "reports" / "summary.json"),
            "exploratory_status": behavior_summary.get("status"),
        }
    )

    _step_start("6/6 HTML summary", options)
    html_summary = generate_experiment_html_report(
        forward.session_dir,
        max_raw_points=options.max_raw_points,
        max_alpha_points=options.max_alpha_points,
    )
    loaded_summary = _load_json(Path(forward.session_dir) / "reports" / "summary.json")
    analysis_issues = _analysis_validity_issues(loaded_summary, record_eeg=options.record_eeg)
    pipeline_steps.append(
        {
            "step": "html_summary",
            "status": "failed" if analysis_issues else "complete",
            "summary_file": str(Path(forward.session_dir) / "reports" / "summary.json"),
            "html_file": html_summary.get("html_file"),
            "analysis_status": loaded_summary.get("analysis_status") if loaded_summary else None,
            "issues": analysis_issues,
        }
    )

    issues = [*calibration_issues, *forward_issues, *replay_issues, *analysis_issues]
    status = "failed" if issues else ("degraded" if options.record_eeg and replay_summary.get("quality_status") == "degraded" else "complete")
    summary = {
        "schema_version": 1,
        "pipeline": "inhibition8.full",
        "status": status,
        "started_at": started_at,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "options": _options_payload(options),
        "steps": pipeline_steps,
        "preflight_report": str(preflight_report),
        "session_dir": str(forward.session_dir),
        "session_summary_file": str(forward.summary_file),
        "analysis_summary_file": str(Path(forward.session_dir) / "reports" / "summary.json"),
        "html_report_file": html_summary.get("html_file"),
        "calibration": forward_payload.get("calibration"),
        "task": forward_payload.get("task"),
        "eeg": forward_payload.get("eeg"),
        "analysis_status": loaded_summary.get("analysis_status") if loaded_summary else None,
        "realtime_feature_replay": replay_summary,
        "exploratory_feature_behavior": behavior_summary,
        "issues": issues,
    }
    _write_pipeline_summary(forward.session_dir, summary)
    _print_step(f"Inhibition8 full pipeline {status}: {forward.session_dir}", options)
    return summary


def _config_contract_issues(config: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    realtime = config.get("realtime", {})
    feedback = realtime.get("feedback", {})
    if config.get("hardware", {}).get("eeg", {}).get("profile") != "enobio8_inhibition":
        issues.append("wrong_montage_profile")
    if not bool(realtime.get("event_features", {}).get("enabled", False)):
        issues.append("event_features_disabled")
    if bool(realtime.get("event_features", {}).get("alpha_rebound", {}).get("enabled", False)):
        issues.append("alpha_rebound_enabled_in_v1")
    if bool(realtime.get("epoching", {}).get("enabled", False)):
        issues.append("legacy_realtime_epoch_inference_enabled")
    if realtime.get("decision_policy", {}).get("kind") != "observe_only":
        issues.append("decision_policy_not_observe_only")
    if list(realtime.get("decision_policy", {}).get("actions", [])) != ["observe_only"]:
        issues.append("non_observe_actions_configured")
    if realtime.get("model", {}).get("kind") != "none_observe_only":
        issues.append("realtime_model_decoder_configured")
    if bool(realtime.get("decision_policy", {}).get("allow_task_adaptation", False)):
        issues.append("task_adaptation_enabled")
    if bool(feedback.get("allow_task_adaptation", False)) or bool(feedback.get("client", {}).get("enabled", False)):
        issues.append("feedback_or_task_mutation_enabled")
    process_feedback = config.get("processes", {}).get("feedback", {})
    if bool(process_feedback.get("enabled", False)) or process_feedback.get("backend") not in {"disabled", "none"}:
        issues.append("feedback_process_enabled")
    return issues


def _forward_validity_issues(forward: dict[str, Any], *, record_eeg: bool, expected_trials: int | None = None) -> list[str]:
    issues: list[str] = []
    if forward.get("task") is None:
        issues.append("task_did_not_run")
    elif expected_trials is not None and int(forward["task"].get("summary", {}).get("trials", 0) or 0) != int(expected_trials):
        issues.append("task_did_not_complete_expected_trials")
    if (forward.get("processes") or {}).get("status") == "failed":
        issues.append("managed_process_validation_failed")
    if record_eeg and not forward.get("eeg"):
        issues.append("eeg_summary_missing")
    return issues


def _analysis_validity_issues(summary: dict[str, Any] | None, *, record_eeg: bool) -> list[str]:
    if summary is None:
        return ["analysis_summary_missing"]
    issues: list[str] = []
    if summary.get("processes", {}).get("status") == "failed":
        issues.append("analysis_detected_failed_process")
    if record_eeg:
        if summary.get("raw_eeg", {}).get("status") != "ok":
            issues.append("raw_eeg_missing_or_empty")
        if summary.get("realtime_feature_replay", {}).get("status") != "pass":
            issues.append("realtime_feature_replay_not_accepted")
    return issues


def _options_from_args(args: argparse.Namespace) -> Inhibition8PipelineOptions:
    if args.trials < 1 or args.trials > MAX_INHIBITION8_TRIALS:
        raise ValueError(f"--trials must be between 1 and {MAX_INHIBITION8_TRIALS}")
    record_eeg = not bool(args.skip_eeg)
    require_eeg = record_eeg and not bool(args.allow_missing_eeg)
    return Inhibition8PipelineOptions(
        config_path=args.config,
        participant_id=args.participant,
        task_mode=args.task_mode,
        trials=args.trials,
        record_eeg=record_eeg,
        require_eeg=require_eeg,
        lsl_wait_seconds=float(args.lsl_wait),
        quiet=bool(args.quiet),
        log_level=args.log_level,
        trace=bool(args.trace),
        max_raw_points=int(args.max_raw_points),
        max_alpha_points=int(args.max_alpha_points),
    )


def _apply_pipeline_telemetry_options(config: dict[str, Any], options: Inhibition8PipelineOptions) -> dict[str, Any]:
    namespace = argparse.Namespace(log_level=options.log_level, trace=options.trace, quiet=options.quiet)
    return apply_cli_telemetry_overrides(config, namespace)


def _preflight_payload(results: list[CheckResult], report: Path) -> dict[str, Any]:
    return {
        "status": "failed" if any(result.status == "fail" for result in results) else "complete",
        "report_file": str(report),
        "checks": [result.__dict__ for result in results],
    }


def _print_preflight(results: list[CheckResult], report: Path, options: Inhibition8PipelineOptions) -> None:
    if options.quiet:
        return
    for result in results:
        print(f"{result.status.upper():5} {result.name:18} {result.detail}")
    print(f"preflight report: {report}")


def _step_start(label: str, options: Inhibition8PipelineOptions) -> None:
    _print_step(f"[inhibition8] {label}", options)


def _print_step(message: str, options: Inhibition8PipelineOptions) -> None:
    if not options.quiet:
        print(message, flush=True)


def _write_pipeline_summary(session_dir: str | Path, summary: dict[str, Any]) -> None:
    target = Path(session_dir) / "reports" / "inhibition8_full_summary.json"
    _write_json_file(target, summary)


def _write_reanalysis_summary(session_dir: str | Path, summary: dict[str, Any]) -> None:
    root = Path(session_dir)
    target = root / "reports" / "inhibition8_reanalysis_summary.json"
    _write_json_file(target, summary)
    full_path = root / "reports" / "inhibition8_full_summary.json"
    full = _load_json(full_path)
    if not full:
        return
    full["current_analytical_status"] = (
        "pass" if summary.get("realtime_feature_replay", {}).get("status") == "pass" else summary.get("status", "unknown")
    )
    full["latest_reanalysis"] = {
        "status": summary.get("status"),
        "summary_file": str(target),
        "realtime_feature_replay": summary.get("realtime_feature_replay", {}).get("status"),
        "analysis_status": summary.get("publication_analysis", {}).get("analysis_status"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    _write_json_file(full_path, full)


def _write_json_file(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _options_payload(options: Inhibition8PipelineOptions) -> dict[str, Any]:
    payload = asdict(options)
    payload["config_path"] = str(payload["config_path"])
    return payload


def _compact_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "pipeline": summary.get("pipeline"),
        "status": summary.get("status"),
        "session_dir": summary.get("session_dir"),
        "preflight_report": summary.get("preflight_report"),
        "calibration": None if summary.get("calibration") is None else {
            "status": summary["calibration"].get("status"),
            "online_band": summary["calibration"].get("online_band"),
            "summary_file": summary["calibration"].get("files", {}).get("summary_json"),
        },
        "task": summary.get("task"),
        "analysis_summary_file": summary.get("analysis_summary_file"),
        "html_report_file": summary.get("html_report_file"),
        "analysis_status": summary.get("analysis_status"),
        "steps": [
            {
                "step": step.get("step"),
                "status": step.get("status"),
                "session_dir": step.get("session_dir"),
                "html_file": step.get("html_file"),
            }
            for step in summary.get("steps", [])
        ],
        "reason": summary.get("reason"),
    }


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


if __name__ == "__main__":
    raise SystemExit(main())
