# Neuracle W64 Windows Test Runbook

This is the operator path for testing EEGle on a Windows x64 Dell with the
Neuracle 64-channel wet system. Neuracle Collect owns the amplifier connection
and publishes EEG to Lab Streaming Layer (LSL). EEGle owns the task, marker
stream, managed LabRecorder process, XDF file, and CSV safety mirror.

The eight operator PowerShell scripts are under
`scripts\windows\neuracle64`:

| Script | Purpose | EEG recorded? |
| --- | --- | --- |
| `00-Setup.ps1` | Create/check the Python 3.10 environment | No |
| `01-DryRun-Task.ps1` | Show a 10-trial Dynamic SART task | No |
| `02-Test-NeuracleLsl.ps1` | Discover Collect, lock the confirmed cap contract, and run physical preflight | No recording session |
| `03-Run-EEGTaskTest.ps1` | Full preflight followed by a 10-20 trial recorded task | Yes: authoritative XDF |
| `04-Run-FullShortTest.ps1` | Preflight, resting controls, practice, and 30 experimental trials | Yes: authoritative XDF |
| `05-Diagnose-Lsl.ps1` | Separate local pylsl/config failures from a missing external outlet | No |
| `06-Test-StorageAccess.ps1` | Reproduce parent/child recording permission operations without EEG | No |
| `07-Run-Full.ps1` | Complete two-minute baselines, practice gate, and 1,000-trial task | Yes: authoritative XDF |

Run every command below from a 64-bit Windows PowerShell terminal. Do not use
Git Bash or WSL for the hardware run.

## Storage permission gate

Use one explicit data root for the whole visit. Do not automatically fall back
from one folder to another after a visit starts: that can divide raw EEG,
markers, manifests, and recovery state across different locations.

Before applying the cap, run the storage transition probe against the exact
folder you plan to use:

```powershell
$EegleData = Join-Path $env:LOCALAPPDATA "EEGle\data"

.\scripts\windows\neuracle64\06-Test-StorageAccess.ps1 `
  -DataRoot $EegleData
```

The probe performs nested directory creation, append, flush, atomic replacement,
rename, read-back, and deletion. It also launches the same fresh child Python
process boundary used when the resting baseline transitions to the visual task.
It creates and removes only a disposable `.eegle_recording_storage_probe`
directory. The full short and complete runs perform this gate again and stop
before LabRecorder or the baseline starts if it fails.

On a BeyondTrust-managed computer, elevating PowerShell alone does not prove
that the task worker will receive the same policy. BeyondTrust evaluates child
processes, and its application definition has an explicit **Allow child
processes will match this application definition** option. Run the storage
probe once from a normal terminal and once from a PowerShell terminal launched
with the organization's approved BeyondTrust action. If only the latter passes,
give both probe outputs to IT. Ask them to inspect the rules for:

- the PowerShell executable used to start the script;
- `.venv\Scripts\python.exe`, including the child Python process;
- the configured `LabRecorder.exe`;
- the chosen data directory.

Prefer an IT-provisioned data directory with Modify permission for your normal
user account over granting administrator privileges to Python. An elevated
Python interpreter can execute arbitrary code and is a much broader permission
than EEGle needs. A typical dedicated path is `C:\EEGleData`, but IT must create
and approve it; the root of `C:\` is not a fallback data directory.

Test an IT-provisioned directory before using it:

```powershell
$EegleData = "C:\EEGleData"

.\scripts\windows\neuracle64\06-Test-StorageAccess.ps1 `
  -DataRoot $EegleData
```

If both the parent and child tests fail, inspect the directory ACL and Windows
Security **Protection history**. Microsoft Defender Controlled Folder Access
can block an executable even when NTFS permissions look writable, and enterprise
policy can add more protected locations. An administrator should allow only the
required executable paths or approve a dedicated unprotected recording folder.
Do not disable ransomware protection for a study run.

For a persistent unexplained denial, IT can capture a short Process Monitor
trace filtered to `python.exe`, `LabRecorder.exe`, and result `ACCESS DENIED`.
That identifies the exact process, operation, and path instead of guessing from
the final Python exception.

## 1. One-time Windows preparation

Install these items before test day:

- 64-bit CPython 3.10, with the `py` launcher available.
- Neuracle Collect and the amplifier driver supplied for the system.
- A Windows x64 LabRecorder build that supports its remote-control socket.
- This EEGle repository in a normal local folder.

