"""Development-only simulated EEG LSL outlet."""

from __future__ import annotations

import math
import threading
from time import monotonic, sleep
from typing import Any, Sequence

import numpy as np


class SimulatedEegOutlet:
    """Managed synthetic EEG outlet for hardware-free acquisition rehearsals."""

    def __init__(
        self,
        *,
        name: str = "EnobioMock",
        stream_type: str = "EEG",
        channel_count: int = 32,
        sample_rate_hz: float = 500.0,
        channel_names: Sequence[str] | None = None,
        source_id: str | None = None,
    ) -> None:
        self.name = str(name)
        self.stream_type = str(stream_type)
        self.channel_count = int(channel_count)
        self.sample_rate_hz = float(sample_rate_hz)
        self.channel_names = list(channel_names or _generic_channel_names(self.channel_count))
        self.source_id = str(source_id or f"{self.name}-source")
        if self.channel_count <= 0:
            raise ValueError("simulated EEG channel_count must be positive")
        if self.sample_rate_hz <= 0:
            raise ValueError("simulated EEG sample_rate_hz must be positive")
        if len(self.channel_names) != self.channel_count:
            raise ValueError(
                "simulated EEG channel_names length does not match channel_count: "
                f"{len(self.channel_names)} != {self.channel_count}"
            )
        if len(set(self.channel_names)) != len(self.channel_names):
            raise ValueError("simulated EEG channel_names must be unique")
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = "initialized"
        self._error: str | None = None
        self._sample_count = 0

    def start(self, *, timeout: float = 5.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("simulated EEG outlet is already running")
        self._stop.clear()
        self._ready.clear()
        self._status = "starting"
        self._error = None
        self._sample_count = 0
        self._thread = threading.Thread(target=self._publish, name="eegle-simulated-eeg", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=max(0.1, float(timeout))):
            self.close()
            raise TimeoutError("simulated EEG outlet did not become ready")
        if self._status != "recording":
            self.close()
            raise RuntimeError(self._error or "simulated EEG outlet failed during startup")

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "name": self.name,
            "type": self.stream_type,
            "source_id": self.source_id,
            "channel_count": self.channel_count,
            "channel_names": list(self.channel_names),
            "sample_rate_hz": self.sample_rate_hz,
            "sample_count": self._sample_count,
            "simulated": True,
            "error": self._error,
        }

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                self._status = "failed"
                self._error = "simulated EEG outlet did not stop within 5 seconds"

    def _publish(self) -> None:
        outlet: Any | None = None
        try:
            import pylsl

            info = pylsl.StreamInfo(
                self.name,
                self.stream_type,
                self.channel_count,
                self.sample_rate_hz,
                "float32",
                self.source_id,
            )
            channels = info.desc().append_child("channels")
            for name in self.channel_names:
                channel = channels.append_child("channel")
                channel.append_child_value("label", name)
                channel.append_child_value("unit", "microvolts")
                channel.append_child_value("type", _channel_type(name))
            outlet = pylsl.StreamOutlet(info)
            interval = 1.0 / self.sample_rate_hz
            next_sample_monotonic = monotonic()
            next_sample_lsl = float(pylsl.local_clock())
            sample_index = 0
            self._status = "recording"
            self._ready.set()
            while not self._stop.is_set():
                sample = _simulated_sample(sample_index, self.sample_rate_hz, self.channel_count)
                for index, channel_name in enumerate(self.channel_names):
                    if _channel_type(channel_name) == "stim":
                        sample[index] = 0.0
                outlet.push_sample(sample, timestamp=next_sample_lsl)
                sample_index += 1
                self._sample_count = sample_index
                next_sample_monotonic += interval
                next_sample_lsl += interval
                sleep(max(0.0, next_sample_monotonic - monotonic()))
            self._status = "stopped"
        except Exception as exc:
            self._status = "failed"
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
        finally:
            outlet = None


def run_simulated_eeg(
    name: str = "EnobioMock",
    stream_type: str = "EEG",
    channel_count: int = 32,
    sample_rate_hz: float = 500.0,
    duration_seconds: float = 30.0,
    channel_names: Sequence[str] | None = None,
    source_id: str | None = None,
) -> None:
    simulator = SimulatedEegOutlet(
        name=name,
        stream_type=stream_type,
        channel_count=channel_count,
        sample_rate_hz=sample_rate_hz,
        channel_names=channel_names,
        source_id=source_id,
    )
    simulator.start()
    try:
        deadline = monotonic() + max(0.0, float(duration_seconds))
        while monotonic() < deadline:
            sleep(min(0.2, max(0.0, deadline - monotonic())))
    finally:
        simulator.close()


def _simulated_sample(sample_index: int, sample_rate_hz: float, channel_count: int) -> list[float]:
    t = sample_index / sample_rate_hz
    alpha = math.sin(2.0 * math.pi * 10.0 * t)
    theta = math.sin(2.0 * math.pi * 6.0 * t)
    noise = np.random.normal(0.0, 0.1, channel_count)
    return (5.0 * alpha + 2.0 * theta + noise).astype(float).tolist()


def _generic_channel_names(channel_count: int) -> list[str]:
    return [f"ch_{index + 1:03d}" for index in range(channel_count)]


def _channel_type(name: str) -> str:
    normalized = str(name).upper()
    if normalized == "ECG":
        return "ECG"
    if normalized in {"HEOR", "HEOL", "VEOU", "VEOL"}:
        return "EOG"
    if normalized == "TRIGGER_STATUS":
        return "stim"
    return "EEG"
