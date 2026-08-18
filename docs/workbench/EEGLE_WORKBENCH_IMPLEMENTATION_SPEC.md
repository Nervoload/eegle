# EEGle Workbench — Implementation Specification

Status: Codex handoff  
Target: local PySide6 desktop demo  
Architecture authority: `split` commit `8105b2f9f1d96a630b7c8b377450fd86254243f9`  
Study 1 source: `neuracletest` commit `29d9b88f2a6653291e7cf6d9677286a8f9e4b59b`

## 1. Implementation directive

Build EEGle Workbench as a local Python desktop application beside EEGle's core package. Use PySide6 for the interface and pyqtgraph for the read-only signal preview.

Do not introduce a web server, REST interface, database, user accounts, authentication, cloud deployment, or a second experiment representation. Workbench calls EEGle's Python application services directly and supervises only local child processes.

Keep Workbench demo dependencies outside EEGle's base wheel. The preferred location is:

```text
demos/workbench/
├─ README.md
├─ requirements.txt
├─ app.py
├─ controller.py
├─ state.py
├─ settings.py
├─ runner.py
├─ task_runner.py
├─ profiles/
│  ├─ study1_neuracle64_demo.json
│  └─ profile_provenance.json
├─ tasks/
│  └─ study1_dsart.py
├─ views/
│  ├─ experiments.py
│  ├─ design.py
│  ├─ apparatus.py
│  ├─ build.py
│  ├─ run.py
│  ├─ sessions.py
│  └─ replay.py
├─ widgets/
│  ├─ sidebar.py
│  ├─ status_badge.py
│  ├─ deployment_table.py
│  ├─ preflight_table.py
│  ├─ eeg_preview.py
│  ├─ marker_timeline.py
│  └─ evidence_summary.py
└─ tests/
   ├─ test_state.py
   ├─ test_study1_profile.py
   ├─ test_controller_simulation.py
   └─ test_runner_protocol.py
```

Initial `requirements.txt`:

```text
PySide6
pyqtgraph
```

The development environment must also install EEGle with `eegle[live]`. PsychoPy is required only for the live DSART task process; use the existing `runtime` optional dependency or a Workbench-specific environment constraint.

## 2. Branch-integration rule

The two branches are conceptually related but not runtime-compatible.

- `split` owns the target `ExperimentDesign → DeploymentSpec → ExecutionPlan/ExecutionLock → ExecutionEngine → EvidenceBundle` path.
- `neuracletest` owns a legacy visit-aware Study 1 pipeline that directly manages preflight, baseline, LabRecorder, DSART sessions, manifests, and retry/resume behaviour.

Do **not** import `neuracletest` modules at runtime and do **not** launch its `study1.py` pipeline beside the `split` runtime. That would duplicate lifecycle and recording authority.

Instead:

1. Extract only the prepared Study 1 design constants and task-presentation behaviour needed by the demo.
2. Store a versioned Workbench profile with explicit provenance: source branch, commit, source file paths, Git blob hashes (`study1.py` `b233bd32e4233360e19a2f563edbd969abac6695`; config `397a35120a7ab6bc3779959df30708eef02483c0`), and a local profile digest.
3. Convert that profile into a real `split` `ExperimentDesign` through a single `build_study1_design(profile)` function.
4. Port the DSART presentation into a demo-only external task process. It owns display, keyboard input, behavioural output, and LSL marker publication; it does not own EEG recording.
5. Let the compiled `split` runtime be the sole authority for admitted EEG/marker packets and EEGle evidence.

The port should preserve the legacy marker semantics where practical: task start/end, practice, block start/end, break start/end, stimulus onset/offset, response-window close, trial complete, support complete, and abort. Marker values must remain label-blind with respect to model input; ground-truth task metadata may be recorded as outcomes but must not leak into causal model inputs.

## 3. Prepared Study 1 profile

Create `profiles/study1_neuracle64_demo.json` as a small, UI-oriented profile, not a copy of the entire legacy config. Include:

