"""EEG device identification helpers for preflight."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eegle.hardware.enobio import stream_enobio_score, stream_matches_enobio
from eegle.hardware.system import CheckResult

DeviceDetector = Callable[[list[dict[str, Any]], dict[str, Any], dict[str, Any]], CheckResult]


def identify_eeg_device(streams: list[dict[str, Any]], eeg_config: dict[str, Any]) -> CheckResult:
    """Identify the configured EEG device family and any matching live streams."""
    family = str(eeg_config.get("family") or "unknown")
    normalized_family = family.strip().lower()
    expected = _expected_device_context(eeg_config, family)
    detector = DEVICE_DETECTORS.get(normalized_family)
    if detector is not None:
        return detector(streams, eeg_config, expected)

    eeg_streams = _candidate_eeg_streams(streams, eeg_config)
    if normalized_family in {"", "unknown", "none"}:
        return CheckResult(
            "eeg_device",
            "warn",
            "no configured EEG device family",
            {**expected, "candidate_eeg_streams": eeg_streams},
        )

    return CheckResult(
        "eeg_device",
        "warn",
        f"configured EEG family '{family}' has no registered detector",
        {**expected, "candidate_eeg_streams": eeg_streams},
    )


def matching_eeg_streams(streams: list[dict[str, Any]], eeg_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return configured-device stream matches in preferred order."""
    family = str(eeg_config.get("family") or "").strip().lower()
    if family == "enobio":
        matches = [stream for stream in streams if stream_matches_enobio(stream, eeg_config)]
        return sorted(matches, key=lambda stream: stream_enobio_score(stream, eeg_config), reverse=True)
    return []


def _expected_device_context(eeg_config: dict[str, Any], family: str) -> dict[str, Any]:
    profile = eeg_config.get("profile")
    return {
        "family": family,
        "profile": profile,
        "interface": "lsl",
        "expected_channel_counts": list(eeg_config.get("expected_channel_counts", [])),
        "expected_sample_rate_hz": eeg_config.get("expected_sample_rate_hz"),
        "lsl_stream_type": eeg_config.get("lsl_stream_type", "EEG"),
        "lsl_name_patterns": list(eeg_config.get("lsl_name_patterns", [])),
    }


def _candidate_eeg_streams(streams: list[dict[str, Any]], eeg_config: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        stream
        for stream in streams
        if str(stream.get("type", "")).lower() == str(eeg_config.get("lsl_stream_type", "EEG")).lower()
    ]


def _identify_enobio_lsl(
    streams: list[dict[str, Any]],
    eeg_config: dict[str, Any],
    expected: dict[str, Any],
) -> CheckResult:
    matches = matching_eeg_streams(streams, eeg_config)
    data = {
        **expected,
        "detector": "enobio_lsl",
        "matches": matches,
        "candidate_eeg_streams": _candidate_eeg_streams(streams, eeg_config),
    }
    label = f"Enobio/NIC2 profile {expected.get('profile') or 'unspecified'}"
    if matches:
        detail = ", ".join(_stream_label(stream) for stream in matches)
        return CheckResult("eeg_device", "ok", f"{label}; detected {detail}", data)
    return CheckResult("eeg_device", "warn", f"{label}; no matching LSL stream detected", data)


DEVICE_DETECTORS: dict[str, DeviceDetector] = {
    "enobio": _identify_enobio_lsl,
}


def _stream_label(stream: dict[str, Any]) -> str:
    name = stream.get("name") or "unnamed"
    channels = stream.get("channel_count")
    rate = stream.get("nominal_srate")
    return f"{name} ({channels} ch, {rate} Hz)"
