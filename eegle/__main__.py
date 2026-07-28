"""Public module entry point for artifact-oriented EEGle operations."""

from __future__ import annotations

def main() -> int:
    from eegle.operations.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
