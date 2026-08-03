from __future__ import annotations

import io
import tomllib
import unittest
from contextlib import redirect_stdout
from importlib.metadata import version
from pathlib import Path

import eegle
from eegle.operations.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]


class Phase7ReleaseIntegrityTests(unittest.TestCase):
    def test_distribution_metadata_is_the_runtime_version_authority(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        declared = pyproject["project"]["version"]

        self.assertEqual(eegle.__version__, declared)
        self.assertEqual(version("eegle"), declared)
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            build_parser().parse_args(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"eegle {declared}")

        package_source = (ROOT / "eegle" / "__init__.py").read_text(encoding="utf-8")
        cli_source = (ROOT / "eegle" / "operations" / "cli.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(f'__version__ = "{declared}"', package_source)
        self.assertNotIn(f'%(prog)s {declared}', cli_source)

    def test_release_identity_and_stage_are_truthful(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = pyproject["project"]
        self.assertIn("Development Status :: 2 - Pre-Alpha", project["classifiers"])
        self.assertNotIn("Development Status :: 3 - Alpha", project["classifiers"])
        self.assertEqual(project["license"], "MIT")
        self.assertEqual(project["license-files"], ["LICENSE"])
        self.assertNotIn(
            "License :: OSI Approved :: MIT License",
            project["classifiers"],
        )
        self.assertEqual(
            project["urls"]["Documentation"],
            "https://github.com/Nervoload/eegle/tree/main/docs",
        )
        citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
        self.assertIn(f'version: "{project["version"]}"', citation)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("pre-alpha, simulation-first candidate", readme)
        self.assertIn("editable installation is a\ncontributor workflow", readme)
        self.assertNotIn("git switch split", readme)

    def test_public_api_names_only_the_current_surface(self) -> None:
        public_api = (ROOT / "docs" / "api" / "PUBLIC_API.md").read_text(
            encoding="utf-8"
        )
        for removed in (
            "eegle.core",
            "eegle.protocols",
            "eegle.realtime",
            "ModelBundle",
            "register_model_spec",
        ):
            self.assertNotIn(removed, public_api)
        self.assertIn("eegle.operations", public_api)
        self.assertIn("eegle plugin", public_api)
        self.assertIn("belongs to Phase 8", public_api)

    def test_release_workflow_separates_installed_artifact_evidence(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "package.yml").read_text(
            encoding="utf-8"
        )
        for job in (
            "wheel-smoke:",
            "sdist-smoke:",
            "optional-smoke:",
            "plugin-smoke:",
            "reference-project-smoke:",
        ):
            self.assertIn(job, workflow)
        self.assertIn("working-directory: ${{ runner.temp }}", workflow)
        self.assertIn(
            "needs: [wheel-smoke, sdist-smoke, optional-smoke, plugin-smoke, reference-project-smoke]",
            workflow,
        )
        self.assertGreaterEqual(workflow.count("forbidden = ('eegle.analysis'"), 2)
        self.assertIn(
            'pip install "pylsl>=1.18,<2" "mne>=1.12,<2"',
            workflow,
        )
        self.assertIn("tests/fixtures/phase7_native_lsl_smoke.py", workflow)
        self.assertIn("for name in 01-recording 02-event-locked-observation", workflow)
        self.assertIn("publish_testpypi:", workflow)
        self.assertIn("default: false", workflow)
        self.assertIn("if: ${{ inputs.publish_testpypi }}", workflow)

        native_smoke = (
            ROOT / "tests" / "fixtures" / "phase7_native_lsl_smoke.py"
        ).read_text(encoding="utf-8")
        self.assertIn("outlet.wait_for_consumers", native_smoke)
        self.assertIn('== ("C3", "C4")', native_smoke)
        self.assertIn("dense_batch_to_mne_raw", native_smoke)
        self.assertIn('timing_representation == "explicit"', native_smoke)
        self.assertIn("sparse_events_to_mne_annotations", native_smoke)
        self.assertIn("dense_windows_to_mne_epochs", native_smoke)
        self.assertIn("mne_raw_to_replay_inputs", native_smoke)


if __name__ == "__main__":
    unittest.main()
