# Neuracle W64 Windows Test Runbook

This is the operator path for testing EEGle on a Windows x64 Dell with the
Neuracle 64-channel wet system. Neuracle Collect owns the amplifier connection
and publishes EEG to Lab Streaming Layer (LSL). EEGle owns the task, marker
stream, managed LabRecorder process, XDF file, and CSV safety mirror.

The six operator PowerShell scripts are under
`scripts\windows\neuracle64`:

| Script | Purpose | EEG recorded? |
| --- | --- | --- |
| `00-Setup.ps1` | Create/check the Python 3.10 environment | No |
| `01-DryRun-Task.ps1` | Show a 10-trial Dynamic SART task | No |
| `02-Test-NeuracleLsl.ps1` | Discover Collect, lock the confirmed cap contract, and run physical preflight | No recording session |
| `03-Run-EEGTaskTest.ps1` | Full preflight followed by a 10-20 trial recorded task | Yes: XDF + CSV |
| `04-Run-FullShortTest.ps1` | Preflight, resting controls, practice, and 30 experimental trials | Yes: XDF + CSV |
| `05-Diagnose-Lsl.ps1` | Separate local pylsl/config failures from a missing external outlet | No |

Run every command below from a 64-bit Windows PowerShell terminal. Do not use
Git Bash or WSL for the hardware run.

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

Close and reopen PowerShell after changing the user `PATH`. The six operator `.ps1`
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

### Resume an interrupted complete short test

Resume uses the same participant, no-go digit, operator, baseline duration, and
visit identity. Completed phase boundaries are skipped; an interrupted active
recording is not appended to or overwritten.

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
confirmed generated config maps them positionally to the 65-value contract. If
Collect later exposes meaningful labels, they must match the configured order
exactly; a mismatch remains a preflight failure.

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
