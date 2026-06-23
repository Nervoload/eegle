# Config Guidance

The JSON files in this directory are experiment presets. They are loaded by
`eegle.config.load_config` and then refined by CLI flags or pipeline helpers.

## Presets

- `default_experiment.json`: software development and dry-run PVT defaults.
- `forward_pvt_enobio.json`: PVT with Enobio/NIC2 stream matching.
- `forward_pvt_enobio8.json`: PVT tuned for an 8-channel, 500 Hz Enobio stream.
- `forward_go_nogo_enobio8.json`: posterior-alpha Go/No-go workflow.
- `forward_go_nogo_inhibition8.json`: observe-only inhibition workflow.
- `forward_go_nogo_classifier8.json`: GO/NO-GO condition-classifier capture,
  training, online testing, dashboard, and analysis defaults.

## Editing Rules

- Keep hardware expectations, task parameters, realtime processing, process
  launch settings, and analysis settings in their existing top-level sections.
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
