# EEGle Workbench

EEGle Workbench is a local, scientist-facing demonstration built beside the
EEGle package. It reads real EEGle project artifacts but does not add a second
compiler, runtime, recording authority, server, or database.

## Run the Workbench

Install EEGle from this checkout, then install the demo-only dependencies:

```bash
python3.11 -m venv .venv-workbench
.venv-workbench/bin/python -m pip install -e ".[live]"
.venv-workbench/bin/python -m pip install -r demos/workbench/requirements.txt
.venv-workbench/bin/python -m demos.workbench
```

The first launch creates the prepared Study 1 rehearsal project under the
ignored `data/workbench/projects/` directory through
`eegle.operations.create_project()`. Existing non-empty projects are never
overwritten.

The complete deterministic presentation path is available from Apparatus:
select **Simulation**, continue to Build, compile/preflight, and select
**Start Simulation**. The supervised EEGle runner registers the rehearsal,
Sessions inspects the real bundle, and Replay evaluates equivalence. Every
surface labels this path as simulation; it makes no hardware, task-timing, or
marker-synchronization claim.

## Dedicated PsychoPy task environment

Live Start requires a separate Python 3.12 interpreter with PsychoPy and pylsl.
The default location is `.venv-workbench-task`; it can also be selected from
Apparatus or supplied through `EEGLE_WORKBENCH_TASK_PYTHON`:

```bash
python3.12 -m venv .venv-workbench-task
.venv-workbench-task/bin/python -m pip install -e .
.venv-workbench-task/bin/python -m pip install -r demos/workbench/requirements-task.txt
export EEGLE_WORKBENCH_TASK_PYTHON="$PWD/.venv-workbench-task/bin/python"
```

For **Live LSL**, arm PsychoPy before the final scan so its `EEGleMarkers`
outlet is visible. Workbench then requires one exact 65-value, 1000 Hz EEG
stream, one exact task marker stream, and an explicit site review containing
the physical value order, unit, reference, ground, auxiliary allocation, and
selector. It recompares those identities and runs a fresh preflight immediately
before Start.

During a live run, EEGle is the only EEG/evidence authority. The PsychoPy child
owns display, responses, behavioral CSV/summary, and flip-timed task markers.
The pyqtgraph waveform and marker timeline use separate read-only LSL inlets.
Normal task completion drains the trailing marker and gracefully completes the
open capture; Stop registers a cancelled partial session where possible.

If the task interpreter is absent, Workbench may open its Qt presentation
fallback, but that fallback cannot enable live recording. Real Neuracle and
display acceptance remain an on-site laboratory gate.
