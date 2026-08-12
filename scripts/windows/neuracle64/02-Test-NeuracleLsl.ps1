param(
    [string] $LabRecorderPath = "",
    [string] $DataRoot = "",
    [string] $Participant = "neuracle-system-check",
    [string] $Reference = "CPz",
    [string] $Ground = "AFz",
    [string] $EogAllocation = "ECG, HEOR, HEOL, VEOU, VEOL",
    [string] $LslNamePattern = "",
    [switch] $DiscoverOnly,
    [switch] $ConfirmCapContract,
    [switch] $ConfirmElectrodes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common.ps1")

$python = Get-EeglePython
$resolvedDataRoot = Get-EegleDataRoot $DataRoot
$env:EEGLE_SESSION_ROOT = $resolvedDataRoot
$checkRoot = Join-Path $resolvedDataRoot "system_checks\collect-discovery"
New-Item -ItemType Directory -Force -Path $checkRoot | Out-Null
$discoveryReport = Join-Path $checkRoot "$(New-EegleRunId 'collect-lsl').json"
$baseConfig = Join-Path $script:EegleRepoRoot "configs\study1_neuracle64.json"
$generator = Join-Path $script:EegleRepoRoot "scripts\prepare_neuracle64_windows_config.py"
$discoveryConfig = Get-EegleConfigPath "display"
$discoveryConfigArguments = @(
    $generator,
    "--base-config", $baseConfig,
    "--display-output", $discoveryConfig
)
if (-not [string]::IsNullOrWhiteSpace($LslNamePattern)) {
    $discoveryConfigArguments += @("--lsl-name-pattern", $LslNamePattern)
}
& $python @discoveryConfigArguments
Assert-EegleExit "Collect discovery-config generation"

Write-Host "Discovering the Neuracle Collect LSL stream and probing samples..."
& $python -m eegle.cli check-setup `
    --config $discoveryConfig `
    --require-eeg `
    --lsl-wait 10 `
    --save $discoveryReport
$discoveryExitCode = $LASTEXITCODE

if (-not (Test-Path -LiteralPath $discoveryReport -PathType Leaf)) {
    throw "Neuracle Collect LSL discovery did not create its report (exit $discoveryExitCode): $discoveryReport"
}

$report = Get-Content -LiteralPath $discoveryReport -Raw | ConvertFrom-Json
$probe = $report | Where-Object { $_.name -eq "eeg_sample_probe" } | Select-Object -First 1
if ($null -eq $probe) {
    $lsl = $report | Where-Object { $_.name -eq "lsl" } | Select-Object -First 1
    Write-Host "LSL streams visible to EEGle:"
    $visibleStreams = @()
    if ($null -ne $lsl -and $null -ne $lsl.data.PSObject.Properties["streams"]) {
        $visibleStreams = @($lsl.data.streams)
        foreach ($stream in $visibleStreams) {
            Write-Host "  $($stream.name) | type=$($stream.type) | channels=$($stream.channel_count) | rate=$($stream.nominal_srate) | source=$($stream.source_id)"
        }
    }
    else {
        Write-Host "  none (or LSL discovery itself failed)"
    }
    if ($visibleStreams.Count -eq 0) {
        throw "Zero LSL streams are visible. -LslNamePattern cannot help until discovery sees at least one outlet. Keep Collect running and run .\scripts\windows\neuracle64\05-Diagnose-Lsl.ps1 -DataRoot `$EegleData, then test visibility independently with LabRecorder's Update button. Report: $discoveryReport"
    }
    throw "LSL works, but no unique Neuracle EEG stream matched. Inspect the streams above, close duplicates, or rerun with -LslNamePattern using part of the EEG outlet's name/source ID. Report: $discoveryReport"
}
Write-Host "Stream: $($probe.data.stream.name)"
Write-Host "Source ID: $($probe.data.stream.source_id)"
Write-Host "Channels/rate: $($probe.data.stream.channel_count) / $($probe.data.stream.nominal_srate) Hz"
Write-Host "Samples read: $($probe.data.sample_count)"
Write-Host "Raw Collect labels: $(@($probe.data.original_channel_names) -join ', ')"
Write-Host "Discovery report: $discoveryReport"
if ($discoveryExitCode -ne 0) {
    throw "Neuracle Collect LSL discovery reported a failed check. Review: $discoveryReport"
}
if ([int] $probe.data.stream.channel_count -ne 65) {
    throw "Collect must expose exactly 65 values for this W64 LSL contract (64 physical inputs plus the reserved trigger/status value); observed $($probe.data.stream.channel_count)."
}
if ([Math]::Abs(([double] $probe.data.stream.nominal_srate) - 1000.0) -ge 1.0) {
    throw "Collect must stream at 1000 Hz for this Study 1 contract; observed $($probe.data.stream.nominal_srate) Hz."
}
if ([int] $probe.data.sample_count -le 0) {
    throw "Collect's LSL outlet was discovered but yielded no EEG samples."
}

if ($DiscoverOnly) {
    Write-Host "Discovery-only check complete. No live config was generated."
    exit 0
}
if (-not $ConfirmElectrodes) {
    throw "Inspect cap contact/impedance in Collect, then rerun the physical preflight with -ConfirmElectrodes."
}

$liveConfig = Get-EegleConfigPath "live"
$liveTaskConfig = Get-EegleConfigPath "live-task"
if ($ConfirmCapContract) {
    $labRecorder = Find-EegleLabRecorder $LabRecorderPath
    if ($null -eq $labRecorder) {
        throw "LabRecorder was not found. Pass -LabRecorderPath C:\path\to\LabRecorder.exe."
    }
    $arguments = @(
        $generator,
        "--base-config", $baseConfig,
        "--display-output", (Get-EegleConfigPath "display"),
        "--live-output", $liveConfig,
        "--live-task-output", $liveTaskConfig,
        "--labrecorder", $labRecorder,
        "--reference", $Reference,
        "--ground", $Ground,
        "--eog-allocation", $EogAllocation,
        "--confirm-cap-contract"
    )
    if (-not [string]::IsNullOrWhiteSpace($LslNamePattern)) {
        $arguments += @("--lsl-name-pattern", $LslNamePattern)
    }
    & $python @arguments
    Assert-EegleExit "confirmed live-config generation"
}

if (-not (Test-Path -LiteralPath $liveConfig -PathType Leaf)) {
    throw "No confirmed live config exists. Verify Collect's positional channel order, then rerun with -ConfirmCapContract and -LabRecorderPath."
}
Assert-EegleLiveConfig $liveConfig

$preflightId = New-EegleRunId "physical-preflight"
$preflightArguments = @(
    "-m", "eegle.pipelines.study1",
    "--config", $liveConfig,
    "--participant", $Participant,
    "--visit", "1",
    "--visit-id", $preflightId,
    "--task-mode", "psychopy",
    "--preflight-only",
    "--session-root", $resolvedDataRoot,
    "--lsl-wait", "10"
)
$preflightArguments += "--confirm-electrodes"
& $python @preflightArguments
Assert-EegleExit "full Neuracle/LSL/electrode/LabRecorder preflight"
Write-Host "Physical preflight complete. Review its printed report and electrode-quality path."
