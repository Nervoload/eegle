param(
    [Parameter(Mandatory = $true)]
    [string] $Participant,
    [Parameter(Mandatory = $true)]
    [ValidateRange(0, 9)]
    [int] $NoGoDigit,
    [Parameter(Mandatory = $true)]
    [string] $Operator,
    [string] $DataRoot = "",
    [string] $VisitId = "",
    [ValidateRange(1, 600)]
    [int] $BaselineSeconds = 60,
    [switch] $Resume,
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
$config = Get-EegleConfigPath "live"
Update-EegleGeneratedLiveConfigs $python $config
$resolvedDataRoot = Get-EegleDataRoot $DataRoot
$env:EEGLE_SESSION_ROOT = $resolvedDataRoot

Write-Host "Starting the complete short Study 1 test:"
Write-Host "  full preflight and electrode checks"
Write-Host "  $BaselineSeconds seconds eyes open"
Write-Host "  $BaselineSeconds seconds eyes closed"
Write-Host "  participant practice"
Write-Host "  30 experimental trials (three 10-trial blocks)"
Write-Host "  fixed timing: 250 ms digit + 1350 ms fixation (1600 ms SOI; no jitter)"
Write-Host "Keep Neuracle Collect LSL streaming. EEGle launches/stops LabRecorder."
Write-Host "A phase-end liblsl ERR mentioning R_EEGleMarkers is expected when EEGle closes that phase's marker receiver; it is not the Neuracle EEG stream."
Write-Host "LabRecorder may buffer XDF disk writes; paused XDF file growth is a warning while the independent CSV/LSL recorder is still receiving samples."

$arguments = @(
    "-m", "eegle.pipelines.study1",
    "--config", $config,
    "--participant", $Participant,
    "--visit", "1",
    "--operator", $Operator,
    "--task-mode", "psychopy",
    "--no-go-digit", [string] $NoGoDigit,
    "--smoke",
    "--include-practice",
    "--baseline-seconds", [string] $BaselineSeconds,
    "--window-size", "1000", "700",
    "--confirm-electrodes",
    "--session-root", $resolvedDataRoot,
    "--lsl-wait", "10"
)
if (-not [string]::IsNullOrWhiteSpace($VisitId)) {
    $arguments += @("--visit-id", $VisitId)
}
if ($Resume) {
    $arguments += "--resume"
}
else {
    $arguments += "--retry-incomplete"
}
if ($FullScreen) {
    $arguments += "--fullscreen"
}
& $python @arguments
Assert-EegleExit "full short Study 1 test"