Open PowerShell, move to the repository, and allow these local scripts for this
terminal if the current policy requires it:

```powershell
Set-Location "C:\path\to\closedloop"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

The process-scoped policy disappears when that terminal closes. If an
organization controls script execution, use its approved policy instead of
changing machine policy.

Choose an approved data folder outside the repository. The default is
`%LOCALAPPDATA%\EEGle\data`:

```powershell
$EegleData = Join-Path $env:LOCALAPPDATA "EEGle\data"
```

Run setup. Substitute the installed LabRecorder path:

```powershell
.\scripts\windows\neuracle64\00-Setup.ps1 `
  -LabRecorderPath "C:\Tools\LabRecorder\LabRecorder.exe" `
  -DataRoot $EegleData
```

Setup creates `.venv`, installs `.[runtime,analysis]`, checks dependency
consistency, verifies `pylsl`, `pyxdf`, and PsychoPy imports, and creates the
display-only local config. It does not create a live cap config or weaken the
checked-in hardware safety gate.

### Put EEGle commands in the terminal

For the current terminal, activate the environment:

```powershell
.\.venv\Scripts\Activate.ps1
eegle --help
study1 --help
```

Activation is optional because the supplied scripts call the environment's
Python executable directly. To add `eegle.exe` and `study1.exe` to the user
`PATH` for future terminals, explicitly request it during setup:

```powershell
.\scripts\windows\neuracle64\00-Setup.ps1 `
  -LabRecorderPath "C:\Tools\LabRecorder\LabRecorder.exe" `
  -DataRoot $EegleData `
  -AddCommandsToUserPath
```

Close and reopen PowerShell after changing the user `PATH`. The operator `.ps1`
scripts still run by repository-relative path, from the repository root.

## 2. Display-only dry run: 10 trials

Collect, the amplifier, and LabRecorder are not needed. The script disables the
EEG recorder and practice so the window contains ten experimental trials only:

```powershell
.\scripts\windows\neuracle64\01-DryRun-Task.ps1 `
  -Trials 10 `
  -Participant "display-check-001" `
  -DataRoot $EegleData
```

Confirm that:

- the task window opens on the intended monitor;
- digits and instructions are legible;
- SPACE records responses;
- ESCAPE or Q aborts cleanly;
- the task exits without asking for an EEG stream or starting LabRecorder.

Use `-Trials 10` for the intended smoke test. The script accepts larger display
checks only when deliberately requested.

## 3. Configure Collect and inspect LSL

Before starting Collect streaming:

1. Connect the amplifier and verify all intended physical electrodes in
   Collect.
2. Configure the W64 acquisition at 1000 Hz.
3. Enable an LSL EEG outlet with stream type `EEG` and a stable, unique name or
   source ID. A name containing `Neuracle` matches the default profile.
4. Keep the outlet publishing continuously during every EEGle check and task.
5. Allow Neuracle Collect and Python on the Windows Private network in Windows
   Firewall if LSL discovery is blocked.
6. Close any manually opened LabRecorder. EEGle must own its control port and
   launch it itself.

First perform discovery only:

```powershell
.\scripts\windows\neuracle64\02-Test-NeuracleLsl.ps1 `
  -DiscoverOnly `
  -DataRoot $EegleData
```

The result must show one unique EEG stream, 65 values, nominal 1000 Hz, samples
received, and the raw channel labels supplied by Collect. If the outlet has a
different name, use a unique fragment from its name or source ID:

```powershell
.\scripts\windows\neuracle64\02-Test-NeuracleLsl.ps1 `
  -DiscoverOnly `
  -LslNamePattern "YourCollectStreamName" `
  -DataRoot $EegleData
```

Discovery reports are retained below
`$EegleData\system_checks\collect-discovery`.

### Mandatory cap-contract confirmation

The operator-confirmed W64 positional order is 59 scalp values followed by five
physical auxiliary inputs and one reserved transport value:

```text
60 ECG
61 HEOR
62 HEOL
63 VEOU
64 VEOL
65 TRIGGER_STATUS (observed empty without a hardware trigger)
```

The dedicated reference is CPz and ground is AFz. Collect exposes generic LSL
labels (`ch_001` through `ch_065`), so EEGle maps them positionally to the
confirmed order. Value 65 is preserved in raw XDF/CSV as `TRIGGER_STATUS` but is
excluded from electrode quality and derived EEG analysis. ECG/EOG values are
also preserved raw and excluded from default scalp-EEG analysis.

