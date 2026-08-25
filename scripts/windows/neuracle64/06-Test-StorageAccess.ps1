[CmdletBinding(PositionalBinding = $false)]
param(
    [string] $DataRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "Common.ps1")

$python = Get-EeglePython
$resolvedDataRoot = Get-EegleDataRoot $DataRoot

Write-Host "Testing the exact EEGle recording root with parent and fresh child Python processes:"
Write-Host "  $resolvedDataRoot"
Write-Host "This creates and removes only a disposable .eegle_recording_storage_probe tree."

& $python -m eegle.storage_permissions --root $resolvedDataRoot
Assert-EegleExit "recording storage and task-worker permission probe"

Write-Host "Storage transition probe passed. Use this exact path as -DataRoot for the test run."
