[CmdletBinding(PositionalBinding = $false)]
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
    [ValidateRange(10, 100000)]
    [int] $Trials = 30,
    [ValidateRange(10, 10000)]
    [int] $PracticeTrials = 30,
    [ValidateRange(1, 9999)]
    [int] $PracticeNoGoTrials = 4,
    [ValidateRange(1, 100)]
    [int] $PracticeMaxRounds = 3,
    [switch] $SkipPractice,
    [ValidateRange(0, 600)]
    [double] $BaselineSeconds = 60,
    [switch] $SkipBaseline,
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
if (-not $SkipPractice -and $PracticeNoGoTrials -ge $PracticeTrials) {
    throw "-PracticeNoGoTrials must be less than -PracticeTrials."
}
$python = Get-EeglePython
$config = Get-EegleConfigPath "live"
Update-EegleGeneratedLiveConfigs $python $config
$resolvedDataRoot = Get-EegleDataRoot $DataRoot
$env:EEGLE_SESSION_ROOT = $resolvedDataRoot
$outcomeFile = New-EegleStudyOutcomePath $resolvedDataRoot

Write-Host "Starting the complete short Study 1 test:"
Write-Host "  full preflight, three-second XDF recording probe, and electrode checks"
if ($SkipBaseline) {
    Write-Host "  resting baseline skipped by operator request"
}
else {
    Write-Host "  $BaselineSeconds seconds eyes open"
    Write-Host "  $BaselineSeconds seconds eyes closed"
}
if ($SkipPractice) {
    Write-Host "  participant practice skipped by operator request"
}
else {
    Write-Host "  up to $PracticeMaxRounds practice round(s) of $PracticeTrials trials ($PracticeNoGoTrials no-go)"
}
Write-Host "  $Trials experimental trials"
Write-Host "  fixed timing: 250 ms digit + 1350 ms fixation (1600 ms SOI; no jitter)"
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
    "--smoke",
    "--trials", [string] $Trials,
    "--window-size", "1000", "700",
    "--confirm-electrodes",
    "--session-root", $resolvedDataRoot,
    "--result-file", $outcomeFile,
    "--lsl-wait", "10"
)
if ($SkipPractice) {
    $arguments += "--skip-practice"
}
else {
    $arguments += @(
        "--include-practice",
        "--practice-trials", [string] $PracticeTrials,
        "--practice-no-go-trials", [string] $PracticeNoGoTrials,
        "--practice-max-rounds", [string] $PracticeMaxRounds
    )
}
if ($SkipBaseline) {
    $arguments += "--skip-baseline"
}
else {
    $arguments += @("--baseline-seconds", [string] $BaselineSeconds)
}
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
$nativeStudyExitCode = $LASTEXITCODE
$studyExitCode = Resolve-EegleStudyExit $nativeStudyExitCode $outcomeFile
Write-Host "EEGle Study 1 native process exit code: $nativeStudyExitCode"
Write-Host "EEGle Study 1 verified outcome exit code: $studyExitCode"
Write-Host "EEGle Study 1 outcome file: $outcomeFile"
Assert-EegleExit "full short Study 1 test" $studyExitCode
Write-Host "Full short Study 1 test completed successfully."
