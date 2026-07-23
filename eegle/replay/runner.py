"""Replay captured inputs through a freshly assembled ExecutionEngine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from eegle.replay.compare import EquivalencePolicy, EquivalenceReport, compare_runs
from eegle.replay.source import ReplayMode
from eegle.runtime.engine import EngineRunResult, ExecutionEngine
from eegle.streams.packets import Packet


EngineFactory = Callable[[tuple[Packet, ...], ReplayMode], ExecutionEngine]


@dataclass(frozen=True, slots=True)
class ReplayExecution:
    mode: ReplayMode
    result: EngineRunResult
    equivalence: EquivalenceReport


class ReplayRunner:
    def __init__(self, engine_factory: EngineFactory) -> None:
        self.engine_factory = engine_factory

    def run(
        self,
        reference: EngineRunResult,
        *,
        mode: ReplayMode = ReplayMode.ACCELERATED_CAUSAL,
        policy: EquivalencePolicy | None = None,
    ) -> ReplayExecution:
        normalized_mode = ReplayMode(mode)
        engine = self.engine_factory(reference.captured_packets, normalized_mode)
        if not isinstance(engine, ExecutionEngine):
            raise TypeError("replay engine_factory must return ExecutionEngine")
        result = engine.run()
        comparison = compare_runs(reference, result, policy or EquivalencePolicy())
        return ReplayExecution(normalized_mode, result, comparison)
