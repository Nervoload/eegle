"""Managed Architecture C process orchestration."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from eegle.runtime import PROJECT_ROOT
from eegle.session import SessionPaths
from eegle.telemetry import Telemetry
from eegle.workers.common import StatusWriter, load_status


@dataclass
class WorkerHandle:
    name: str
    backend: str
    module: str
    command: list[str]
    status_file: Path
    stdout_file: Path
    stderr_file: Path
    stop_file: Path
    process: subprocess.Popen[bytes] | None = None
    stdout_handle: Any | None = None
    stderr_handle: Any | None = None
    started_at_monotonic: float | None = None
    stopped_at_monotonic: float | None = None


@dataclass
class FeedbackManagerSummary:
    status: str
    processes: dict[str, dict[str, Any]]
    normalized_processes: dict[str, Any]
    manager_summary_file: str
    notes: list[str] = field(default_factory=list)


class FeedbackManager:
    """Start, stop, and summarize optional experiment pipeline processes."""

    def __init__(self, config: dict[str, Any], paths: SessionPaths, record_eeg: bool = True) -> None:
        self.config = config
        self.paths = paths
        self.record_eeg = record_eeg
        self.processes = normalize_processes(config, record_eeg=record_eeg)
        self.telemetry = Telemetry.from_config(config, paths, component="feedback_manager")
        self._workers: dict[str, WorkerHandle] = {}
        self._summary: FeedbackManagerSummary | None = None

    def start_before_task(self) -> None:
        self.telemetry.emit(
            "manager.start",
            level="default",
            message="Feedback manager starting enabled processes",
            metadata={"processes": self.processes},
        )
        self.paths.process_logs.mkdir(parents=True, exist_ok=True)
        self.paths.realtime.mkdir(parents=True, exist_ok=True)
        self._patch_manifest()
        self.start_recorder()
        self.start_realtime_processor()
        self.start_dashboard()

    def start_recorder(self) -> None:
        """Start only the EEG recorder process when enabled."""
        recorder = self.processes["recorder"]

        if recorder["enabled"]:
            self._start_worker(
                "recorder",
                "eegle.workers.recorder",
                ["--backend", recorder["backend"]],
                wait_states={"recording", "failed", "unsupported", "stopped"},
                timeout_seconds=float(recorder.get("startup_timeout_seconds", 8.0)),
            )
        else:
            self._write_disabled_status("recorder", recorder["backend"], "recorder disabled by experiment settings")

    def start_realtime_processor(self) -> None:
        """Start only the realtime processor process when enabled."""
        realtime = self.processes["realtime_processor"]
        if realtime["enabled"]:
            self._start_worker(
                "realtime_processor",
                "eegle.workers.realtime_processor",
                [
                    "--backend",
                    realtime["backend"],
                    "--preprocessor",
                    realtime["preprocessor"],
                    "--model",
                    realtime["model"],
                    "--feedback-backend",
                    self.processes["feedback"]["backend"],
                ],
                wait_states={"running", "failed", "disabled"},
                timeout_seconds=float(realtime.get("startup_timeout_seconds", 8.0)),
            )
        else:
            self._write_disabled_status(
                "realtime_processor",
                realtime["backend"],
                "realtime processor disabled by experiment settings",
            )

    def start_dashboard(self) -> None:
        """Start the optional non-critical localhost classifier dashboard."""
        dashboard = self.processes["dashboard"]
        if not dashboard["enabled"]:
            self._write_disabled_status("dashboard", dashboard["backend"], "dashboard disabled by experiment settings")
            return
        try:
            self._start_worker(
                "dashboard",
                "eegle.workers.dashboard",
                [
                    "--backend",
                    dashboard["backend"],
                    "--host",
                    dashboard["host"],
                    "--port",
                    str(dashboard["port"]),
                ],
                wait_states={"running", "failed", "disabled"},
                timeout_seconds=float(dashboard.get("startup_timeout_seconds", 4.0)),
            )
            dashboard_status = load_status(self.paths.process_logs / "dashboard.status.json") or {}
            if dashboard_status.get("url"):
                self.telemetry.emit(
                    "dashboard.ready",
                    level="default",
                    message=f"Dashboard ready: {dashboard_status['url']}",
                    metadata=dashboard_status,
                )
        except Exception as exc:
            self.telemetry.emit(
                "dashboard.failed_noncritical",
                level="default",
                message="Classifier dashboard failed; experiment will continue",
                metadata={"exception_type": type(exc).__name__, "exception": str(exc)},
            )

    def stop_after_task(self) -> None:
        self.telemetry.emit("manager.stop", level="default", message="Feedback manager stopping processes")
        failures = []
        # Notify every child first. A stuck non-critical dashboard must never
        # postpone the recorder's tail guard and XDF finalization request.
        for name in ("dashboard", "realtime_processor", "recorder"):
            worker = self._workers.get(name)
            process = None if worker is None else getattr(worker, "process", None)
            if process is None or process.poll() is not None:
                continue
            try:
                self._request_worker_stop(worker)
            except Exception as exc:
                # _stop_worker retries the cooperative request. This first-pass
                # warning must not prevent the recorder from being signaled.
                self.telemetry.emit(
                    "process.stop_request_warning",
                    level="default",
                    message=f"Initial cooperative stop request for {name} failed; it will be retried",
                    metadata={
                        "name": name,
                        "exception_type": type(exc).__name__,
                        "exception": str(exc),
                    },
                )
        # Confirm the authoritative recorder first; the other children have
        # already received their cooperative stop requests and cannot hold XDF
        # finalization hostage.
        for name in ("recorder", "realtime_processor", "dashboard"):
            worker = self._workers.get(name)
            if worker is not None:
                try:
                    self._stop_worker(worker)
                except Exception as exc:
                    if name != "dashboard":
                        failures.append(f"{name}: {type(exc).__name__}: {exc}")
                    self.telemetry.emit(
                        "process.stop_failed",
                        level="default",
                        message=f"Failed to stop {name} cleanly",
                        metadata={"name": name, "exception_type": type(exc).__name__, "exception": str(exc)},
                    )
        if failures:
            raise RuntimeError("one or more managed processes did not stop cleanly: " + "; ".join(failures))

    def run_offline_analysis(self) -> dict[str, Any] | None:
        analyzer = self.processes["offline_analyzer"]
        if not analyzer["enabled"]:
            self._write_disabled_status(
                "offline_analyzer",
                analyzer["backend"],
                "offline analyzer disabled by experiment settings",
            )
            self._write_summary()
            return None

        worker = self._make_worker(
            "offline_analyzer",
            "eegle.workers.offline_analyzer",
            ["--backend", analyzer["backend"]],
        )
        self.telemetry.emit(
            "analysis.start",
            level="default",
            message="Offline analyzer worker starting",
            metadata={"backend": analyzer["backend"]},
        )
        self._run_worker_to_completion(worker, timeout_seconds=float(analyzer.get("timeout_seconds", 120.0)))
        self._write_summary()
        status = load_status(worker.status_file) or {}
        if status.get("status") in {"failed", "killed"}:
            self.telemetry.emit("analysis.failed", level="default", message="Offline analyzer failed", metadata=status)
        else:
            self.telemetry.emit("analysis.complete", level="default", message="Offline analyzer complete", metadata=status)
        return status.get("summary")

    def summary(self) -> dict[str, Any]:
        if self._summary is None:
            self._write_summary()
        assert self._summary is not None
        return {
            "status": self._summary.status,
            "processes": self._summary.processes,
            "normalized_processes": self._summary.normalized_processes,
            "manager_summary_file": self._summary.manager_summary_file,
            "notes": self._summary.notes,
        }

    def reconfigure(self, config: dict[str, Any]) -> None:
        """Refresh process normalization after calibration updates parameters."""
        self.config = config
        self.processes = normalize_processes(config, record_eeg=self.record_eeg)
        self.telemetry = Telemetry.from_config(config, self.paths, component="feedback_manager")
        self._patch_manifest()

    @property
    def eeg_summary(self) -> dict[str, Any] | None:
        status = load_status(self.paths.process_logs / "recorder.status.json")
        if not status:
            return None
        return status.get("summary") or status

    def _start_worker(
        self,
        name: str,
        module: str,
        extra_args: list[str],
        wait_states: set[str],
        timeout_seconds: float,
    ) -> None:
        worker = self._make_worker(name, module, extra_args)
        try:
            worker.stdout_handle = worker.stdout_file.open("ab")
            worker.stderr_handle = worker.stderr_file.open("ab")
            worker.stop_file.unlink(missing_ok=True)
            self.telemetry.emit(
                "process.start",
                level="default",
                message=f"Starting {name}",
                metadata={
                    "name": name,
                    "backend": worker.backend,
                    "module": module,
                    "command": worker.command,
                    "stdout_file": str(worker.stdout_file),
                    "stderr_file": str(worker.stderr_file),
                },
            )
            with self.telemetry.span(
                "worker_launch",
                component="feedback_manager",
                metadata={"name": name, "backend": worker.backend, "command": worker.command},
            ):
                worker.process = subprocess.Popen(
                    worker.command,
                    cwd=str(PROJECT_ROOT),
                    stdout=worker.stdout_handle,
                    stderr=worker.stderr_handle,
                    env=self._worker_env(),
                )
        except BaseException:
            try:
                self._close_worker_logs(worker)
            except Exception:
                pass
            raise
        worker.started_at_monotonic = monotonic()
        self._workers[name] = worker
        ready_status = self._wait_for_status(worker, wait_states, timeout_seconds)
        if ready_status and ready_status.get("status") in wait_states:
            status_name = ready_status.get("status")
            if status_name in {"failed", "unsupported", "killed"}:
                event = "process.failed"
            elif status_name in {"stopped", "complete"}:
                event = "process.stop"
            elif status_name == "disabled":
                event = "process.disabled"
            else:
                event = "process.ready"
            self.telemetry.emit(
                event,
                level="default",
                message=f"{name} status: {status_name}",
                metadata=ready_status,
            )
            if status_name in {"failed", "unsupported", "killed", "stopped"}:
                detail = ready_status.get("error") or ready_status.get("reason") or ready_status
                raise RuntimeError(f"{name} failed during startup: {detail}")
        else:
            self.telemetry.emit(
                "process.timeout",
                level="default",
                message=f"{name} did not report ready before timeout",
                metadata={"status_file": str(worker.status_file), "timeout_seconds": timeout_seconds, "latest_status": ready_status},
            )
            raise RuntimeError(f"{name} did not become ready within {timeout_seconds:.1f}s")

    def _run_worker_to_completion(self, worker: WorkerHandle, timeout_seconds: float) -> None:
        try:
            worker.stdout_handle = worker.stdout_file.open("ab")
            worker.stderr_handle = worker.stderr_file.open("ab")
            worker.stop_file.unlink(missing_ok=True)
        except BaseException:
            try:
                self._close_worker_logs(worker)
            except Exception:
                pass
            raise
        self.telemetry.emit(
            "process.start",
            level="default",
            message=f"Starting {worker.name}",
            metadata={"name": worker.name, "backend": worker.backend, "module": worker.module, "command": worker.command},
        )
        with self.telemetry.span(
            "worker_run",
            component="feedback_manager",
            metadata={"name": worker.name, "backend": worker.backend, "command": worker.command},
        ):
            worker.process = subprocess.Popen(
                worker.command,
                cwd=str(PROJECT_ROOT),
                stdout=worker.stdout_handle,
                stderr=worker.stderr_handle,
                env=self._worker_env(),
            )
            worker.started_at_monotonic = monotonic()
            self._workers[worker.name] = worker
            task_complete_started = monotonic()
            next_notice = task_complete_started
            timeout_reported = False
            while worker.process.poll() is None:
                elapsed = monotonic() - task_complete_started
                if elapsed >= timeout_seconds and not timeout_reported:
                    timeout_reported = True
                    message = (
                        f"{worker.name} exceeded its {timeout_seconds:.1f}s advisory duration; "
                        "processing continues and will not be force-killed"
                    )
                    print(message, flush=True)
                    self.telemetry.emit(
                        "process.duration_warning",
                        level="default",
                        message=message,
                        metadata={"timeout_seconds": timeout_seconds, "command": worker.command},
                    )
                if monotonic() >= next_notice:
                    latest = load_status(worker.status_file) or {}
                    message = (
                        f"Task is complete; {worker.name} is still processing "
                        f"({elapsed:.1f}s, status={latest.get('status', 'starting')})."
                    )
                    print(message, flush=True)
                    next_notice = monotonic() + 5.0
                self.telemetry.emit(
                    "process.post_task_progress",
                    level="debug",
                    message=f"{worker.name} continues after task completion",
                    metadata={"elapsed_seconds": elapsed, "latest_status": latest},
                )
                try:
                    worker.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            self._ensure_terminal_worker_status(worker)
            worker.stopped_at_monotonic = monotonic()
            self.telemetry.emit(
                "process.stop",
                level="default",
                message=f"{worker.name} exited",
                metadata={
                    "name": worker.name,
                    "returncode": None if worker.process is None else worker.process.poll(),
                    "elapsed_seconds": None if worker.started_at_monotonic is None else worker.stopped_at_monotonic - worker.started_at_monotonic,
                },
            )
            self._close_worker_logs(worker)

    def _stop_worker(self, worker: WorkerHandle) -> None:
        if worker.process is None:
            return
        failures = []
        if worker.process.poll() is None:
            self.telemetry.emit(
                "process.stop",
                level="default",
                message=f"Stopping {worker.name}",
                metadata={"name": worker.name, "backend": worker.backend},
            )
            try:
                self._request_worker_stop(worker)
            except Exception as exc:
                failures.append(f"cooperative stop request failed: {type(exc).__name__}: {exc}")
            task_complete_started = monotonic()
            next_notice = task_complete_started
            while worker.process.poll() is None:
                elapsed = monotonic() - task_complete_started
                if monotonic() >= next_notice:
                    latest = load_status(worker.status_file) or {}
                    detail = latest.get("message") or latest.get("finalization_stage") or latest.get("status") or "stopping"
                    message = (
                        f"Task is complete; {worker.name} is still processing "
                        f"({elapsed:.1f}s): {detail}. No forced kill will be used."
                    )
                    print(message, flush=True)
                    self.telemetry.emit(
                        "process.post_task_progress",
                        level="default",
                        message=message,
                        metadata={"name": worker.name, "elapsed_seconds": elapsed, "latest_status": latest},
                    )
                    next_notice = monotonic() + 5.0
                try:
                    worker.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
                except Exception as exc:
                    message = (
                        f"Could not query {worker.name} completion cleanly "
                        f"({type(exc).__name__}: {exc}); the process remains under observation"
                    )
                    print(message, flush=True)
                    self.telemetry.emit(
                        "process.wait_warning",
                        level="default",
                        message=message,
                        metadata={"name": worker.name},
                    )
                    sleep(0.5)
        try:
            self._ensure_terminal_worker_status(worker)
        except Exception as exc:
            failures.append(f"terminal status update failed: {type(exc).__name__}: {exc}")
        worker.stopped_at_monotonic = monotonic()
        self.telemetry.emit(
            "process.stop",
            level="default",
            message=f"{worker.name} stopped",
            metadata={
                "name": worker.name,
                "returncode": worker.process.poll(),
                "elapsed_seconds": None if worker.started_at_monotonic is None else worker.stopped_at_monotonic - worker.started_at_monotonic,
            },
        )
        try:
            self._close_worker_logs(worker)
        except Exception as exc:
            self.telemetry.emit(
                "process.log_cleanup_warning",
                level="default",
                message=f"{worker.name} stopped but its manager log handles did not close cleanly",
                metadata={"name": worker.name, "exception_type": type(exc).__name__, "exception": str(exc)},
            )
        if failures:
            raise RuntimeError("; ".join(failures))

    def _make_worker(self, name: str, module: str, extra_args: list[str]) -> WorkerHandle:
        backend = extra_args[extra_args.index("--backend") + 1] if "--backend" in extra_args else "default"
        status_file = self.paths.process_logs / f"{name}.status.json"
        stdout_file = self.paths.process_logs / f"{name}.stdout.log"
        stderr_file = self.paths.process_logs / f"{name}.stderr.log"
        stop_file = self.paths.process_logs / f"{name}.stop"
        command = [
            self._python_executable(),
            "-m",
            module,
            "--config",
            str(self.paths.parameters),
            "--session-dir",
            str(self.paths.root),
            *extra_args,
        ]
        return WorkerHandle(name, backend, module, command, status_file, stdout_file, stderr_file, stop_file)

    def _wait_for_status(
        self,
        worker: WorkerHandle,
        ready_states: set[str],
        timeout_seconds: float,
    ) -> dict[str, Any] | None:
        deadline = monotonic() + timeout_seconds
        latest = None
        while monotonic() < deadline:
            latest = load_status(worker.status_file)
            if latest and latest.get("status") in ready_states:
                return latest
            if worker.process is not None and worker.process.poll() is not None:
                self._ensure_terminal_worker_status(worker)
                return load_status(worker.status_file) or latest
            sleep(0.05)
        return latest

    def _write_disabled_status(self, name: str, backend: str, reason: str) -> None:
        writer = StatusWriter(self.paths.process_logs / f"{name}.status.json", name, backend, self.telemetry)
        writer.update("disabled", reason=reason)

    def _write_forced_status(self, worker: WorkerHandle, status: str, reason: str) -> None:
        writer = StatusWriter(worker.status_file, worker.name, worker.backend, self.telemetry)
        metadata = {
            "reason": reason,
            "returncode": None if worker.process is None else worker.process.poll(),
        }
        stderr_tail = self._worker_stderr_tail(worker)
        if stderr_tail:
            metadata["error"] = stderr_tail
        writer.update(status, **metadata)

    def _request_worker_stop(self, worker: WorkerHandle) -> None:
        worker.stop_file.parent.mkdir(parents=True, exist_ok=True)
        with worker.stop_file.open("w", encoding="utf-8") as handle:
            json.dump({"action": "stop", "worker": worker.name}, handle, sort_keys=True)
            handle.write("\n")

    @staticmethod
    def _worker_stderr_tail(worker: WorkerHandle) -> str | None:
        try:
            if worker.stderr_handle is not None:
                worker.stderr_handle.flush()
            lines = worker.stderr_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        return next((line.strip() for line in reversed(lines) if line.strip()), None)

    def _ensure_terminal_worker_status(self, worker: WorkerHandle) -> None:
        if worker.process is None:
            return
        current = load_status(worker.status_file) or {}
        returncode = worker.process.poll()
        if returncode not in (None, 0) and current.get("status") not in {"failed", "killed", "unsupported"}:
            self._write_forced_status(worker, "failed", f"worker exited with return code {returncode}")
        elif returncode == 0 and current.get("status") not in {"complete", "disabled", "stopped", "failed", "killed", "unsupported"}:
            self._write_forced_status(worker, "failed", "worker exited without a terminal status")

    def _write_summary(self) -> None:
        statuses = {}
        for name in ("recorder", "realtime_processor", "dashboard", "offline_analyzer"):
            statuses[name] = load_status(self.paths.process_logs / f"{name}.status.json") or {
                "name": name,
                "status": "missing",
            }
        failed = [
            name
            for name, status in statuses.items()
            if name != "dashboard" and status.get("status") in {"failed", "killed", "unsupported"}
        ]
        validity_failures = _pipeline_validity_failures(statuses, self.processes, self.config)
        failed.extend(name for name in validity_failures if name not in failed)
        overall = "failed" if failed else "complete"
        notes = [f"{name} status is {statuses[name].get('status')}" for name in failed if name in statuses]
        notes.extend(detail for name, detail in validity_failures.items() if detail not in notes)
        self._summary = FeedbackManagerSummary(
            status=overall,
            processes=statuses,
            normalized_processes=self.processes,
            manager_summary_file=str(self.paths.manager_summary),
            notes=notes,
        )
        self.paths.manager_summary.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.manager_summary.open("w", encoding="utf-8") as handle:
            json.dump(self.summary(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        self.telemetry.emit(
            "manager.summary",
            level="default",
            message=f"Feedback manager summary: {overall}",
            metadata=self.summary(),
        )

    def _patch_manifest(self) -> None:
        if not self.paths.manifest.exists():
            return
        with self.paths.manifest.open("r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest["normalized_processes"] = self.processes
        manifest["feedback_manager_summary"] = str(self.paths.manager_summary)
        with self.paths.manifest.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def _python_executable(self) -> str:
        return str(self.config.get("runtime", {}).get("python") or sys.executable)

    def _worker_env(self) -> dict[str, str]:
        env = os.environ.copy()
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(PROJECT_ROOT) if not existing else f"{PROJECT_ROOT}{os.pathsep}{existing}"
        return env

    def _close_worker_logs(self, worker: WorkerHandle) -> None:
        failures = []
        for handle_name in ("stdout_handle", "stderr_handle"):
            handle = getattr(worker, handle_name)
            if handle is not None:
                try:
                    handle.close()
                except Exception as exc:
                    failures.append(f"{handle_name}: {type(exc).__name__}: {exc}")
                finally:
                    setattr(worker, handle_name, None)
        if failures:
            raise RuntimeError("; ".join(failures))


def normalize_processes(config: dict[str, Any], record_eeg: bool = True) -> dict[str, Any]:
    components = config.get("experiment", {}).get("components", {})
    process_config = config.get("processes", {})
    realtime = config.get("realtime", {})

    recorder = dict(process_config.get("recorder", {}))
    recorder_backend = recorder.get("backend") or components.get("eeg_recorder", "disabled")
    recorder_enabled = bool(recorder.get("enabled", recorder_backend not in {"disabled", "none"})) and record_eeg
    if not record_eeg:
        recorder_backend = "disabled"
    recorder_tail_guard = float(recorder.get("tail_guard_seconds", 1.0))

    realtime_proc = dict(process_config.get("realtime_processor", {}))
    realtime_component = components.get("realtime_processor", "disabled")
    realtime_enabled_default = bool(realtime.get("enabled", False)) and realtime_component not in {"disabled", "none"}
    realtime_enabled = bool(realtime_proc.get("enabled", realtime_enabled_default))
    realtime_backend = realtime_proc.get("backend") or ("lsl" if realtime_enabled else "disabled")
    preprocessing = realtime.get("preprocessing", {})
    model = realtime.get("model", {})

    feedback = dict(process_config.get("feedback", {}))
    feedback_component = components.get("feedback", "disabled")
    feedback_enabled = bool(feedback.get("enabled", feedback_component not in {"disabled", "none"}))
    feedback_backend = feedback.get("backend") or realtime.get("feedback", {}).get("emitter")
    if not feedback_backend:
        feedback_backend = "lsl" if feedback_enabled else "disabled"

    analyzer = dict(process_config.get("offline_analyzer", {}))
    analyzer_backend = analyzer.get("backend") or components.get("analysis", "minimal")
    analyzer_enabled = bool(analyzer.get("enabled", analyzer_backend not in {"disabled", "none"}))
    dashboard = dict(process_config.get("dashboard", {}))
    dashboard_config = dict(realtime.get("dashboard", {}))
    dashboard_enabled = bool(dashboard.get("enabled", dashboard_config.get("enabled", False)))
    dashboard_backend = str(dashboard.get("backend", "http" if dashboard_enabled else "disabled"))

    return {
        "recorder": {
            "enabled": recorder_enabled,
            "backend": recorder_backend,
            "csv_mirror": bool(recorder.get("csv_mirror", recorder_backend == "lsl_csv")),
            "lsl_sample_heartbeat": bool(
                recorder.get("lsl_sample_heartbeat", recorder_backend == "labrecorder_xdf")
            ),
            "executable": str(recorder.get("executable", "LabRecorder.exe")),
            "rcs_port": int(recorder.get("rcs_port", 22345)),
            "startup_timeout_seconds": float(
                recorder.get("startup_timeout_seconds", 20.0 if recorder_backend == "labrecorder_xdf" else 8.0)
            ),
            "finalization_status_interval_seconds": float(
                recorder.get("finalization_status_interval_seconds", 5.0)
            ),
            "xdf_growth_warning_seconds": float(
                recorder.get(
                    "xdf_growth_warning_seconds",
                    recorder.get("xdf_stall_timeout_seconds", 15.0),
                )
            ),
            "tail_guard_seconds": recorder_tail_guard,
            "maximum_xdf_tail_shortfall_warning_seconds": float(
                recorder.get("maximum_xdf_tail_shortfall_warning_seconds", 2.0)
            ),
        },
        "realtime_processor": {
            "enabled": realtime_enabled,
            "backend": realtime_backend,
            "preprocessor": realtime_proc.get("preprocessor") or preprocessing.get("kind", "causal_bandpass_notch"),
            "model": realtime_proc.get("model") or model.get("kind", "erp_peak_baseline"),
            "startup_timeout_seconds": float(realtime_proc.get("startup_timeout_seconds", 8.0)),
        },
        "feedback": {
            "enabled": feedback_enabled,
            "backend": feedback_backend,
        },
        "dashboard": {
            "enabled": dashboard_enabled,
            "backend": dashboard_backend,
            "host": str(dashboard.get("host", dashboard_config.get("host", "127.0.0.1"))),
            "port": int(dashboard.get("port", dashboard_config.get("port", 8765))),
            "mode": "demo" if bool(dict(dashboard_config.get("demo", {})).get("enabled", False)) else "classifier",
            "startup_timeout_seconds": float(dashboard.get("startup_timeout_seconds", 4.0)),
        },
        "offline_analyzer": {
            "enabled": analyzer_enabled,
            "backend": analyzer_backend,
            "timeout_seconds": float(analyzer.get("timeout_seconds", 120.0)),
        },
    }


def _pipeline_validity_failures(
    statuses: dict[str, dict[str, Any]],
    processes: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, str]:
    """Return enabled process outputs that are present but not operationally valid."""
    failures: dict[str, str] = {}
    recorder = statuses.get("recorder", {})
    if processes.get("recorder", {}).get("enabled") and _status_metric(recorder, "sample_count") <= 0:
        failures["recorder"] = "recorder produced no EEG samples"

    realtime = statuses.get("realtime_processor", {})
    if not processes.get("realtime_processor", {}).get("enabled"):
        return failures
    if _status_metric(realtime, "sample_count") <= 0:
        failures["realtime_processor"] = "realtime processor received no EEG samples"
        return failures
    realtime_config = config.get("realtime", {})
    if bool(realtime_config.get("alpha", {}).get("enabled", False)) and _status_metric(realtime, "alpha_estimate_count") <= 0:
        failures["realtime_processor"] = "realtime alpha was enabled but produced no estimates"
    if bool(realtime_config.get("event_features", {}).get("enabled", False)) and _status_metric(realtime, "event_feature_packet_count") <= 0:
        failures["realtime_processor"] = "realtime staged event features were enabled but produced no packets"
    inference_enabled = bool(realtime_config.get("inference", {}).get("enabled", True))
    if (
        inference_enabled
        and bool(realtime_config.get("epoching", {}).get("enabled", False))
        and _status_metric(realtime, "marker_count") <= 0
    ):
        failures["realtime_processor"] = "realtime epoching was enabled but received no task markers"
    elif (
        inference_enabled
        and bool(realtime_config.get("epoching", {}).get("enabled", False))
        and _status_metric(realtime, "epoch_count") <= 0
    ):
        failures["realtime_processor"] = "realtime epoching received markers but produced no usable epochs"
    elif (
        bool(realtime_config.get("epoching", {}).get("enabled", False))
        and bool(realtime_config.get("classifier", {}).get("enabled", False))
        and inference_enabled
        and _status_metric(realtime, "classifier_prediction_count") <= 0
        and _status_metric(realtime, "classifier_rejected_epoch_count") <= 0
    ):
        failures["realtime_processor"] = "classifier inference produced neither predictions nor explicit rejections"
    elif (
        bool(realtime_config.get("classifier", {}).get("enabled", False))
        and inference_enabled
        and (
            _status_metric(realtime, "classifier_predicted_epoch_count")
            + _status_metric(realtime, "classifier_rejected_epoch_count")
            < _status_metric(realtime, "eligible_marker_count")
        )
    ):
        failures["realtime_processor"] = "classifier did not account for every eligible marker"
    return failures


def _status_metric(status: dict[str, Any], key: str) -> int:
    value = status.get(key)
    if value is None and isinstance(status.get("summary"), dict):
        value = status["summary"].get(key)
    return int(value or 0)
