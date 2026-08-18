"""NDJSON supervisor boundary for the external Study 1 task process."""

from __future__ import annotations

import argparse
import importlib
import json
import queue
import sys
import threading
import traceback
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from demos.workbench.profile import load_study1_profile
from demos.workbench.tasks.study1_dsart import (
    build_task_plan,
    run_psychopy_presentation,
    run_qt_presentation,
)

PROTOCOL = "eegle.workbench.study1_task_protocol.v1"


def _write_message(value: Mapping[str, Any], lock: threading.Lock) -> None:
    with lock:
        payload = {"schema": PROTOCOL, **dict(value)}
        sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        sys.stdout.flush()


def _command_reader(commands: queue.Queue[dict[str, Any]], lock: threading.Lock) -> None:
    for line in sys.stdin:
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise TypeError("command must be an object")
            if payload.get("schema") != PROTOCOL:
                raise TypeError("task command has an unsupported schema")
        except (json.JSONDecodeError, TypeError) as exc:
            _write_message(
                {"type": "warning", "code": "task.invalid_command", "message": str(exc)},
                lock,
            )
            continue
        commands.put(payload)
    commands.put({"command": "abort", "reason": "workbench_disconnected"})


def _marker_outlet(profile: Any, session_id: str) -> tuple[Any | None, dict[str, Any]]:
    try:
        pylsl = importlib.import_module("pylsl")
    except (ImportError, OSError, RuntimeError):
        return None, {
            "name": profile.payload["markers"]["stream_name"],
            "type": profile.payload["markers"]["stream_type"],
            "source_id": None,
            "available": False,
        }
    source_id = f"eegle-workbench-study1-{session_id}"
    try:
        info = pylsl.StreamInfo(
            profile.payload["markers"]["stream_name"],
            profile.payload["markers"]["stream_type"],
            1,
            0.0,
            "string",
            source_id,
        )
        event_kinds = info.desc().append_child("event_kinds")
        for kind in profile.marker_kinds:
            event_kinds.append_child_value("kind", kind)
        info.desc().append_child_value("contract", "eegle.workbench.study1_dsart.v1")
        outlet = pylsl.StreamOutlet(info)
    except (OSError, RuntimeError) as exc:
        return None, {
            "name": profile.payload["markers"]["stream_name"],
            "type": profile.payload["markers"]["stream_type"],
            "source_id": source_id,
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return outlet, {
        "name": profile.payload["markers"]["stream_name"],
        "type": profile.payload["markers"]["stream_type"],
        "source_id": source_id,
        "available": True,
        "event_kinds": list(profile.marker_kinds),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Workbench Study 1 DSART task")
    parser.add_argument("--variant", default="demo_rehearsal")
    parser.add_argument("--participant", default="DEMO-001")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--session-root", type=Path, required=True)
    parser.add_argument(
        "--renderer",
        choices=("qt_preview", "psychopy"),
        default="qt_preview",
    )
    parser.add_argument("--controlled-start", action="store_true")
    args = parser.parse_args(argv)
    lock = threading.Lock()
    commands: queue.Queue[dict[str, Any]] = queue.Queue()
    threading.Thread(target=_command_reader, args=(commands, lock), daemon=True).start()
    try:
        profile = load_study1_profile()
        plan = build_task_plan(
            profile,
            participant_pseudonym=args.participant,
            variant=args.variant,
        )
        outlet, marker_stream = _marker_outlet(profile, args.session_id)

        def marker(value: str) -> None:
            if outlet is not None:
                outlet.push_sample([value])

        def message(value: Mapping[str, Any]) -> None:
            payload = dict(value)
            if payload.get("type") == "armed":
                payload["marker_stream"] = marker_stream
                payload["lsl_available"] = bool(marker_stream["available"])
                payload["session_id"] = args.session_id
                payload["plan_hash"] = plan.plan_hash
            _write_message(payload, lock)

        if args.renderer == "psychopy":
            return run_psychopy_presentation(
                plan,
                args.session_root,
                publish_marker=marker,
                publish_message=message,
                command_queue=commands,
                display=profile.payload["display"],
            )
        return run_qt_presentation(
            plan,
            args.session_root,
            publish_marker=marker,
            publish_message=message,
            allow_local_start=not args.controlled_start,
            command_queue=commands,
        )
    except Exception as exc:  # noqa: BLE001 - child boundary must emit one terminal failure
        _write_message(
            {
                "type": "failed",
                "error": {
                    "code": "task.start_failed",
                    "message": f"{type(exc).__name__}: {exc}",
                    "traceback": "".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    ),
                },
            },
            lock,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
