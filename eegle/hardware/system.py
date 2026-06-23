"""System information and hardware preflight checks."""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field
from importlib import metadata, util
from pathlib import Path
from typing import Any


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"ok", "warn", "skip"}


def system_snapshot() -> dict[str, Any]:
    return {
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }


def check_platform(expected_os: str | None = None, expected_machine: str | None = None) -> CheckResult:
    snap = system_snapshot()
    status = "ok"
    details = []
    if expected_os and snap["system"] != expected_os:
        status = "warn"
        details.append(f"expected OS {expected_os}, found {snap['system']}")
    if expected_machine and snap["machine"] != expected_machine:
        status = "warn"
        details.append(f"expected machine {expected_machine}, found {snap['machine']}")
    if not details:
        details.append(f"{snap['system']} on {snap['machine']}")
    return CheckResult("platform", status, "; ".join(details), snap)


def check_python(expected: str | Path | None = None) -> CheckResult:
    actual_path = Path(sys.executable).expanduser()
    if not expected:
        return CheckResult("python", "ok", str(actual_path), {"actual": str(actual_path)})
    expected_path = Path(expected).expanduser()
    if actual_path == expected_path:
        return CheckResult("python", "ok", str(actual_path), {"expected": str(expected_path)})
    return CheckResult(
        "python",
        "warn",
        f"running {actual_path}; configured runtime is {expected_path}",
        {"expected": str(expected_path), "actual": str(actual_path)},
    )


def check_packages(required: list[str], optional: list[str]) -> list[CheckResult]:
    results: list[CheckResult] = []
    for package in required:
        results.append(_package_result(package, required=True))
    for package in optional:
        results.append(_package_result(package, required=False))
    return results


def _package_result(package: str, required: bool) -> CheckResult:
    spec = util.find_spec(package)
    if spec is None:
        status = "fail" if required else "warn"
        return CheckResult(f"package:{package}", status, "not importable")
    version = "unknown"
    try:
        version = metadata.version(package)
    except metadata.PackageNotFoundError:
        pass
    return CheckResult(f"package:{package}", "ok", f"available ({version})")
