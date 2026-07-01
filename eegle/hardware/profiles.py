"""Shared EEG profile lookup and channel-name mapping."""

from __future__ import annotations

import re
from typing import Any

from eegle.hardware.enobio import ENOBIO_PROFILES, EnobioProfile, expected_profile as expected_enobio_profile
from eegle.hardware.neuracle import NEURACLE_PROFILES, NeuracleProfile, expected_profile as expected_neuracle_profile

EegProfile = EnobioProfile | NeuracleProfile


def expected_profile(name: str, family: str | None = None) -> EegProfile:
    normalized_family = str(family or "").strip().lower()
    if normalized_family == "enobio":
        return expected_enobio_profile(name)
    if normalized_family == "neuracle":
        return expected_neuracle_profile(name)
    if name in ENOBIO_PROFILES:
        return expected_enobio_profile(name)
    if name in NEURACLE_PROFILES:
        return expected_neuracle_profile(name)
    known = ", ".join(sorted([*ENOBIO_PROFILES, *NEURACLE_PROFILES]))
    raise KeyError(f"Unknown EEG profile '{name}'. Known profiles: {known}")


def mapped_channel_names(channel_names: list[str], eeg_config: dict[str, Any]) -> tuple[list[str], str]:
    """Map generic LSL channel labels to the configured physical montage."""
    names = [str(name) for name in channel_names]
    profile_name = eeg_config.get("profile")
    if not profile_name:
        return names, "lsl_metadata"
    try:
        profile = expected_profile(str(profile_name), eeg_config.get("family"))
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
    if normalized.isdigit():
        return True
    if normalized.startswith(("ch_", "channel_")):
        return True
    return re.fullmatch(r"(ch|chan|channel|data)[-_ ]?\d+", normalized) is not None
