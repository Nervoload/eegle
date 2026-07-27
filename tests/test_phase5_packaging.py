from __future__ import annotations

import os
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
    def test_wheel_excludes_legacy_root_modules_and_module_cli_is_inert(self) -> None:
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
            root_modules = {
                name
                for name in names
                if name.startswith("eegle/")
                and name.endswith(".py")
                and name.count("/") == 1
            }
            self.assertEqual(root_modules, V1_ROOT_WHEEL_PATHS)
            self.assertFalse(any(name.endswith("entry_points.txt") for name in names))

            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(wheels[0])
            invoked = subprocess.run(
                [sys.executable, "-m", "eegle"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(invoked.returncode, 2)
            self.assertIn("command-line interface is not available yet", invoked.stderr)
            self.assertNotIn("check-setup", invoked.stdout + invoked.stderr)


if __name__ == "__main__":
    unittest.main()
