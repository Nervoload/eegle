"""Build-time boundary between the v1 library and migration-only sources."""

from __future__ import annotations

from setuptools import setup
from setuptools.command.build_py import build_py


LEGACY_ROOT_MODULES = frozenset(
    {
        "cli",
        "components",
        "config",
        "eeg_csv",
        "experiment",
        "factory",
        "feedback_manager",
        "lsl",
        "preflight",
        "psychopy_input",
        "recording_health",
        "session",
        "telemetry",
    }
)


class V1BuildPy(build_py):
    """Exclude migration evidence that still occupies the root package."""

    def find_package_modules(self, package: str, package_dir: str):
        modules = super().find_package_modules(package, package_dir)
        if package != "eegle":
            return modules
        return [value for value in modules if value[1] not in LEGACY_ROOT_MODULES]


setup(cmdclass={"build_py": V1BuildPy})
