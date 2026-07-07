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
