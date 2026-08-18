"""NDJSON supervisor boundary for one authoritative EEGle project run."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from eegle.compiler import read_lock, read_plan
from eegle.operations import RunControl, open_project, rehearse_project, run_project

PROTOCOL = "eegle.workbench.runner_protocol.v1"


def _write(payload: Mapping[str, Any], lock: threading.Lock) -> None:
    value = {"schema": PROTOCOL, **dict(payload)}
    with lock:
        sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def _error_payload(exc: BaseException) -> dict[str, Any]:
    diagnostics = getattr(exc, "diagnostics", ())
    return {
        "code": "runner.execution_failed",
        "message": f"{type(exc).__name__}: {exc}",
        "diagnostics": [
            {
                "code": getattr(value, "code", "unknown"),
                "title": getattr(value, "title", "Execution issue"),
                "message": getattr(value, "message", str(value)),
            }
            for value in diagnostics
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one EEGle Workbench session")
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--mode", choices=("live_lsl", "simulation"), required=True)
    args = parser.parse_args(argv)

    write_lock = threading.Lock()
    start_event = threading.Event()
    stop_before_start = threading.Event()
    control = RunControl()

    try:
        project = open_project(args.project_root)
        plan = read_plan(project.path_for("execution_plan"))
        lock = read_lock(project.path_for("execution_lock"))
        lock.verify_plan(plan)
    except Exception as exc:  # noqa: BLE001 - process boundary
        _write({"type": "failed", "error": _error_payload(exc)}, write_lock)
        return 1

    def read_commands() -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                command = json.loads(line)
                if not isinstance(command, dict):
                    raise TypeError("runner command must be an object")
                if command.get("schema") != PROTOCOL:
                    raise TypeError("runner command has an unsupported schema")
                name = str(command.get("command", ""))
            except (json.JSONDecodeError, TypeError) as exc:
                _write(
                    {
                        "type": "warning",
                        "code": "runner.invalid_command",
                        "message": str(exc),
                    },
                    write_lock,
                )
                continue
            if name == "start":
                start_event.set()
            elif name == "complete":
                control.complete(str(command.get("reason") or "task_complete"))
                _write({"type": "state", "state": "finalizing"}, write_lock)
            elif name == "cancel":
                reason = str(command.get("reason") or "operator_stop")
                if not start_event.is_set():
                    stop_before_start.set()
                    start_event.set()
                else:
                    control.cancel(reason)
                    _write({"type": "state", "state": "cancelling"}, write_lock)
            else:
                _write(
                    {
                        "type": "warning",
                        "code": "runner.unknown_command",
                        "message": f"Unknown runner command: {name or '<empty>'}",
                    },
                    write_lock,
                )
        if not start_event.is_set():
            stop_before_start.set()
            start_event.set()
        else:
            control.cancel("workbench_disconnected")

    threading.Thread(target=read_commands, daemon=True).start()
    _write(
        {
            "type": "ready",
            "mode": args.mode,
            "session_id": args.session_id,
            "plan_hash": plan.plan_hash,
            "lock_hash": lock.lock_hash,
        },
        write_lock,
    )
    start_event.wait()
    if stop_before_start.is_set():
        _write(
            {
                "type": "failed",
                "error": {
                    "code": "runner.cancelled_before_start",
                    "message": "The runner was cancelled before session creation.",
                },
            },
            write_lock,
        )
        return 2

    try:
        if args.mode == "simulation":
            _write({"type": "state", "state": "running"}, write_lock)
            result = rehearse_project(
                args.project_root,
                session_id=args.session_id,
                control=control,
            )
        else:
            def announce_attachment() -> None:
                if control.wait_until_attached(timeout_seconds=15.0):
                    _write({"type": "state", "state": "running"}, write_lock)
                else:
                    _write(
                        {
                            "type": "warning",
                            "code": "runner.attach_timeout",
                            "message": "EEGle did not attach the controlled engine within 15 seconds.",
                        },
                        write_lock,
                    )

            threading.Thread(target=announce_attachment, daemon=True).start()
            result = run_project(
                args.project_root,
                session_id=args.session_id,
                kind="demo_rehearsal",
                control=control,
            )
        _write(
            {
                "type": "completed",
                "session_id": args.session_id,
                "project_run": result.to_payload(),
            },
            write_lock,
        )
        return 0
    except BaseException as exc:  # noqa: BLE001 - must report process failures
        _write({"type": "failed", "error": _error_payload(exc)}, write_lock)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
