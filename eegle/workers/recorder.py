"""Recorder worker process."""

from __future__ import annotations

import argparse
import threading
from time import monotonic, sleep

from eegle.config import load_config
from eegle.devices.lsl_eeg import LslEegRecorder
from eegle.session import paths_for_existing_session
from eegle.telemetry import Telemetry, telemetry_config_from
from eegle.workers.common import StatusWriter, install_stop_signal_handlers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Managed recorder worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--backend", default="lsl_csv")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    paths = paths_for_existing_session(args.session_dir)
    telemetry = Telemetry.from_config(config, paths, component="recorder")
    telemetry_config = telemetry_config_from(config)
    status = StatusWriter(paths.process_logs / "recorder.status.json", "recorder", args.backend, telemetry)
    stop_event = threading.Event()
    install_stop_signal_handlers(stop_event)

    if args.backend in {"disabled", "none"}:
        status.update("disabled", reason="recorder backend disabled")
        return 0
    if args.backend == "labrecorder_xdf":
        status.update(
            "unsupported",
            reason="LabRecorder/XDF backend is scaffolded but not launched in this pass",
            xdf_file=str(paths.raw / "recording.xdf"),
            csv_mirror_file=str(paths.eeg_csv),
        )
        return 2
    if args.backend != "lsl_csv":
        status.update("failed", error=f"recorder backend '{args.backend}' is not implemented")
        return 2

    eeg_config = config.get("hardware", {}).get("eeg", {})
    timeout = float(eeg_config.get("stream_timeout_seconds", 5.0))
    recorder = LslEegRecorder(eeg_config, paths.eeg_csv, paths.eeg_metadata, stream_timeout_seconds=timeout)
    telemetry.emit(
        "lsl.discovery.start",
        level="default",
        message="Resolving EEG LSL stream",
        metadata={
            "backend": args.backend,
            "lsl_stream_type": eeg_config.get("lsl_stream_type", "EEG"),
            "lsl_name_patterns": eeg_config.get("lsl_name_patterns", []),
            "timeout_seconds": timeout,
        },
    )
    status.update("starting", raw_file=str(paths.eeg_csv), metadata_file=str(paths.eeg_metadata))
    recorder.start()
    recorder.wait_until_ready(timeout=timeout + 2.0)
    snapshot = recorder.snapshot()
    if snapshot.get("status") != "recording":
        summary = recorder.stop()
        failed_status = summary.get("status", snapshot.get("status", "failed"))
        telemetry.emit(
            "lsl.discovery.failed",
            level="default",
            message=summary.get("error") or "EEG LSL stream did not start recording",
            metadata=summary,
        )
        status.update(failed_status, summary=summary, error=summary.get("error"))
        return 1

    telemetry.emit("lsl.discovery.complete", level="default", message="EEG LSL stream connected", metadata=snapshot)
    status.update("recording", summary=snapshot)
    last_update = monotonic()
    heartbeat_seconds = float(telemetry_config.get("heartbeat_seconds", 5.0))
    last_health_event = monotonic()
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
                if monotonic() - last_health_event >= heartbeat_seconds:
                    telemetry.emit("eeg.sample_heartbeat", level="realtime", message="EEG recorder heartbeat", metadata=snapshot)
                    last_health_event = monotonic()
            sleep(0.1)
    finally:
        summary = recorder.stop()
        final_status = summary.get("status", "stopped")
        telemetry.emit(
            "recorder.stop",
            level="default",
            message=f"Recorder stopped with status {final_status}",
            metadata=summary,
        )
        status.update(final_status, summary=summary, error=summary.get("error"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
