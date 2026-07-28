"""Optional Lab Streaming Layer adapters over EEGle's general contracts.

``pylsl`` is imported only when a detector, source, or outlet is constructed.
Importing this module, the base package, or loading descriptor metadata remains
safe when the optional native dependency is absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import importlib
import json
import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from eegle._domain import ComponentKind, Determinism, EquivalenceLevel, ExecutionMode
from eegle._validation import freeze_json, require_identifier, thaw_json
from eegle.authoring import SourceKind, SourceLocation
from eegle.operations.discovery import (
    ClockCapability,
    SourceCapability,
    signal_contract_from_stream,
)
from eegle.plugins import (
    PluginCapabilities,
    PluginDescriptor,
    PortSpec,
    StateBehavior,
)
from eegle.specs import ClockMappingBinding, ClockMappingStrategy, Placement
from eegle.streams import (
    ChannelSpec,
    ContentKind,
    DenseSampleBatch,
    MetadataEvent,
    MissingDataPolicy,
    RateModel,
    SparseEvent,
    SparseEventBatch,
    StreamSpec,
    TimePoint,
)


LSL_DENSE_SOURCE_PLUGIN_ID = "eegle.integrations.lsl_dense_source"
LSL_SPARSE_SOURCE_PLUGIN_ID = "eegle.integrations.lsl_sparse_source"
LSL_METADATA_SOURCE_PLUGIN_ID = "eegle.integrations.lsl_metadata_source"
LSL_DENSE_OUTLET_PLUGIN_ID = "eegle.integrations.lsl_dense_outlet"
LSL_SPARSE_OUTLET_PLUGIN_ID = "eegle.integrations.lsl_sparse_outlet"
LSL_METADATA_OUTLET_PLUGIN_ID = "eegle.integrations.lsl_metadata_outlet"
LSL_PLUGIN_VERSION = "0.1.0"
LSL_CLOCK_ID = "clock.lsl.local"
LSL_SUPPORT_SCHEMA_ID = "eegle.lsl_support.v1"

_DENSE = "eegle.dense_sample_batch.v1"
_SPARSE = "eegle.sparse_event_batch.v1"
_METADATA = "eegle.metadata_event.v1"
_SCHEMA = "https://json-schema.org/draft/2020-12/schema"
_SELECTOR_FIELDS = ("uid", "source_id", "name", "type", "hostname")


class LslSupportLevel(str, Enum):
    UNAVAILABLE = "unavailable"
    SIMULATED_VALIDATED = "simulated_validated"
    LIVE_OBSERVE_ONLY_VALIDATED = "live_observe_only_validated"


@dataclass(frozen=True, slots=True)
class LslSupportReport:
    dependency_available: bool
    library_version: str | None
    support_level: LslSupportLevel
    real_acceptance_id: str | None = None
    schema: str = LSL_SUPPORT_SCHEMA_ID

    def __post_init__(self) -> None:
        if self.schema != LSL_SUPPORT_SCHEMA_ID:
            raise ValueError(f"unsupported LSL support schema: {self.schema}")
        object.__setattr__(self, "support_level", LslSupportLevel(self.support_level))
        if not self.dependency_available and self.support_level != LslSupportLevel.UNAVAILABLE:
            raise ValueError("LSL cannot be supported while its dependency is unavailable")
        if self.support_level == LslSupportLevel.LIVE_OBSERVE_ONLY_VALIDATED:
            if self.real_acceptance_id is None:
                raise ValueError("live LSL validation requires an acceptance evidence identity")
        elif self.real_acceptance_id is not None:
            raise ValueError("real acceptance identity is valid only for live validation")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "dependency_available": self.dependency_available,
            "library_version": self.library_version,
            "support_level": self.support_level.value,
            "real_acceptance_id": self.real_acceptance_id,
        }


@dataclass(frozen=True, slots=True)
class LslStreamIdentity:
    name: str
    stream_type: str
    channel_count: int
    nominal_rate_hz: float
    channel_format: str
    source_id: str
    uid: str
    hostname: str

    @property
    def selector(self) -> Mapping[str, str]:
        strongest = {"uid": self.uid} if self.uid else {"source_id": self.source_id}
        if not any(strongest.values()):
            strongest = {"name": self.name, "type": self.stream_type, "hostname": self.hostname}
        return freeze_json(strongest)

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.stream_type,
            "channel_count": self.channel_count,
            "nominal_rate_hz": self.nominal_rate_hz,
            "channel_format": self.channel_format,
            "source_id": self.source_id,
            "uid": self.uid,
            "hostname": self.hostname,
        }


@dataclass(frozen=True, slots=True)
class LslClockObservation:
    correction_seconds: float
    measured_at_seconds: float
    uncertainty_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class LslPacketLoss:
    expected_period_seconds: float
    observed_gap_seconds: float
    estimated_missing_samples: int
    previous_timestamp: float
    current_timestamp: float


@dataclass(frozen=True, slots=True)
class LslDetection:
    sources: tuple[SourceCapability, ...]
    clocks: tuple[ClockCapability, ...]
    support: LslSupportReport
    streams: tuple[LslStreamIdentity, ...]

    def observations_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.lsl_detection.v1",
            "sources": [value.to_payload() for value in self.sources],
            "storage": [],
            "clocks": [value.to_payload() for value in self.clocks],
            "placements": [],
            "authorization_providers": [],
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            **self.observations_payload(),
            "support": self.support.to_payload(),
            "streams": [value.to_payload() for value in self.streams],
        }


def lsl_plugin_descriptors() -> tuple[PluginDescriptor, ...]:
    """Return dependency-lazy descriptors for three source and outlet types."""

    capabilities = PluginCapabilities(
        supported_modes=frozenset({ExecutionMode.CAUSAL}),
        determinism=Determinism.EXTERNAL,
        equivalence=EquivalenceLevel.TRACE,
        state_behavior=StateBehavior.EXTERNAL,
        resources=("lsl_stream",),
    )
    source_schema = {
        "$schema": _SCHEMA,
        "type": "object",
        "properties": {
            "stream_spec": {"type": "object"},
            "selector": _selector_schema(),
            "boundary_clock_id": {"type": "string", "minLength": 1},
            "pull_timeout_seconds": {"type": "number", "minimum": 0},
            "max_samples": {"type": "integer", "minimum": 1},
            "reconnect_attempts": {"type": "integer", "minimum": 0},
            "resolve_timeout_seconds": {"type": "number", "minimum": 0},
            "dejitter": {"type": "boolean"},
            "gap_tolerance": {"type": "number", "minimum": 1},
        },
        "required": ["stream_spec", "selector"],
        "additionalProperties": False,
    }
    outlet_schema = {
        "$schema": _SCHEMA,
        "type": "object",
        "properties": {
            "name": {"type": "string", "minLength": 1},
            "type": {"type": "string", "minLength": 1},
            "source_id": {"type": "string", "minLength": 1},
            "channel_count": {"type": "integer", "minimum": 1},
            "nominal_rate_hz": {"type": "number", "minimum": 0},
            "channel_format": {"type": "string", "minLength": 1},
        },
        "required": ["name", "type", "source_id", "channel_count", "nominal_rate_hz", "channel_format"],
        "additionalProperties": False,
    }
    sources = (
        (LSL_DENSE_SOURCE_PLUGIN_ID, _DENSE, "samples", ContentKind.DENSE_SAMPLES),
        (LSL_SPARSE_SOURCE_PLUGIN_ID, _SPARSE, "events", ContentKind.SPARSE_EVENTS),
        (LSL_METADATA_SOURCE_PLUGIN_ID, _METADATA, "metadata", ContentKind.METADATA),
    )
    outlets = (
        (LSL_DENSE_OUTLET_PLUGIN_ID, _DENSE),
        (LSL_SPARSE_OUTLET_PLUGIN_ID, _SPARSE),
        (LSL_METADATA_OUTLET_PLUGIN_ID, _METADATA),
    )
    values: list[PluginDescriptor] = []
    for plugin_id, type_id, port_name, content_kind in sources:
        values.append(
            PluginDescriptor(
                plugin_id,
                LSL_PLUGIN_VERSION,
                ComponentKind.SOURCE,
                source_schema,
                (),
                (PortSpec(port_name, type_id, multiple=True),),
                capabilities,
                lambda config, kind=content_kind: LslSource(config, expected_kind=kind),
                "eegle.integrations.lsl:LslSource",
                distribution="eegle[live]",
            )
        )
    for plugin_id, type_id in outlets:
        values.append(
            PluginDescriptor(
                plugin_id,
                LSL_PLUGIN_VERSION,
                ComponentKind.SINK,
                outlet_schema,
                (PortSpec("records", type_id, multiple=True),),
                (),
                capabilities,
                LslOutlet,
                "eegle.integrations.lsl:LslOutlet",
                distribution="eegle[live]",
            )
        )
    return tuple(values)


class LslSource:
    """Dense, sparse, or metadata source selected by one exact LSL identity."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        expected_kind: ContentKind | None = None,
        pylsl_module: Any | None = None,
    ) -> None:
        self._config = thaw_json(freeze_json(config))
        self._stream_spec = StreamSpec.from_payload(self._config["stream_spec"])
        if expected_kind is not None and self._stream_spec.content_kind != expected_kind:
            raise ValueError("LSL source descriptor does not match stream content kind")
        self._selector = exact_selector(self._config["selector"])
        self._boundary_clock_id = str(
            self._config.get("boundary_clock_id", self._stream_spec.clock_id)
        )
        self._timeout = float(self._config.get("pull_timeout_seconds", 0.0))
        self._max_samples = int(self._config.get("max_samples", 1024))
        self._reconnect_attempts = int(self._config.get("reconnect_attempts", 2))
        self._resolve_timeout = float(self._config.get("resolve_timeout_seconds", 1.0))
        self._gap_tolerance = float(self._config.get("gap_tolerance", 1.5))
        self._pylsl = pylsl_module or _import_pylsl()
        self._inlet: Any | None = None
        self._closed = False
        self._sequence = 0
        self._batch = 0
        self._last_timestamp: float | None = None
        self._losses: list[LslPacketLoss] = []
        self._reconnect_count = 0
        self._open()

    @property
    def stream_spec(self) -> StreamSpec:
        return self._stream_spec

    @property
    def packet_loss_observations(self) -> tuple[LslPacketLoss, ...]:
        return tuple(self._losses)

    @property
    def reconnect_count(self) -> int:
        return self._reconnect_count

    def clock_observation(self, *, timeout_seconds: float = 1.0) -> LslClockObservation:
        if self._inlet is None:
            raise RuntimeError("LSL inlet is closed")
        correction = float(self._inlet.time_correction(timeout=timeout_seconds))
        return LslClockObservation(correction, float(self._pylsl.local_clock()))

    def read(self) -> DenseSampleBatch | SparseEventBatch | MetadataEvent | None:
        if self._closed:
            return None
        for attempt in range(self._reconnect_attempts + 1):
            try:
                samples, timestamps = self._inlet.pull_chunk(
                    timeout=self._timeout,
                    max_samples=self._max_samples,
                )
                break
            except Exception:
                if attempt >= self._reconnect_attempts:
                    raise
                self._reconnect_count += 1
                self._close_inlet()
                self._open()
        if not timestamps:
            return None
        if len(samples) != len(timestamps):
            raise ValueError("LSL chunk sample and timestamp counts differ")
        now = float(self._pylsl.local_clock())
        missing = self._observe_gap(float(timestamps[0]))
        self._sequence += missing
        sequence = self._sequence
        self._sequence += len(timestamps)
        self._last_timestamp = float(timestamps[-1])
        self._batch += 1
        boundary = TimePoint(now, self._boundary_clock_id)
        kind = self._stream_spec.content_kind
        if kind == ContentKind.DENSE_SAMPLES:
            values = np.asarray(samples, dtype=self._stream_spec.sample_dtype)
            if values.ndim == 1:
                values = values.reshape(-1, 1)
            return DenseSampleBatch(
                batch_id=f"batch.{self._stream_spec.stream_id}.{self._batch}",
                stream_id=self._stream_spec.stream_id,
                stream_revision=self._stream_spec.revision,
                sequence_start=sequence,
                channel_ids=tuple(value.channel_id for value in self._stream_spec.channels),
                values=values,
                received_time=boundary,
                available_time=boundary,
                sample_times=tuple(
                    TimePoint(float(value), self._stream_spec.clock_id) for value in timestamps
                ),
            )
        if kind == ContentKind.SPARSE_EVENTS:
            events = tuple(
                SparseEvent(
                    event_id=f"event.{self._stream_spec.stream_id}.{sequence + index}",
                    kind="lsl_marker",
                    event_time=TimePoint(float(timestamp), self._stream_spec.clock_id),
                    source_time=TimePoint(float(timestamp), self._stream_spec.clock_id),
                    received_time=boundary,
                    available_time=boundary,
                    value=sample[0] if isinstance(sample, Sequence) and len(sample) == 1 else sample,
                )
                for index, (sample, timestamp) in enumerate(zip(samples, timestamps))
            )
            return SparseEventBatch(
                batch_id=f"batch.{self._stream_spec.stream_id}.{self._batch}",
                stream_id=self._stream_spec.stream_id,
                stream_revision=self._stream_spec.revision,
                sequence_start=sequence,
                events=events,
            )
        raw = samples[-1]
        value = raw[0] if isinstance(raw, Sequence) and len(raw) == 1 else raw
        if isinstance(value, str):
            try:
                metadata = json.loads(value)
            except json.JSONDecodeError:
                metadata = {"value": value}
        elif isinstance(value, Mapping):
            metadata = dict(value)
        else:
            metadata = {"value": value}
        return MetadataEvent(
            event_id=f"metadata.{self._stream_spec.stream_id}.{sequence + len(samples) - 1}",
            stream_id=self._stream_spec.stream_id,
            stream_revision=self._stream_spec.revision,
            sequence=sequence + len(samples) - 1,
            kind="lsl_metadata",
            event_time=TimePoint(float(timestamps[-1]), self._stream_spec.clock_id),
            received_time=boundary,
            available_time=boundary,
            metadata=metadata,
        )

    def close(self) -> None:
        self._closed = True
        self._close_inlet()

    def _open(self) -> None:
        infos = tuple(self._pylsl.resolve_streams(wait_time=self._resolve_timeout))
        info = select_exact_stream(infos, self._selector)
        flags = int(getattr(self._pylsl, "proc_clocksync", 0))
        if bool(self._config.get("dejitter", False)):
            flags |= int(getattr(self._pylsl, "proc_dejitter", 0))
            flags |= int(getattr(self._pylsl, "proc_monotonize", 0))
        self._inlet = self._pylsl.StreamInlet(
            info,
            recover=True,
            processing_flags=flags,
        )

    def _close_inlet(self) -> None:
        inlet, self._inlet = self._inlet, None
        if inlet is not None:
            close = getattr(inlet, "close_stream", None)
            if callable(close):
                close()

    def _observe_gap(self, timestamp: float) -> int:
        if (
            self._last_timestamp is None
            or self._stream_spec.sample_rate_hz is None
            or self._stream_spec.rate_model != RateModel.REGULAR
        ):
            return 0
        period = 1.0 / self._stream_spec.sample_rate_hz
        gap = timestamp - self._last_timestamp
        if gap <= period * self._gap_tolerance:
            return 0
        missing = max(1, int(round(gap / period)) - 1)
        self._losses.append(
            LslPacketLoss(period, gap, missing, self._last_timestamp, timestamp)
        )
        return missing


