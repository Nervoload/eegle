# Study 1 acquisition pipeline

`study1` is the proposal-specific, visit-aware Dynamic SART pipeline. It reuses
EEGle's existing PsychoPy task, deterministic sequence generator, LSL preflight,
raw recorder lifecycle, resting baseline, child-process isolation, analysis,
and resume machinery.

This is the canonical end-to-end Study 1 guide. The shorter
[`NEURACLE64_WINDOWS_TEST.md`](NEURACLE64_WINDOWS_TEST.md) runbook is useful at
the acquisition computer, but the workflow and command semantics below are
authoritative.

## End-to-end workflow

The production Windows Visit 1 path is intentionally staged. Complete the
software and hardware tests on the acquisition computer before using a
participant identity:

| Stage | Command or script | Result |
| --- | --- | --- |
| Install | `00-Setup.ps1` | Python 3.10 environment, runtime dependencies, and local display config |
| Prove storage | `06-Test-StorageAccess.ps1` | Parent and child-process write/flush/rename/read/delete probe |
| Test display/task | `01-DryRun-Task.ps1` | PsychoPy Dynamic SART without EEG or LabRecorder |
| Discover EEG | `02-Test-NeuracleLsl.ps1 -DiscoverOnly` | Retained Collect/LSL stream and sample report |
| Lock hardware contract | `02-Test-NeuracleLsl.ps1 -ConfirmCapContract ...` | Machine-local live configs and full physical/XDF preflight |
| Test short recording | `03-Run-EEGTaskTest.ps1` | A 10-20 trial authoritative XDF recording |
| Rehearse whole visit | `FullTest` | Short preflight, baseline, practice, and task visit |
| Record participant | `FullRun` | Full Visit 1 profile unless logged overrides are supplied |
| Validate data | `ValidateRun` or `study1-validate` | Independent quick or comprehensive read-only report |

`FullRun` is the guarded production launcher for the implemented complete Visit
1 profile. The generic `study1` command also models Visit 2, but live Visit 2
PsychoPy acquisition remains gated at the physical auditory cue extension; see
the intentional live gates below. Do not interpret a software-only Visit 2
rehearsal as permission to collect that unimplemented cue phase.

## Install the acquisition computer

The supported hardware path requires 64-bit Windows x64, 64-bit CPython 3.10,
Neuracle Collect and its amplifier driver, and a Windows LabRecorder build with
remote-control support. Run the supplied scripts in Windows PowerShell, not WSL
or Git Bash. From the repository root:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
$EegleData = Join-Path $env:LOCALAPPDATA "EEGle\data"

.\scripts\windows\neuracle64\00-Setup.ps1 `
  -LabRecorderPath "C:\Tools\LabRecorder\LabRecorder.exe" `
  -DataRoot $EegleData `
  -AddCommandsToUserPath
```

The process-scoped execution policy lasts only for that PowerShell window. Use
the institution-approved policy when local policy does not permit this change.
`00-Setup.ps1` creates `.venv`, installs `.[runtime,analysis]`, runs dependency
and setup checks, and generates a machine-local display configuration. It does
not confirm the physical cap mapping; that is a later, explicit step.

The setup switches are:

| Parameter | Required? | Use |
| --- | --- | --- |
| `-LabRecorderPath <exe>` | Recommended | Explicit executable when LabRecorder is not discoverable on `PATH` |
| `-DataRoot <path>` | Optional | Exact acquisition root; defaults to `%LOCALAPPDATA%\EEGle\data` |
| `-AddCommandsToUserPath` | Optional | Add `.venv\Scripts` and the guarded launcher directory to the current and future user terminals |

If `-AddCommandsToUserPath` is omitted, keep using repository-relative script
paths. Activation is optional because every supplied launcher uses the virtual
environment directly:

```powershell
.\.venv\Scripts\Activate.ps1
eegle --help
study1 --help
study1-validate --help
```

For development or software-only rehearsal on macOS/Linux, install the same
package extras with Python 3.10:

```bash
python3.10 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[runtime,analysis]"
study1 --help
study1-validate --help
```

Do not use the macOS constraints snapshot on Windows. Production Neuracle,
LabRecorder, display, and permission qualification must still be completed on
the actual Windows acquisition computer.

## Qualify storage, task, EEG, and XDF

Use the same `$EegleData` value throughout setup, testing, acquisition, resume,
and participant-based validation. A different root can split a visit's raw data
from its manifest and recovery state.

### 1. Storage and display

Before applying the cap:

```powershell
.\scripts\windows\neuracle64\06-Test-StorageAccess.ps1 -DataRoot $EegleData

.\scripts\windows\neuracle64\01-DryRun-Task.ps1 `
  -Trials 10 `
  -Participant "display-check-001" `
  -ScreenIndex 0 `
  -DataRoot $EegleData
```

The storage probe exercises the fresh child-process boundary used between
phases. The dry task must open on the intended monitor, accept SPACE, allow
ESCAPE/Q to abort, and finish without starting LabRecorder or requiring an EEG
stream.

Useful dry-task options are `-Trials 10..100`, `-Participant <test-id>`,
`-ScreenIndex <zero-based-index>`, and `-DataRoot <path>`. Fullscreen is the
default. `-Windowed` is a display diagnostic only; it cannot certify the strict
VBlank timing contract. `-FullScreen` is retained for explicit/backward-
compatible invocation, and it cannot be combined with `-Windowed`.

### 2. Collect discovery and cap confirmation

In Collect, connect the amplifier, select 1000 Hz, inspect contact/impedance,
and enable one unique 65-value LSL EEG outlet. Keep it publishing continuously.
Close any LabRecorder that was opened manually because EEGle owns its process
and loopback control port.

Discover first without generating a live configuration:

```powershell
.\scripts\windows\neuracle64\02-Test-NeuracleLsl.ps1 `
  -DiscoverOnly `
  -DataRoot $EegleData
```

If the default matcher is ambiguous, supply a unique fragment of the outlet's
name or source ID with `-LslNamePattern "fragment"`. If no external stream is
visible, run:

```powershell
.\scripts\windows\neuracle64\05-Diagnose-Lsl.ps1 -DataRoot $EegleData
```

After physically verifying the 65-value positional order, reference, ground,
and auxiliary allocation, generate the live configs and run the real preflight:

```powershell
.\scripts\windows\neuracle64\02-Test-NeuracleLsl.ps1 `
  -ConfirmCapContract `
  -ConfirmElectrodes `
  -LabRecorderPath "C:\Tools\LabRecorder\LabRecorder.exe" `
  -Reference "CPz" `
  -Ground "AFz" `
  -EogAllocation "ECG, HEOR, HEOL, VEOU, VEOL" `
  -DataRoot $EegleData
```

`-ConfirmElectrodes` records that the operator inspected the electrodes in
Collect; it is not a numeric impedance reading. The preflight reports signal,
timestamp, channel, electrode, storage, marker, and recorder findings. Quality
findings remain precise warnings that the operator may accept or decline.
Unavailable required streams, invalid channel identity/order, unwritable
storage, LabRecorder startup/process failure, or a missing/empty/unreadable
authoritative XDF remain acquisition failures.

The discovery/preflight launcher also accepts `-Participant <test-id>`,
`-LslNamePattern <unique-fragment>`, and hardware values different from the
defaults through `-Reference`, `-Ground`, and `-EogAllocation`. Use those values
only when they describe the physically verified installation.

### 3. Short authoritative recording

Keep Collect streaming and do not start LabRecorder yourself:

```powershell
.\scripts\windows\neuracle64\03-Run-EEGTaskTest.ps1 `
  -Trials 20 `
  -Participant "neuracle-task-test-001" `
  -ScreenIndex 0 `
  -ConfirmElectrodes `
  -DataRoot $EegleData
```

