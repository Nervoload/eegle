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
from reproduce.hardware.capabilities import check_training_ready, missing_training_packages
from reproduce.realtime.classification import load_model_bundle
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
        elif args.command == "demo":
            result = demo(args)
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
    train_parser.add_argument("--check-ready", action="store_true", help="Only report training dependency readiness")
    online_parser = subparsers.add_parser("online", help="Run observe-only primary plus shadow models")
    _run_arguments(online_parser)
    online_parser.add_argument("--model-dir", required=True, help="Directory containing model-kind bundle directories")
    online_parser.add_argument("--primary", choices=MODEL_KINDS, default="erp_roi_logreg")
    online_parser.add_argument("--shadow", action="append", choices=MODEL_KINDS, default=[])
    online_parser.add_argument("--no-dashboard", action="store_true")
    demo_parser = subparsers.add_parser("demo", help="Run a transparent marker-driven classroom dashboard demo")
    demo_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    demo_parser.add_argument("--participant", default="classroom-demo")
    demo_parser.add_argument("--trials", type=int, default=40)
    demo_parser.add_argument("--record-eeg", action="store_true", help="Also record EEG; demo guesses still come from markers")
    demo_parser.add_argument("--prediction-delay-seconds", type=float, default=1.2)
    demo_parser.add_argument("--error-rate", type=float, default=0.1)
    demo_parser.add_argument("--seed", type=int, default=42)
    demo_parser.add_argument("--port", type=int, default=8765)
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
    kinds = list(args.kind or list(MODEL_KINDS))
    readiness = check_training_ready(kinds, required=True)
    if bool(args.check_ready):
        return {
            "status": "ok" if readiness.status == "ok" else "failed",
            "workflow": "classify8.train",
            "session_dir": str(session),
            "training_ready": readiness.__dict__,
        }
    results = {}
    for kind in kinds:
        missing = missing_training_packages(kind)
        if missing:
            results[kind] = {
                "status": "failed",
                "model_kind": kind,
                "reason": "missing_training_dependencies",
                "missing_packages": missing,
                "error": "missing training packages: " + ", ".join(missing),
            }
            continue
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
        "training_ready": readiness.__dict__,
        "models": results,
    }


def online(args: argparse.Namespace) -> dict[str, Any]:
    config = _classifier_config(load_config(args.config))
    model_root = Path(args.model_dir).expanduser().resolve()
    shadows = list(dict.fromkeys(args.shadow))
    bundle_paths = _validate_online_model_bundles(
        model_root,
        [args.primary, *(kind for kind in shadows if kind != args.primary)],
    )
    config["realtime"]["inference"]["enabled"] = True
    config["realtime"]["model"] = {
        **dict(config["realtime"].get("model", {})),
        "kind": args.primary,
        "bundle_path": bundle_paths[args.primary],
    }
    config["realtime"]["shadow_models"] = [
        {"id": f"shadow-{kind}", "kind": kind, "bundle_path": bundle_paths[kind]}
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


def demo(args: argparse.Namespace) -> dict[str, Any]:
    config = copy.deepcopy(load_config(args.config))
    config.setdefault("realtime", {})["enabled"] = True
    config["realtime"].setdefault("inference", {})["enabled"] = False
    config["realtime"].setdefault("classifier", {})["enabled"] = False
    config["realtime"].setdefault("dashboard", {}).update(
        {
            "enabled": True,
            "host": "127.0.0.1",
            "port": int(args.port),
            "demo": {
                "enabled": True,
                "prediction_delay_seconds": max(0.0, float(args.prediction_delay_seconds)),
                "error_rate": min(1.0, max(0.0, float(args.error_rate))),
                "seed": int(args.seed),
            },
        }
    )
    config.setdefault("hardware", {}).setdefault("markers", {})["required_for_realtime"] = True
    config.setdefault("processes", {}).setdefault("realtime_processor", {}).update(
        {"enabled": False, "backend": "disabled"}
    )
    config["processes"].setdefault("dashboard", {}).update(
        {"enabled": True, "backend": "http", "host": "127.0.0.1", "port": int(args.port)}
    )
    config["processes"].setdefault("offline_analyzer", {})["enabled"] = False
    config.setdefault("experiment", {}).setdefault("components", {})["realtime_processor"] = "disabled"
    result = ForwardExperimentRunner(
        config,
        task_name="go_nogo",
        task_mode="psychopy",
        participant_id=args.participant,
        trials=args.trials,
        record_eeg=bool(args.record_eeg),
        require_eeg=bool(args.record_eeg),
    ).run()
    return {
        "status": result.as_dict()["status"],
        "workflow": "classify8.demo",
        "session_dir": str(result.session_dir),
        "dashboard_url": f"http://127.0.0.1:{int(args.port)}",
        "prediction_delay_seconds": max(0.0, float(args.prediction_delay_seconds)),
        "error_rate": min(1.0, max(0.0, float(args.error_rate))),
        "record_eeg": bool(args.record_eeg),
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


def _validate_online_model_bundles(model_root: Path, kinds: list[str]) -> dict[str, str]:
    if not model_root.is_dir():
        raise ValueError(
            f"--model-dir does not exist or is not a directory: {model_root}. "
            "Point it at the directory containing the model-kind bundle directories."
        )

    validated = {}
    for kind in kinds:
        bundle_path = model_root / kind
        manifest_path = bundle_path / "manifest.json"
        if not manifest_path.is_file():
            raise ValueError(
                f"model bundle for '{kind}' was not found at {bundle_path}. "
                f"Expected {manifest_path}; --model-dir must be the parent directory containing '{kind}'."
            )
        try:
            bundle = load_model_bundle(bundle_path)
        except Exception as exc:
            raise ValueError(
                f"model bundle for '{kind}' is invalid at {bundle_path}: {type(exc).__name__}: {exc}"
            ) from exc
        if bundle.get("kind") != kind:
            raise ValueError(
                f"model bundle at {bundle_path} declares kind '{bundle.get('kind')}', expected '{kind}'"
            )
        validated[kind] = str(Path(bundle["bundle_dir"]).resolve())
    return validated


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