class LslOutlet:
    """Generic LSL sink for dense packets, sparse events, or metadata."""

    def __init__(self, config: Mapping[str, Any], *, pylsl_module: Any | None = None) -> None:
        self._config = thaw_json(freeze_json(config))
        self._pylsl = pylsl_module or _import_pylsl()
        info = self._pylsl.StreamInfo(
            self._config["name"],
            self._config["type"],
            int(self._config["channel_count"]),
            float(self._config["nominal_rate_hz"]),
            _lsl_channel_format(self._pylsl, str(self._config["channel_format"])),
            self._config["source_id"],
        )
        self._outlet = self._pylsl.StreamOutlet(info)

    def append(self, record: Any) -> None:
        if isinstance(record, DenseSampleBatch):
            timestamps = [point.seconds for point in record.sample_times]
            if not timestamps:
                assert record.first_sample_time is not None
                assert record.sample_period_seconds is not None
                timestamps = [
                    record.first_sample_time.seconds + index * record.sample_period_seconds
                    for index in range(record.sample_count)
                ]
            if record.sample_period_seconds is not None:
                self._outlet.push_chunk(record.values.tolist(), timestamp=timestamps[-1])
            else:
                for row, timestamp in zip(record.values.tolist(), timestamps):
                    self._outlet.push_sample(row, timestamp=timestamp)
            return
        if isinstance(record, SparseEventBatch):
            for event in record.events:
                value = event.value if isinstance(event.value, str) else json.dumps(thaw_json(event.value))
                self._outlet.push_sample([value], timestamp=event.event_time.seconds)
            return
        if isinstance(record, MetadataEvent):
            self._outlet.push_sample(
                [json.dumps(thaw_json(record.metadata), sort_keys=True, separators=(",", ":"))],
                timestamp=record.event_time.seconds,
            )
            return
        raise TypeError(f"unsupported LSL outlet record: {type(record).__name__}")


