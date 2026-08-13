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
| `03-Run-EEGTaskTest.ps1` | Full preflight followed by a 10-20 trial recorded task | Yes: XDF + CSV |
| `04-Run-FullShortTest.ps1` | Preflight, resting controls, practice, and 30 experimental trials | Yes: XDF + CSV |
| `05-Diagnose-Lsl.ps1` | Separate local pylsl/config failures from a missing external outlet | No |
| `06-Test-StorageAccess.ps1` | Reproduce parent/child recording permission operations without EEG | No |
| `07-Run-Full.ps1` | Complete two-minute baselines, practice gate, and 1,000-trial task | Yes: XDF + CSV |

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
- live sample delivery;
- 65 values in the confirmed transport order (64 physical inputs plus value 65);
- a nominal/observed rate compatible with 1000 Hz;
- a local EEGle marker-stream loopback;
- PsychoPy availability;
- writable storage and free space;
- LabRecorder executable, `pyxdf`, and loopback control-port availability;
- channel signal/contact summary plus the operator's explicit confirmation for
  the 64 physical inputs.

`-ConfirmElectrodes` means the operator has already inspected cap contact or
impedance in Collect. It is an attestation, not an impedance measurement read
from Collect. If available, a structured quality file can instead be passed to
the underlying `study1 --preflight-only` command with
`--electrode-quality-file`.

## 4. Short EEG task test: 10-20 trials

Keep Collect streaming. Do not start LabRecorder manually. The script repeats
the full physical preflight, then records the task to XDF with a CSV mirror:

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
raw\eeg.csv
raw\xdf_metadata.json
raw\eeg_metadata.json
raw\lsl_markers_received.csv
events\events.jsonl
events\dynamic_sart_trials.jsonl
process_logs\manager_summary.json
```

The XDF is the primary raw recording. The CSV is an independent live safety
mirror. A task run is not successful merely because the PsychoPy window closed;
the final command must exit without an error and the recorder/validation status
must be complete.

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

The short test uses the same timing contract as a genuine Study 1 run: a 250 ms
digit, 1350 ms post-digit fixation, fixed 1600 ms SOI, no intentional jitter,
VBlank waiting, and a required measured refresh-rate match.

```powershell
$TestId = "systemtest-$(Get-Date -Format 'yyyyMMdd-HHmmss')"

.\scripts\windows\neuracle64\04-Run-FullShortTest.ps1 `
  -Participant $TestId `
  -VisitId "$TestId-visit1" `
  -NoGoDigit 3 `
  -Operator "operator-initials" `
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
`full_1000_support500_query500_v1` acquisition profile. It cannot be combined
with the smoke profile, a non-120-second baseline override, Visit 2, or skipped
practice. The profile name and protocol hash are written to the participant and
visit manifests, preventing an accidental resume with the standard or short
protocol.

To resume at a completed phase boundary, repeat the exact identity and options:

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
an interrupted recording. A participant who completed the run needs a new
participant/visit identity for another test.

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

LabRecorder and the CSV mirror can serialize the same Collect channel
descriptors differently. If an XDF descriptor does not match the canonical
name, EEGle accepts the positional XDF order only when all of the following are
true: the cap mapping was explicitly operator-confirmed, the XDF has exactly 65
values, the independent CSV mirror stopped cleanly, it recorded the same LSL
stream identity, and its raw contract proves that channel value order was not
changed. This produces a validation warning, not a failure. Otherwise the XDF
fails with the exact mismatching value numbers and observed/expected labels.

### `R_EEGleMarkers` disconnect line at a phase transition

Each baseline/task phase has a unique EEGle marker outlet and independent
marker receiver. When the phase completes, EEGle deliberately closes them so
LabRecorder can finalize that phase's XDF. Some Windows liblsl builds print a
native line such as `R_EEGleMarkers ... Stream transmission broke off;
re-connecting` during that close. The `R_EEGleMarkers` name identifies the
short-lived marker receiver, not the Neuracle EEG stream. EEGle does not treat
this phase-boundary line as an acquisition failure.

A genuine EEG interruption is reported separately by the recorder health gate,
CSV timestamp-gap validation, XDF timestamp-gap validation, or the selected
Neuracle stream identity. Do not ignore those failures merely because the
marker transition message is harmless.

### LabRecorder gate fails

- Ensure the path points to `LabRecorder.exe` itself.
- Close every manually running LabRecorder instance and stale
  `LabRecorder.exe` process in Task Manager.
- Confirm no other process owns TCP port 22345.
- Keep Collect streaming before starting EEGle; the managed recorder requires
  both the chosen EEG stream and EEGle's prestarted marker stream.

### Task window is on the wrong display

The generated configs use screen 0 and a resizable 1000 by 700 window for safe
testing. Change only the local generated display settings after verifying the
correct Windows display index. Add `-FullScreen` to scripts 01, 03, or 04 after
the abort keys have been tested. At launch, EEGle measures the refresh rate and
requires it to match the generated config (60 Hz by default, within 2 Hz). If
Windows is intentionally set to another rate, update
`hardware.display.expected_refresh_rate_hz` in all three generated JSON files
before rerunning setup tests.

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