Only after that physical comparison, generate the local live configs and run
the full preflight:

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

Include the same `-LslNamePattern` used during discovery when required. The
script writes machine-local live configs under
`.runtime\windows-neuracle64`; it does not modify the checked-in candidate
config.

The physical preflight checks:

- exactly one matching Collect EEG stream;
- live sample delivery, effective source-timestamp rate, and retained sample fraction;
- 65 values in the confirmed transport order (64 physical inputs plus value 65);
- a nominal/observed rate compatible with 1000 Hz;
- non-finite samples, flat/failed channels, constant runs, possible clipping,
  and line-noise warnings;
- a local EEGle marker-stream loopback;
- PsychoPy availability;
- writable storage and free space;
- LabRecorder executable, `pyxdf`, and loopback control-port availability;
- a finalized three-second LabRecorder XDF probe with start/end markers and the
  full stored-signal integrity scan;
- channel signal/contact summary plus the operator's explicit confirmation for
  the 64 physical inputs.

`-ConfirmElectrodes` means the operator has already inspected cap contact or
impedance in Collect. It is an attestation, not an impedance measurement read
from Collect. If available, a structured quality file can instead be passed to
the underlying `study1 --preflight-only` command with
`--electrode-quality-file`.

If the live inlet or XDF probe finds nonfatal sample-rate, sample-retention,
timestamp, or signal-quality concerns, preflight prints a numbered warning
list. A live run proceeds only after the operator types `YES`;
`-ConfirmElectrodes` does not bypass warnings.

## 4. Short EEG task test: 10-20 trials

Keep Collect streaming. Do not start LabRecorder manually. The script repeats
the full physical preflight, then records the task to authoritative XDF while a
non-writing LSL heartbeat reports sample progress:

```powershell
.\scripts\windows\neuracle64\03-Run-EEGTaskTest.ps1 `
  -Trials 20 `
  -Participant "neuracle-task-test-001" `
  -ConfirmElectrodes `
  -DataRoot $EegleData
```

Use `-Trials 10` for the shortest permitted version. This task-test config has
practice disabled.

After successful completion, locate the printed session directory and verify:

```text
raw\recording.xdf
raw\xdf_metadata.json
raw\lsl_markers_received.csv
events\events.jsonl
events\dynamic_sart_trials.jsonl
process_logs\manager_summary.json
```

The XDF is the only full raw EEG recording. A task run is not successful merely
because the PsychoPy window closed; the terminal reports while XDF or other
processing continues, and the final command returns only after graceful
finalization completes.

## 5. Complete short test

Use a new non-participant test ID. Assign a valid counterbalanced no-go digit
for the test; the example uses 3. This command performs:

1. full preflight;
2. 60 seconds eyes open;
3. 60 seconds eyes closed;
4. Dynamic SART practice;
5. three 10-trial blocks, for 30 experimental trials total.

At launch, the script rebuilds the generated live runtime config from the
current checked-in Study 1 protocol. It retains the previously confirmed cap
mapping, LSL name patterns, reference, ground, EOG allocation, and LabRecorder
path, but replaces stale task/display parameters. The rebuilt config is
validated before preflight or recording. A protocol/configuration failure or a
failed hardware preflight prevents the baseline recorder and task from starting.
The accepted preflight report fingerprints the hardware, display, marker, and
recorder settings; baseline and task startup reject any change to that contract.
The baseline's post-recording XDF validation is not a second preflight.

The short test uses the same timing contract as a genuine Study 1 run: a 250 ms
digit, 1350 ms post-digit fixation, fixed 1600 ms SOI, no intentional jitter,
VBlank waiting, and a required measured refresh-rate match.

The refresh probe leaves the new window available for two seconds before
sampling. Move it to the intended monitor during that interval. EEGle identifies
the active monitor from the largest window/monitor overlap, reads that monitor's
current video mode, and measures lightweight blank flips without PsychoPy's
costly frame-rate splash. If the first result is bad or the window moves during
sampling, it discards that attempt and makes up to three measurements.

```powershell
$TestId = "systemtest-$(Get-Date -Format 'yyyyMMdd-HHmmss')"

.\scripts\windows\neuracle64\04-Run-FullShortTest.ps1 `
  -Participant $TestId `
  -VisitId "$TestId-visit1" `
  -NoGoDigit 3 `
  -Operator "operator-initials" `
  -ScreenIndex 0 `
  -BaselineSeconds 60 `
  -ConfirmElectrodes `
  -DataRoot $EegleData
```