def detect_lsl(
    *,
    wait_time: float = 1.0,
    boundary_clock_id: str = "boundary.clock",
    logical_clock_id: str = "device.clock",
    default_dense_unit: str = "unknown",
    pylsl_module: Any | None = None,
) -> LslDetection:
    """Resolve all visible streams and emit proposal-ready typed observations."""

    wait = float(wait_time)
    if not math.isfinite(wait) or wait < 0:
        raise ValueError("LSL discovery wait_time must be finite and nonnegative")
    try:
        pylsl = pylsl_module or _import_pylsl()
    except (ImportError, OSError, RuntimeError):
        return LslDetection((), (), LslSupportReport(False, None, LslSupportLevel.UNAVAILABLE), ())
    infos = tuple(pylsl.resolve_streams(wait_time=wait))
    streams: list[LslStreamIdentity] = []
    sources: list[SourceCapability] = []
    clocks: dict[tuple[str, str], ClockCapability] = {}
    for info in infos:
        identity = stream_identity(info)
        streams.append(identity)
        content_kind = _content_kind(identity)
        clock_id = require_identifier(logical_clock_id, "logical_clock_id")
        stream_id = f"stream.lsl.{_identifier(identity.source_id or identity.uid or identity.name)}"
        channels = ()
        rate_model = RateModel.EVENT
        sample_rate = None
        sample_dtype = None
        if content_kind == ContentKind.DENSE_SAMPLES:
            rate_model = RateModel.REGULAR if identity.nominal_rate_hz > 0 else RateModel.IRREGULAR
            sample_rate = identity.nominal_rate_hz if identity.nominal_rate_hz > 0 else None
            sample_dtype = _numpy_dtype(identity.channel_format)
            labels, units = _channel_metadata(info, identity.channel_count, default_dense_unit)
            channels = tuple(
                ChannelSpec(
                    f"channel.{index + 1}.{_identifier(label or str(index + 1))}",
                    identity.stream_type or "signal",
                    units[index],
                    name=label,
                )
                for index, label in enumerate(labels)
            )
        stream = StreamSpec(
            stream_id,
            1,
            _modality(identity),
            content_kind,
            rate_model,
            clock_id,
            channels,
            sample_rate,
            sample_dtype,
            MissingDataPolicy.FORBID,
            metadata={"lsl": identity.to_payload()},
        )
        plugin_id = {
            ContentKind.DENSE_SAMPLES: LSL_DENSE_SOURCE_PLUGIN_ID,
            ContentKind.SPARSE_EVENTS: LSL_SPARSE_SOURCE_PLUGIN_ID,
            ContentKind.METADATA: LSL_METADATA_SOURCE_PLUGIN_ID,
        }[content_kind]
        suffix = _identifier(identity.uid or identity.source_id or identity.name)
        source = SourceLocation(
            SourceKind.DETECTION,
            locator="lsl-network",
            symbol=identity.uid or identity.source_id or identity.name,
        )
        sources.append(
            SourceCapability(
                capability_id=f"capability.lsl.{suffix}",
                resource_id=f"resource.lsl.{suffix}",
                resource_kind="lsl_stream",
                stream=stream,
                contract=signal_contract_from_stream(stream),
                plugin_id=plugin_id,
                plugin_version=LSL_PLUGIN_VERSION,
                selector=identity.selector,
                capabilities=("lsl", content_kind.value, "reconnect", "clock_sync", "packet_loss"),
                config={
                    "stream_spec": stream.to_payload(),
                    "selector": thaw_json(identity.selector),
                    "boundary_clock_id": boundary_clock_id,
                },
                placement=Placement.IN_PROCESS,
                source=source,
            )
        )
        binding = ClockMappingBinding(
            clock_id,
            boundary_clock_id,
            ClockMappingStrategy.ONLINE_ESTIMATED,
            0.01,
        )
        clocks[(clock_id, boundary_clock_id)] = ClockCapability(
            f"capability.clock.lsl.{suffix}", binding, source
        )
    version = _library_version(pylsl)
    return LslDetection(
        tuple(sources),
        tuple(clocks.values()),
        LslSupportReport(True, version, LslSupportLevel.SIMULATED_VALIDATED),
        tuple(streams),
    )


