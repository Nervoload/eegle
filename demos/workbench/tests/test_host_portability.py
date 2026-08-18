"""Host-portability behaviour the Workbench relies on outside macOS."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from demos.workbench import platform_support
from demos.workbench.operations import (
    DEFAULT_LSL_WAIT_SECONDS,
    LSL_WAIT_ENV_VAR,
    lsl_scan_wait_seconds,
)
from demos.workbench.platform_support import (
    DATA_ROOT_ENV_VAR,
    child_environment,
    child_interpreter,
    default_data_root,
    discovery_remediation,
    enforced_project_root_risk,
    project_root_length_risk,
)
from eegle._paths import MAX_PATH, io_path, path_length_risk
from eegle.integrations.lsl import (
    LslSupportLevel,
    LslSupportReport,
    detect_lsl,
    lsl_dependency_remediation,
    probe_lsl_dependency,
)


class ExtendedPathTests(unittest.TestCase):
    def test_posix_paths_are_returned_unchanged(self) -> None:
        if sys.platform == "win32":
            self.skipTest("POSIX behaviour")
        self.assertEqual(io_path("/tmp/a/b"), "/tmp/a/b")

    def test_windows_paths_gain_the_extended_prefix(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows behaviour")
        value = str(io_path(Path("C:/tmp/a")))
        self.assertTrue(value.startswith("\\\\?\\"))
        self.assertEqual(io_path(value), value)

    def test_long_paths_are_reported_on_every_host(self) -> None:
        self.assertIsNone(path_length_risk("/short"))
        risk = path_length_risk("/" + "a" * (MAX_PATH + 1))
        self.assertIsNotNone(risk)
        self.assertIn(str(MAX_PATH), risk or "")


class ChildProcessTests(unittest.TestCase):
    def test_child_environment_pins_utf8_and_unbuffered_pipes(self) -> None:
        environment = child_environment({"PATH": "/usr/bin"})
        self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(environment["PYTHONUTF8"], "1")
        self.assertEqual(environment["PYTHONUNBUFFERED"], "1")

    def test_child_environment_puts_the_checkout_first_on_the_path(self) -> None:
        environment = child_environment({"PYTHONPATH": "/existing"})
        entries = environment["PYTHONPATH"].split(os.pathsep)
        self.assertEqual(entries[0], str(platform_support.REPOSITORY_ROOT))
        self.assertIn("/existing", entries)

    def test_existing_checkout_entry_is_not_duplicated(self) -> None:
        root = str(platform_support.REPOSITORY_ROOT)
        environment = child_environment({"PYTHONPATH": root})
        self.assertEqual(environment["PYTHONPATH"].split(os.pathsep).count(root), 1)

    def test_a_console_interpreter_replaces_pythonw(self) -> None:
        """A GUI-only interpreter cannot own the NDJSON supervision pipes."""

        # Forward slashes so the branch is exercised identically on a POSIX
        # test host, where Path does not split on backslashes.
        with (
            mock.patch.object(platform_support.sys, "platform", "win32"),
            mock.patch.object(Path, "is_file", return_value=True),
        ):
            resolved = child_interpreter("C:/Python/pythonw.exe")
        self.assertEqual(Path(resolved).name, "python.exe")
        self.assertEqual(Path(resolved).parent, Path("C:/Python"))

    def test_pythonw_is_kept_when_no_console_interpreter_exists(self) -> None:
        with (
            mock.patch.object(platform_support.sys, "platform", "win32"),
            mock.patch.object(Path, "is_file", return_value=False),
        ):
            resolved = child_interpreter("C:/Python/pythonw.exe")
        self.assertEqual(Path(resolved).name, "pythonw.exe")

    def test_a_console_interpreter_is_left_alone(self) -> None:
        resolved = child_interpreter(sys.executable)
        self.assertEqual(Path(resolved), Path(sys.executable))


class DataRootTests(unittest.TestCase):
    def test_an_explicit_override_wins(self) -> None:
        with mock.patch.dict(os.environ, {DATA_ROOT_ENV_VAR: "/opt/eegle-demo"}):
            self.assertEqual(default_data_root(), Path("/opt/eegle-demo").resolve())

    def test_posix_hosts_never_relocate_an_existing_project(self) -> None:
        if sys.platform == "win32":
            self.skipTest("POSIX behaviour")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(DATA_ROOT_ENV_VAR, None)
            expected = (
                platform_support.REPOSITORY_ROOT / "data" / "workbench" / "projects"
            )
            self.assertEqual(default_data_root(), expected)

    def test_deep_project_roots_are_reported_as_a_risk(self) -> None:
        deep = "C:/" + "d" * 200
        risk = project_root_length_risk(deep)
        self.assertIsNotNone(risk)
        self.assertIn(DATA_ROOT_ENV_VAR, risk or "")

    def test_shallow_project_roots_carry_no_risk(self) -> None:
        self.assertIsNone(project_root_length_risk("C:/eegle"))

    def test_only_an_enforcing_host_raises_the_risk_in_the_interface(self) -> None:
        deep = "C:/" + "d" * 200
        if sys.platform == "win32":
            self.assertIsNotNone(enforced_project_root_risk(deep))
        else:
            self.assertIsNone(enforced_project_root_risk(deep))


class LslDiagnosticTests(unittest.TestCase):
    def test_a_load_failure_reports_its_cause_and_remediation(self) -> None:
        """An operator can only act on the real liblsl failure, not on a flag."""

        message = "liblsl library 'lsl.dll' found but could not be loaded"
        with mock.patch(
            "eegle.integrations.lsl._import_pylsl",
            side_effect=RuntimeError(message),
        ):
            detection = detect_lsl()
            probe = probe_lsl_dependency()
        for support in (detection.support, probe):
            self.assertFalse(support.dependency_available)
            self.assertEqual(support.support_level, LslSupportLevel.UNAVAILABLE)
            self.assertIn(message, support.unavailable_reason or "")
            self.assertTrue(support.remediation)
        self.assertEqual(detection.streams, ())

    def test_the_reason_survives_the_support_payload(self) -> None:
        report = LslSupportReport(
            False,
            None,
            LslSupportLevel.UNAVAILABLE,
            unavailable_reason="ImportError: no module named pylsl",
            remediation=("install it",),
        )
        payload = report.to_payload()
        self.assertEqual(
            payload["unavailable_reason"], "ImportError: no module named pylsl"
        )
        self.assertEqual(payload["remediation"], ["install it"])

    def test_an_available_dependency_cannot_claim_a_failure(self) -> None:
        with self.assertRaises(ValueError):
            LslSupportReport(
                True,
                "117",
                LslSupportLevel.SIMULATED_VALIDATED,
                unavailable_reason="not a real failure",
            )

    def test_remediation_is_host_specific(self) -> None:
        with mock.patch.object(sys, "platform", "win32"):
            windows = lsl_dependency_remediation()
            discovery = discovery_remediation()
        self.assertTrue(any("Redistributable" in value for value in windows))
        self.assertTrue(any("Firewall" in value for value in discovery))


class LslWaitTests(unittest.TestCase):
    def test_the_default_wait_exceeds_a_single_resolve_round(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(LSL_WAIT_ENV_VAR, None)
            self.assertEqual(lsl_scan_wait_seconds(), DEFAULT_LSL_WAIT_SECONDS)
        self.assertGreater(DEFAULT_LSL_WAIT_SECONDS, 1.0)

    def test_a_site_can_override_the_wait(self) -> None:
        with mock.patch.dict(os.environ, {LSL_WAIT_ENV_VAR: "7.5"}):
            self.assertEqual(lsl_scan_wait_seconds(), 7.5)

    def test_unusable_overrides_fall_back_to_the_default(self) -> None:
        for value in ("not-a-number", "0", "-3", ""):
            with mock.patch.dict(os.environ, {LSL_WAIT_ENV_VAR: value}):
                self.assertEqual(lsl_scan_wait_seconds(), DEFAULT_LSL_WAIT_SECONDS)


if __name__ == "__main__":
    unittest.main()
