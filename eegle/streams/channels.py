"""Modality-neutral channel and stream descriptions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

import numpy as np

from eegle._validation import freeze_json, require_finite, require_identifier, thaw_json


CHANNEL_SPEC_SCHEMA = "eegle.channel_spec.v1"
STREAM_SPEC_SCHEMA = "eegle.stream_spec.v1"


class ContentKind(str, Enum):
    DENSE_SAMPLES = "dense_samples"
    SPARSE_EVENTS = "sparse_events"
    METADATA = "metadata"


class RateModel(str, Enum):
    REGULAR = "regular"
    IRREGULAR = "irregular"
    EVENT = "event"


class MissingDataPolicy(str, Enum):
    FORBID = "forbid"
    VALIDITY_MASK = "validity_mask"


@dataclass(frozen=True, slots=True)
class ChannelSpec:
    channel_id: str
    kind: str
    unit: str
    name: str | None = None
    sensor_reference: str | None = None
    anatomical_reference: str | None = None
    geometry: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = CHANNEL_SPEC_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CHANNEL_SPEC_SCHEMA:
            raise ValueError(f"unsupported channel spec schema: {self.schema}")
        object.__setattr__(self, "channel_id", require_identifier(self.channel_id, "channel_id"))
        if not self.kind.strip():
            raise ValueError("channel kind cannot be empty")
        if not self.unit.strip():
            raise ValueError("channel unit cannot be empty")
        object.__setattr__(self, "geometry", freeze_json(self.geometry or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "channel_id": self.channel_id,
            "kind": self.kind,
            "unit": self.unit,
            "name": self.name,
            "sensor_reference": self.sensor_reference,
            "anatomical_reference": self.anatomical_reference,
            "geometry": thaw_json(self.geometry),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ChannelSpec":
        return cls(
            schema=str(payload.get("schema", CHANNEL_SPEC_SCHEMA)),
            channel_id=str(payload["channel_id"]),
            kind=str(payload["kind"]),
            unit=str(payload["unit"]),
            name=None if payload.get("name") is None else str(payload["name"]),
            sensor_reference=None
            if payload.get("sensor_reference") is None
            else str(payload["sensor_reference"]),
            anatomical_reference=None
            if payload.get("anatomical_reference") is None
            else str(payload["anatomical_reference"]),
            geometry=dict(payload.get("geometry") or {}),
        )


@dataclass(frozen=True, slots=True)
class StreamSpec:
    stream_id: str
    revision: int
    modality: str
    content_kind: ContentKind
    rate_model: RateModel
    clock_id: str
    channels: tuple[ChannelSpec, ...] = ()
    sample_rate_hz: float | None = None
    sample_dtype: str | None = None
    missing_data_policy: MissingDataPolicy = MissingDataPolicy.FORBID
    coordinate_frame: str | None = None
    geometry_reference: str | None = None
    geometry_revision: str | None = None
    metadata: Mapping[str, Any] = None  # type: ignore[assignment]
    schema: str = STREAM_SPEC_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != STREAM_SPEC_SCHEMA:
            raise ValueError(f"unsupported stream spec schema: {self.schema}")
        object.__setattr__(self, "stream_id", require_identifier(self.stream_id, "stream_id"))
        object.__setattr__(self, "revision", int(self.revision))
        if self.revision <= 0:
            raise ValueError("stream revision must be positive")
        object.__setattr__(self, "clock_id", require_identifier(self.clock_id, "clock_id"))
        object.__setattr__(self, "content_kind", ContentKind(self.content_kind))
        object.__setattr__(self, "rate_model", RateModel(self.rate_model))
        object.__setattr__(self, "missing_data_policy", MissingDataPolicy(self.missing_data_policy))
        if not self.modality.strip():
            raise ValueError("stream modality cannot be empty")
        channel_ids = tuple(channel.channel_id for channel in self.channels)
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("stream channel identities must be unique")
        if self.content_kind == ContentKind.DENSE_SAMPLES and not self.channels:
            raise ValueError("dense sample streams require at least one channel")
        if self.content_kind == ContentKind.DENSE_SAMPLES:
            if self.sample_dtype is None:
                raise ValueError("dense sample streams require sample_dtype")
            dtype = np.dtype(self.sample_dtype)
            if not np.issubdtype(dtype, np.number):
                raise ValueError("dense sample stream dtype must be numeric")
            object.__setattr__(self, "sample_dtype", dtype.name)
        elif self.sample_dtype is not None:
            raise ValueError("sample_dtype is only valid for dense sample streams")
        if self.rate_model == RateModel.REGULAR:
            if self.sample_rate_hz is None:
                raise ValueError("regular streams require sample_rate_hz")
            object.__setattr__(
                self, "sample_rate_hz", require_finite(self.sample_rate_hz, "sample_rate_hz")
            )
            if self.sample_rate_hz <= 0:
                raise ValueError("sample_rate_hz must be positive")
        elif self.sample_rate_hz is not None:
            raise ValueError("sample_rate_hz is only valid for regular streams")
        for field in ("coordinate_frame", "geometry_reference", "geometry_revision"):
            value = getattr(self, field)
            if value is not None and not str(value).strip():
                raise ValueError(f"{field} cannot be empty")
        object.__setattr__(self, "metadata", freeze_json(self.metadata or {}))

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "stream_id": self.stream_id,
            "revision": self.revision,
            "modality": self.modality,
            "content_kind": self.content_kind.value,
            "rate_model": self.rate_model.value,
            "clock_id": self.clock_id,
            "channels": [channel.to_payload() for channel in self.channels],
            "sample_rate_hz": self.sample_rate_hz,
            "sample_dtype": self.sample_dtype,
            "missing_data_policy": self.missing_data_policy.value,
            "coordinate_frame": self.coordinate_frame,
            "geometry_reference": self.geometry_reference,
            "geometry_revision": self.geometry_revision,
            "metadata": thaw_json(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StreamSpec":
        return cls(
            schema=str(payload.get("schema", STREAM_SPEC_SCHEMA)),
            stream_id=str(payload["stream_id"]),
            revision=int(payload["revision"]),
            modality=str(payload["modality"]),
            content_kind=ContentKind(str(payload["content_kind"])),
            rate_model=RateModel(str(payload["rate_model"])),
            clock_id=str(payload["clock_id"]),
            channels=tuple(ChannelSpec.from_payload(item) for item in payload.get("channels", ())),
            sample_rate_hz=None
            if payload.get("sample_rate_hz") is None
            else float(payload["sample_rate_hz"]),
            sample_dtype=None
            if payload.get("sample_dtype") is None
            else str(payload["sample_dtype"]),
            missing_data_policy=MissingDataPolicy(
                str(payload.get("missing_data_policy", MissingDataPolicy.FORBID.value))
            ),
            coordinate_frame=None
            if payload.get("coordinate_frame") is None
            else str(payload["coordinate_frame"]),
            geometry_reference=None
            if payload.get("geometry_reference") is None
            else str(payload["geometry_reference"]),
            geometry_revision=None
            if payload.get("geometry_revision") is None
            else str(payload["geometry_revision"]),
            metadata=dict(payload.get("metadata") or {}),
        )
