Yes. Create a compact review bundle first. It will contain everything needed to assess calibration yield, training metrics, offline comparisons, online predictions, latency, process health, and replay—without initially sharing raw EEG or model binaries.

You do not need to move the repository. The export will remain under its ignored `data\review_exports` directory.

### 1. Locate the calibration and online sessions

Run from the EEGle repository in Windows PowerShell:

```powershell
$Repo = (Get-Location).Path

Get-ChildItem "$Repo\data\participants" -Recurse -Directory |
    Where-Object { $_.Name -like "run-*" } |
    Sort-Object LastWriteTime |
    Select-Object LastWriteTime, FullName
```

Copy the two appropriate paths:

```powershell
$Cal = "C:\...\run-YYYYMMDDTHHMMSS"
$Online = "C:\...\run-YYYYMMDDTHHMMSS"

Test-Path "$Cal\parameters.json"
Test-Path "$Online\parameters.json"
```

Both should print `True`.

### 2. Build the review bundle

Paste this into the same PowerShell window:

```powershell
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Bundle = Join-Path $Repo "data\review_exports\attention8-review-$Stamp"
$Zip = "$Bundle.zip"

New-Item -ItemType Directory -Force -Path $Bundle | Out-Null

function Copy-One {
    param(
        [string]$Source,
        [string]$Destination
    )

    if (Test-Path -LiteralPath $Source) {
        New-Item -ItemType Directory -Force `
            -Path (Split-Path $Destination -Parent) | Out-Null

        Copy-Item -LiteralPath $Source `
            -Destination $Destination `
            -Force
    }
}

function Copy-Tree {
    param(
        [string]$Source,
        [string]$Destination
    )

    if (Test-Path -LiteralPath $Source) {
        New-Item -ItemType Directory -Force `
            -Path (Split-Path $Destination -Parent) | Out-Null

        Copy-Item -LiteralPath $Source `
            -Destination $Destination `
            -Recurse `
            -Force
    }
}

