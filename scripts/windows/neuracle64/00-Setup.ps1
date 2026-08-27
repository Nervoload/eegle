[CmdletBinding(PositionalBinding = $false)]
param(
    [string] $LabRecorderPath = "",
    [string] $DataRoot = "",
    [switch] $AddCommandsToUserPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common.ps1")

Write-Host "EEGle repository: $script:EegleRepoRoot"

$py = Get-Command py -ErrorAction Stop
& $py.Source -3.10 -c "import struct,sys; print(sys.version); print('bits=', struct.calcsize('P')*8); raise SystemExit(0 if sys.version_info[:2] == (3,10) and struct.calcsize('P')*8 == 64 else 2)"
Assert-EegleExit "64-bit Python 3.10 check"

$venvPython = Join-Path $script:EegleRepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    & $py.Source -3.10 -m venv (Join-Path $script:EegleRepoRoot ".venv")
    Assert-EegleExit "virtual-environment creation"
}
& $venvPython -c "import struct,sys; print('venv=', sys.executable); raise SystemExit(0 if sys.version_info[:2] == (3,10) and struct.calcsize('P')*8 == 64 else 2)"
if ($LASTEXITCODE -ne 0) {
    throw "The existing .venv is not 64-bit Python 3.10. Rename or remove that environment, then rerun setup."
}

& $venvPython -m pip install --upgrade pip
Assert-EegleExit "pip upgrade"
Push-Location $script:EegleRepoRoot
try {
    & $venvPython -m pip install -e ".[runtime,analysis]"
    Assert-EegleExit "EEGle runtime installation"
    & $venvPython -m pip check
    Assert-EegleExit "Python dependency consistency check"
}
finally {
    Pop-Location
}

$resolvedDataRoot = Get-EegleDataRoot $DataRoot
$env:EEGLE_SESSION_ROOT = $resolvedDataRoot
$env:EEGLE_RUNTIME_CACHE_ROOT = Join-Path ([IO.Path]::GetDirectoryName($resolvedDataRoot)) "runtime"
New-Item -ItemType Directory -Force -Path $script:EegleRuntimeRoot | Out-Null

$generator = Join-Path $script:EegleRepoRoot "scripts\prepare_neuracle64_windows_config.py"
$displayConfig = Get-EegleConfigPath "display"
& $venvPython $generator --base-config (Join-Path $script:EegleRepoRoot "configs\study1_neuracle64.json") --display-output $displayConfig
Assert-EegleExit "display-test config generation"

$labRecorder = Find-EegleLabRecorder $LabRecorderPath
if ($null -eq $labRecorder) {
    Write-Warning "LabRecorder was not found. The display-only test is ready; install LabRecorder before EEG tests."
}
else {
    Write-Host "LabRecorder found: $labRecorder"
}

& $venvPython -c "import importlib.metadata as m; print('pylsl', m.version('pylsl')); print('pyxdf', m.version('pyxdf')); import psychopy; print('psychopy', psychopy.__version__)"
Assert-EegleExit "runtime import/version check"
& $venvPython -m eegle.cli check-setup --config $displayConfig --allow-missing-eeg --lsl-wait 0.2
Assert-EegleExit "software/display setup check"

if ($AddCommandsToUserPath) {
    $commandPaths = @(
        (Join-Path $script:EegleRepoRoot ".venv\Scripts"),
        $PSScriptRoot
    )
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    $pathChanged = $false
    foreach ($commandPath in $commandPaths) {
        if (-not ($entries | Where-Object { $_.TrimEnd("\") -ieq $commandPath.TrimEnd("\") })) {
            $entries += $commandPath
            $pathChanged = $true
            Write-Host "Added to user PATH for future terminals: $commandPath"
        }
        else {
            Write-Host "EEGle command directory is already on the user PATH: $commandPath"
        }
    }
    if ($pathChanged) {
        [Environment]::SetEnvironmentVariable("Path", ($entries -join ";"), "User")
    }
    $currentEntries = @($env:Path -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    foreach ($commandPath in $commandPaths) {
        if (-not ($currentEntries | Where-Object { $_.TrimEnd("\") -ieq $commandPath.TrimEnd("\") })) {
            $currentEntries += $commandPath
        }
    }
    $env:Path = $currentEntries -join ";"
    Write-Host "FullRun and FullTest are now available without a repository-relative path in this and future terminals."
}

Write-Host ""
Write-Host "Setup complete. Data root: $resolvedDataRoot"
Write-Host "Current-terminal activation: .\.venv\Scripts\Activate.ps1"
Write-Host "Next: .\scripts\windows\neuracle64\01-DryRun-Task.ps1"