`-BaselineSeconds 60` applies to each resting condition, so the controls take
two minutes in total. If one minute total is required, pass
`-BaselineSeconds 30`.

The current implemented order is eyes open followed by eyes closed. Both phases
show instructions and emit LSL boundary markers. Keep the participant still and
do not stop Collect between the baseline and task.

### Retry or resume an interrupted complete short test

For a failed or interrupted test, first correct the reported cause and rerun the
same `04-Run-FullShortTest.ps1` command. The launcher now selects
`--retry-incomplete` by default: it detects the participant's incomplete Visit
1, reuses the original visit ID, skips completed phases, and reruns the failed
phase into a new session directory. You do not need to find or rename the old
folder, and retained XDF/CSV data are never overwritten.

`-Resume` remains available when deliberately resuming with an explicit visit
identity. It uses the same participant, no-go digit, operator, baseline
duration, and visit identity:

```powershell
.\scripts\windows\neuracle64\04-Run-FullShortTest.ps1 `
  -Participant $TestId `
  -VisitId "$TestId-visit1" `
  -NoGoDigit 3 `
  -Operator "operator-initials" `
  -BaselineSeconds 60 `
  -Resume `
  -ConfirmElectrodes `
  -DataRoot $EegleData
```

Do not reuse a completed participant/Visit 1 identity for a new test. Generate a
new `$TestId` instead.

## 6. Complete 1,000-trial run

Use `07-Run-Full.ps1` only after the dry task, storage probe, LSL preflight, and
short recorded tests pass. The complete profile is a validated Visit 1
acquisition contract:

1. full hardware, display, storage, LSL, channel, and LabRecorder preflight;
2. 120 seconds eyes open;
3. 120 seconds eyes closed;
4. 30-trial criterion-gated practice rounds (up to three), followed by a
   participant-controlled ready screen;
5. 1,000 experimental trials in four equal 250-trial sections; and
6. bounded breaks after trials 250, 500, and 750. SPACE continues after 30
   seconds; the task continues automatically at 60 seconds.

Trials 1-500 (sections 1 and 2) are support and trials 501-1000 (sections 3 and
4) are held-out query. The task looks identical to the participant on both
sides of that boundary. EEGle uses support responses to freeze the behavioral
reaction-time reference after trial 500, then evaluates later query trials
without updating that reference. Do not interleave new support trials into
each section: later support would move the reference boundary and leak later
behavior into query evaluation.

Each section contains either 37 or 38 no-go trials, allocating exactly 150
no-go trials across the task: `[38, 37, 38, 37]`. At the fixed 1.6-second SOI,
the experimental trials take about 26 minutes 40 seconds before practice and
breaks. Plan for at least 33 minutes after preflight for the two baselines, one
successful practice round, and the minimum breaks, plus instructions and
participant transitions.

No-go positions use the corrected v2 weighted-stratified schedule. Every
section is independently randomized; each hidden 50-trial stratum contains
seven or eight no-go trials. Adjacent and one-go-separated no-go trials remain
possible but are down-weighted, three consecutive no-go trials are prohibited,
and the first and last four trials of each section are go trials. The schedule
must show varied inter-no-go gaps rather than a repeating 6/7-trial rhythm.

```powershell
$ParticipantId = "sub-001"
$VisitId = "$ParticipantId-full-visit1"

.\scripts\windows\neuracle64\07-Run-Full.ps1 `
  -Participant $ParticipantId `
  -VisitId $VisitId `
  -NoGoDigit 3 `
  -Operator "operator-initials" `
  -ConfirmElectrodes `
  -DataRoot $EegleData
```

The launcher selects the explicit
`full_1000_support500_query500_v2` acquisition profile by default. Its operator
controls are real PowerShell parameters rather than edits to the script:

- `-BaselineSeconds 30` sets both resting conditions to 30 seconds;
- `-SkipBaseline` creates no baseline child session and advances from preflight
  directly to the task;
- `-Trials 100` requests a deterministic shortened support/query task;
- `-PracticeTrials 12 -PracticeNoGoTrials 1 -PracticeMaxRounds 1` changes the
  criterion-gated practice shape; and
