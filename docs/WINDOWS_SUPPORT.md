# Windows Support

EEGle is designed to run from the same Python package on macOS, Windows, and
Linux. Windows support is centered on installed console scripts, PowerShell
commands, module launches, and explicit hardware checks.

## Supported Surface

- Package import and public APIs: OS-agnostic Python.
- CLI commands: installed `eegle`, `alpha8`, `inhibition8`, `classify8`, and
  `attention8` scripts, or `py -3.10 -m eegle.cli ...`.
- Worker processes: launched with the current Python executable, `-m`, and
  `shell=False`.
- Session output: created through `pathlib`, with relative paths documented for
  both POSIX and Windows.
- Runtime cache: `.runtime` contains Matplotlib, PsychoPy, and LSL config; on
  Windows, `USERPROFILE`, `APPDATA`, and `LOCALAPPDATA` are redirected for the
  process.

## Windows-Specific Operator Checks

- Use 64-bit CPython 3.10 on Windows x64.
- Install runtime and analysis extras for PsychoPy plus report generation:

  ```powershell
  py -3.10 -m venv .venv
  .venv\Scripts\Activate.ps1
  python -m pip install --upgrade pip
  python -m pip install -e ".[runtime,analysis]"
  ```

- Do not use `constraints/macos-python310.txt` on Windows.
- Allow NIC2 and Python through Windows Firewall when LSL discovery is blocked.
- Validate device streams with `eegle check-setup --require-eeg` before
  collecting data.
- Treat Windows on ARM as unvalidated until compatible LSL and device-driver
  binaries are confirmed.

## Portable Commands

Prefer installed commands:

```powershell
eegle check-setup --allow-missing-eeg
eegle run-forward --config configs\default_experiment.json --task pvt --task-mode dry-run --skip-eeg --allow-missing-eeg
alpha8 full --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2
inhibition8 full --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2
attention8 collect --task-mode dry-run --skip-eeg --allow-missing-eeg --trials 2
```

Use module form when console scripts are not on `PATH`:

```powershell
py -3.10 -m eegle.cli --help
py -3.10 -m eegle.pipelines.classify8 --help
py -3.10 -m eegle.pipelines.attention8 --help
```

Makefile targets and repository-root `./alpha8` wrappers are POSIX conveniences,
not the Windows operator path.

## Attention8 Dry-Electrode Pilot Suite

Install the runtime plus model-training extras, then verify the dry-electrode
LSL stream before collecting data:

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[runtime,ml]"
eegle check-setup --config configs\forward_attention_lapse_go_nogo8.json --require-eeg --lsl-wait 5
```

Generate the three-phase suite configs and PowerShell commands:

```powershell
attention8 pilot-suite --participant sub-001 --model-dir <existing-or-calibrated-attention8-model-dir> --write-configs
```

If Windows Controlled Folder Access, enterprise policy, OneDrive redirection,
or antivirus rules block writes under `Documents\Codespaces\eegle\data`, choose
an approved user-writable data root and pass it explicitly:

```powershell
$EegleData = Join-Path $env:LOCALAPPDATA "EEGle\data"
$env:EEGLE_SESSION_ROOT = $EegleData
attention8 pilot-suite --participant sub-001 --session-root $EegleData --model-dir <existing-or-calibrated-attention8-model-dir> --write-configs
attention8 online --config <phase-config> --participant sub-001-postcal --model-dir <model-dir> --session-root $EegleData
```

This does not bypass Windows security policy. It keeps the same session layout
and worker subprocesses, but points generated session data at a location the
current user is allowed to write. `EEGLE_SESSION_ROOT` is honored by all new
session creation, so setting it once keeps generated phase configs, calibration
runs, online runs, and child worker paths on the approved data root. Regenerate
pilot-suite configs after setting the variable if an older config still points
at `Documents\Codespaces\eegle\data`. `attention8 collect` and
`attention8 online` also probe the selected root before realtime startup so an
access denial is reported before the task window begins.

The emitted phases are:

- `smoke`: 24 trials, no resting or closed-eyes baseline, loads the chosen model
  directory, and checks that live predictions are written.
- `calibrate` plus `post_calibration_online`: collects subject-specific data,
  trains/stores `models\attention8`, then runs a longer online task with
  adaptation state logging.
- `challenge_100`: 100 trials with deliberate-inattention cue windows, default
  cue trials `20,40,60,80`.

For training, point `attention8 train --session-dir` at the calibration session,
not the post-calibration online session. If `epochs.npz` is missing but raw EEG,
task events, and parameters are present, `attention8 train` will now try to
export epochs first. When a realtime marker log is sparse, offline export falls
back to the fuller task stimulus manifest and records epoch rejection counts in
`realtime\epochs\manifest.json`.

When salvaging a session collected before this fix, explicitly use that
session's parameters so the attention8 pre-stimulus epoch window is preserved:

```powershell
eegle extract-epochs --config "$Cal\parameters.json" --session-dir $Cal --source stimulus_manifest
attention8 train --session-dir $Cal --support-trials 50
```

Online adaptation writes session artifacts such as
`realtime\adaptation_updates.jsonl` and `realtime\adaptation_state\`; it does
not mutate the source model bundle in place during the task.
