from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reproduce.cli import build_parser, cmd_check_setup
from reproduce.config import DEFAULT_CONFIG
from reproduce.hardware.system import check_platform, check_python
from reproduce.runtime import _disable_psychopy_glfw
from reproduce.session import create_session


class PortabilityTests(unittest.TestCase):
    def test_primary_cli_name_and_setup_check_command_are_clear(self) -> None:
        parser = build_parser()
        self.assertEqual(parser.prog, "eegle")
        self.assertIs(parser.parse_args(["check-setup"]).func, cmd_check_setup)
        self.assertIs(parser.parse_args(["doctor"]).func, cmd_check_setup)

    def test_default_config_is_packaged_with_the_runtime(self) -> None:
        self.assertTrue(DEFAULT_CONFIG.exists())

    def test_unconstrained_runtime_uses_current_platform_and_python(self) -> None:
        self.assertEqual(check_platform().status, "ok")
        self.assertEqual(check_python().status, "ok")
        self.assertEqual(check_python().data["actual"], sys.executable)

    def test_macos_psychopy_workaround_does_not_modify_other_platforms(self) -> None:
        with patch("reproduce.runtime.sys.platform", "linux"), patch.dict(sys.modules, {}, clear=False):
            sys.modules.pop("glfw", None)
            _disable_psychopy_glfw()
            self.assertNotIn("glfw", sys.modules)

    def test_session_layout_uses_named_participant_and_session_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = {
                "runtime": {"session_root": tmp},
                "experiment": {
                    "experiment_id": "example-experiment",
                    "participant_id": "example-participant",
                    "task": "pvt",
                },
            }

            paths = create_session(config)
            relative = paths.root.relative_to(Path(tmp))

            self.assertEqual(relative.parts[0:2], ("participants", "example-participant"))
            self.assertEqual(relative.parts[2], "sessions")
            self.assertEqual(relative.parts[-3:-1], ("example-experiment", "pvt"))
            self.assertTrue(relative.parts[-1].startswith("run-"))


if __name__ == "__main__":
    unittest.main()