- `-SkipPractice` bypasses practice explicitly; and
- `-ScreenIndex 1` initially opens the PsychoPy windows on the second monitor.

`-ScreenIndex` is zero-based. The Windows task launchers now use fullscreen by
default because Pyglet deliberately disables OpenGL swap-interval VSync for
windowed windows while the Windows Desktop Window Manager is active. Windowed
`flip()` cadence can therefore appear stable with one display but become
irregular with two displays, and it cannot validate physical VBlank timing.
Use `-ScreenIndex N` by itself for acquisition; `-FullScreen` remains accepted
for backward-compatible commands. `-Windowed` is diagnostic-only and the strict
display gate explains why it cannot certify an acquisition run.

On failure, the terminal now lists every Pyglet screen index, bounds, resolution,
and current mode. Compare that inventory with Windows **Settings > System >
Display > Advanced display** for the selected display. This directly exposes an
index reversal or a Windows/driver mode such as 100 Hz when 60 Hz was expected.

Every override is persisted in the visit manifest. Baseline duration is an
operational visit choice and does not change the participant's scientific
protocol hash. A shortened task retains its own exact prepared sequence hash
and must not be pooled as the default 1,000-trial acquisition.

PowerShell script parameters use one leading dash (`-Resume`,
`-BaselineSeconds`), not Python/GNU spellings such as `--resume`. All operator
scripts disable positional parameter binding, so an unknown double-dash token
now fails at launch instead of becoming a directory such as `--resume`.

To resume at a completed phase boundary, repeat the participant/visit identity.
The visit manifest supplies the original task-shaping options:

```powershell
.\scripts\windows\neuracle64\07-Run-Full.ps1 `
  -Participant $ParticipantId `
  -VisitId $VisitId `
  -NoGoDigit 3 `
  -Operator "operator-initials" `
  -Resume `
  -ConfirmElectrodes `
  -DataRoot $EegleData