def exact_selector(selector: Mapping[str, Any]) -> Mapping[str, str]:
    unknown = sorted(set(selector) - set(_SELECTOR_FIELDS))
    if unknown:
        raise ValueError("unknown LSL selector fields: " + ", ".join(unknown))
    values = {str(key): str(value) for key, value in selector.items() if str(value)}
    if not values:
        raise ValueError("LSL selector must contain an exact identity field")
    if not ({"uid", "source_id"} & set(values)) and not {"name", "type"}.issubset(values):
        raise ValueError("LSL selector requires uid, source_id, or the exact name/type pair")
    return freeze_json(values)


def select_exact_stream(infos: Iterable[Any], selector: Mapping[str, Any]) -> Any:
    normalized = exact_selector(selector)
    matches = [
        info
        for info in infos
        if all(_identity_field(stream_identity(info), key) == value for key, value in normalized.items())
    ]
    if not matches:
        raise LookupError(f"no LSL stream matches exact selector {dict(normalized)}")
    if len(matches) != 1:
        identities = [stream_identity(value).to_payload() for value in matches]
        raise ValueError(f"LSL selector is ambiguous across {len(matches)} streams: {identities}")
    return matches[0]


def stream_identity(info: Any) -> LslStreamIdentity:
    return LslStreamIdentity(
        str(info.name()),
        str(info.type()),
        int(info.channel_count()),
        float(info.nominal_srate()),
        str(info.channel_format()),
        str(info.source_id()),
        str(info.uid()),
        str(info.hostname()),
    )


