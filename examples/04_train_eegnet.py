"""Train an EEGNet bundle from an existing epochs.npz file.

Install the torch extra first:

    python -m pip install -e ".[torch]"
"""

from __future__ import annotations

import argparse

from eegle.realtime.models import train_epoch_model


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("epochs_npz")
    parser.add_argument("output_dir")
    parser.add_argument("--target", default="condition")
    args = parser.parse_args()
    result = train_epoch_model(
        "torch_eegnet",
        args.epochs_npz,
        args.output_dir,
        {"target": args.target, "permutations": 10},
    )
    print(result)


if __name__ == "__main__":
    main()
