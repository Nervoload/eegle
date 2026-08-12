"""Build local Windows display/live configs without weakening the checked-in live gate."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, Sequence

from eegle.config import load_config
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
    if len(SIMULATED_NEURACLE64_CHANNELS) != 64:
        raise RuntimeError("the candidate Neuracle W64 order must contain exactly 64 unique positions")
    if len(set(SIMULATED_NEURACLE64_CHANNELS)) != 64:
        raise RuntimeError("the candidate Neuracle W64 order contains duplicate positions")
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
            "mapping_source": "operator_confirmed_user_supplied_neuracle_w64_position_order",
            "mapping_version": 2,
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
        "contract": "Neuracle W64 candidate positional order supplied for this project",
        "channel_count": 64,
        "sample_rate_hz": 1000,
        "reference": reference,
        "ground": ground,
        "eog_allocation": eog_allocation,
        "warning": (
            "Use only after the operator verifies that Collect's 64 LSL values follow this exact "
            "physical position order. Generic Ch1..Ch64 metadata cannot prove the mapping."
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
        "--confirm-cap-contract",
        action="store_true",
        help="Attest that Collect's 64 values use the supplied W64 positional order",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.confirm_cap_contract and (not args.live_output or not args.live_task_output):
        raise SystemExit("--confirm-cap-contract requires --live-output and --live-task-output")
    base = load_config(args.base_config)
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
