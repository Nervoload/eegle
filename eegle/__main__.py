"""Temporary module entry point for the clean v1 package rebuild."""

from __future__ import annotations

import sys


MESSAGE = (
    "EEGle's v1 command-line interface is not available yet. "
    "Use the typed Python APIs in eegle.specs, eegle.compiler, and "
    "eegle.runtime; the artifact-oriented CLI arrives in migration Phase 7."
)


def main() -> int:
    print(MESSAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
