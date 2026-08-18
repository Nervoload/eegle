# EEGle Workbench — Demo Vision and Feature Specification

Status: implementation handoff  
Primary codebase: [`Nervoload/eegle@split`](https://github.com/Nervoload/eegle/tree/split), commit `8105b2f9f1d96a630b7c8b377450fd86254243f9`  
Study 1 reference: [`Nervoload/eegle@neuracletest`](https://github.com/Nervoload/eegle/tree/neuracletest), commit `29d9b88f2a6653291e7cf6d9677286a8f9e4b59b`

## 1. Product statement

EEGle Workbench is a local desktop demonstration of how a non-coding scientist can define an electroencephalography (EEG) experiment, bind it to available laboratory hardware and task software, compile and check the exact configuration, run a session, inspect what happened, and replay the evidence.

The demo tells one coherent story:

> Here is my experiment. Here is the EEG hardware and task software available today. EEGle binds the two without changing the scientific design. It compiles an immutable execution plan, checks the laboratory environment, runs the Dynamic Sustained Attention to Response Task (DSART), records causal evidence, and makes the session inspectable and replayable.

The Workbench is not a visual wrapper around Python source code and is not a complete experiment-development environment. It is a streamlined, scientist-facing projection of EEGle's real architecture.

## 2. Demonstration objective

The primary live demonstration must be able to run the Study 1 DSART Go/No-Go task using a Neuracle wet-electrode system exposed through Lab Streaming Layer (LSL), while the `split` architecture remains authoritative for experiment authoring, device binding, compilation, preflight, runtime evidence, inspection, and replay.

The prepared Study 1 experiment is based on the `neuracletest` branch's [`eegle/pipelines/study1.py`](https://github.com/Nervoload/eegle/blob/neuracletest/eegle/pipelines/study1.py) and [`configs/study1_neuracle64.json`](https://github.com/Nervoload/eegle/blob/neuracletest/configs/study1_neuracle64.json). The demo defaults shown in the interface are:

| Design element | Prepared value |
| --- | --- |
| Protocol | `study1_dynamic_sart_v1`, observe-only |
| Visit 1 baseline | 120 s eyes open + 120 s eyes closed |
| Main task | 3 blocks × 200 trials |
| Block roles | 200 support + 400 query trials |
| No-Go schedule | 30 planned No-Go trials per 200-trial block (15%) |
| Stimulus | digits 0–9; participant-specific No-Go digit |
| Stimulus duration | 0.25 s |
| Stimulus-onset interval | fixed 1.6 s; no inter-trial jitter |
| Response | space bar; minimum valid reaction time 0.1 s |
| Practice | 30 trials/round, up to 3 rounds; explicit performance thresholds |
| Causal EEG window | −0.75 s to −0.10 s before stimulus onset |
| EEG source | Neuracle, 65 LSL values at 1000 Hz: 64 physiological inputs plus reserved `TRIGGER_STATUS` |
| Marker source | `EEGleMarkers`, type `Markers` |
| Feedback/action | disabled; observe-only; no stimulation or task adaptation |

The full protocol is the scientific preset. A clearly labelled **Demo rehearsal** may shorten baselines and trials for presentations. A shortened rehearsal must never be displayed or stored as a completed Study 1 visit.

## 3. Scope and truthfulness contract

### The demo must really do

- Create or open an EEGle project.
- Load a prepared Study 1 design and expose a bounded set of scientist-relevant parameters.
- Detect visible LSL streams and show their actual metadata.
- Bind logical EEG and marker requirements to exact detected streams through a reviewable deployment proposal.
- Compile the selected deployment into an EEGle `ExecutionPlan` and `ExecutionLock`.
- Run EEGle preflight and distinguish blocking failures from non-blocking warnings.
- Launch the external DSART task and run a live or simulated EEGle session.
- Show a read-only live EEG preview labelled as visualization-only.
- Persist and inspect the resulting EEGle session and evidence.
- Replay a completed session and show equivalence or first divergence.

### The demo may simplify

- Only the prepared Study 1 experiment needs complete authoring and execution support.
- New Experiment may offer only a small set of presets.
- Experiment forks may be shallow copies with changed metadata rather than a general version-control system.
- The live execution graph may show configured/running/completed state without instrumenting every node.
- Model replacement may initially support one prepared replacement model and one comparison path.
- Advanced tabs and secondary actions may be disabled with an honest “Not included in this demo” explanation.

### The demo must not pretend

- Do not show synthetic values as detected hardware metadata.
- Do not silently accept a deployment proposal.
- Do not let a visual EEG inlet become EEGle's recording or timing authority.
- Do not show a preflight failure as “ready.”
- Do not label a shortened rehearsal as a Study 1 participant session.
- Do not run the legacy `study1.py` recorder concurrently with the `split` runtime; this would create two recording authorities.
- Do not implement inert controls that appear to have executed scientific actions.

## 4. Application shell

The visual style is restrained and desktop-native: a narrow sidebar, one active content page, neutral surfaces, subtle borders, compact status pills, and limited accent colour. The reference feeling is the clarity of the ChatGPT desktop application and Obsidian, not a medical-device control panel.

```text
┌──────────────────────────────────────────────────────────────────────┐
│ eegle     Study 1 — DSART      LIVE LSL       READY / WARNING       │
├─────────────────┬────────────────────────────────────────────────────┤
│ + New Experiment│                                                    │
│ Experiments   ▾ │                                                    │
│ Replay        ▾ │                                                    │
│─────────────────│                 ACTIVE PAGE                        │
│ ACTIVE EXPERIMENT                                                    │
│ Design          │                                                    │
│ Apparatus       │                                                    │
│ Build Settings  │                                                    │
│ Run Experiment  │                                                    │
│ Sessions        │                                                    │
│                 │                                      [Continue →] │
└─────────────────┴────────────────────────────────────────────────────┘
```

Global shell behaviour:

- The header always shows the active experiment, environment mode, and highest-severity status.
- The sidebar's Active Experiment section appears only when a project is open.
- The active page is highlighted; completed pages receive a small check; pages requiring attention receive a warning dot.
- A persistent primary action in the lower-right advances the canonical workflow.
- Enter invokes that primary action only when focus is not inside a text field, combo box, table editor, or modal. It must never submit a destructive confirmation.
- Back navigation is allowed. If an upstream scientific or deployment input changes, downstream compilation, preflight, and readiness become stale and are visibly invalidated.
- Long operations show progress and remain cancellable where the underlying operation supports cancellation.
- Status is expressed with text and icon, not colour alone.

## 5. General pages

### 5.1 New Experiment

The New Experiment button opens a compact creation surface rather than a blank visual-programming canvas.

Prepared choices:

1. **Study 1 — DSART + Neuracle 64** — primary, fully supported demo.
2. **Event-locked observer** — simple simulation/reference project.
3. **Continuous recording** — quick hardware diagnostic project.
4. **Open existing EEGle project** — select a local project directory.

For the Study 1 preset, the user enters an experiment name and optional description, chooses Full protocol or Demo rehearsal, then selects **Create Experiment**. The project is created using EEGle's authoring/project services; Workbench does not maintain an independent scientific source of truth.

### 5.2 Experiments

The sidebar dropdown shows recently opened experiments. Selecting the Experiments label opens a full page with a tree-oriented project browser.

The page groups related experiments without inventing a database:

```text
Study 1 — DSART
├─ study1-neuracle64                 active
├─ study1-neuracle64-window-test     fork
└─ study1-neuracle64-demo-rehearsal  fork

Reference experiments
├─ lsl-observe-only
└─ first-simulation
```

Each card or row shows name, description, last opened time, current lifecycle state, mode, and latest session. Supported actions are Open, Fork, Reveal folder, and Archive from recent list. Archive only removes Workbench recency metadata; it does not delete the project.

### 5.3 Replay

The sidebar dropdown shows recently inspected sessions. The full page title is **Replay & Compare** because this is the general offline area, not merely a transport replay button.

The page supports:

- selecting an experiment and session;
- inspecting integrity, sources, timing, phases, model activity, and issues;
- running authoritative EEGle replay;
- viewing equivalence level and first divergence;
- choosing one prepared replacement model and comparing results;
- linking back to the source experiment and its Sessions page.

Training new models, arbitrary notebooks, cohort analysis, and a general analytics workbench are future areas. In this demo they appear only as disabled navigation affordances if needed for storytelling.

## 6. Active Experiment workflow

The five Active Experiment pages form one progressive path. The user may switch pages manually, but the primary Continue action follows Design → Apparatus → Build Settings → Run Experiment → Sessions.

### 6.1 Experiment Design

Purpose: show the portable scientific intent before any local device or path is selected.

The top block contains:

- Experiment name and short scientific statement.
- Preset identity and source revision.
- Full protocol / Demo rehearsal badge.
- Observe-only safety badge.
- Optional participant/session fields that are run metadata rather than portable design.

Below, use bounded collapsible cards rather than a drag-and-drop language:

1. **Protocol structure** — Visit 1, baseline, practice, support block, query blocks, breaks.
2. **Task parameters** — digits, No-Go allocation, trial counts, stimulus timing, response key.
3. **Signals and events** — logical EEG signal, DSART marker stream, expected channels/rate/units.
4. **Processing and windows** — raw/source-preserving recording and causal prestimulus window.
5. **Models and policy** — observe-only, no active model required for the first live slice; optional prepared observer model.
6. **Recording and acceptance** — selected inputs, evidence capture, and concise acceptance criteria.

The default view is a human-readable **Overview**. Secondary tabs are **Structure**, **Graph**, and **Source JSON**. JSON is read-only in v1.

Only a small set of fields is editable in the demo: experiment name, description, run profile, baseline duration in rehearsal mode, trial count in rehearsal mode, display mode, and optional observer model. Scientific defaults remain visible and changes are summarized before saving.

Primary action: **Save Design & Continue**.

### 6.2 Apparatus

Purpose: show how portable intent is bound to the laboratory that exists now.

At the top, select one environment:

- **Simulation** — deterministic streams for rehearsal.
- **Live LSL** — detect real LSL streams and installed EEGle plugins.
- **Bench test** — quick stream preview/test recording; labelled diagnostic and not a Study 1 run.

For Live LSL, the primary action begins as **Scan for devices**. The page then shows actual detected streams, for example:

```text
● NeuracleEEG              EEG       65 values    1000 Hz
  source_id / uid / host / format / channel metadata

● EEGleMarkers             Markers   irregular
  source_id / uid / host
```

The binding table maps portable requirements to physical capabilities:

| Experiment requirement | Selected capability | Compatibility |
| --- | --- | --- |
| `signal.eeg` | exact Neuracle LSL stream | channels, rate, unit, clock |
| `event.markers` | exact EEGle marker stream | sparse events, LSL clock |
| `device.clock → boundary.clock` | LSL clock mapping | available/uncertain |
| evidence storage | local project session directory | writable/free space |

The user may choose among compatible streams when detection is ambiguous. Exact identifiers are shown in a disclosure panel.

The page also contains optional checks:

- sample preview and effective rate;
- flat channels, clipping, timestamp gaps, and line-noise warnings;
- expected channel order and the reserved `TRIGGER_STATUS` channel;
- reference, ground, and auxiliary-channel notes;
- display availability and refresh rate;
- marker round-trip test;
- storage path and free-space estimate.

Quality warnings may be acknowledged and carried forward. Contract failures—such as no matching EEG stream, wrong channel count for the chosen Study 1 profile, or no required marker source—cannot be relabelled as ready. A scientist who only wants to inspect hardware uses Bench test, which remains deliberately separate from a valid Study 1 session.

The final action is **Review Deployment**. The review sheet states exactly which physical resources will satisfy each logical requirement. The user selects **Accept Proposal & Continue**; detection alone never authorizes a deployment.

### 6.3 Build Settings

Purpose: turn the saved design and accepted deployment into an immutable, checked execution.

The page has three compact sections.

**Build configuration** shows selected design revision, deployment, evidence location, plugins, model package if any, and execution mode. Advanced settings are collapsed.

**Compile** shows a linear result:

- portable design resolved;
- reviewed deployment selected;
- plugins and contracts validated;
- `ExecutionPlan` hash;
- `ExecutionLock` hash;
- View compiled graph.

**Preflight** is a table of checks with Passed, Warning, Failed, or Unavailable status. It includes lock integrity, source identity, signal contract, rate, marker stream, clock mapping, plugins, artifacts, evidence storage, and operator confirmations. Warnings have acknowledgements; failures link to the Apparatus or Design field that must change.

The primary button is **Compile & Preflight** until a fresh ready report exists, then **Continue to Run**. Simulation additionally offers **Rehearse**. Changing an upstream setting makes the plan and preflight visibly stale.

### 6.4 Run Experiment

Purpose: provide a calm operator surface before, during, and immediately after acquisition.

Before running, show a final summary:

- project and immutable plan hash;
- Full protocol or Demo rehearsal;
- participant pseudonym, visit, and operator;
- selected EEG and marker streams;
- task profile, baseline, blocks, trials, and expected duration;
- evidence destination;
- preflight timestamp and warnings;
- observe-only/no-stimulation statement.

The operator arms the external DSART task so its marker outlet is visible, confirms the participant is ready, then selects **Start Session**. The ordering is explicit:

1. task process armed and marker outlet visible;
2. current LSL environment rechecked;
3. EEGle runner started from the locked plan;
4. evidence recording confirmed;
5. task receives Start;

During execution, the page emphasizes four areas:

- **Live monitor** — a subset of channels plotted from an independent read-only LSL inlet and labelled “visualization only.”
- **Marker timeline** — stimulus, response-window, block, break, and task markers.
- **Task progress** — baseline/practice/block, trial count, elapsed time, response summary, and current support/query phase.
- **Execution health** — runner state, selected sources, effective rate, packet-loss observations, reconnect count, latest warning, and evidence record count where available.

The Stop control requests controlled cancellation and requires confirmation. Emergency process termination is a separate last-resort action and is reported as an interrupted/partial session, never a clean completion.

When the task ends, the runner requests a graceful, evidence-recorded completion, Workbench inspects the session, and the page changes to a compact result: Completed, Cancelled/Partial, or Failed; evidence integrity; sources; records; warnings; and **Open Session**.

### 6.5 Sessions

Purpose: make the durable result of execution immediately understandable.

The left pane lists sessions for the active experiment with timestamp, profile, status, duration, and integrity. Selecting one opens:

- Overview;
- Sources;
- Phases and task timeline;
- Timing and latency;
- Models;
- Evidence and integrity;
- Issues.

The authoritative content comes from EEGle session inspection, not from transient GUI state. Participant-identifying fields are not shown by default.

Primary actions are **Replay Session** and, when a prepared replacement exists, **Compare Model**. Both open the general Replay & Compare page with the experiment, session, and bundle already selected.

## 7. Canonical presenter walkthrough

The polished live walkthrough should take approximately five minutes before the task is allowed to continue or is stopped after a short rehearsal:

1. Open **Study 1 — DSART + Neuracle 64** from Recent Experiments.
2. On Design, show the pre-existing protocol: 600 trials, support/query structure, fixed timing, causal window, observe-only policy.
3. Continue to Apparatus and scan LSL.
4. Show the real Neuracle stream, the external DSART marker outlet, and the logical-to-physical bindings.
5. Accept the reviewable deployment proposal.
6. Compile and point to the immutable plan and lock hashes.
7. Run preflight; explain warnings versus blocking failures.
8. Start a Demo rehearsal and show real EEG, marker events, task progress, and evidence health.
9. Stop through controlled cancellation or let the short task complete.
10. Open Sessions and show exactly what was recorded, including any packet loss or interruption.
11. Replay the session and show equivalence or first divergence.
12. Optionally select a prepared replacement model and compare outputs.

## 8. Definition of demo success

The demo is successful when a new operator can complete the primary path without a terminal and can answer:

- What experiment is this?
- Which exact EEG and marker streams were used?
- What did EEGle compile and preflight?
- Is the run real, simulated, diagnostic, or shortened?
- What is running now, and what warnings exist?
- Where is the durable session evidence?
- Can EEGle replay the session and localize disagreement?

Visual polish is secondary to this causal chain. If a control does not strengthen the chain, it is not required for v1.