This launcher accepts `-Trials 10..20`, `-Participant`, `-DataRoot`,
`-ScreenIndex`, optional `-AudioOutputDevice`, and the mutually exclusive
`-FullScreen`/`-Windowed` display switches. It repeats the complete physical
preflight, then records a short task to XDF.

### 4. Whole-visit rehearsal

Use a non-participant test identity:

```powershell
$TestId = "systemtest-$(Get-Date -Format 'yyyyMMdd-HHmmss')"

FullTest `
  -Participant $TestId `
  -VisitId "$TestId-visit1" `
  -NoGoDigit 3 `
  -Operator "operator-initials" `
  -ScreenIndex 0 `
  -BaselineSeconds 60 `
  -ConfirmElectrodes `
  -DataRoot $EegleData
```

The default `FullTest` profile uses 30 experimental trials, 30-trial practice
rounds with four no-go trials, up to three initially materialized rounds, and
60 seconds for each baseline condition. It uses the same phase transitions,
managed XDF recorder, marker receipts, task ledgers, warnings, and resume logic
as the full run.

## Run a participant

Before launch, confirm that Collect is publishing the verified outlet, the
participant has been allocated exactly one no-go digit, the correct data root
is available, the acquisition display is using the intended mode, and no
manually started LabRecorder owns port 22345.

The normal complete Visit 1 command is:

```powershell
$ParticipantId = "sub-001"
$VisitId = "$ParticipantId-full-visit1"

FullRun `
  -Participant $ParticipantId `
  -VisitId $VisitId `
  -NoGoDigit 3 `
  -Operator "operator-initials" `
  -ScreenIndex 0 `
  -ConfirmElectrodes `
  -DataRoot $EegleData
```

For a new acquisition, `-Participant`, `-NoGoDigit`, `-Operator`, and
`-ConfirmElectrodes` are required. `-VisitId` is optional but recommended for
an operator-readable visit identity. Participant values such as names, spaces,
Unicode, and ordinary Windows-valid punctuation are preserved exactly in the
manifests. Only values that would be unsafe as a directory component are given
a stable encoded storage component; participant lookup and resume use the exact
manifest identity.

The normal `FullRun` defaults are 120 seconds for each baseline condition,
criterion-gated practice, and 1,000 experimental trials in four 250-trial
sections. The important operational alternatives are:

```powershell
$RunArgs = @{
  Participant = $ParticipantId
  VisitId = $VisitId
  NoGoDigit = 3
  Operator = "operator-initials"
  ConfirmElectrodes = $true
  DataRoot = $EegleData
}

# Choose one of these launch forms for a new visit; do not run them in sequence.

# Shorten both eyes-open and eyes-closed baselines.
FullRun @RunArgs -BaselineSeconds 30

# Deliberately omit the baseline child session.
FullRun @RunArgs -SkipBaseline

# Deliberately omit practice.
FullRun @RunArgs -SkipPractice

# Use a shortened deterministic task and custom practice plan.
FullRun @RunArgs -Trials 100 -PracticeTrials 12 -PracticeNoGoTrials 1 -PracticeMaxRounds 1

