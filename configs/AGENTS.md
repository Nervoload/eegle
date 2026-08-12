# Config Guidance

The JSON files in this directory are experiment presets. They are loaded by
`eegle.config.load_config` and then refined by CLI flags or pipeline helpers.

## Presets

- `default_experiment.json`: software development and dry-run PVT defaults.
- `forward_pvt_enobio.json`: PVT with Enobio/NIC2 stream matching.
- `forward_pvt_enobio8.json`: PVT tuned for an 8-channel, 500 Hz Enobio stream.
- `forward_pvt_neuracle64.json`: PVT tuned for the 65-value, 1000 Hz Neuracle
  W64 LSL transport (64 physical inputs plus a reserved trigger/status value).
- `forward_go_nogo_enobio8.json`: posterior-alpha Go/No-go workflow.
- `forward_go_nogo_inhibition8.json`: observe-only inhibition workflow.
- `forward_go_nogo_classifier8.json`: GO/NO-GO condition-classifier capture,
  training, online testing, dashboard, and analysis defaults.
- `forward_dynamic_sart.json`: formal standalone Dynamic-State SART recipe.
- `record_dsart8.json` and `record_dsart32.json`: recording-first two-session
  Dynamic-State SART visit recipes; keep their task protocol identical and put
  montage-specific differences under `hardware.eeg`.
- `study1_neuracle64.json`: visit-aware proposal candidate built on Dynamic
  SART for Neuracle 64; unresolved channel, reference/ground/EOG, and physical
  cue-delivery values must remain explicit live gates rather than guessed defaults.

## Editing Rules

- Keep hardware expectations, task parameters, realtime processing, process
  launch settings, and analysis settings in their existing top-level sections.
- Keep formal Dynamic SART timing fixed at a 0.25-second digit plus 1.35-second
  fixation (1.60-second SOI), with both intentional jitter bounds at zero.
- Formal Dynamic SART display presets must wait for VBlank and require a
  measured refresh-rate match. Windowed and full-screen launch modes are both
  valid; keep windowed mode resizable and use `units: height` so layouts scale.
- When adding a new config key, verify that the consumer code handles a missing
  value with a safe default. Older session `parameters.json` files can be
  replayed later.
- Do not enable task adaptation, stimulation, or feedback side effects in
  classifier configs unless the user explicitly asks for a closed-loop behavior
  change.
- For classifier work, keep `realtime.quality_gate`, `realtime.epoching`,
  `realtime.preprocessing`, and `realtime.model` aligned with the contracts in
  `eegle/realtime/classification.py` and `eegle/realtime/models.py`.
- Config changes that affect generated artifacts should also update the README
  or `docs/MODEL_TRAINING_TESTING_GOALS.md` when operator commands change.

## Useful Checks

```bash
python3 -m unittest tests.test_portability
python3 -m unittest tests.test_classification
```
