"""Observe-only attention-lapse risk workflow over the classify8 runtime."""

from __future__ import annotations

import argparse
import copy
import csv
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
DEFAULT_COMPARISON_METHODS = ("log-reg", "riemann", "lora", "film")
COMPARISON_METHOD_PROFILES: dict[str, dict[str, str]] = {
    "log-reg": {
        "method": "log-reg",
        "model_kind": "causal_bandpower_logreg",
        "label": "Causal bandpower logistic regression",
        "implementation_note": "Implemented directly as causal_bandpower_logreg.",
    },
    "riemann": {
        "method": "riemann",
        "model_kind": "riemann_tangent_logreg",
        "label": "Riemannian tangent-space logistic regression",
        "implementation_note": "Implemented directly as riemann_tangent_logreg.",
    },
    "lora": {
        "method": "lora",
        "model_kind": "foundation_head_logreg",
        "label": "LoRA-style foundation-head comparison profile",
        "implementation_note": (
            "Comparison proxy only: EEGle currently fits a frozen-embedding logistic head, "
            "not LoRA parameter-efficient fine-tuning."
        ),
    },
    "film": {
        "method": "film",
        "model_kind": "foundation_prototype",
        "label": "FiLM-style foundation-prototype comparison profile",
        "implementation_note": (
            "Comparison proxy only: EEGle currently updates frozen-embedding prototypes, "
            "not FiLM conditioning layers."
        ),
    },
    "torch_eegnet": {
        "method": "torch_eegnet",
        "model_kind": "torch_eegnet",
        "label": "Torch EEGNet",
        "implementation_note": "Implemented directly as torch_eegnet when the torch extra is installed.",
    },
}
COMPARISON_METHOD_ALIASES = {
    "logreg": "log-reg",
    "log_reg": "log-reg",
    "logistic": "log-reg",
    "logistic-regression": "log-reg",
    "causal_bandpower_logreg": "log-reg",
    "riemannian": "riemann",
    "riemann_tangent": "riemann",
    "riemann_tangent_logreg": "riemann",
    "foundation_lora": "lora",
    "foundation_head": "lora",
    "foundation_head_logreg": "lora",
    "foundation_film": "film",
    "foundation_prototype": "film",
    "eegnet": "torch_eegnet",
    "cnn_eegnet": "torch_eegnet",
}
COMPARISON_METHOD_CHOICES = tuple(sorted({*COMPARISON_METHOD_PROFILES, *COMPARISON_METHOD_ALIASES}))
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
        elif args.command == "compare":
            result = compare(args)
        elif args.command == "evaluate":
            result = evaluate(args)
        elif args.command == "readiness":
            result = readiness(args)
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

    compare_parser = subparsers.add_parser("compare", help="Offline compare attention-lapse classification methods")
    compare_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    compare_parser.add_argument("--session-dir", action="append", required=True, help="Calibration/session directory with realtime/epochs/epochs.npz")
    compare_parser.add_argument("--online-session-dir", default=None, help="Optional online session to use as the live-model reference")
    compare_parser.add_argument(
        "--method",
        action="append",
        choices=COMPARISON_METHOD_CHOICES,
        help="Offline method/profile to compare; defaults to log-reg, riemann, lora, and film",
    )
    compare_parser.add_argument("--target", choices=SUPPORTED_TARGETS, default="attention_lapse_binary")
    compare_parser.add_argument("--attention-lapse-label", default="slow_go_rt")
    compare_parser.add_argument("--slow-rt-quantile", type=float, default=0.8)
    compare_parser.add_argument("--support-trials", type=int, default=50)
    compare_parser.add_argument("--output-dir", default=None)
    compare_parser.add_argument("--check-ready", action="store_true", help="Only report comparison dependency readiness")

    evaluate_parser = subparsers.add_parser("evaluate", help="Replay, score, and report an attention-lapse session")
    evaluate_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    evaluate_parser.add_argument("--session-dir", required=True)

    readiness_parser = subparsers.add_parser("readiness", help="Print the two-subject attention8 operator checklist")
    readiness_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    readiness_parser.add_argument("--participant", action="append", default=[])
    readiness_parser.add_argument("--expected-subjects", type=int, default=2)
    readiness_parser.add_argument("--calibration-trials", type=int, default=240)
    readiness_parser.add_argument("--online-trials", type=int, default=160)

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


