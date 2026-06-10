"""Experiment orchestration for the forward task -> EEG -> data loop."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reproduce.calibration import make_calibration_suite
from reproduce.config import load_config
from reproduce.factory import make_task_component
from reproduce.feedback_manager import FeedbackManager
from reproduce.hardware.system import CheckResult
from reproduce.preflight import run_preflight, write_preflight_report
from reproduce.session import SessionPaths, create_session
from reproduce.tasks.base import TaskRunResult
from reproduce.tasks.registry import get_task_spec
from reproduce.telemetry import Telemetry


@dataclass
class ForwardExperimentResult:
    session_dir: Path
    preflight: list[CheckResult]
    task: TaskRunResult | None
    eeg: dict[str, Any] | None
    analysis: dict[str, Any] | None
    processes: dict[str, Any] | None
    summary_file: Path
    calibration: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        status = "failed" if self.task is None or (self.processes or {}).get("status") == "failed" else "complete"
        return {
            "status": status,
            "session_dir": str(self.session_dir),
            "summary_file": str(self.summary_file),
            "preflight": [result.__dict__ for result in self.preflight],
            "task": None if self.task is None else {
                "task": self.task.task,
                "mode": self.task.mode,
                "summary": self.task.summary,
            },
            "eeg": self.eeg,
            "analysis": self.analysis,
            "processes": self.processes,
            "calibration": self.calibration,
            "telemetry": {
                "telemetry_file": str(self.session_dir / "logs" / "telemetry.jsonl"),
                "debug_file": str(self.session_dir / "logs" / "debug.jsonl"),
            },
        }


class ForwardExperimentRunner:
    """Run the minimal forward experiment without closing the loop."""

    def __init__(
        self,
        config: dict[str, Any],
        task_name: str | None = None,
        task_mode: str = "psychopy",
        participant_id: str | None = None,
        trials: int | None = None,
        record_eeg: bool = True,
        require_eeg: bool | None = None,
        calibration_suite: str | None = None,
        preflight_results: list[CheckResult] | None = None,
    ) -> None:
        self.config = config
        self.task_name = task_name or config.get("experiment", {}).get("task", "pvt")
        self.task_mode = task_mode
        self.participant_id = participant_id
        self.trials = trials
        self.record_eeg = record_eeg
        self.require_eeg = require_eeg
        self.calibration_suite = calibration_suite or config.get("experiment", {}).get("calibration_suite")
        self.preflight_results = preflight_results
        self.telemetry: Telemetry | None = None

    def run(self) -> ForwardExperimentResult:
        get_task_spec(self.task_name)
        paths = create_session(self.config, task=self.task_name, participant_id=self.participant_id)
        # Session creation adds run-unique stream identifiers. All components must
        # use the persisted config so they agree on the exact LSL marker stream.
        self.config = load_config(paths.parameters)
        self.telemetry = Telemetry.from_config(self.config, paths, component="experiment")
        self.telemetry.emit(
            "session.init",
            level="default",
            message=f"Session initialized: {paths.root}",
            metadata={
                "task": self.task_name,
                "task_mode": self.task_mode,
                "record_eeg": self.record_eeg,
                "calibration_suite": self.calibration_suite,
            },
        )
        try:
            if self.preflight_results is None:
                with self.telemetry.span("preflight", component="experiment", message="Preflight checks"):
                    preflight = self._run_preflight(paths)
            else:
                preflight = list(self.preflight_results)
                self.telemetry.emit(
                    "preflight.reused",
                    level="default",
                    message="Using preflight results accepted by the enclosing pipeline",
                    metadata={"statuses": {result.name: result.status for result in preflight}},
                )
            blocking_failures = [result for result in preflight if result.status == "fail"]
            if blocking_failures:
                self.telemetry.emit(
                    "preflight.failed",
                    level="default",
                    message="Preflight failed; task will not start",
                    metadata={"failures": [result.__dict__ for result in blocking_failures]},
                )
                result = ForwardExperimentResult(paths.root, preflight, None, None, None, None, paths.completion_summary)
                self._write_summary(result)
                return result

            manager = FeedbackManager(self.config, paths, record_eeg=self.record_eeg)
            task_result = None
            calibration_result = None
            try:
                if self.calibration_suite:
                    manager.start_recorder()
                    manager.start_quality_recorder()
                    with self.telemetry.span(
                        "calibration",
                        component="experiment",
                        message="Calibration suite",
                        metadata={"suite": self.calibration_suite},
                    ):
                        self.telemetry.emit(
                            "calibration.start",
                            level="default",
                            message=f"Starting calibration suite {self.calibration_suite}",
                            metadata={"suite": self.calibration_suite},
                        )
                        suite = make_calibration_suite(
                            self.calibration_suite,
                            self.config,
                            paths,
                            mode=self.task_mode,
                            record_eeg=self.record_eeg,
                            telemetry=self.telemetry,
                        )
                        calibration_result = suite.run()
                        self.config = suite.apply_result_to_config(calibration_result)
                        manager.reconfigure(self.config)
                        self.telemetry.emit(
                            "calibration.end",
                            level="default",
                            message=f"Calibration suite {self.calibration_suite} ended",
                            metadata=calibration_result,
                        )
                    manager.start_realtime_processor()
                else:
                    manager.start_before_task()
                with self.telemetry.span(
                    "task_run",
                    component="experiment",
                    message="Task run",
                    metadata={"task": self.task_name, "mode": self.task_mode},
                ):
                    self.telemetry.emit(
                        "task.start",
                        level="default",
                        message=f"Starting task {self.task_name}",
                        metadata={"task": self.task_name, "mode": self.task_mode},
                    )
                    task_result = self._run_task(paths)
                    self.telemetry.emit(
                        "task.end",
                        level="default",
                        message=f"Task {self.task_name} ended",
                        metadata={"task": task_result.task, "mode": task_result.mode, "summary": task_result.summary},
                    )
            finally:
                manager.stop_after_task()

            with self.telemetry.span("analysis", component="experiment", message="Post-session analysis"):
                analysis = manager.run_offline_analysis()
            processes = manager.summary()
            result = ForwardExperimentResult(
                paths.root,
                preflight,
                task_result,
                manager.eeg_summary,
                analysis,
                processes,
                paths.completion_summary,
                calibration_result,
            )
            self._write_summary(result)
            result_status = result.as_dict()["status"]
            self.telemetry.emit(
                "session.complete" if result_status == "complete" else "session.failed",
                level="default",
                message=f"Experiment {result_status}: {paths.root}",
                metadata={"status": result_status, "summary_file": str(paths.completion_summary), "session_dir": str(paths.root)},
            )
            return result
        except Exception as exc:
            self.telemetry.emit(
                "fatal_error",
                level="default",
                message=f"Experiment failed: {type(exc).__name__}: {exc}",
                metadata={"exception_type": type(exc).__name__, "exception": str(exc)},
            )
            raise

    def _run_preflight(self, paths: SessionPaths) -> list[CheckResult]:
        eeg_config = self.config.get("hardware", {}).get("eeg", {})
        require_eeg = self.require_eeg
        if require_eeg is None:
            require_eeg = self.record_eeg and bool(eeg_config.get("required_for_run", False))
        lsl_wait = float(eeg_config.get("stream_timeout_seconds", 5.0)) if self.record_eeg else 0.2
        if self.telemetry is not None:
            self.telemetry.emit(
                "preflight.start",
                level="default",
                message="Running preflight checks",
                metadata={"lsl_wait_seconds": lsl_wait, "require_eeg": require_eeg, "record_eeg": self.record_eeg},
            )
        results = run_preflight(
            self.config,
            lsl_wait=lsl_wait,
            require_eeg=require_eeg,
        )
        write_preflight_report(results, paths.logs / "preflight.json")
        if self.telemetry is not None:
            statuses = {result.name: result.status for result in results}
            failures = [result.__dict__ for result in results if result.status == "fail"]
            self.telemetry.emit(
                "preflight.complete",
                level="default",
                message="Preflight checks complete",
                metadata={"statuses": statuses, "failures": failures, "report_file": str(paths.logs / "preflight.json")},
            )
        return results

    def _run_task(self, paths: SessionPaths) -> TaskRunResult:
        components = self.config.get("experiment", {}).get("components", {})
        task_component = components.get("task", self.task_name)
        return make_task_component(
            task_component,
            self.config,
            task_mode=self.task_mode,
            trials=self.trials,
            participant_id=self.participant_id,
        ).run(paths=paths)

    def _write_summary(self, result: ForwardExperimentResult) -> None:
        result.summary_file.parent.mkdir(parents=True, exist_ok=True)
        with result.summary_file.open("w", encoding="utf-8") as handle:
            json.dump(result.as_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
