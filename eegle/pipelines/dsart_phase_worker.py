"""Fresh-process executor for one visual DSART child session."""

from __future__ import annotations

import argparse
import traceback
from pathlib import Path
from typing import Any

from eegle.pipelines.dsart_recording import (
    PHASE_WORKER_SCHEMA,
    _deserialize_recording_options,
    _load_json,
    _run_dsart_child_session_inline,
    _write_json_atomic,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated DSART visual session")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    request_path = Path(args.request).expanduser().resolve()
    result_path = Path(args.result).expanduser().resolve()
    result: dict[str, Any]
    try:
        request = _load_json(request_path)
        if request is None:
            raise ValueError(f"phase-worker request is missing or not a JSON object: {request_path}")
        if request.get("schema") != PHASE_WORKER_SCHEMA:
            raise ValueError(f"unsupported phase-worker request schema: {request.get('schema')}")
        options_payload = request.get("options")
        config = request.get("config")
        preflight = request.get("preflight")
        if not isinstance(options_payload, dict) or not isinstance(config, dict) or not isinstance(preflight, dict):
            raise ValueError("phase-worker request requires object-valued config, options, and preflight")
        options = _deserialize_recording_options(options_payload)
        result = _run_dsart_child_session_inline(
            config,
            options,
            visit_id=str(request["visit_id"]),
            session_index=int(request["session_index"]),
            seed=int(request["seed"]),
            preflight=preflight,
        )
    except BaseException as exc:
        result = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "failure_kind": "phase_worker_exception",
            "traceback": traceback.format_exc(),
        }
    _write_json_atomic(result_path, result)
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
