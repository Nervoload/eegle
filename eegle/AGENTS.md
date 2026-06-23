# EEGle Package Guidance

`eegle/` contains the implementation behind the installed `eegle`, `alpha8`,
`inhibition8`, and `classify8` commands.

## Package Map

- `cli.py`: generic command parser and command handlers.
- `pipelines/`: top-level operator workflows that configure and call shared
  orchestration.
- `experiment.py`: forward task plus recording orchestration.
- `feedback_manager.py`: managed process lifecycle for recorder, realtime
  processor, dashboard, and offline analyzer.
- `session.py`: BciPy-inspired session directory creation.
- `telemetry.py`: JSONL telemetry and console routing.
- `tasks/`: PVT and Go/No-go task implementations plus registry metadata.
- `realtime/`: online buffers, preprocessing, epoching, model contracts,
  adapters, policies, and feedback emitters.
- `workers/`: process entrypoints.
- `analysis/`: post-session reports, ERP/P300 analysis, classifier scoring, and
  replay.
- `calibration/`: posterior-alpha calibration helpers.
- `devices/` and `hardware/`: LSL and hardware discovery/setup helpers.

## Classifier Invariants

- Keep shared epoch quality and model metadata sanitation in
  `realtime/classification.py`.
- Keep training and inference adapter behavior in `realtime/models.py`.
- Keep online worker loading, primary/shadow model execution, and prediction row
  emission in `workers/realtime_processor.py`.
- Keep `classify8` CLI workflow wiring in `pipelines/classify8.py`.
- Do not feed ground-truth labels, stimulus condition, or response correctness
  into online model metadata.
- Do not let `classify8 demo` write real classifier prediction artifacts.

## Realtime and Worker Rules

- `FeedbackManager` owns worker startup, readiness, shutdown, and status files.
  Prefer extending that lifecycle instead of starting unmanaged processes from
  tasks.
- Realtime model decisions should become action records. Task code should accept
  or reject actions only at deterministic boundaries.
- Dashboards are observers of session artifacts. They should remain safe to
  disable and should not be required for task completion.
- Preserve replayability when changing realtime behavior. If online output
  changes, update replay and evaluation code in `analysis/` as needed.

## Verification Pointers

```bash
python3 -m unittest tests.test_realtime_synchrony
python3 -m unittest tests.test_closed_loop_model_system
python3 -m unittest tests.test_classification
python3 -m compileall -q eegle
```