# Choose another monitor and an exact optional speaker endpoint.
FullRun @RunArgs -ScreenIndex 1 -AudioOutputDevice "Speakers (Realtek(R) Audio)"
```

All overrides are recorded. A shortened task has its own prepared sequence
identity and must not be treated as the default 1,000-trial profile.

### FullTest and FullRun parameter reference

Both launchers expose the same operator-facing parameter names. Their defaults
differ only where shown:

| Parameter | FullTest default | FullRun default | Meaning |
| --- | --- | --- | --- |
| `-Participant <text>` | Required for new run | Required for new run | Exact participant identity stored in manifests |
| `-NoGoDigit <0..9>` | Required for new run | Required for new run | Counterbalanced no-go assignment |
| `-Operator <text>` | Required for new run | Required for new run | Operator identity |
| `-ConfirmElectrodes` | Required for new run | Required for new run | Attest that Collect electrode/contact inspection was performed |
| `-DataRoot <path>` | Configured/default root | Configured/default root | Root used for every visit and child session artifact |
| `-VisitId <text>` | Generated if omitted | Generated if omitted | Explicit visit identity; also usable for exact resume selection |
| `-Trials <n>` | `30` | `1000` | Experimental trial count (`10..100000`) |
| `-BaselineSeconds <seconds>` | `60` | `120` | Duration of each eyes-open and eyes-closed condition |
| `-SkipBaseline` | Off | Off | Create no baseline child session |
| `-PracticeTrials <n>` | `30` | `30` | Trials in each planned practice round |
| `-PracticeNoGoTrials <n>` | `4` | `4` | No-go trials per practice round; must be less than practice trials |
| `-PracticeMaxRounds <n>` | `3` | `3` | Rounds materialized initially; participant-requested retries may extend it |
| `-SkipPractice` | Off | Off | Proceed without practice |
| `-ScreenIndex <0..16>` | `0` | `0` | Zero-based PsychoPy monitor |
| `-AudioOutputDevice <name>` | Automatic | Automatic | Prefer the exact PsychoPy speaker name; unavailability is warning-only |
| `-FullScreen` | Default behavior | Default behavior | Explicitly request acquisition-capable fullscreen |
| `-Windowed` | Off | Off | Diagnostic-only window; mutually exclusive with `-FullScreen` |
| `-Resume` | Off | Off | Resume a selected existing incomplete visit |
| `-ResumeTarget <id-or-path>` | Empty | Empty | Visit ID, child run/session name, visit directory, child directory, or manifest path |

PowerShell wrapper parameters use one leading dash. Python `study1` options use
two leading dashes; do not mix the two forms.

## Resume and retry safely

`FullTest` and `FullRun` automatically request `--retry-incomplete` for a normal
new-run invocation. Repeating the same full command after correcting a failure
continues that participant's incomplete Visit 1, preserves completed phases,
and records a failed/interrupted phase into a new child directory. It never
appends to or overwrites an earlier XDF.

Use `-Resume` when deliberately selecting an existing visit. The smallest forms
are:

```powershell
# Newest incomplete Visit 1 matching the exact participant identity.
FullRun -Participant "sub-001" -Resume -DataRoot $EegleData

# Visit ID, child run/session name, or an exact directory/manifest path.
FullRun -Resume -ResumeTarget "visit-1-20260825T120000" -DataRoot $EegleData
FullRun -Resume -ResumeTarget "D:\EEGleData\study1\...\visit_manifest.json" -DataRoot $EegleData
```

Resume reloads participant allocation, operator identity, protocol/task shape,
prepared sequence, and completed-phase state from the manifests. Do not add
new task-shaping flags in an attempt to change an existing visit. A completed
baseline stays complete; a visit initially created with `-SkipBaseline` stays
baseline-free. If a recording completed but its warning decision was deferred,
resume presents the warnings again instead of reacquiring the phase.

## Validate after the run

Validation is separate from acquisition. It does not change the XDF, ledgers,
quality decisions, or Study 1 completion status. Run quick validation after
each completed participant visit:

```powershell
ValidateRun -Participant $ParticipantId -Visit 1 -DataRoot $EegleData -Mode Quick
```

Run comprehensive validation before analysis/export and whenever investigating
warnings:

```powershell
ValidateRun -Participant $ParticipantId -Visit 1 -DataRoot $EegleData -Mode Comprehensive
```

To validate a retained incomplete visit or exact baseline/task child session,
use `-RunRoot` rather than `-Participant`:

```powershell
ValidateRun -RunRoot "D:\EEGleData\study1\...\visit_manifest.json" -Mode Comprehensive
```

To compare a separately maintained mirror without writing to it:

```powershell
ValidateRun -RunRoot "D:\EEGleData\study1\...\visit_manifest.json" `
  -Mode Comprehensive `
  -BackupRoot "E:\EEGleBackup"
