[CmdletBinding(PositionalBinding = $false)]
param(
    [ValidateRange(10, 100)]
    [int] $Trials = 10,
    [string] $Participant = "neuracle-display-check",
    [string] $DataRoot = "",
    [switch] $FullScreen
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common.ps1")

$python = Get-EeglePython
$config = Get-EegleConfigPath "display"
Assert-EegleFile $config "Display-only test config"
$resolvedDataRoot = Get-EegleDataRoot $DataRoot
$env:EEGLE_SESSION_ROOT = $resolvedDataRoot

Write-Host "Starting a Dynamic SART display test with $Trials experimental trials."
Write-Host "No EEG or LabRecorder is used. Press ESCAPE or Q to abort."
$arguments = @(
    "-m", "eegle.cli", "run-forward",
    "--config", $config,
    "--task", "dynamic_sart",
    "--task-mode", "psychopy",
    "--trials", [string] $Trials,
    "--participant", $Participant,
    "--skip-eeg",
    "--allow-missing-eeg"
)
if ($FullScreen) {
    $arguments += "--fullscreen"
}
& $python @arguments
Assert-EegleExit "display-only task run"
