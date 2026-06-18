"""Enobio/NIC2 device profiles and LSL matching helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EnobioProfile:
    name: str
    channel_count: int
    channel_names: tuple[str, ...]


ENOBIO_PROFILES: dict[str, EnobioProfile] = {
    "enobio8": EnobioProfile(
        "enobio8",
        8,
        ("Fz", "Cz", "Pz", "Oz", "P3", "P4", "O1", "O2"),
    ),
    "enobio8_alpha_posterior": EnobioProfile(
        "enobio8_alpha_posterior",
        8,
        ("P3", "P4", "PO3", "PO4", "Pz", "O1", "O2", "Oz"),
    ),
    "enobio8_inhibition": EnobioProfile(
        "enobio8_inhibition",
        8,
        ("Fz", "Cz", "Pz", "C3", "C4", "P3", "P4", "Oz"),
    ),
    "enobio22": EnobioProfile(
        "enobio22",
        22,
        (
            "Fp1",
            "Fp2",
            "F7",
            "F3",
            "Fz",
            "F4",
            "F8",
            "T7",
            "C3",
            "Cz",
            "C4",
            "T8",
            "P7",
            "P3",
            "Pz",
            "P4",
            "P8",
            "O1",
            "Oz",
            "O2",
            "M1",
            "M2",
        ),
    ),
    "enobio32": EnobioProfile(
        "enobio32",
        32,
        (
            "Fp1",
            "Fp2",
            "F7",
            "F3",
            "Fz",
            "F4",
            "F8",
            "FC5",
            "FC1",
            "FC2",
            "FC6",
            "T7",
            "C3",
            "Cz",
            "C4",
            "T8",
            "CP5",
            "CP1",
            "CP2",
            "CP6",
            "P7",
            "P3",
            "Pz",
            "P4",
            "P8",
            "POz",
            "O1",
            "Oz",
            "O2",
            "M1",
            "M2",
            "TRG",
        ),
    ),
    "enobio64": EnobioProfile(
        "enobio64",
        64,
        (),
    ),
}


def expected_profile(name: str) -> EnobioProfile:
    try:
        return ENOBIO_PROFILES[name]
    except KeyError as exc:
        known = ", ".join(sorted(ENOBIO_PROFILES))
        raise KeyError(f"Unknown Enobio profile '{name}'. Known profiles: {known}") from exc


def mapped_channel_names(channel_names: list[str], eeg_config: dict[str, Any]) -> tuple[list[str], str]:
    """Map generic NIC2 channel labels to the configured physical montage."""
    names = [str(name) for name in channel_names]
    profile_name = eeg_config.get("profile")
    if not profile_name:
        return names, "lsl_metadata"
    try:
        profile = expected_profile(str(profile_name))
    except KeyError:
        return names, "lsl_metadata"
    if len(profile.channel_names) != len(names):
        return names, "lsl_metadata"
    generic = not names or all(_is_generic_channel_name(name) for name in names)
    if generic:
        return list(profile.channel_names), f"profile:{profile.name}"
    return names, "lsl_metadata"


def _is_generic_channel_name(name: str) -> bool:
    normalized = str(name).strip().lower()
    return normalized.startswith("ch_") or normalized.startswith("channel_") or normalized.isdigit()


def stream_matches_enobio(stream: dict[str, Any], config: dict[str, Any]) -> bool:
    patterns = [str(pattern).lower() for pattern in config.get("lsl_name_patterns", [])]
    if patterns:
        name = str(stream.get("name", "")).lower()
        source_id = str(stream.get("source_id", "")).lower()
        if not any(pattern in name or pattern in source_id for pattern in patterns):
            return False
    return stream_enobio_score(stream, config) >= 5


def stream_enobio_score(stream: dict[str, Any], config: dict[str, Any]) -> int:
    stream_type = str(stream.get("type", "")).lower()
    name = str(stream.get("name", "")).lower()
    source_id = str(stream.get("source_id", "")).lower()
    expected_type = str(config.get("lsl_stream_type", "EEG")).lower()
    patterns = [str(pattern).lower() for pattern in config.get("lsl_name_patterns", [])]
    score = 0
    if stream_type == expected_type:
        score += 3
    else:
        return 0
    name_match = any(pattern in name for pattern in patterns)
    source_match = any(pattern in source_id for pattern in patterns)
    if name_match:
        score += 5
    if source_match:
        score += 4
    count_match = stream.get("channel_count") in set(config.get("expected_channel_counts", []))
    if count_match:
        score += 2
    expected_rate = float(config.get("expected_sample_rate_hz", 0) or 0)
    nominal_rate = float(stream.get("nominal_srate", 0) or 0)
    if expected_rate and nominal_rate and abs(expected_rate - nominal_rate) < 1:
        score += 2
    return score