```

Exactly one of `-Participant` or `-RunRoot` is required. `-Visit 1|2` narrows
participant lookup, `-DataRoot` supplies that lookup root, `-Mode` is `Quick`
or `Comprehensive`, and `-BackupRoot` is valid only in comprehensive mode.
Participant lookup selects the newest completed visit whose manifest contains
the exact participant identity. Exact targets may be incomplete.

The installed cross-platform equivalents are:

```bash
study1-validate --participant "Participant Name" --visit 1 --session-root /path/to/data --mode quick
study1-validate --target /path/to/visit-or-run --mode comprehensive
study1-validate --target /path/to/visit-or-run --mode comprehensive --backup-root /path/to/mirror
```

The direct validator parameters are:

| Parameter | Meaning |
| --- | --- |
| `--participant <text>` | Select the newest completed visit with this exact manifest identity |
| `--target <path>` | Select an exact visit/session/run directory or `visit_manifest.json`, including retained incomplete data |
| `--visit 1\|2` | Optionally narrow participant lookup |
| `--session-root <path>` | DataRoot used for participant lookup |
| `--mode quick\|comprehensive` | Quick is the default |
| `--backup-root <path>` | Read-only DataRoot mirror comparison; comprehensive mode only |
| `--config <json>` | Optional config override used for defaults and validation contracts |

Exactly one of `--participant` or `--target` is required.

Quick mode resolves every phase/session, checks canonical artifacts for
existence/readability/nonzero size, reads XDF headers, strictly parses canonical
JSON/JSONL/CSV ledgers, and checks declared identities, counts, sequences, and
basic key-event references. Comprehensive mode also scans all XDF samples and
clock data with bounded memory; reconciles XDF markers, independent receipts,
baseline/task/trial/keypress/stimulus ledgers; recomputes response assignment,
correctness, and RT; reports supportable channel/timing intervals and trials;
and writes a SHA-256 canonical inventory. Backup comparison is read-only.

Every invocation atomically creates a timestamped report below the visit's
`reports\post_run_validation` directory. Interpret validator exit codes as:

| Exit code | Meaning |
| --- | --- |
| `0` | Validation completed; inspect the report because scientific, integrity, or backup warnings may still exist |
| `2` | Target not found or a canonical core artifact is missing/unreadable |
| `1` | Internal validator error |

Cross-ledger mismatches, timestamp defects, signal artifacts, and backup
differences are reported as warnings and do not themselves produce a nonzero
exit. Missing signal cannot be recreated; clock correction may address
offset/drift, while isolated nonmonotonic samples can sometimes be dropped or
reordered during documented post-processing. The report describes the affected
intervals and likely recoverability without making the exclusion decision.

## Required Study 1 artifacts

XDF is the authoritative raw EEG and LSL container. Study 1 deliberately does
not create `raw/eeg.csv`. JSONL task, keypress, event, and stimulus-manifest
files are canonical behavioral/timing artifacts; any CSV task files are
convenience mirrors and never replace or rewrite XDF.

A completed recorded child phase must retain at least:

```text
raw/recording.xdf
raw/xdf_metadata.json
raw/lsl_markers_received.csv
logs/processes/recorder.status.json
reports/recording_quality_warnings.json
```

Task phases also retain canonical trial, keypress, event, and stimulus-manifest
ledgers. The visit manifest links each phase to its child session. Use the
validator to resolve and reconcile these files; do not infer completeness from
the presence of a single XDF or from the PsychoPy window closing.

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
  the display when `hardware.display.check_refresh_rate` is enabled, and
  verifies the asynchronous PTB keyboard queue. Accepted rates, tolerance, and
  measurement settings come from `configs/study1_neuracle64.json`. A mismatch
  is recorded but is non-blocking by default; set
  `require_refresh_rate_match: true` only when an operator wants a strict gate.
  Frame counts are rounded to the closest count for the configured nominal
  rate when a duration is not exactly representable.
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
FullRun -Participant "sub-001" -Resume

# A visit ID or child run/session directory name under the selected data root.
FullRun -Resume -ResumeTarget "visit-1-20260825T120000"
FullRun -Resume -ResumeTarget "run-20260825T121500"

# An exact visit directory, visit_manifest.json, or retained child session path.
FullRun -Resume -ResumeTarget "D:\EEGleData\study1\sub-001\visits\visit-1\visit-id\visit_manifest.json"
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

### Direct `study1` option reference

Use `FullTest` and `FullRun` for guarded Windows Visit 1 acquisition. Use the
installed `study1` command (or `python -m eegle.pipelines.study1`) for software
rehearsal, physical preflight automation, or explicitly controlled development.
The direct options are grouped below; `study1 --help` is the executable source
of truth.

| Options | Use and constraints |
| --- | --- |
| `--config <json>` | Recipe/config path; the checked-in candidate is not a confirmed live cap config |
| `--participant <text>` | Required nonempty exact identity |
| `--visit 1\|2` | Required visit number; Visit 2 requires an existing completed Visit 1 participant manifest |
| `--visit-id <text>` | Optional explicit visit identity |
| `--operator <text>` | Operator identity; required by the guarded live launcher |
| `--master-seed <int>` | Deterministic participant/session seed; do not change it between visits/resume |
| `--no-go-digit 0..9` | Required when creating a real EEG participant; dry/synthetic rehearsals can allocate deterministically |
| `--task-mode psychopy\|dry-run` | Real display task or software-only task execution |
| `--smoke` | Shorten every visit phase; cannot be combined with `--full-1000` |
| `--full-1000` | Select the separate complete Visit 1 profile; Visit 1 only |
| `--include-practice` / `--skip-practice` | Mutually exclusive practice choice |
| `--trials <n>` | Override each experimental segment; minimum 10 |
| `--practice-trials <n>` | Planned practice length; minimum 10 |
| `--practice-no-go-trials <n>` | At least 1 and less than practice trials |
| `--practice-max-rounds <n>` | Initially materialized deterministic rounds; minimum 1 |
| `--baseline-seconds <seconds>` / `--skip-baseline` | Mutually exclusive per-condition duration or deliberate omission |
| `--window-size <width> <height>` | Positive diagnostic window dimensions |
| `--screen-index <n>` | Nonnegative zero-based monitor index |
| `--audio-output-device <name>` | Exact preferred PsychoPy speaker; unavailable audio remains warning-only |
| `--fullscreen` / `--windowed` | Mutually exclusive; windowed mode is diagnostic, not acquisition timing qualification |
| `--skip-eeg` | Do not start an EEG recorder; used for software rehearsal |
| `--allow-missing-eeg` | Permit missing EEG only when not recording it; Study 1 cannot record while allowing the required stream to be absent |
| `--simulate-eeg` | Development-only synthetic 65-value LSL outlet; requires dry-run and real recorder, and conflicts with skip/allow-missing EEG |
| `--labrecorder-executable <path>` | Per-invocation executable override |
| `--preflight-only` | Physical EEG/LSL/channel/electrode/LabRecorder/XDF gate without creating a visit; conflicts with resume/retry/simulation |
| `--confirm-electrodes` | Operator attestation after inspecting Collect |
| `--electrode-quality-file <json>` | Optional external structured electrode/contact report; unrecognized status values are reported as warnings |
| `--electrode-note <text>` | Operator note stored with electrode-quality evidence |
| `--resume` | Resume an existing visit without appending to an existing XDF |
| `--resume-target <id-or-path>` | Visit ID, child run/session name, directory, or visit manifest; requires resume |
| `--retry-incomplete` | Automatically continue the participant's incomplete visit; mutually exclusive with resume |
| `--session-root <path>` | Exact DataRoot; `--output-root` is a compatibility alias |
| `--result-file <path>` | Atomic final launcher handshake; not a substitute for the visit/session artifacts |
| `--lsl-wait <seconds>` | Stream-discovery wait duration |
| `--allow-visit-interval-override` | Logged pilot-only exception to the configured 2-7 day Visit 2 interval |

For a direct live Visit 1, use the generated confirmed live config created by
`02-Test-NeuracleLsl.ps1`, not the checked-in candidate config:

```powershell
study1 --config .runtime\windows-neuracle64\study1-live.json `
  --participant "sub-001" --visit 1 --no-go-digit 3 --operator "operator-id" `
  --include-practice --confirm-electrodes --fullscreen --session-root $EegleData
