# Agent Orientation

This repository is migrating EEGle into an EEG-first, neurophysiology-general
framework for reproducing, recording, replaying, and validating synchronized
model systems. The target branch now requires Python 3.11+. The first cleanup
removed isolated legacy recipes/facades; selected Phase 6–8 extraction evidence
remains while the clean architecture is completed.

## First Read

Start here before changing code:

1. `docs/EEGLE.md` for the accepted product and architecture.
2. `docs/MIGRATION.md` for phase sequencing and gates.
3. `docs/MIGRATION_STATUS.md` for the active phase, decisions, and tasks.
4. `docs/PHASE0_INVENTORY.md` before moving, replacing, or deleting legacy code.
5. `docs/PHASE2_FOUNDATIONS.md` before building engine or compiler behavior.
6. `docs/PHASE7_AUTHORING_OPERATIONS.md` for the active authoring, operations,
   packaging, and integration work plan.
7. `docs/MODEL_PACKAGING.md` before changing model package, optional adapter,
   or replacement-replay behavior.
8. `README.md` for the current v1 foundation installation and API workflow.
9. `eegle/AGENTS.md` for package-level implementation guidance.
10. `configs/AGENTS.md` before changing legacy JSON experiment configs.
11. `tests/AGENTS.md` before deciding which tests to run.

`docs/ARCHITECTURE.md`, `docs/MODEL_TRAINING_TESTING_GOALS.md`, and other
pre-vision documents are legacy implementation evidence, not design authority.

## Legacy Current-Implementation Map

- `eegle/cli.py`: historical generic command implementation excluded from the wheel.
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
- `tests/`: target architecture tests plus selected Phase 6–8 scientific and
  integration evidence.

## Local Contracts

- Treat `data/` as generated and potentially sensitive. It is ignored by Git and
  should not be used for durable docs or fixtures unless the user explicitly asks.
- Classifier inference must stay label-blind. Do not pass stimulus condition,
  response correctness, or training labels into model input metadata.
- Dashboards bind to localhost and are non-critical observers of session
  artifacts; they should not block PsychoPy task execution.
- Model packages are content-addressed by their canonical indexes, manifests,
  and artifacts. Preserve pre-materialization hash validation when changing
  model transport or storage.
- New migration code follows the target boundaries and phase gates. Do not make
  a legacy module the permanent authority merely because it currently works.
- Do not delete protected Phase 6–8 evidence until its replacement acceptance
  test exists. Do not recreate deleted paths from the historical baseline.

## Development Commands

Install for development:

```text
macOS/Linux:
  python3.11 -m venv .venv
  source .venv/bin/activate
  python -m pip install -e ".[runtime]"

Windows PowerShell:
  py -3.11 -m venv .venv
  .venv\Scripts\Activate.ps1
  python -m pip install -e ".[runtime]"
```

Fast foundation checks:

```bash
python3 -m unittest tests.test_phase5_specs_compiler tests.test_source_boundaries
python3 -m compileall -q eegle tests
```

Focused verification while extracting model or validation behavior:

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
