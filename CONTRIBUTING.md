# Contributing

EEGle is moving toward a clean pip-installable library for reproducible realtime
EEG/BCI experiments. Contributions should preserve replayability, label-blind
inference, and conservative closed-loop safety defaults.

## Development Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Install additional extras only for the surface you are changing:

```bash
python -m pip install -e ".[runtime]"
python -m pip install -e ".[analysis]"
python -m pip install -e ".[ml]"
python -m pip install -e ".[torch]"
```

## Checks

Run focused tests first:

```bash
python3 -m unittest tests.test_phase2_foundations tests.test_source_boundaries
python3 -m unittest tests.test_ml_infrastructure tests.test_classification
python3 -m compileall -q eegle tests
```

Run the full suite before opening a pull request:

```bash
python3 -m unittest discover -s tests
```

## Project Rules

- Do not commit generated `data/` session recordings.
- Keep online classifier metadata label-blind.
- Keep foundation-model integrations optional. The plan-owned runtime must run
  primary roles first and explicitly account for every shadow disposition.
- Do not recreate deleted recipe or compatibility APIs; use the typed specs,
  compiler, runtime, recording, replay, and plugin boundaries.