def _identity_field(identity: LslStreamIdentity, field: str) -> str:
    return identity.stream_type if field == "type" else str(getattr(identity, field))


def _import_pylsl() -> Any:
    return importlib.import_module("pylsl")


def _selector_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {value: {"type": "string", "minLength": 1} for value in _SELECTOR_FIELDS},
        "minProperties": 1,
        "additionalProperties": False,
    }


def _content_kind(identity: LslStreamIdentity) -> ContentKind:
    lowered = identity.stream_type.lower()
    if "marker" in lowered or "event" in lowered:
        return ContentKind.SPARSE_EVENTS
    if identity.nominal_rate_hz > 0 and identity.channel_format.lower() not in {"string", "3", "cf_string"}:
        return ContentKind.DENSE_SAMPLES
    return ContentKind.METADATA


def _modality(identity: LslStreamIdentity) -> str:
    value = identity.stream_type.strip().lower().replace(" ", "_")
    return value or "metadata"


def _identifier(value: str) -> str:
    normalized = "".join(character.lower() if character.isalnum() else "." for character in value)
    normalized = ".".join(filter(None, normalized.split(".")))
    return require_identifier(normalized or "anonymous", "LSL identity")


def _numpy_dtype(channel_format: str) -> str:
    lowered = channel_format.lower()
    numeric = {
        "1": "float32",
        "2": "float64",
        "4": "int32",
        "5": "int16",
        "6": "int8",
        "7": "int64",
    }
    if lowered in numeric:
        return numeric[lowered]
    for token, dtype in (
        ("double", "float64"),
        ("float64", "float64"),
        ("float", "float32"),
        ("int64", "int64"),
        ("int32", "int32"),
        ("int16", "int16"),
        ("int8", "int8"),
    ):
        if token in lowered:
            return dtype
    return "float64"


