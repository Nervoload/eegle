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

from reproduce.runtime import PROJECT_ROOT
from reproduce.session import SessionPaths
from reproduce.telemetry import Telemetry
from reproduce.workers.common import StatusWriter, load_status


@dataclass
class WorkerHandle:
    name: str
    backend: str
    module: str
    command: list[str]
    status_file: Path
    stdout_file: Path
    stderr_file: Path
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
        self.start_quality_recorder()
        self.start_realtime_processor()
        self.start_dashboard()

    def start_recorder(self) -> None:
        """Start only the EEG recorder process when enabled."""
        recorder = self.processes["recorder"]

        if recorder["enabled"]:
            self._start_worker(
                "recorder",
                "reproduce.workers.recorder",
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
                "reproduce.workers.realtime_processor",
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

    def start_quality_recorder(self) -> None:
        """Start the optional low-rate Enobio contact/impedance stream recorder."""
        quality = self.processes["quality_recorder"]
        if not quality["enabled"]:
            self._write_disabled_status("quality_recorder", quality["backend"], "quality recorder disabled")
            return
        try:
            self._start_worker(
                "quality_recorder",
                "reproduce.workers.quality_recorder",
                ["--backend", quality["backend"]],
                wait_states={"recording", "failed", "unsupported", "stopped"},
                timeout_seconds=float(quality.get("startup_timeout_seconds", 8.0)),
            )
        except Exception:
            if quality.get("required"):
                raise

    def start_dashboard(self) -> None:
        """Start the optional non-critical localhost classifier dashboard."""
        dashboard = self.processes["dashboard"]
        if not dashboard["enabled"]:
            self._write_disabled_status("dashboard", dashboard["backend"], "dashboard disabled by experiment settings")
            return
        try:
            self._start_worker(
                "dashboard",
                "reproduce.workers.dashboard",
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
        except Exception as exc:
            self.telemetry.emit(
                "dashboard.failed_noncritical",
                level="default",
                message="Classifier dashboard failed; experiment will continue",
                metadata={"exception_type": type(exc).__name__, "exception": str(exc)},
            )

    def stop_after_task(self) -> None:
        self.telemetry.emit("manager.stop", level="default", message="Feedback manager stopping processes")
        for name in ("dashboard", "realtime_processor", "quality_recorder", "recorder"):
            worker = self._workers.get(name)
            if worker is not None:
                self._stop_worker(worker)

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
            "reproduce.workers.offline_analyzer",
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
        worker.stdout_handle = worker.stdout_file.open("ab")
        worker.stderr_handle = worker.stderr_file.open("ab")
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
        worker.started_at_monotonic = monotonic()
        self._workers[name] = worker
        ready_status = self._wait_for_status(worker.status_file, wait_states, timeout_seconds)
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
                raise RuntimeError(f"{name} failed during startup: {ready_status}")
        else:
            self.telemetry.emit(
                "process.timeout",
                level="default",
                message=f"{name} did not report ready before timeout",
                metadata={"status_file": str(worker.status_file), "timeout_seconds": timeout_seconds, "latest_status": ready_status},
            )
            raise RuntimeError(f"{name} did not become ready within {timeout_seconds:.1f}s")

    def _run_worker_to_completion(self, worker: WorkerHandle, timeout_seconds: float) -> None:
        worker.stdout_handle = worker.stdout_file.open("ab")
        worker.stderr_handle = worker.stderr_file.open("ab")
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
            try:
                worker.process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                self.telemetry.emit(
                    "process.timeout",
                    level="default",
                    message=f"{worker.name} timed out",
                    metadata={"timeout_seconds": timeout_seconds, "command": worker.command},
                )
                worker.process.terminate()
                try:
                    worker.process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    worker.process.kill()
                    worker.process.wait(timeout=5.0)
                self._write_forced_status(worker, "killed", "offline analyzer timed out")
            finally:
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
        if worker.process.poll() is None:
            self.telemetry.emit(
                "process.stop",
                level="default",
                message=f"Stopping {worker.name}",
                metadata={"name": worker.name, "backend": worker.backend},
            )
            worker.process.terminate()
            try:
                worker.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.telemetry.emit(
                    "process.timeout",
                    level="default",
                    message=f"{worker.name} did not stop after SIGTERM",
                    metadata={"name": worker.name, "backend": worker.backend},
                )
                worker.process.kill()
                self._write_forced_status(worker, "killed", "worker did not stop after SIGTERM")
                worker.process.wait(timeout=5.0)
        self._ensure_terminal_worker_status(worker)
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
        self._close_worker_logs(worker)

    def _make_worker(self, name: str, module: str, extra_args: list[str]) -> WorkerHandle:
        backend = extra_args[extra_args.index("--backend") + 1] if "--backend" in extra_args else "default"
        status_file = self.paths.process_logs / f"{name}.status.json"
        stdout_file = self.paths.process_logs / f"{name}.stdout.log"
        stderr_file = self.paths.process_logs / f"{name}.stderr.log"
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
        return WorkerHandle(name, backend, module, command, status_file, stdout_file, stderr_file)

    def _wait_for_status(self, path: Path, ready_states: set[str], timeout_seconds: float) -> dict[str, Any] | None:
        deadline = monotonic() + timeout_seconds
        latest = None
        while monotonic() < deadline:
            latest = load_status(path)
            if latest and latest.get("status") in ready_states:
                return latest
            sleep(0.05)
        return latest

    def _write_disabled_status(self, name: str, backend: str, reason: str) -> None:
        writer = StatusWriter(self.paths.process_logs / f"{name}.status.json", name, backend, self.telemetry)
        writer.update("disabled", reason=reason)

    def _write_forced_status(self, worker: WorkerHandle, status: str, reason: str) -> None:
        writer = StatusWriter(worker.status_file, worker.name, worker.backend, self.telemetry)
        writer.update(status, reason=reason, returncode=None if worker.process is None else worker.process.poll())

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
        for name in ("recorder", "quality_recorder", "realtime_processor", "dashboard", "offline_analyzer"):
            statuses[name] = load_status(self.paths.process_logs / f"{name}.status.json") or {
                "name": name,
                "status": "missing",
            }
        failed = [
            name
            for name, status in statuses.items()
            if name not in {"dashboard", "quality_recorder"} and status.get("status") in {"failed", "killed", "unsupported"}
        ]
        quality_status = statuses["quality_recorder"].get("status")
        if self.processes["quality_recorder"].get("required") and quality_status in {"failed", "killed", "unsupported", "missing"}:
            failed.append("quality_recorder")
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
        for handle_name in ("stdout_handle", "stderr_handle"):
            handle = getattr(worker, handle_name)
            if handle is not None:
                handle.close()
                setattr(worker, handle_name, None)


def normalize_processes(config: dict[str, Any], record_eeg: bool = True) -> dict[str, Any]:
    components = config.get("experiment", {}).get("components", {})
    process_config = config.get("processes", {})
    realtime = config.get("realtime", {})

    recorder = dict(process_config.get("recorder", {}))
    recorder_backend = recorder.get("backend") or components.get("eeg_recorder", "disabled")
    recorder_enabled = bool(recorder.get("enabled", recorder_backend not in {"disabled", "none"})) and record_eeg
    if not record_eeg:
        recorder_backend = "disabled"
    quality = dict(process_config.get("quality_recorder", {}))
    quality_config = dict(config.get("hardware", {}).get("quality", {}))
    quality_backend = str(quality.get("backend", "lsl_csv"))
    quality_enabled = bool(quality.get("enabled", quality_config.get("enabled", False))) and record_eeg
    if not record_eeg:
        quality_backend = "disabled"

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
            "startup_timeout_seconds": float(recorder.get("startup_timeout_seconds", 8.0)),
        },
        "quality_recorder": {
            "enabled": quality_enabled,
            "backend": quality_backend,
            "required": bool(quality.get("required", quality_config.get("required_for_run", False))),
            "startup_timeout_seconds": float(quality.get("startup_timeout_seconds", 8.0)),
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
    quality = statuses.get("quality_recorder", {})
    if (
        processes.get("quality_recorder", {}).get("enabled")
        and processes.get("quality_recorder", {}).get("required")
        and _status_metric(quality, "sample_count") <= 0
    ):
        failures["quality_recorder"] = "required quality recorder produced no samples"

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
    if bool(realtime_config.get("epoching", {}).get("enabled", False)) and _status_metric(realtime, "marker_count") <= 0:
        failures["realtime_processor"] = "realtime epoching was enabled but received no task markers"
    elif bool(realtime_config.get("epoching", {}).get("enabled", False)) and _status_metric(realtime, "epoch_count") <= 0:
        failures["realtime_processor"] = "realtime epoching received markers but produced no usable epochs"
    elif (
        bool(realtime_config.get("epoching", {}).get("enabled", False))
        and bool(realtime_config.get("classifier", {}).get("enabled", False))
        and bool(realtime_config.get("inference", {}).get("enabled", True))
        and _status_metric(realtime, "classifier_prediction_count") <= 0
        and _status_metric(realtime, "classifier_rejected_epoch_count") <= 0
    ):
        failures["realtime_processor"] = "classifier inference produced neither predictions nor explicit rejections"
    elif (
        bool(realtime_config.get("classifier", {}).get("enabled", False))
        and bool(realtime_config.get("inference", {}).get("enabled", True))
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
