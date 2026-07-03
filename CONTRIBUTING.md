# Contributing

EEGle is moving toward a clean pip-installable library for reproducible realtime
EEG/BCI experiments. Contributions should preserve replayability, label-blind
inference, and conservative closed-loop safety defaults.

## Development Setup

```bash
python3.10 -m venv .venv
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
python3 -m unittest tests.test_public_api
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
- Keep `classify8 demo` predictions separate from real classifier artifacts.
- Keep foundation-model integrations optional and shadow-first until their
  contracts, latency, and replay behavior are validated.
