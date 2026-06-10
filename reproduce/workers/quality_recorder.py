"""Managed Enobio Quality stream recorder worker."""

from __future__ import annotations

import argparse
import threading
from time import monotonic, sleep

from reproduce.config import load_config
from reproduce.devices.lsl_quality import LslQualityRecorder
from reproduce.session import paths_for_existing_session
from reproduce.telemetry import Telemetry, telemetry_config_from
from reproduce.workers.common import StatusWriter, install_stop_signal_handlers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Managed Quality stream recorder")
    parser.add_argument("--config", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--backend", default="lsl_csv")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    paths = paths_for_existing_session(args.session_dir)
    telemetry = Telemetry.from_config(config, paths, component="quality_recorder")
    status = StatusWriter(paths.process_logs / "quality_recorder.status.json", "quality_recorder", args.backend, telemetry)
    stop_event = threading.Event()
    install_stop_signal_handlers(stop_event)
    if args.backend in {"disabled", "none"}:
        status.update("disabled", reason="quality recorder disabled")
        return 0
    if args.backend != "lsl_csv":
        status.update("failed", error=f"quality recorder backend '{args.backend}' is not implemented")
        return 2

    quality_config = dict(config.get("hardware", {}).get("quality", {}))
    timeout = float(quality_config.get("stream_timeout_seconds", 5.0))
    recorder = LslQualityRecorder(
        quality_config,
        dict(config.get("hardware", {}).get("eeg", {})),
        paths.quality_csv,
        paths.quality_metadata,
        timeout,
    )
    status.update("starting", raw_file=str(paths.quality_csv), metadata_file=str(paths.quality_metadata))
    recorder.start()
    recorder.wait_until_ready(timeout=timeout + 2.0)
    snapshot = recorder.snapshot()
    if snapshot.get("status") != "recording":
        summary = recorder.stop()
        status.update(summary.get("status", "failed"), summary=summary, error=summary.get("error"))
        return 1
    status.update("recording", summary=snapshot)
    heartbeat = float(telemetry_config_from(config).get("heartbeat_seconds", 5.0))
    last_update = last_health = monotonic()
    try:
        while not stop_event.is_set():
            if not recorder.is_alive():
                snapshot = recorder.snapshot()
                status.update(snapshot.get("status", "stopped"), summary=snapshot, error=snapshot.get("error"))
                return 1 if snapshot.get("status") == "failed" else 0
            if monotonic() - last_update >= 1.0:
                snapshot = recorder.snapshot()
                status.update("recording", summary=snapshot)
                last_update = monotonic()
                if monotonic() - last_health >= heartbeat:
                    telemetry.emit("quality.sample_heartbeat", level="realtime", metadata=snapshot)
                    last_health = monotonic()
            sleep(0.1)
    finally:
        summary = recorder.stop()
        status.update(summary.get("status", "stopped"), summary=summary, error=summary.get("error"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
