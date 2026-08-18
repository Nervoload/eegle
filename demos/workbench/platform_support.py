"""Host differences the Workbench demo must handle explicitly.

The Workbench supervises two child interpreters over a line-delimited JSON
protocol and stores evidence under a project directory. Both are places where
Windows and POSIX hosts differ in ways that are silent until they fail, so the
differences are resolved here instead of at each call site.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from eegle._paths import MAX_PATH

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT_ENV_VAR = "EEGLE_WORKBENCH_DATA_ROOT"

# The deepest path the recording layer creates under a project root is a
# session evidence artifact: sessions/<session id>/artifacts/bundles/<bundle
# id>/<64-character digest>, plus the atomic temporary prefix. Measured from a
# real rehearsal bundle rather than estimated.
DEEPEST_PROJECT_RELATIVE_PATH = 180


def child_interpreter(executable: str | os.PathLike[str] | None = None) -> str:
    """Return an interpreter that can own the NDJSON supervision pipes.

    A Workbench launched through ``pythonw.exe`` has no usable standard
    streams, and a child started from it inherits that: the supervisor would
    then wait forever for a protocol message the child can never write. The
    console interpreter beside it is the same installation.
    """

    candidate = Path(executable or sys.executable)
    if sys.platform != "win32":
        return str(candidate)
    if candidate.name.lower() == "pythonw.exe":
        console = candidate.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(candidate)


def child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return the environment a supervised child interpreter needs.

    The protocol between Workbench and its children is UTF-8 JSON over pipes.
    Windows still defaults those pipes to the ANSI code page, and Python block
    buffers a piped stdout, so both are pinned here rather than trusted.
    """

    environment = dict(os.environ if base is None else base)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    existing = environment.get("PYTHONPATH", "")
    root = str(REPOSITORY_ROOT)
    entries = [value for value in existing.split(os.pathsep) if value]
    if root not in entries:
        entries.insert(0, root)
    environment["PYTHONPATH"] = os.pathsep.join(entries)
    return environment


def default_data_root() -> Path:
    """Resolve where Workbench keeps its projects and recorded sessions.

    An explicit override wins. Otherwise the in-repository location is used
    while it leaves room for the deepest evidence path; when it does not, a
    short per-user location is chosen so recording cannot fail partway through
    a session on a host with a legacy path limit.
    """

    override = os.environ.get(DATA_ROOT_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    in_repository = REPOSITORY_ROOT / "data" / "workbench" / "projects"
    if sys.platform != "win32":
        # POSIX hosts have no comparable limit, so the in-repository location
        # stays authoritative and existing projects are never relocated.
        return in_repository
    if not project_root_length_risk(in_repository / "study1-neuracle64-demo"):
        return in_repository
    return user_data_root()


def user_data_root() -> Path:
    """Return the short per-user fallback location for Workbench projects."""

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base).expanduser().resolve() / "EEGle" / "Workbench" / "projects"
    return Path.home().resolve() / ".eegle" / "workbench" / "projects"


def project_root_length_risk(project_root: str | os.PathLike[str]) -> str | None:
    """Describe whether evidence under ``project_root`` can exceed MAX_PATH.

    Reported on every platform: a POSIX host should be able to warn that a
    layout would fail for a collaborator on Windows.
    """

    root = str(Path(project_root))
    deepest = len(root) + DEEPEST_PROJECT_RELATIVE_PATH
    if deepest <= MAX_PATH:
        return None
    return (
        f"evidence paths under this project reach about {deepest} characters, "
        f"beyond the {MAX_PATH}-character limit Windows applies to "
        f"non-extended paths. EEGle writes evidence through extended-length "
        f"paths, so recording still succeeds, but Explorer, archive tools, and "
        f"editors may not be able to open the session. Set "
        f"{DATA_ROOT_ENV_VAR} to a shorter directory (for example "
        f"{user_data_root()}) to keep sessions reachable by other tools."
    )


def enforced_project_root_risk(project_root: str | os.PathLike[str]) -> str | None:
    """Return the path-length risk only where the host enforces the limit."""

    if sys.platform != "win32":
        return None
    return project_root_length_risk(project_root)


def discovery_remediation() -> tuple[str, ...]:
    """Return host-specific reasons a loadable LSL library still finds nothing."""

    if sys.platform == "win32":
        return (
            (
                "Allow this interpreter through Windows Defender Firewall on "
                "private networks; blocked UDP discovery resolves zero "
                "streams while the library itself loads normally."
            ),
            (
                "With several network adapters present (Wi-Fi, Ethernet, "
                "virtual switches), set a longer "
                "EEGLE_WORKBENCH_LSL_WAIT_SECONDS or pin the interface in a "
                "liblsl configuration file."
            ),
            "Confirm the acquisition host and this host share one subnet.",
        )
    return (
        "Confirm the acquisition host and this host share one subnet.",
        (
            "Raise EEGLE_WORKBENCH_LSL_WAIT_SECONDS if discovery is slow to "
            "answer on this network."
        ),
    )


__all__ = [
    "DATA_ROOT_ENV_VAR",
    "REPOSITORY_ROOT",
    "child_environment",
    "child_interpreter",
    "default_data_root",
    "discovery_remediation",
    "enforced_project_root_risk",
    "project_root_length_risk",
    "user_data_root",
]
