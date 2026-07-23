"""Typed component-port graph produced during compilation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from eegle._validation import require_identifier
from eegle.compiler.lock import canonical_hash
from eegle.specs.suite import SignalContract


class PortDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


@dataclass(frozen=True, slots=True)
class CompiledPort:
    component_id: str
    name: str
    direction: PortDirection
    type_id: str
    required: bool
    multiple: bool
    contract: SignalContract

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "component_id", require_identifier(self.component_id, "component_id")
        )
        object.__setattr__(self, "name", require_identifier(self.name, "port name"))
        object.__setattr__(self, "direction", PortDirection(self.direction))
        object.__setattr__(self, "type_id", require_identifier(self.type_id, "port type_id"))
        if self.contract.type_id != self.type_id:
            raise ValueError("compiled port contract must match descriptor type_id")

    @property
    def endpoint(self) -> str:
        return f"{self.component_id}.{self.name}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "name": self.name,
            "direction": self.direction.value,
            "type_id": self.type_id,
            "required": self.required,
            "multiple": self.multiple,
            "contract": self.contract.to_payload(),
        }


@dataclass(frozen=True, slots=True)
class CompiledRoute:
    route_id: str
    source: str
    target: str
    type_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_id", require_identifier(self.route_id, "route_id"))
        if "." not in self.source or "." not in self.target:
            raise ValueError("compiled route endpoints must be component.port identities")
        object.__setattr__(self, "type_id", require_identifier(self.type_id, "route type_id"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "source": self.source,
            "target": self.target,
            "type_id": self.type_id,
        }


@dataclass(frozen=True, slots=True)
class CompiledGraph:
    ports: tuple[CompiledPort, ...]
    routes: tuple[CompiledRoute, ...]
    component_order: tuple[str, ...]

    def __post_init__(self) -> None:
        endpoints = tuple(
            (value.component_id, value.direction.value, value.name) for value in self.ports
        )
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("compiled graph port endpoints must be unique")
        route_ids = tuple(value.route_id for value in self.routes)
        if len(route_ids) != len(set(route_ids)):
            raise ValueError("compiled graph route identities must be unique")
        if len(self.component_order) != len(set(self.component_order)):
            raise ValueError("compiled graph component order must be unique")

    @property
    def graph_hash(self) -> str:
        return canonical_hash(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "eegle.compiled_graph.v1",
            "ports": [value.to_payload() for value in self.ports],
            "routes": [value.to_payload() for value in self.routes],
            "component_order": list(self.component_order),
        }


def contract_issues(source: SignalContract, target: SignalContract) -> tuple[str, ...]:
    """Return precise reasons a source cannot prove a target contract."""

    issues: list[str] = []
    if source.type_id != target.type_id:
        issues.append(f"type {source.type_id} does not match required {target.type_id}")
    if target.unit is not None:
        if source.unit is None:
            issues.append(f"source does not declare required unit {target.unit}")
        elif source.unit != target.unit:
            issues.append(f"unit {source.unit} does not match required {target.unit}")
    source_channels = source.channel_count
    if target.channel_count is not None:
        if source_channels is None:
            issues.append(f"source channel count is unknown; required {target.channel_count}")
        elif source_channels != target.channel_count:
            issues.append(
                f"channel count {source_channels} does not match required {target.channel_count}"
            )
    if target.minimum_channels is not None:
        if source_channels is None:
            issues.append(
                f"source channel count is unknown; minimum is {target.minimum_channels}"
            )
        elif source_channels < target.minimum_channels:
            issues.append(
                f"channel count {source_channels} is below minimum {target.minimum_channels}"
            )
    if target.maximum_channels is not None and source_channels is not None:
        if source_channels > target.maximum_channels:
            issues.append(
                f"channel count {source_channels} exceeds maximum {target.maximum_channels}"
            )
    source_rate = source.nominal_rate_hz
    if target.nominal_rate_hz is not None:
        if source_rate is None:
            issues.append(f"source rate is unknown; required {target.nominal_rate_hz} Hz")
        elif source_rate != target.nominal_rate_hz:
            issues.append(
                f"sample rate {source_rate} Hz does not match required {target.nominal_rate_hz} Hz"
            )
    if target.minimum_rate_hz is not None:
        if source_rate is None:
            issues.append(f"source rate is unknown; minimum is {target.minimum_rate_hz} Hz")
        elif source_rate < target.minimum_rate_hz:
            issues.append(
                f"sample rate {source_rate} Hz is below minimum {target.minimum_rate_hz} Hz"
            )
    if target.maximum_rate_hz is not None and source_rate is not None:
        if source_rate > target.maximum_rate_hz:
            issues.append(
                f"sample rate {source_rate} Hz exceeds maximum {target.maximum_rate_hz} Hz"
            )
    source_window = source.window_samples
    if target.window_samples is not None:
        if source_window is None:
            issues.append(
                f"source window length is unknown; required {target.window_samples} samples"
            )
        elif source_window != target.window_samples:
            issues.append(
                f"window length {source_window} does not match required {target.window_samples}"
            )
    if target.minimum_window_samples is not None:
        if source_window is None:
            issues.append(
                "source window length is unknown; minimum is "
                f"{target.minimum_window_samples} samples"
            )
        elif source_window < target.minimum_window_samples:
            issues.append(
                f"window length {source_window} is below minimum "
                f"{target.minimum_window_samples}"
            )
    return tuple(issues)


def topological_order(
    component_ids: tuple[str, ...], routes: tuple[tuple[str, str], ...]
) -> tuple[str, ...] | None:
    """Return deterministic order, or ``None`` when the graph has a cycle."""

    outgoing: dict[str, set[str]] = {value: set() for value in component_ids}
    indegree: dict[str, int] = {value: 0 for value in component_ids}
    for source, target in routes:
        if target not in outgoing[source]:
            outgoing[source].add(target)
            indegree[target] += 1
    ready = sorted(value for value, count in indegree.items() if count == 0)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target in sorted(outgoing[current]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    return tuple(order) if len(order) == len(component_ids) else None
