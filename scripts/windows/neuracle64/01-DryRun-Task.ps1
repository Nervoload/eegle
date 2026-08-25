[CmdletBinding(PositionalBinding = $false)]
param(
    [ValidateRange(10, 100)]
    [int] $Trials = 10,
    [string] $Participant = "neuracle-display-check",
    [string] $DataRoot = "",
    [ValidateRange(0, 16)]
    [int] $ScreenIndex = 0,
    [switch] $FullScreen,
    [switch] $Windowed
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common.ps1")

if ($FullScreen -and $Windowed) {
    throw "-FullScreen and -Windowed cannot be used together. Fullscreen is the Windows default."
}
$useFullScreen = -not $Windowed

$python = Get-EeglePython
$config = Get-EegleConfigPath "display"
Assert-EegleFile $config "Display-only test config"
$resolvedDataRoot = Get-EegleDataRoot $DataRoot
$env:EEGLE_SESSION_ROOT = $resolvedDataRoot

Write-Host "Starting a Dynamic SART display test with $Trials experimental trials."
Write-Host "Using PsychoPy monitor index $ScreenIndex in $(if ($useFullScreen) { 'fullscreen VBlank-synchronized' } else { 'windowed diagnostic-only' }) mode."
Write-Host "No EEG or LabRecorder is used. Press ESCAPE or Q to abort."
$arguments = @(
    "-m", "eegle.cli", "run-forward",
    "--config", $config,
    "--task", "dynamic_sart",
    "--task-mode", "psychopy",
    "--trials", [string] $Trials,
    "--participant", $Participant,
    "--screen-index", [string] $ScreenIndex,
    "--skip-eeg",
    "--allow-missing-eeg"
)
if ($useFullScreen) {
    $arguments += "--fullscreen"
}
else {
    $arguments += "--windowed"
}
& $python @arguments
Assert-EegleExit "display-only task run"
