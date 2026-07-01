"""Preflight checks for a reproducible EEG session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eegle.devices.lsl_eeg import probe_eeg_stream
from eegle.hardware.capabilities import (
    check_command_entrypoints,
    check_display_ready,
    check_realtime_ready,
    check_training_ready,
    training_model_kinds_from_config,
)
from eegle.hardware.eeg_device import identify_eeg_device, matching_eeg_streams
from eegle.hardware.os_support import check_os_support
from eegle.hardware.system import CheckResult, check_packages, check_platform, check_python
from eegle.lsl import resolve_streams

REQUIRED_PACKAGES = ["numpy", "scipy", "pandas", "matplotlib", "mne", "pylsl"]
OPTIONAL_PACKAGES = ["psychopy", "specparam", "sklearn", "joblib", "pyriemann", "torch", "onnxruntime"]


def run_preflight(config: dict[str, Any], lsl_wait: float = 1.0, require_eeg: bool | None = None) -> list[CheckResult]:
    runtime = config.get("runtime", {})
    hardware = config.get("hardware", {})
    computer = hardware.get("computer", {})
    eeg = hardware.get("eeg", {})
    required_for_run = bool(eeg.get("required_for_run", False)) if require_eeg is None else require_eeg

    python_check = check_python(runtime.get("python"))
    if bool(runtime.get("require_configured_python", False)) and python_check.status != "ok":
        python_check = CheckResult("python", "fail", python_check.detail, python_check.data)
    checks: list[CheckResult] = [
        check_os_support(),
        check_platform(
            expected_os=computer.get("expected_os"),
            expected_machine=computer.get("expected_machine"),
        ),
        python_check,
        check_command_entrypoints(),
    ]
    checks.extend(check_packages(REQUIRED_PACKAGES, OPTIONAL_PACKAGES))
    checks.append(check_display_ready(config))

    streams, error = resolve_streams(wait_time=lsl_wait)
    if error:
        checks.append(CheckResult("lsl", "warn", error))
        device_check = identify_eeg_device([], eeg)
        checks.append(device_check)
        checks.append(check_realtime_ready(config, [], error, device_check, require_eeg=required_for_run))
        checks.append(check_training_ready(training_model_kinds_from_config(config)))
        return checks

    stream_dicts = [stream.as_dict() for stream in streams]
    checks.append(CheckResult("lsl", "ok", f"found {len(streams)} streams", {"streams": stream_dicts}))
    device_check = identify_eeg_device(stream_dicts, eeg)
    checks.append(device_check)
    checks.append(check_realtime_ready(config, stream_dicts, None, device_check, require_eeg=required_for_run))
    checks.append(check_training_ready(training_model_kinds_from_config(config)))

    eeg_matches = matching_eeg_streams(stream_dicts, eeg)
    device_check_name = _device_stream_check_name(eeg)
    device_label = _device_stream_label(eeg)
    if eeg_matches:
        detail = ", ".join(f"{stream['name']} ({stream['channel_count']} ch)" for stream in eeg_matches)
        checks.append(CheckResult(device_check_name, "ok", detail, {"matches": eeg_matches}))
        probe = probe_eeg_stream(
            eeg,
            seconds=float(eeg.get("sample_probe_seconds", 2.0)),
            timeout=float(eeg.get("stream_timeout_seconds", 5.0)),
        )
        if probe.get("status") == "ok":
            checks.append(CheckResult("eeg_sample_probe", "ok", f"read {probe.get('sample_count')} samples", probe))
        elif required_for_run:
            checks.append(CheckResult("eeg_sample_probe", "fail", str(probe.get("error", "no samples read")), probe))
        else:
            checks.append(CheckResult("eeg_sample_probe", "warn", str(probe.get("error", "no samples read")), probe))
    else:
        status = "fail" if required_for_run else "warn"
        checks.append(CheckResult(device_check_name, status, f"no {device_label} EEG LSL stream matched", {"streams": stream_dicts}))
    return checks


def _device_stream_check_name(eeg_config: dict[str, Any]) -> str:
    family = str(eeg_config.get("family") or "").strip().lower()
    if family == "enobio":
        return "enobio_lsl"
    if family == "neuracle":
        return "neuracle_lsl"
    return "eeg_lsl"


def _device_stream_label(eeg_config: dict[str, Any]) -> str:
    family = str(eeg_config.get("family") or "").strip()
    if family.lower() == "enobio":
        return "Enobio/NIC2"
    return family or "configured"


def write_preflight_report(results: list[CheckResult], path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump([result.__dict__ for result in results], handle, indent=2, sort_keys=True)
        handle.write("\n")
