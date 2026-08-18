"""QProcess supervision for the isolated Workbench task presentation."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from demos.workbench.platform_support import (
    REPOSITORY_ROOT,
    child_environment,
    child_interpreter,
)

PROTOCOL = "eegle.workbench.study1_task_protocol.v1"


class TaskProcess(QObject):
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
        environment = QProcessEnvironment()
        for key, value in child_environment().items():
            environment.insert(key, value)
        self.process.setProcessEnvironment(environment)

    @property
    def running(self) -> bool:
        return self.process.state() != QProcess.ProcessState.NotRunning

    def launch(
        self,
        project_root: Path,
        *,
        session_id: str,
        participant: str = "DEMO-001",
        variant: str = "demo_rehearsal",
        controlled_start: bool = True,
        renderer: str = "qt_preview",
        python_executable: Path | None = None,
    ) -> None:
        if self.running:
            self.message_received.emit(
                {"type": "warning", "code": "task.already_open", "message": "The task is already open."}
            )
            return
        session_root = project_root / "task_sessions" / session_id
        arguments = [
            "-X",
            "utf8",
            "-m",
            "demos.workbench.task_runner",
            "--variant",
            variant,
            "--participant",
            participant,
            "--session-id",
            session_id,
            "--session-root",
            str(session_root),
            "--renderer",
            renderer,
        ]
        if controlled_start:
            arguments.append("--controlled-start")
        self._buffer = ""
        self._terminal_message = False
        self.process.setWorkingDirectory(str(REPOSITORY_ROOT))
        self.process.start(child_interpreter(python_executable), arguments)

    def start_task(self) -> None:
        self.send({"command": "start"})

    def abort(self, reason: str = "operator") -> None:
        self.send({"command": "abort", "reason": reason})

    def shutdown(self) -> None:
        if not self.running:
            return
        self.abort("workbench_closed")
        if not self.process.waitForFinished(3000):
            self.process.terminate()
            if not self.process.waitForFinished(1500):
                # terminate() posts WM_CLOSE on Windows, which a console child
                # without a message loop never handles; kill() is the only
                # reliable stop there.
                self.process.kill()
                self.process.waitForFinished(1500)

    def send(self, payload: dict[str, object]) -> None:
        if not self.running:
            return
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
                    raise TypeError("task message is not an object")
                if payload.get("schema") != PROTOCOL:
                    raise TypeError("task message has an unsupported schema")
            except (json.JSONDecodeError, TypeError) as exc:
                payload = {
                    "type": "warning",
                    "code": "task.invalid_message",
                    "message": str(exc),
                }
            if payload.get("type") in {"completed", "aborted", "failed"}:
                self._terminal_message = True
            self.message_received.emit(payload)

    def _read_stderr(self) -> None:
        message = bytes(self.process.readAllStandardError()).decode(errors="replace").strip()
        if message:
            self.message_received.emit(
                {"type": "warning", "code": "task.stderr", "message": message[-1000:]}
            )

    def _process_error(self, error: QProcess.ProcessError) -> None:
        self._terminal_message = True
        self.message_received.emit(
            {
                "type": "failed",
                "error": {
                    "code": "task.process_error",
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
                        "code": "task.unexpected_exit",
                        "message": f"Task process exited with code {exit_code} before a terminal message.",
                    },
                }
            )