```

That direct command uses the standard proposal profile, not `FullRun`'s
separately versioned complete 1,000-trial profile. Prefer `FullRun` when the
complete profile is intended.

## Participant allocation

The first live visit requires an explicitly assigned counterbalanced digit. In
a direct command, the identity/allocation portion is:

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

After each recorder stops, the phase writes
`reports/recording_quality_warnings.json`. This atomic sidecar contains stable,
structured warning codes plus exact channels, trials, counts, timestamp-gap
intervals, recoverability, and suggested review actions where the evidence
supports them. It does not emit new LSL markers, change the completion decision,
or turn signal/electrode/timing quality findings into automatic failures. Task,
keypress, event, and stimulus-manifest ledgers receive best-effort durable flushes
at block and final boundaries. A Windows `fsync` denial is recorded as
`io.durability_sync_failed` and remains non-blocking. XDF remains the authoritative
raw EEG/LSL artifact; CSV task files are convenience mirrors only.

## Read-only post-run validation

Use the separate validator after acquisition. Participant lookup matches the
exact `participant_id` stored in visit manifests and selects the newest completed
visit; an exact target may also select an incomplete retained visit or one child
session:

```bash
study1-validate --participant "Participant Name" --session-root /path/to/data --mode quick
study1-validate --participant "Participant Name" --visit 1 --session-root /path/to/data
study1-validate --target /path/to/visit-or-run --mode comprehensive
study1-validate --target /path/to/visit-or-run --mode comprehensive --backup-root /path/to/mirror
```

Quick mode checks canonical file readability, XDF headers, strict JSON/JSONL/CSV
parsing, declared counts, identities, sequence consistency, and key-event
references. Comprehensive mode additionally scans XDF samples and clocks,
reconciles marker/baseline/task/key ledgers, recomputes scoring and RT, hashes the
canonical inventory, and optionally compares a read-only backup mirror. Every
invocation writes a unique atomic JSON report beneath
`reports/post_run_validation/`. It never rewrites raw files or Study 1 completion
manifests.

A very large absolute EEG/marker boundary separation can mean that PyXDF placed
the device and PC streams on incompatible clock origins; it is not, by itself,
evidence that hours of EEG are missing. EEGle checks marker parity and the
authoritative XDF sample timestamps, then uses the non-writing heartbeat's
source/local endpoint anchors to derive an affine mapping. Gaps seen only by
that diagnostic heartbeat are retained as warnings and do not veto the mapping;
a nonmonotonic heartbeat clock, missing endpoints, stream-identity mismatch, or
real XDF coverage shortfall still prevents normalization.

For an older retained session whose `raw/xdf_metadata.json` records the former
clock-bridge failure, epoch extraction automatically reruns XDF integrity
validation and refreshes that derived sidecar before loading samples:

```bash
eegle extract-epochs --session-dir /path/to/retained/run --source stimulus_manifest
```

This writes derived validation/epoch artifacts only. It does not alter
`raw/recording.xdf` or mark an incomplete Study 1 visit complete.

Exit code `0` means validation completed, even when scientific or integrity
warnings were reported. Exit code `2` means the target or a canonical core
artifact was missing/unreadable; exit code `1` is an internal validator error.
On Windows, `ValidateRun` exposes `-Participant`, optional `-Visit`, `-RunRoot`,
`-DataRoot`, `-Mode`, and `-BackupRoot` equivalents.

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
