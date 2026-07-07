"""Versioned EEGle model-bundle helpers."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
import zipfile
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


def import_runtime_bundle(source_zip: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Import an eegle-model runtime zip as a content-addressed EEGle bundle."""
    source = Path(source_zip).expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"runtime bundle zip does not exist: {source}")
    if not zipfile.is_zipfile(source):
        raise ValueError(f"runtime bundle is not a zip file: {source}")
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        metadata_name = _runtime_metadata_name(names)
        runtime = json.loads(archive.read(metadata_name).decode("utf-8"))
        _validate_runtime_export(runtime, names)
    kind = str(runtime["kind"])
    contract = dict(runtime["contract"])
    metrics = dict(runtime.get("metrics") or {"status": "imported_without_metrics"})
    training_source = {
        "source": "eegle-model-runtime-zip",
        "runtime_metadata_file": metadata_name,
        "source_zip_sha256": file_sha256(source),
        "path_values_redacted": True,
        **dict(runtime.get("training_source") or {}),
    }
    extra = {
        "model_version": str(runtime.get("model_version", "imported")),
        "model_family": runtime.get("model_family", "eeg_foundation"),
        "target": runtime.get("target", dict(runtime.get("target_spec") or {}).get("target", "attention_lapse_binary")),
        "target_spec": runtime.get("target_spec"),
        "label_mapping": runtime.get("label_mapping", {"attentive": 0, "attention_lapse": 1}),
        "calibration": runtime.get("calibration"),
        "support_query": runtime.get("support_query"),
        "prototype_state": runtime.get("prototype_state"),
        "runtime_export": {
            "metadata_file": metadata_name,
            "files": sorted(names),
            "license": runtime.get("license"),
            "provenance": runtime.get("provenance"),
        },
    }
    return write_model_bundle(
        output_dir,
        kind=kind,
        artifact_path=source,
        artifact_format="eegle_runtime_zip",
        contract=contract,
        metrics=metrics,
        training_source=training_source,
        extra=extra,
    )


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_metadata_name(names: set[str]) -> str:
    for candidate in ("runtime.json", "eegle_runtime.json", "manifest.runtime.json"):
        if candidate in names:
            return candidate
    raise ValueError("runtime bundle zip must contain runtime.json")


def _validate_runtime_export(runtime: dict[str, Any], names: set[str]) -> None:
    kind = str(runtime.get("kind", "")).strip()
    if not kind:
        raise ValueError("runtime export metadata must declare kind")
    if not isinstance(runtime.get("contract"), dict):
        raise ValueError("runtime export metadata must include an input contract")
    contract = dict(runtime["contract"])
    if not contract.get("channel_names") and not contract.get("channel_order"):
        raise ValueError("runtime export contract must declare channel_names or channel_order")
    if not contract.get("sample_rate_hz"):
        raise ValueError("runtime export contract must declare sample_rate_hz")
    runtime_files = [name for name in names if not name.endswith("/") and name not in {"runtime.json", "eegle_runtime.json", "manifest.runtime.json"}]
    if not runtime_files:
        raise ValueError("runtime bundle zip must contain at least one runtime artifact file")


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
