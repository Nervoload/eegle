# Agent Orientation

This repository is migrating EEGle into an EEG-first, neurophysiology-general
framework for reproducing, recording, replaying, and validating synchronized
model systems. The target branch now requires Python 3.11+; substantial legacy
EEG application code remains while the clean architecture is built.

## First Read

Start here before changing code:

1. `docs/EEGLE.md` for the accepted product and architecture.
2. `docs/MIGRATION.md` for phase sequencing and gates.
3. `docs/MIGRATION_STATUS.md` for the active phase, decisions, and tasks.
4. `docs/PHASE0_INVENTORY.md` before moving, replacing, or deleting legacy code.
5. `docs/PHASE2_FOUNDATIONS.md` before building engine or compiler behavior.
6. `README.md` for the still-current legacy installation and operator workflow.
7. `eegle/AGENTS.md` for package-level implementation guidance.
8. `configs/AGENTS.md` before changing legacy JSON experiment configs.
9. `tests/AGENTS.md` before deciding which tests to run.

`docs/ARCHITECTURE.md`, `docs/MODEL_TRAINING_TESTING_GOALS.md`, and other
pre-vision documents are legacy implementation evidence, not design authority.

## Legacy Current-Implementation Map

- `eegle/cli.py`: installed `eegle` command and generic subcommands.
- `eegle/pipelines/`: specialized operator workflows such as `alpha8`,
  `inhibition8`, and `classify8`.
- `eegle/experiment.py`: forward experiment orchestration.
- `eegle/feedback_manager.py`: managed recorder, realtime, dashboard, and
  analyzer worker lifecycle.
- `eegle/tasks/`: PsychoPy and dry-run task implementations.
- `eegle/realtime/`: ring buffers, preprocessing, epoching, model adapters,
  classifier contracts, policies, and feedback emitters.
- `eegle/workers/`: process entrypoints for recorder, realtime processor,
  dashboard, and offline analyzer.
- `eegle/analysis/`: post-session reports, ERP/P300 analysis, classifier
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
- New migration code follows the target boundaries and phase gates. Do not make
  a legacy module the permanent authority merely because it currently works.
- Do not delete a legacy path until the cleanup precondition in
  `docs/PHASE0_INVENTORY.md` is satisfied.

## Legacy Current-Implementation Commands

Install for development:

```text
macOS/Linux:
  python3.11 -m venv .venv
  source .venv/bin/activate
  python -m pip install -e ".[runtime,ml]"

Windows PowerShell:
  py -3.11 -m venv .venv
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
python3 -m compileall -q eegle tests
```

Broader verification:

```bash
python3 -m unittest discover -s tests
```

## Documentation Placement

Use scoped `AGENTS.md` files when a directory has local contracts that future
agents should read before editing. The useful scopes today are the repository
root, `eegle/`, `configs/`, and `tests/`. Avoid adding agent guidance under
generated data directories.

Use `docs/` for durable human-facing explanations. Amend `docs/EEGLE.md` only
for an intentional architecture decision, `docs/MIGRATION.md` for phase/gate
changes, and `docs/MIGRATION_STATUS.md` for live progress. Treat legacy model,
recipe, and operator documents as evidence until their replacements exist.
