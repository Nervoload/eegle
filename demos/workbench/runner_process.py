"""QProcess supervision for the authoritative EEGle runner."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal

PROTOCOL = "eegle.workbench.runner_protocol.v1"


class RunnerProcess(QObject):
    message_received = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.errorOccurred.connect(self._process_error)
        self.process.finished.connect(self._finished)
        self._buffer = ""
        self._terminal_message = False

    @property
    def running(self) -> bool:
        return self.process.state() != QProcess.ProcessState.NotRunning

    def launch(self, project_root: Path, *, session_id: str, mode: str) -> None:
        if self.running:
            self.message_received.emit(
                {"type": "warning", "code": "runner.already_open", "message": "A runner is already open."}
            )
            return
        self._buffer = ""
        self._terminal_message = False
        self.process.setWorkingDirectory(str(Path(__file__).resolve().parents[2]))
        self.process.start(
            sys.executable,
            [
                "-m",
                "demos.workbench.runner",
                "--project-root",
                str(project_root),
                "--session-id",
                session_id,
                "--mode",
                mode,
            ],
        )

    def start_run(self) -> None:
        self.send({"command": "start"})

    def complete(self, reason: str = "task_complete") -> None:
        self.send({"command": "complete", "reason": reason})

    def cancel(self, reason: str = "operator_stop") -> None:
        self.send({"command": "cancel", "reason": reason})

    def shutdown(self) -> None:
        if not self.running:
            return
        self.cancel("workbench_closed")
        if not self.process.waitForFinished(5000):
            self.process.terminate()
            self.process.waitForFinished(1500)

    def send(self, payload: dict[str, object]) -> None:
        if self.running:
            message = {"schema": PROTOCOL, **payload}
            self.process.write(
                (json.dumps(message, sort_keys=True, separators=(",", ":")) + "\n").encode()
            )

    def _read_stdout(self) -> None:
        self._buffer += bytes(self.process.readAllStandardOutput()).decode(errors="replace")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise TypeError("runner message is not an object")
                if payload.get("schema") != PROTOCOL:
                    raise TypeError("runner message has an unsupported schema")
            except (json.JSONDecodeError, TypeError) as exc:
                payload = {
                    "type": "warning",
                    "code": "runner.invalid_message",
                    "message": str(exc),
                }
            if payload.get("type") in {"completed", "failed"}:
                self._terminal_message = True
            self.message_received.emit(payload)

    def _read_stderr(self) -> None:
        message = bytes(self.process.readAllStandardError()).decode(errors="replace").strip()
        if message:
            self.message_received.emit(
                {"type": "warning", "code": "runner.stderr", "message": message[-1600:]}
            )

    def _process_error(self, error: QProcess.ProcessError) -> None:
        self._terminal_message = True
        self.message_received.emit(
            {
                "type": "failed",
                "error": {
                    "code": "runner.process_error",
                    "message": f"{error.name}: {self.process.errorString()}",
                },
            }
        )

    def _finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if not self._terminal_message:
            self.message_received.emit(
                {
                    "type": "failed",
                    "error": {
                        "code": "runner.unexpected_exit",
                        "message": f"Runner process exited with code {exit_code} before a terminal message.",
                    },
                }
            )
