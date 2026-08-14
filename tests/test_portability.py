from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eegle.cli import build_parser, cmd_check_setup
from eegle.config import DEFAULT_CONFIG, load_config, resolve_session_root
from eegle.hardware.capabilities import (
    check_command_entrypoints,
    check_realtime_ready,
    check_training_ready,
)
from eegle.hardware.eeg_device import identify_eeg_device
from eegle.hardware.profiles import mapped_channel_names as mapped_eeg_channel_names
from eegle.hardware.os_support import check_os_support
from eegle.hardware.system import CheckResult, check_platform, check_python
from eegle.lsl import LslStream
from eegle.preflight import run_preflight
from eegle.runtime import _disable_psychopy_glfw, ensure_runtime_environment, resolve_runtime_cache_root
from eegle.session import create_session


ROOT = Path(__file__).resolve().parents[1]


class PortabilityTests(unittest.TestCase):
    def test_neuracle_windows_operator_kit_uses_activation_independent_commands(self) -> None:
        scripts = ROOT / "scripts" / "windows" / "neuracle64"
        expected = {
            "00-Setup.ps1",
            "01-DryRun-Task.ps1",
            "02-Test-NeuracleLsl.ps1",
            "03-Run-EEGTaskTest.ps1",
            "04-Run-FullShortTest.ps1",
            "05-Diagnose-Lsl.ps1",
            "06-Test-StorageAccess.ps1",
            "07-Run-Full.ps1",
            "Common.ps1",
        }
        self.assertEqual({path.name for path in scripts.glob("*.ps1")}, expected)
        common = (scripts / "Common.ps1").read_text(encoding="utf-8")
        self.assertIn(r".venv\Scripts\python.exe", common)
        dry_run = (scripts / "01-DryRun-Task.ps1").read_text(encoding="utf-8")
        self.assertIn('"--task-mode", "psychopy"', dry_run)
        self.assertIn("--skip-eeg", dry_run)
        self.assertIn("$FullScreen", dry_run)
        preflight = (scripts / "02-Test-NeuracleLsl.ps1").read_text(encoding="utf-8")
        self.assertIn("--confirm-cap-contract", preflight)
        self.assertIn("--preflight-only", preflight)
        self.assertIn("channel_count -ne 65", preflight)
        common = (scripts / "Common.ps1").read_text(encoding="utf-8")
        self.assertIn('expected_channel_names[64] -ne "TRIGGER_STATUS"', common)
        short_task = (scripts / "03-Run-EEGTaskTest.ps1").read_text(encoding="utf-8")
        self.assertIn("--preflight-only", short_task)
        self.assertIn("--require-eeg", short_task)
        self.assertIn("$FullScreen", short_task)
        full = (scripts / "04-Run-FullShortTest.ps1").read_text(encoding="utf-8")
        self.assertIn("Update-EegleGeneratedLiveConfigs", full)
        self.assertIn('"--smoke"', full)
        self.assertIn('"--include-practice"', full)
        self.assertIn('"--baseline-seconds"', full)
        self.assertIn('"--retry-incomplete"', full)
        self.assertIn("R_EEGleMarkers", full)
        self.assertIn("$FullScreen", full)
        diagnostics = (scripts / "05-Diagnose-Lsl.ps1").read_text(encoding="utf-8")
        self.assertIn("eegle.lsl_diagnostics", diagnostics)
        self.assertIn("--ignore-lsl-config", diagnostics)
        storage = (scripts / "06-Test-StorageAccess.ps1").read_text(encoding="utf-8")
        self.assertIn("eegle.storage_permissions", storage)
        complete = (scripts / "07-Run-Full.ps1").read_text(encoding="utf-8")
        self.assertIn('"--full-1000"', complete)
        self.assertIn('"--baseline-seconds", "120"', complete)
        self.assertIn('"--include-practice"', complete)
        self.assertIn('"--retry-incomplete"', complete)
        self.assertNotIn('"--smoke"', complete)
        self.assertIn("$FullScreen", complete)
        study_config = json.loads((ROOT / "configs" / "study1_neuracle64.json").read_text(encoding="utf-8"))
        self.assertEqual(study_config["recording_suite"]["marker_receipt_timeout_seconds"], 2.0)
        self.assertEqual(study_config["processes"]["recorder"]["tail_guard_seconds"], 1.0)
        self.assertEqual(study_config["processes"]["recorder"]["worker_shutdown_timeout_seconds"], 22.0)
        self.assertEqual(
            study_config["processes"]["recorder"]["maximum_xdf_tail_shortfall_warning_seconds"],
            2.0,
        )

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
                with patch("eegle.runtime.sys.platform", platform_name), patch.dict(sys.modules, {}, clear=False):
                    sys.modules.pop("glfw", None)
                    _disable_psychopy_glfw()
                    self.assertNotIn("glfw", sys.modules)

    def test_unknown_os_is_warning_not_a_separate_codebase_requirement(self) -> None:
        with patch("eegle.hardware.system.platform.system", return_value="Plan9"):
            result = check_os_support()
        self.assertEqual(result.status, "warn")
        self.assertFalse(result.data["requires_separate_codebase"])

    def test_known_os_support_is_single_codebase(self) -> None:
        for system_name in ("Darwin", "Windows", "Linux"):
            with self.subTest(system=system_name):
                with patch("eegle.hardware.system.platform.system", return_value=system_name):
                    result = check_os_support()
                self.assertEqual(result.status, "ok")
                self.assertFalse(result.data["requires_separate_codebase"])

    def test_windows_os_support_reports_windows_command_and_cache_model(self) -> None:
        with patch("eegle.hardware.system.platform.system", return_value="Windows"):
            result = check_os_support()

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["path_style"], "windows")
        self.assertIn("PowerShell", result.data["command_shells"])
        self.assertIn("USERPROFILE", result.data["runtime_cache_env_vars"])
        self.assertIn("LOCALAPPDATA", result.data["runtime_cache_env_vars"])

    def test_windows_runtime_environment_redirects_user_cache_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "runtime-cache"
            with patch("eegle.runtime.sys.platform", "win32"), patch.dict(
                os.environ,
                {
                    "HOME": "C:\\Users\\RealUser",
                    "USERPROFILE": "C:\\Users\\RealUser",
                    "APPDATA": "C:\\Users\\RealUser\\AppData\\Roaming",
                    "LOCALAPPDATA": "C:\\Users\\RealUser\\AppData\\Local",
                },
                clear=True,
            ):
                root = ensure_runtime_environment(cache)

                self.assertEqual(root, cache.resolve())
                self.assertEqual(os.environ["HOME"], str(root / "psychopy_home"))
                self.assertEqual(os.environ["USERPROFILE"], str(root / "psychopy_home"))
                self.assertEqual(os.environ["APPDATA"], str(root / "appdata"))
                self.assertEqual(os.environ["LOCALAPPDATA"], str(root / "local_appdata"))
                self.assertEqual(os.environ["CLOSEDLOOP_ORIGINAL_USERPROFILE"], "C:\\Users\\RealUser")
                self.assertTrue((root / "lsl_api.cfg").exists())

    def test_runtime_cache_env_override_supports_constrained_windows_shells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "approved-runtime"
            with patch.dict(
                os.environ,
                {"EEGLE_RUNTIME_CACHE_ROOT": str(cache)},
                clear=True,
            ):
                self.assertEqual(resolve_runtime_cache_root(".runtime"), cache.resolve())
                self.assertEqual(ensure_runtime_environment(".runtime"), cache.resolve())

    def test_relative_runtime_cache_falls_back_below_session_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_root = Path(tmp) / "approved-data"
            with patch.dict(
                os.environ,
                {"EEGLE_SESSION_ROOT": str(session_root)},
                clear=True,
            ):
                self.assertEqual(
                    resolve_runtime_cache_root(".runtime"),
                    (session_root / ".runtime").resolve(),
                )

    def test_absolute_runtime_cache_is_not_replaced_by_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit-runtime"
            with patch.dict(
                os.environ,
                {"EEGLE_RUNTIME_CACHE_ROOT": str(Path(tmp) / "environment-runtime")},
                clear=True,
            ):
                self.assertEqual(resolve_runtime_cache_root(explicit), explicit.resolve())

    def test_eegle_session_root_env_overrides_default_session_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_root = Path(tmp) / "approved-data"
            config = {
                "runtime": {"session_root": "data"},
                "experiment": {"experiment_id": "test", "participant_id": "unit", "task": "go_nogo"},
            }
            with patch.dict(os.environ, {"EEGLE_SESSION_ROOT": str(session_root)}, clear=False):
                self.assertEqual(os.path.realpath(resolve_session_root(config)), os.path.realpath(session_root))
                paths = create_session(config, task="go_nogo", participant_id="unit")

                written = load_config(paths.parameters)

        self.assertTrue(os.path.realpath(paths.root).startswith(os.path.realpath(session_root)))
        self.assertEqual(os.path.realpath(written["runtime"]["session_root"]), os.path.realpath(session_root))

    def test_console_command_visibility_is_reported_without_failing_preflight(self) -> None:
        with patch("eegle.hardware.capabilities.shutil.which", return_value=None):
            result = check_command_entrypoints(("eegle", "classify8"))
        self.assertEqual(result.status, "warn")
        self.assertEqual(result.data["missing"], ["eegle", "classify8"])

        with patch("eegle.hardware.capabilities.shutil.which", side_effect=lambda command: f"/bin/{command}"):
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

    def test_preflight_identifies_configured_neuracle_lsl_device(self) -> None:
        result = identify_eeg_device(
            [
                {
                    "name": "Neuracle EEG",
                    "type": "EEG",
                    "channel_count": 65,
                    "nominal_srate": 1000.0,
                    "source_id": "neuracle-lsl",
                }
            ],
            {
                "family": "Neuracle",
                "profile": "neuracle64",
                "expected_channel_counts": [65],
                "expected_sample_rate_hz": 1000,
                "lsl_stream_type": "EEG",
                "lsl_name_patterns": ["neuracle"],
            },
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["detector"], "neuracle_lsl")
        self.assertEqual(result.data["matches"][0]["channel_count"], 65)

    def test_neuracle_large_cap_profile_preserves_lsl_channel_labels(self) -> None:
        labels = [f"NE{index:02d}" for index in range(1, 66)]

        mapped, source = mapped_eeg_channel_names(labels, {"family": "Neuracle", "profile": "neuracle64"})

        self.assertEqual(mapped, labels)
        self.assertEqual(source, "lsl_metadata")

    def test_neuracle_pvt_preset_is_packaged_for_lsl_csv_recording(self) -> None:
        config = load_config("configs/forward_pvt_neuracle64.json")
        eeg = config["hardware"]["eeg"]

        self.assertEqual(eeg["family"], "Neuracle")
        self.assertEqual(eeg["profile"], "neuracle64")
        self.assertEqual(eeg["expected_channel_counts"], [65])
        self.assertEqual(len(eeg["expected_channel_names"]), 65)
        self.assertEqual(eeg["expected_channel_names"][-1], "TRIGGER_STATUS")
        self.assertEqual(eeg["expected_sample_rate_hz"], 1000)
        self.assertEqual(config["processes"]["recorder"]["backend"], "lsl_csv")

    def test_preflight_emits_neuracle_stream_check_and_sample_probe(self) -> None:
        config = {
            "hardware": {
                "eeg": {
                    "family": "Neuracle",
                    "profile": "neuracle64",
                    "expected_channel_counts": [65],
                    "expected_sample_rate_hz": 1000,
                    "lsl_stream_type": "EEG",
                    "lsl_name_patterns": ["neuracle"],
                    "sample_probe_seconds": 0.01,
                    "stream_timeout_seconds": 0.01,
                }
            }
        }
        stream = LslStream("Neuracle EEG", "EEG", 65, 1000.0, "neuracle-lsl")
        with patch("eegle.preflight.check_packages", return_value=[]), patch(
            "eegle.preflight.resolve_streams",
            return_value=([stream], None),
        ), patch(
            "eegle.preflight.probe_eeg_stream",
            return_value={"status": "ok", "sample_count": 8},
        ):
            results = run_preflight(config, lsl_wait=0, require_eeg=True)
        by_name = {result.name: result for result in results}

        self.assertEqual(by_name["eeg_device"].status, "ok")
        self.assertEqual(by_name["eeg_device"].data["detector"], "neuracle_lsl")
        self.assertEqual(by_name["neuracle_lsl"].status, "ok")
        self.assertEqual(by_name["eeg_sample_probe"].status, "ok")

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
        with patch("eegle.preflight.check_packages", return_value=[]), patch(
            "eegle.preflight.resolve_streams",
            return_value=([], "pylsl import failed: missing liblsl"),
        ):
            results = run_preflight(config, lsl_wait=0, require_eeg=False)
        by_name = {result.name: result for result in results}
        self.assertEqual(by_name["lsl"].status, "warn")
        self.assertEqual(by_name["eeg_device"].data["family"], "Enobio")
        self.assertEqual(by_name["eeg_device"].data["profile"], "enobio8_inhibition")

    def test_preflight_warns_when_lsl_works_but_no_outlets_are_visible(self) -> None:
        config = {
            "hardware": {
                "eeg": {
                    "family": "Neuracle",
                    "profile": "neuracle64",
                    "expected_channel_counts": [65],
                    "expected_sample_rate_hz": 1000,
                    "lsl_stream_type": "EEG",
                    "lsl_name_patterns": ["neuracle"],
                }
            }
        }
        with patch("eegle.preflight.check_packages", return_value=[]), patch(
            "eegle.preflight.resolve_streams",
            return_value=([], None),
        ):
            results = run_preflight(config, lsl_wait=0, require_eeg=True)

        by_name = {result.name: result for result in results}
        self.assertEqual(by_name["lsl"].status, "warn")
        self.assertIn("no visible stream outlets", by_name["lsl"].detail)
        self.assertEqual(by_name["neuracle_lsl"].status, "fail")

    def test_preflight_rejects_multiple_matching_eeg_streams(self) -> None:
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
        streams = [
            LslStream("Enobio EEG", "EEG", 8, 500.0, "enobio-a"),
            LslStream("NIC EEG", "EEG", 8, 500.0, "enobio-b"),
        ]
        with patch("eegle.preflight.check_packages", return_value=[]), patch(
            "eegle.preflight.resolve_streams", return_value=(streams, None)
        ), patch("eegle.preflight.probe_eeg_stream") as probe:
            results = run_preflight(config, lsl_wait=0, require_eeg=True)

        by_name = {result.name: result for result in results}
        self.assertEqual(by_name["enobio_lsl"].status, "fail")
        self.assertTrue(by_name["enobio_lsl"].data["ambiguous"])
        probe.assert_not_called()

    def test_preflight_skips_lsl_resolution_when_eeg_checks_are_disabled(self) -> None:
        config = {
            "hardware": {
                "eeg": {
                    "family": "Enobio",
                    "profile": "enobio32_dsart_wet",
                    "required_for_run": True,
                }
            },
            "realtime": {"enabled": False},
        }
        with patch("eegle.preflight.check_packages", return_value=[]), patch(
            "eegle.preflight.resolve_streams"
        ) as resolve:
            results = run_preflight(config, lsl_wait=30, require_eeg=False, check_eeg=False)

        resolve.assert_not_called()
        by_name = {result.name: result for result in results}
        self.assertEqual(by_name["eeg_acquisition"].status, "skip")
        self.assertTrue(by_name["eeg_acquisition"].ok)
        self.assertNotIn("eeg_sample_probe", by_name)

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
        with patch("eegle.hardware.capabilities.util.find_spec", return_value=None):
            result = check_realtime_ready(config, [], "pylsl import failed", device, require_eeg=False)
        self.assertEqual(result.status, "fail")
        self.assertIn("pylsl is not importable", result.data["failures"])
        self.assertIn("no matching EEG LSL stream detected", result.data["failures"])

    def test_training_ready_reports_missing_and_available_model_dependencies(self) -> None:
        def missing_torch_and_pyriemann(package: str) -> object | None:
            return None if package in {"torch", "pyriemann"} else object()

        with patch("eegle.hardware.capabilities.util.find_spec", side_effect=missing_torch_and_pyriemann):
            result = check_training_ready(["torch_eegnet", "pyriemann_erp_cov"], required=True)
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.data["missing_by_kind"]["torch_eegnet"], ["torch"])
        self.assertEqual(result.data["missing_by_kind"]["pyriemann_erp_cov"], ["pyriemann"])

        with patch("eegle.hardware.capabilities.util.find_spec", return_value=object()):
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