def _channel_metadata(info: Any, count: int, default_unit: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    labels = [f"CH{index + 1}" for index in range(count)]
    units = [default_unit for _ in range(count)]
    try:
        channel = info.desc().child("channels").child("channel")
        for index in range(count):
            if channel.empty():
                break
            label = str(channel.child_value("label") or labels[index])
            unit = str(channel.child_value("unit") or units[index])
            labels[index], units[index] = label, unit
            channel = channel.next_sibling("channel")
    except (AttributeError, TypeError):
        pass
    return tuple(labels), tuple(units)


def _library_version(pylsl: Any) -> str | None:
    try:
        return str(pylsl.library_version())
    except (AttributeError, RuntimeError):
        return str(getattr(pylsl, "__version__", "unknown"))


def _lsl_channel_format(pylsl: Any, value: str) -> Any:
    if hasattr(pylsl, value):
        return getattr(pylsl, value)
    name = value if value.startswith("cf_") else f"cf_{value}"
    return getattr(pylsl, name, value)


__all__ = [
    "LSL_CLOCK_ID",
    "LSL_DENSE_OUTLET_PLUGIN_ID",
    "LSL_DENSE_SOURCE_PLUGIN_ID",
    "LSL_METADATA_OUTLET_PLUGIN_ID",
    "LSL_METADATA_SOURCE_PLUGIN_ID",
    "LSL_PLUGIN_VERSION",
    "LSL_SPARSE_OUTLET_PLUGIN_ID",
    "LSL_SPARSE_SOURCE_PLUGIN_ID",
    "LSL_SUPPORT_SCHEMA_ID",
    "LslClockObservation",
    "LslDetection",
    "LslOutlet",
    "LslPacketLoss",
    "LslSource",
    "LslStreamIdentity",
    "LslSupportLevel",
    "LslSupportReport",
    "detect_lsl",
    "exact_selector",
    "lsl_plugin_descriptors",
    "select_exact_stream",
    "stream_identity",
]
