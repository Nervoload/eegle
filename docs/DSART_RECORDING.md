# DSART8 and DSART32 Recording Runbook

These are recording-first visits. They deliberately disable online inference,
model loading, task adaptation, stimulation, and dashboard dependencies. The
canonical outputs are raw EEG, task events, full key-event logs, published LSL
marker timestamps, the precomputed stimulus plan, and visit/session manifests.

## Active acquisition contract

Only two critical components run during a DSART recording: the PsychoPy task
process and the isolated LSL CSV recorder process. Realtime processing,
realtime capture, filtering workers, model inference, feedback, dashboards,
calibration, and the managed offline analyzer are disabled.

EEG sample amplitudes are written in incoming LSL order and native units. The
recorder performs no filtering, resampling, scaling, re-referencing, artifact
rejection, interpolation, or channel-value reordering. Mapping generic NIC
channel names to the verified montage changes CSV headers only; original device
labels and the mapping source remain in `raw/eeg_metadata.json`.

The recorder also disables LSL inlet clock-sync, dejitter, and monotonization so
the device-provided timestamp is not destroyed. `raw/eeg.csv` retains:

- `source_lsl_timestamp`: the timestamp received from the EEG outlet;
- `lsl_time_correction_seconds`: the measured remote-to-local LSL correction;
- `lsl_timestamp`: their sum, used to align EEG with task markers;
- `local_received_time`: the independent local monotonic receive estimate;
- followed by untouched EEG sample values.

Metadata explicitly declares the amplitude and timestamp contract. The suite
will not accept a live child session if those provenance fields or timestamp
columns are absent.

Stimulus onset and offset markers are scheduled with PsychoPy `callOnFlip`.
The flip callback only captures monotonic/LSL clocks and pushes the run-specific
LSL marker. Disk logging occurs after `flip()` returns. Every event ledger row
retains the exact pushed LSL timestamp, flip timestamp, expected visual-onset
timestamp, and marker source ID. Initial and post-break preflights independently
loop a marker through LSL; post-session validation checks marker/trial parity,
finite LSL timestamps, and one run-specific source ID.
Validation also rejects duplicate trial indices, non-increasing stimulus clocks,
onsets that were not captured on a display flip, invalid onset/offset/response
ordering, overlapping response windows, and EEG timestamp spans that do not
cover the recorded task or baseline markers.

The current CSV recorder does not independently subscribe to and persist the
marker stream during the task. The task ledger proves the label, source ID, and
explicit timestamp supplied to LSL; the preflight loopback proves that a local
subscriber can receive that stream before acquisition. It does not prove
receipt of every runtime marker by a second process. If independent receipt in
one multi-stream container is required, record the EEG and marker outlets with
LabRecorder/XDF as an additional validated recording path before participant
collection; that path is not yet managed by these recipes.

Per-stimulus telemetry is disabled in these recipes. Raw task rows still flush
incrementally, while the large stimulus manifest checkpoints only at block
boundaries. Behavioral label generation and report analysis begin only after
the recorder is closed.

## Before the participant

Install the runtime environment and verify that the installed commands resolve:

```bash
python -m pip install -e ".[runtime,analysis]"
dsart8 --help
dsart32 --help
```

Run one complete software rehearsal. The dry run advances virtual task and
baseline time, so it does not take the real visit duration:

```bash
dsart8 --participant rehearsal --visit-id rehearsal-001 --task-mode dry-run \
  --skip-eeg --allow-missing-eeg --break-seconds 0 --output-root data/rehearsal
```

Do not use `--allow-missing-eeg` for participant acquisition. The suite rejects
that flag unless `--skip-eeg` explicitly selects a software-only recording.

To exercise the real PsychoPy windows and complete suite lifecycle without EEG,
run ten experimental trials per session, two seconds per baseline condition,
and no inter-session wait:

```bash
dsart8 --participant local-smoke --visit-id smoke-001 --task-mode psychopy \
  --trials 10 --baseline-seconds 2 --break-seconds 0 --skip-eeg \
  --window-size 1000 700 --output-root data/rehearsal
```

