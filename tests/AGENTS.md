# Test Guidance

The test suite uses `unittest`. Prefer focused tests while iterating, then run
broader discovery when touching shared orchestration.

## Test Map

- `test_classification.py`: classify8 workflow contracts, label-blind metadata,
  model bundles, epoch quality, dashboard/demo behavior, replay, and scoring.
- `test_closed_loop_model_system.py`: closed-loop model system and realtime
  integration behavior.
- `test_realtime_synchrony.py`: marker timing, realtime synchrony, and process
  coordination behavior.
- `test_inhibition8.py`: observe-only inhibition pipeline behavior.
- `test_portability.py`: config/package portability and import-level checks.

## Common Commands

Classifier path:

```bash
python3 -m unittest tests.test_classification
```

Realtime/worker path:

```bash
python3 -m unittest tests.test_realtime_synchrony tests.test_closed_loop_model_system
```

Full suite:

```bash
python3 -m unittest discover -s tests
```

Compile check after structural edits:

```bash
python3 -m compileall -q eegle tests
```

If the full suite fails because of environment-only dependencies, preserve the
focused test result in your final notes and name the blocker explicitly.
