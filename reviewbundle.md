$EegleData = Join-Path $env:LOCALAPPDATA "EEGle\data"

.\scripts\windows\neuracle64\00-Setup.ps1 `
  -LabRecorderPath "C:\Tools\LabRecorder\LabRecorder.exe" `
  -DataRoot $EegleData

.\scripts\windows\neuracle64\01-DryRun-Task.ps1 -Trials 10 -DataRoot $EegleData

.\scripts\windows\neuracle64\02-Test-NeuracleLsl.ps1 `
  -DiscoverOnly -DataRoot $EegleData


.\scripts\windows\neuracle64\02-Test-NeuracleLsl.ps1 `
  -ConfirmCapContract `
  -ConfirmElectrodes `
  -LabRecorderPath "C:\Tools\LabRecorder\LabRecorder.exe" `
  -DataRoot $EegleData

.\scripts\windows\neuracle64\03-Run-EEGTaskTest.ps1 `
  -Trials 20 `
  -ConfirmElectrodes `
  -DataRoot $EegleData