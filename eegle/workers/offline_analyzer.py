"""Offline analyzer worker process."""

from __future__ import annotations

import argparse

from eegle.analysis.reports import analyze_session
from eegle.config import load_config
from eegle.session import paths_for_existing_session
from eegle.telemetry import Telemetry
from eegle.workers.common import StatusWriter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Managed offline analyzer worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--backend", default="minimal")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    paths = paths_for_existing_session(args.session_dir)
    telemetry = Telemetry.from_config(config, paths, component="offline_analyzer")
    status = StatusWriter(paths.process_logs / "offline_analyzer.status.json", "offline_analyzer", args.backend, telemetry)

    if args.backend in {"disabled", "none"}:
        status.update("disabled", reason="offline analysis disabled")
        return 0
    if args.backend != "minimal":
        status.update("failed", error=f"offline analyzer backend '{args.backend}' is not implemented")
        return 2

    try:
        telemetry.emit("analysis.start", level="default", message="Offline analysis starting", metadata={"backend": args.backend})
        status.update("running", session_dir=str(paths.root))
        summary = analyze_session(paths.root)
    except Exception as exc:
        telemetry.emit(
            "analysis.failed",
            level="default",
            message="Offline analysis failed",
            metadata={"exception_type": type(exc).__name__, "exception": str(exc)},
        )
        status.update("failed", error=f"{type(exc).__name__}: {exc}")
        return 1
    telemetry.emit("analysis.complete", level="default", message="Offline analysis complete", metadata={"summary": summary})
    status.update("complete", summary=summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
