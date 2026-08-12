"""Build local Windows display/live configs without weakening the checked-in live gate."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Sequence

from eegle.config import load_config
from eegle.hardware.neuracle import (
    NEURACLE_W64_AUX_CHANNELS,
    NEURACLE_W64_LSL_CHANNEL_COUNT,
    NEURACLE_W64_LSL_CHANNEL_TYPES,
    NEURACLE_W64_PHYSIOLOGICAL_CHANNELS,
    NEURACLE_W64_TRIGGER_STATUS_CHANNEL,
)
from eegle.pipelines.study1 import SIMULATED_NEURACLE64_CHANNELS
from eegle.protocols.study1 import validate_study1_config


DEFAULT_BASE_CONFIG = Path("configs/study1_neuracle64.json")


def build_configs(
    base_config: dict[str, Any],
    *,
    labrecorder_executable: str | None = None,
    reference: str = "CPz",
    ground: str = "AFz",
    eog_allocation: str = "ECG, HEOR, HEOL, VEOU, VEOL",
    extra_lsl_name_patterns: Sequence[str] = (),
    confirm_cap_contract: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    """Return display-only, locked Study 1, and short live-task configs."""
    display = copy.deepcopy(base_config)
    display.setdefault("hardware", {}).setdefault("eeg", {})["required_for_run"] = False
    display_eeg = display["hardware"]["eeg"]
    display_patterns = [
        str(value).strip().lower()
        for value in display_eeg.get("lsl_name_patterns", [])
        if str(value).strip()
    ]
    for pattern in extra_lsl_name_patterns:
        normalized = str(pattern).strip().lower()
        if normalized and normalized not in display_patterns:
            display_patterns.append(normalized)
    display_eeg["lsl_name_patterns"] = display_patterns
    display.setdefault("hardware", {}).setdefault("markers", {})["required_for_realtime"] = False
    display.setdefault("processes", {}).setdefault("recorder", {})["enabled"] = False
    display["experiment"].setdefault("components", {})["eeg_recorder"] = "disabled"
    display.setdefault("tasks", {}).setdefault("dynamic_sart", {}).setdefault("practice", {})[
        "enabled"
    ] = False
    display["operator_test_profile"] = {
        "mode": "display_only",
        "physical_eeg_required": False,
        "participant_data": False,
    }

    if not confirm_cap_contract:
        return display, None, None
    if not str(labrecorder_executable or "").strip():
        raise ValueError("labrecorder_executable is required when confirming the live cap contract")
    if len(SIMULATED_NEURACLE64_CHANNELS) != NEURACLE_W64_LSL_CHANNEL_COUNT:
        raise RuntimeError("the observed Neuracle W64 LSL order must contain exactly 65 unique values")
    if len(set(SIMULATED_NEURACLE64_CHANNELS)) != NEURACLE_W64_LSL_CHANNEL_COUNT:
        raise RuntimeError("the observed Neuracle W64 LSL order contains duplicate labels")
    for label, value in (
        ("reference", reference),
        ("ground", ground),
        ("eog_allocation", eog_allocation),
    ):
        if not str(value).strip():
            raise ValueError(f"{label} must be nonempty for a live config")

    live = copy.deepcopy(base_config)
    eeg = live.setdefault("hardware", {}).setdefault("eeg", {})
    patterns = [str(value).strip().lower() for value in eeg.get("lsl_name_patterns", []) if str(value).strip()]
    for pattern in extra_lsl_name_patterns:
        normalized = str(pattern).strip().lower()
        if normalized and normalized not in patterns:
            patterns.append(normalized)
    eeg.update(
        {
            "expected_channel_names": list(SIMULATED_NEURACLE64_CHANNELS),
            "expected_channel_types": list(NEURACLE_W64_LSL_CHANNEL_TYPES),
            "electrode_channel_names": list(NEURACLE_W64_PHYSIOLOGICAL_CHANNELS),
            "analysis_excluded_channel_names": [
                *NEURACLE_W64_AUX_CHANNELS,
                NEURACLE_W64_TRIGGER_STATUS_CHANNEL,
            ],
            "quality_excluded_channel_names": [NEURACLE_W64_TRIGGER_STATUS_CHANNEL],
            "embedded_trigger_channel": {
                "index": 65,
                "name": NEURACLE_W64_TRIGGER_STATUS_CHANNEL,
                "role": "reserved_trigger_status",
                "observed_without_trigger": "empty",
            },
            "mapping_source": "operator_confirmed_neuracle_w64_65_value_lsl_order",
            "mapping_version": 3,
            "reference": f"operator-confirmed: {reference}",
            "ground": f"operator-confirmed: {ground}",
            "eog_allocation": f"operator-confirmed positional allocation: {eog_allocation}",
            "lsl_name_patterns": patterns,
            "required_for_run": True,
        }
    )
    live.setdefault("processes", {}).setdefault("recorder", {}).update(
        {
            "enabled": True,
            "backend": "labrecorder_xdf",
            "csv_mirror": True,
            "executable": str(labrecorder_executable),
        }
    )
    live["operator_confirmation"] = {
        "required_before_use": True,
        "confirmed_for_this_generated_config": True,
        "contract": "Neuracle W64 observed 65-value LSL transport order",
        "lsl_value_count": NEURACLE_W64_LSL_CHANNEL_COUNT,
        "physical_input_count": len(NEURACLE_W64_PHYSIOLOGICAL_CHANNELS),
        "embedded_trigger_status_index": 65,
        "sample_rate_hz": 1000,
        "reference": reference,
        "ground": ground,
        "eog_allocation": eog_allocation,
        "warning": (
            "Use only after the operator verifies that Collect's 65 LSL values follow this exact "
            "order: 64 physical inputs followed by the reserved trigger/status value. "
            "Generic Ch1..Ch65 metadata cannot prove the mapping."
        ),
    }
    issues = validate_study1_config(live)
    failures = [issue["detail"] for issue in issues if issue["status"] == "fail"]
    if failures:
        raise ValueError("generated live Study 1 config is invalid: " + "; ".join(failures))

    live_task = copy.deepcopy(live)
    live_task.setdefault("tasks", {}).setdefault("dynamic_sart", {}).setdefault("practice", {})[
        "enabled"
    ] = False
    live_task["operator_test_profile"] = {
        "mode": "short_live_task",
        "physical_eeg_required": True,
        "participant_data": False,
        "practice_enabled": False,
    }
    return display, live, live_task


def refresh_confirmed_configs(
    base_config: dict[str, Any],
    confirmed_config: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Rebuild generated configs while retaining the confirmed hardware facts.

    Runtime configs are deliberately generated artifacts.  When the checked-in
    protocol changes, carrying the entire old runtime config forward would also
    carry stale timing or display parameters.  Only operator/hardware facts are
    migrated into a fresh copy of the current base protocol.
    """

    confirmation = dict(confirmed_config.get("operator_confirmation") or {})
    if not bool(confirmation.get("confirmed_for_this_generated_config", False)):
        raise ValueError("existing live config has no confirmed Neuracle cap contract")
    recorder = dict(confirmed_config.get("processes", {}).get("recorder", {}) or {})
    executable = str(recorder.get("executable") or "").strip()
    if not executable:
        raise ValueError("existing live config has no LabRecorder executable")
    required_confirmation = {
        name: str(confirmation.get(name) or "").strip()
        for name in ("reference", "ground", "eog_allocation")
    }
    missing = [name for name, value in required_confirmation.items() if not value]
    if missing:
        raise ValueError(
            "existing live config is missing confirmed hardware fields: " + ", ".join(missing)
        )
    existing_eeg = dict(confirmed_config.get("hardware", {}).get("eeg", {}) or {})
    existing_patterns = [
        str(value).strip()
        for value in existing_eeg.get("lsl_name_patterns", [])
        if str(value).strip()
    ]
    display, live, live_task = build_configs(
        base_config,
        labrecorder_executable=executable,
        reference=required_confirmation["reference"],
        ground=required_confirmation["ground"],
        eog_allocation=required_confirmation["eog_allocation"],
        extra_lsl_name_patterns=existing_patterns,
        confirm_cap_contract=True,
    )
    assert live is not None and live_task is not None
    return display, live, live_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate local Windows Neuracle64 operator-test configs",
    )
    parser.add_argument("--base-config", default=str(DEFAULT_BASE_CONFIG))
    parser.add_argument("--display-output", required=True)
    parser.add_argument("--live-output", default=None)
    parser.add_argument("--live-task-output", default=None)
    parser.add_argument("--labrecorder", default=None)
    parser.add_argument("--reference", default="CPz")
    parser.add_argument("--ground", default="AFz")
    parser.add_argument("--eog-allocation", default="ECG, HEOR, HEOL, VEOU, VEOL")
    parser.add_argument("--lsl-name-pattern", action="append", default=[])
    parser.add_argument(
        "--refresh-confirmed-config",
        default=None,
        help=(
            "Rebuild from the current base protocol while retaining hardware confirmation "
            "from an existing generated live config"
        ),
    )
    parser.add_argument(
        "--confirm-cap-contract",
        action="store_true",
        help="Attest that Collect's 65 values use the confirmed W64 transport order",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.refresh_confirmed_config and args.confirm_cap_contract:
        raise SystemExit("--refresh-confirmed-config cannot be combined with --confirm-cap-contract")
    if (args.confirm_cap_contract or args.refresh_confirmed_config) and (
        not args.live_output or not args.live_task_output
    ):
        raise SystemExit(
            "--confirm-cap-contract/--refresh-confirmed-config requires --live-output and --live-task-output"
        )
    base = load_config(args.base_config)
    if args.refresh_confirmed_config:
        display, live, live_task = refresh_confirmed_configs(
            base,
            load_config(args.refresh_confirmed_config),
        )
    else:
        display, live, live_task = build_configs(
            base,
            labrecorder_executable=args.labrecorder,
            reference=args.reference,
            ground=args.ground,
            eog_allocation=args.eog_allocation,
            extra_lsl_name_patterns=args.lsl_name_pattern,
            confirm_cap_contract=bool(args.confirm_cap_contract),
        )
    written = [_write_json(Path(args.display_output), display)]
    if live is not None and live_task is not None:
        written.extend(
            [
                _write_json(Path(args.live_output), live),
                _write_json(Path(args.live_task_output), live_task),
            ]
        )
    print(json.dumps({"written_configs": [str(path) for path in written]}, indent=2))
    return 0


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(target)
    return target


if __name__ == "__main__":
    raise SystemExit(main())
