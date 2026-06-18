"""Operating-system support checks for EEGle preflight."""

from __future__ import annotations

from typing import Any

from reproduce.hardware.system import CheckResult, system_snapshot


SUPPORTED_OS: dict[str, dict[str, Any]] = {
    "Darwin": {
        "display_name": "macOS",
        "notes": [
            "Use installed console scripts or python -m modules.",
            "PsychoPy display validation and LSL firewall settings remain operator checks.",
        ],
    },
    "Windows": {
        "display_name": "Windows",
        "notes": [
            "Use installed console scripts from PowerShell or cmd.",
            "Repository-root sh wrappers and Makefile targets are POSIX conveniences.",
            "PsychoPy display validation, NIC2, and Windows firewall settings remain operator checks.",
        ],
    },
    "Linux": {
        "display_name": "Linux",
        "notes": [
            "Use installed console scripts or python -m modules.",
            "PsychoPy display validation, display-server setup, and LSL firewall settings remain operator checks.",
        ],
    },
}


def check_os_support() -> CheckResult:
    """Report whether the current OS is covered by the shared codebase."""
    snap = system_snapshot()
    system = str(snap.get("system") or "unknown")
    support = SUPPORTED_OS.get(system)
    if support is None:
        known = ", ".join(entry["display_name"] for entry in SUPPORTED_OS.values())
        return CheckResult(
            "os_support",
            "warn",
            f"{system} is not in the validated OS set ({known})",
            {
                "system": system,
                "supported_systems": sorted(SUPPORTED_OS),
                "requires_separate_codebase": False,
                "reason": "unknown OS; portable Python path should be verified",
            },
        )
    display = str(support["display_name"])
    return CheckResult(
        "os_support",
        "ok",
        f"{display} is supported by the shared Python codebase",
        {
            "system": system,
            "display_name": display,
            "supported_systems": sorted(SUPPORTED_OS),
            "requires_separate_codebase": False,
            "os_specific_notes": list(support.get("notes", [])),
        },
    )
