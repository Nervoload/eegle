"""Prepared Study 1 profile and its lowering into real EEGle authoring."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from eegle.authoring import ContractUpdate, ExperimentDesign, ProcessingStep
from eegle.compiler import canonical_hash

PROFILE_SCHEMA = "eegle.workbench.study1_profile.v1"
PROVENANCE_SCHEMA = "eegle.workbench.profile_provenance.v1"
DEFAULT_VARIANT = "demo_rehearsal"
PROFILE_ROOT = Path(__file__).with_name("profiles")


@dataclass(frozen=True, slots=True)
class Study1Profile:
    """Validated, immutable view of the prepared demo profile."""

    payload: Mapping[str, Any]
    provenance: Mapping[str, Any]
    digest: str

    @property
    def project_id(self) -> str:
        return str(self.payload["project_id"])

    @property
    def display_name(self) -> str:
        return str(self.payload["display_name"])

    @property
    def statement(self) -> str:
        return str(self.payload["statement"])

    @property
    def default_variant(self) -> str:
        return str(self.payload["default_variant"])

    def variant(self, name: str | None = None) -> Mapping[str, Any]:
        selected = name or self.default_variant
        variants = self.payload["variants"]
        if not isinstance(variants, Mapping) or selected not in variants:
            raise KeyError(f"unknown Study 1 profile variant: {selected}")
        value = variants[selected]
        if not isinstance(value, Mapping):
            raise TypeError(f"Study 1 profile variant {selected} must be an object")
        return value

    @property
    def logical_channels(self) -> tuple[str, ...]:
        physiological = int(self.payload["signal"]["physiological_value_count"])
        reserved = str(self.payload["signal"]["reserved_value"])
        return tuple(f"PHYSIO_{index:02d}" for index in range(1, physiological + 1)) + (
            reserved,
        )

    @property
    def marker_kinds(self) -> tuple[str, ...]:
        return tuple(str(value) for value in self.payload["markers"]["event_kinds"])


def _read_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"profile document must be a JSON object: {path}")
    return payload


def _validate_profile(payload: Mapping[str, Any], provenance: Mapping[str, Any]) -> None:
    if payload.get("schema") != PROFILE_SCHEMA:
        raise ValueError(f"unsupported Workbench profile schema: {payload.get('schema')}")
    if provenance.get("schema") != PROVENANCE_SCHEMA:
        raise ValueError(
            f"unsupported Workbench profile provenance schema: {provenance.get('schema')}"
        )
    if payload.get("profile_id") != provenance.get("profile_id"):
        raise ValueError("profile and provenance identities do not match")
    variants = payload.get("variants")
    if not isinstance(variants, Mapping):
        raise TypeError("Study 1 profile variants must be an object")
    for required in ("full_study1", "demo_rehearsal"):
        if required not in variants:
            raise ValueError(f"Study 1 profile is missing {required}")
    signal = payload.get("signal")
    if not isinstance(signal, Mapping):
        raise TypeError("Study 1 signal contract must be an object")
    if int(signal.get("physiological_value_count", 0)) != 64:
        raise ValueError("Study 1 profile requires 64 physiological values")
    if int(signal.get("total_value_count", 0)) != 65:
        raise ValueError("Study 1 profile requires 65 total LSL values")
    if signal.get("reserved_value") != "TRIGGER_STATUS":
        raise ValueError("Study 1 profile must reserve TRIGGER_STATUS")
    if float(signal.get("nominal_rate_hz", 0.0)) != 1000.0:
        raise ValueError("Study 1 profile requires a 1000 Hz nominal rate")
    rehearsal = variants["demo_rehearsal"]
    if rehearsal.get("scientific_status") != "demonstration_only":
        raise ValueError("the rehearsal variant must remain demonstration_only")
    if rehearsal.get("session_kind") == "study1_visit":
        raise ValueError("a rehearsal cannot use the Study 1 visit session kind")


def load_study1_profile(root: Path | None = None) -> Study1Profile:
    """Load and validate the tracked profile without importing a task framework."""

    profile_root = root or PROFILE_ROOT
    payload = _read_object(profile_root / "study1_neuracle64_demo.json")
    provenance = _read_object(profile_root / "profile_provenance.json")
    _validate_profile(payload, provenance)
    digest = canonical_hash(payload)
    if provenance.get("local_profile_digest") != digest:
        raise ValueError("profile provenance does not match the local profile digest")
    return Study1Profile(
        payload=MappingProxyType(payload),
        provenance=MappingProxyType(provenance),
        digest=digest,
    )


def build_study1_design(
    profile: Study1Profile,
    *,
    variant: str | None = None,
) -> ExperimentDesign:
    """Lower the prepared profile into one real, bounded ExperimentDesign."""

    variant_name = variant or profile.default_variant
    selected = profile.variant(variant_name)
    signal = profile.payload["signal"]
    markers = profile.payload["markers"]
    processing = profile.payload["processing"]
    source = profile.provenance
    design = ExperimentDesign.create(
        profile.project_id,
        profile.statement,
        annotations={
            "workbench_profile_id": profile.payload["profile_id"],
            "workbench_profile_digest": profile.digest,
            "workbench_profile_variant": variant_name,
            "scientific_status": selected["scientific_status"],
            "session_kind": selected["session_kind"],
            "policy": "observe_only",
            "apparatus_mapping": "pending_lab_confirmation",
            "source_branch": source["source_branch"],
            "source_commit": source["source_commit"],
        },
    )
    return (
        design.dense_signal(
            str(signal["signal_id"]),
            modality=str(signal["modality"]),
            channels=profile.logical_channels,
            unit=str(signal["unit"]),
            rate_hz=float(signal["nominal_rate_hz"]),
            clock_id=str(signal["clock_id"]),
        )
        .event_stream(
            str(markers["event_id"]),
            kinds=profile.marker_kinds,
            modality="markers",
            clock_id=str(signal["clock_id"]),
        )
        .processing_chain(
            str(processing["chain_id"]),
            input=f"signal.{signal['signal_id']}",
            steps=(
                ProcessingStep(
                    "identity",
                    str(processing["plugin_id"]),
                    ContractUpdate(),
                ),
            ),
        )
        .event_window(
            str(processing["window_id"]),
            input=f"processing.{processing['chain_id']}",
            event_stream=f"event.{markers['event_id']}",
            event_kind=str(processing["event_kind"]),
            start_seconds=float(processing["window_start_seconds"]),
            end_seconds=float(processing["window_end_seconds"]),
        )
        .phase(
            "observe",
            goals=(f"window.{processing['window_id']}",),
            initial=True,
        )
        .record(
            f"signal.{signal['signal_id']}",
            f"event.{markers['event_id']}",
            phases=("phase.observe",),
        )
    )
