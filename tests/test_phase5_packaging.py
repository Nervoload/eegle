from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

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
            for filename in (
                "pyproject.toml",
                "setup.py",
                "MANIFEST.in",
                "README.md",
            ):
                shutil.copy2(ROOT / filename, project / filename)
            shutil.copytree(
                ROOT / "eegle",
                project / "eegle",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            wheel_dir = root / "wheel"
            wheel_dir.mkdir()
            # Keep this consumer-like: the build must provision the exact
            # backend declared by the copied project's pyproject.toml.
            built = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    ".",
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

            sdist_dir = root / "sdist"
            sdist_dir.mkdir()
            built_sdist = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--sdist",
                    "--outdir",
                    str(sdist_dir),
                ],
                cwd=project,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                built_sdist.returncode,
                0,
                built_sdist.stdout + built_sdist.stderr,
            )
            sdists = tuple(sdist_dir.glob("eegle-*.tar.gz"))
            self.assertEqual(len(sdists), 1)
            with tarfile.open(sdists[0], "r:gz") as archive:
                sdist_names = {
                    "/".join(Path(value.name).parts[1:])
                    for value in archive.getmembers()
                }
            self.assertFalse(LEGACY_ROOT_WHEEL_PATHS & sdist_names)
            self.assertFalse(any(name.startswith("eegle/ml/") for name in sdist_names))
            self.assertFalse(any(name.startswith("tests/") for name in sdist_names))
            self.assertIn("eegle/operations/plugin_tools.py", sdist_names)

            installed = root / "installed"
            installed_base = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--target",
                    str(installed),
                    str(wheels[0]),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                installed_base.returncode,
                0,
                installed_base.stdout + installed_base.stderr,
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(installed)
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
                "{new,compile,detect,explain,diff,graph,rehearse,preflight,run,inspect,replay,compare,export,model,plugin}",
                invoked.stdout,
            )
            self.assertNotIn("check-setup", invoked.stdout + invoked.stderr)

            declared_version = tomllib.loads(
                (ROOT / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]["version"]
            installed_version = subprocess.run(
                [sys.executable, "-m", "eegle", "--version"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                installed_version.returncode,
                0,
                installed_version.stdout + installed_version.stderr,
            )
            self.assertEqual(
                installed_version.stdout.strip(),
                f"eegle {declared_version}",
            )

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

            installed_plugin = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--target",
                    str(installed),
                    str(plugin_wheels[0]),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                installed_plugin.returncode,
                0,
                installed_plugin.stdout + installed_plugin.stderr,
            )
            model_environment = environment.copy()
            for command in (
                ("plugin", "inspect", "eegle.example_models.mean_threshold"),
                ("plugin", "check", "eegle.example_models.mean_threshold", "--construct"),
            ):
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

            reference_root = root / "reference-projects"
            shutil.copytree(ROOT / "reference_projects", reference_root)
            for name in (
                "01-recording",
                "02-event-locked-observation",
                "03-model-comparison",
                "04-adaptation",
                "05-simulated-closed-loop",
                "06-lsl-observe-only",
            ):
                reference = reference_root / name
                session_id = f"session.reference.{name.replace('-', '.')}"
                for command in (
                    ("compile", str(reference)),
                    ("preflight", str(reference)),
                    ("run", str(reference), "--session-id", session_id),
                    ("replay", str(reference)),
                ):
                    with self.subTest(reference=name, command=command[0]):
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
                        self.assertTrue(json.loads(completed.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
