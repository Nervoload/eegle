"""Realtime EEG ring buffer."""

from __future__ import annotations

import numpy as np


class RingBuffer:
    def __init__(self, max_samples: int, channel_count: int) -> None:
        self.max_samples = int(max_samples)
        self.channel_count = int(channel_count)
        self._timestamps = np.empty(self.max_samples, dtype=float)
        self._data = np.empty((self.max_samples, self.channel_count), dtype=float)
        self._write_index = 0
        self._size = 0

    def append(self, timestamp: float, values: np.ndarray) -> None:
        arr = np.asarray(values, dtype=float)
        if arr.shape[0] != self.channel_count:
            raise ValueError(f"expected {self.channel_count} channels, got {arr.shape[0]}")
        self._timestamps[self._write_index] = float(timestamp)
        self._data[self._write_index] = arr
        self._write_index = (self._write_index + 1) % self.max_samples
        self._size = min(self._size + 1, self.max_samples)

    def append_chunk(self, timestamps: np.ndarray, values: np.ndarray) -> None:
        ts = np.asarray(timestamps, dtype=float)
        data = np.asarray(values, dtype=float)
        if data.ndim != 2 or data.shape[1] != self.channel_count:
            raise ValueError(f"expected samples x {self.channel_count} channels")
        if ts.ndim != 1 or ts.shape[0] != data.shape[0]:
            raise ValueError("timestamp and sample counts do not match")
        if ts.size == 0:
            return
        if ts.size >= self.max_samples:
            ts = ts[-self.max_samples :]
            data = data[-self.max_samples :]
        first = min(ts.size, self.max_samples - self._write_index)
        self._timestamps[self._write_index : self._write_index + first] = ts[:first]
        self._data[self._write_index : self._write_index + first] = data[:first]
        remaining = ts.size - first
        if remaining:
            self._timestamps[:remaining] = ts[first:]
            self._data[:remaining] = data[first:]
        self._write_index = (self._write_index + ts.size) % self.max_samples
        self._size = min(self._size + ts.size, self.max_samples)

    def window(self, samples: int) -> tuple[np.ndarray, np.ndarray]:
        count = min(max(0, int(samples)), self._size)
        if count == 0:
            return np.empty((0,), dtype=float), np.empty((0, self.channel_count), dtype=float)
        start = (self._write_index - count) % self.max_samples
        if start < self._write_index and self._size < self.max_samples:
            return self._timestamps[start : self._write_index].copy(), self._data[start : self._write_index].copy()
        indices = (start + np.arange(count)) % self.max_samples
        return self._timestamps[indices].copy(), self._data[indices].copy()

    def range(self, start_timestamp: float, end_timestamp: float) -> tuple[np.ndarray, np.ndarray]:
        """Return samples inside an inclusive timestamp range."""
        if self._size == 0:
            return np.empty((0,), dtype=float), np.empty((0, self.channel_count), dtype=float)
        start = (self._write_index - self._size) % self.max_samples
        lower, upper = float(start_timestamp), float(end_timestamp)
        if start < self._write_index and self._size < self.max_samples:
            timestamps = self._timestamps[start : self._write_index]
            mask = np.isfinite(timestamps) & (timestamps >= lower) & (timestamps <= upper)
            return timestamps[mask].copy(), self._data[start : self._write_index][mask].copy()
        indices = (start + np.arange(self._size)) % self.max_samples
        timestamps = self._timestamps[indices]
        mask = np.isfinite(timestamps) & (timestamps >= lower) & (timestamps <= upper)
        selected = indices[mask]
        return timestamps[mask].copy(), self._data[selected].copy()

    @property
    def earliest_timestamp(self) -> float | None:
        if self._size == 0:
            return None
        index = (self._write_index - self._size) % self.max_samples
        return float(self._timestamps[index])

    @property
    def latest_timestamp(self) -> float | None:
        if self._size == 0:
            return None
        return float(self._timestamps[(self._write_index - 1) % self.max_samples])

    def __len__(self) -> int:
        return self._size