def compare(args: argparse.Namespace) -> dict[str, Any]:
    sessions = [Path(value).expanduser().resolve() for value in args.session_dir]
    session = sessions[0]
    epochs = [value / "realtime" / "epochs" / "epochs.npz" for value in sessions]
    config = load_config(session / "parameters.json") if (session / "parameters.json").exists() else load_config(args.config)
    methods = _comparison_methods(args.method)
    kinds = [str(profile["model_kind"]) for profile in methods]
    readiness = check_training_ready(kinds, required=False)
    output_root = _comparison_output_root(args, session)
    online_reference = _online_reference(args.online_session_dir)
    if bool(args.check_ready):
        return {
            "status": "ok" if readiness.status == "ok" else "degraded",
            "workflow": "attention8.compare",
            "session_dirs": [str(value) for value in sessions],
            "methods": methods,
            "training_ready": readiness.__dict__,
            "online_reference": online_reference,
        }

    results: dict[str, dict[str, Any]] = {}
    for profile in methods:
        method = str(profile["method"])
        kind = str(profile["model_kind"])
        missing = missing_training_packages(kind)
        if missing:
            results[method] = {
                "status": "failed",
                "method": method,
                "model_kind": kind,
                "comparison_profile": profile,
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
        kind_config["comparison_profile"] = dict(profile)
        try:
            trained = train_epoch_model(kind, epochs, output_root / "bundles" / method, kind_config)
            metrics = _preferred_metrics(trained)
            results[method] = {
                "status": "ok",
                "method": method,
                "model_kind": kind,
                "comparison_profile": profile,
                "bundle_path": trained.get("bundle_path"),
                "bundle_hash": trained.get("bundle_hash"),
                "training_epochs": trained.get("training_epochs"),
                "query_epochs": trained.get("query_epochs"),
                "calibration": trained.get("calibration"),
                "metrics": metrics,
                "raw_training_result": trained,
                "comparison_to_online": _metric_delta(metrics, online_reference.get("metrics")),
            }
        except Exception as exc:
            results[method] = {
                "status": "failed",
                "method": method,
                "model_kind": kind,
                "comparison_profile": profile,
                "error": f"{type(exc).__name__}: {exc}",
            }

    complete = [value for value in results.values() if value.get("status") == "ok"]
    status = "ok" if len(complete) == len(results) else ("degraded" if complete else "failed")
    summary = {
        "schema_version": 1,
        "status": status,
        "workflow": "attention8.compare",
        "session_dir": str(session),
        "session_dirs": [str(value) for value in sessions],
        "target": args.target,
        "attention_lapse_label": args.attention_lapse_label,
        "slow_rt_quantile": float(args.slow_rt_quantile),
        "support_trials": int(args.support_trials),
        "online_reference": online_reference,
        "training_ready": readiness.__dict__,
        "methods": results,
    }
    files = _write_comparison_outputs(output_root, summary)
    return {**summary, "files": files}


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


def readiness(args: argparse.Namespace) -> dict[str, Any]:
    expected = max(1, int(args.expected_subjects))
    participants = [str(value) for value in args.participant if str(value).strip()]
    if not participants:
        participants = [f"sub-{index:03d}" for index in range(1, expected + 1)]
    readiness_check = check_training_ready(DEFAULT_MODEL_KINDS, required=False)
    commands = [
        {
            "participant": participant,
            "collect": f"attention8 collect --participant {participant} --trials {int(args.calibration_trials)}",
            "train": "attention8 train --session-dir <calibration-session> --support-trials 50",
            "compare": "attention8 compare --session-dir <calibration-session> --online-session-dir <online-session>",
            "online": (
                f"attention8 online --participant {participant} "
                "--model-dir <calibration-session>/models/attention8 "
                f"--primary causal_bandpower_logreg --shadow riemann_tangent_logreg "
                f"--shadow foundation_head_logreg --shadow foundation_prototype --trials {int(args.online_trials)}"
            ),
            "evaluate": "attention8 evaluate --session-dir <online-session>",
        }
        for participant in participants
    ]
    status = "ok" if len(participants) >= expected and readiness_check.status == "ok" else "degraded"
    return {
        "status": status,
        "workflow": "attention8.readiness",
        "expected_subjects": expected,
        "participants": participants,
        "protocol": attention_lapse_protocol().payload(),
        "training_ready": readiness_check.__dict__,
        "operator_checks": [
            "Use one calibration and one online session per subject unless pooling is deliberately requested.",
            "Confirm each calibration session wrote raw/eeg.csv, events/stimulus_manifest.json, and realtime/epochs/epochs.npz.",
            "Run attention8 compare per subject before treating an online model as the reference.",
            "Use the localhost dashboard only during attention8 online; it follows realtime/model_predictions.jsonl, not demo_predictions.jsonl.",
            "Keep adaptation disabled unless the protocol explicitly calls for --enable-adaptation.",
        ],
        "commands": commands,
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


def _comparison_methods(values: list[str] | None) -> list[dict[str, str]]:
    requested = values or list(DEFAULT_COMPARISON_METHODS)
    profiles: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in requested:
        key = str(value).strip().lower()
        method = COMPARISON_METHOD_ALIASES.get(key, key)
        if method not in COMPARISON_METHOD_PROFILES:
            raise ValueError(f"unknown attention8 comparison method '{value}'")
        if method in seen:
            continue
        seen.add(method)
        profiles.append(dict(COMPARISON_METHOD_PROFILES[method], requested_method=str(value)))
    return profiles


def _comparison_output_root(args: argparse.Namespace, session: Path) -> Path:
    if args.output_dir:
        return Path(args.output_dir).expanduser().resolve()
    online_session = getattr(args, "online_session_dir", None)
    if online_session:
        return Path(str(online_session)).expanduser().resolve() / "reports" / "classification" / "attention8_offline_comparison"
    return session / "reports" / "classification" / "attention8_offline_comparison"


def _online_reference(value: str | None) -> dict[str, Any]:
    if not value:
        return {"status": "not_requested"}
    root = Path(value).expanduser().resolve()
    if not root.exists():
        return {
            "status": "missing",
            "session_dir": str(root),
            "reason": "online_session_dir_not_found",
        }
    config = load_config(root / "parameters.json") if (root / "parameters.json").exists() else {}
    primary_kind = str(dict(dict(config.get("realtime", {})).get("model", {})).get("kind", "unknown"))
    try:
        evaluated = evaluate_classifier_session(root)
    except Exception as exc:
        return {
            "status": "failed",
            "session_dir": str(root),
            "primary_model_kind": primary_kind,
            "error": f"{type(exc).__name__}: {exc}",
        }
    primary_metrics = dict(dict(evaluated.get("metrics") or {}).get("primary") or {})
    return {
        "status": evaluated.get("status", "unknown"),
        "session_dir": str(root),
        "primary_model_kind": primary_kind,
        "metrics": primary_metrics,
        "prediction_count": evaluated.get("prediction_count"),
        "metrics_file": str(root / "reports" / "classification" / "metrics.json"),
    }


def _preferred_metrics(training_result: dict[str, Any]) -> dict[str, Any]:
    metrics = dict(training_result.get("metrics") or {})
    query = metrics.get("query_metrics")
    if isinstance(query, dict) and query:
        return {**query, "comparison_metric_source": "temporal_query"}
    blocked = dict(metrics.get("blocked_validation") or {})
    blocked_metrics = blocked.get("metrics")
    if blocked.get("status") == "ok" and isinstance(blocked_metrics, dict):
        return {**blocked_metrics, "comparison_metric_source": "blocked_validation"}
    return {**metrics, "comparison_metric_source": "training_fit"}


def _metric_delta(candidate: dict[str, Any] | None, reference: dict[str, Any] | None) -> dict[str, Any]:
    if not candidate or not reference:
        return {"status": "missing_online_reference"}
    result: dict[str, Any] = {"status": "ok"}
    for key in ("balanced_accuracy", "roc_auc", "auprc", "brier_score", "ece", "coverage"):
        left = _optional_float(candidate.get(key))
        right = _optional_float(reference.get(key))
        result[f"{key}_delta"] = None if left is None or right is None else float(left - right)
    return result


def _write_comparison_outputs(output_root: Path, summary: dict[str, Any]) -> dict[str, str]:
    output_root.mkdir(parents=True, exist_ok=True)
    summary_path = output_root / "summary.json"
    csv_path = output_root / "summary.csv"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fields = [
        "method",
        "model_kind",
        "status",
        "metric_source",
        "balanced_accuracy",
        "roc_auc",
        "auprc",
        "brier_score",
        "ece",
        "coverage",
        "operating_threshold",
        "online_balanced_accuracy_delta",
        "online_auprc_delta",
        "bundle_path",
        "implementation_note",
        "error",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in _comparison_csv_rows(summary):
            writer.writerow(row)
    return {"summary_json": str(summary_path), "summary_csv": str(csv_path)}


def _comparison_csv_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for method, payload in dict(summary.get("methods") or {}).items():
        metrics = dict(payload.get("metrics") or {})
        delta = dict(payload.get("comparison_to_online") or {})
        profile = dict(payload.get("comparison_profile") or {})
        rows.append(
            {
                "method": method,
                "model_kind": payload.get("model_kind"),
                "status": payload.get("status"),
                "metric_source": metrics.get("comparison_metric_source"),
                "balanced_accuracy": metrics.get("balanced_accuracy"),
                "roc_auc": metrics.get("roc_auc"),
                "auprc": metrics.get("auprc"),
                "brier_score": metrics.get("brier_score"),
                "ece": metrics.get("ece"),
                "coverage": metrics.get("coverage"),
                "operating_threshold": metrics.get("operating_threshold"),
                "online_balanced_accuracy_delta": delta.get("balanced_accuracy_delta"),
                "online_auprc_delta": delta.get("auprc_delta"),
                "bundle_path": payload.get("bundle_path"),
                "implementation_note": profile.get("implementation_note"),
                "error": payload.get("error"),
            }
        )
    return rows


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
