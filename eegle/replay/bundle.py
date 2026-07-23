"""Load a plan-bearing evidence bundle and replay its recorded execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from eegle._domain import EquivalenceLevel
from eegle.compiler.plan import ExecutionPlan
from eegle.recording.bundles import EvidenceReader
from eegle.recording.stores import FRAMED_SAMPLE_MEDIA_TYPE, read_framed_sample_store
from eegle.replay.compare import EquivalencePolicy, compare_runs
from eegle.replay.runner import ReplayExecution
from eegle.replay.source import ReplayMode
from eegle.runtime.engine import EngineRunResult, EngineStatus, ExecutionEngine
from eegle.streams.channels import StreamSpec
from eegle.streams.packets import Packet


@dataclass(frozen=True, slots=True)
class RecordedExecution:
    plan: ExecutionPlan
    streams: tuple[StreamSpec, ...]
    reference: EngineRunResult


BundleEngineFactory = Callable[
    [ExecutionPlan, tuple[Packet, ...], tuple[StreamSpec, ...], ReplayMode],
    ExecutionEngine,
]


def load_recorded_execution(reader: EvidenceReader) -> RecordedExecution:
    """Resolve and verify the plan, evidence ledger, and exact admitted capture."""

    report = reader.verify()
    if not report.valid:
        raise ValueError(f"cannot replay bundle with {report.status.value} integrity")
    plans = tuple(
        reference for reference in reader.bundle.artifacts if reference.role == "execution_plan"
    )
    if len(plans) != 1:
        raise ValueError(
            f"replay requires exactly one execution_plan artifact; observed {len(plans)}"
        )
    plan_reference = plans[0]
    if not plan_reference.embedded:
        raise ValueError("execution_plan must be embedded for bundle-driven replay")
    try:
        plan_payload = json.loads(
            reader.session.artifacts.resolve(plan_reference).read_text(encoding="utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("execution_plan artifact is not valid JSON") from exc
    if not isinstance(plan_payload, dict):
        raise ValueError("execution_plan artifact must contain a JSON object")
    plan = ExecutionPlan.from_payload(plan_payload)
    if plan.plan_hash != reader.bundle.plan_hash:
        raise ValueError("execution_plan hash does not match the evidence bundle")

    captures = reader.bundle.execution_captures
    if len(captures) != 1:
        raise ValueError(
            f"replay requires exactly one execution capture; observed {len(captures)}"
        )
    capture = captures[0]
    if not capture.embedded or capture.media_type != FRAMED_SAMPLE_MEDIA_TYPE:
        raise ValueError("bundle replay currently requires one embedded framed execution capture")
    restored = read_framed_sample_store(reader.session.artifacts.resolve(capture))
    metadata = reader.bundle.metadata
    try:
        status = EngineStatus(str(metadata["engine_status"]))
    except (KeyError, ValueError) as exc:
        raise ValueError("bundle is missing a valid engine_status") from exc
    equivalence_value = metadata.get("equivalence_ceiling")
    if equivalence_value is None:
        raise ValueError("bundle is missing its equivalence_ceiling")
    reference = EngineRunResult(
        execution_id=str(metadata.get("execution_id") or reader.bundle.bundle_id),
        plan_hash=reader.bundle.plan_hash,
        status=status,
        evidence=reader.records(),
        captured_packets=restored.packets,
        work=(),
        predictions=(),
        equivalence_ceiling=EquivalenceLevel(str(equivalence_value)),
        failure=None if metadata.get("failure") is None else str(metadata["failure"]),
    )
    return RecordedExecution(plan, restored.streams, reference)


class BundleReplayRunner:
    """Replay directly from an EvidenceBundle through a fresh execution engine."""

    def __init__(self, engine_factory: BundleEngineFactory) -> None:
        self.engine_factory = engine_factory

    def run(
        self,
        reader: EvidenceReader,
        *,
        mode: ReplayMode = ReplayMode.ACCELERATED_CAUSAL,
        policy: EquivalencePolicy | None = None,
    ) -> ReplayExecution:
        recorded = load_recorded_execution(reader)
        normalized_mode = ReplayMode(mode)
        engine = self.engine_factory(
            recorded.plan,
            recorded.reference.captured_packets,
            recorded.streams,
            normalized_mode,
        )
        if not isinstance(engine, ExecutionEngine):
            raise TypeError("bundle replay engine_factory must return ExecutionEngine")
        result = engine.run()
        comparison = compare_runs(
            recorded.reference,
            result,
            policy or EquivalencePolicy(),
        )
        return ReplayExecution(normalized_mode, result, comparison)
