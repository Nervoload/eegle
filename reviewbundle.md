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

  cd C:\Users\Surettej\Desktop\code\eegle

$EegleData = Join-Path $env:LOCALAPPDATA "EEGle\data"
$env:EEGLE_SESSION_ROOT = $EegleData

.\.venv\Scripts\python.exe -m eegle.cli simulate-eeg `
    --name NeuracleDiagnostic `
    --channels 64 `
    --sample-rate 1000 `
    --duration 120


    cd C:\Users\Surettej\Desktop\code\eegle

$EegleData = Join-Path $env:LOCALAPPDATA "EEGle\data"

.\scripts\windows\neuracle64\02-Test-NeuracleLsl.ps1 `
    -DiscoverOnly `
    -LslNamePattern "NeuracleDiagnostic" `
    -DataRoot $EegleData