"""Host path-length portability for local evidence and artifact storage.

Windows rejects most filesystem calls whose path exceeds ``MAX_PATH`` (260)
unless the caller opts into extended-length paths with the ``\\\\?\\`` prefix.
Evidence bundles nest a session id, a bundle id, and a content-addressed
artifact name, so a normal project checkout crosses that limit while POSIX
hosts stay far below their own limits.

``io_path`` is applied at the moment of a filesystem call and never stored.
Paths held on objects keep their original form so ``relative_to``, equality,
manifest URIs, and recorded provenance remain identical on every platform.
On non-Windows hosts this module is an identity function.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_WINDOWS = sys.platform == "win32"
_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"
# The documented Windows limit is 260 including the terminating NUL, so 259
# characters is the longest path a legacy call can address.
MAX_PATH = 259


def io_path(path: str | os.PathLike[str]) -> str | os.PathLike[str]:
    """Return ``path`` in the form the host's filesystem calls can address."""

    if not _WINDOWS:
        return path
    text = os.fspath(path)
    if text.startswith(_EXTENDED):
        return text
    absolute = os.path.abspath(text)
    if absolute.startswith("\\\\"):
        return _EXTENDED_UNC + absolute[2:]
    return _EXTENDED + absolute


def opened(path: str | os.PathLike[str], mode: str, **kwargs: object):
    """Open a file without inheriting the host's legacy path-length limit."""

    return open(io_path(path), mode, **kwargs)  # type: ignore[call-overload]


def path_length_risk(path: str | os.PathLike[str], *, headroom: int = 0) -> str | None:
    """Describe a host path-length risk for operator-facing diagnostics.

    Returns ``None`` when the path is safe. The check runs on every platform so
    a POSIX host can still warn about a layout that would fail on Windows.
    """

    text = os.fspath(Path(path))
    length = len(text) + int(headroom)
    if length <= MAX_PATH:
        return None
    return (
        f"path is {length} characters, beyond the {MAX_PATH}-character Windows "
        "limit for non-extended paths"
    )


__all__ = ["MAX_PATH", "io_path", "opened", "path_length_risk"]
