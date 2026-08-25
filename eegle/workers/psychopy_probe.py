"""Disposable live PsychoPy display and PTB-keyboard preflight probe."""

from __future__ import annotations

import argparse
import json
import traceback
from pathlib import Path
from typing import Any

from eegle.psychopy_display import _probe_psychopy_display_and_keyboard_inline


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe PsychoPy display and PTB keyboard")
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    request_path = Path(args.request).expanduser().resolve()
    result_path = Path(args.result).expanduser().resolve()

    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        config = request.get("config") if isinstance(request, dict) else None
        if not isinstance(config, dict):
            raise ValueError("probe request requires an object-valued config")
        result = {
            "status": "ok",
            "timing": _probe_psychopy_display_and_keyboard_inline(config),
        }
    except BaseException as exc:
        result = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    _write_result(result_path, result)
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