```

Resume skips completed baseline/task phases but never appends to or overwrites
an interrupted XDF recording. The original experimental/practice task shape is
reloaded from the visit manifest, while a completed baseline remains completed
regardless of the current baseline default. An incomplete task attempt gets a
fresh run directory using the same prepared sequence. A participant who
completed the run needs a new participant/visit identity for another test.

Without `-Resume`, rerunning the same `07-Run-Full.ps1` command automatically
retries an incomplete full visit in the same way as the short-test launcher.

## Troubleshooting gates

### No matching stream

- If the setup report says that LSL found **zero streams**, a name pattern
  cannot help yet. Keep Collect running and execute:

  ```powershell
  .\scripts\windows\neuracle64\05-Diagnose-Lsl.ps1 -DataRoot $EegleData
  ```

  This starts an outlet in a second Python process, proves whether pylsl can
  discover and read it, enumerates every external stream, and repeats the check
  with and without EEGle's explicit `LSLAPICFG`. Reports are written below
  `$EegleData\system_checks\lsl-diagnostics`.
- Open the native LabRecorder manually while Collect is running and click
  **Update**. This is an independent liblsl client. If neither LabRecorder nor
  the diagnostic sees a Collect EEG outlet, the problem is upstream of EEGle:
  live waveforms in Collect do not prove that an LSL outlet was enabled.
- In Collect, locate and explicitly start the setting described by the installed
  version as an LSL outlet, real-time data interface, data transmission, or
  secondary-development interface. Confirm that it exports raw EEG rather than
  only accepting incoming event markers. If the installed Collect build has no
  EEG LSL-output option, obtain the vendor's LSL-enabled module/bridge or SDK;
  EEGle cannot subscribe to a stream that Collect has not published.
- Confirm Collect is actively publishing, not merely connected to the
  amplifier.
- Confirm LSL type is `EEG`.
- Pass a unique `-LslNamePattern` from the stream name/source ID printed by the
  discovery script.
- Close duplicate Collect/LSL outlets.
- Check Windows Private-network firewall permission for both Collect and the
  `.venv` Python executable.
- Verify the active lab Ethernet/Wi-Fi connection is marked **Private**, and
  temporarily disconnect VPNs or disable unused Hyper-V/VMware/VirtualBox
  adapters during diagnosis. Multiple Windows adapters can route LSL discovery
  onto the wrong interface.

Interpret the diagnostic as follows:

| Result | Meaning | Next action |
| --- | --- | --- |
| Cross-process loopback fails with both configurations | Local pylsl/runtime/firewall problem | Repair the VC++ runtime or reinstall pylsl; allow `.venv\Scripts\python.exe` on Private networks |
| Loopback works, zero external streams in both runs | Local Python LSL works; Collect is not publishing a visible outlet or is isolated by its own LSL config | Use LabRecorder Update; enable Collect's EEG LSL output; inspect Collect's `lsl_api.cfg`/SessionID |
| External streams appear only without EEGle's config | Stale or incompatible EEGle `lsl_api.cfg` | Back it up/remove it and rerun `00-Setup.ps1` |
| Streams appear but none has type `EEG` | LSL works, but the wrong Collect export is enabled | Enable raw EEG output or confirm the actual type with the vendor |
| An EEG stream appears | Discovery works | Use its exact name/source fragment with `-LslNamePattern` only if normal matching still rejects it |

The warnings about missing `sklearn`, `joblib`, `pyriemann`, `torch`, or
`onnxruntime` are unrelated to acquisition discovery. They are optional model
training/inference packages and do not prevent pylsl from seeing an EEG outlet.

### Generic channel labels

Generic `ch_001..ch_065` labels establish count, not physical identity. The
confirmed generated config maps them positionally to the 65-value contract.
Mixed descriptors are accepted only when every meaningful label agrees with
its configured position; generic values and common names for the reserved
trigger/status value are canonicalized by position. A conflicting meaningful
label still fails.

If an XDF descriptor does not match the canonical name, EEGle accepts the
positional XDF order when the cap mapping was explicitly operator-confirmed,
the XDF has exactly 65 values, and the stable selected XDF stream identity is
available. This produces a validation warning, not a failure. Otherwise the XDF
reports the exact mismatching value numbers and observed/expected labels.

### `R_EEGleMarkers` disconnect line at a phase transition

Each baseline/task phase has a unique EEGle marker outlet and independent
marker receiver. At a phase boundary, EEGle now waits until that receiver has
seen every successfully emitted marker. LabRecorder then remains open for a
one-second EEG/marker tail guard before it receives its stop command. Some
Windows liblsl builds print a
native line such as `R_EEGleMarkers ... Stream transmission broke off;
re-connecting` during that close. The `R_EEGleMarkers` name identifies the
short-lived marker receiver, not the Neuracle EEG stream. EEGle does not treat
this phase-boundary line as an acquisition failure.

A phase still fails if a marker is missing, duplicated, reordered, has a
different LSL timestamp at the independent receipt, uses the wrong source ID,
or if a stimulus/baseline-start marker was not scheduled on its actual display
flip. Local marker delivery latency is recorded; a delivery taking more than
250 ms is a warning because the preserved LSL timestamp, rather than arrival
time, defines alignment.

PyXDF synchronized timestamps, exact XDF/receipt marker parity, and the locked
stream identities provide alignment evidence. Timestamp gaps, estimated missing
samples, effective-rate mismatch, retained fraction, and signal-quality findings
are warnings; they never stop a recording in progress.

A genuine EEG interruption is reported separately by the recorder health gate,
sample-heartbeat warning, XDF timestamp/sample-retention warning, or the selected
Neuracle stream identity. Review and explicitly accept quality warnings; do not
confuse them with the harmless marker-transition message.

### XDF file-growth warning during a live phase

LabRecorder can buffer XDF chunks on Windows, so the visible `recording.xdf`
file may remain the same size for 15 seconds or longer even though LabRecorder
is alive and EEG samples continue to arrive. EEGle records this as an
`xdf_buffering_warning` and continues. Final XDF structure, timestamps, streams,
sample retention, signal quality, and marker parity are validated after
LabRecorder stops.

Timestamp gaps, heartbeat degradation, and visible XDF growth pauses are
reported once while the authoritative XDF continues. A temporarily stale or
unreadable status JSON is also a warning. If LabRecorder itself exits, the
baseline/task reports that the primary acquisition has already failed.

### LabRecorder gate fails

- EEGle waits for LabRecorder's `OK` acknowledgement after each remote-control
  `update`, `filename`, `start`, and `stop` command. This prevents the start
  request from overtaking LabRecorder's stream refresh or filename update.
- A failure saying that commands were acknowledged but no XDF became observable
  means LabRecorder handled the requests but did not open the expected file. It
  is not an EEGle ACL or read-only restriction. Inspect the reported
  `labrecorder.stdout.log` and `labrecorder.stderr.log` paths for the exact
  LabRecorder-side path/start failure.
- EEGle keeps the nested preflight probe under the compact `preflight\xdfp`
  hierarchy and refuses to launch LabRecorder when an XDF destination exceeds
  its conservative 240-character Windows budget. If that early path error is
  reported, shorten `-DataRoot`, `-Participant`, or `-VisitId`; no recording has
  started and no acquired file has been overwritten.
- Ensure the path points to `LabRecorder.exe` itself.
- Close every manually running LabRecorder instance and stale
  `LabRecorder.exe` process in Task Manager.
- Confirm no other process owns TCP port 22345.
- Keep Collect streaming before starting EEGle; the managed recorder requires
  both the chosen EEG stream and EEGle's prestarted marker stream.

### Dynamic SART instruction window says Not Responding

Dynamic SART uses PsychoPy's asynchronous PTB keyboard queue. On Windows,
multiple physical keyboards are intentionally exposed to PTB as one combined
keyboard, so connecting a second keyboard does not require a device selection.
The preflight display/input check runs in a disposable Python process and stops
its PTB queue before exit, so it cannot leave an active queue competing with the
later task process. The task also stops its queue during guarded cleanup.
EEGle services the native PsychoPy/pyglet event queue while instruction,
practice, countdown, probe, break, and completion screens wait for input. This
keeps those static screens responsive without adding GUI-event work to the
frame-locked experimental trial loops.

Run `01-DryRun-Task.ps1` before repeating a recorded run. Confirm that the
instruction screen remains responsive and that SPACE from the intended response
keyboard advances into practice. If it does not, retain the generated task
session and terminal transcript; do not proceed to a participant recording.
The terminal prints the current task-worker stage and the retained
`phase_workers\*.status.json` path. A final stage of
`awaiting_instruction_key` means the worker created the window and PTB queue but
did not return from the instruction-key wait; `instruction_screen_complete`
proves that SPACE reached the Python task code.

### Task window is on the wrong display

Pass `-ScreenIndex 0` or `-ScreenIndex 1`; do not edit the generated config.
Scripts 01, 03, 04, and 07 use fullscreen by default so Pyglet's Windows backend
keeps swap-interval VSync enabled. Before LabRecorder starts, EEGle opens the
real window, measures the refresh rate, verifies the PTB keyboard queue, and
prints a complete monitor inventory. A measured 60 Hz or 120 Hz mode is selected
automatically (within 2 Hz); other display modes fail preflight. If the inventory
reports 100 Hz for the selected display, check that same display in Windows
Advanced display settings and disable Dynamic Refresh Rate or Variable Refresh
Rate for the acquisition test before retrying at a fixed supported mode.

### Script reports exit code 1 after the task window closes

The window closing is not the end of a Study 1 phase: LabRecorder still has to
stop, finalize, and pass XDF/marker validation. The final JSON object reports
`status`, `process_exit_code`, and, on failure, `failed_phase`,
`failure_detail`, and `next_action`. The Windows launchers also atomically save
that object under `operator_outcomes` in the selected data root and verify it
after Python exits. Only `status: completed` together with
`process_exit_code: 0` is accepted as success; a partial or failed durable
outcome remains exit code 1. A nonzero native Python exit is never masked, even
if the durable outcome says completed; the launcher prints both values and the
outcome path so that contradiction can be diagnosed. A missing or unreadable
requested outcome file also fails the launcher handshake. With Study 1's configured
`csv_mirror: false`, the absence of
`raw\eeg.csv` is no longer emitted as a post-recording warning or treated as an
incomplete phase.

### Windows blocks data writes

Pass an approved `-DataRoot`, commonly under `%LOCALAPPDATA%`. The scripts never
need to write participant/test data into the Git checkout.

## Direct module commands

The scripts are the recommended operator interface. For diagnosis, their core
Python commands use these activation-independent forms:

```powershell
.\.venv\Scripts\python.exe -m eegle.cli check-setup --help
.\.venv\Scripts\python.exe -m eegle.cli run-forward --help
.\.venv\Scripts\python.exe -m eegle.pipelines.study1 --help
```

Use the generated JSON paths under `.runtime\windows-neuracle64` when invoking
those commands manually. The checked-in `configs\study1_neuracle64.json`
intentionally remains locked against live use until the physical cap contract
has been confirmed.
