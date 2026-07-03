"""Typed scientific protocol files for EEGle experiments."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


PROTOCOL_SCHEMA = "eegle.protocol.v1"


@dataclass(frozen=True)
class ProtocolTarget:
    name: str
    positive: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["positive"] = list(self.positive)
        return data


@dataclass(frozen=True)
class ScientificProtocol:
    """A reproducible declaration of the scientific question being tested."""

    name: str
    task: str
    primary_endpoint: str
    prediction_window_seconds: tuple[float, float]
    prediction_horizon: str
    targets: tuple[ProtocolTarget, ...]
    splits: tuple[str, ...]
    baselines: tuple[str, ...]
    metrics: tuple[str, ...]
    schema: str = PROTOCOL_SCHEMA
    metadata: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["prediction_window_seconds"] = list(self.prediction_window_seconds)
        data["targets"] = [target.payload() for target in self.targets]
        data["splits"] = list(self.splits)
        data["baselines"] = list(self.baselines)
        data["metrics"] = list(self.metrics)
        return data

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ScientificProtocol":
        if payload.get("schema", PROTOCOL_SCHEMA) != PROTOCOL_SCHEMA:
            raise ValueError(f"unsupported protocol schema: {payload.get('schema')}")
        return cls(
            name=str(payload["name"]),
            task=str(payload["task"]),
            primary_endpoint=str(payload["primary_endpoint"]),
            prediction_window_seconds=_pair(payload["prediction_window_seconds"]),
            prediction_horizon=str(payload["prediction_horizon"]),
            targets=tuple(
                ProtocolTarget(
                    name=str(target["name"]),
                    positive=tuple(str(value) for value in target.get("positive", ())),
                    metadata=dict(target.get("metadata") or {}),
                )
                for target in payload.get("targets", ())
            ),
            splits=tuple(str(value) for value in payload.get("splits", ())),
            baselines=tuple(str(value) for value in payload.get("baselines", ())),
            metrics=tuple(str(value) for value in payload.get("metrics", ())),
            schema=str(payload.get("schema", PROTOCOL_SCHEMA)),
            metadata=dict(payload.get("metadata") or {}),
        )


def write_protocol(protocol: ScientificProtocol, path: str | Path) -> ScientificProtocol:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(protocol.payload(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return protocol


def load_protocol(path: str | Path) -> ScientificProtocol:
    with Path(path).expanduser().resolve().open("r", encoding="utf-8") as handle:
        return ScientificProtocol.from_payload(json.load(handle))


def _pair(value: Any) -> tuple[float, float]:
    values = list(value)
    if len(values) != 2:
        raise ValueError("expected two-value prediction window")
    return (float(values[0]), float(values[1]))
