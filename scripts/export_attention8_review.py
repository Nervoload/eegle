#!/usr/bin/env python3
"""Create a portable Attention8 review bundle without PowerShell scripting.

The default bundle contains derived reports, event truth, logs, predictions,
epoch metadata, model metadata, and adaptation state. Potentially sensitive or
large reconstruction artifacts are excluded unless --include-forensic is set.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCRIPT_VERSION = 1
ROOT_FILES = (
    "parameters.json",
    "manifest.json",
    "protocol.json",
    "session_summary.json",
    "triggers.txt",
)
REALTIME_FILES = (
    "model_predictions.jsonl",
    "adaptation_updates.jsonl",
    "markers.jsonl",
    "decisions.jsonl",
    "feedback.jsonl",
    "event_features.jsonl",
    "engine_metadata.json",
    "epochs/manifest.json",
    "epochs/epochs.jsonl",
)
MODEL_METADATA_SUFFIXES = {
    ".csv",
    ".html",
    ".json",
    ".jsonl",
    ".md",
    ".png",
    ".sha256",
    ".svg",
    ".txt",
}
FORENSIC_FILES = (
    "raw/eeg.csv",
    "realtime/epochs/epochs.npz",
    "realtime/engine_input.bin",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export a review-safe Attention8 calibration and online session bundle. "
            "This script uses only the Python standard library."
        )
    )
    parser.add_argument(
        "--calibration-session",
        help="Calibration run directory. If omitted, infer it from the selected online model bundle.",
    )
    parser.add_argument(
        "--online-session",
        help="Online run directory. If omitted, use the newest real online run under participants/.",
    )
    parser.add_argument(
        "--session-root",
        help="EEGle data root. Defaults to EEGLE_SESSION_ROOT, then <repository>/data.",
    )
    parser.add_argument(
        "--output-dir",
        help="Export parent directory. Defaults to <session-root>/review_exports.",
    )
    parser.add_argument(
        "--include-forensic",
        action="store_true",
        help=(
            "Also include raw EEG, epochs.npz, engine_input.bin, and complete model bundles. "
            "These files can be large and sensitive."
        ),
    )
    parser.add_argument("--no-zip", action="store_true", help="Create only the review folder.")
    return parser.parse_args(argv)


def load_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"status": "unreadable", "file": str(path), "error": str(exc)}


def session_candidates(session_root: Path) -> tuple[list[Path], list[Path]]:
    """Return real calibration and online runs, excluding generated exports."""
    participants = session_root / "participants"
    if not participants.is_dir():
        raise RuntimeError(f"participant session directory does not exist: {participants}")

    calibrations: list[Path] = []
    online: list[Path] = []
    for parameters in participants.rglob("parameters.json"):
        session = parameters.parent
        if not session.name.startswith("run-"):
            continue
        if (session / "models" / "attention8").is_dir():
            calibrations.append(session)
        if (session / "realtime" / "model_predictions.jsonl").is_file():
            online.append(session)

    newest_first = lambda value: value.stat().st_mtime  # noqa: E731
    calibrations.sort(key=newest_first, reverse=True)
    online.sort(key=newest_first, reverse=True)
    return calibrations, online


def configured_bundle_paths(online_session: Path) -> list[Path]:
    payload = load_json(online_session / "parameters.json")
    if not isinstance(payload, dict):
        return []
    realtime = payload.get("realtime")
    if not isinstance(realtime, dict):
        return []

    values: list[str] = []
    primary = realtime.get("model")
    if isinstance(primary, dict) and primary.get("bundle_path"):
        values.append(str(primary["bundle_path"]))
    shadows = realtime.get("shadow_models")
    if isinstance(shadows, list):
        for shadow in shadows:
            if isinstance(shadow, dict) and shadow.get("bundle_path"):
                values.append(str(shadow["bundle_path"]))
    return [Path(value).expanduser() for value in values]


def calibration_from_bundle_paths(online_session: Path) -> list[Path]:
    matches: list[Path] = []
    for bundle in configured_bundle_paths(online_session):
        for ancestor in (bundle, *bundle.parents):
            if ancestor.name.lower() != "attention8" or ancestor.parent.name.lower() != "models":
                continue
            candidate = ancestor.parent.parent
            if (candidate / "parameters.json").is_file() and candidate not in matches:
                matches.append(candidate.resolve())
    return matches


def bundle_hashes(root: Path) -> set[str]:
    hashes: set[str] = set()
    if not root.is_dir():
        return hashes
    for manifest in root.rglob("manifest.json"):
        payload = load_json(manifest)
        if isinstance(payload, dict) and payload.get("bundle_hash"):
            hashes.add(str(payload["bundle_hash"]))
    return hashes


def infer_calibration(online_session: Path, candidates: list[Path]) -> Path:
    path_matches = calibration_from_bundle_paths(online_session)
    if len(path_matches) == 1:
        return path_matches[0]
    if len(path_matches) > 1:
        formatted = "\n".join(f"  {path}" for path in path_matches)
        raise RuntimeError(
            "online parameters reference more than one calibration session; pass "
            f"--calibration-session explicitly:\n{formatted}"
        )

    online_hashes = bundle_hashes(online_session / "realtime" / "models")
    scored = []
    for candidate in candidates:
        score = len(online_hashes & bundle_hashes(candidate / "models" / "attention8"))
        if score:
            scored.append((score, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    if len(candidates) == 1:
        return candidates[0]

    formatted = "\n".join(f"  {path}" for path in candidates[:20]) or "  <none>"
    raise RuntimeError(
        "could not safely identify the calibration session used by the online run. "
        "Pass --calibration-session explicitly. Candidates:\n" + formatted
    )


def resolve_sessions(args: argparse.Namespace, session_root: Path) -> tuple[Path, Path, list[str]]:
    calibrations, online_candidates = session_candidates(session_root)
    notes: list[str] = []

    if args.online_session:
        online = Path(args.online_session).expanduser().resolve()
        notes.append("online session supplied explicitly")
    elif online_candidates:
        online = online_candidates[0].resolve()
        notes.append("selected newest online run under participants/")
    else:
        raise RuntimeError("no online session containing realtime/model_predictions.jsonl was found")

    if not (online / "parameters.json").is_file():
        raise RuntimeError(f"online session is missing parameters.json: {online}")
    if not (online / "realtime" / "model_predictions.jsonl").is_file():
        raise RuntimeError(f"online session is missing realtime/model_predictions.jsonl: {online}")

    if args.calibration_session:
        calibration = Path(args.calibration_session).expanduser().resolve()
        notes.append("calibration session supplied explicitly")
    else:
        calibration = infer_calibration(online, calibrations).resolve()
        notes.append("matched calibration to online model bundle path or bundle hash")

    if not (calibration / "parameters.json").is_file():
        raise RuntimeError(f"calibration session is missing parameters.json: {calibration}")
    if not (calibration / "models" / "attention8").is_dir():
        raise RuntimeError(f"calibration session is missing models/attention8: {calibration}")

    referenced = calibration_from_bundle_paths(online)
    if referenced and calibration not in referenced:
        notes.append(
            "WARNING: explicit calibration path differs from the model source path recorded by the online run"
        )
    return calibration, online, notes


def copy_file(source: Path, destination: Path, warnings: list[str]) -> None:
    if not source.is_file():
        return
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    except OSError as exc:
        warnings.append(f"could not copy {source}: {exc}")


def iter_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    return (path for path in root.rglob("*") if path.is_file())


def copy_tree(source: Path, destination: Path, warnings: list[str]) -> None:
    for path in iter_files(source):
        copy_file(path, destination / path.relative_to(source), warnings)


def copy_model_metadata(source: Path, destination: Path, warnings: list[str]) -> None:
    for path in iter_files(source):
        if path.suffix.lower() in MODEL_METADATA_SUFFIXES:
            copy_file(path, destination / path.relative_to(source), warnings)


def export_phase(
    session: Path,
    destination: Path,
    warnings: list[str],
    *,
    include_forensic: bool,
) -> None:
    for relative in ROOT_FILES:
        copy_file(session / relative, destination / relative, warnings)
    copy_file(session / "raw" / "eeg_metadata.json", destination / "raw" / "eeg_metadata.json", warnings)
    copy_tree(session / "events", destination / "events", warnings)
    copy_tree(session / "logs", destination / "logs", warnings)
    copy_tree(session / "reports", destination / "reports", warnings)
    copy_tree(
        session / "realtime" / "adaptation_state",
        destination / "realtime" / "adaptation_state",
        warnings,
    )
    for relative in REALTIME_FILES:
        copy_file(session / "realtime" / relative, destination / "realtime" / relative, warnings)

    copy_model_metadata(
        session / "models" / "attention8",
        destination / "models" / "attention8",
        warnings,
    )
    copy_model_metadata(
        session / "realtime" / "models",
        destination / "realtime" / "models",
        warnings,
    )

    if include_forensic:
        for relative in FORENSIC_FILES:
            copy_file(session / relative, destination / relative, warnings)
        copy_tree(
            session / "models" / "attention8",
            destination / "models" / "attention8",
            warnings,
        )
        copy_tree(
            session / "realtime" / "models",
            destination / "realtime" / "models",
            warnings,
        )


def process_statuses(session: Path) -> list[dict[str, Any]]:
    statuses = []
    for path in sorted((session / "logs" / "processes").glob("*.status.json")):
        statuses.append({"file": path.name, "payload": load_json(path)})
    return statuses


def model_manifests(bundle: Path) -> list[dict[str, Any]]:
    manifests = []
    for path in sorted(bundle.rglob("manifest.json")):
        if "models" not in path.parts:
            continue
        manifests.append({"path": path.relative_to(bundle).as_posix(), "payload": load_json(path)})
    return manifests


def git_output(repository: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def environment_payload(repository: Path) -> dict[str, Any]:
    packages: dict[str, str] = {}
    try:
        for distribution in importlib.metadata.distributions():
            name = distribution.metadata.get("Name")
            if name:
                packages[str(name)] = distribution.version
    except Exception as exc:  # Environment metadata must not block the export.
        packages = {"collection_error": str(exc)}
    return {
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "git_commit": git_output(repository, "rev-parse", "HEAD"),
        "git_status": git_output(repository, "status", "--short"),
        "packages": dict(sorted(packages.items(), key=lambda item: item[0].lower())),
    }


def review_summary(
    bundle: Path,
    calibration: Path,
    online: Path,
    selection_notes: list[str],
    warnings: list[str],
    *,
    include_forensic: bool,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "export_script_version": SCRIPT_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "selection_notes": selection_notes,
        "warnings": warnings,
        "forensic_artifacts_included": include_forensic,
        "excluded_by_default": list(FORENSIC_FILES) + ["fitted model artifacts"],
        "calibration": {
            "session": str(calibration),
            "session_summary": load_json(calibration / "session_summary.json"),
            "epoch_manifest": load_json(calibration / "realtime" / "epochs" / "manifest.json"),
            "process_statuses": process_statuses(calibration),
            "offline_comparison": load_json(
                calibration
                / "reports"
                / "classification"
                / "attention8_offline_comparison"
                / "summary.json"
            ),
        },
        "online": {
            "session": str(online),
            "session_summary": load_json(online / "session_summary.json"),
            "process_statuses": process_statuses(online),
            "evaluation": load_json(online / "reports" / "classification" / "metrics.json"),
            "classifier_replay": load_json(
                online / "reports" / "classification" / "replay_summary.json"
            ),
            "realtime_feature_replay": load_json(
                online / "reports" / "realtime_features" / "replay_summary.json"
            ),
            "offline_comparison": load_json(
                online
                / "reports"
                / "classification"
                / "attention8_offline_comparison"
                / "summary.json"
            ),
        },
        "model_manifests": model_manifests(bundle),
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_inventory(bundle: Path) -> None:
    inventory_path = bundle / "file_inventory.csv"
    files = sorted(path for path in bundle.rglob("*") if path.is_file() and path != inventory_path)
    with inventory_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("relative_path", "bytes", "sha256"))
        writer.writeheader()
        for path in files:
            writer.writerow(
                {
                    "relative_path": path.relative_to(bundle).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )


def write_zip(bundle: Path) -> Path:
    target = bundle.with_suffix(".zip")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, Path(bundle.name) / path.relative_to(bundle))
    return target


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repository = Path(__file__).resolve().parents[1]
    configured_root = args.session_root or os.environ.get("EEGLE_SESSION_ROOT")
    session_root = Path(configured_root).expanduser().resolve() if configured_root else repository / "data"
    if not session_root.is_dir():
        raise RuntimeError(f"session root does not exist: {session_root}")

    calibration, online, selection_notes = resolve_sessions(args, session_root)
    output_parent = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else session_root / "review_exports"
    )
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bundle = output_parent / f"attention8-review-{stamp}"
    bundle.mkdir(parents=True, exist_ok=False)

    warnings: list[str] = []
    export_phase(
        calibration,
        bundle / "calibration",
        warnings,
        include_forensic=bool(args.include_forensic),
    )
    export_phase(
        online,
        bundle / "online",
        warnings,
        include_forensic=bool(args.include_forensic),
    )

    (bundle / "environment.json").write_text(
        json.dumps(environment_payload(repository), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = review_summary(
        bundle,
        calibration,
        online,
        selection_notes,
        warnings,
        include_forensic=bool(args.include_forensic),
    )
    (bundle / "review_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    (bundle / "README-review-bundle.txt").write_text(
        "EEGle Attention8 review bundle\n\n"
        f"Calibration session: {calibration}\n"
        f"Online session: {online}\n\n"
        "Inspect manifests and logs for participant identifiers, Windows user paths, "
        "and device source IDs before sharing. Do not commit this bundle.\n",
        encoding="utf-8",
    )
    write_inventory(bundle)

    archive: Path | None = None
    if not args.no_zip:
        try:
            archive = write_zip(bundle)
        except (OSError, zipfile.BadZipFile) as exc:
            warnings.append(f"ZIP creation failed; use the review folder directly: {exc}")

    print(f"Calibration session: {calibration}")
    print(f"Online session:      {online}")
    print(f"Review folder:       {bundle}")
    if archive is not None:
        print(f"Review ZIP:          {archive}")
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    print("Raw EEG, epoch arrays, engine capture, and fitted model artifacts are excluded by default.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
