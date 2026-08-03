from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from eegle.operations import (
    PROJECT_GRAPH_SCHEMA_ID,
    PROJECT_MANIFEST_SCHEMA_ID,
    SESSION_INSPECTION_SCHEMA_ID,
    ExitCode,
    OperationError,
    compile_project,
    create_project,
    diff_projects,
    explain_project,
    graph_project,
    inspect_session,
    open_project,
    rehearse_project,
    replay_session,
    run_locked_plan,
    run_project,
)
from eegle.operations.cli import COMMAND_RESULT_SCHEMA_ID
from eegle.operations.cli import main as cli_main

ROOT = Path(__file__).resolve().parents[1]


class Phase7ProjectCliTests(unittest.TestCase):
    def test_scaffold_separates_sources_generated_bindings_locks_and_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            project = create_project(root, project_id="first-simulation")

            self.assertEqual(project.manifest.schema, PROJECT_MANIFEST_SCHEMA_ID)
            self.assertTrue((root / "authoring" / "experiment.json").is_file())
            self.assertTrue((root / "generated" / "protocol.json").is_file())
            self.assertTrue((root / "deployments" / "simulation.json").is_file())
            self.assertFalse((root / "locks").exists())
            roles = {value.role: value for value in project.manifest.artifacts}
            self.assertFalse(roles["authoring_source"].immutable)
            self.assertFalse(roles["simulation_deployment"].immutable)
            with self.assertRaises(FileExistsError):
                create_project(root, project_id="replacement")

            compiled = compile_project(root)
            self.assertTrue(compiled.project.path_for("execution_plan").is_file())
            self.assertTrue(compiled.project.path_for("execution_lock").is_file())
            self.assertTrue(
                compiled.project.manifest.artifact("execution_plan").immutable
            )
            self.assertTrue(
                compiled.project.manifest.artifact("execution_lock").immutable
            )
            repeated = compile_project(root)
            self.assertEqual(repeated.plan.plan_hash, compiled.plan.plan_hash)
            self.assertEqual(repeated.lock.lock_hash, compiled.lock.lock_hash)

    def test_python_services_complete_first_simulation_journey(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            create_project(root, project_id="service-journey")
            compiled = compile_project(root)
            explanation = explain_project(root)
            graph = graph_project(root)
            difference = diff_projects(root, root)
            rehearsal = rehearse_project(
                root,
                session_id="session.rehearsal.service",
            )
            run = run_project(root, session_id="session.run.service")
            inspection = inspect_session(run.session_root)
            replay = replay_session(run.session_root)

            self.assertEqual(
                explanation["compiler_projection"]["plan_hash"],
                compiled.plan.plan_hash,
            )
            self.assertTrue(rehearsal.successful)
            self.assertTrue(run.successful)
            self.assertEqual(inspection.to_payload()["schema"], SESSION_INSPECTION_SCHEMA_ID)
            self.assertTrue(inspection.valid)
            self.assertTrue(replay.equivalent)
            self.assertEqual(replay.result_status.value, "complete")
            self.assertEqual(graph["schema"], PROJECT_GRAPH_SCHEMA_ID)
            self.assertEqual(graph["plan_hash"], compiled.plan.plan_hash)
            self.assertTrue(difference["equivalent"])
            project = open_project(root)
            self.assertEqual(
                project.manifest.session_uris,
                (
                    "sessions/session.rehearsal.service",
                    "sessions/session.run.service",
                ),
            )

    def test_compile_publishes_one_revision_and_failure_preserves_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            create_project(
                root,
                project_id="transactional-compile",
                parameters={"sample_rate_hz": 128.0},
            )
            compiled = compile_project(root)
            generated_roles = (
                "authoring_project",
                "protocol",
                "suite",
                "deployment_requirements",
            )
            generated = {
                role: compiled.project.manifest.artifact(role)
                for role in generated_roles
            }
            parents = {
                Path(value.uri).parent for value in generated.values()
            }
            self.assertEqual(len(parents), 1)
            revision = next(iter(parents))
            self.assertEqual(revision.parts[:2], ("generated", "revisions"))
            before_manifest = (root / "eegle-project.json").read_bytes()
            before_payloads = {
                role: compiled.project.path_for(role).read_bytes()
                for role in generated_roles
            }

            source_path = root / "authoring" / "experiment.json"
            source = json.loads(source_path.read_text(encoding="utf-8"))
            source["template"]["parameters"]["sample_rate_hz"] = 250.0
            source_path.write_text(json.dumps(source), encoding="utf-8")

            with self.assertRaises(OperationError):
                compile_project(root)

            self.assertEqual(
                (root / "eegle-project.json").read_bytes(),
                before_manifest,
            )
            preserved = open_project(root)
            self.assertEqual(
                {role: preserved.manifest.artifact(role) for role in generated_roles},
                generated,
            )
            self.assertEqual(
                {
                    role: preserved.path_for(role).read_bytes()
                    for role in generated_roles
                },
                before_payloads,
            )

    def test_run_uses_locked_artifacts_and_not_a_mutated_authoring_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            create_project(root, project_id="locked-run")
            compiled = compile_project(root)
            source_path = root / "authoring" / "experiment.json"
            source = json.loads(source_path.read_text(encoding="utf-8"))
            source["template"]["parameters"]["sample_rate_hz"] = 250.0
            source_path.write_text(json.dumps(source), encoding="utf-8")
            source_path.unlink()

            result = run_project(root, session_id="session.locked.source-change")

            self.assertTrue(result.successful)
            self.assertEqual(result.plan_hash, compiled.plan.plan_hash)

    def test_tampered_lock_fails_before_a_session_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            create_project(root, project_id="tamper")
            compiled = compile_project(root)
            lock_path = compiled.project.path_for("execution_lock")
            tampered_path = root / "tampered-lock.json"
            payload = json.loads(lock_path.read_text(encoding="utf-8"))
            payload["plan_hash"] = "sha256:" + "0" * 64
            tampered_path.write_text(json.dumps(payload), encoding="utf-8")
            session_root = root / "sessions" / "session.must-not-exist"

            with self.assertRaises(OperationError) as caught:
                run_locked_plan(
                    compiled.project.path_for("execution_plan"),
                    tampered_path,
                    session_root,
                    session_id="session.must-not-exist",
                )

            self.assertEqual(caught.exception.exit_code, ExitCode.INTEGRITY_FAILED)
            self.assertFalse(session_root.exists())

    def test_cli_machine_output_and_stable_exit_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            for arguments, operation in (
                (["--json", "new", str(root), "--id", "cli-journey"], "new"),
                (["--json", "compile", str(root)], "compile"),
                (["--json", "graph", str(root)], "graph"),
                (["--json", "diff", str(root), str(root)], "diff"),
                (
                    [
                        "--json",
                        "run",
                        str(root),
                        "--session-id",
                        "session.cli.journey",
                    ],
                    "run",
                ),
                (["--json", "inspect", str(root)], "inspect"),
                (["--json", "replay", str(root)], "replay"),
                (["--json", "validate", str(root), "--strict"], "validate"),
            ):
                code, output, errors = _invoke_cli(arguments)
                payload = json.loads(output)
                self.assertEqual(code, ExitCode.SUCCESS, errors)
                self.assertEqual(payload["schema"], COMMAND_RESULT_SCHEMA_ID)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["operation"], operation)

            code, output, _ = _invoke_cli(["--json", "compile", str(root / "missing")])
            payload = json.loads(output)
            self.assertEqual(code, ExitCode.INVALID_INPUT)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["exit_code"], int(ExitCode.INVALID_INPUT))

            code, output, _ = _invoke_cli(["--json", "unknown"])
            payload = json.loads(output)
            self.assertEqual(code, ExitCode.USAGE_ERROR)
            self.assertEqual(payload["exit_code"], int(ExitCode.USAGE_ERROR))

            invalid_root = Path(directory) / "invalid"
            code, output, _ = _invoke_cli(
                [
                    "--json",
                    "new",
                    str(invalid_root),
                    "--id",
                    "invalid-authoring",
                    "--set",
                    "channel_count=0",
                ]
            )
            payload = json.loads(output)
            self.assertEqual(code, ExitCode.INVALID_INPUT)
            self.assertEqual(payload["operation"], "new")

    def test_module_cli_has_no_repository_root_assumption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            project = workspace / "elsewhere" / "simulation"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT)
            created = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "eegle",
                    "--json",
                    "new",
                    str(project),
                    "--id",
                    "outside-repository",
                ],
                cwd=workspace,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(created.returncode, ExitCode.SUCCESS, created.stderr)
            self.assertTrue(json.loads(created.stdout)["ok"])
            self.assertTrue((project / "eegle-project.json").is_file())


def _invoke_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli_main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