- protocol and profile identity;
- full versus rehearsal variants;
- Visit 1 baseline and phase structure;
- practice thresholds;
- digits, No-Go allocation requirement, response keys, trial timing, block schedule, and support/query labels;
- logical EEG and marker contracts;
- prestimulus window;
- observe-only policy;
- display defaults;
- expected session outputs;
- unresolved site-local values.

The full variant uses the source values exactly. The rehearsal variant may reduce baselines and trial counts but must include `scientific_status: "demonstration_only"` and must produce a session kind that cannot be confused with a Study 1 visit.

The source config leaves exact channel names, physical input mapping, reference, ground, and electrooculography allocation pending. Do not hard-code guessed values. Add a site-local apparatus overlay selected on the Apparatus page. Live Study 1 readiness requires a reviewed overlay with:

- exact 65-value LSL order;
- exact 64 physiological input names/order;
- reserved `TRIGGER_STATUS` position;
- units;
- reference;
- ground;
- auxiliary-channel allocation;
- exact stream selector (`uid` preferred, then `source_id`);
- storage path and display information.

If that overlay is missing, Workbench may preview the stream in Bench test but must not present the Study 1 deployment as ready.

## 4. EEGle service mapping

Use public package surfaces; do not call the command-line interface as the normal GUI backend.

| Workbench action | EEGle surface |
| --- | --- |
| Create project | `eegle.operations.create_project(..., design=ExperimentDesign)` |
| Open project | `eegle.operations.open_project()` |
| Explain design | `eegle.operations.explain_project()` |
| Show graph | `eegle.operations.graph_project()` after compilation |
| Detect LSL | `eegle.integrations.lsl.detect_lsl()` |
| Combine capabilities | `eegle.operations.detect_capabilities(sources=..., clocks=...)` |
| Propose binding | `eegle.operations.propose_project_deployment()` |
| Compile live proposal | `eegle.operations.compile_project(deployment_role="deployment_proposal")` |
| Compile simulation | `eegle.operations.compile_project(deployment_role="simulation_deployment")` |
| Preflight | `eegle.operations.preflight_project()` |
| Rehearse | `eegle.operations.rehearse_project()` |
| Run | `eegle.operations.run_project()` through `runner.py` |
| Inspect | `eegle.operations.inspect_session()` |
| Replay | `eegle.operations.replay_session()` |
| Compare replacement | `eegle.operations.compare_session_models()` |

The live detection path is:

```python
lsl = detect_lsl(wait_time=1.0)
report = detect_capabilities(
    sources=lsl.sources,
    clocks=lsl.clocks,
    include_entry_points=True,
)
proposal = propose_project_deployment(
    project_root,
    report,
    selection=reviewed_selection,
)
compilation = compile_project(
    project_root,
    deployment_role="deployment_proposal",
)
preflight = preflight_project(
    project_root,
    detection_report=report,
    operator_confirmations=acknowledged_confirmations,
)
```

`detect_lsl()` returns typed stream identities, sources, clocks, and support state. Present those values; do not reinterpret unknown units or invent absent channel labels. `propose_project_deployment()` produces immutable proposal evidence but does not authorize selection by itself. The GUI must require explicit review before compile.

## 5. Authoring the prepared design

Build a real `ExperimentDesign`; do not create a parallel GUI-only graph.

The first executable slice should be recording/event-locked observation, not a complete online attention-forecasting model. The prepared design should declare:

- dense 65-value source stream with the reviewed contract and 1000 Hz nominal rate; the reserved `TRIGGER_STATUS` value is declared but excluded from the 64-channel physiological analysis path;
- sparse marker event stream;
- source-preserving or identity processing chain;
- causal event window from −0.75 s to −0.10 s for `dynamic_sart_stimulus_onset` events;
- phases representing baseline/practice/support/query as far as the current compiler can validly express them;
- recording for EEG, markers, execution capture, and semantic evidence;
- no actions and no stimulation;
- optional observer model only when its plugin and model package are installed and preflighted.

`ExperimentDesign` already supports signals, events, processing chains, continuous/event windows, quality gates, models, comparisons, outcomes, adaptation, calibration, policies, actions, phases, recording, and acceptance. Expose only the subset required by this demo.

