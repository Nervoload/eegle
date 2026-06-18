"""Realtime performance configuration and counters."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any


@dataclass(frozen=True)
class RealtimePerformanceConfig:
    primary_latency_budget_ms: float = 50.0
    writer_flush_every: int = 50
    writer_flush_interval_ms: float = 100.0
    inference_queue_max_epochs: int = 4
    skip_shadows_when_queue_depth_gte: int = 2
    large_cap_channel_threshold: int = 32
    large_cap_detected: bool = False

    @property
    def writer_flush_interval_seconds(self) -> float:
        return max(0.001, self.writer_flush_interval_ms / 1000.0)

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RealtimePerformanceStats:
    eeg_pull_time_ms: float = 0.0
    preprocessing_time_ms: float = 0.0
    epoch_extraction_time_ms: float = 0.0
    primary_latency_ms: float = 0.0
    shadow_latency_ms: float = 0.0
    writer_backlog: int = 0
    inference_queue_depth: int = 0
    skipped_shadow_count: int = 0
    buffer_utilization: float = 0.0
    raw_buffer_utilization: float = 0.0
    worst_loop_time_ms: float = 0.0

    def record_loop(self, started_at: float) -> None:
        self.worst_loop_time_ms = max(self.worst_loop_time_ms, elapsed_ms(started_at))

    def snapshot(
        self,
        *,
        writer_backlog: int,
        inference_queue_depth: int,
        buffer_utilization: float,
        raw_buffer_utilization: float,
    ) -> dict[str, Any]:
        self.writer_backlog = int(writer_backlog)
        self.inference_queue_depth = int(inference_queue_depth)
        self.buffer_utilization = float(buffer_utilization)
        self.raw_buffer_utilization = float(raw_buffer_utilization)
        return asdict(self)


def performance_config_from(config: dict[str, Any], channel_count: int) -> RealtimePerformanceConfig:
    realtime = dict(config.get("realtime") or {})
    values = dict(realtime.get("performance") or {})
    threshold = int(values.get("large_cap_channel_threshold", 32))
    return RealtimePerformanceConfig(
        primary_latency_budget_ms=float(values.get("primary_latency_budget_ms", 50.0)),
        writer_flush_every=max(1, int(values.get("writer_flush_every", 50))),
        writer_flush_interval_ms=max(1.0, float(values.get("writer_flush_interval_ms", 100.0))),
        inference_queue_max_epochs=max(1, int(values.get("inference_queue_max_epochs", 4))),
        skip_shadows_when_queue_depth_gte=max(1, int(values.get("skip_shadows_when_queue_depth_gte", 2))),
        large_cap_channel_threshold=threshold,
        large_cap_detected=int(channel_count) >= threshold,
    )


def elapsed_ms(started_at: float) -> float:
    return (monotonic() - started_at) * 1000.0


def buffer_utilization(buffer: Any | None) -> float:
    if buffer is None:
        return 0.0
    max_samples = max(1, int(getattr(buffer, "max_samples", 1)))
    return min(1.0, len(buffer) / max_samples)
