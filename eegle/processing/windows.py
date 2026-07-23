"""Typed generic window specifications and materialized dense windows.

Window builders are engine-facing stateful components.  They consume only
admitted packets and expose the exact packet identities and availability time
that contributed to every window.  Dense arrays remain samples x channels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping

import numpy as np

from eegle._domain import Lineage
from eegle._validation import require_finite, require_identifier
from eegle.compiler.lock import canonical_hash
from eegle.streams.clocks import TimePoint
from eegle.streams.packets import (
    DenseSampleBatch,
    Packet,
    SparseEvent,
    SparseEventBatch,
)

if TYPE_CHECKING:
    from eegle.plugins.contracts import ExecutionContext


@dataclass(frozen=True, slots=True)
class ContinuousWindowSpec:
    duration_seconds: float
    step_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "duration_seconds", require_finite(self.duration_seconds, "duration_seconds")
        )
        object.__setattr__(self, "step_seconds", require_finite(self.step_seconds, "step_seconds"))
        if self.duration_seconds <= 0 or self.step_seconds <= 0:
            raise ValueError("continuous window duration and step must be positive")


@dataclass(frozen=True, slots=True)
class EventWindowSpec:
    start_offset_seconds: float
    end_offset_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "start_offset_seconds",
            require_finite(self.start_offset_seconds, "start_offset_seconds"),
        )
        object.__setattr__(
            self,
            "end_offset_seconds",
            require_finite(self.end_offset_seconds, "end_offset_seconds"),
        )
        if self.end_offset_seconds <= self.start_offset_seconds:
            raise ValueError("event window end must follow its start")


@dataclass(frozen=True, slots=True)
class Window:
    window_id: str
    stream_id: str
    start_time: TimePoint
    end_time: TimePoint
    input_ids: tuple[str, ...]
    trigger_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "window_id", require_identifier(self.window_id, "window_id"))
        object.__setattr__(self, "stream_id", require_identifier(self.stream_id, "stream_id"))
        if self.start_time.clock_id != self.end_time.clock_id:
            raise ValueError("window start and end must share a clock")
        if self.end_time.seconds <= self.start_time.seconds:
            raise ValueError("window end must follow its start")
        inputs = tuple(require_identifier(value, "input_id") for value in self.input_ids)
        if not inputs:
            raise ValueError("window must reference at least one input")
        object.__setattr__(self, "input_ids", inputs)
        if self.trigger_id is not None:
            object.__setattr__(self, "trigger_id", require_identifier(self.trigger_id, "trigger_id"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.window.v1",
            "window_id": self.window_id,
            "stream_id": self.stream_id,
            "start_time": self.start_time.to_payload(),
            "end_time": self.end_time.to_payload(),
            "input_ids": list(self.input_ids),
            "trigger_id": self.trigger_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Window":
        if payload.get("schema") != "eegle.window.v1":
            raise ValueError(f"unsupported window schema: {payload.get('schema')}")
        return cls(
            window_id=str(payload["window_id"]),
            stream_id=str(payload["stream_id"]),
            start_time=TimePoint.from_payload(payload["start_time"]),
            end_time=TimePoint.from_payload(payload["end_time"]),
            input_ids=tuple(str(value) for value in payload["input_ids"]),
            trigger_id=None if payload.get("trigger_id") is None else str(payload["trigger_id"]),
        )


DENSE_WINDOW_SCHEMA = "eegle.dense_window.v1"


@dataclass(frozen=True, slots=True, eq=False)
class DenseWindow:
    """A bounded, materialized dense window suitable for model inference."""

    window_id: str
    stream_id: str
    stream_revision: int
    sequence_start: int
    channel_ids: tuple[str, ...]
    values: np.ndarray
    start_time: TimePoint
    end_time: TimePoint
    available_time: TimePoint
    input_ids: tuple[str, ...]
    lineage: Lineage
    validity_mask: np.ndarray | None = None
    schema: str = DENSE_WINDOW_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != DENSE_WINDOW_SCHEMA:
            raise ValueError(f"unsupported dense window schema: {self.schema}")
        object.__setattr__(self, "window_id", require_identifier(self.window_id, "window_id"))
        object.__setattr__(self, "stream_id", require_identifier(self.stream_id, "stream_id"))
        object.__setattr__(self, "stream_revision", int(self.stream_revision))
        if self.stream_revision <= 0:
            raise ValueError("stream_revision must be positive")
        object.__setattr__(self, "sequence_start", int(self.sequence_start))
        if self.sequence_start < 0:
            raise ValueError("sequence_start cannot be negative")
        channels = tuple(require_identifier(value, "channel_id") for value in self.channel_ids)
        if not channels or len(channels) != len(set(channels)):
            raise ValueError("channel_ids must be non-empty and unique")
        object.__setattr__(self, "channel_ids", channels)
        values = np.array(self.values, copy=True)
        if values.ndim != 2 or values.shape[0] == 0 or values.shape[1] != len(channels):
            raise ValueError("dense window values must be non-empty samples x channels")
        if not np.issubdtype(values.dtype, np.number):
            raise ValueError("dense window values must use a numeric dtype")
        mask = None
        if self.validity_mask is not None:
            mask = np.asarray(self.validity_mask, dtype=bool).copy()
            if mask.shape != values.shape:
                raise ValueError("validity_mask shape must match dense window values")
        finite = np.isfinite(values)
        if not bool(np.all(finite)):
            if mask is None or bool(np.any(mask & ~finite)):
                raise ValueError("non-finite dense window values must be marked invalid")
        values.setflags(write=False)
        object.__setattr__(self, "values", values)
        if mask is not None:
            mask.setflags(write=False)
            object.__setattr__(self, "validity_mask", mask)
        if self.start_time.clock_id != self.end_time.clock_id:
            raise ValueError("dense window start and end must share a sample clock")
        if self.end_time.seconds <= self.start_time.seconds:
            raise ValueError("dense window end must follow its start")
        inputs = tuple(require_identifier(value, "input_id") for value in self.input_ids)
        if not inputs:
            raise ValueError("dense window must reference at least one admitted input")
        object.__setattr__(self, "input_ids", inputs)
        if self.lineage.input_ids != inputs:
            raise ValueError("dense window input_ids must exactly match lineage input_ids")
        latest = self.lineage.latest_input_available_time
        if latest is None or latest != self.available_time:
            raise ValueError("dense window lineage must record its exact input availability")
        revision = self.lineage.stream_revisions.get(self.stream_id)
        if revision != self.stream_revision:
            raise ValueError("dense window lineage must record its stream revision")

    @property
    def sample_count(self) -> int:
        return int(self.values.shape[0])

    @property
    def sequence_end(self) -> int:
        return self.sequence_start + self.sample_count - 1

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DenseWindow):
            return NotImplemented
        masks_equal = (
            self.validity_mask is None
            and other.validity_mask is None
            or self.validity_mask is not None
            and other.validity_mask is not None
            and bool(np.array_equal(self.validity_mask, other.validity_mask))
        )
        return bool(
            self.schema == other.schema
            and self.window_id == other.window_id
            and self.stream_id == other.stream_id
            and self.stream_revision == other.stream_revision
            and self.sequence_start == other.sequence_start
            and self.channel_ids == other.channel_ids
            and self.values.dtype == other.values.dtype
            and np.array_equal(self.values, other.values, equal_nan=True)
            and masks_equal
            and self.start_time == other.start_time
            and self.end_time == other.end_time
            and self.available_time == other.available_time
            and self.input_ids == other.input_ids
            and self.lineage == other.lineage
        )

    def to_payload(self) -> dict[str, Any]:
        mask = self.validity_mask
        rows: list[list[Any]] = []
        for row_index in range(self.values.shape[0]):
            row: list[Any] = []
            for column_index in range(self.values.shape[1]):
                valid = mask is None or bool(mask[row_index, column_index])
                row.append(self.values[row_index, column_index].item() if valid else None)
            rows.append(row)
        return {
            "schema": self.schema,
            "window_id": self.window_id,
            "stream_id": self.stream_id,
            "stream_revision": self.stream_revision,
            "sequence_start": self.sequence_start,
            "channel_ids": list(self.channel_ids),
            "dtype": self.values.dtype.name,
            "shape": list(self.values.shape),
            "values": rows,
            "validity_mask": None if mask is None else mask.astype(bool).tolist(),
            "start_time": self.start_time.to_payload(),
            "end_time": self.end_time.to_payload(),
            "available_time": self.available_time.to_payload(),
            "input_ids": list(self.input_ids),
            "lineage": self.lineage.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DenseWindow":
        dtype = np.dtype(str(payload["dtype"]))
        mask_payload = payload.get("validity_mask")
        mask = None if mask_payload is None else np.asarray(mask_payload, dtype=bool)
        invalid_placeholder = 0 if dtype.kind in {"i", "u"} else np.nan
        values = np.asarray(
            [
                [invalid_placeholder if value is None else value for value in row]
                for row in payload["values"]
            ],
            dtype=dtype,
        )
        if values.shape != tuple(int(value) for value in payload["shape"]):
            raise ValueError("dense window payload shape does not match encoded values")
        return cls(
            schema=str(payload.get("schema", DENSE_WINDOW_SCHEMA)),
            window_id=str(payload["window_id"]),
            stream_id=str(payload["stream_id"]),
            stream_revision=int(payload["stream_revision"]),
            sequence_start=int(payload["sequence_start"]),
            channel_ids=tuple(str(value) for value in payload["channel_ids"]),
            values=values,
            validity_mask=mask,
            start_time=TimePoint.from_payload(payload["start_time"]),
            end_time=TimePoint.from_payload(payload["end_time"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            input_ids=tuple(str(value) for value in payload["input_ids"]),
            lineage=Lineage.from_payload(payload["lineage"]),
        )


class ContinuousWindowBuilder:
    """Build fixed-size overlapping windows from sequential dense batches.

    The retained buffer is bounded by ``window_samples - 1`` after each update;
    all emitted windows are based only on packets already admitted by the engine.
    """

    def __init__(self, window_samples: int, step_samples: int) -> None:
        self.window_samples = int(window_samples)
        self.step_samples = int(step_samples)
        if self.window_samples <= 0 or self.step_samples <= 0:
            raise ValueError("window_samples and step_samples must be positive")
        if self.step_samples > self.window_samples:
            raise ValueError("step_samples cannot exceed window_samples")
        self._values: np.ndarray | None = None
        self._validity: np.ndarray | None = None
        self._sample_input_ids: list[str] = []
        self._sample_available_times: list[TimePoint] = []
        self._sequence_start: int | None = None
        self._next_sequence: int | None = None
        self._first_sample_time: TimePoint | None = None
        self._sample_period_seconds: float | None = None
        self._stream_id: str | None = None
        self._stream_revision: int | None = None
        self._channel_ids: tuple[str, ...] | None = None
        self._clock_mapping_revisions: dict[str, int] = {}
        self._stream_revisions: dict[str, int] = {}

    @property
    def buffered_samples(self) -> int:
        return 0 if self._values is None else int(self._values.shape[0])

    def update(
        self,
        packet: Packet,
        context: "ExecutionContext",
    ) -> Iterable[DenseWindow]:
        if not isinstance(packet, DenseSampleBatch):
            raise TypeError("continuous dense windows require DenseSampleBatch input")
        self._validate_packet(packet, context)
        self._append(packet)
        windows: list[DenseWindow] = []
        while self.buffered_samples >= self.window_samples:
            windows.append(self._materialize(context))
            self._discard(self.step_samples)
        return tuple(windows)

    def snapshot_state(self) -> dict[str, Any]:
        values = self._values
        mask = self._validity
        rows: list[list[Any]] | None = None
        if values is not None:
            rows = []
            for row_index in range(values.shape[0]):
                row: list[Any] = []
                for column_index in range(values.shape[1]):
                    valid = mask is None or bool(mask[row_index, column_index])
                    row.append(values[row_index, column_index].item() if valid else None)
                rows.append(row)
        payload: dict[str, Any] = {
            "schema": "eegle.continuous_window_state.v1",
            "window_samples": self.window_samples,
            "step_samples": self.step_samples,
            "dtype": None if values is None else values.dtype.name,
            "values": rows,
            "validity_mask": None if mask is None else mask.astype(bool).tolist(),
            "sample_input_ids": list(self._sample_input_ids),
            "sample_available_times": [
                value.to_payload() for value in self._sample_available_times
            ],
            "sequence_start": self._sequence_start,
            "next_sequence": self._next_sequence,
            "first_sample_time": None
            if self._first_sample_time is None
            else self._first_sample_time.to_payload(),
            "sample_period_seconds": self._sample_period_seconds,
            "stream_id": self._stream_id,
            "stream_revision": self._stream_revision,
            "channel_ids": None if self._channel_ids is None else list(self._channel_ids),
            "clock_mapping_revisions": dict(self._clock_mapping_revisions),
            "stream_revisions": dict(self._stream_revisions),
        }
        payload["state_hash"] = canonical_hash(payload)
        return payload

    def restore_state(self, payload: Mapping[str, Any]) -> None:
        if payload.get("schema") != "eegle.continuous_window_state.v1":
            raise ValueError(
                f"unsupported continuous window state schema: {payload.get('schema')}"
            )
        if (
            int(payload.get("window_samples", -1)) != self.window_samples
            or int(payload.get("step_samples", -1)) != self.step_samples
        ):
            raise ValueError("continuous window state belongs to different window parameters")
        content = {key: value for key, value in payload.items() if key != "state_hash"}
        if payload.get("state_hash") != canonical_hash(content):
            raise ValueError("continuous window state hash mismatch")
        raw_values = payload.get("values")
        raw_mask = payload.get("validity_mask")
        if raw_values is None:
            values = None
            mask = None
        else:
            dtype = np.dtype(str(payload["dtype"]))
            mask = None if raw_mask is None else np.asarray(raw_mask, dtype=bool)
            invalid_placeholder = 0 if dtype.kind in {"i", "u"} else np.nan
            values = np.asarray(
                [
                    [invalid_placeholder if value is None else value for value in row]
                    for row in raw_values
                ],
                dtype=dtype,
            )
            if values.ndim != 2:
                raise ValueError("continuous window state values must be two-dimensional")
            if mask is not None and mask.shape != values.shape:
                raise ValueError("continuous window state validity shape mismatch")
        input_ids = [str(value) for value in payload.get("sample_input_ids", ())]
        available = [
            TimePoint.from_payload(value)
            for value in payload.get("sample_available_times", ())
        ]
        sample_count = 0 if values is None else int(values.shape[0])
        if len(input_ids) != sample_count or len(available) != sample_count:
            raise ValueError("continuous window state sample metadata length mismatch")
        first = payload.get("first_sample_time")
        channels = payload.get("channel_ids")
        self._values = None if values is None else values.copy()
        self._validity = None if mask is None else mask.copy()
        self._sample_input_ids = input_ids
        self._sample_available_times = available
        self._sequence_start = (
            None if payload.get("sequence_start") is None else int(payload["sequence_start"])
        )
        self._next_sequence = (
            None if payload.get("next_sequence") is None else int(payload["next_sequence"])
        )
        self._first_sample_time = None if first is None else TimePoint.from_payload(first)
        self._sample_period_seconds = (
            None
            if payload.get("sample_period_seconds") is None
            else float(payload["sample_period_seconds"])
        )
        self._stream_id = None if payload.get("stream_id") is None else str(payload["stream_id"])
        self._stream_revision = (
            None
            if payload.get("stream_revision") is None
            else int(payload["stream_revision"])
        )
        self._channel_ids = (
            None if channels is None else tuple(str(value) for value in channels)
        )
        self._clock_mapping_revisions = {
            str(key): int(value)
            for key, value in dict(payload.get("clock_mapping_revisions") or {}).items()
        }
        self._stream_revisions = {
            str(key): int(value)
            for key, value in dict(payload.get("stream_revisions") or {}).items()
        }


    def _validate_packet(self, packet: DenseSampleBatch, context: "ExecutionContext") -> None:
        if context.current_time.clock_id != packet.available_time.clock_id:
            raise ValueError("window context and packet availability must share a clock")
        if context.current_time.seconds < packet.available_time.seconds:
            raise ValueError("window builder cannot consume a packet before it is available")
        if packet.first_sample_time is None or packet.sample_period_seconds is None:
            raise ValueError("continuous dense windows currently require regular sample timing")
        if self._next_sequence is not None and packet.sequence_start != self._next_sequence:
            raise ValueError("continuous dense window input sequences must be contiguous")
        if self._stream_id is not None:
            if (
                packet.stream_id != self._stream_id
                or packet.stream_revision != self._stream_revision
                or packet.channel_ids != self._channel_ids
                or packet.first_sample_time.clock_id != self._first_sample_time.clock_id
                or packet.sample_period_seconds != self._sample_period_seconds
            ):
                raise ValueError("continuous dense window stream contract changed mid-buffer")

    def _append(self, packet: DenseSampleBatch) -> None:
        if self._values is None:
            self._values = np.array(packet.values, copy=True)
            self._validity = (
                None
                if packet.validity_mask is None
                else np.asarray(packet.validity_mask, dtype=bool).copy()
            )
            self._sequence_start = packet.sequence_start
            self._first_sample_time = packet.first_sample_time
            self._sample_period_seconds = packet.sample_period_seconds
            self._stream_id = packet.stream_id
            self._stream_revision = packet.stream_revision
            self._channel_ids = packet.channel_ids
        else:
            self._values = np.concatenate((self._values, packet.values), axis=0)
            if self._validity is not None or packet.validity_mask is not None:
                current = (
                    np.ones(self._values.shape[:1] + (len(packet.channel_ids),), dtype=bool)
                    if self._validity is None
                    else self._validity
                )
                incoming = (
                    np.ones(packet.values.shape, dtype=bool)
                    if packet.validity_mask is None
                    else packet.validity_mask
                )
                # ``current`` must describe the buffer before the new packet.
                prior_count = self._values.shape[0] - packet.sample_count
                if self._validity is None:
                    current = np.ones((prior_count, len(packet.channel_ids)), dtype=bool)
                self._validity = np.concatenate((current, incoming), axis=0)
        self._sample_input_ids.extend([packet.batch_id] * packet.sample_count)
        self._sample_available_times.extend([packet.available_time] * packet.sample_count)
        self._next_sequence = packet.sequence_end + 1
        if packet.lineage is not None:
            self._merge_revisions(
                self._clock_mapping_revisions,
                packet.lineage.clock_mapping_revisions,
                "clock mapping",
            )
            self._merge_revisions(
                self._stream_revisions,
                packet.lineage.stream_revisions,
                "stream",
            )
        self._merge_revisions(
            self._stream_revisions,
            {packet.stream_id: packet.stream_revision},
            "stream",
        )

    def _materialize(self, context: "ExecutionContext") -> DenseWindow:
        assert self._values is not None
        assert self._sequence_start is not None
        assert self._first_sample_time is not None
        assert self._sample_period_seconds is not None
        assert self._stream_id is not None
        assert self._stream_revision is not None
        assert self._channel_ids is not None
        input_ids = tuple(dict.fromkeys(self._sample_input_ids[: self.window_samples]))
        available = self._sample_available_times[0]
        for value in self._sample_available_times[1 : self.window_samples]:
            if value.clock_id != available.clock_id:
                raise ValueError("window inputs must use one availability clock")
            if value.seconds > available.seconds:
                available = value
        if context.current_time.clock_id != available.clock_id:
            raise ValueError("window output and input availability must share a clock")
        if context.current_time.seconds < available.seconds:
            raise ValueError("window cannot become available before all contributing inputs")
        self._merge_revisions(
            self._clock_mapping_revisions,
            context.clock_mapping_revisions,
            "clock mapping",
        )
        lineage = Lineage(
            component_id=context.component_id,
            component_version=context.component_version,
            input_ids=input_ids,
            latest_input_available_time=context.current_time,
            clock_mapping_revisions=self._clock_mapping_revisions,
            stream_revisions=self._stream_revisions,
        )
        return DenseWindow(
            window_id=context.next_id("window"),
            stream_id=self._stream_id,
            stream_revision=self._stream_revision,
            sequence_start=self._sequence_start,
            channel_ids=self._channel_ids,
            values=self._values[: self.window_samples],
            validity_mask=None
            if self._validity is None
            else self._validity[: self.window_samples],
            start_time=self._first_sample_time,
            end_time=TimePoint(
                self._first_sample_time.seconds
                + self.window_samples * self._sample_period_seconds,
                self._first_sample_time.clock_id,
            ),
            available_time=context.current_time,
            input_ids=input_ids,
            lineage=lineage,
        )

    def _discard(self, count: int) -> None:
        assert self._values is not None
        assert self._sequence_start is not None
        assert self._first_sample_time is not None
        assert self._sample_period_seconds is not None
        self._values = self._values[count:]
        if self._validity is not None:
            self._validity = self._validity[count:]
        del self._sample_input_ids[:count]
        del self._sample_available_times[:count]
        self._sequence_start += count
        self._first_sample_time = TimePoint(
            self._first_sample_time.seconds + count * self._sample_period_seconds,
            self._first_sample_time.clock_id,
        )

    @staticmethod
    def _merge_revisions(
        target: dict[str, int], source: Mapping[str, int], field: str
    ) -> None:
        for key, raw_value in source.items():
            value = int(raw_value)
            if key in target and target[key] != value:
                raise ValueError(f"{field} revision conflict for {key}")
            target[str(key)] = value


class EventWindowBuilder:
    """Causally join dense samples with delayed sparse-event windows."""

    def __init__(
        self,
        *,
        start_offset_seconds: float,
        end_offset_seconds: float,
        event_kinds: tuple[str, ...] = (),
        max_buffer_samples: int = 100_000,
    ) -> None:
        self.spec = EventWindowSpec(start_offset_seconds, end_offset_seconds)
        self.event_kinds = tuple(str(value) for value in event_kinds)
        self.max_buffer_samples = int(max_buffer_samples)
        if self.max_buffer_samples <= 0:
            raise ValueError("max_buffer_samples must be positive")
        self._packets: list[DenseSampleBatch] = []
        self._events: list[SparseEvent] = []

    def process(
        self, input_port: str, value: Any, context: "ExecutionContext"
    ) -> Mapping[str, tuple[DenseWindow, ...]]:
        if input_port == "samples":
            if not isinstance(value, DenseSampleBatch):
                raise TypeError("event windows samples port requires DenseSampleBatch")
            self._packets.append(value)
            if sum(packet.sample_count for packet in self._packets) > self.max_buffer_samples:
                raise RuntimeError("event window sample buffer exceeded max_buffer_samples")
        elif input_port == "events":
            if not isinstance(value, SparseEventBatch):
                raise TypeError("event windows events port requires SparseEventBatch")
            self._events.extend(
                event
                for event in value.events
                if not self.event_kinds or event.kind in self.event_kinds
            )
        else:
            raise ValueError(f"unknown event window input port {input_port}")
        ready: list[DenseWindow] = []
        pending: list[SparseEvent] = []
        for event in self._events:
            window = self._event_window(event, context)
            if window is None:
                pending.append(event)
            else:
                ready.append(window)
        self._events = pending
        return {"windows": tuple(ready)}

    def _event_window(
        self, event: SparseEvent, context: "ExecutionContext"
    ) -> DenseWindow | None:
        if not self._packets:
            return None
        first = self._packets[0]
        if first.first_sample_time is None or first.sample_period_seconds is None:
            raise ValueError("event windows require regular dense sample timing")
        sample_clock = first.first_sample_time.clock_id
        if event.event_time.clock_id != sample_clock:
            raise ValueError("event and dense sample times must share a mapped clock")
        start = event.event_time.seconds + self.spec.start_offset_seconds
        end = event.event_time.seconds + self.spec.end_offset_seconds
        selected: list[tuple[DenseSampleBatch, int]] = []
        latest_sample: float | None = None
        for packet in self._packets:
            self._validate_dense_contract(first, packet, sample_clock)
            assert packet.first_sample_time is not None
            assert packet.sample_period_seconds is not None
            for index in range(packet.sample_count):
                seconds = packet.first_sample_time.seconds + index * packet.sample_period_seconds
                latest_sample = seconds if latest_sample is None else max(latest_sample, seconds)
                if start <= seconds < end:
                    selected.append((packet, index))
        if latest_sample is None or latest_sample < end - first.sample_period_seconds:
            return None
        if not selected:
            raise ValueError(f"event {event.event_id} window contains no samples")
        packets = tuple(dict.fromkeys(packet.batch_id for packet, _ in selected))
        packet_by_id = {packet.batch_id: packet for packet, _ in selected}
        contributors = tuple(packet_by_id[value] for value in packets)
        availability = [event.available_time, *(packet.available_time for packet in contributors)]
        if len({value.clock_id for value in availability}) != 1:
            raise ValueError("event window input availability must share the execution clock")
        latest_available = max(availability, key=lambda value: value.seconds)
        any_mask = any(packet.validity_mask is not None for packet, _ in selected)
        validity = None
        if any_mask:
            validity = np.stack(
                [
                    np.ones(len(packet.channel_ids), dtype=bool)
                    if packet.validity_mask is None
                    else packet.validity_mask[index]
                    for packet, index in selected
                ]
            )
        input_ids = (event.event_id, *packets)
        return DenseWindow(
            window_id=context.next_id("window"),
            stream_id=first.stream_id,
            stream_revision=first.stream_revision,
            sequence_start=selected[0][0].sequence_start + selected[0][1],
            channel_ids=first.channel_ids,
            values=np.stack([packet.values[index] for packet, index in selected]),
            validity_mask=validity,
            start_time=TimePoint(start, sample_clock),
            end_time=TimePoint(end, sample_clock),
            available_time=latest_available,
            input_ids=input_ids,
            lineage=Lineage(
                component_id=context.component_id,
                component_version=context.component_version,
                input_ids=input_ids,
                latest_input_available_time=latest_available,
                clock_mapping_revisions=context.clock_mapping_revisions,
                stream_revisions={first.stream_id: first.stream_revision},
            ),
        )

    @staticmethod
    def _validate_dense_contract(
        first: DenseSampleBatch, packet: DenseSampleBatch, sample_clock: str
    ) -> None:
        if (
            packet.first_sample_time is None
            or packet.sample_period_seconds is None
            or packet.stream_id != first.stream_id
            or packet.stream_revision != first.stream_revision
            or packet.channel_ids != first.channel_ids
            or packet.first_sample_time.clock_id != sample_clock
            or packet.sample_period_seconds != first.sample_period_seconds
        ):
            raise ValueError("event window dense stream contract changed")

    def snapshot_state(self) -> Mapping[str, Any]:
        payload = {
            "schema": "eegle.event_window_state.v1",
            "spec": [self.spec.start_offset_seconds, self.spec.end_offset_seconds],
            "event_kinds": list(self.event_kinds),
            "max_buffer_samples": self.max_buffer_samples,
            "packets": [value.to_payload() for value in self._packets],
            "events": [value.to_payload() for value in self._events],
        }
        payload["state_hash"] = canonical_hash(payload)
        return payload

    def restore_state(self, payload: Mapping[str, Any]) -> None:
        content = {key: value for key, value in payload.items() if key != "state_hash"}
        if payload.get("schema") != "eegle.event_window_state.v1":
            raise ValueError("unsupported event window state")
        if payload.get("state_hash") != canonical_hash(content):
            raise ValueError("event window state hash mismatch")
        if (
            tuple(float(value) for value in payload["spec"])
            != (self.spec.start_offset_seconds, self.spec.end_offset_seconds)
            or tuple(str(value) for value in payload.get("event_kinds", ())) != self.event_kinds
            or int(payload["max_buffer_samples"]) != self.max_buffer_samples
        ):
            raise ValueError("event window state belongs to different parameters")
        self._packets = [DenseSampleBatch.from_payload(value) for value in payload["packets"]]
        self._events = [SparseEvent.from_payload(value) for value in payload["events"]]
