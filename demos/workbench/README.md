# EEGle Workbench

EEGle Workbench is a local, scientist-facing demonstration built beside the
EEGle package. It reads real EEGle project artifacts but does not add a second
compiler, runtime, recording authority, server, or database.

## Check the host first

Before opening the interface on a new machine, run the headless self-test. It
drives the same code the interface drives, without Qt, and prints a full stack
for anything that fails:

```bash
python -m demos.workbench.diagnose
```

It reports the interpreter and encodings, whether the GUI and LSL dependencies
load, whether recorded evidence paths fit this host's limits, and whether the
complete simulation path (compile, preflight, rehearse, inspect, replay) and
the supervised runner protocol both succeed. Warnings do not block the
simulation demo; failures are real host problems.

## Run the Workbench

Install EEGle from this checkout, then install the demo-only dependencies:

```bash
python3.11 -m venv .venv-workbench
.venv-workbench/bin/python -m pip install -e ".[live]"
.venv-workbench/bin/python -m pip install -r demos/workbench/requirements.txt
.venv-workbench/bin/python -m demos.workbench
```

On Windows, use the same steps with the Windows interpreter layout:

```bat
py -3.11 -m venv .venv-workbench
.venv-workbench\Scripts\python -m pip install -e ".[live]"
.venv-workbench\Scripts\python -m pip install -r demos/workbench/requirements.txt
.venv-workbench\Scripts\python -m demos.workbench
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

## Windows notes

Three host differences matter on Windows. `python -m demos.workbench.diagnose`
reports all three.

**Evidence path length.** A session evidence artifact nests a session id, a
bundle id, and a content-addressed digest, which reaches roughly 180
characters below the project root. Windows rejects non-extended paths beyond
260 characters, so a checkout under `Documents` can cross the limit. EEGle
writes evidence through extended-length paths so recording itself succeeds,
but Explorer, archive tools, and editors cannot open such a session. Point
Workbench at a shorter directory to keep sessions reachable:

```bat
set EEGLE_WORKBENCH_DATA_ROOT=%LOCALAPPDATA%\EEGle\Workbench\projects
```

If the in-repository location would cross the limit, Workbench selects that
per-user directory on Windows by default. POSIX hosts are never relocated.

**LSL discovery.** `pylsl` needs the native `liblsl` library. Confirm pip
selected a `win_amd64` wheel, since the pure-python sdist omits `lsl.dll`;
loading it also requires the Microsoft Visual C++ Redistributable (x64). Once
it loads, discovery is UDP multicast, so `python.exe` must be allowed through
Windows Defender Firewall on private networks — a blocked host resolves zero
streams while reporting a perfectly healthy library. Hosts with several
adapters (Wi-Fi, Ethernet, virtual switches) may need a longer wait than the
2.5 second default:

```bat
set EEGLE_WORKBENCH_LSL_WAIT_SECONDS=6
```

The Apparatus page now states which of these applies after every scan instead
of showing an empty table.

**Launching.** Start Workbench with `python.exe`, not `pythonw.exe`. Workbench
supervises its runner and task children over line-delimited JSON on their
standard streams, which a GUI-only interpreter does not provide. Workbench
substitutes the console interpreter beside `pythonw.exe` when it can find one,
and pins UTF-8 and unbuffered pipes for both children.