Task markers do not directly control the engine. Workbench supervises both children: normal task completion allows a bounded trailing-marker drain and requests graceful runtime completion; operator Stop requests cancellation and preserves a partial session. Both requests and reasons are evidence.

## 6. State model

Keep one immutable-ish application state object owned by the main controller. Views render state and emit intents; they do not call EEGle directly.

Recommended lifecycle:

```text
NO_PROJECT
  → DESIGN_READY
  → APPARATUS_UNBOUND
  → DEPLOYMENT_REVIEW
  → DEPLOYMENT_ACCEPTED
  → COMPILED
  → PREFLIGHT_WARNING | PREFLIGHT_READY
  → ARMED
  → RUNNING
  → SESSION_AVAILABLE
  → REPLAYED
```

State must store identities, not duplicated scientific payloads:

```python
@dataclass(frozen=True)
class WorkbenchState:
    project_root: Path | None
    project_id: str | None
    page: Page
    lifecycle: Lifecycle
    environment_mode: EnvironmentMode
    design_revision: str | None
    detection_report_hash: str | None
    deployment_proposal_hash: str | None
    plan_hash: str | None
    lock_hash: str | None
    preflight_report_hash: str | None
    active_session_id: str | None
    active_bundle_id: str | None
    stale_reasons: tuple[str, ...]
    warnings: tuple[UiIssue, ...]
```

Hold the actual EEGle objects in the controller or reload them from project artifacts. Never serialize arbitrary Python objects into Workbench recency settings.

Invalidation rules:

- design edit invalidates deployment, compile, and preflight;
- environment/binding edit invalidates compile and preflight;
- compilation invalidates earlier preflight;
- a new scan after preflight marks readiness stale until the exact selected streams are rechecked;
- UI-only changes such as sidebar width do not affect scientific state.

## 7. Controller and view responsibilities

### Main controller

- Own `WorkbenchState` and page transitions.
- Execute short EEGle operations on a worker thread and marshal results to the Qt main thread.
- Convert `OperationError` diagnostics into structured UI issues.
- Persist recent project paths and non-scientific UI preferences with `QSettings`.
- Never treat recency metadata as the project registry or scientific authority.

### Views

- Receive view models; do not read project files opportunistically.
- Emit typed intents such as `scan_requested`, `proposal_accepted`, `compile_requested`, and `run_requested`.
- Disable the primary action when prerequisites are absent.
- Show explicit stale state rather than silently rebuilding.

### Primary action / Enter

Each page registers one current primary command. A top-level shortcut triggers it only if:

- the command is enabled;
- no modal is open;
- focus is not an editable/input widget;
- the command is not Stop, Force terminate, Delete, or overwrite.

## 8. Process architecture

Run the GUI, EEGle execution, and DSART task as separate local processes:

```text
Workbench (PySide6)
├─ independent LSL preview inlet       visualization only
├─ runner.py                           split run_project authority
└─ task_runner.py → study1_dsart.py    PsychoPy + keyboard + markers
```

Use `QProcess` for both children. Communicate with newline-delimited JSON over stdin/stdout; reserve stderr for human-readable diagnostics.

Minimum runner messages:

```json
{"type":"ready","session_id":"session.demo.001"}
{"type":"state","state":"running"}
{"type":"warning","code":"...","message":"..."}
{"type":"completed","project_run":{}}
{"type":"failed","error":{}}
```

Minimum task messages:

```json
{"type":"armed","marker_stream":{}}
{"type":"phase","phase":"support"}
{"type":"trial","current":148,"total":600}
{"type":"completed","behavior_summary":{}}
{"type":"aborted","reason":"operator"}
```

Do not pass participant names or unbounded raw data through the UI event log.

## 9. Controlled runtime lifecycle

The provisional operations surface now exposes an operations-owned `RunControl` without exposing `ExecutionEngine` to Workbench. `run_project()`, `run_locked_plan()`, and `rehearse_project()` accept the control and optional operator controller.

