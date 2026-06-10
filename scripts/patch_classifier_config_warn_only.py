#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

CONFIG = Path("configs/forward_go_nogo_classifier8.json")

data = json.loads(CONFIG.read_text())

realtime = data.setdefault("realtime", {})
gate = realtime.setdefault("quality_gate", {})

# Quality is still recorded into model_predictions.jsonl, but artifact thresholds
# no longer prevent model prediction.
gate["reject_predictions"] = False
gate["always_reject_reasons"] = [
    "invalid_shape",
    "non_finite",
    "quality_preparation_failed",
]

# These thresholds are applied after prepared-epoch preprocessing by the worker
# patch. Keep them physiologically meaningful, not raw-offset tolerant.
gate.setdefault("minimum_finite_fraction", 1.0)
gate.setdefault("minimum_channel_std_uv", 0.01)
gate.setdefault("max_abs_uv", 250.0)
gate.setdefault("max_peak_to_peak_uv", 400.0)

# Ensure model contracts default to the representation your data needs.
model = realtime.setdefault("model", {})
model.setdefault("input_units", "microvolts")
model.setdefault("baseline_seconds", [-0.2, 0.0])
model.setdefault("notch_hz", 60.0)
model.setdefault("notch_q", 30.0)
model.setdefault("average_reference", True)

# Keep bandpass optional. Add later if you want ERP-band-only model input.
model.setdefault("filter_low_hz", None)
model.setdefault("filter_high_hz", None)
model.setdefault("filter_order", 2)

CONFIG.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
print("OK: patched", CONFIG)
print(json.dumps(data["realtime"]["quality_gate"], indent=2))
