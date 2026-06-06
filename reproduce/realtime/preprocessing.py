"""Preprocessing hooks for online EEG windows."""

from __future__ import annotations

from typing import Any

import numpy as np


def preprocess_window(data: np.ndarray, sample_rate_hz: float, config: dict[str, Any]) -> np.ndarray:
    """Apply a conservative online preprocessing chain to samples x channels data."""
    processed = np.asarray(data, dtype=float)
    if processed.ndim != 2:
        raise ValueError("expected EEG data with shape samples x channels")

    reference = config.get("reference", "average")
    if reference == "average" and processed.shape[1] > 1:
        processed = processed - processed.mean(axis=1, keepdims=True)

    notch_hz = config.get("notch_hz")
    if notch_hz:
        processed = _notch(processed, sample_rate_hz, float(notch_hz))

    low = config.get("bandpass_low_hz")
    high = config.get("bandpass_high_hz")
    if low and high:
        processed = _bandpass(processed, sample_rate_hz, float(low), float(high))

    factor = int(config.get("downsample_factor", 1))
    if factor > 1:
        processed = processed[::factor]

    return processed


def _notch(data: np.ndarray, sample_rate_hz: float, notch_hz: float) -> np.ndarray:
    from scipy import signal

    b, a = signal.iirnotch(w0=notch_hz, Q=30, fs=sample_rate_hz)
    return signal.filtfilt(b, a, data, axis=0)


def _bandpass(data: np.ndarray, sample_rate_hz: float, low_hz: float, high_hz: float) -> np.ndarray:
    from scipy import signal

    sos = signal.butter(4, [low_hz, high_hz], btype="bandpass", fs=sample_rate_hz, output="sos")
    return signal.sosfiltfilt(sos, data, axis=0)


class CausalBandpassNotchPreprocessor:
    """Stateful online preprocessing for samples x channels EEG chunks."""

    def __init__(self, sample_rate_hz: float, channel_count: int, config: dict[str, Any]) -> None:
        self.sample_rate_hz = float(sample_rate_hz)
        self.channel_count = int(channel_count)
        self.config = config
        self.reference = config.get("reference", "average")
        self.downsample_factor = max(1, int(config.get("downsample_factor", 1)))
        self._sample_index = 0
        self._notch_b: np.ndarray | None = None
        self._notch_a: np.ndarray | None = None
        self._notch_zi: np.ndarray | None = None
        self._bandpass_sos: np.ndarray | None = None
        self._bandpass_zi: np.ndarray | None = None
        self._configure_filters()

    @property
    def output_sample_rate_hz(self) -> float:
        return self.sample_rate_hz / self.downsample_factor

    def process_chunk(self, timestamps: np.ndarray, data: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        processed = np.asarray(data, dtype=float)
        ts = np.asarray(timestamps, dtype=float)
        if processed.ndim != 2:
            raise ValueError("expected EEG data with shape samples x channels")
        if processed.shape[1] != self.channel_count:
            raise ValueError(f"expected {self.channel_count} channels, got {processed.shape[1]}")
        if processed.shape[0] == 0:
            return ts, processed

        if self.reference == "average" and processed.shape[1] > 1:
            processed = processed - processed.mean(axis=1, keepdims=True)

        if self._notch_b is not None and self._notch_a is not None:
            processed = self._apply_notch(processed)
        if self._bandpass_sos is not None:
            processed = self._apply_bandpass(processed)

        if self.downsample_factor > 1:
            indices = np.arange(self._sample_index, self._sample_index + processed.shape[0])
            keep = indices % self.downsample_factor == 0
            self._sample_index += processed.shape[0]
            processed = processed[keep]
            ts = ts[keep]
        else:
            self._sample_index += processed.shape[0]

        return ts, processed

    def _configure_filters(self) -> None:
        from scipy import signal

        notch_hz = self.config.get("notch_hz")
        if notch_hz:
            self._notch_b, self._notch_a = signal.iirnotch(w0=float(notch_hz), Q=30, fs=self.sample_rate_hz)

        low = self.config.get("bandpass_low_hz")
        high = self.config.get("bandpass_high_hz")
        if low and high:
            self._bandpass_sos = signal.butter(
                4,
                [float(low), float(high)],
                btype="bandpass",
                fs=self.sample_rate_hz,
                output="sos",
            )

    def _apply_notch(self, data: np.ndarray) -> np.ndarray:
        from scipy import signal

        if self._notch_zi is None:
            zi = signal.lfilter_zi(self._notch_b, self._notch_a)
            self._notch_zi = zi[:, np.newaxis] * data[0][np.newaxis, :]
        filtered, self._notch_zi = signal.lfilter(self._notch_b, self._notch_a, data, axis=0, zi=self._notch_zi)
        return filtered

    def _apply_bandpass(self, data: np.ndarray) -> np.ndarray:
        from scipy import signal

        if self._bandpass_zi is None:
            zi = signal.sosfilt_zi(self._bandpass_sos)
            self._bandpass_zi = zi[:, :, np.newaxis] * data[0][np.newaxis, np.newaxis, :]
        filtered, self._bandpass_zi = signal.sosfilt(self._bandpass_sos, data, axis=0, zi=self._bandpass_zi)
        return filtered
