"""Replay classifier predictions for an existing online session."""

from __future__ import annotations

import argparse

from eegle.analysis.classification import replay_classifier_session


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_dir")
    args = parser.parse_args()
    print(replay_classifier_session(args.session_dir))


if __name__ == "__main__":
    main()
