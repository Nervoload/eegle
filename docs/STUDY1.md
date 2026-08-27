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
- The standard 200-trial blocks use the same weighted 50-trial strata as the
  complete profile: adjacent no-go trials are possible but down-weighted,
  three consecutive no-go trials are prohibited, and every block begins and
  ends with at least four go trials.
- Digits are 0-9. The counterbalanced no-go digit is stored in the participant
  manifest and reused across visits.
- Each digit is planned for 250 ms, followed by 1350 ms of fixation. The SOI is
  fixed at 1600 ms with no intentional jitter.
- Before LabRecorder starts, preflight opens the real PsychoPy window, measures
  the display, and verifies the asynchronous PTB keyboard queue. Only measured
  60 Hz and 120 Hz modes are accepted. The digit/SOI use 15/96 or 30/192 frames,
  respectively, on absolute VBlank boundaries.
- The cue extension creates 20 deterministic opportunities, grouped into five
  permuted blocks with two cue and two no-cue assignments in each block.

### Complete 1,000-trial acquisition profile

The Windows complete-run launcher opts Visit 1 into the separately identified
`full_1000_support500_query500_v2` profile. It does not change the standard
600-trial proposal profile above. The complete profile defaults to:

- 120-second eyes-open and 120-second eyes-closed baselines;
- the standard criterion-gated practice; after two failed rounds, the operator
  can retry without a fixed cutoff or proceed to the main task, followed by an
  explicit participant-ready confirmation before the main countdown. A round
  ends early only when the no-go target has become impossible to reach with its
  remaining planned no-go trials;
- four 250-trial sections with breaks after trials 250, 500, and 750;
- support trials 1-500, followed by held-out query trials 501-1000; and
- 150 no-go trials allocated `[38, 37, 38, 37]` across the sections.

The Windows launcher exposes logged operator overrides for baseline duration or
skip, experimental trial count, practice trial/no-go/round counts, and practice
skip. These change the visit/task artifacts, not the participant-level
scientific protocol hash. Exact prepared sequence hashes still prevent a resume
from silently changing experimental or initially planned practice shape. Any
operator-requested extra practice rounds are deterministically derived, assigned
unique negative trial IDs, and appended to the stimulus manifest. A completed
baseline is skipped on resume; a requested baseline skip creates no baseline
session folder.

## Resume and retained recordings

Study 1 resume is phase-aware. It never appends to an existing XDF and never
restarts a phase already recorded as completed. A completed baseline is reused.
If acquisition and validation completed but the final warning response was
declined or misread, resume presents that warning decision again and promotes
the same retained baseline/task session after acceptance; it does not reacquire
the baseline or experimental task.

The Windows Visit 1 launchers accept these minimal resume selectors:

```powershell
# Most recent incomplete Visit 1 for this participant.
.\scripts\windows\neuracle64\07-Run-Full.ps1 -Participant "sub-001" -Resume

# A visit ID or child run/session directory name under the selected data root.
.\scripts\windows\neuracle64\07-Run-Full.ps1 -Resume -ResumeTarget "visit-1-20260825T120000"
.\scripts\windows\neuracle64\07-Run-Full.ps1 -Resume -ResumeTarget "run-20260825T121500"

# An exact visit directory, visit_manifest.json, or retained child session path.
.\scripts\windows\neuracle64\07-Run-Full.ps1 -Resume -ResumeTarget "D:\EEGleData\study1\sub-001\visits\visit-1\visit-id\visit_manifest.json"
```

`-VisitId` may be used instead of `-ResumeTarget`. For a new acquisition,
`-Participant`, `-NoGoDigit`, `-Operator`, and `-ConfirmElectrodes` remain
required. Resume reloads the no-go digit, operator identity, and experimental/
practice shape from the durable participant and visit manifests. If a phase was
actually interrupted, its partial files are retained and that phase restarts in
a new child run directory.

The v2 no-go schedule corrects the periodic v1 fallback. Each 250-trial section
is generated independently from its participant-specific section seed. Its five
hidden 50-trial randomization strata contain seven or eight no-go trials in a
separately permuted order, so no-go events cannot accumulate at one end of a
section. Within those constraints, positions are sampled from a weighted
distribution: adjacent no-go trials and no-go trials separated by one go trial
remain possible, but receive weights `0.10` and `0.35` relative to gaps of two
or more go trials. Three consecutive no-go trials are prohibited. Every section
begins and ends with at least four go trials. These strata are schedule
constraints only and are not participant-visible task blocks.

The v1 evenly spaced schedule must not be used for new acquisition. Recordings
made with v1 retain their original protocol identity and must not be resumed or
silently pooled as v2 recordings.

Support and query are analysis roles, not participant-visible conditions. The
support reference is frozen after section 2. Keeping all support trials before
all query trials preserves the temporal holdout and prevents later behavior
from changing the reference used to label earlier query observations. The 500
support trials provide 425 planned go trials before behavioral exclusions,
which exceeds the labeler's preferred 200-valid-go reference threshold.

The profile has its own declaration metadata and changes the protocol hash.
Resume identity therefore rejects attempts to combine it with a standard or
smoke Visit 1 recording.

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

To exercise the real LSL, non-writing sample heartbeat, LabRecorder, XDF,
marker-receipt, and PyXDF validation path without an EEG amplifier, run a
synthetic acquisition rehearsal:

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
session writes `raw/recording.xdf` as its authoritative recording. A non-writing
LSL sample heartbeat reports live progress without duplicating the full EEG
amplitudes. The XDF is never appended across phases or visits.

On the Windows x64 acquisition computer:

