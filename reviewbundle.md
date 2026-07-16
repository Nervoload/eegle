Use this PowerShell sequence from the repository root. Replace participant and operator values before the full recordings.

The current equivalent of `check-eeg` is `eegle check-setup --require-eeg`.

### 0. Prepare the approved Windows paths

```powershell
Set-Location "C:\Users\Surettej\Documents\Codespace\eegle"
.\.venv\Scripts\Activate.ps1

# Ensure this working tree is installed.
python -m pip install -e ".[runtime,ml]"

$EegleData = Join-Path $env:LOCALAPPDATA "EEGle\data"
$env:EEGLE_SESSION_ROOT = $EegleData

$ConfigDir = Join-Path $EegleData "operator-configs"
$CheckDir = Join-Path $EegleData "setup-checks"

New-Item -ItemType Directory -Force $EegleData, $ConfigDir, $CheckDir | Out-Null

# Make Windows-safe copies of both configs. These copies put runtime caches
# under LOCALAPPDATA instead of the repository in Documents.
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Write-DsartConfig {
    param(
        [string]$Source,
        [string]$Target
    )

    $Config = Get-Content $Source -Raw | ConvertFrom-Json
    $Config.runtime.session_root = $EegleData
    $Config.runtime.runtime_cache_dir = Join-Path $EegleData ".runtime"

    $Json = $Config | ConvertTo-Json -Depth 100
    [System.IO.File]::WriteAllText($Target, $Json, $Utf8NoBom)
}

$Dsart8Config = Join-Path $ConfigDir "record_dsart8.windows.json"
$Dsart32Config = Join-Path $ConfigDir "record_dsart32.windows.json"

Write-DsartConfig ".\configs\record_dsart8.json" $Dsart8Config
Write-DsartConfig ".\configs\record_dsart32.json" $Dsart32Config

$RunStamp = Get-Date -Format "yyyyMMdd-HHmmss"
```

### 1. PsychoPy tests without EEG

These run the complete two-session suite lifecycle, shortened to ten trials per session, two-second baseline conditions, and no timed break.

```powershell
dsart8 `
  --config $Dsart8Config `
  --participant "smoke-noeeg-dsart8" `
  --visit-id "noeeg-dsart8-$RunStamp" `
  --task-mode psychopy `
  --trials 10 `
  --baseline-seconds 2 `
  --break-seconds 0 `
  --skip-eeg `
  --window-size 1000 700 `
  --session-root $EegleData

if ($LASTEXITCODE -ne 0) {
    throw "DSART8 no-EEG PsychoPy test failed."
}
```

```powershell
dsart32 `
  --config $Dsart32Config `
  --participant "smoke-noeeg-dsart32" `
  --visit-id "noeeg-dsart32-$RunStamp" `
  --task-mode psychopy `
  --trials 10 `
  --baseline-seconds 2 `
  --break-seconds 0 `
  --skip-eeg `
  --window-size 1000 700 `
  --session-root $EegleData

if ($LASTEXITCODE -ne 0) {
    throw "DSART32 no-EEG PsychoPy test failed."
}
```

### 2. Check live EEG input

Start NIC2 and the 8-channel LSL outlet first. Close unrelated EEG outlets.

```powershell
Read-Host "Start the DSART8/NIC2 LSL stream, then press ENTER"

eegle check-setup `
  --config $Dsart8Config `
  --require-eeg `
  --lsl-wait 10 `
  --save (Join-Path $CheckDir "dsart8-check-$RunStamp.json")

if ($LASTEXITCODE -ne 0) {
    throw "DSART8 EEG setup check failed. Do not record."
}
```

Stop the 8-channel outlet, switch to the confirmed 32-channel montage, and start its NIC2 LSL outlet.

```powershell
Read-Host "Stop DSART8, start the DSART32/NIC2 LSL stream, then press ENTER"

eegle check-setup `
  --config $Dsart32Config `
  --require-eeg `
  --lsl-wait 10 `
  --save (Join-Path $CheckDir "dsart32-check-$RunStamp.json")

if ($LASTEXITCODE -ne 0) {
    throw "DSART32 EEG setup check failed. Do not record."
}
```

Each check should find exactly one matching stream with the expected channel count and 500 Hz sample rate.

### 3. Full DSART8 recording

Set the real identifiers, stop the 32-channel outlet, and restart the 8-channel outlet.

```powershell
$Participant8 = "sub-001"
$Operator = "operator-id"
$Visit8 = "dsart8-$(Get-Date -Format 'yyyyMMdd')"

Read-Host "Start the DSART8/NIC2 LSL stream and confirm the 8-channel montage, then press ENTER"

dsart8 `
  --config $Dsart8Config `
  --participant $Participant8 `
  --visit-id $Visit8 `
  --operator $Operator `
  --task-mode psychopy `
  --session-root $EegleData `
  --lsl-wait 10