Shortened `--trials` rehearsals skip participant qualification practice by
default, so `--trials 10` means exactly ten experimental trials in each
session. Add `--include-practice` when the purpose of the rehearsal is to test
practice instructions, scoring, and repeat/failure screens. Full 600-trial
participant runs include practice in session 1 automatically; session 2 follows
the configured no-repeat-practice rule. These choices and duration overrides
are recorded in the suite manifest and are never treated as a scientific
recording.

DSART windows are windowed at 1000 by 700 pixels by default. Use
`--window-size WIDTH HEIGHT` to choose another launch size. The formal recording
recipes reject full-screen configuration so an accidental config edit cannot
restore the failure-prone launch behavior. Keyboard input
uses the same `psychopy.event` path as the existing Go/No-go task, with one
shared normalizer for event names and timestamps. Space, Escape, and Q are
recognized consistently in the baseline, practice, experimental trials,
breaks, and completion screen.

Verify NIC is publishing one EEG LSL stream at 500 Hz with the expected channel
count. Close unrelated EEG outlets before starting. The suites reject an
incorrect count, missing or duplicate mapped labels, wrong label order, an
unwritable output root, a failed sample probe, or a failed marker loopback.

## Channel contracts

DSART8 uses device-channel order:

| Device channel | Label |
| ---: | --- |
| 1 | Fz |
| 2 | Cz |
| 3 | Pz |
| 4 | C3 |
| 5 | C4 |
| 6 | P3 |
| 7 | P4 |
| 8 | Oz |

DSART32 uses this device-channel order:

| Device channel | Label | Device channel | Label |
| ---: | --- | ---: | --- |
| 1 | T7 | 17 | FC5 |
| 2 | CP1 | 18 | Fp1 |
| 3 | C1 | 19 | AF3 |
| 4 | Pz | 20 | Fz |
| 5 | CP2 | 21 | PO4 |
| 6 | T8 | 22 | C2 |
| 7 | Cz | 23 | C4 |
| 8 | Oz | 24 | FC2 |
| 9 | FC6 | 25 | CP6 |
| 10 | AF4 | 26 | P4 |
| 11 | P8 | 27 | P3 |
| 12 | F4 | 28 | CP5 |
| 13 | Fp2 | 29 | C3 |
| 14 | F8 | 30 | FC1 |
| 15 | P7 | 31 | F3 |
| 16 | F7 | 32 | PO3 |

The supplied mapping contained a collision: Fp1=17 and FC5=17, with channel 18
unused. The recipe infers Fp1=18 and retains FC5=17. This is an explicit
assumption, not a silently corrected fact. Confirm it against the configured
NIC montage and physical electrode/cable labels before using
`--confirm-channel-map`. If the physical layout differs, change the profile and
config before recording; do not relabel the data afterward without a versioned
correction record.

The 32-channel session also writes `events/dsart8_overlap_channels.json` so the
eight labels shared with DSART8 can be selected reproducibly later. All 32
channels remain canonical raw data.

## Participant commands

Start NIC/LSL first, confirm the correct cap and participant identity, and then
run exactly one visit command:

```bash
dsart8 --participant sub-001 --visit-id visit-20260716 --operator operator-id
```

```bash
dsart32 --participant sub-002 --visit-id visit-20260716 --operator operator-id \
  --confirm-channel-map
```

The command performs, in order:

1. software, display, storage, LSL, sample, channel-order, marker-loopback, and
   electrode/contact preflight;
2. an operator acceptance gate;
3. an independently recorded 120-second eyes-open baseline;
4. an independently recorded 120-second eyes-closed baseline;
5. DSART session 1: practice plus 600 experimental trials;
6. a 600-second break with explicit boundary markers;
7. a second full preflight and operator electrode/contact gate;
8. DSART session 2: 600 experimental trials, without repeated practice by
   default.

Each 600-trial plan is generated before task execution from its own persisted
seed. It contains 200 support trials, 400 query trials, and exactly 67 no-go
trials. The support reference freezes once, after trial 200. The second session
uses a different deterministic seed so it is not a duplicate sequence.