function Read-Json {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    try {
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    catch {
        return [pscustomobject]@{
            status = "unreadable"
            file = $Path
            error = $_.Exception.Message
        }
    }
}

function Read-Statuses {
    param([string]$Session)

    $Results = @()
    $StatusRoot = Join-Path $Session "logs\processes"

    if (Test-Path -LiteralPath $StatusRoot) {
        foreach ($File in Get-ChildItem $StatusRoot -Filter "*.status.json" -File) {
            $Results += [pscustomobject]@{
                file = $File.Name
                payload = Read-Json $File.FullName
            }
        }
    }

    return $Results
}

$RootFiles = @(
    "parameters.json",
    "manifest.json",
    "protocol.json",
    "session_summary.json",
    "triggers.txt"
)

$RealtimeFiles = @(
    "model_predictions.jsonl",
    "adaptation_updates.jsonl",
    "markers.jsonl",
    "decisions.jsonl",
    "feedback.jsonl",
    "event_features.jsonl",
    "engine_metadata.json",
    "epochs\manifest.json",
    "epochs\epochs.jsonl"
)

$Phases = @(
    [pscustomobject]@{ Name = "calibration"; Root = $Cal },
    [pscustomobject]@{ Name = "online"; Root = $Online }
)

foreach ($Phase in $Phases) {
    $Destination = Join-Path $Bundle $Phase.Name

    foreach ($Relative in $RootFiles) {
        Copy-One `
            (Join-Path $Phase.Root $Relative) `
            (Join-Path $Destination $Relative)
    }

    Copy-One `
        (Join-Path $Phase.Root "raw\eeg_metadata.json") `
        (Join-Path $Destination "raw\eeg_metadata.json")

    Copy-Tree `
        (Join-Path $Phase.Root "events") `
        (Join-Path $Destination "events")

    Copy-Tree `
        (Join-Path $Phase.Root "logs") `
        (Join-Path $Destination "logs")

    Copy-Tree `
        (Join-Path $Phase.Root "reports") `
        (Join-Path $Destination "reports")

    foreach ($Relative in $RealtimeFiles) {
        Copy-One `
            (Join-Path $Phase.Root "realtime\$Relative") `
            (Join-Path $Destination "realtime\$Relative")
    }
}

# Copy training-bundle metadata, but not fitted model binaries.
$ModelSources = @(
    [pscustomobject]@{
        Source = Join-Path $Cal "models\attention8"
        Destination = Join-Path $Bundle "calibration\models\attention8"
    },
    [pscustomobject]@{
        Source = Join-Path $Online "realtime\models"
        Destination = Join-Path $Bundle "online\realtime\models"
    }
)

foreach ($ModelSource in $ModelSources) {
    if (Test-Path -LiteralPath $ModelSource.Source) {
        foreach ($ModelDir in Get-ChildItem $ModelSource.Source -Directory) {
            foreach ($Name in @(
                "manifest.json",
                "metrics.json",
                "protocol.json",
                "training_summary.json"
            )) {
                Copy-One `
                    (Join-Path $ModelDir.FullName $Name) `
                    (Join-Path $ModelSource.Destination "$($ModelDir.Name)\$Name")
            }
        }
    }
}

# Capture the software version used.
(& git -C $Repo rev-parse HEAD 2>$null | Out-String).Trim() |
    Set-Content "$Bundle\git_commit.txt" -Encoding UTF8

(& git -C $Repo status --short 2>$null | Out-String).Trim() |
    Set-Content "$Bundle\git_status.txt" -Encoding UTF8

(& python --version 2>&1 | Out-String).Trim() |
    Set-Content "$Bundle\python_version.txt" -Encoding UTF8

& python -m pip list --format=json 2>$null |
    Set-Content "$Bundle\python_packages.json" -Encoding UTF8

# Gather model manifests into the top-level summary.
$ModelManifests = @()

Get-ChildItem $Bundle -Recurse -Filter "manifest.json" -File |
    Where-Object { $_.FullName -match "\\models\\" } |
    ForEach-Object {
        $Relative = $_.FullName.Substring($Bundle.Length).TrimStart("\")

        $ModelManifests += [pscustomobject]@{
            path = $Relative
            payload = Read-Json $_.FullName
        }
    }

$ReviewSummary = [ordered]@{
    schema_version = 1
    created_utc = [DateTime]::UtcNow.ToString("o")

    calibration = [ordered]@{
        session = Split-Path $Cal -Leaf
        session_summary = Read-Json "$Cal\session_summary.json"
        epoch_manifest = Read-Json "$Cal\realtime\epochs\manifest.json"
        process_statuses = Read-Statuses $Cal
        offline_comparison = Read-Json "$Cal\reports\classification\attention8_offline_comparison\summary.json"
    }

    online = [ordered]@{
        session = Split-Path $Online -Leaf
        session_summary = Read-Json "$Online\session_summary.json"
        process_statuses = Read-Statuses $Online
        evaluation = Read-Json "$Online\reports\classification\metrics.json"
        classifier_replay = Read-Json "$Online\reports\classification\replay_summary.json"
        realtime_feature_replay = Read-Json "$Online\reports\realtime_features\replay_summary.json"
        offline_comparison = Read-Json "$Online\reports\classification\attention8_offline_comparison\summary.json"
    }

    model_manifests = $ModelManifests
}

$ReviewSummary |
    ConvertTo-Json -Depth 100 |
    Set-Content "$Bundle\review_summary.json" -Encoding UTF8

# Create an inventory with hashes.
$Inventory = foreach ($File in Get-ChildItem $Bundle -Recurse -File) {
    [pscustomobject]@{
        relative_path = $File.FullName.Substring($Bundle.Length).TrimStart("\")
        bytes = $File.Length
        sha256 = (Get-FileHash $File.FullName -Algorithm SHA256).Hash
    }
}

$Inventory |
    Export-Csv "$Bundle\file_inventory.csv" `
        -NoTypeInformation `
        -Encoding UTF8

Compress-Archive `
    -Path $Bundle `
    -DestinationPath $Zip `
    -CompressionLevel Optimal

Write-Host ""
Write-Host "Review folder: $Bundle"
Write-Host "Transfer archive: $Zip"
```

### 3. Check for identifying information

Session manifests, EEG metadata, preflight reports, and logs may contain participant IDs, Windows usernames, absolute paths, device source identifiers, or machine information.

Inspect likely matches before uploading:

```powershell
$TextFiles = Get-ChildItem $Bundle -Recurse -File |
    Where-Object {
        $_.Extension -in @(".json", ".jsonl", ".csv", ".txt", ".log", ".html")
    }

$TextFiles |
    Select-String -Pattern @(
        $env:USERNAME,
        "C:\\Users\\",
        "participant_id",
        "source_id"
    ) |
    Select-Object Path, LineNumber, Line
```

Do not commit or push the review bundle to GitHub.

### 4. Get it to me

Use an employer-approved method to transfer the ZIP from Windows to your Mac. Then:

- Extract it under the Mac repository’s `data/review_exports/` directory and tell me its absolute path; this is the easiest option for this Codex task.
- Or attach the source files directly. ChatGPT web supports attaching source files for review; if the interface rejects the ZIP, attach `review_summary.json`, `file_inventory.csv`, and the relevant JSON/CSV report files individually. [OpenAI: Work with files](https://learn.chatgpt.com/docs/artifacts-viewer.md)

Start by giving me the compact bundle. From it I can review:

- Calibration completion and usable/rejected epoch counts
- Label distribution and support/query split
- Every trained model’s validation and calibrated threshold
- Offline comparison across log-reg, Riemann, foundation-head, and prototype
- Online AUPRC, ROC AUC, Brier score, coverage, false alarms, and confusion matrices
- Primary versus shadow model performance
- Prediction and processing latency
- Worker status, warnings, permission errors, and shutdown health
- Replay parity and adaptation status

Raw files such as `raw\eeg.csv`, `epochs.npz`, `engine_input.bin`, `model.joblib`, and `.pt` files are intentionally excluded. Keep them on the Windows machine for now. I’ll request only the necessary ones if the compact review indicates a signal-quality, timebase, preprocessing, or deterministic-replay issue.

When sharing the bundle, use this prompt:

> Review this Attention8 experiment end-to-end. Verify calibration integrity, epoch yield and rejection reasons, training and threshold calibration, offline model comparison, online performance and coverage, latency, replay parity, adaptation behavior, process health, and any evidence of leakage or invalid evaluation. Separate confirmed results from warnings and recommend whether this experiment is scientifically usable.