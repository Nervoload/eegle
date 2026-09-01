[CmdletBinding(PositionalBinding = $false)]
param(
    [string] $Participant = "",
    [string] $RunRoot = "",
    [string] $DataRoot = "",
    [ValidateSet("Quick", "Comprehensive")]
    [string] $Mode = "Quick",
    [ValidateRange(1, 2)]
    [Nullable[int]] $Visit = $null,
    [string] $BackupRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common.ps1")

if ([string]::IsNullOrWhiteSpace($Participant) -eq [string]::IsNullOrWhiteSpace($RunRoot)) {
    throw "Supply exactly one of -Participant or -RunRoot."
}
if (-not [string]::IsNullOrWhiteSpace($BackupRoot) -and $Mode -ne "Comprehensive") {
    throw "-BackupRoot requires -Mode Comprehensive."
}

$python = Get-EeglePython
$config = Join-Path $script:EegleRepoRoot "configs\study1_neuracle64.json"
$arguments = @(
    "-m", "eegle.pipelines.study1_validation",
    "--config", $config,
    "--mode", $Mode.ToLowerInvariant()
)
if (-not [string]::IsNullOrWhiteSpace($Participant)) {
    if (-not [string]::IsNullOrWhiteSpace($DataRoot)) {
        $resolvedDataRoot = [IO.Path]::GetFullPath($DataRoot)
    }
    elseif (-not [string]::IsNullOrWhiteSpace($env:EEGLE_SESSION_ROOT)) {
        $resolvedDataRoot = [IO.Path]::GetFullPath($env:EEGLE_SESSION_ROOT)
    }
    elseif (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $resolvedDataRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "EEGle\data"))
    }
    else {
        throw "Pass -DataRoot or set EEGLE_SESSION_ROOT. LOCALAPPDATA is unavailable."
    }
    $arguments += @("--participant", $Participant, "--session-root", $resolvedDataRoot)
    if ($null -ne $Visit) {
        $arguments += @("--visit", [string] $Visit)
    }
}
else {
    $arguments += @("--target", $RunRoot)
}
if (-not [string]::IsNullOrWhiteSpace($BackupRoot)) {
    $arguments += @("--backup-root", $BackupRoot)
}

& $python @arguments
$exitCode = $LASTEXITCODE
Write-Host "Study 1 validation exit code: $exitCode"
if ($exitCode -ne 0) {
    Write-Host "Study 1 validation reported unreadable core data or an internal error." -ForegroundColor Red
}
exit $exitCode
