"""Versioned EEGle model-bundle helpers."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any


BUNDLE_SCHEMA = "eegle.model_bundle.v1"


@dataclass(frozen=True)
class ModelBundle:
    """Validated model-bundle manifest plus resolved local paths."""

    manifest: dict[str, Any]
    bundle_dir: Path
    artifact_path: Path

    @property
    def kind(self) -> str:
        return str(self.manifest.get("kind", "unknown"))

    @property
    def bundle_hash(self) -> str:
        return str(self.manifest.get("bundle_hash", ""))

    @property
    def contract(self) -> dict[str, Any]:
        return dict(self.manifest.get("contract") or {})

    def payload(self) -> dict[str, Any]:
        return {
            **self.manifest,
            "bundle_dir": str(self.bundle_dir),
            "artifact_path": str(self.artifact_path),
        }


def write_model_bundle(
    bundle_dir: str | Path,
    *,
    kind: str,
    artifact_path: str | Path,
    artifact_format: str,
    contract: dict[str, Any],
    metrics: dict[str, Any],
    training_source: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write a content-addressed EEGle model bundle."""
    target = Path(bundle_dir).expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    source = Path(artifact_path).expanduser().resolve()
    artifact_name = source.name
    copied = target / artifact_name
    if source != copied:
        shutil.copy2(source, copied)
    manifest = {
        "schema": BUNDLE_SCHEMA,
        "kind": kind,
        "artifact": artifact_name,
        "artifact_format": artifact_format,
        "artifact_sha256": file_sha256(copied),
        "bundle_hash": "",
        "label_mapping": {"go": 0, "no_go": 1},
        "contract": contract,
        "metrics_file": "metrics.json",
        "training_source": training_source,
        "software": software_versions(),
        **dict(extra or {}),
    }
    metrics_path = target / "metrics.json"
    _write_json(metrics_path, metrics)
    manifest["bundle_hash"] = _bundle_hash(manifest, metrics_path, copied)
    _write_json(target / "manifest.json", manifest)
    return manifest


def load_model_bundle(path: str | Path) -> dict[str, Any]:
    """Load and validate an EEGle model bundle manifest."""
    return load_model_bundle_object(path).payload()


def load_model_bundle_object(path: str | Path) -> ModelBundle:
    """Load and validate an EEGle model bundle as a typed object."""
    target = Path(path).expanduser().resolve()
    manifest_path = target / "manifest.json" if target.is_dir() else target
    manifest = _load_json(manifest_path)
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise ValueError(f"unsupported model bundle schema in {manifest_path}")
    bundle_dir = manifest_path.parent
    artifact = bundle_dir / str(manifest["artifact"])
    if not artifact.exists():
        raise ValueError(f"model bundle artifact missing: {artifact}")
    actual_hash = file_sha256(artifact)
    if actual_hash != manifest.get("artifact_sha256"):
        raise ValueError(f"model bundle artifact hash mismatch: {artifact}")
    metrics_path = bundle_dir / str(manifest.get("metrics_file", "metrics.json"))
    if not metrics_path.exists():
        raise ValueError(f"model bundle metrics missing: {metrics_path}")
    expected_bundle_hash = _bundle_hash(manifest, metrics_path, artifact)
    if expected_bundle_hash != manifest.get("bundle_hash"):
        raise ValueError(f"model bundle hash mismatch: {bundle_dir}")
    return ModelBundle(manifest=dict(manifest), bundle_dir=bundle_dir, artifact_path=artifact)


def snapshot_model_bundle(bundle_path: str | Path, snapshots_root: str | Path, role: str) -> dict[str, Any]:
    """Copy a validated bundle into a session-local snapshot directory."""
    loaded = load_model_bundle(bundle_path)
    destination = Path(snapshots_root).expanduser().resolve() / f"{role}-{loaded['kind']}-{loaded['bundle_hash'][:12]}"
    if not destination.exists():
        shutil.copytree(loaded["bundle_dir"], destination)
    return load_model_bundle(destination)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def software_versions() -> dict[str, str]:
    versions = {"python": sys.version.split()[0], "platform": platform.platform()}
    for package in ("numpy", "scipy", "scikit-learn", "joblib", "pyriemann", "torch"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            continue
    return versions


def _bundle_hash(manifest: dict[str, Any], metrics_path: Path, artifact_path: Path) -> str:
    return hashlib.sha256(
        json.dumps({**manifest, "bundle_hash": ""}, sort_keys=True).encode("utf-8")
        + metrics_path.read_bytes()
        + artifact_path.read_bytes()
    ).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return payload
