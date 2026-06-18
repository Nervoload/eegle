from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reproduce.cli import build_parser, cmd_check_setup
from reproduce.config import DEFAULT_CONFIG
from reproduce.hardware.capabilities import (
    check_command_entrypoints,
    check_realtime_ready,
    check_training_ready,
)
from reproduce.hardware.eeg_device import identify_eeg_device
from reproduce.hardware.os_support import check_os_support
from reproduce.hardware.system import CheckResult, check_platform, check_python
from reproduce.preflight import run_preflight
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
        os_support = check_os_support()
        self.assertEqual(os_support.status, "ok")
        self.assertFalse(os_support.data["requires_separate_codebase"])

    def test_macos_psychopy_workaround_does_not_modify_other_platforms(self) -> None:
        for platform_name in ("linux", "win32"):
            with self.subTest(platform=platform_name):
                with patch("reproduce.runtime.sys.platform", platform_name), patch.dict(sys.modules, {}, clear=False):
                    sys.modules.pop("glfw", None)
                    _disable_psychopy_glfw()
                    self.assertNotIn("glfw", sys.modules)

    def test_unknown_os_is_warning_not_a_separate_codebase_requirement(self) -> None:
        with patch("reproduce.hardware.system.platform.system", return_value="Plan9"):
            result = check_os_support()
        self.assertEqual(result.status, "warn")
        self.assertFalse(result.data["requires_separate_codebase"])

    def test_known_os_support_is_single_codebase(self) -> None:
        for system_name in ("Darwin", "Windows", "Linux"):
            with self.subTest(system=system_name):
                with patch("reproduce.hardware.system.platform.system", return_value=system_name):
                    result = check_os_support()
                self.assertEqual(result.status, "ok")
                self.assertFalse(result.data["requires_separate_codebase"])

    def test_console_command_visibility_is_reported_without_failing_preflight(self) -> None:
        with patch("reproduce.hardware.capabilities.shutil.which", return_value=None):
            result = check_command_entrypoints(("eegle", "classify8"))
        self.assertEqual(result.status, "warn")
        self.assertEqual(result.data["missing"], ["eegle", "classify8"])

        with patch("reproduce.hardware.capabilities.shutil.which", side_effect=lambda command: f"/bin/{command}"):
            ready = check_command_entrypoints(("eegle", "classify8"))
        self.assertEqual(ready.status, "ok")

    def test_preflight_identifies_configured_enobio_lsl_device(self) -> None:
        result = identify_eeg_device(
            [
                {
                    "name": "LSLOutletStreamName-EEG",
                    "type": "EEG",
                    "channel_count": 8,
                    "nominal_srate": 500.0,
                    "source_id": "nic2-enobio",
                }
            ],
            {
                "family": "Enobio",
                "profile": "enobio8_inhibition",
                "expected_channel_counts": [8],
                "expected_sample_rate_hz": 500,
                "lsl_stream_type": "EEG",
                "lsl_name_patterns": ["enobio", "nic", "LSLOutletStreamName-EEG"],
            },
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["detector"], "enobio_lsl")
        self.assertEqual(result.data["matches"][0]["channel_count"], 8)

    def test_preflight_identifies_32_and_64_channel_enobio_streams(self) -> None:
        for channel_count in (32, 64):
            with self.subTest(channel_count=channel_count):
                result = identify_eeg_device(
                    [
                        {
                            "name": "NIC2 EEG",
                            "type": "EEG",
                            "channel_count": channel_count,
                            "nominal_srate": 500.0,
                            "source_id": "enobio",
                            "channel_names": [f"E{index:02d}" for index in range(1, channel_count + 1)],
                        }
                    ],
                    {
                        "family": "Enobio",
                        "profile": f"enobio{channel_count}",
                        "expected_channel_counts": [32, 64],
                        "expected_sample_rate_hz": 500,
                        "lsl_stream_type": "EEG",
                        "lsl_name_patterns": ["enobio", "nic"],
                    },
                )

                self.assertEqual(result.status, "ok")
                self.assertEqual(result.data["matches"][0]["channel_count"], channel_count)

    def test_preflight_names_configured_device_when_lsl_is_unavailable(self) -> None:
        config = {
            "hardware": {
                "eeg": {
                    "family": "Enobio",
                    "profile": "enobio8_inhibition",
                    "expected_channel_counts": [8],
                    "expected_sample_rate_hz": 500,
                    "lsl_stream_type": "EEG",
                    "lsl_name_patterns": ["enobio", "nic"],
                }
            }
        }
        with patch("reproduce.preflight.check_packages", return_value=[]), patch(
            "reproduce.preflight.resolve_streams",
            return_value=([], "pylsl import failed: missing liblsl"),
        ):
            results = run_preflight(config, lsl_wait=0, require_eeg=False)
        by_name = {result.name: result for result in results}
        self.assertEqual(by_name["lsl"].status, "warn")
        self.assertEqual(by_name["eeg_device"].data["family"], "Enobio")
        self.assertEqual(by_name["eeg_device"].data["profile"], "enobio8_inhibition")

    def test_unknown_device_family_warns_without_forking_runtime(self) -> None:
        result = identify_eeg_device([], {"family": "FutureHeadset", "profile": "pilot"})
        self.assertEqual(result.status, "warn")
        self.assertIn("no registered detector", result.detail)

    def test_realtime_ready_fails_when_requested_without_pylsl_or_eeg_stream(self) -> None:
        config = {
            "realtime": {"enabled": True},
            "hardware": {
                "markers": {"lsl_stream_name": "EEGleMarkers", "lsl_stream_type": "Markers"},
            },
        }
        device = CheckResult("eeg_device", "warn", "missing", {"matches": [], "candidate_eeg_streams": []})
        with patch("reproduce.hardware.capabilities.util.find_spec", return_value=None):
            result = check_realtime_ready(config, [], "pylsl import failed", device, require_eeg=False)
        self.assertEqual(result.status, "fail")
        self.assertIn("pylsl is not importable", result.data["failures"])
        self.assertIn("no matching EEG LSL stream detected", result.data["failures"])

    def test_training_ready_reports_missing_and_available_model_dependencies(self) -> None:
        def missing_torch_and_pyriemann(package: str) -> object | None:
            return None if package in {"torch", "pyriemann"} else object()

        with patch("reproduce.hardware.capabilities.util.find_spec", side_effect=missing_torch_and_pyriemann):
            result = check_training_ready(["torch_eegnet", "pyriemann_erp_cov"], required=True)
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.data["missing_by_kind"]["torch_eegnet"], ["torch"])
        self.assertEqual(result.data["missing_by_kind"]["pyriemann_erp_cov"], ["pyriemann"])

        with patch("reproduce.hardware.capabilities.util.find_spec", return_value=object()):
            ready = check_training_ready(["erp_roi_logreg", "pyriemann_erp_cov", "torch_eegnet"], required=True)
        self.assertEqual(ready.status, "ok")

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
