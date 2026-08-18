# EEGle Workbench — Implementation Tracker

This is the lightweight execution ledger for the Workbench demo. The vision document defines the product behaviour; the implementation specification defines the technical contract. This file records only current status, blockers, decisions that change implementation, and the evidence that a slice is complete.

Do not turn this into a second specification or a diary. Update it at the end of a meaningful implementation batch.

## Status legend

- `not started` — no accepted implementation exists.
- `in progress` — active work; name the immediate next check.
- `blocked` — progress requires a decision, dependency, or laboratory resource.
- `done` — acceptance evidence is linked or summarized.

## Current snapshot

Last updated: 2026-08-17  
Overall status: `in progress`  
Current slice: Complete Study 1 demonstration software path; laboratory acceptance remains blocked  
Next demonstrable checkpoint: connect the laboratory Neuracle stream, accept its exact site overlay, validate PsychoPy on the intended display, and complete the two-minute rehearsal.

## Delivery slices

| Slice | Status | Acceptance checkpoint | Evidence |
| --- | --- | --- | --- |
| A. Shell + simulation | done | Prepared Study 1 design → compile → preflight → run/rehearse → inspect → replay | Controller-driven end-to-end test compiles `simulation_deployment`, runs the supervised child, inspects a valid replay-ready session, and returns equivalent replay; both supported window sizes render |
| B. LSL apparatus | in progress | Detect/bind simulated and live LSL streams; preview remains non-authoritative | Exact selection, 65-value positional overlay evidence, fresh identity/preflight recheck, ambiguity/disappearance rejection, separate 8-position/10-second/20 Hz preview, marker timeline, and inlet cleanup are software-tested; hardware acceptance remains blocked |
| C. DSART + Neuracle | in progress | External task + reviewed Neuracle contract + controlled, inspectable live session | PsychoPy renderer, deterministic full/rehearsal plans, practice thresholds, flip-timed markers, behavioral CSV/hashed summary, strict task/runner protocols, graceful completion, partial cancellation, session inspection, and sidecar reconciliation are implemented; on-site display and Neuracle acceptance remain |
| D. Replay + comparison | in progress | Replay result and optional prepared model replacement shown in GUI | Bundle selection, result/equivalence level, compared-record count, issues, and first divergence are functional; model replacement remains explicitly unavailable for this observe-only design |

## Required pre-demo gaps

| Gap | Status | Resolution criterion |
| --- | --- | --- |
| Public controlled lifecycle for `run_project()` | done | `RunControl` retains pre-attachment requests, first request wins, live completion drains queued work, cancellation stays partial, reasons persist through inspection, sources/writers close, and graceful live capture replays equivalently |
| Reviewed Neuracle 65-value site overlay | blocked | Review UI and immutable proposal evidence exist; exact laboratory channel order, units, reference, ground, auxiliary allocation, selector, and display facts must be supplied and accepted on site |
| Demo-only DSART task port | in progress | Software implementation and protocol tests are complete; the dedicated Python 3.12 environment, intended display, refresh measurement, keyboard behavior, and marker counts require a manual laboratory run |

## Active work

The full simulation path and live orchestration software are implemented and
software-tested. Laboratory acceptance is the active continuation.

When work begins, keep at most one row per active batch:

| Batch | Owner | Status | Next check |
| --- | --- | --- | --- |
| Study 1 laboratory acceptance | Lab operator | blocked | Real Neuracle + task marker scan, reviewed overlay, PsychoPy display acceptance, complete/stop run, inspection, and replay |

## Verification evidence

- Workbench suite: 29 tests passed, including offscreen rendering at 1440×900 and 1120×720.
- Full repository discovery: 435 of 436 tests passed. The remaining packaging test could not install the pinned build dependency because this environment cannot reach PyPI; there were no product-code failures.
- Focused runtime, operations, LSL, public-boundary, packaging-source-boundary, and Workbench checks passed.
- `compileall` and `git diff --check` passed; the two light-theme screenshots were visually inspected.
- Manual Neuracle, intended-display PsychoPy, and physical marker-count acceptance remain blocked on laboratory hardware.

## Blockers

1. The source Study 1 config intentionally leaves the exact Neuracle channel mapping, reference, ground, and electrooculography allocation pending. These require laboratory confirmation before a valid live Study 1 deployment.
2. No Neuracle stream is visible in the current development environment, and the dedicated PsychoPy display path cannot be manually accepted off site. Simulation remains the truthful presentation fallback.

## Decision log

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-08-17 | PySide6 desktop app; no server/database | Direct use of EEGle Python services and local laboratory processes |
| 2026-08-17 | `split` is the runtime/architecture authority | It contains the target authoring, compiler, operations, evidence, and replay surfaces |
| 2026-08-17 | `neuracletest` is design/task reference only | Running its visit pipeline beside `split` would duplicate lifecycle and recording authority |
| 2026-08-17 | Bench test is separate from Study 1 overrides | Scientists retain hardware-testing freedom without mislabelling invalid scientific runs |
| 2026-08-17 | Slice A0 opens a real prepared project in a read-only UI shell | The presentation has truthful pages and state now without fabricating discovery, compilation, readiness, runtime, or session evidence |
| 2026-08-17 | Qt DSART is an explicitly labelled presentation preview | It gives the immediate demo a real external task and marker outlet without claiming PsychoPy timing validation or EEG recording |
| 2026-08-17 | Reviewed positional overlay projects detected values onto neutral logical positions | Physical labels and exact selector remain preserved in proposal evidence; no electrode identities are inferred |
| 2026-08-17 | `RunControl` is an operations-owned provisional supervisor boundary | Workbench can request complete/cancel without receiving the engine; first-request-wins makes task/operator races deterministic |
| 2026-08-17 | PsychoPy and Workbench use separate interpreters | The task-only Python 3.12 path isolates PsychoPy while the desktop shell keeps Workbench-only dependencies |
| 2026-08-17 | Simulation and live use the same supervised Run/Sessions/Replay workflow | The fallback demonstrates real EEGle execution/evidence without impersonating hardware or task synchronization |

## Completion evidence template

For a slice marked `done`, record only:

- commit or pull request;
- automated checks run and result;
- manual demo path exercised;
- hardware/simulation environment;
- known limitations that remain outside that slice.

## Handoff rule

The next agent should read, in order:

1. `EEGLE_WORKBENCH_DEMO_VISION.md`
2. `EEGLE_WORKBENCH_IMPLEMENTATION_SPEC.md`
3. this tracker
4. repository `AGENTS.md` and scoped guidance before editing code

Then update Current snapshot and Active work before implementation, and update them again when yielding the task.
