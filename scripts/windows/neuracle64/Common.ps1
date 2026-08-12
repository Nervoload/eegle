Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:EegleRepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\.."))
$script:EegleRuntimeRoot = Join-Path $script:EegleRepoRoot ".runtime\windows-neuracle64"

function Get-EeglePython {
    $python = Join-Path $script:EegleRepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "EEGle virtual environment is missing. Run 00-Setup.ps1 first: $python"
    }
    return $python
}

function Get-EegleDataRoot([string] $RequestedRoot) {
    $configuredRoot = [Environment]::GetEnvironmentVariable("EEGLE_SESSION_ROOT", "Process")
    $localAppData = [Environment]::GetEnvironmentVariable("LOCALAPPDATA", "Process")
    if (-not [string]::IsNullOrWhiteSpace($RequestedRoot)) {
        $root = $RequestedRoot
    }
    elseif (-not [string]::IsNullOrWhiteSpace($configuredRoot)) {
        $root = $configuredRoot
    }
    elseif (-not [string]::IsNullOrWhiteSpace($localAppData)) {
        $root = Join-Path $localAppData "EEGle\data"
    }
    else {
        throw "Pass -DataRoot or set EEGLE_SESSION_ROOT. LOCALAPPDATA is unavailable."
    }
    $resolved = [IO.Path]::GetFullPath($root)
    New-Item -ItemType Directory -Force -Path $resolved | Out-Null
    return $resolved
}

function Get-EegleConfigPath([ValidateSet("display", "live", "live-task")] [string] $Kind) {
    switch ($Kind) {
        "display" { return (Join-Path $script:EegleRuntimeRoot "display-only.json") }
        "live" { return (Join-Path $script:EegleRuntimeRoot "study1-live.json") }
        "live-task" { return (Join-Path $script:EegleRuntimeRoot "short-live-task.json") }
    }
}

function Assert-EegleFile([string] $Path, [string] $Description) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description is missing: $Path"
    }
}

function Assert-EegleLiveConfig([string] $Path) {
    Assert-EegleFile $Path "Confirmed Neuracle live config"
    $config = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $confirmationProperty = $config.PSObject.Properties["operator_confirmation"]
    if ($null -eq $confirmationProperty) {
        throw "Live config has no operator cap-contract confirmation: $Path"
    }
    $confirmation = $confirmationProperty.Value
    if (-not $confirmation.confirmed_for_this_generated_config) {
        throw "Live config lacks the required operator cap-contract confirmation: $Path"
    }
    if (@($config.hardware.eeg.expected_channel_names).Count -ne 65) {
        throw "Live config must contain exactly 65 expected LSL values: $Path"
    }
    if (@($config.hardware.eeg.electrode_channel_names).Count -ne 64) {
        throw "Live config must contain exactly 64 physical input positions: $Path"
    }
    if ([string] $config.hardware.eeg.expected_channel_names[64] -ne "TRIGGER_STATUS") {
        throw "Live config value 65 must be the reserved TRIGGER_STATUS value: $Path"
    }
    foreach ($field in @("reference", "ground", "eog_allocation")) {
        $value = [string] $config.hardware.eeg.$field
        if ([string]::IsNullOrWhiteSpace($value) -or $value.StartsWith("pending")) {
            throw "Live config has an unconfirmed $field value: $Path"
        }
    }
    if ([string] $config.processes.recorder.backend -ne "labrecorder_xdf") {
        throw "Live config must use the managed labrecorder_xdf backend: $Path"
    }
    $labRecorder = [string] $config.processes.recorder.executable
    Assert-EegleFile $labRecorder "Configured LabRecorder executable"
}

function Assert-EegleExit([string] $Operation) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Operation failed with exit code $LASTEXITCODE"
    }
}

function New-EegleRunId([string] $Prefix) {
    return "$Prefix-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
}

function Find-EegleLabRecorder([string] $RequestedPath) {
    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        $resolved = [IO.Path]::GetFullPath($RequestedPath)
        Assert-EegleFile $resolved "LabRecorder executable"
        return $resolved
    }
    $command = Get-Command LabRecorder.exe -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return [string] $command.Source
    }
    $candidates = @(
        "C:\Program Files\LabRecorder\LabRecorder.exe",
        "C:\Program Files (x86)\LabRecorder\LabRecorder.exe",
        "C:\LSL\LabRecorder\LabRecorder.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}
