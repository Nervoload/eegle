"""A deliberately small built-in transform set.

Dense arrays are always samples x channels. Causal SOS filters retain state
between chunks. Retrospective zero-phase filters explicitly require a complete
segment and cannot validate for causal execution.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

import numpy as np
from scipy import signal

from eegle._domain import Determinism, ExecutionMode, Lineage
from eegle.compiler.lock import canonical_hash
from eegle.processing.capabilities import TransformCapabilities
from eegle.streams.packets import DenseSampleBatch, Packet

if TYPE_CHECKING:
    from eegle.plugins.contracts import ExecutionContext


class IdentityTransform:
    capabilities = TransformCapabilities(
        supported_modes=frozenset(
            {ExecutionMode.CAUSAL, ExecutionMode.RETROSPECTIVE, ExecutionMode.ORACLE}
        ),
        stateful=False,
        requires_future=False,
    )

    def __init__(
        self,
        *,
        output_stream_id: str | None = None,
        output_stream_revision: int | None = None,
    ) -> None:
        self.output_stream_id = output_stream_id
        self.output_stream_revision = output_stream_revision

    def update(self, packet: Packet, context: "ExecutionContext") -> DenseSampleBatch:
        self.capabilities.validate_mode(context.execution_mode)
        batch = _dense_packet(packet)
        values = np.array(batch.values, copy=True)
        values.setflags(write=False)
        return _derived_dense_batch(
            batch,
            values,
            context,
            output_stream_id=self.output_stream_id,
            output_stream_revision=self.output_stream_revision,
        )


class CausalSosFilter:
    capabilities = TransformCapabilities(
        supported_modes=frozenset({ExecutionMode.CAUSAL, ExecutionMode.RETROSPECTIVE}),
        stateful=True,
        requires_future=False,
        determinism=Determinism.DETERMINISTIC,
    )

    def __init__(
        self,
        sos: np.ndarray,
        channel_count: int,
        *,
        output_stream_id: str | None = None,
        output_stream_revision: int | None = None,
    ) -> None:
        self.sos = _sos_array(sos)
        self.channel_count = int(channel_count)
        if self.channel_count <= 0:
            raise ValueError("channel_count must be positive")
        self.output_stream_id = output_stream_id
        self.output_stream_revision = output_stream_revision
        self._state = np.zeros((self.sos.shape[0], 2, self.channel_count), dtype=float)

    def update(self, packet: Packet, context: "ExecutionContext") -> DenseSampleBatch:
        self.capabilities.validate_mode(context.execution_mode)
        batch = _dense_packet(packet)
        _validate_component_time(batch, context)
        data = _dense_array(batch.values, channel_count=self.channel_count)
        prior_state_hash = self.snapshot_state()["state_hash"]
        filtered, state = signal.sosfilt(self.sos, data, axis=0, zi=self._state)
        self._state = np.asarray(state, dtype=float)
        filtered = np.asarray(filtered, dtype=batch.values.dtype)
        filtered.setflags(write=False)
        return _derived_dense_batch(
            batch,
            filtered,
            context,
            state_hash=prior_state_hash,
            output_stream_id=self.output_stream_id,
            output_stream_revision=self.output_stream_revision,
        )

    def snapshot_state(self) -> dict[str, Any]:
        payload = {
            "schema": "eegle.causal_sos_state.v1",
            "sos_hash": canonical_hash(self.sos.tolist()),
            "channel_count": self.channel_count,
            "state": self._state.tolist(),
        }
        payload["state_hash"] = canonical_hash(payload)
        return payload

    def restore_state(self, payload: Mapping[str, Any]) -> None:
        if payload.get("schema") != "eegle.causal_sos_state.v1":
            raise ValueError(f"unsupported causal SOS state schema: {payload.get('schema')}")
        if payload.get("sos_hash") != canonical_hash(self.sos.tolist()):
            raise ValueError("causal SOS state was produced by different coefficients")
        if int(payload.get("channel_count", -1)) != self.channel_count:
            raise ValueError("causal SOS state channel count does not match")
        without_hash = {key: value for key, value in payload.items() if key != "state_hash"}
        if payload.get("state_hash") != canonical_hash(without_hash):
            raise ValueError("causal SOS state hash mismatch")
        restored = np.asarray(payload["state"], dtype=float)
        if restored.shape != self._state.shape or not bool(np.all(np.isfinite(restored))):
            raise ValueError("invalid causal SOS state array")
        self._state = restored.copy()


class RetrospectiveSosFilter:
    capabilities = TransformCapabilities(
        supported_modes=frozenset({ExecutionMode.RETROSPECTIVE, ExecutionMode.ORACLE}),
        stateful=False,
        requires_future=True,
        determinism=Determinism.DETERMINISTIC,
    )

    def __init__(
        self,
        sos: np.ndarray,
        *,
        output_stream_id: str | None = None,
        output_stream_revision: int | None = None,
    ) -> None:
        self.sos = _sos_array(sos)
        self.output_stream_id = output_stream_id
        self.output_stream_revision = output_stream_revision

    def update(self, packet: Packet, context: "ExecutionContext") -> DenseSampleBatch:
        self.capabilities.validate_mode(context.execution_mode)
        batch = _dense_packet(packet)
        data = _dense_array(batch.values)
        filtered = np.asarray(
            signal.sosfiltfilt(self.sos, data, axis=0),
            dtype=batch.values.dtype,
        )
        filtered.setflags(write=False)
        return _derived_dense_batch(
            batch,
            filtered,
            context,
            output_stream_id=self.output_stream_id,
            output_stream_revision=self.output_stream_revision,
        )


def _dense_packet(packet: Packet) -> DenseSampleBatch:
    if not isinstance(packet, DenseSampleBatch):
        raise TypeError("built-in dense transforms require DenseSampleBatch input")
    return packet


def _validate_component_time(
    batch: DenseSampleBatch,
    context: "ExecutionContext",
) -> None:
    current_time = context.current_time
    if current_time.clock_id != batch.available_time.clock_id:
        raise ValueError("component time and packet availability must use the execution clock")
    if current_time.seconds < batch.available_time.seconds:
        raise ValueError("causal component cannot consume a packet before it is available")
    return None


def _derived_dense_batch(
    batch: DenseSampleBatch,
    values: np.ndarray,
    context: "ExecutionContext",
    *,
    state_hash: str | None = None,
    output_stream_id: str | None = None,
    output_stream_revision: int | None = None,
) -> DenseSampleBatch:
    _validate_component_time(batch, context)
    current_time = context.current_time
    target_stream = output_stream_id or batch.stream_id
    if output_stream_id is not None and output_stream_id != batch.stream_id:
        if output_stream_revision is None:
            raise ValueError("a new output stream requires output_stream_revision")
        target_revision = int(output_stream_revision)
    else:
        target_revision = batch.stream_revision
        if output_stream_revision is not None and int(output_stream_revision) != target_revision:
            raise ValueError("same-stream transform cannot change the stream revision")
    clock_mappings = _merge_revisions(
        {} if batch.lineage is None else batch.lineage.clock_mapping_revisions,
        context.clock_mapping_revisions,
        field="clock mapping",
    )
    stream_revisions = dict({} if batch.lineage is None else batch.lineage.stream_revisions)
    previous_revision = stream_revisions.get(batch.stream_id)
    if previous_revision is not None and previous_revision != batch.stream_revision:
        raise ValueError("packet lineage conflicts with its declared stream revision")
    stream_revisions[batch.stream_id] = batch.stream_revision
    stream_revisions[target_stream] = target_revision
    lineage = Lineage(
        component_id=context.component_id,
        input_ids=(batch.batch_id,),
        component_version=context.component_version,
        state_hash=state_hash,
        latest_input_available_time=batch.available_time,
        clock_mapping_revisions=clock_mappings,
        stream_revisions=stream_revisions,
    )
    return DenseSampleBatch(
        batch_id=context.next_id("batch"),
        stream_id=target_stream,
        stream_revision=target_revision,
        sequence_start=batch.sequence_start,
        channel_ids=batch.channel_ids,
        values=values,
        received_time=batch.received_time,
        available_time=current_time,
        first_sample_time=batch.first_sample_time,
        sample_period_seconds=batch.sample_period_seconds,
        sample_times=batch.sample_times,
        validity_mask=batch.validity_mask,
        lineage=lineage,
    )


def _merge_revisions(
    first: Mapping[str, int],
    second: Mapping[str, int],
    *,
    field: str,
) -> dict[str, int]:
    merged = {str(key): int(value) for key, value in first.items()}
    for key, value in second.items():
        normalized = str(key)
        revision = int(value)
        if normalized in merged and merged[normalized] != revision:
            raise ValueError(f"{field} revision conflict for {normalized}")
        merged[normalized] = revision
    return merged


def _sos_array(sos: np.ndarray) -> np.ndarray:
    coefficients = np.asarray(sos, dtype=float)
    if coefficients.ndim != 2 or coefficients.shape[1] != 6 or coefficients.shape[0] == 0:
        raise ValueError("SOS coefficients must have shape sections x 6")
    if not bool(np.all(np.isfinite(coefficients))):
        raise ValueError("SOS coefficients must be finite")
    return coefficients.copy()


def _dense_array(values: np.ndarray, channel_count: int | None = None) -> np.ndarray:
    data = np.asarray(values)
    if data.ndim != 2 or data.shape[0] == 0 or data.shape[1] == 0:
        raise ValueError("transform input must be a non-empty samples x channels array")
    if not np.issubdtype(data.dtype, np.floating):
        raise ValueError("SOS transforms require a floating-point sample dtype")
    if channel_count is not None and data.shape[1] != channel_count:
        raise ValueError(
            f"transform expected {channel_count} channels, received {data.shape[1]}"
        )
    if not bool(np.all(np.isfinite(data))):
        raise ValueError("built-in transforms require finite admitted values")
    return data