Before the polished live demo, add a small general-purpose operation-layer control surface, for example:

```python
control = RunControl()

run_project(
    project_root,
    session_id=session_id,
    control=control,
    operator=operator_controller,
)

# Called from runner.py's command-listener thread.
control.complete(reason="task_complete")  # orderly queue drain and completed run
control.cancel(reason="operator_stop")     # cancelled/partial run
```

`RunControl` attaches internally after runtime components (and therefore LSL inlets) are constructed. The first terminal request wins. `cancel()` delegates to reason-aware engine cancellation; `complete()` stops polling open live sources and drains admitted/queued work. A typed `run_control_requested` record and terminal reason persist into the bundle, `ProjectRun`, and inspection. The request itself and source-only lifecycle state are non-comparable during replay because recorded sources replace live sources. Tests cover:

- completion or cancellation before engine attachment;
- graceful task completion during open live sources;
- cancellation during an open live source;
- task-complete versus operator-stop reason;
- evidence writer finalization;
- session status and inspection after cancellation;
- live-source cleanup, writer finalization, and equivalent replay after graceful completion.

Force termination remains a last-resort interrupted outcome, never normal completion.

## 10. External task process

The task is an external laboratory environment. Workbench may launch and supervise it, but task presentation does not become arbitrary code embedded in `ExperimentDesign`.

Port the minimal DSART behaviour from `neuracletest`:

- deterministic sequence generation from participant pseudonym, Visit 1 segment, and master seed;
- explicit participant-specific No-Go digit;
- practice rounds and thresholds;
- 0.25 s digit presentation and fixed 1.6 s onset interval;
- space-bar responses with reaction-time validation;
- support/query block identities and breaks;
- LSL marker outlet armed before deployment detection/preflight;
- flip-timed stimulus markers when PsychoPy is active;
- behavioural CSV/JSON summary under the Workbench task session area;
- clean abort and task-end marker.

The task process must support two profiles:

- `full_study1`: exact prepared protocol;
- `demo_rehearsal`: short, visibly labelled, never written as a real visit.

Task arming and run ordering:

1. launch task process with profile and session metadata;
2. create LSL marker outlet and emit `armed` to Workbench;
3. scan and bind both EEG and marker streams;
4. compile and preflight;
5. launch EEGle runner;
6. after runner reports ready/running, send `start` to task;
7. when task emits completed, request graceful EEGle completion with reason `task_complete`;
8. inspect the resulting session and reconcile task summary with marker evidence.

## 11. Live preview

The Workbench preview opens its own read-only LSL inlet to the selected EEG stream. It must not share buffers with the EEGle runtime, emit task markers, modify preprocessing, or write authoritative evidence.

Implementation constraints:

- label the plot `LIVE MONITOR — visualization only`;
- show 8–16 selected channels by default, not all 65 at once;
- maintain a bounded ring buffer, e.g. 5–10 s;
- update at a UI-friendly rate, e.g. 15–30 frames/s, independent of 1000 Hz acquisition;
- perform only display transforms such as offset/scaling; do not call them scientific preprocessing;
- expose effective preview rate and disconnect state;
- stop and close the inlet when leaving the apparatus/run context or switching stream.

The marker timeline may subscribe independently to the selected marker stream. It is also visual-only.

## 12. Preflight and warning policy

Map EEGle `PreflightCheck` results directly. Do not collapse warnings and failures into a single red/green light.

Three classes are sufficient:

1. **Blocking contract/integrity failures** — wrong source identity, incompatible channel/rate contract, missing required marker source, invalid plan/lock, missing plugin/artifact, unwritable evidence storage. Cannot proceed as Study 1.
2. **Acknowledgable quality warnings** — noisy/flat preview channels, moderate line noise, refresh-rate uncertainty, imperfect effective preview rate, non-critical metadata gaps. May proceed after explicit acknowledgement if EEGle preflight remains ready.
3. **Informational checks** — observer model disabled, optional analysis unavailable, simulation support level.

