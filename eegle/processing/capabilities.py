"""Machine-readable causality and state declarations for transforms."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from eegle._domain import Determinism, ExecutionMode
from eegle._validation import require_finite


TRANSFORM_CAPABILITIES_SCHEMA = "eegle.transform_capabilities.v1"


@dataclass(frozen=True, slots=True)
class TransformCapabilities:
    supported_modes: frozenset[ExecutionMode]
    stateful: bool
    requires_future: bool
    determinism: Determinism = Determinism.DETERMINISTIC
    lookahead_seconds: float = 0.0
    warmup_samples: int = 0
    schema: str = TRANSFORM_CAPABILITIES_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != TRANSFORM_CAPABILITIES_SCHEMA:
            raise ValueError(f"unsupported transform capabilities schema: {self.schema}")
        modes = frozenset(ExecutionMode(mode) for mode in self.supported_modes)
        if not modes:
            raise ValueError("transform must support at least one execution mode")
        object.__setattr__(self, "supported_modes", modes)
        object.__setattr__(self, "determinism", Determinism(self.determinism))
        object.__setattr__(
            self, "lookahead_seconds", require_finite(self.lookahead_seconds, "lookahead_seconds")
        )
        if self.lookahead_seconds < 0:
            raise ValueError("lookahead_seconds cannot be negative")
        object.__setattr__(self, "warmup_samples", int(self.warmup_samples))
        if self.warmup_samples < 0:
            raise ValueError("warmup_samples cannot be negative")
        if ExecutionMode.CAUSAL in modes and (self.requires_future or self.lookahead_seconds > 0):
            raise ValueError("causal transforms cannot require future information or lookahead")

    def validate_mode(self, mode: ExecutionMode) -> None:
        requested = ExecutionMode(mode)
        if requested not in self.supported_modes:
            supported = ", ".join(sorted(value.value for value in self.supported_modes))
            raise ValueError(
                f"transform does not support {requested.value} execution; supported: {supported}"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "supported_modes": sorted(mode.value for mode in self.supported_modes),
            "stateful": self.stateful,
            "requires_future": self.requires_future,
            "determinism": self.determinism.value,
            "lookahead_seconds": self.lookahead_seconds,
            "warmup_samples": self.warmup_samples,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TransformCapabilities":
        return cls(
            schema=str(payload["schema"]),
            supported_modes=frozenset(
                ExecutionMode(str(value)) for value in payload["supported_modes"]
            ),
            stateful=bool(payload["stateful"]),
            requires_future=bool(payload["requires_future"]),
            determinism=Determinism(str(payload.get("determinism", "deterministic"))),
            lookahead_seconds=float(payload.get("lookahead_seconds", 0.0)),
            warmup_samples=int(payload.get("warmup_samples", 0)),
        )
