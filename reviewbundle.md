Recreated it as a single Python script, avoiding PowerShell’s restricted-language features entirely:

[export_attention8_review.py](/Users/johnsurette/Documents/Codespace🪐/closedloop/scripts/export_attention8_review.py)

After syncing it to the Windows checkout, run from the repository:

```powershell
.\.venv\Scripts\python.exe .\scripts\export_attention8_review.py
```

Or, if the virtual environment is already activated:

```powershell
python .\scripts\export_attention8_review.py
```

The script now:

- Searches only real `data\participants\...\run-*` sessions.
- Cannot accidentally select a previous review export.
- Matches calibration to the online session using the recorded model path or bundle hashes.
- Includes adaptation-state snapshots.
- Creates the ZIP using Python, not `Compress-Archive`.
- Uses only Python’s standard library.
- Excludes raw EEG, epoch arrays, engine captures, and model binaries by default.

If automatic selection is incorrect, provide both paths on one line:

```powershell
python .\scripts\export_attention8_review.py --calibration-session "C:\...\calibration-run" --online-session "C:\...\online-run"
```

The output will be under:

```text
<EEGLE_SESSION_ROOT>\review_exports\attention8-review-<timestamp>\
<EEGLE_SESSION_ROOT>\review_exports\attention8-review-<timestamp>.zip
```

Verification passed:

- Focused end-to-end export test: passed
- ZIP integrity and contents: passed
- Prior-export exclusion: passed
- Calibration/online matching: passed
- Python compilation: passed

The old PowerShell collector was removed to prevent accidentally using the problematic version.