#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from reproduce.realtime.classification import assess_epoch_quality
from reproduce.realtime.models import npz_value, prepare_classifier_epoch, training_contract

session = Path(os.environ["CALIB_SESSION"]).expanduser().resolve()
epochs_npz = session / "realtime" / "epochs" / "epochs.npz"

data = np.load(epochs_npz, allow_pickle=True)
X = np.asarray(data["X"], dtype=float)
y = np.asarray(data["y"], dtype=int)
trials = np.asarray(npz_value(data, "trials", np.arange(y.size) + 1), dtype=int)

cfg = {
    "input_units": "microvolts",
    "baseline_seconds": [-0.2, 0.0],
    "notch_hz": 60.0,
    "notch_q": 30.0,
    "average_reference": True,
    # Keep bandpass disabled initially. Notch + baseline + reference is the
    # minimal correction for your current Enobio/NIC2 data.
    "filter_low_hz": None,
    "filter_high_hz": None,
}

contract = training_contract(data, cfg)
times = np.asarray(npz_value(data, "times", []), dtype=float)
channels = [str(v) for v in contract["channel_names"]]

valid = (y >= 0) & (trials >= 1)
prepared = []
qualities = []

for epoch in X[valid]:
    values, _, _ = prepare_classifier_epoch(
        epoch,
        float(contract["sample_rate_hz"]),
        channels,
        {
            "relative_times": times.tolist(),
            "epoch_window_seconds": contract["epoch_window_seconds"],
        },
        contract,
    )
    prepared.append(values)
    qualities.append(assess_epoch_quality(values.T, {}).payload())

P = np.stack(prepared, axis=0)
q_valid = np.asarray([q["valid"] for q in qualities], dtype=bool)

max_abs = np.nanmax(np.abs(P), axis=(1, 2))
ptp = np.nanmax(np.nanmax(P, axis=2) - np.nanmin(P, axis=2), axis=1)
std = np.nanmedian(np.nanstd(P, axis=2), axis=1)

print("prepared_epochs:", P.shape)
print("quality_valid:", int(q_valid.sum()), "/", len(q_valid))
print("max_abs min/median/max:", float(np.min(max_abs)), float(np.median(max_abs)), float(np.max(max_abs)))
print("max_channel_ptp min/median/max:", float(np.min(ptp)), float(np.median(ptp)), float(np.max(ptp)))
print("median_channel_std min/median/max:", float(np.min(std)), float(np.median(std)), float(np.max(std)))
print("contract preprocessing:", contract["preprocessing"])
