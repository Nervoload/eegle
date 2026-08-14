"""Recorder worker process."""

from __future__ import annotations

import argparse
import os
import threading
from time import monotonic, sleep
from typing import Any

from eegle.config import load_config
from eegle.devices.labrecorder_xdf import LabRecorderXdfRecorder
from eegle.devices.lsl_eeg import LslEegRecorder
from eegle.session import SessionPaths, paths_for_existing_session
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
    install_stop_signal_handlers(stop_event, paths.process_logs / "recorder.stop")
    manager_pid = os.getppid()

    if args.backend in {"disabled", "none"}:
        status.update("disabled", reason="recorder backend disabled")
        return 0
    if args.backend == "labrecorder_xdf":
        return _run_labrecorder_xdf(
            config,
            paths,
            status,
            telemetry,
            telemetry_config,
            stop_event,
            manager_pid,
        )
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
    final_status = "failed"
    try:
        while not stop_event.is_set():
            if _manager_process_disappeared(manager_pid):
                telemetry.emit(
                    "recorder.parent_lost",
                    level="default",
                    message="Recorder manager process disappeared; stopping raw EEG acquisition",
                    metadata={"manager_pid": manager_pid, "current_parent_pid": os.getppid()},
                )
                stop_event.set()
                break
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
    return 0 if final_status == "stopped" else 1


def _run_labrecorder_xdf(
    config: dict[str, Any],
    paths: SessionPaths,
    status: StatusWriter,
    telemetry: Telemetry,
    telemetry_config: dict[str, Any],
    stop_event: threading.Event,
    manager_pid: int,
) -> int:
    """Run LabRecorder and the existing CSV mirror behind one worker status."""
    recorder_config = dict(config.get("processes", {}).get("recorder", {}) or {})
    startup_timeout = float(recorder_config.get("startup_timeout_seconds", 20.0))
    recorder = LabRecorderXdfRecorder(config, paths, startup_timeout_seconds=startup_timeout)
    status.update(
        "starting",
        xdf_file=str(paths.eeg_xdf),
        csv_mirror_file=str(paths.eeg_csv),
        metadata_file=str(paths.xdf_metadata),
    )
    telemetry.emit(
        "lsl.discovery.start",
        level="default",
        message="Starting managed LabRecorder/XDF acquisition and CSV mirror",
        metadata={"backend": "labrecorder_xdf", "xdf_file": str(paths.eeg_xdf)},
    )
    try:
        snapshot = recorder.start()
    except Exception as exc:
        snapshot = recorder.snapshot()
        error = snapshot.get("error") or f"{type(exc).__name__}: {exc}"
        telemetry.emit(
            "lsl.discovery.failed",
            level="default",
            message=str(error),
            metadata=snapshot,
        )
        status.update("failed", summary=snapshot, error=error)
        return 1
    telemetry.emit(
        "lsl.discovery.complete",
        level="default",
        message="Managed LabRecorder/XDF acquisition is recording",
        metadata=snapshot,
    )
    status.update("recording", summary=snapshot)
    last_update = monotonic()
    heartbeat_seconds = float(telemetry_config.get("heartbeat_seconds", 5.0))
    last_health_event = monotonic()
    last_xdf_growth_status = str(snapshot.get("xdf_growth_status") or "advancing")
    stop_reason = "stop_requested"
    final_status = "failed"
    try:
        while not stop_event.is_set():
            if _manager_process_disappeared(manager_pid):
                stop_reason = "manager_parent_lost"
                telemetry.emit(
                    "recorder.parent_lost",
                    level="default",
                    message="Recorder manager process disappeared; finalizing XDF acquisition",
                    metadata={"manager_pid": manager_pid, "current_parent_pid": os.getppid()},
                )
                stop_event.set()
                break
            snapshot = recorder.snapshot()
            if snapshot.get("status") != "recording":
                status.update("failed", summary=snapshot, error=snapshot.get("error"))
                return 1
            xdf_growth_status = str(snapshot.get("xdf_growth_status") or "advancing")
            if xdf_growth_status != last_xdf_growth_status:
                if xdf_growth_status == "buffering_warning":
                    telemetry.emit(
                        "recorder.xdf_buffering_warning",
                        level="default",
                        message=(
                            "LabRecorder XDF disk growth paused while its process remains alive; "
                            "recording continues pending final validation"
                        ),
                        metadata=snapshot,
                    )
                else:
                    telemetry.emit(
                        "recorder.xdf_growth_resumed",
                        level="default",
                        message="LabRecorder XDF disk growth resumed",
                        metadata=snapshot,
                    )
                last_xdf_growth_status = xdf_growth_status
            if monotonic() - last_update >= 1.0:
                status.update("recording", summary=snapshot)
                last_update = monotonic()
                if monotonic() - last_health_event >= heartbeat_seconds:
                    telemetry.emit(
                        "eeg.sample_heartbeat",
                        level="realtime",
                        message="Managed XDF recorder heartbeat",
                        metadata=snapshot,
                    )
                    last_health_event = monotonic()
            sleep(0.1)
    finally:
        summary = recorder.stop(reason=stop_reason)
        final_status = str(summary.get("status", "failed"))
        telemetry.emit(
            "recorder.stop",
            level="default",
            message=f"XDF recorder stopped with status {final_status}",
            metadata=summary,
        )
        status.update(final_status, summary=summary, error=summary.get("error"))
    return 0 if final_status == "stopped" else 1


def _manager_process_disappeared(manager_pid: int) -> bool:
    """Detect Unix-style reparenting without making normal shutdown depend on it."""
    current_parent = os.getppid()
    return manager_pid > 1 and current_parent != manager_pid


if __name__ == "__main__":
    raise SystemExit(main())
