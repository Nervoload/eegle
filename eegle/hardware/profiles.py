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
    expected_names = [str(value) for value in eeg_config.get("expected_channel_names", [])]
    physical_names = expected_names or list(profile.channel_names)
    if len(physical_names) != len(names):
        return names, "lsl_metadata"
    generic = not names or all(_is_generic_channel_name(name) for name in names)
    if generic:
        source = "config:expected_channel_names" if expected_names else f"profile:{profile.name}"
        return physical_names, source
    if names == physical_names:
        return names, "lsl_metadata"
    positionally_compatible = all(
        _is_generic_channel_name(observed)
        or _channel_names_equal(observed, expected)
        or _reserved_channel_alias(observed, expected)
        for observed, expected in zip(names, physical_names)
    )
    if positionally_compatible:
        source = (
            "config:expected_channel_names:mixed_positional"
            if expected_names
            else f"profile:{profile.name}:mixed_positional"
        )
        return physical_names, source
    return names, "lsl_metadata"


def analysis_channel_indices(channel_names: list[str], eeg_config: dict[str, Any]) -> list[int]:
    """Return raw-channel indices eligible for derived EEG analysis.

    Acquisition always preserves every transmitted value. This selection only
    removes explicitly declared auxiliary/reserved values from derived EEG data.
    """
    excluded = {
        str(name) for name in eeg_config.get("analysis_excluded_channel_names", [])
    }
    return [index for index, name in enumerate(channel_names) if name not in excluded]


def configured_channel_types(channel_names: list[str], eeg_config: dict[str, Any]) -> list[str]:
    """Resolve configured channel roles without changing the raw value order."""
    expected_names = [str(value) for value in eeg_config.get("expected_channel_names", [])]
    expected_types = [str(value).lower() for value in eeg_config.get("expected_channel_types", [])]
    if len(expected_names) == len(expected_types):
        by_name = dict(zip(expected_names, expected_types))
    else:
        by_name = {}
    result = []
    for name in channel_names:
        normalized = str(name).upper()
        if name in by_name:
            result.append(by_name[name])
        elif normalized == "ECG":
            result.append("ecg")
        elif normalized in {"HEOR", "HEOL", "VEOU", "VEOL"}:
            result.append("eog")
        elif normalized in {"TRIGGER_STATUS", "TRG", "STI", "STIM"}:
            result.append("stim")
        else:
            result.append("eeg")
    return result


def _is_generic_channel_name(name: str) -> bool:
    normalized = str(name).strip().lower()
    if normalized.isdigit():
        return True
    if normalized.startswith(("ch_", "channel_")):
        return True
    return re.fullmatch(r"(ch|chan|channel|data|eeg)[-_ ]?\d+", normalized) is not None


def _channel_names_equal(observed: str, expected: str) -> bool:
    return str(observed).strip().casefold() == str(expected).strip().casefold()


def _reserved_channel_alias(observed: str, expected: str) -> bool:
    if str(expected).strip().upper() != "TRIGGER_STATUS":
        return False
    normalized = re.sub(r"[-_ ]+", "", str(observed).strip().lower())
    return normalized in {"trigger", "triggerstatus", "trg", "sti", "stim", "status"}
