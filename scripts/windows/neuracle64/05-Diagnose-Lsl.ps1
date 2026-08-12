param(
    [string] $DataRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common.ps1")

$python = Get-EeglePython
$resolvedDataRoot = Get-EegleDataRoot $DataRoot
$env:EEGLE_SESSION_ROOT = $resolvedDataRoot
$checkRoot = Join-Path $resolvedDataRoot "system_checks\lsl-diagnostics"
New-Item -ItemType Directory -Force -Path $checkRoot | Out-Null
$runId = New-EegleRunId "lsl-diagnostic"
$configuredReport = Join-Path $checkRoot "$runId-eegle-config.json"
$normalSearchReport = Join-Path $checkRoot "$runId-normal-config-search.json"

$configuredRuntimeRoot = [Environment]::GetEnvironmentVariable("EEGLE_RUNTIME_CACHE_ROOT", "Process")
if ([string]::IsNullOrWhiteSpace($configuredRuntimeRoot)) {
    $configuredRuntimeRoot = Join-Path $resolvedDataRoot ".runtime"
}
$lslConfig = Join-Path $configuredRuntimeRoot "lsl_api.cfg"

Write-Host "Testing pylsl in two separate processes with EEGle's LSL configuration..."
if (Test-Path -LiteralPath $lslConfig -PathType Leaf) {
    & $python -m eegle.lsl_diagnostics `
        --label "eegle-config" `
        --lsl-config $lslConfig `
        --wait 6 `
        --output $configuredReport
    $configuredExit = $LASTEXITCODE
}
else {
    Write-Warning "EEGle LSL configuration was not found: $lslConfig"
    $configuredExit = -1
}

Write-Host "Testing again without an explicit LSLAPICFG override..."
& $python -m eegle.lsl_diagnostics `
    --label "normal-config-search" `
    --ignore-lsl-config `
    --wait 6 `
    --output $normalSearchReport
$normalSearchExit = $LASTEXITCODE

$configured = $null
if (Test-Path -LiteralPath $configuredReport -PathType Leaf) {
    $configured = Get-Content -LiteralPath $configuredReport -Raw | ConvertFrom-Json
}
$normalSearch = Get-Content -LiteralPath $normalSearchReport -Raw | ConvertFrom-Json

Write-Host ""
Write-Host "Diagnostic summary"
if ($null -ne $configured) {
    Write-Host "  EEGle config: loopback=$($configured.loopback.status); external streams=$($configured.stream_count); verdict=$($configured.verdict.code)"
}
Write-Host "  normal config search: loopback=$($normalSearch.loopback.status); external streams=$($normalSearch.stream_count); verdict=$($normalSearch.verdict.code)"
Write-Host "  EEGle LSL config: $lslConfig"
Write-Host "  Reports: $configuredReport"
Write-Host "           $normalSearchReport"

Write-Host ""
Write-Host "Relevant running processes"
$candidates = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -match "Collect|Neuracle|LabRecorder|EEG"
}
if (@($candidates).Count -eq 0) {
    Write-Host "  No process name matched Collect, Neuracle, LabRecorder, or EEG."
}
else {
    foreach ($process in $candidates) {
        try {
            $processPath = $process.Path
        }
        catch {
            $processPath = "<path unavailable>"
        }
        Write-Host "  $($process.ProcessName) pid=$($process.Id) path=$processPath"
    }
}

if ($null -ne $configured -and $configured.loopback.status -eq "fail" -and $normalSearch.loopback.status -eq "pass") {
    Write-Warning "The local LSL stack works with liblsl's normal config search but fails with EEGle's explicit LSL config. Back up the reported lsl_api.cfg, remove it, and rerun 00-Setup.ps1 to regenerate defaults."
}
elseif ($normalSearch.loopback.status -eq "fail") {
    Write-Warning "The cross-process pylsl loopback failed with liblsl's normal config search. Repair pylsl/Visual C++ runtime or Windows program/firewall permissions before testing Collect."
}
elseif ($normalSearch.stream_count -eq 0 -and ($null -eq $configured -or $configured.stream_count -eq 0)) {
    Write-Warning "Python LSL works across processes, but no external outlet is visible. Confirm that Collect has an explicit EEG LSL OUTLET enabled; live waveform display alone does not publish LSL. Open LabRecorder manually once and click Update as an independent visibility check."
}
elseif ($null -ne $configured -and $configured.stream_count -eq 0 -and $normalSearch.stream_count -gt 0) {
    Write-Warning "External streams are visible through liblsl's normal config search but hidden by EEGle's explicit LSL config. Back up/remove the reported lsl_api.cfg and regenerate it with 00-Setup.ps1."
}
else {
    Write-Host "At least one external stream is visible. Inspect the reports for its exact name, type, channel count, rate, source ID, host, and session ID."
}

if ($configuredExit -gt 0 -or $normalSearchExit -gt 0) {
    exit 1
}
