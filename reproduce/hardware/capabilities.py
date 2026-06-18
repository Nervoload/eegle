"""Capability-level readiness checks for cross-platform operation."""

from __future__ import annotations

import shutil
import sys
from importlib import util
from typing import Any

from reproduce.hardware.system import CheckResult

CONSOLE_COMMANDS = ("eegle", "alpha8", "inhibition8", "classify8")

TRAINING_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "erp_roi_logreg": ("sklearn", "joblib"),
    "sklearn_flatten_lda": ("sklearn", "joblib"),
    "sklearn_xdawn_lda": ("sklearn", "joblib"),
    "pyriemann_erp_cov": ("sklearn", "joblib", "pyriemann"),
    "torch_eegnet": ("torch",),
}


def check_command_entrypoints(commands: tuple[str, ...] = CONSOLE_COMMANDS) -> CheckResult:
    """Report whether installed console commands are visible on PATH."""
    found = {command: shutil.which(command) for command in commands}
    missing = [command for command, path in found.items() if not path]
    status = "ok" if not missing else "warn"
    detail = "all EEGle console commands found" if not missing else "missing console commands: " + ", ".join(missing)
    return CheckResult(
        "commands",
        status,
        detail,
        {
            "commands": found,
            "missing": missing,
            "module_fallback": f"{sys.executable} -m reproduce.cli",
        },
    )


def check_display_ready(config: dict[str, Any]) -> CheckResult:
    """Check PsychoPy import readiness without opening a display window."""
    display = dict(config.get("hardware", {}).get("display", {}))
    if not display:
        return CheckResult("display_ready", "skip", "no display configuration present")
    if _importable("psychopy"):
        return CheckResult(
            "display_ready",
            "ok",
            "PsychoPy is importable; display should be validated with a dry task run",
            {"win_type": display.get("win_type", "pyglet"), "opens_window": False},
        )
    return CheckResult(
        "display_ready",
        "warn",
        "PsychoPy is not importable; psychopy task mode will not run",
        {"win_type": display.get("win_type", "pyglet"), "opens_window": False, "missing_package": "psychopy"},
    )


def check_realtime_ready(
    config: dict[str, Any],
    stream_dicts: list[dict[str, Any]],
    lsl_error: str | None,
    eeg_device: CheckResult,
    *,
    require_eeg: bool,
) -> CheckResult:
    """Check whether the configured realtime worker can start on this host."""
    if not _realtime_requested(config):
        return CheckResult("realtime_ready", "skip", "realtime processor disabled")

    failures: list[str] = []
    warnings: list[str] = []
    if not _importable("pylsl"):
        failures.append("pylsl is not importable")
    if lsl_error:
        failures.append(lsl_error)
    matches = list(eeg_device.data.get("matches", []))
    if not matches:
        failures.append("no matching EEG LSL stream detected")
    marker_issue = _marker_config_issue(config)
    if marker_issue:
        failures.append(marker_issue)
    dashboard_issue = _dashboard_config_issue(config)
    if dashboard_issue:
        failures.append(dashboard_issue)

    status = "fail" if failures else ("warn" if warnings else "ok")
    detail_parts = failures or warnings or ["realtime worker launch path is portable"]
    return CheckResult(
        "realtime_ready",
        status,
        "; ".join(detail_parts),
        {
            "realtime_requested": True,
            "matching_eeg_stream_count": len(matches),
            "candidate_eeg_stream_count": len(eeg_device.data.get("candidate_eeg_streams", stream_dicts)),
            "worker_launch": {
                "python": sys.executable,
                "module_launch": True,
                "shell": False,
            },
            "require_eeg": require_eeg,
            "failures": failures,
            "warnings": warnings,
        },
    )


def check_training_ready(model_kinds: list[str] | tuple[str, ...], *, required: bool = False) -> CheckResult:
    """Check import readiness for requested trainable model kinds."""
    kinds = list(dict.fromkeys(_normalize_model_kind(kind) for kind in model_kinds if kind))
    if not kinds:
        return CheckResult("training_ready", "skip", "no trainable model kinds requested")
    missing_by_kind = {kind: missing_training_packages(kind) for kind in kinds}
    missing = sorted({package for packages in missing_by_kind.values() for package in packages})
    status = "ok" if not missing else ("fail" if required else "warn")
    detail = "training dependencies available" if not missing else "missing training packages: " + ", ".join(missing)
    return CheckResult(
        "training_ready",
        status,
        detail,
        {
            "model_kinds": kinds,
            "requirements": {kind: list(training_requirements(kind)) for kind in kinds},
            "missing_by_kind": missing_by_kind,
            "missing": missing,
        },
    )


def training_model_kinds_from_config(config: dict[str, Any]) -> list[str]:
    realtime = dict(config.get("realtime", {}))
    classifier_enabled = bool(realtime.get("classifier", {}).get("enabled", False))
    model = dict(realtime.get("model", {}))
    kind = str(model.get("kind", "") or "")
    shadows = [
        str(shadow.get("kind", "") or "")
        for shadow in realtime.get("shadow_models", [])
        if isinstance(shadow, dict)
    ]
    kinds = [kind, *shadows]
    return [
        _normalize_model_kind(value)
        for value in dict.fromkeys(kinds)
        if classifier_enabled or _normalize_model_kind(value) in TRAINING_REQUIREMENTS
    ]


def training_requirements(kind: str) -> tuple[str, ...]:
    return TRAINING_REQUIREMENTS.get(_normalize_model_kind(kind), ())


def missing_training_packages(kind: str) -> list[str]:
    return [package for package in training_requirements(kind) if not _importable(package)]


def _normalize_model_kind(kind: str) -> str:
    value = str(kind).strip().lower()
    return "sklearn_flatten_lda" if value == "sklearn_xdawn_lda" else value


def _importable(package: str) -> bool:
    return util.find_spec(package) is not None


def _realtime_requested(config: dict[str, Any]) -> bool:
    realtime = dict(config.get("realtime", {}))
    processes = dict(config.get("processes", {}))
    components = dict(config.get("experiment", {}).get("components", {}))
    process_config = dict(processes.get("realtime_processor", {}))
    component = str(components.get("realtime_processor", "disabled"))
    backend = str(process_config.get("backend", component))
    return bool(realtime.get("enabled", False)) or bool(process_config.get("enabled", False)) or backend not in {
        "disabled",
        "none",
    }


def _marker_config_issue(config: dict[str, Any]) -> str | None:
    markers = dict(config.get("hardware", {}).get("markers", {}))
    if not str(markers.get("lsl_stream_name", "") or "").strip():
        return "marker LSL stream name is not configured"
    if not str(markers.get("lsl_stream_type", "") or "").strip():
        return "marker LSL stream type is not configured"
    return None


def _dashboard_config_issue(config: dict[str, Any]) -> str | None:
    realtime_dashboard = dict(config.get("realtime", {}).get("dashboard", {}))
    process_dashboard = dict(config.get("processes", {}).get("dashboard", {}))
    enabled = bool(realtime_dashboard.get("enabled", False)) or bool(process_dashboard.get("enabled", False))
    if not enabled:
        return None
    backend = str(process_dashboard.get("backend", "http"))
    host = str(process_dashboard.get("host", realtime_dashboard.get("host", "127.0.0.1")))
    if backend != "http":
        return f"dashboard backend '{backend}' is not supported"
    if host not in {"127.0.0.1", "localhost"}:
        return "dashboard must bind to localhost"
    return None
