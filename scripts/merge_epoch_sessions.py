#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np


def load_epochs(session: Path) -> dict:
    path = session / "realtime" / "epochs" / "epochs.npz"
    if not path.exists():
        raise FileNotFoundError(f"missing epochs file: {path}")

    data = np.load(path, allow_pickle=True)
    return {key: data[key] for key in data.files}


def scalar_float(value) -> float:
    return float(np.asarray(value).ravel()[0])


def as_str_list(values) -> list[str]:
    return [str(v) for v in np.asarray(values).tolist()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge classify8 epoch datasets from multiple sessions.")
    parser.add_argument("--output-session", required=True)
    parser.add_argument("sessions", nargs="+")
    args = parser.parse_args()

    output = Path(args.output_session).expanduser().resolve()
    sessions = [Path(s).expanduser().resolve() for s in args.sessions]

    if len(sessions) < 2:
        raise SystemExit("provide at least two input sessions")

    loaded = [load_epochs(session) for session in sessions]
    first = loaded[0]

    first_channels = as_str_list(first["channel_names"])
    first_rate = scalar_float(first["sample_rate_hz"])
    first_times = np.asarray(first["times"], dtype=float)

    for session, data in zip(sessions[1:], loaded[1:]):
        channels = as_str_list(data["channel_names"])
        rate = scalar_float(data["sample_rate_hz"])
        times = np.asarray(data["times"], dtype=float)

        if channels != first_channels:
            raise ValueError(f"channel mismatch in {session}: {channels} != {first_channels}")
        if abs(rate - first_rate) > 1e-9:
            raise ValueError(f"sample-rate mismatch in {session}: {rate} != {first_rate}")
        if times.shape != first_times.shape or not np.allclose(times, first_times):
            raise ValueError(f"epoch timebase mismatch in {session}")

    X_parts = []
    y_parts = []
    trials_parts = []
    source_session_parts = []
    source_trial_parts = []
    condition_parts = []

    trial_offset = 0

    for source_index, (session, data) in enumerate(zip(sessions, loaded), start=1):
        X = np.asarray(data["X"], dtype=float)
        y = np.asarray(data["y"], dtype=int)
        trials = np.asarray(data.get("trials", np.arange(len(y)) + 1), dtype=int)

        X_parts.append(X)
        y_parts.append(y)

        # Renumber trials so blocked validation does not treat both runs as overlapping trial numbers.
        renumbered = np.arange(1, len(y) + 1, dtype=int) + trial_offset
        trials_parts.append(renumbered)
        trial_offset += len(y)

        source_session_parts.append(np.full(len(y), source_index, dtype=int))
        source_trial_parts.append(trials)

        if "conditions" in data:
            condition_parts.append(np.asarray(data["conditions"]))

    merged_dir = output / "realtime" / "epochs"
    merged_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "X": np.concatenate(X_parts, axis=0),
        "y": np.concatenate(y_parts, axis=0),
        "trials": np.concatenate(trials_parts, axis=0),
        "source_session_index": np.concatenate(source_session_parts, axis=0),
        "source_trial": np.concatenate(source_trial_parts, axis=0),
        "times": first_times,
        "channel_names": np.asarray(first_channels),
        "sample_rate_hz": np.asarray([first_rate], dtype=float),
        "layout": np.asarray(["epochs_x_channels_x_samples"]),
    }

    if condition_parts:
        payload["conditions"] = np.concatenate(condition_parts, axis=0)

    np.savez_compressed(merged_dir / "epochs.npz", **payload)

    manifest = {
        "schema_version": 1,
        "kind": "merged_classify8_epochs",
        "output_session": str(output),
        "input_sessions": [str(s) for s in sessions],
        "epoch_count": int(payload["X"].shape[0]),
        "channel_names": first_channels,
        "sample_rate_hz": first_rate,
        "epoch_window_seconds": [float(first_times[0]), float(first_times[-1])],
        "label_counts": {
            "go": int(np.sum(payload["y"] == 0)),
            "no_go": int(np.sum(payload["y"] == 1)),
            "invalid": int(np.sum(payload["y"] < 0)),
        },
    }
    (merged_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    # Keep classify8.train happy if it expects session-level parameters.
    first_params = sessions[0] / "parameters.json"
    if first_params.exists():
        shutil.copy2(first_params, output / "parameters.json")

    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