1. Install LabRecorder 1.17.1 and the EEGle runtime dependencies, including
   `pyxdf>=1.17.5,<2`.
2. Put `LabRecorder.exe` on `PATH`, or replace
   `processes.recorder.executable` with its absolute path.
3. Ensure TCP port 22345 is free on loopback before starting Study 1.
4. Start the Neuracle LSL outlet, then run the normal Study 1 command. Preflight
   blocks before acquisition if LabRecorder, PyXDF, the port, or the required
   Neuracle stream is unavailable.

Preflight measures the effective source-timestamp rate and retained sample
fraction rather than trusting only the stream's declared rate. It also checks
for non-finite samples, flat or failed channels, long constant runs, repeated
extrema consistent with clipping, and excessive line noise. These acquisition-
quality findings are warnings: the operator receives a concise numbered list
and must type `Y`/`YES` (case-insensitive) to continue a live run, or `N`/`NO`
to decline. Missing required streams, invalid
channel identity/order, unavailable storage, and an unavailable XDF recorder
remain blocking setup errors.

When the managed XDF backend is enabled, preflight also launches LabRecorder
for a separate three-second probe, emits start/end markers, finalizes the file,
and runs the same bounded XDF integrity scan used after full phases. The probe
is retained under the visit's deliberately compact `preflight/xdfp` directory
as evidence that the actual XDF storage path—not only the live LSL inlet—worked.
The compact probe hierarchy keeps LabRecorder below Windows path-length limits.
Before any LabRecorder process is launched, EEGle also rejects an XDF destination
longer than its conservative 240-character Windows budget with an actionable
error, avoiding the LabRecorder state where remote commands succeed but the file
remains absent or at 0 kB.

The task process creates the run-specific marker outlet before the recorder
starts. The recorder worker resolves both required streams to their exact
`name (hostname)` LabRecorder rows, launches the visible LabRecorder application,
and enables its loopback remote-control socket. Prestarting and resolving the
marker is required: LabRecorder does not add a marker outlet that appears after
recording begins, and a name-only marker selection can create duplicate XDF
stream headers. The exact marker source ID and received marker sequence are
checked after LabRecorder stops.

The run-specific marker outlet is intentionally closed between independently
recorded phases. Liblsl may log a native `R_EEGleMarkers` transmission-broke
message while that receiver closes; it is not the Neuracle EEG stream and is
not itself a validation failure.

Before phase cleanup, the independent receipt waits for the exact number of
markers successfully emitted by the task. Baseline starts and Dynamic SART
stimulus onsets/offsets are captured on their PsychoPy flips; emitted and
received LSL timestamps, marker order, source identity, and delivery latency are
persisted and validated. LabRecorder is held open for a one-second tail guard
before its stop command. The worker then waits without a deadline for file-size
settlement and a PyXDF scan to confirm that the finalized file is structurally
readable. The terminal prints continuing finalization status. Neither the
recorder worker nor its manager uses a forced-kill fallback.

Live XDF file growth is not itself a liveness gate because LabRecorder may
buffer Windows disk writes. A 15-second growth pause is logged as a warning.
The non-writing LSL sample heartbeat reports live sample progress and timestamp
anomalies. Heartbeat degradation, timestamp gaps, stalled visible XDF growth,
sample-rate mismatch, and signal-quality findings are warnings and do not stop
LabRecorder or the task.

At launch and shutdown, EEGle consumes LabRecorder's remote-control `OK`
acknowledgement after each `update`, `filename`, `start`, and `stop` request.
This serializes LabRecorder's UI-thread operations so a stream refresh cannot
race the filename or recording-start command. These acknowledgements do not
replace the XDF gate: the expected file must still appear, finalize, parse, and
pass stream and marker validation before a recording phase is accepted.
LabRecorder process exit, XDF startup/finalization failure, unavailable storage,
and loss of required marker/stream structure remain blocking acquisition
failures.

A completed acquisition phase must contain:

- `raw/recording.xdf` and `raw/xdf_metadata.json`;
- the independent `raw/lsl_markers_received.csv` receipt;
- a terminal `logs/processes/recorder.status.json` with status `stopped`; and
- an XDF integrity section with no structural failures.

`raw/eeg.csv` is intentionally absent for Study 1. The full CSV mirror is
disabled so that XDF acquisition and finalization have priority.

If LabRecorder itself exits, the shared recorder health gate reports that the
authoritative acquisition has already failed. XDF structural/finalization
failures retain available raw files. Progress, timestamp, rate, retention, and
signal-quality warnings do not automatically interrupt acquisition.

For the operator-confirmed Neuracle positional mapping, XDF descriptor names
may be canonicalized from the stable, preflight-selected XDF stream identity.
Conflicting channel counts, stream identities, meaningful positional labels,
or marker receipts remain failures. Sample-rate, sample-count, timestamp-gap,
non-finite, flatline, and possible-clipping findings are retained as warnings.

After every baseline or task phase, the XDF scan checks the complete
stored signal for sample retention, effective rate, timestamp continuity,
non-finite amplitudes, flat/failed channels, long constant runs, and possible
clipping. A live operator must explicitly accept any resulting warning list
before Study 1 advances. Report-copy, report-read, or unexpected validation-
report exceptions do not invalidate an otherwise retained recording.

PyXDF synchronized timestamps, the independently received marker sequence, and
the stable selected stream identities are the Study 1 alignment evidence.
Because the CSV bridge is disabled, a genuine failure to retain the required
EEG or marker stream remains separately visible rather than being hidden by a
secondary recording.

BDF, photodiode/audio loopback qualification, and the full modeling suite
remain separate follow-up work.
