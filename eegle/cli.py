"""Command-line interface for EEGle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from eegle.analysis.erp import run_erp_analysis
from eegle.analysis.classification import evaluate_classifier_session, replay_classifier_session
from eegle.analysis.html_summary import generate_experiment_html_report
from eegle.analysis.inhibition8 import replay_realtime_session, run_feature_behavior_analysis
from eegle.analysis.reanalysis import rerun_alpha_analysis
from eegle.analysis.reports import analyze_session
from eegle.config import DEFAULT_CONFIG, load_config
from eegle.experiment import ForwardExperimentRunner
from eegle.factory import make_task_component
from eegle.preflight import run_preflight, write_preflight_report
from eegle.realtime.controller import ClosedLoopController
from eegle.realtime.epoching import extract_epochs_for_session
from eegle.realtime.models import train_epoch_model
from eegle.runtime import ensure_runtime_environment
from eegle.session import create_session
from eegle.tasks.registry import get_task_spec, list_task_specs
from eegle.telemetry import apply_cli_telemetry_overrides


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(getattr(args, "config", DEFAULT_CONFIG))
    config = apply_cli_telemetry_overrides(config, args)
    ensure_runtime_environment(config.get("runtime", {}).get("runtime_cache_dir", ".runtime"))
    return args.func(args, config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eegle", description="Reproducible realtime EEG experiments")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Path to experiment JSON config")
    _add_telemetry_args(parser)
    subparsers = parser.add_subparsers(required=True)

    setup_check = subparsers.add_parser(
        "check-setup",
        aliases=["doctor"],
        help="Check Python dependencies, LSL discovery, and configured EEG hardware",
    )
    _add_config_arg(setup_check)
    _add_telemetry_args(setup_check)
    setup_check.add_argument("--lsl-wait", type=float, default=1.0, help="Seconds to wait for LSL stream discovery")
    setup_check.add_argument("--require-eeg", action="store_true", help="Fail if no Enobio/NIC2 LSL stream is found")
    setup_check.add_argument("--allow-missing-eeg", action="store_true", help="Warn instead of failing if no Enobio/NIC2 LSL stream is found")
    setup_check.add_argument("--save", default=None, help="Optional JSON report path")
    setup_check.set_defaults(func=cmd_check_setup)

    tasks = subparsers.add_parser("list-tasks", help="List known experiment tasks")
    _add_config_arg(tasks)
    _add_telemetry_args(tasks)
    tasks.set_defaults(func=cmd_list_tasks)

    init = subparsers.add_parser("init-session", help="Create a session directory without running a task")
    _add_config_arg(init)
    _add_telemetry_args(init)
    init.add_argument("--task", default=None, help="Task name")
    init.add_argument("--participant", default=None, help="Participant/session id")
    init.add_argument("--root", default=None, help="Session root directory")
    init.set_defaults(func=cmd_init_session)

    run_task = subparsers.add_parser("run-task", help="Run a task scaffold")
    _add_config_arg(run_task)
    _add_telemetry_args(run_task)
    run_task.add_argument("--task", default=None, help="Task name")
    run_task.add_argument("--participant", default=None, help="Participant/session id")
    run_task.add_argument("--mode", choices=["dry-run", "psychopy"], default="dry-run")
    run_task.add_argument("--trials", type=int, default=None)
    run_task.set_defaults(func=cmd_run_task)

    forward = subparsers.add_parser("run-forward", help="Run forward task + optional EEG recording experiment")
    _add_config_arg(forward)
    _add_telemetry_args(forward)
    forward.add_argument("--task", default=None, help="Task name")
    forward.add_argument("--participant", default=None, help="Participant/session id")
    forward.add_argument("--task-mode", choices=["dry-run", "psychopy"], default="psychopy")
    forward.add_argument("--trials", type=int, default=None)
    forward.add_argument("--skip-eeg", action="store_true", help="Do not start the LSL EEG recorder")
    forward.add_argument("--require-eeg", action="store_true", help="Fail preflight if no Enobio/NIC2 LSL stream is found")
    forward.add_argument("--allow-missing-eeg", action="store_true", help="Allow task to run even if no EEG stream is found")
    forward.add_argument(
        "--calibration-suite",
        choices=["posterior_alpha"],
        default=None,
        help="Run a calibration suite before the task and use its results for realtime processing",
    )
    forward.set_defaults(func=cmd_run_forward)

    realtime = subparsers.add_parser("run-realtime", help="Run a simulated realtime controller pass")
    _add_config_arg(realtime)
    _add_telemetry_args(realtime)
    realtime.add_argument("--sample-rate", type=float, default=None)
    realtime.set_defaults(func=cmd_run_realtime)

    sim = subparsers.add_parser("simulate-eeg", help="Start a development-only simulated EEG LSL outlet")
    _add_config_arg(sim)
    _add_telemetry_args(sim)
    sim.add_argument("--name", default="EnobioMock")
    sim.add_argument("--channels", type=int, default=32)
    sim.add_argument("--sample-rate", type=float, default=500.0)
    sim.add_argument("--duration", type=float, default=30.0)
    sim.set_defaults(func=cmd_simulate_eeg)

    analyze = subparsers.add_parser("analyze", help="Generate a post-session summary report")
    _add_config_arg(analyze)
    _add_telemetry_args(analyze)
    analyze.add_argument("--session-dir", required=True)
    analyze.set_defaults(func=cmd_analyze)

    html_report = subparsers.add_parser("report-html", help="Generate an interactive post-session HTML replay report")
    _add_config_arg(html_report)
    _add_telemetry_args(html_report)
    html_report.add_argument("--session-dir", required=True)
    html_report.add_argument("--output", default=None, help="Output HTML path; defaults to <session>/reports/experiment_summary.html")
    html_report.add_argument("--max-raw-points", type=int, default=120000, help="Maximum raw EEG points embedded for replay")
    html_report.add_argument("--max-alpha-points", type=int, default=12000, help="Maximum alpha windows embedded for plots")
    html_report.set_defaults(func=cmd_report_html)

    reanalyze_alpha = subparsers.add_parser("reanalyze-alpha", help="Rerun posterior-alpha calibration and reports from an existing session")
    _add_config_arg(reanalyze_alpha)
    _add_telemetry_args(reanalyze_alpha)
    reanalyze_alpha.add_argument("--session-dir", required=True)
    reanalyze_alpha.add_argument("--no-update-parameters", action="store_true", help="Do not write the recalibrated band back to parameters.json")
    reanalyze_alpha.add_argument("--no-html", action="store_true", help="Skip regenerating reports/experiment_summary.html")
    reanalyze_alpha.add_argument("--max-raw-points", type=int, default=120000, help="Maximum raw EEG points embedded for replay")
    reanalyze_alpha.add_argument("--max-alpha-points", type=int, default=12000, help="Maximum alpha windows embedded for plots")
    reanalyze_alpha.set_defaults(func=cmd_reanalyze_alpha)

    replay_realtime = subparsers.add_parser("replay-realtime", help="Replay captured realtime engine inputs and validate staged features")
    _add_config_arg(replay_realtime)
    _add_telemetry_args(replay_realtime)
    replay_realtime.add_argument("--session-dir", required=True)
    replay_realtime.add_argument("--tolerance", type=float, default=1e-9)
    replay_realtime.set_defaults(func=cmd_replay_realtime)

    erp = subparsers.add_parser("analyze-erp", help="Generate Go/No-go ERP and P300 dashboard reports")
    _add_config_arg(erp)
    _add_telemetry_args(erp)
    erp.add_argument("--session-dir", required=True)
    erp.add_argument("--tmin", type=float, default=None, help="Epoch start in seconds relative to stimulus onset")
    erp.add_argument("--tmax", type=float, default=None, help="Epoch end in seconds relative to stimulus onset")
    erp.add_argument("--p300-min", type=float, default=None, help="P300 search window start in seconds")
    erp.add_argument("--p300-max", type=float, default=None, help="P300 search window end in seconds")
    erp.set_defaults(func=cmd_analyze_erp)

    epochs = subparsers.add_parser("extract-epochs", help="Extract marker-locked EEG epochs for analysis and model training")
    _add_config_arg(epochs)
    _add_telemetry_args(epochs)
    epochs.add_argument("--session-dir", required=True)
    epochs.add_argument(
        "--source",
        choices=["auto", "markers_jsonl", "events_jsonl", "stimulus_manifest"],
        default="auto",
        help="Marker source to use. auto prefers realtime LSL markers, then stimulus manifest, then task events.",
    )
    epochs.add_argument("--output-dir", default=None, help="Derived epoch output directory; defaults to <session>/realtime/epochs")
    epochs.add_argument("--tmin", type=float, default=None, help="Epoch start in seconds relative to marker")
    epochs.add_argument("--tmax", type=float, default=None, help="Epoch end in seconds relative to marker")
    epochs.add_argument("--marker-prefix", default=None, help="Marker label prefix to epoch around")
    epochs.set_defaults(func=cmd_extract_epochs)

    train_model = subparsers.add_parser("train-model", help="Train an epoch classifier from epochs.npz")
    _add_config_arg(train_model)
    _add_telemetry_args(train_model)
    train_model.add_argument(
        "--kind",
        choices=["erp_roi_logreg", "sklearn_flatten_lda", "sklearn_xdawn_lda", "pyriemann_erp_cov", "torch_eegnet"],
        default=None,
    )
    train_model.add_argument("--epochs-npz", required=True, help="Path to realtime/epochs/epochs.npz")
    train_model.add_argument("--output", required=True, help="Output artifact path, or suffix-free model-bundle directory")
    train_model.set_defaults(func=cmd_train_model)

    evaluate_model = subparsers.add_parser("evaluate-model", help="Evaluate classifier predictions against the stimulus manifest")
    _add_config_arg(evaluate_model)
    _add_telemetry_args(evaluate_model)
    evaluate_model.add_argument("--session-dir", required=True)
    evaluate_model.set_defaults(func=cmd_evaluate_model)

    replay_classifier = subparsers.add_parser("replay-classifier", help="Replay captured classifier EEG and markers")
    _add_config_arg(replay_classifier)
    _add_telemetry_args(replay_classifier)
    replay_classifier.add_argument("--session-dir", required=True)
    replay_classifier.set_defaults(func=cmd_replay_classifier)

    return parser


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=argparse.SUPPRESS, help="Path to experiment JSON config")


def _add_telemetry_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--log-level",
        choices=["quiet", "default", "realtime", "debug"],
        default=None,
        help="Console telemetry level for this run",
    )
    parser.add_argument("--trace", action="store_true", help="Write debug trace spans for major calls")
    parser.add_argument("--quiet", action="store_true", help="Suppress console telemetry for this run")


def cmd_check_setup(args: argparse.Namespace, config: dict[str, Any]) -> int:
    require_eeg = None
    if args.require_eeg:
        require_eeg = True
    if args.allow_missing_eeg:
        require_eeg = False
    results = run_preflight(config, lsl_wait=args.lsl_wait, require_eeg=require_eeg)
    for result in results:
        print(f"{result.status.upper():5} {result.name:18} {result.detail}")
    if args.save:
        write_preflight_report(results, args.save)
        print(f"saved preflight report: {args.save}")
    return 0 if all(result.ok for result in results) else 1


# Compatibility for callers that imported the old command handler.
cmd_doctor = cmd_check_setup


def cmd_list_tasks(args: argparse.Namespace, config: dict[str, Any]) -> int:
    for spec in list_task_specs():
        loop = "closed-loop-ready" if spec.closed_loop_ready else "offline-first"
        print(f"{spec.name:10} {spec.status:12} {loop:18} {spec.display_name}")
        print(f"  {spec.description}")
        for note in spec.notes:
            print(f"  note: {note}")
    return 0


def cmd_init_session(args: argparse.Namespace, config: dict[str, Any]) -> int:
    task = args.task or config.get("experiment", {}).get("task", "pvt")
    get_task_spec(task)
    paths = create_session(config, task=task, participant_id=args.participant, root=args.root)
    print(paths.root)
    return 0


def cmd_run_task(args: argparse.Namespace, config: dict[str, Any]) -> int:
    task = args.task or config.get("experiment", {}).get("task", "pvt")
    get_task_spec(task)
    result = make_task_component(task, config, task_mode=args.mode, trials=args.trials, participant_id=args.participant).run()
    print(json.dumps({"session_dir": str(result.session_dir), "summary": result.summary}, indent=2, sort_keys=True))
    return 0


def cmd_run_forward(args: argparse.Namespace, config: dict[str, Any]) -> int:
    task = args.task or config.get("experiment", {}).get("task", "pvt")
    get_task_spec(task)
    require_eeg = None
    if args.require_eeg:
        require_eeg = True
    if args.allow_missing_eeg:
        require_eeg = False
    result = ForwardExperimentRunner(
        config,
        task_name=task,
        task_mode=args.task_mode,
        participant_id=args.participant,
        trials=args.trials,
        record_eeg=not args.skip_eeg,
        require_eeg=require_eeg,
        calibration_suite=args.calibration_suite,
    ).run()
    payload = result.as_dict()
    compact = {
        "session_dir": payload["session_dir"],
        "summary_file": payload["summary_file"],
        "task": payload["task"],
        "eeg": payload["eeg"],
        "analysis_report": None if payload["analysis"] is None else str(Path(payload["analysis"]["session_dir"]) / "reports" / "summary.json"),
        "calibration": None if payload.get("calibration") is None else {
            "suite": payload["calibration"].get("suite"),
            "status": payload["calibration"].get("status"),
            "online_band": payload["calibration"].get("online_band"),
            "summary_file": payload["calibration"].get("files", {}).get("summary_json"),
        },
        "processes": None if payload["processes"] is None else {
            "status": payload["processes"].get("status"),
            "manager_summary_file": payload["processes"].get("manager_summary_file"),
            "process_statuses": {
                name: status.get("status")
                for name, status in payload["processes"].get("processes", {}).items()
            },
        },
    }
    print(json.dumps(compact, indent=2, sort_keys=True))
    return 0 if not any(check.status == "fail" for check in result.preflight) else 1


def cmd_run_realtime(args: argparse.Namespace, config: dict[str, Any]) -> int:
    import numpy as np

    sample_rate = args.sample_rate or float(config.get("hardware", {}).get("eeg", {}).get("expected_sample_rate_hz", 500))
    seconds = float(config.get("realtime", {}).get("window_seconds", 2.0))
    channels = int(config.get("hardware", {}).get("eeg", {}).get("expected_channel_counts", [32])[-1])
    samples = int(sample_rate * seconds)
    data = np.random.normal(0, 1, size=(samples, channels))
    result = ClosedLoopController(config, sample_rate_hz=sample_rate).process_window(data)
    print(
        json.dumps(
            {
                "prediction_label": result.prediction_label,
                "prediction_score": result.prediction_score,
                "prediction": result.prediction.to_payload(),
                "actions": [action.to_payload() for action in result.actions],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_replay_realtime(args: argparse.Namespace, config: dict[str, Any]) -> int:
    replay = replay_realtime_session(args.session_dir, tolerance=float(args.tolerance))
    behavior = run_feature_behavior_analysis(
        args.session_dir,
        config.get("analysis", {}).get("inhibition8", {}),
    )
    payload = {"replay": replay, "exploratory_behavior": behavior}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if replay.get("status") == "pass" else 1


def cmd_simulate_eeg(args: argparse.Namespace, config: dict[str, Any]) -> int:
    from eegle.devices.simulated_lsl import run_simulated_eeg

    print(
        f"Streaming simulated EEG LSL outlet '{args.name}' "
        f"({args.channels} ch @ {args.sample_rate} Hz) for {args.duration} s"
    )
    run_simulated_eeg(
        name=args.name,
        channel_count=args.channels,
        sample_rate_hz=args.sample_rate,
        duration_seconds=args.duration,
    )
    return 0


def cmd_analyze(args: argparse.Namespace, config: dict[str, Any]) -> int:
    summary = analyze_session(Path(args.session_dir))
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def cmd_report_html(args: argparse.Namespace, config: dict[str, Any]) -> int:
    summary = generate_experiment_html_report(
        Path(args.session_dir),
        config,
        output=args.output,
        max_raw_points=args.max_raw_points,
        max_alpha_points=args.max_alpha_points,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") == "ok" else 1


def cmd_reanalyze_alpha(args: argparse.Namespace, config: dict[str, Any]) -> int:
    summary = rerun_alpha_analysis(
        Path(args.session_dir),
        config,
        update_parameters=not bool(args.no_update_parameters),
        write_html=not bool(args.no_html),
        max_raw_points=int(args.max_raw_points),
        max_alpha_points=int(args.max_alpha_points),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") == "ok" else 1


def cmd_analyze_erp(args: argparse.Namespace, config: dict[str, Any]) -> int:
    erp_config = dict(config.get("analysis", {}).get("erp", {}))
    if args.tmin is not None:
        erp_config["tmin_seconds"] = args.tmin
    if args.tmax is not None:
        erp_config["tmax_seconds"] = args.tmax
    if args.p300_min is not None or args.p300_max is not None:
        current = list(erp_config.get("p300_window_seconds", [0.25, 0.6]))
        if args.p300_min is not None:
            current[0] = args.p300_min
        if args.p300_max is not None:
            current[1] = args.p300_max
        erp_config["p300_window_seconds"] = current
    summary = run_erp_analysis(Path(args.session_dir), erp_config)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") in {"ok", "missing", "disabled", "no_epochs"} else 1


def cmd_extract_epochs(args: argparse.Namespace, config: dict[str, Any]) -> int:
    realtime = dict(config.get("realtime", {}))
    epoching = dict(realtime.get("epoching", {}))
    if args.tmin is not None:
        epoching["tmin_seconds"] = args.tmin
    if args.tmax is not None:
        epoching["tmax_seconds"] = args.tmax
    if args.marker_prefix is not None:
        epoching["marker_prefix"] = args.marker_prefix
    realtime["epoching"] = epoching
    effective_config = dict(config)
    effective_config["realtime"] = realtime
    manifest = extract_epochs_for_session(
        Path(args.session_dir),
        effective_config,
        source=args.source,
        output_dir=args.output_dir,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest.get("status") == "ok" and manifest.get("raw_file_unchanged") else 1


def cmd_train_model(args: argparse.Namespace, config: dict[str, Any]) -> int:
    model_config = dict(config.get("realtime", {}).get("model", {}))
    kind = args.kind or str(model_config.get("kind", "sklearn_xdawn_lda"))
    try:
        manifest = train_epoch_model(kind, Path(args.epochs_npz), Path(args.output), model_config)
    except Exception as exc:
        manifest = {
            "status": "failed",
            "model_kind": kind,
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest.get("status") == "ok" else 1


def cmd_evaluate_model(args: argparse.Namespace, config: dict[str, Any]) -> int:
    summary = evaluate_classifier_session(args.session_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") == "ok" else 1


def cmd_replay_classifier(args: argparse.Namespace, config: dict[str, Any]) -> int:
    summary = replay_classifier_session(args.session_dir)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
