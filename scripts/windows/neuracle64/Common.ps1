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

function Update-EegleGeneratedLiveConfigs([string] $Python, [string] $LiveConfig) {
    Assert-EegleLiveConfig $LiveConfig
    $generator = Join-Path $script:EegleRepoRoot "scripts\prepare_neuracle64_windows_config.py"
    $baseConfig = Join-Path $script:EegleRepoRoot "configs\study1_neuracle64.json"
    $arguments = @(
        $generator,
        "--base-config", $baseConfig,
        "--display-output", (Get-EegleConfigPath "display"),
        "--live-output", (Get-EegleConfigPath "live"),
        "--live-task-output", (Get-EegleConfigPath "live-task"),
        "--refresh-confirmed-config", $LiveConfig
    )
    & $Python @arguments
    Assert-EegleExit "confirmed live-config protocol refresh"
    Assert-EegleLiveConfig (Get-EegleConfigPath "live")
}

function Assert-EegleExit([string] $Operation, [int] $ExitCode = $LASTEXITCODE) {
    if ($ExitCode -ne 0) {
        throw "$Operation stopped without a successful completion (exit code $ExitCode). Read the EEGle failure_detail and next_action printed immediately above; retained raw/session files are not overwritten."
    }
}

function New-EegleStudyOutcomePath([string] $DataRoot) {
    $outcomeDirectory = Join-Path $DataRoot "operator_outcomes"
    New-Item -ItemType Directory -Force -Path $outcomeDirectory | Out-Null
    $fileName = "study1-$(Get-Date -Format 'yyyyMMdd-HHmmss')-pid$PID.json"
    return (Join-Path $outcomeDirectory $fileName)
}

function Resolve-EegleStudyExit([int] $NativeExitCode, [string] $OutcomePath) {
    if (-not (Test-Path -LiteralPath $OutcomePath -PathType Leaf)) {
        Write-Warning "Study 1 did not produce its final outcome file. Expected: $OutcomePath"
        if ($NativeExitCode -ne 0) {
            return $NativeExitCode
        }
        return 1
    }
    try {
        $outcome = Get-Content -LiteralPath $OutcomePath -Raw | ConvertFrom-Json
        $statusProperty = $outcome.PSObject.Properties["status"]
        $exitProperty = $outcome.PSObject.Properties["process_exit_code"]
        if ($null -eq $statusProperty -or $null -eq $exitProperty) {
            throw "outcome is missing status or process_exit_code"
        }
        $reportedStatus = [string] $statusProperty.Value
        $reportedExitCode = [int] $exitProperty.Value
    }
    catch {
        Write-Warning "Study 1 outcome file is unreadable or incomplete. File: $OutcomePath. Error: $_"
        if ($NativeExitCode -ne 0) {
            return $NativeExitCode
        }
        return 1
    }
    if ($NativeExitCode -ne 0) {
        if ($reportedStatus -eq "completed" -and $reportedExitCode -eq 0) {
            Write-Warning "Python returned native exit code $NativeExitCode despite a completed durable Study 1 outcome. The contradiction is not masked; inspect the terminal and outcome file: $OutcomePath"
        }
        return $NativeExitCode
    }
    if ($reportedStatus -eq "completed" -and $reportedExitCode -eq 0) {
        return 0
    }
    Write-Warning "Python returned native exit code 0 but the durable Study 1 outcome reports status '$reportedStatus' and exit code $reportedExitCode; treating the run as incomplete. File: $OutcomePath"
    return 1
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
