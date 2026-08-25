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
    [int] $Trials = 1000,
    [ValidateRange(10, 10000)]
    [int] $PracticeTrials = 30,
    [ValidateRange(1, 9999)]
    [int] $PracticeNoGoTrials = 4,
    [ValidateRange(1, 100)]
    [int] $PracticeMaxRounds = 3,
    [switch] $SkipPractice,
    [ValidateRange(0, 86400)]
    [double] $BaselineSeconds = 120,
    [switch] $SkipBaseline,
    [ValidateRange(0, 16)]
    [int] $ScreenIndex = 0,
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

Write-Host "Starting the Study 1 Visit 1 run ($Trials experimental trials):"
Write-Host "  full preflight, three-second XDF recording probe, storage gate, and electrode checks"
Write-Host "  PsychoPy monitor index $ScreenIndex (window moves are detected before refresh measurement)"
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
    Write-Host "  up to $PracticeMaxRounds criterion-gated practice round(s) of $PracticeTrials trials ($PracticeNoGoTrials no-go), then participant-ready confirmation"
}
if ($Trials -eq 1000) {
    Write-Host "  1,000 experimental trials in four 250-trial sections"
    Write-Host "  trials 1-500 are support; trials 501-1000 are held-out query"
    Write-Host "  breaks after trials 250, 500, and 750 (continue from 30 seconds; automatic at 60)"
}
else {
    Write-Host "  $Trials experimental trials using the deterministic shortened support/query plan"
}
Write-Host "  fixed timing: 250 ms digit + 1350 ms fixation (1600 ms SOI; no jitter)"
Write-Host "The experimental trials last about $([math]::Round($Trials * 1.6 / 60, 1)) minutes before practice and breaks."
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
    "--trials", [string] $Trials,
    "--screen-index", [string] $ScreenIndex,
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
Assert-EegleExit "Study 1 Visit 1 run" $studyExitCode
Write-Host "Study 1 Visit 1 run completed successfully."
