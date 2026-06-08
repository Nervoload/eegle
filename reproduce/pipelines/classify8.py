"""Participant-specific Go/No-go EEG condition-classification workflow."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from reproduce.analysis.classification import evaluate_classifier_session, replay_classifier_session
from reproduce.analysis.html_summary import generate_experiment_html_report
from reproduce.analysis.reports import analyze_session
from reproduce.config import load_config
from reproduce.experiment import ForwardExperimentRunner
from reproduce.realtime.epoching import extract_epochs_for_session
from reproduce.realtime.models import train_epoch_model


DEFAULT_CONFIG = Path("configs/forward_go_nogo_classifier8.json")
MODEL_KINDS = ("erp_roi_logreg", "pyriemann_erp_cov", "torch_eegnet")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            result = collect(args)
        elif args.command == "train":
            result = train(args)
        elif args.command == "online":
            result = online(args)
        elif args.command == "evaluate":
            result = evaluate(args)
        else:
            raise ValueError(f"unknown classify8 command {args.command}")
    except Exception as exc:
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"ok", "complete", "degraded"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="classify8", description="GO/NO-GO EEG condition-classification workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect", help="Collect participant calibration EEG and export epochs")
    _run_arguments(collect_parser)
    train_parser = subparsers.add_parser("train", help="Train frozen participant model bundles")
    train_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    train_parser.add_argument("--session-dir", required=True)
    train_parser.add_argument("--kind", action="append", choices=MODEL_KINDS, help="Train one or more kinds; defaults to all")
    train_parser.add_argument("--output-dir", default=None)
    online_parser = subparsers.add_parser("online", help="Run observe-only primary plus shadow models")
    _run_arguments(online_parser)
    online_parser.add_argument("--model-dir", required=True, help="Directory containing model-kind bundle directories")
    online_parser.add_argument("--primary", choices=MODEL_KINDS, default="erp_roi_logreg")
    online_parser.add_argument("--shadow", action="append", choices=MODEL_KINDS, default=[])
    online_parser.add_argument("--no-dashboard", action="store_true")
    evaluate_parser = subparsers.add_parser("evaluate", help="Replay, score, and report an online classifier session")
    evaluate_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    evaluate_parser.add_argument("--session-dir", required=True)
    return parser


def _run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--participant", default=None)
    parser.add_argument("--trials", type=int, default=240)
    parser.add_argument("--task-mode", choices=["psychopy", "dry-run"], default="psychopy")
    parser.add_argument("--skip-eeg", action="store_true")
    parser.add_argument("--allow-missing-eeg", action="store_true")


def collect(args: argparse.Namespace) -> dict[str, Any]:
    config = _classifier_config(load_config(args.config))
    config["realtime"]["inference"]["enabled"] = False
    config["realtime"]["dashboard"]["enabled"] = False
    config["processes"]["dashboard"]["enabled"] = False
    result = _run_forward(config, args)
    session = Path(result.session_dir)
    epochs = None
    if (session / "raw" / "eeg.csv").exists():
        epochs = extract_epochs_for_session(session, load_config(session / "parameters.json"), source="auto")
    return {
        "status": result.as_dict()["status"],
        "workflow": "classify8.collect",
        "session_dir": str(session),
        "epochs": epochs,
        "forward": result.as_dict(),
    }


def train(args: argparse.Namespace) -> dict[str, Any]:
    session = Path(args.session_dir).expanduser().resolve()
    epochs = session / "realtime" / "epochs" / "epochs.npz"
    config = load_config(session / "parameters.json") if (session / "parameters.json").exists() else load_config(args.config)
    output_root = Path(args.output_dir).expanduser().resolve() if args.output_dir else session / "models" / "classifier"
    results = {}
    for kind in args.kind or list(MODEL_KINDS):
        kind_config = dict(config.get("realtime", {}).get("model", {}))
        kind_config["kind"] = kind
        try:
            results[kind] = train_epoch_model(kind, epochs, output_root / kind, kind_config)
        except Exception as exc:
            results[kind] = {"status": "failed", "model_kind": kind, "error": f"{type(exc).__name__}: {exc}"}
    complete = [value for value in results.values() if value.get("status") == "ok"]
    return {
        "status": "ok" if len(complete) == len(results) else ("degraded" if complete else "failed"),
        "workflow": "classify8.train",
        "session_dir": str(session),
        "model_dir": str(output_root),
        "models": results,
    }


def online(args: argparse.Namespace) -> dict[str, Any]:
    config = _classifier_config(load_config(args.config))
    model_root = Path(args.model_dir).expanduser().resolve()
    config["realtime"]["inference"]["enabled"] = True
    config["realtime"]["model"] = {
        **dict(config["realtime"].get("model", {})),
        "kind": args.primary,
        "bundle_path": str(model_root / args.primary),
    }
    shadows = list(dict.fromkeys(args.shadow))
    config["realtime"]["shadow_models"] = [
        {"id": f"shadow-{kind}", "kind": kind, "bundle_path": str(model_root / kind)}
        for kind in shadows
        if kind != args.primary
    ]
    config["processes"]["realtime_processor"]["model"] = args.primary
    config["realtime"]["dashboard"]["enabled"] = not bool(args.no_dashboard)
    config["processes"]["dashboard"]["enabled"] = not bool(args.no_dashboard)
    result = _run_forward(config, args)
    return {
        "status": result.as_dict()["status"],
        "workflow": "classify8.online",
        "session_dir": str(result.session_dir),
        "primary": args.primary,
        "shadows": shadows,
        "dashboard_url": None if args.no_dashboard else f"http://{config['realtime']['dashboard']['host']}:{config['realtime']['dashboard']['port']}",
        "forward": result.as_dict(),
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.session_dir).expanduser().resolve()
    replay = replay_classifier_session(root)
    classification = evaluate_classifier_session(root)
    analysis = analyze_session(root)
    html = generate_experiment_html_report(root)
    status = "ok" if classification.get("status") == "ok" and replay.get("status") == "pass" else "degraded"
    return {
        "status": status,
        "workflow": "classify8.evaluate",
        "session_dir": str(root),
        "replay": replay,
        "classification": classification,
        "analysis": analysis,
        "html": html,
    }


def _classifier_config(config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    result.setdefault("realtime", {}).setdefault("inference", {})["enabled"] = True
    result["realtime"].setdefault("classifier", {})["enabled"] = True
    result["realtime"].setdefault("capture", {})["enabled"] = True
    result["realtime"].setdefault("event_features", {})["enabled"] = False
    result["realtime"].setdefault("decision_policy", {}).update(
        {"kind": "observe_only", "allow_task_adaptation": False, "actions": ["observe_only"]}
    )
    result["realtime"].setdefault("feedback", {}).update(
        {"mode": "observe_only", "allow_task_adaptation": False, "allow_stimulation": False}
    )
    result.setdefault("processes", {}).setdefault("feedback", {}).update({"enabled": False, "backend": "disabled"})
    result["processes"].setdefault("realtime_processor", {}).update({"enabled": True, "backend": "lsl"})
    result["experiment"].setdefault("components", {})["realtime_processor"] = "lsl"
    return result


def _run_forward(config: dict[str, Any], args: argparse.Namespace) -> Any:
    effective = copy.deepcopy(config)
    if bool(args.skip_eeg):
        effective["processes"]["realtime_processor"]["enabled"] = False
        effective["processes"]["dashboard"]["enabled"] = False
    return ForwardExperimentRunner(
        effective,
        task_name="go_nogo",
        task_mode=args.task_mode,
        participant_id=args.participant,
        trials=args.trials,
        record_eeg=not bool(args.skip_eeg),
        require_eeg=not bool(args.skip_eeg) and not bool(args.allow_missing_eeg),
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