Bench test deliberately relaxes the Study 1 contract so the user can detect, preview, and perform a short diagnostic recording. Its header, session kind, and result must say `BENCH TEST`; it is not an override switch on the scientific deployment.

## 13. Sessions and replay

Populate Sessions from project session registrations and `inspect_session(session_root)`. Do not reconstruct integrity, source health, phase timelines, latency, adaptation, actions, or replay state from GUI logs.

The first replay slice should:

- select a published bundle;
- call `replay_session()`;
- show requested/evaluated equivalence, result status, issues, and first divergence;
- retain links to plan and bundle hashes.

Model comparison is implemented only after a prepared replacement plan can be compiled and its plugin/model artifacts are available. Call `compare_session_models()` with explicit replaced component IDs. If those prerequisites are absent, disable the control with a specific explanation.

## 14. Functional versus demonstrative controls

| Surface | v1 status |
| --- | --- |
| Study 1 preset, project create/open | functional |
| Overview/structure/source display | functional, mostly read-only |
| Bounded rehearsal parameter edits | functional |
| LSL scan, stream metadata, preview | functional |
| Exact binding and deployment review | functional |
| Compile, graph, preflight | functional |
| Simulation rehearsal | functional |
| External DSART task launch | functional |
| Live EEGle run and evidence | software-functional; laboratory acceptance pending |
| Session inspection | functional |
| Replay | functional |
| One prepared model replacement | optional final slice |
| Arbitrary visual graph editing | out of scope |
| General model training | out of scope |
| Cohort analytics | out of scope |
| Cloud sync/multi-user permissions | out of scope |

Disabled controls must look disabled and explain their scope. Do not create clickable no-ops.

## 15. Delivery slices

### Slice A — shell and simulation

- PySide6 shell, sidebar, page routing, state machine, recents.
- Prepared Study 1 design display.
- Create simulation project, compile, graph, preflight, rehearse/run, inspect, replay.
- Tests run without LSL, PsychoPy, or display hardware.

### Slice B — LSL apparatus

- `detect_lsl()` and capability report.
- Stream table, exact selector, proposal review, live compilation/preflight.
- Independent EEG and marker previews.
- Bench-test path remains disabled with an explanation in this pass.

### Slice C — DSART + Neuracle

- Ported external DSART task and task-runner protocol.
- Reviewed site-local Neuracle overlay.
- Controlled graceful completion and partial cancellation.
- End-to-end rehearsal, then real Neuracle acceptance run.

### Slice D — replay and comparison

- Rich inspection projection.
- Replay equivalence/divergence view.
- Optional prepared model replacement and comparison.
- Presentation polish and failure recovery.

## 16. Verification

Minimum automated checks:

- state transitions and stale invalidation;
- Study 1 full profile matches all frozen source constants used by the demo;
- rehearsal profile cannot claim real-study status;
- exact LSL selector and ambiguous-stream handling;
- deployment proposal requires explicit acceptance;
- live compile uses `deployment_proposal`, not simulation deployment;
- preflight failure blocks Study 1 Start;
- Bench test remains separately labelled;
- task is armed before final detection/preflight;
- runner does not execute on the Qt thread;
- graceful completion and controlled cancellation both finalize inspectable evidence with distinct statuses;
- preview inlet cannot write project artifacts;
- replay displays structured unavailable/diverged results without mutating the session.

Manual hardware acceptance sequence:

1. Neuracle stream visible with reviewed 65-value contract.
2. DSART marker stream armed and visible.
3. Exact streams bound and proposal accepted.
4. Compile and preflight ready, with acknowledged warnings documented.
5. Short live rehearsal produces EEG and task markers.
6. Controlled task completion produces an inspectable session.
7. Source health and packet-loss projections are plausible.
8. Replay completes or reports a precise supported equivalence ceiling/divergence.
9. Full protocol remains selectable but was not silently shortened.

## 17. Done criteria

The Workbench is demo-ready only when the primary presenter walkthrough can be completed from a clean launch without terminal intervention, synthetic metadata masquerading as hardware, duplicate recording authorities, or force-killing the normal run.
