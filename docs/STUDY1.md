# Study 1 acquisition pipeline

`study1` is the proposal-specific, visit-aware Dynamic SART pipeline. It reuses
EEGle's existing PsychoPy task, deterministic sequence generator, LSL preflight,
raw recorder lifecycle, resting baseline, child-process isolation, analysis,
and resume machinery.

## Current protocol structure

- Visit 1: preflight, 120-second eyes-open baseline, 120-second eyes-closed
  baseline, practice, and three 200-trial cue-free blocks.
- Visit 2: independent preflight, 120-second eyes-open baseline, 600 cue-free
  trials (200 support and 400 immutable query), then a separate 400-trial cue
  extension.
- Every 200-trial block contains exactly 30 no-go trials.
- Digits are 0-9. The counterbalanced no-go digit is stored in the participant
  manifest and reused across visits.
- Stimuli are planned for 250 ms with stimulus-onset intervals sampled from
  1.75-2.15 seconds before the task starts.
- The cue extension creates 20 deterministic opportunities, grouped into five
  permuted blocks with two cue and two no-cue assignments in each block.

## Software rehearsal

Run both visits against an approved temporary session root:

```bash
study1 --participant rehearsal-001 --visit 1 --visit-id rehearsal-v1 \
  --task-mode dry-run --smoke --skip-eeg --session-root /path/to/rehearsal-data

study1 --participant rehearsal-001 --visit 2 --visit-id rehearsal-v2 \
  --task-mode dry-run --smoke --skip-eeg --allow-visit-interval-override \
  --session-root /path/to/rehearsal-data
```

`--allow-visit-interval-override` is a logged pilot-only exception. Without it,
Visit 2 must start 2-7 days after a completed Visit 1.

This `--skip-eeg` path does not start LabRecorder and therefore does not create
or test an XDF file. Missing XDF is reported as `skipped`, not as a passing XDF
validation.

To exercise the real LSL, LabRecorder, XDF, CSV-mirror, marker-receipt, and
PyXDF validation path without an EEG amplifier, run a synthetic acquisition
rehearsal:

```bash
python -m eegle.pipelines.study1 \
  --participant xdf-rehearsal-001 --visit 1 --visit-id xdf-rehearsal-v1 \
  --task-mode dry-run --smoke --simulate-eeg --baseline-seconds 0 \
  --labrecorder-executable /absolute/path/to/LabRecorder \
  --session-root /path/to/rehearsal-data
```

`--simulate-eeg` is accepted only with `--task-mode dry-run`, cannot be combined
with `--skip-eeg` or `--allow-missing-eeg`, and labels the visit as synthetic and
not participant data. It publishes a 1000 Hz, 65-value Neuracle-like stream
using the observed live transport contract: 59 scalp EEG channels, `ECG`,
`HEOR`, `HEOL`, `VEOU`, `VEOL`, and a final empty `TRIGGER_STATUS` value. The
first 64 values correspond to physical inputs; value 65 is preserved raw but
excluded from derived EEG analysis.

## Participant allocation

The first live visit requires an explicitly assigned counterbalanced digit:

```bash
study1 --participant sub-001 --visit 1 --no-go-digit 7 --operator operator-id
```

Later runs read the digit from `participant_manifest.json`. A conflicting digit,
master seed, participant identity, or protocol hash is rejected.

## Intentional live gates

The candidate configuration cannot yet start live EEG acquisition. Before the
gate is removed, populate `hardware.eeg.expected_channel_names` with the exact
65-value Neuracle W64 outlet order and lock the reference, ground, and EOG roles.

Cue opportunities, randomization, trial fields, and assignment markers are
implemented. Physical auditory delivery is still marked `assignment_only`, so
Visit 2 PsychoPy execution stops before acquisition until the cue sound and
delivery-timing contract are implemented. The software does not claim that an
assigned cue was delivered.

## Managed XDF acquisition

Study 1 uses the managed `labrecorder_xdf` backend. Each baseline or task child
session writes `raw/recording.xdf` as its authoritative recording and keeps
`raw/eeg.csv` plus `raw/eeg_metadata.json` as an independent acquisition mirror.
The XDF is never appended across phases or visits.

On the Windows x64 acquisition computer:

1. Install LabRecorder 1.17.1 and the EEGle runtime dependencies, including
   `pyxdf>=1.17.5,<2`.
2. Put `LabRecorder.exe` on `PATH`, or replace
   `processes.recorder.executable` with its absolute path.
3. Ensure TCP port 22345 is free on loopback before starting Study 1.
4. Start the Neuracle LSL outlet, then run the normal Study 1 command. Preflight
   blocks before acquisition if LabRecorder, PyXDF, the port, or the required
   Neuracle stream is unavailable.

The task process creates the run-specific marker outlet before the recorder
starts. The recorder worker resolves both required streams to their exact
`name (hostname)` LabRecorder rows, launches the visible LabRecorder application,
and enables its loopback remote-control socket. Prestarting and resolving the
marker is required: LabRecorder does not add a marker outlet that appears after
recording begins, and a name-only marker selection can create duplicate XDF
stream headers. The exact marker source ID and received marker sequence are
checked after LabRecorder stops.

Before terminating LabRecorder, the worker waits for file-size settlement and
for a bounded PyXDF scan to confirm that the finalized file is structurally
readable. This prevents a partially flushed XDF from being reported as stopped.

A completed acquisition phase must contain:

- `raw/recording.xdf` and `raw/xdf_metadata.json`;
- `raw/eeg.csv` and `raw/eeg_metadata.json`;
- the independent `raw/lsl_markers_received.csv` receipt;
- a terminal `logs/processes/recorder.status.json` with status `stopped`; and
- passing XDF and CSV integrity sections in the session validation output.

If LabRecorder exits, the XDF stops growing, the CSV mirror stalls, or disk
space falls below the configured reserve, the shared recorder health gate stops
the baseline/task. Finalization and validation failures retain both raw files
but prevent the phase from being marked complete.

BDF, photodiode/audio loopback qualification, and the full modeling suite
remain separate follow-up work.
