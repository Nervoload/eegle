from __future__ import annotations

import io
import json
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QProcess

from demos.workbench.operations import WorkbenchOperations
from demos.workbench.profile import load_study1_profile
from demos.workbench.project import bootstrap_study1_project
from demos.workbench.runner import PROTOCOL as RUNNER_PROTOCOL
from demos.workbench.runner_process import RunnerProcess
from demos.workbench.task_process import TaskProcess
from demos.workbench.task_runner import (
    PROTOCOL as TASK_PROTOCOL,
)
from demos.workbench.task_runner import (
    _command_reader,
)


class RunnerProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name) / "study1"
        bootstrap_study1_project(load_study1_profile(), project_root=cls.root)
        result = WorkbenchOperations().compile_and_preflight(
            cls.root,
            None,
            simulation=True,
        )
        if not result.preflight.ready:
            raise RuntimeError("simulation fixture preflight is not ready")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_runner_orders_ready_running_and_completed_with_structured_run(self) -> None:
        process = self._runner("session.protocol.complete")
        ready = json.loads(process.stdout.readline())
        self.assertEqual(ready["type"], "ready")
        self.assertEqual(ready["schema"], RUNNER_PROTOCOL)
        process.stdin.write("not-json\n")
        process.stdin.write(
            json.dumps({"schema": RUNNER_PROTOCOL, "command": "start"}) + "\n"
        )
        process.stdin.flush()
        messages = []
        while not messages or messages[-1].get("type") != "completed":
            line = process.stdout.readline()
            self.assertTrue(line, process.stderr.read())
            messages.append(json.loads(line))
        process.wait(timeout=15)
        errors = process.stderr.read()
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()

        self.assertEqual(process.returncode, 0, errors)
        self.assertEqual(
            [value["type"] for value in messages],
            ["warning", "state", "completed"],
        )
        self.assertTrue(all(value["schema"] == RUNNER_PROTOCOL for value in messages))
        run = messages[-1]["project_run"]
        self.assertEqual(run["session_id"], "session.protocol.complete")
        self.assertEqual(run["status"], "complete")
        self.assertGreater(run["evidence_record_count"], 0)

    def test_runner_cancel_before_start_never_creates_a_session(self) -> None:
        session_id = "session.protocol.cancel-before-start"
        process = self._runner(session_id)
        ready = json.loads(process.stdout.readline())
        self.assertEqual(ready["type"], "ready")
        process.stdin.write(
            json.dumps(
                {
                    "schema": RUNNER_PROTOCOL,
                    "command": "cancel",
                    "reason": "operator_stop",
                }
            )
            + "\n"
        )
        process.stdin.flush()
        output, _errors = process.communicate(timeout=10)
        message = json.loads(output.strip())

        self.assertEqual(process.returncode, 2)
        self.assertEqual(message["type"], "failed")
        self.assertEqual(message["error"]["code"], "runner.cancelled_before_start")
        self.assertFalse((self.root / "sessions" / session_id).exists())

    def _runner(self, session_id: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "demos.workbench.runner",
                "--project-root",
                str(self.root),
                "--session-id",
                session_id,
                "--mode",
                "simulation",
            ],
            cwd=Path(__file__).resolve().parents[3],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )


class TaskProtocolTests(unittest.TestCase):
    def test_task_reader_rejects_wrong_schema_and_aborts_on_stdin_loss(self) -> None:
        commands: queue.Queue[dict[str, object]] = queue.Queue()
        source = io.StringIO(
            json.dumps({"schema": "wrong", "command": "start"}) + "\n"
        )
        output = io.StringIO()
        with patch("demos.workbench.task_runner.sys.stdin", source), patch(
            "demos.workbench.task_runner.sys.stdout",
            output,
        ):
            _command_reader(commands, threading.Lock())

        warning = json.loads(output.getvalue())
        self.assertEqual(warning["schema"], TASK_PROTOCOL)
        self.assertEqual(warning["type"], "warning")
        command = commands.get_nowait()
        self.assertEqual(command["command"], "abort")
        self.assertEqual(command["reason"], "workbench_disconnected")

    def test_supervisors_treat_silent_zero_exit_as_failure(self) -> None:
        for supervisor in (RunnerProcess(), TaskProcess()):
            messages = []
            supervisor.message_received.connect(messages.append)
            supervisor._finished(0, QProcess.ExitStatus.NormalExit)
            self.assertEqual(messages[-1]["type"], "failed")
            self.assertIn("before a terminal message", messages[-1]["error"]["message"])


if __name__ == "__main__":
    unittest.main()
