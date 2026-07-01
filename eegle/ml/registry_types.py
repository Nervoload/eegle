"""Small data structures for model registry declarations."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ModelSpec:
    kind: str
    family: str
    description: str
    adapter_kind: str
    train_kind: str | None
    trainable: bool
    realtime_supported: bool
    primary_realtime_allowed: bool
    dependencies: tuple[str, ...]
    artifact_format: str
    checkpoint_format: str
    supported_targets: tuple[str, ...]
    latency_budget_ms: float
    aliases: tuple[str, ...] = ()
    external_checkpoint: bool = False

    def payload(self) -> dict[str, object]:
        data = asdict(self)
        data["aliases"] = list(self.aliases)
        data["dependencies"] = list(self.dependencies)
        data["supported_targets"] = list(self.supported_targets)
        return data
