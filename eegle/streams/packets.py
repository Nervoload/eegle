"""Versioned dense, sparse, and metadata packet records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, TypeAlias, Union

import numpy as np

from eegle._domain import Lineage
from eegle._validation import freeze_json, require_finite, require_identifier, thaw_json
from eegle.streams.clocks import TimePoint


DENSE_SAMPLE_BATCH_SCHEMA = "eegle.dense_sample_batch.v1"
SPARSE_EVENT_BATCH_SCHEMA = "eegle.sparse_event_batch.v1"
METADATA_EVENT_SCHEMA = "eegle.metadata_event.v1"


def _validate_boundary_times(received_time: TimePoint, available_time: TimePoint) -> None:
    if received_time.clock_id != available_time.clock_id:
        raise ValueError("received_time and available_time must share a boundary clock")
    if available_time.seconds < received_time.seconds:
        raise ValueError("available_time cannot precede received_time")


@dataclass(frozen=True, slots=True, eq=False)
class DenseSampleBatch:
    batch_id: str
    stream_id: str
    stream_revision: int
    sequence_start: int
    channel_ids: tuple[str, ...]
    values: np.ndarray
    received_time: TimePoint
    available_time: TimePoint
    first_sample_time: TimePoint | None = None
    sample_period_seconds: float | None = None
    sample_times: tuple[TimePoint, ...] = ()
    validity_mask: np.ndarray | None = None
    lineage: Lineage | None = None
    schema: str = DENSE_SAMPLE_BATCH_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != DENSE_SAMPLE_BATCH_SCHEMA:
            raise ValueError(f"unsupported dense sample batch schema: {self.schema}")
        object.__setattr__(self, "batch_id", require_identifier(self.batch_id, "batch_id"))
        object.__setattr__(self, "stream_id", require_identifier(self.stream_id, "stream_id"))
        object.__setattr__(self, "stream_revision", int(self.stream_revision))
        if self.stream_revision <= 0:
            raise ValueError("stream_revision must be positive")
        if int(self.sequence_start) < 0:
            raise ValueError("sequence_start cannot be negative")
        object.__setattr__(self, "sequence_start", int(self.sequence_start))
        channels = tuple(require_identifier(value, "channel_id") for value in self.channel_ids)
        if not channels or len(channels) != len(set(channels)):
            raise ValueError("channel_ids must be non-empty and unique")
        object.__setattr__(self, "channel_ids", channels)
        values = np.array(self.values, copy=True)
        if values.ndim != 2 or values.shape[0] == 0:
            raise ValueError("dense values must be a non-empty samples x channels array")
        if values.shape[1] != len(channels):
            raise ValueError("dense values second dimension must match channel_ids")
        if not np.issubdtype(values.dtype, np.number):
            raise ValueError("dense values must use a numeric dtype")
        mask = None
        if self.validity_mask is not None:
            mask = np.asarray(self.validity_mask, dtype=bool).copy()
            if mask.shape != values.shape:
                raise ValueError("validity_mask shape must match dense values")
        finite = np.isfinite(values)
        if not bool(np.all(finite)):
            if mask is None:
                raise ValueError("non-finite dense values require an explicit validity_mask")
            if bool(np.any(mask & ~finite)):
                raise ValueError("non-finite dense values must be marked invalid")
        values.setflags(write=False)
        object.__setattr__(self, "values", values)
        if mask is not None:
            mask.setflags(write=False)
            object.__setattr__(self, "validity_mask", mask)
        _validate_boundary_times(self.received_time, self.available_time)
        uses_regular = self.first_sample_time is not None or self.sample_period_seconds is not None
        uses_explicit = bool(self.sample_times)
        if uses_regular == uses_explicit:
            raise ValueError(
                "dense timing must use exactly one of first_sample_time/sample_period_seconds "
                "or explicit sample_times"
            )
        if uses_regular:
            if self.first_sample_time is None or self.sample_period_seconds is None:
                raise ValueError("regular timing requires first_sample_time and sample_period_seconds")
            object.__setattr__(
                self,
                "sample_period_seconds",
                require_finite(self.sample_period_seconds, "sample_period_seconds"),
            )
            if self.sample_period_seconds <= 0:
                raise ValueError("sample_period_seconds must be positive")
        else:
            if len(self.sample_times) != values.shape[0]:
                raise ValueError("explicit sample_times must match the sample count")
            clock_ids = {point.clock_id for point in self.sample_times}
            if len(clock_ids) != 1:
                raise ValueError("all explicit sample_times must share one clock")
            seconds = [point.seconds for point in self.sample_times]
            if seconds != sorted(seconds):
                raise ValueError("explicit sample_times must be nondecreasing")

    @property
    def sample_count(self) -> int:
        return int(self.values.shape[0])

    @property
    def sequence_end(self) -> int:
        return self.sequence_start + self.sample_count - 1

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DenseSampleBatch):
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
            and self.batch_id == other.batch_id
            and self.stream_id == other.stream_id
            and self.stream_revision == other.stream_revision
            and self.sequence_start == other.sequence_start
            and self.channel_ids == other.channel_ids
            and self.values.dtype == other.values.dtype
            and np.array_equal(self.values, other.values, equal_nan=True)
            and masks_equal
            and self.received_time == other.received_time
            and self.available_time == other.available_time
            and self.first_sample_time == other.first_sample_time
            and self.sample_period_seconds == other.sample_period_seconds
            and self.sample_times == other.sample_times
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
            "batch_id": self.batch_id,
            "stream_id": self.stream_id,
            "stream_revision": self.stream_revision,
            "sequence_start": self.sequence_start,
            "channel_ids": list(self.channel_ids),
            "dtype": self.values.dtype.name,
            "shape": list(self.values.shape),
            "values": rows,
            "validity_mask": None if mask is None else mask.astype(bool).tolist(),
            "received_time": self.received_time.to_payload(),
            "available_time": self.available_time.to_payload(),
            "first_sample_time": None
            if self.first_sample_time is None
            else self.first_sample_time.to_payload(),
            "sample_period_seconds": self.sample_period_seconds,
            "sample_times": [point.to_payload() for point in self.sample_times],
            "lineage": None if self.lineage is None else self.lineage.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DenseSampleBatch":
        dtype = np.dtype(str(payload["dtype"]))
        raw_rows = payload["values"]
        mask_payload = payload.get("validity_mask")
        mask = None if mask_payload is None else np.asarray(mask_payload, dtype=bool)
        invalid_placeholder = 0 if dtype.kind in {"i", "u"} else np.nan
        prepared = [
            [invalid_placeholder if value is None else value for value in row]
            for row in raw_rows
        ]
        values = np.asarray(prepared, dtype=dtype)
        expected_shape = tuple(int(value) for value in payload["shape"])
        if values.shape != expected_shape:
            raise ValueError("dense payload shape does not match encoded values")
        first = payload.get("first_sample_time")
        lineage = payload.get("lineage")
        return cls(
            schema=str(payload.get("schema", DENSE_SAMPLE_BATCH_SCHEMA)),
            batch_id=str(payload["batch_id"]),
            stream_id=str(payload["stream_id"]),
            stream_revision=int(payload["stream_revision"]),
            sequence_start=int(payload["sequence_start"]),
            channel_ids=tuple(str(value) for value in payload["channel_ids"]),
            values=values,
            validity_mask=mask,
            received_time=TimePoint.from_payload(payload["received_time"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            first_sample_time=None if first is None else TimePoint.from_payload(first),
            sample_period_seconds=None
            if payload.get("sample_period_seconds") is None
            else float(payload["sample_period_seconds"]),
            sample_times=tuple(TimePoint.from_payload(item) for item in payload.get("sample_times", ())),
            lineage=None if lineage is None else Lineage.from_payload(lineage),
        )


@dataclass(frozen=True, slots=True)
class SparseEvent:
    event_id: str
    kind: str
    event_time: TimePoint
    received_time: TimePoint
    available_time: TimePoint
    value: Any = None
    source_time: TimePoint | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", require_identifier(self.event_id, "event_id"))
        if not self.kind.strip():
            raise ValueError("event kind cannot be empty")
        _validate_boundary_times(self.received_time, self.available_time)
        object.__setattr__(self, "value", freeze_json(self.value))

    def to_payload(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "event_time": self.event_time.to_payload(),
            "source_time": None if self.source_time is None else self.source_time.to_payload(),
            "received_time": self.received_time.to_payload(),
            "available_time": self.available_time.to_payload(),
            "value": thaw_json(self.value),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SparseEvent":
        source = payload.get("source_time")
        return cls(
            event_id=str(payload["event_id"]),
            kind=str(payload["kind"]),
            event_time=TimePoint.from_payload(payload["event_time"]),
            source_time=None if source is None else TimePoint.from_payload(source),
            received_time=TimePoint.from_payload(payload["received_time"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            value=payload.get("value"),
        )


@dataclass(frozen=True, slots=True)
class SparseEventBatch:
    batch_id: str
    stream_id: str
    stream_revision: int
    sequence_start: int
    events: tuple[SparseEvent, ...]
    lineage: Lineage | None = None
    schema: str = SPARSE_EVENT_BATCH_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SPARSE_EVENT_BATCH_SCHEMA:
            raise ValueError(f"unsupported sparse event batch schema: {self.schema}")
        object.__setattr__(self, "batch_id", require_identifier(self.batch_id, "batch_id"))
        object.__setattr__(self, "stream_id", require_identifier(self.stream_id, "stream_id"))
        object.__setattr__(self, "stream_revision", int(self.stream_revision))
        if self.stream_revision <= 0:
            raise ValueError("stream_revision must be positive")
        if int(self.sequence_start) < 0:
            raise ValueError("sequence_start cannot be negative")
        object.__setattr__(self, "sequence_start", int(self.sequence_start))
        if not self.events:
            raise ValueError("sparse event batch cannot be empty")
        event_ids = tuple(event.event_id for event in self.events)
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("sparse event identities must be unique within a batch")

    @property
    def sequence_end(self) -> int:
        return self.sequence_start + len(self.events) - 1

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "batch_id": self.batch_id,
            "stream_id": self.stream_id,
            "stream_revision": self.stream_revision,
            "sequence_start": self.sequence_start,
            "events": [event.to_payload() for event in self.events],
            "lineage": None if self.lineage is None else self.lineage.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SparseEventBatch":
        lineage = payload.get("lineage")
        return cls(
            schema=str(payload.get("schema", SPARSE_EVENT_BATCH_SCHEMA)),
            batch_id=str(payload["batch_id"]),
            stream_id=str(payload["stream_id"]),
            stream_revision=int(payload["stream_revision"]),
            sequence_start=int(payload["sequence_start"]),
            events=tuple(SparseEvent.from_payload(item) for item in payload["events"]),
            lineage=None if lineage is None else Lineage.from_payload(lineage),
        )


@dataclass(frozen=True, slots=True)
class MetadataEvent:
    event_id: str
    stream_id: str
    stream_revision: int
    sequence: int
    kind: str
    event_time: TimePoint
    received_time: TimePoint
    available_time: TimePoint
    metadata: Mapping[str, Any]
    lineage: Lineage | None = None
    schema: str = METADATA_EVENT_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != METADATA_EVENT_SCHEMA:
            raise ValueError(f"unsupported metadata event schema: {self.schema}")
        object.__setattr__(self, "event_id", require_identifier(self.event_id, "event_id"))
        object.__setattr__(self, "stream_id", require_identifier(self.stream_id, "stream_id"))
        object.__setattr__(self, "stream_revision", int(self.stream_revision))
        if self.stream_revision <= 0:
            raise ValueError("stream_revision must be positive")
        if int(self.sequence) < 0:
            raise ValueError("metadata sequence cannot be negative")
        object.__setattr__(self, "sequence", int(self.sequence))
        if not self.kind.strip():
            raise ValueError("metadata kind cannot be empty")
        _validate_boundary_times(self.received_time, self.available_time)
        object.__setattr__(self, "metadata", freeze_json(self.metadata))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "stream_id": self.stream_id,
            "stream_revision": self.stream_revision,
            "sequence": self.sequence,
            "kind": self.kind,
            "event_time": self.event_time.to_payload(),
            "received_time": self.received_time.to_payload(),
            "available_time": self.available_time.to_payload(),
            "metadata": thaw_json(self.metadata),
            "lineage": None if self.lineage is None else self.lineage.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "MetadataEvent":
        lineage = payload.get("lineage")
        return cls(
            schema=str(payload.get("schema", METADATA_EVENT_SCHEMA)),
            event_id=str(payload["event_id"]),
            stream_id=str(payload["stream_id"]),
            stream_revision=int(payload["stream_revision"]),
            sequence=int(payload["sequence"]),
            kind=str(payload["kind"]),
            event_time=TimePoint.from_payload(payload["event_time"]),
            received_time=TimePoint.from_payload(payload["received_time"]),
            available_time=TimePoint.from_payload(payload["available_time"]),
            metadata=dict(payload["metadata"]),
            lineage=None if lineage is None else Lineage.from_payload(lineage),
        )


Packet: TypeAlias = Union[DenseSampleBatch, SparseEventBatch, MetadataEvent]
