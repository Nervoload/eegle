"""Neuracle LSL device profiles and matching helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NeuracleProfile:
    name: str
    channel_count: int
    channel_names: tuple[str, ...]


NEURACLE_PROFILES: dict[str, NeuracleProfile] = {
    "neuracle8": NeuracleProfile(
        "neuracle8",
        8,
        ("Fz", "Cz", "Pz", "Oz", "P3", "P4", "O1", "O2"),
    ),
    "neuracle32": NeuracleProfile(
        "neuracle32",
        32,
        (),
    ),
    "neuracle64": NeuracleProfile(
        "neuracle64",
        64,
        (),
    ),
}


def expected_profile(name: str) -> NeuracleProfile:
    try:
        return NEURACLE_PROFILES[name]
    except KeyError as exc:
        known = ", ".join(sorted(NEURACLE_PROFILES))
        raise KeyError(f"Unknown Neuracle profile '{name}'. Known profiles: {known}") from exc


def stream_matches_neuracle(stream: dict[str, Any], config: dict[str, Any]) -> bool:
    patterns = [str(pattern).lower() for pattern in config.get("lsl_name_patterns", [])]
    if patterns:
        name = str(stream.get("name", "")).lower()
        source_id = str(stream.get("source_id", "")).lower()
        if not any(pattern in name or pattern in source_id for pattern in patterns):
            return False
    return stream_neuracle_score(stream, config) >= 5


def stream_neuracle_score(stream: dict[str, Any], config: dict[str, Any]) -> int:
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
