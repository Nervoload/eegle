"""Extract marker-locked epochs from an existing EEGle session."""

from __future__ import annotations

import argparse

from eegle.config import load_config
from eegle.realtime.epoching import extract_epochs_for_session


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_dir")
    args = parser.parse_args()
    config = load_config(f"{args.session_dir}/parameters.json")
    summary = extract_epochs_for_session(args.session_dir, config, source="auto")
    print(summary)


if __name__ == "__main__":
    main()
