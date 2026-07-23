"""Explicit filesystem I/O for compiled plans and lock manifests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from eegle.compiler.lockfile import ExecutionLock
from eegle.compiler.plan import ExecutionPlan


def write_plan(path: str | Path, plan: ExecutionPlan) -> Path:
    return _atomic_json(path, plan.to_payload())


def read_plan(path: str | Path) -> ExecutionPlan:
    return ExecutionPlan.from_payload(_read_object(path))


def write_lock(path: str | Path, lock: ExecutionLock) -> Path:
    return _atomic_json(path, lock.to_payload())


def read_lock(path: str | Path) -> ExecutionLock:
    return ExecutionLock.from_payload(_read_object(path))


def _read_object(path: str | Path) -> Mapping[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise TypeError(f"{path} must contain a JSON object")
    return payload


def _atomic_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target
