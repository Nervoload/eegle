[CmdletBinding(PositionalBinding = $false)]
param(
    [ValidateRange(10, 20)]
    [int] $Trials = 20,
    [string] $Participant = "neuracle-eeg-task-test",
    [string] $DataRoot = "",
    [switch] $FullScreen,
    [switch] $ConfirmElectrodes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common.ps1")

if (-not $ConfirmElectrodes) {
    throw "Inspect cap contact/impedance in Collect, then rerun with -ConfirmElectrodes."
}
$python = Get-EeglePython
$config = Get-EegleConfigPath "live-task"
Assert-EegleLiveConfig $config
$resolvedDataRoot = Get-EegleDataRoot $DataRoot
$env:EEGLE_SESSION_ROOT = $resolvedDataRoot

Write-Host "Running the full physical preflight before the short recorded task..."
$preflightId = New-EegleRunId "task-test-preflight"
& $python -m eegle.pipelines.study1 `
    --config $config `
    --participant $Participant `
    --visit 1 `
    --visit-id $preflightId `
    --task-mode psychopy `
    --preflight-only `
    --confirm-electrodes `
    --session-root $resolvedDataRoot `
    --lsl-wait 10
Assert-EegleExit "full Neuracle/LSL/electrode/LabRecorder preflight"

Write-Host "Starting $Trials experimental Dynamic SART trials with authoritative XDF recording."
Write-Host "Keep Neuracle Collect LSL streaming. Do not start LabRecorder manually."
$taskArguments = @(
    "-m", "eegle.cli", "run-forward",
    "--config", $config,
    "--task", "dynamic_sart",
    "--task-mode", "psychopy",
    "--trials", [string] $Trials,
    "--participant", $Participant,
    "--require-eeg"
)
if ($FullScreen) {
    $taskArguments += "--fullscreen"
}
& $python @taskArguments
Assert-EegleExit "short recorded EEG task"
