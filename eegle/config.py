"""Configuration loading for reproducible experiment runs."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from eegle.runtime import PROJECT_ROOT

DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default_experiment.json"
SESSION_ROOT_ENV_VARS = ("EEGLE_SESSION_ROOT", "CLOSEDLOOP_SESSION_ROOT")


def resolve_path(path: str | Path) -> Path:
    candidate = Path(os.path.expandvars(str(path))).expanduser()
    if candidate.is_absolute():
        return candidate
    return (PROJECT_ROOT / candidate).resolve()


def session_root_env_override() -> str | None:
    for name in SESSION_ROOT_ENV_VARS:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value
    return None


def resolve_session_root(config: dict[str, Any], root: str | Path | None = None) -> Path:
    value = root
    if value is None:
        value = session_root_env_override()
    if value is None:
        value = config.get("runtime", {}).get("session_root", "data")
    return resolve_path(value)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = resolve_path(path or DEFAULT_CONFIG)
    with config_path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    config["_config_path"] = str(config_path)
    return config


def write_config(config: dict[str, Any], path: str | Path) -> None:
    target = resolve_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, sort_keys=True)
        handle.write("\n")


def merged_config(config: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    _merge_into(result, updates)
    return result


def _merge_into(target: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge_into(target[key], value)
        else:
            target[key] = value


def task_config(config: dict[str, Any], task_name: str | None = None) -> dict[str, Any]:
    task = task_name or config.get("experiment", {}).get("task", "pvt")
    tasks = config.get("tasks", {})
    if task not in tasks:
        raise KeyError(f"Unknown task '{task}'. Known tasks: {', '.join(sorted(tasks))}")
    return copy.deepcopy(tasks[task])