if ($LASTEXITCODE -ne 0) {
    throw "DSART8 recording did not complete. Preserve the visit and inspect it before resuming."
}
```

Do not add `--skip-eeg`, `--trials`, `--baseline-seconds`, `--break-seconds`, or `--confirm-electrodes` to the formal run.

### 4. Full DSART32 recording

Stop the 8-channel outlet and start the 32-channel outlet.

```powershell
$Participant32 = "sub-002"
$Visit32 = "dsart32-$(Get-Date -Format 'yyyyMMdd')"

Read-Host "Start the DSART32/NIC2 LSL stream and confirm the 32-channel montage, then press ENTER"

dsart32 `
  --config $Dsart32Config `
  --participant $Participant32 `
  --visit-id $Visit32 `
  --operator $Operator `
  --task-mode psychopy `
  --session-root $EegleData `
  --lsl-wait 10

if ($LASTEXITCODE -ne 0) {
    throw "DSART32 recording did not complete. Preserve the visit and inspect it before resuming."
}
```

If a formal visit is interrupted, rerun its exact command with `--resume`; keep the same participant, visit ID, config, and session root.

I changed the runtime so JSON rewriting is no longer necessary. Relative caches now honor:

- `EEGLE_RUNTIME_CACHE_ROOT`
- Otherwise, `EEGLE_SESSION_ROOT`

This works for `eegle check-setup` as well as both DSART suites. The full suite passes: **252 tests**.

Use these Constrained Language Mode-safe commands. They avoid activation scripts, PowerShell functions, `New-Object`, JSON conversion, and static .NET calls.

### Preparation

```powershell
Set-Location "C:\Users\Surettej\Documents\Codespace\eegle"

$env:EEGLE_SESSION_ROOT = "$env:LOCALAPPDATA\EEGle\data"
$env:EEGLE_RUNTIME_CACHE_ROOT = "$env:LOCALAPPDATA\EEGle\runtime"

.\.venv\Scripts\python.exe -m pip install -e ".[runtime,ml]"
```

The Python code creates the data and runtime directories automatically.

### 1. PsychoPy tests without EEG

Use new visit IDs if you repeat these tests.

```powershell
.\.venv\Scripts\dsart8.exe `
  --config .\configs\record_dsart8.json `
  --participant smoke-noeeg-dsart8 `
  --visit-id smoke-dsart8-001 `
  --task-mode psychopy `
  --trials 10 `
  --baseline-seconds 2 `
  --break-seconds 0 `
  --skip-eeg `
  --window-size 1000 700 `
  --session-root "$env:EEGLE_SESSION_ROOT"
```

```powershell
.\.venv\Scripts\dsart32.exe `
  --config .\configs\record_dsart32.json `
  --participant smoke-noeeg-dsart32 `
  --visit-id smoke-dsart32-001 `
  --task-mode psychopy `
  --trials 10 `
  --baseline-seconds 2 `
  --break-seconds 0 `
  --skip-eeg `
  --window-size 1000 700 `
  --session-root "$env:EEGLE_SESSION_ROOT"
```

### 2. Check live EEG

Start only the DSART8/NIC2 outlet:

```powershell
.\.venv\Scripts\eegle.exe check-setup `
  --config .\configs\record_dsart8.json `
  --require-eeg `
  --lsl-wait 10 `
  --save "$env:EEGLE_SESSION_ROOT\setup-checks\dsart8-check.json"
```

Stop DSART8, then start only the DSART32/NIC2 outlet:

```powershell
.\.venv\Scripts\eegle.exe check-setup `
  --config .\configs\record_dsart32.json `
  --require-eeg `
  --lsl-wait 10 `
  --save "$env:EEGLE_SESSION_ROOT\setup-checks\dsart32-check.json"
```

Only proceed when each command exits successfully and reports exactly one matching stream, the expected channel count, and 500 Hz.

### 3. Full DSART8 recording

Restart the DSART8/NIC2 outlet and replace the identifiers:

```powershell
.\.venv\Scripts\dsart8.exe `
  --config .\configs\record_dsart8.json `
  --participant sub-001 `
  --visit-id dsart8-visit-001 `
  --operator operator-id `
  --task-mode psychopy `
  --session-root "$env:EEGLE_SESSION_ROOT" `
  --lsl-wait 10
```

### 4. Full DSART32 recording

Stop DSART8, start the DSART32/NIC2 outlet, and replace the identifiers:

```powershell
.\.venv\Scripts\dsart32.exe `
  --config .\configs\record_dsart32.json `
  --participant sub-002 `
  --visit-id dsart32-visit-001 `
  --operator operator-id `
  --task-mode psychopy `
  --session-root "$env:EEGLE_SESSION_ROOT" `
  --lsl-wait 10
```

Run each command separately and confirm `$LASTEXITCODE` is `0` before proceeding:

```powershell
$LASTEXITCODE
```

If a formal recording is interrupted, rerun its exact command with `--resume`.