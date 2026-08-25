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

Write-Host "Starting the complete 1,000-trial Study 1 run:"
Write-Host "  full preflight, three-second XDF recording probe, storage gate, and electrode checks"
Write-Host "  120 seconds eyes open"
Write-Host "  120 seconds eyes closed"
Write-Host "  criterion-gated participant practice, then a participant-ready confirmation"
Write-Host "  1,000 experimental trials in four 250-trial sections"
Write-Host "  trials 1-500 are support; trials 501-1000 are held-out query"
Write-Host "  breaks after trials 250, 500, and 750 (continue from 30 seconds; automatic at 60)"
Write-Host "  fixed timing: 250 ms digit + 1350 ms fixation (1600 ms SOI; no jitter)"
Write-Host "The main task lasts about 26 minutes 40 seconds before practice and breaks."
Write-Host "Keep Neuracle Collect LSL streaming. EEGle launches/stops LabRecorder."
Write-Host "A phase-end liblsl ERR mentioning R_EEGleMarkers is expected when EEGle closes that phase's marker receiver; it is not the Neuracle EEG stream."
Write-Host "LabRecorder may buffer XDF disk writes; paused XDF growth and CSV-mirror degradation are warnings. Primary XDF failure remains fatal."
Write-Host "Review each numbered preflight/post-recording warning list; type YES only if you accept it."

$arguments = @(
    "-m", "eegle.pipelines.study1",
    "--config", $config,
    "--participant", $Participant,
    "--visit", "1",
    "--operator", $Operator,
    "--task-mode", "psychopy",
    "--no-go-digit", [string] $NoGoDigit,
    "--full-1000",
    "--include-practice",
    "--baseline-seconds", "1",
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
$studyExitCode = $LASTEXITCODE
Write-Host "EEGle Study 1 process exit code: $studyExitCode"
Assert-EegleExit "complete 1,000-trial Study 1 run" $studyExitCode
Write-Host "Complete 1,000-trial Study 1 run completed successfully."
