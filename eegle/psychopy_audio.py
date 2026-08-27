"""Best-effort PsychoPy speaker discovery and cue playback."""

from __future__ import annotations

from dataclasses import dataclass
from time import sleep
from typing import Any, Callable


DEFAULT_PREFERRED_OUTPUT_PATTERNS = (
    "speakers",
    "realtek",
    "built-in",
    "built in",
    "internal",
)
DEFAULT_AVOID_OUTPUT_PATTERNS = (
    "nvidia high definition audio",
    "amd high definition audio",
    "intel display audio",
    "display audio",
    "hdmi",
    "monitor",
)


@dataclass
class PsychoPyAudioOutput:
    """An opened speaker selected for non-critical operator cues."""

    speaker: Any
    device_name: str
    device_index: int | float | None
    selection_reason: str

    def close(self) -> None:
        close = getattr(self.speaker, "close", None)
        if callable(close):
            close()


def audio_output_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("hardware", {}).get("audio", {}).get("output_enabled", False))


def prepare_psychopy_audio_output(
    config: dict[str, Any],
    *,
    speaker_class: Any | None = None,
) -> tuple[PsychoPyAudioOutput | None, dict[str, Any]]:
    """Open the best available speaker without making audio visit-fatal."""

    audio = dict(config.get("hardware", {}).get("audio", {}) or {})
    if not bool(audio.get("output_enabled", False)):
        return None, {
            "status": "skip",
            "detail": "audio output is disabled by configuration",
            "selected_device_name": None,
            "available_output_devices": [],
        }
    try:
        if speaker_class is None:
            from psychopy.hardware.speaker import SpeakerDevice

            speaker_class = SpeakerDevice
        raw_devices = list(speaker_class.getAvailableDevices() or [])
    except Exception as exc:
        return None, _audio_warning(
            f"speaker enumeration failed: {type(exc).__name__}: {exc}",
            devices=[],
        )
    devices = [_normalise_device(row, position) for position, row in enumerate(raw_devices)]
    devices = [row for row in devices if row["device_name"]]
    if not devices:
        return None, _audio_warning("no audio playback devices were found", devices=[])

    ranked = _rank_audio_devices(devices, audio)
    attempts: list[dict[str, Any]] = []
    latency_class = int(audio.get("latency_class", 0))
    for candidate in ranked:
        try:
            kwargs: dict[str, Any] = {
                "latencyClass": latency_class,
                "resample": True,
            }
            if candidate["device_index"] is not None:
                kwargs["index"] = candidate["device_index"]
            else:
                kwargs["name"] = candidate["device_name"]
            speaker = speaker_class(**kwargs)
        except Exception as exc:
            attempts.append(
                {
                    "device_name": candidate["device_name"],
                    "device_index": candidate["device_index"],
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        selected_name = str(getattr(speaker, "name", None) or candidate["device_name"])
        selected_index = getattr(speaker, "index", candidate["device_index"])
        output = PsychoPyAudioOutput(
            speaker=speaker,
            device_name=selected_name,
            device_index=selected_index,
            selection_reason=candidate["selection_reason"],
        )
        attempts.append(
            {
                "device_name": selected_name,
                "device_index": selected_index,
                "status": "opened",
            }
        )
        avoided = bool(candidate["avoided"])
        status = "warn" if avoided else "ok"
        detail = (
            f"selected speaker '{selected_name}' ({candidate['selection_reason']})"
            if not avoided
            else (
                f"only a display/HDMI-like output could be opened: '{selected_name}'; "
                "the optional baseline end cue may be inaudible"
            )
        )
        return output, {
            "status": status,
            "detail": detail,
            "selected_device_name": selected_name,
            "selected_device_index": selected_index,
            "selection_reason": candidate["selection_reason"],
            "available_output_devices": [row["device_name"] for row in devices],
            "attempts": attempts,
            "failure_policy": "warn",
        }
    errors = "; ".join(
        f"{row['device_name']}: {row.get('error', 'unavailable')}" for row in attempts
    )
    return None, _audio_warning(
        f"no enumerated speaker could be opened{': ' + errors if errors else ''}",
        devices=devices,
        attempts=attempts,
    )


def probe_psychopy_audio_output(config: dict[str, Any]) -> dict[str, Any]:
    """Open and close the selected speaker for disposable preflight probing."""

    output, report = prepare_psychopy_audio_output(config)
    if output is not None:
        try:
            output.close()
        except Exception as exc:
            report = dict(report)
            report["status"] = "warn"
            report["detail"] = (
                f"{report['detail']}; speaker cleanup warned: {type(exc).__name__}: {exc}"
            )
    return report


def play_psychopy_end_signal(
    output: PsychoPyAudioOutput | None,
    config: dict[str, Any],
    *,
    sound_module: Any | None = None,
    sleep_fn: Callable[[float], None] = sleep,
) -> dict[str, Any]:
    """Play the optional eyes-closed completion cue and return a warning on failure."""

    if output is None:
        return {
            "status": "warn",
            "detail": "baseline end signal was not played because no speaker was available",
            "selected_device_name": None,
            "failure_policy": "warn",
        }
    audio = dict(config.get("hardware", {}).get("audio", {}) or {})
    seconds = max(0.05, float(audio.get("end_signal_seconds", 0.5)))
    frequency = max(37.0, float(audio.get("end_signal_frequency_hz", 880.0)))
    tone = None
    try:
        if sound_module is None:
            from psychopy import sound as sound_module
        tone = sound_module.Sound(
            frequency,
            secs=seconds,
            stereo=bool(getattr(output.speaker, "channels", 2) > 1),
            speaker=output.speaker,
        )
        tone.play()
        sleep_fn(seconds + 0.05)
        stop = getattr(tone, "stop", None)
        if callable(stop):
            stop()
        return {
            "status": "played",
            "detail": f"baseline end signal played on '{output.device_name}'",
            "selected_device_name": output.device_name,
            "selected_device_index": output.device_index,
            "frequency_hz": frequency,
            "duration_seconds": seconds,
            "failure_policy": "warn",
        }
    except Exception as exc:
        return {
            "status": "warn",
            "detail": (
                f"baseline end signal failed on '{output.device_name}': "
                f"{type(exc).__name__}: {exc}"
            ),
            "selected_device_name": output.device_name,
            "selected_device_index": output.device_index,
            "failure_policy": "warn",
        }


def _normalise_device(device: Any, position: int) -> dict[str, Any]:
    if not isinstance(device, dict):
        return {"device_name": str(device), "device_index": None, "position": position}
    return {
        "device_name": str(
            device.get("deviceName")
            or device.get("DeviceName")
            or device.get("name")
            or ""
        ).strip(),
        "device_index": device.get("index", device.get("DeviceIndex")),
        "position": position,
    }


def _rank_audio_devices(
    devices: list[dict[str, Any]],
    audio: dict[str, Any],
) -> list[dict[str, Any]]:
    explicit = str(audio.get("output_device_name") or "").strip().casefold()
    preferred = _patterns(
        audio.get("preferred_output_name_patterns"),
        DEFAULT_PREFERRED_OUTPUT_PATTERNS,
    )
    avoided = _patterns(
        audio.get("avoid_output_name_patterns"),
        DEFAULT_AVOID_OUTPUT_PATTERNS,
    )
    ranked = []
    for device in devices:
        name = device["device_name"].casefold()
        explicit_match = bool(explicit and (explicit == name or explicit in name))
        preferred_index = next(
            (index for index, pattern in enumerate(preferred) if pattern in name),
            None,
        )
        is_avoided = any(pattern in name for pattern in avoided)
        if explicit_match:
            priority = (0, 0, device["position"])
            reason = "configured output_device_name"
        elif preferred_index is not None:
            priority = (1, preferred_index, device["position"])
            reason = f"preferred name pattern '{preferred[preferred_index]}'"
        elif not is_avoided:
            priority = (2, 0, device["position"])
            reason = "first non-display playback output"
        else:
            priority = (3, 0, device["position"])
            reason = "last-resort display/HDMI output"
        ranked.append(
            {
                **device,
                "priority": priority,
                "selection_reason": reason,
                "avoided": is_avoided and not explicit_match,
            }
        )
    return sorted(ranked, key=lambda row: row["priority"])


def _patterns(values: Any, defaults: tuple[str, ...]) -> tuple[str, ...]:
    source = values if isinstance(values, (list, tuple)) and values else defaults
    return tuple(str(value).strip().casefold() for value in source if str(value).strip())


def _audio_warning(
    detail: str,
    *,
    devices: list[dict[str, Any]],
    attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "status": "warn",
        "detail": detail,
        "selected_device_name": None,
        "selected_device_index": None,
        "available_output_devices": [row["device_name"] for row in devices],
        "attempts": list(attempts or []),
        "failure_policy": "warn",
    }