Support and query blocks look identical to the participant by design. The first
200 support trials establish a participant/session-specific valid-go reaction
time reference. The following 400 query trials retain the same stimuli, timing,
and response rule but cannot update that frozen reference. Before experimental
trial 1, after practice passes or is skipped, the task displays the mandatory
one-second sequence `5`, `4`, `3`, `2`, `1`, `GO!`; all six display onsets are
written as flip-synchronized countdown markers.

At each live preflight the terminal prints the report path and warnings. Inspect
NIC contact/impedance and type the uppercase word `YES` only when the montage is
acceptable. A JSON quality report is retained even if the gate is declined.

## Failure and resume rules

- Never delete, rename, or reuse a partial child session during the visit.
- A baseline and each task session use separate recorder lifecycles and session
  directories. A later crash does not invalidate an earlier completed child.
- Each PsychoPy task session launches in a fresh Python process. This prevents
  native macOS/Pyglet state left by a closed baseline or prior task window from
  corrupting the next window. Worker request/result artifacts are retained in
  the visit's `phase_workers/` directory for diagnosis.
- Trial rows and marker logs flush incrementally. The full planned sequence is
  written before the first experimental trial.
- During live DSART, a task-independent recorder-health monitor checks the
  managed recorder status, heartbeat freshness, and advancing sample count at
  every trial boundary. Recorder exit or a five-second sample stall aborts the
  task while preserving all completed rows and raw samples.
- The same recorder-health monitor runs throughout both resting phases. The
  eyes-open fixation is drawn and flipped once, then key and recorder status are
  polled without continuously re-flipping an unchanged screen.
- Baseline and task windows, marker outlets, preflight loopback inlets, raw EEG
  inlets, and inter-session break outlets all close through guarded cleanup
  paths, including operator aborts and caught exceptions.
- The recorder also watches its manager process. On systems that reparent an
  orphaned child (including macOS), a native supervisor crash causes the raw LSL
  recorder to stop itself instead of leaking indefinitely in the background.
- No EEG recorder is active during the inter-session break. A break-marker
  failure is therefore retained as a warning instead of aborting the visit; the
  mandatory second marker loopback remains the hard gate before Session 2.
- The managed offline-analyzer worker is disabled during acquisition. A local
  DSART consistency report is attempted only after the recorder closes; report
  failure is retained as a warning and never turns valid raw EEG into a
  re-record instruction.
- Pressing Escape/Q aborts the active task and closes its recorder. The visit
  manifest marks the attempt partial.
- A caught child-session exception writes its full traceback to
  `logs/suite_exception.txt`; an interpreter-level worker failure remains a
  structured failed phase instead of being mistaken for task completion.
- Resume only from the parent suite command. Do not point `run-forward` at a
  partial child directory.

```bash
dsart8 --participant sub-001 --visit-id visit-20260716 --operator operator-id --resume
```

```bash
dsart32 --participant sub-002 --visit-id visit-20260716 --operator operator-id \
  --confirm-channel-map --resume
```

The suite skips completed phases and creates a new child run directory for an
incomplete DSART phase. If `--visit-id` is omitted with `--resume`, EEGle finds
the newest incomplete visit for that participant and recipe; specifying the ID
is safer during participant acquisition.

## Minimum handoff artifacts

Retain the parent
`data/recording_suites/<participant>/<visit>/<recipe>/recording_suite.json` and
every child directory it references. A usable child contains at least:

- `parameters.json` and `manifest.json`;
- `raw/eeg.csv` and `raw/eeg_metadata.json`;
- `events/events.jsonl`, `events/behavior.csv`, and `triggers.txt`;
- `events/stimulus_manifest.json` and `events/dynamic_sart_trials.jsonl`;
- `reports/dynamic_sart_summary.json` and `session_summary.json`.

Do not use a folder name alone to infer completion. Use the parent phase status,
child validation object, raw recorder status, completed trial count, marker/trial
parity, support-boundary count, and abort flag.
