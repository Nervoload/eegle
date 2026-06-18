# Agent Orientation

This repository is EEGle: a Python 3.10 toolkit for reproducible realtime EEG
experiments. It combines PsychoPy tasks, Lab Streaming Layer acquisition,
session recording, realtime closed-loop workers, and post-session analysis.

## First Read

Start here before changing code:

1. `README.md` for installation, commands, session layout, and operator-facing
   workflows.
2. `docs/ARCHITECTURE.md` for the runtime data path and component boundaries.
3. `docs/MODEL_TRAINING_TESTING_GOALS.md` for classifier training, evaluation,
   and current model goals.
4. `reproduce/AGENTS.md` for package-level implementation guidance.
5. `configs/AGENTS.md` before changing JSON experiment configs.
6. `tests/AGENTS.md` before deciding which tests to run.

## Code Map

- `reproduce/cli.py`: installed `eegle` command and generic subcommands.
- `reproduce/pipelines/`: specialized operator workflows such as `alpha8`,
  `inhibition8`, and `classify8`.
- `reproduce/experiment.py`: forward experiment orchestration.
- `reproduce/feedback_manager.py`: managed recorder, realtime, dashboard, and
  analyzer worker lifecycle.
- `reproduce/tasks/`: PsychoPy and dry-run task implementations.
- `reproduce/realtime/`: ring buffers, preprocessing, epoching, model adapters,
  classifier contracts, policies, and feedback emitters.
- `reproduce/workers/`: process entrypoints for recorder, realtime processor,
  dashboard, and offline analyzer.
- `reproduce/analysis/`: post-session reports, ERP/P300 analysis, classifier
  scoring, replay, and HTML summaries.
- `configs/`: JSON experiment presets.
- `tests/`: focused unittest coverage for realtime, classifier, portability, and
  pipeline behavior.

## Local Contracts

- Treat `data/` as generated and potentially sensitive. It is ignored by Git and
  should not be used for durable docs or fixtures unless the user explicitly asks.
- Classifier inference must stay label-blind. Do not pass stimulus condition,
  response correctness, or training labels into model input metadata.
- The current `classify8` workflow is observe-only: online predictions and
  dashboards should not adapt task behavior or claim stimulation control.
- The `classify8 demo` path is simulated from PsychoPy markers. It writes
  `realtime/demo_predictions.jsonl` and must not be mixed with real classifier
  predictions in `realtime/model_predictions.jsonl`.
- Dashboards bind to localhost and are non-critical observers of session
  artifacts; they should not block PsychoPy task execution.
- Generated model bundles are content-addressed by their manifests, metrics, and
  artifacts. Preserve hash validation when changing model storage.

## Common Commands

Install for development:

```text
macOS/Linux:
  python3.10 -m venv .venv
  source .venv/bin/activate
  python -m pip install -e ".[runtime,ml]"

Windows PowerShell:
  py -3.10 -m venv .venv
  .venv\Scripts\Activate.ps1
  python -m pip install -e ".[runtime,ml]"
```

Use `constraints/macos-python310.txt` only when deliberately reproducing the
captured macOS development environment.

Fast software-only checks:

```bash
eegle check-setup --allow-missing-eeg
eegle run-forward --config configs/default_experiment.json --task pvt --task-mode dry-run --trials 2 --participant demo --skip-eeg --allow-missing-eeg
```

Focused verification while editing classifier or dashboard behavior:

```bash
python3 -m unittest tests.test_classification
python3 -m compileall -q reproduce tests
```

Broader verification:

```bash
python3 -m unittest discover -s tests
```

## Documentation Placement

Use scoped `AGENTS.md` files when a directory has local contracts that future
agents should read before editing. The useful scopes today are the repository
root, `reproduce/`, `configs/`, and `tests/`. Avoid adding agent guidance under
generated data directories.

Use `docs/` for durable human-facing explanations. Model-training, evaluation,
and research-goal material belongs in `docs/MODEL_TRAINING_TESTING_GOALS.md`,
not inside inline code comments.
