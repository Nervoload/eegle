"""Observe-only attention-lapse risk workflow over the classify8 runtime."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from eegle.analysis.classification import evaluate_classifier_session, replay_classifier_session
from eegle.analysis.html_summary import generate_experiment_html_report
from eegle.analysis.reports import analyze_session
from eegle.config import load_config
from eegle.hardware.capabilities import check_training_ready, missing_training_packages
from eegle.ml.registry import list_model_kinds, resolve_model_kind
from eegle.ml.targets import SUPPORTED_TARGETS
from eegle.pipelines import classify8
from eegle.protocols import attention_lapse_protocol, write_protocol
from eegle.realtime.models import train_epoch_model


DEFAULT_CONFIG = Path("configs/forward_attention_lapse_go_nogo8.json")
DEFAULT_MODEL_KINDS = (
    "causal_bandpower_logreg",
    "riemann_tangent_logreg",
    "torch_eegnet",
    "foundation_head_logreg",
    "foundation_prototype",
)
MODEL_KINDS = tuple(list_model_kinds(include_aliases=True))
TRAINABLE_MODEL_KINDS = tuple(list_model_kinds(include_aliases=True, trainable=True))


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
        elif args.command == "protocol":
            result = protocol(args)
        else:
            raise ValueError(f"unknown attention8 command {args.command}")
    except Exception as exc:
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") in {"ok", "complete", "degraded"} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="attention8", description="Observe-only attention-lapse realtime system-test workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="Collect Go/No-go EEG for attention-lapse calibration")
    _run_arguments(collect_parser, trials=240)

    train_parser = subparsers.add_parser("train", help="Train attention-lapse risk model bundles")
    train_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    train_parser.add_argument("--session-dir", action="append", required=True)
    train_parser.add_argument("--kind", action="append", choices=TRAINABLE_MODEL_KINDS, help="Train one or more model kinds")
    train_parser.add_argument("--target", choices=SUPPORTED_TARGETS, default="attention_lapse_binary")
    train_parser.add_argument("--attention-lapse-label", default="slow_go_rt")
    train_parser.add_argument("--slow-rt-quantile", type=float, default=0.8)
    train_parser.add_argument("--support-trials", type=int, default=50)
    train_parser.add_argument("--output-dir", default=None)
    train_parser.add_argument("--check-ready", action="store_true", help="Only report training dependency readiness")

    online_parser = subparsers.add_parser("online", help="Run observe-only attention-lapse primary plus shadows")
    _run_arguments(online_parser, trials=160)
    online_parser.add_argument("--model-dir", required=True, help="Directory containing attention8 model-kind bundle directories")
    online_parser.add_argument("--primary", choices=MODEL_KINDS, default="causal_bandpower_logreg")
    online_parser.add_argument("--shadow", action="append", choices=MODEL_KINDS, default=[])
    online_parser.add_argument("--no-dashboard", action="store_true")
    online_parser.add_argument("--enable-adaptation", action="store_true", help="Opt in to delayed-label online model adaptation")
    online_parser.add_argument("--adapt-shadows", action="store_true", help="Also adapt shadow models; primary-only is the default")
    online_parser.add_argument(
        "--adaptation-label-mode",
        choices=["slow_go_rt", "go_only_slow_rt", "composite_lapse", "omission_error", "commission_error"],
        default=None,
    )
    online_parser.add_argument("--min-correct-go-rts-for-threshold", type=int, default=None)
    online_parser.add_argument("--adaptation-warmup-trials", type=int, default=None)

    evaluate_parser = subparsers.add_parser("evaluate", help="Replay, score, and report an attention-lapse session")
    evaluate_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    evaluate_parser.add_argument("--session-dir", required=True)

    protocol_parser = subparsers.add_parser("protocol", help="Print or write the attention-lapse protocol declaration")
    protocol_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    protocol_parser.add_argument("--output", default=None)
    return parser


def _run_arguments(parser: argparse.ArgumentParser, *, trials: int) -> None:
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--participant", default=None)
    parser.add_argument("--trials", type=int, default=trials)
    parser.add_argument("--task-mode", choices=["psychopy", "dry-run"], default="psychopy")
    parser.add_argument("--skip-eeg", action="store_true")
    parser.add_argument("--allow-missing-eeg", action="store_true")


def collect(args: argparse.Namespace) -> dict[str, Any]:
    result = classify8.collect(args)
    root = _write_session_protocol(result)
    return {**result, "workflow": "attention8.collect", "protocol_file": None if root is None else str(root / "protocol.json")}


def train(args: argparse.Namespace) -> dict[str, Any]:
    sessions = [Path(value).expanduser().resolve() for value in args.session_dir]
    session = sessions[0]
    epochs = [value / "realtime" / "epochs" / "epochs.npz" for value in sessions]
    config = load_config(session / "parameters.json") if (session / "parameters.json").exists() else load_config(args.config)
    output_root = Path(args.output_dir).expanduser().resolve() if args.output_dir else session / "models" / "attention8"
    kinds = [resolve_model_kind(kind) for kind in (args.kind or list(DEFAULT_MODEL_KINDS))]
    readiness = check_training_ready(kinds, required=True)
    if bool(args.check_ready):
        return {
            "status": "ok" if readiness.status == "ok" else "failed",
            "workflow": "attention8.train",
            "session_dirs": [str(value) for value in sessions],
            "training_ready": readiness.__dict__,
        }

    protocol_payload = attention_lapse_protocol().payload()
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
        kind_config = _attention_model_config(
            config,
            kind=kind,
            target=args.target,
            sessions=sessions,
            support_trials=args.support_trials,
            slow_rt_quantile=args.slow_rt_quantile,
            label=args.attention_lapse_label,
        )
        try:
            results[kind] = train_epoch_model(kind, epochs, output_root / kind, kind_config)
        except Exception as exc:
            results[kind] = {"status": "failed", "model_kind": kind, "error": f"{type(exc).__name__}: {exc}"}

    complete = [value for value in results.values() if value.get("status") == "ok"]
    if complete:
        write_protocol(attention_lapse_protocol(), output_root / "protocol.json")
    return {
        "status": "ok" if len(complete) == len(results) else ("degraded" if complete else "failed"),
        "workflow": "attention8.train",
        "session_dir": str(session),
        "session_dirs": [str(value) for value in sessions],
        "target": args.target,
        "attention_lapse_label": args.attention_lapse_label,
        "slow_rt_quantile": float(args.slow_rt_quantile),
        "support_trials": int(args.support_trials),
        "model_dir": str(output_root),
        "protocol": protocol_payload,
        "training_ready": readiness.__dict__,
        "models": results,
    }


def online(args: argparse.Namespace) -> dict[str, Any]:
    result = classify8.online(args)
    root = _write_session_protocol(result)
    return {**result, "workflow": "attention8.online", "protocol_file": None if root is None else str(root / "protocol.json")}


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.session_dir).expanduser().resolve()
    replay = replay_classifier_session(root)
    classification = evaluate_classifier_session(root)
    analysis = analyze_session(root)
    html = generate_experiment_html_report(root)
    status = "ok" if classification.get("status") == "ok" and replay.get("status") == "pass" else "degraded"
    return {
        "status": status,
        "workflow": "attention8.evaluate",
        "session_dir": str(root),
        "protocol": attention_lapse_protocol().payload(),
        "replay": replay,
        "classification": classification,
        "analysis": analysis,
        "html": html,
    }


def protocol(args: argparse.Namespace) -> dict[str, Any]:
    declaration = attention_lapse_protocol()
    if args.output:
        write_protocol(declaration, args.output)
    return {
        "status": "ok",
        "workflow": "attention8.protocol",
        "protocol": declaration.payload(),
        "output": None if args.output is None else str(Path(args.output).expanduser().resolve()),
    }


def _attention_model_config(
    config: dict[str, Any],
    *,
    kind: str,
    target: str,
    sessions: list[Path],
    support_trials: int,
    slow_rt_quantile: float,
    label: str,
) -> dict[str, Any]:
    realtime = dict(config.get("realtime", {}))
    epoching = dict(realtime.get("epoching", {}))
    model = copy.deepcopy(realtime.get("model", {}))
    calibration = {**dict(realtime.get("calibration", {})), **dict(model.get("calibration", {}))}
    model.update(
        {
            "kind": kind,
            "target": target,
            "session_dirs": [str(value) for value in sessions],
            "attention_lapse_label": label,
            "slow_rt_quantile": float(slow_rt_quantile),
            "attention_lapse_threshold": float(model.get("attention_lapse_threshold", 0.5)),
            "support_trials": max(0, int(support_trials)),
            "calibration": {
                **calibration,
                "support_trials": max(0, int(support_trials)),
            },
            "epoch_data_source": str(epoching.get("data_source", "raw")),
            "prediction_horizon": str(model.get("prediction_horizon", "same_trial_response")),
            "prediction_window_seconds": [
                float(epoching.get("tmin_seconds", -2.0)),
                float(epoching.get("tmax_seconds", 0.0)),
            ],
            "preprocessing": dict(realtime.get("preprocessing", {})),
        }
    )
    return model


def _write_session_protocol(result: dict[str, Any]) -> Path | None:
    session_dir = result.get("session_dir")
    if not session_dir:
        return None
    root = Path(str(session_dir)).expanduser().resolve()
    if not root.exists():
        return None
    write_protocol(attention_lapse_protocol(), root / "protocol.json")
    return root


if __name__ == "__main__":
    raise SystemExit(main())
