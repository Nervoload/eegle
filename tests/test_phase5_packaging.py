from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
LEGACY_ROOT_WHEEL_PATHS = {
    "eegle/cli.py",
    "eegle/components.py",
    "eegle/config.py",
    "eegle/eeg_csv.py",
    "eegle/experiment.py",
    "eegle/factory.py",
    "eegle/feedback_manager.py",
    "eegle/lsl.py",
    "eegle/preflight.py",
    "eegle/psychopy_input.py",
    "eegle/recording_health.py",
    "eegle/session.py",
    "eegle/telemetry.py",
}
V1_ROOT_WHEEL_PATHS = {
    "eegle/__init__.py",
    "eegle/__main__.py",
    "eegle/_domain.py",
    "eegle/_validation.py",
}
P6_REMOVED_WHEEL_PATHS = {
    "eegle/models/builtins.py",
    "eegle/models/bundles.py",
    "eegle/models/registry.py",
    "eegle/models/registry_types.py",
    "eegle/models/roles.py",
    "eegle/models/targets.py",
}


class Phase5PackagingTests(unittest.TestCase):
    def test_wheel_excludes_legacy_roots_and_completes_the_base_cli_journey(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            for filename in ("pyproject.toml", "setup.py", "README.md"):
                shutil.copy2(ROOT / filename, project / filename)
            shutil.copytree(
                ROOT / "eegle",
                project / "eegle",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            wheel_dir = root / "wheel"
            wheel_dir.mkdir()
            built = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    ".",
                    "--no-build-isolation",
                    "--no-deps",
                    "--wheel-dir",
                    str(wheel_dir),
                ],
                cwd=project,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            wheels = tuple(wheel_dir.glob("eegle-*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as archive:
                names = set(archive.namelist())
            self.assertFalse(LEGACY_ROOT_WHEEL_PATHS & names)
            self.assertFalse(P6_REMOVED_WHEEL_PATHS & names)
            self.assertFalse(any(name.startswith("eegle/ml/") for name in names))
            self.assertIn("eegle/authoring/__init__.py", names)
            self.assertIn("eegle/authoring/drafts.py", names)
            self.assertIn("eegle/authoring/_template_profiles.py", names)
            self.assertIn("eegle/authoring/builders.py", names)
            self.assertIn("eegle/authoring/composed_projects.py", names)
            self.assertIn("eegle/authoring/composition.py", names)
            self.assertIn("eegle/authoring/design.py", names)
            self.assertIn("eegle/authoring/design_provenance.py", names)
            self.assertIn("eegle/authoring/lowering.py", names)
            self.assertIn("eegle/authoring/presets.py", names)
            self.assertIn("eegle/authoring/provenance.py", names)
            self.assertIn("eegle/authoring/schemas.py", names)
            self.assertIn("eegle/authoring/templates.py", names)
            self.assertIn("eegle/authoring/yaml.py", names)
            self.assertIn("eegle/operations/__init__.py", names)
            self.assertIn("eegle/operations/contracts.py", names)
            self.assertIn("eegle/operations/diagnostics.py", names)
            self.assertIn("eegle/operations/explanations.py", names)
            self.assertIn("eegle/operations/preflight.py", names)
            self.assertIn("eegle/operations/projects.py", names)
            self.assertIn("eegle/operations/sessions.py", names)
            self.assertIn("eegle/operations/cli.py", names)
            self.assertIn("eegle/models/packaging.py", names)
            self.assertIn("eegle/integrations/__init__.py", names)
            self.assertIn("eegle/integrations/mne.py", names)
            self.assertIn("eegle/integrations/lsl/__init__.py", names)
            self.assertNotIn("eegle/integrations/legacy_sessions.py", names)
            self.assertNotIn("eegle/integrations/task_environment.py", names)
            root_modules = {
                name
                for name in names
                if name.startswith("eegle/")
                and name.endswith(".py")
                and name.count("/") == 1
            }
            self.assertEqual(root_modules, V1_ROOT_WHEEL_PATHS)
            entry_points = [name for name in names if name.endswith("entry_points.txt")]
            self.assertEqual(len(entry_points), 1)
            with zipfile.ZipFile(wheels[0]) as archive:
                entry_point_text = archive.read(entry_points[0]).decode("utf-8")
            self.assertIn("eegle = eegle.__main__:main", entry_point_text)
            self.assertIn("lsl = eegle.integrations.lsl:lsl_plugin_descriptors", entry_point_text)

            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(wheels[0])
            invoked = subprocess.run(
                [sys.executable, "-m", "eegle", "--help"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(invoked.returncode, 0, invoked.stdout + invoked.stderr)
            self.assertIn(
                "{new,compile,detect,explain,diff,graph,rehearse,preflight,run,inspect,replay,compare,export,model}",
                invoked.stdout,
            )
            self.assertNotIn("check-setup", invoked.stdout + invoked.stderr)

            project_root = root / "first-simulation"
            commands = (
                ("new", str(project_root), "--id", "clean-wheel"),
                ("detect", "--no-entry-points"),
                ("detect", "--lsl", "--lsl-wait", "0"),
                ("compile", str(project_root)),
                ("explain", str(project_root)),
                ("diff", str(project_root), str(project_root)),
                ("graph", str(project_root)),
                ("preflight", str(project_root)),
                (
                    "rehearse",
                    str(project_root),
                    "--session-id",
                    "session.clean.rehearsal",
                ),
                (
                    "run",
                    str(project_root),
                    "--session-id",
                    "session.clean.run",
                ),
                ("inspect", str(project_root)),
                ("replay", str(project_root)),
            )
            for command in commands:
                with self.subTest(command=command[0]):
                    completed = subprocess.run(
                        [sys.executable, "-m", "eegle", "--json", *command],
                        cwd=root,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        completed.stdout + completed.stderr,
                    )
                    payload = json.loads(completed.stdout)
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["operation"], command[0])

            plugin_source = ROOT / "examples" / "plugins" / "eegle-example-models"
            plugin_project = root / "eegle-example-models"
            shutil.copytree(plugin_source, plugin_project)
            plugin_wheel_dir = root / "plugin-wheel"
            plugin_wheel_dir.mkdir()
            plugin_build = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    ".",
                    "--no-build-isolation",
                    "--no-deps",
                    "--wheel-dir",
                    str(plugin_wheel_dir),
                ],
                cwd=plugin_project,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                plugin_build.returncode,
                0,
                plugin_build.stdout + plugin_build.stderr,
            )
            plugin_wheels = tuple(plugin_wheel_dir.glob("eegle_example_models-*.whl"))
            self.assertEqual(len(plugin_wheels), 1)

            model_environment = environment.copy()
            model_environment["PYTHONPATH"] = os.pathsep.join(
                (str(wheels[0]), str(plugin_wheels[0]))
            )
            comparison_root = root / "model-comparison"
            model_commands = (
                (
                    "new",
                    str(comparison_root),
                    "--id",
                    "clean-wheel-model-comparison",
                    "--preset",
                    "eegle.preset.model_comparison",
                ),
                ("compile", str(comparison_root)),
                (
                    "run",
                    str(comparison_root),
                    "--session-id",
                    "session.clean.model-comparison",
                ),
            )
            for command in model_commands:
                with self.subTest(clean_plugin_command=command[0]):
                    completed = subprocess.run(
                        [sys.executable, "-m", "eegle", "--json", *command],
                        cwd=root,
                        env=model_environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        completed.stdout + completed.stderr,
                    )
                    payload = json.loads(completed.stdout)
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["operation"], command[0])

            adaptation_root = root / "adaptation"
            adaptation_session = "session.clean.adaptation"
            adaptation_commands = (
                (
                    "new",
                    str(adaptation_root),
                    "--id",
                    "clean-wheel-adaptation",
                    "--preset",
                    "eegle.preset.adaptation",
                    "--grant-simulated-adaptation",
                ),
                ("compile", str(adaptation_root)),
                (
                    "run",
                    str(adaptation_root),
                    "--session-id",
                    adaptation_session,
                ),
            )
            for command in adaptation_commands:
                with self.subTest(clean_adaptation_command=command[0]):
                    completed = subprocess.run(
                        [sys.executable, "-m", "eegle", "--json", *command],
                        cwd=root,
                        env=model_environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        completed.stdout + completed.stderr,
                    )

            session_root = adaptation_root / "sessions" / adaptation_session
            inspected = subprocess.run(
                [sys.executable, "-m", "eegle", "--json", "inspect", str(session_root)],
                cwd=root,
                env=model_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(inspected.returncode, 0, inspected.stdout + inspected.stderr)
            inspection = json.loads(inspected.stdout)["result"]
            self.assertEqual(
                inspection["adaptation"]["transition_status_counts"],
                {"applied": 1, "requested": 1},
            )
            replayed = subprocess.run(
                [sys.executable, "-m", "eegle", "--json", "replay", str(session_root)],
                cwd=root,
                env=model_environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(replayed.returncode, 0, replayed.stdout + replayed.stderr)
            self.assertTrue(json.loads(replayed.stdout)["result"]["equivalent"])


if __name__ == "__main__":
    unittest.main()
