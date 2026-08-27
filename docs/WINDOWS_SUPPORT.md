# Windows Support

EEGle is designed to run from the same Python package on macOS, Windows, and
Linux. Windows support is centered on installed console scripts, PowerShell
commands, module launches, and explicit hardware checks.

## Supported Surface

- Package import and public APIs: OS-agnostic Python.
- CLI commands: installed `eegle`, `alpha8`, `inhibition8`, `classify8`,
  `attention8`, `dsart8`, `dsart32`, and `study1` scripts, or module forms.
- Guarded Study 1 launchers: `FullTest` and `FullRun` after
  `00-Setup.ps1 -AddCommandsToUserPath`, with `FullTest.ps1` and `FullRun.ps1`
  retained as repository-relative forms.
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
dsart8 --participant rehearsal --visit-id rehearsal --task-mode dry-run --skip-eeg --allow-missing-eeg --break-seconds 0
```

Use module form when console scripts are not on `PATH`:

```powershell
py -3.10 -m eegle.cli --help
py -3.10 -m eegle.pipelines.classify8 --help
py -3.10 -m eegle.pipelines.attention8 --help
py -3.10 -m eegle.pipelines.dsart_recording --help
```

Makefile targets and repository-root `./alpha8` wrappers are POSIX conveniences,
not the Windows operator path.

## Neuracle W64 / Collect Test Path

Use `docs/NEURACLE64_WINDOWS_TEST.md` for the guarded Windows x64 operator
sequence: setup, a visible 10-trial no-EEG run, Collect/LSL discovery, physical
cap-contract confirmation, a 10-20 trial XDF test, and the complete short
baseline/practice/30-trial test. The supplied PowerShell scripts live under
`scripts\windows\neuracle64` and call the virtual environment directly.
When discovery reports zero streams, `05-Diagnose-Lsl.ps1` performs a
cross-process pylsl loopback and compares EEGle's explicit LSL configuration
with normal liblsl configuration discovery before any stream-name matching.

## DSART Recording Suites on Restricted Desktops

Do not record a DSART visit below the checkout when enterprise policy blocks
writes under `Documents`. Select one approved root for the parent visit and all
baseline/DSART child sessions:

```powershell
$EegleData = Join-Path $env:LOCALAPPDATA "EEGle\data"
$env:EEGLE_SESSION_ROOT = $EegleData
$env:EEGLE_RUNTIME_CACHE_ROOT = "$env:LOCALAPPDATA\EEGle\runtime"
dsart8 --participant sub-001 --visit-id visit-20260716 --operator operator-id --session-root $EegleData
```

`--session-root` has highest priority; `EEGLE_SESSION_ROOT` is next; the recipe
config is the fallback. `--output-root` remains a compatibility alias. The
suite resolves and persists this path before the PsychoPy runtime redirects
`LOCALAPPDATA` for dependency caches, so parent manifests, preflights,
baselines, isolated worker artifacts, and both DSART sessions stay together.
Use the same root and identity when resuming.

In PowerShell Constrained Language Mode, avoid shell-side JSON conversion and
static .NET calls. Simple environment assignments are sufficient:

```powershell
$env:EEGLE_SESSION_ROOT = "$env:LOCALAPPDATA\EEGle\data"
$env:EEGLE_RUNTIME_CACHE_ROOT = "$env:LOCALAPPDATA\EEGle\runtime"
```

Relative runtime-cache settings then resolve below the approved cache root.
Commands may be launched directly through `.venv\Scripts\*.exe` without
activating the virtual environment.

The selected root is write-probed before acquisition. Atomic suite reports use
unique temporary files and retry short-lived Windows access/sharing denials;
ongoing policy denials still fail with the exact root to replace. No automatic
copy is made back into the repository. See `docs/DSART_RECORDING.md` for the
full visit lifecycle and artifact contract.

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

An error that specifically shows `*.status.json.tmp -> *.status.json` after raw
EEG or report files were already written is different: the session root is
writable, but Windows or endpoint-protection software briefly locked a worker
status file during its atomic replacement. Current status writers use unique
temporary files and retry transient Windows sharing/access denials. The absolute
path in that exception is diagnostic output; changing the same file reference
to a relative path would not change which Windows file is opened.

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
