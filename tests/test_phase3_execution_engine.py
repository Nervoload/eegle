from __future__ import annotations

from dataclasses import replace
import unittest

import numpy as np

from eegle._domain import ComponentKind, EquivalenceLevel, ExecutionMode, WorkStatus
from eegle.actions import ObserveOnlyPolicy
from eegle.compiler import ExecutionPlan, LockedPlugin, PlannedComponent, canonical_hash
from eegle.models import MeanThresholdModel, ModelRole, ModelRoleKind
from eegle.processing import (
    ContinuousWindowBuilder,
    DenseWindow,
    FiniteQualityGate,
    IdentityTransform,
)
from eegle.replay import EquivalencePolicy, ReplayMode, ReplayRunner, ReplaySource
from eegle.runtime import (
    BackpressurePolicy,
    ComponentBinding,
    ComponentPlacement,
    EngineComponents,
    EngineStatus,
    ExecutionEngine,
    ModelBinding,
    ProcessBoundary,
    SchedulingPolicy,
    SourceBinding,
)
from eegle.streams import (
    ChannelSpec,
    ContentKind,
    DenseSampleBatch,
    PacketSequenceSource,
    RateModel,
    StreamSpec,
    TimePoint,
)


EXECUTION_CLOCK = "host.virtual"
SAMPLE_CLOCK = "device.synthetic"
VERSION = "0.1.0"


def _time(seconds: float, clock: str = EXECUTION_CLOCK) -> TimePoint:
    return TimePoint(seconds, clock)


def _stream(stream_id: str = "stream.synthetic") -> StreamSpec:
    return StreamSpec(
        stream_id=stream_id,
        revision=1,
        modality="synthetic",
        content_kind=ContentKind.DENSE_SAMPLES,
        rate_model=RateModel.REGULAR,
        clock_id=SAMPLE_CLOCK,
        channels=(ChannelSpec("signal.0", "signal", "a.u."),),
        sample_rate_hz=100.0,
        sample_dtype="float64",
    )


def _batch(
    batch_id: str,
    sequence_start: int,
    available: float,
    values: tuple[float, ...],
    *,
    stream_id: str = "stream.synthetic",
) -> DenseSampleBatch:
    return DenseSampleBatch(
        batch_id=batch_id,
        stream_id=stream_id,
        stream_revision=1,
        sequence_start=sequence_start,
        channel_ids=("signal.0",),
        values=np.asarray(values, dtype=np.float64).reshape(-1, 1),
        received_time=_time(available - 0.01),
        available_time=_time(available),
        first_sample_time=_time(sequence_start / 100.0, SAMPLE_CLOCK),
        sample_period_seconds=0.01,
    )


def _packets() -> tuple[DenseSampleBatch, ...]:
    return (
        _batch("input.1", 0, 1.0, (-1.0, -1.0)),
        _batch("input.2", 2, 2.0, (1.0, 1.0)),
        _batch("input.3", 4, 3.0, (2.0, 2.0)),
    )


def _binding(
    component_id: str,
    component: object,
    equivalence: EquivalenceLevel,
    *,
    boundary: ProcessBoundary = ProcessBoundary(),
) -> ComponentBinding:
    return ComponentBinding(component_id, VERSION, component, equivalence, boundary)


_COMPONENT_KINDS = {
    "source.synthetic": ComponentKind.SOURCE,
    "transform.identity": ComponentKind.TRANSFORM,
    "window.continuous": ComponentKind.WINDOW,
    "quality.finite": ComponentKind.QUALITY,
    "model.primary": ComponentKind.MODEL,
    "model.shadow": ComponentKind.MODEL,
    "policy.observe": ComponentKind.POLICY,
}


def _plan(extra_sources: tuple[str, ...] = ()) -> ExecutionPlan:
    kinds = dict(_COMPONENT_KINDS)
    for source_id in extra_sources:
        kinds[source_id] = ComponentKind.SOURCE
    locked = []
    planned = []
    for component_id, kind in kinds.items():
        plugin_id = f"fixture.{component_id}"
        locked.append(
            LockedPlugin(
                plugin_id=plugin_id,
                version=VERSION,
                kind=kind,
                descriptor_hash=canonical_hash(
                    {"plugin_id": plugin_id, "version": VERSION, "kind": kind.value}
                ),
                distribution="tests",
                implementation=f"tests:{component_id}",
            )
        )
        planned.append(
            PlannedComponent(
                component_id=component_id,
                plugin_id=plugin_id,
                plugin_version=VERSION,
                config={},
            )
        )
    return ExecutionPlan(
        plan_id="plan.phase3.synthetic",
        execution_mode=ExecutionMode.CAUSAL,
        plugins=tuple(locked),
        components=tuple(planned),
        spec_hashes={"suite": canonical_hash({"suite": "phase3.synthetic"})},
        clock_policy={"ordering": "availability_watermark"},
        recording_policy={"execution_capture": True},
        validation_rules={"equivalence": "semantic"},
    )


def _components(
    *,
    primary_threshold: float = 0.0,
    shadow_threshold: float = 0.5,
    primary_latency: float = 0.0,
    shadow_latency: float = 0.0,
    shadow_boundary: ProcessBoundary = ProcessBoundary(),
) -> EngineComponents:
    primary_role = ModelRole(
        "primary",
        ModelRoleKind.PRIMARY,
        scheduling_priority=100,
        may_request_actions=False,
        must_share_admitted_inputs_with=("shadow",),
    )
    shadow_role = ModelRole(
        "shadow",
        ModelRoleKind.SHADOW,
        scheduling_priority=10,
        must_share_admitted_inputs_with=("primary",),
    )
    return EngineComponents(
        transform=_binding(
            "transform.identity", IdentityTransform(), EquivalenceLevel.BITWISE
        ),
        window=_binding(
            "window.continuous",
            ContinuousWindowBuilder(window_samples=4, step_samples=2),
            EquivalenceLevel.BITWISE,
        ),
        quality=_binding(
            "quality.finite", FiniteQualityGate(), EquivalenceLevel.SEMANTIC
        ),
        models=(
            ModelBinding(
                _binding(
                    "model.primary",
                    MeanThresholdModel(
                        model_id="mean.primary",
                        role="primary",
                        threshold=primary_threshold,
                        latency_seconds=primary_latency,
                    ),
                    EquivalenceLevel.NUMERIC,
                ),
                primary_role,
                deadline_seconds=0.1,
            ),
            ModelBinding(
                _binding(
                    "model.shadow",
                    MeanThresholdModel(
                        model_id="mean.shadow",
                        role="shadow",
                        threshold=shadow_threshold,
                        latency_seconds=shadow_latency,
                    ),
                    EquivalenceLevel.NUMERIC,
                    boundary=shadow_boundary,
                ),
                shadow_role,
                deadline_seconds=0.1,
            ),
        ),
        policy=_binding(
            "policy.observe", ObserveOnlyPolicy(), EquivalenceLevel.SEMANTIC
        ),
    )


def _engine(
    packets: tuple[DenseSampleBatch, ...] = (),
    *,
    components: EngineComponents | None = None,
    scheduling: SchedulingPolicy | None = None,
    execution_id: str = "execution.live",
    source: object | None = None,
) -> ExecutionEngine:
    stream = _stream()
    actual_source = source or PacketSequenceSource(stream, packets or _packets())
    return ExecutionEngine(
        plan=_plan(),
        sources=(
            SourceBinding(
                _binding("source.synthetic", actual_source, EquivalenceLevel.BITWISE)
            ),
        ),
        components=components or _components(),
        scheduling=scheduling or SchedulingPolicy(EXECUTION_CLOCK),
        execution_id=execution_id,
    )


class DenseWindowTests(unittest.TestCase):
    def test_window_builder_is_bounded_and_records_exact_causal_lineage(self) -> None:
        builder = ContinuousWindowBuilder(window_samples=4, step_samples=2)
        from eegle.runtime import DeterministicIdSource, RuntimeExecutionContext

        ids = DeterministicIdSource()
        first = IdentityTransform().update(
            _packets()[0],
            RuntimeExecutionContext(
                "execution.window",
                "transform.identity",
                VERSION,
                ExecutionMode.CAUSAL,
                _time(1.0),
                {},
                ids,
            ),
        )
        second = IdentityTransform().update(
            _packets()[1],
            RuntimeExecutionContext(
                "execution.window",
                "transform.identity",
                VERSION,
                ExecutionMode.CAUSAL,
                _time(2.0),
                {},
                ids,
            ),
        )
        context = RuntimeExecutionContext(
            "execution.window",
            "window.continuous",
            VERSION,
            ExecutionMode.CAUSAL,
            _time(1.0),
            {},
            ids,
        )
        self.assertEqual(tuple(builder.update(first, context)), ())
        context.current_time = _time(2.0)
        windows = tuple(builder.update(second, context))

        self.assertEqual(len(windows), 1)
        window = windows[0]
        self.assertIsInstance(window, DenseWindow)
        self.assertEqual(window.input_ids, (first.batch_id, second.batch_id))
        self.assertEqual(window.lineage.latest_input_available_time, _time(2.0))
        self.assertEqual(window.lineage.stream_revisions["stream.synthetic"], 1)
        self.assertLess(builder.buffered_samples, builder.window_samples)
        self.assertEqual(DenseWindow.from_payload(window.to_payload()), window)

    def test_window_builder_snapshot_restores_pending_samples_exactly(self) -> None:
        from eegle.runtime import DeterministicIdSource, RuntimeExecutionContext

        first_builder = ContinuousWindowBuilder(4, 2)
        first_ids = DeterministicIdSource()
        first_context = RuntimeExecutionContext(
            "execution.window",
            "window.continuous",
            VERSION,
            ExecutionMode.CAUSAL,
            _time(1.0),
            {},
            first_ids,
        )
        self.assertEqual(tuple(first_builder.update(_packets()[0], first_context)), ())
        snapshot = first_builder.snapshot_state()
        restored_builder = ContinuousWindowBuilder(4, 2)
        restored_builder.restore_state(snapshot)

        first_context.current_time = _time(2.0)
        expected = tuple(first_builder.update(_packets()[1], first_context))
        restored_context = RuntimeExecutionContext(
            "execution.window",
            "window.continuous",
            VERSION,
            ExecutionMode.CAUSAL,
            _time(2.0),
            {},
            DeterministicIdSource(),
        )
        observed = tuple(restored_builder.update(_packets()[1], restored_context))

        self.assertEqual(observed, expected)
        self.assertEqual(restored_builder.snapshot_state(), first_builder.snapshot_state())


class ExecutionEngineVerticalSliceTests(unittest.TestCase):
    def test_vertical_slice_is_primary_first_observe_only_and_fully_evidenced(self) -> None:
        result = _engine().run()

        self.assertEqual(result.status, EngineStatus.COMPLETE)
        self.assertEqual(len(result.captured_packets), 3)
        self.assertEqual(len(result.predictions), 4)
        self.assertEqual(
            [prediction.role for prediction in result.predictions],
            ["primary", "shadow", "primary", "shadow"],
        )
        primary_inputs = [
            prediction.input_ids for prediction in result.predictions if prediction.role == "primary"
        ]
        shadow_inputs = [
            prediction.input_ids for prediction in result.predictions if prediction.role == "shadow"
        ]
        self.assertEqual(primary_inputs, shadow_inputs)
        self.assertTrue(
            all(
                work.status == WorkStatus.SKIPPED and work.reason_code == "observe_only"
                for work in result.work
                if work.stage == "policy"
            )
        )
        self.assertEqual(
            [record.sequence for record in result.evidence],
            list(range(len(result.evidence))),
        )
        record_types = {record.record_type for record in result.evidence}
        self.assertTrue(
            {
                "run_started",
                "input_admitted",
                "packet_produced",
                "window_produced",
                "quality_decision",
                "prediction",
                "work",
                "run_finished",
            }.issubset(record_types)
        )
        admitted = next(
            record for record in result.evidence if record.record_type == "input_admitted"
        )
        self.assertNotIn("values", admitted.payload["packet"])
        self.assertTrue(admitted.payload["packet"]["content_hash"].startswith("sha256:"))

    def test_replay_modes_use_same_engine_and_preserve_declared_equivalence(self) -> None:
        reference = _engine().run()

        def factory(
            packets: tuple[DenseSampleBatch, ...], mode: ReplayMode
        ) -> ExecutionEngine:
            source = ReplaySource(_stream(), packets, mode=mode)
            return _engine(
                packets,
                source=source,
                execution_id=f"execution.replay.{mode.value}",
            )

        runner = ReplayRunner(factory)
        for mode in (
            ReplayMode.ORIGINAL_AVAILABILITY,
            ReplayMode.ACCELERATED_CAUSAL,
        ):
            replayed = runner.run(
                reference,
                mode=mode,
                policy=EquivalencePolicy(requested_level=EquivalenceLevel.BITWISE),
            )
            self.assertEqual(replayed.result.status, EngineStatus.COMPLETE)
            self.assertTrue(replayed.equivalence.equivalent, replayed.equivalence.divergences)
            self.assertEqual(replayed.equivalence.evaluated_level, EquivalenceLevel.SEMANTIC)
            self.assertTrue(replayed.equivalence.downgraded)

    def test_changed_shadow_model_localizes_semantic_divergence(self) -> None:
        reference = _engine().run()

        def factory(
            packets: tuple[DenseSampleBatch, ...], mode: ReplayMode
        ) -> ExecutionEngine:
            return _engine(
                packets,
                source=ReplaySource(_stream(), packets, mode=mode),
                components=_components(shadow_threshold=2.0),
                execution_id="execution.counterfactual",
            )

        replayed = ReplayRunner(factory).run(
            reference,
            mode=ReplayMode.COUNTERFACTUAL,
            policy=EquivalencePolicy(requested_level=EquivalenceLevel.SEMANTIC),
        )

        self.assertFalse(replayed.equivalence.equivalent)
        prediction_divergences = [
            value
            for value in replayed.equivalence.divergences
            if value.record_type == "prediction"
        ]
        self.assertTrue(prediction_divergences)
        self.assertTrue(
            any("outputs.label" in value.path for value in prediction_divergences),
            prediction_divergences,
        )

    def test_primary_deadline_is_accounted_before_shadow_and_policy(self) -> None:
        result = _engine(components=_components(primary_latency=0.2)).run()
        model_work = [value for value in result.work if value.stage == "model"]

        self.assertEqual(result.status, EngineStatus.COMPLETE)
        self.assertEqual(model_work[0].role, "primary")
        self.assertEqual(model_work[0].status, WorkStatus.TIMED_OUT)
        self.assertFalse(any(value.stage == "policy" for value in result.work))

    def test_proxy_placement_is_evidence_not_a_second_semantic_engine(self) -> None:
        boundary = ProcessBoundary(
            ComponentPlacement.SUBPROCESS_PROXY, endpoint_id="worker.shadow"
        )
        result = _engine(components=_components(shadow_boundary=boundary)).run()
        started = [
            record.payload
            for record in result.evidence
            if record.record_type == "component_started"
            and record.payload["component_id"] == "model.shadow"
        ]

        self.assertEqual(result.status, EngineStatus.COMPLETE)
        self.assertEqual(started[0]["placement"], "subprocess_proxy")
        self.assertEqual(started[0]["endpoint_id"], "worker.shadow")

    def test_quality_rejection_prevents_all_model_work(self) -> None:
        rejected_packets = []
        for packet in _packets():
            values = np.full(packet.values.shape, np.nan)
            rejected_packets.append(
                replace(
                    packet,
                    values=values,
                    validity_mask=np.zeros(values.shape, dtype=bool),
                )
            )
        result = _engine(tuple(rejected_packets)).run()

        self.assertEqual(result.status, EngineStatus.COMPLETE)
        self.assertEqual(result.predictions, ())
        quality_work = [value for value in result.work if value.stage == "quality"]
        self.assertTrue(quality_work)
        self.assertTrue(all(value.status == WorkStatus.REJECTED for value in quality_work))

    def test_shadow_is_skipped_under_declared_primary_first_pressure(self) -> None:
        scheduling = SchedulingPolicy(EXECUTION_CLOCK, shadow_queue_limit=0)
        result = _engine(scheduling=scheduling).run()

        self.assertEqual(
            [prediction.role for prediction in result.predictions],
            ["primary", "primary"],
        )
        skipped = [
            value
            for value in result.work
            if value.reason_code == "primary_first_backpressure"
        ]
        self.assertEqual(len(skipped), 2)
        self.assertTrue(all(value.role == "shadow" for value in skipped))

    def test_shadow_failure_can_degrade_without_corrupting_primary_results(self) -> None:
        class FailingShadow:
            def predict(self, item, context):
                raise RuntimeError("shadow unavailable")

        components = _components()
        shadow = components.models[1]
        failing_shadow = replace(
            shadow,
            component=_binding(
                "model.shadow", FailingShadow(), EquivalenceLevel.TRACE
            ),
        )
        components = replace(components, models=(components.models[0], failing_shadow))
        result = _engine(
            components=components,
            scheduling=SchedulingPolicy(EXECUTION_CLOCK, fail_fast=False),
        ).run()

        self.assertEqual(result.status, EngineStatus.COMPLETE)
        self.assertEqual(
            [prediction.role for prediction in result.predictions],
            ["primary", "primary"],
        )
        failures = [value for value in result.work if value.status == WorkStatus.FAILED]
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(value.role == "shadow" for value in failures))

    def test_component_failure_returns_failed_run_with_terminal_work(self) -> None:
        class FailingTransform:
            def update(self, packet, context):
                raise RuntimeError("transform unavailable")

        components = replace(
            _components(),
            transform=_binding(
                "transform.identity", FailingTransform(), EquivalenceLevel.TRACE
            ),
        )
        result = _engine(components=components).run()

        self.assertEqual(result.status, EngineStatus.FAILED)
        self.assertIn("transform unavailable", result.failure)
        failed = [value for value in result.work if value.status == WorkStatus.FAILED]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].stage, "transform")
        self.assertEqual(result.evidence[-1].record_type, "run_finished")

    def test_optional_component_lifecycle_runs_inside_engine_boundary(self) -> None:
        class LifecycleIdentity(IdentityTransform):
            started = False
            stopped = False

            def start(self, context):
                self.started = True

            def stop(self, context):
                self.stopped = True

        transform = LifecycleIdentity()
        components = replace(
            _components(),
            transform=_binding(
                "transform.identity", transform, EquivalenceLevel.BITWISE
            ),
        )
        result = _engine(components=components).run()

        self.assertEqual(result.status, EngineStatus.COMPLETE)
        self.assertTrue(transform.started)
        self.assertTrue(transform.stopped)


class SchedulingFailureTests(unittest.TestCase):
    def test_multi_source_packets_dispatch_by_availability_not_poll_order(self) -> None:
        stream = _stream()
        late_source = PacketSequenceSource(
            stream, (_batch("input.later", 2, 2.0, (2.0, 2.0)),)
        )
        early_source = PacketSequenceSource(
            stream, (_batch("input.earlier", 0, 1.0, (1.0, 1.0)),)
        )
        plan = _plan(("source.early",))
        engine = ExecutionEngine(
            plan=plan,
            sources=(
                SourceBinding(
                    _binding("source.synthetic", late_source, EquivalenceLevel.BITWISE)
                ),
                SourceBinding(_binding("source.early", early_source, EquivalenceLevel.BITWISE)),
            ),
            components=_components(),
            scheduling=SchedulingPolicy(EXECUTION_CLOCK),
        )

        result = engine.run()
        produced = [
            record.payload["packet"]["lineage"]["input_ids"][0]
            for record in result.evidence
            if record.record_type == "packet_produced"
        ]
        self.assertEqual(result.status, EngineStatus.COMPLETE)
        self.assertEqual(produced, ["input.earlier", "input.later"])

    def test_late_packet_is_rejected_with_terminal_work(self) -> None:
        packets = (
            _batch("input.first", 0, 2.0, (1.0, 1.0)),
            _batch("input.late", 2, 1.0, (2.0, 2.0)),
        )
        result = _engine(packets).run()
        late = [value for value in result.work if value.reason_code == "late_packet"]

        self.assertEqual(result.status, EngineStatus.COMPLETE)
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0].status, WorkStatus.REJECTED)
        self.assertEqual(late[0].input_ids, ("input.late",))

    def test_backpressure_rejects_newest_and_leaves_blocked_input_pending(self) -> None:
        class BlockingSource:
            stream_spec = _stream("stream.blocker")
            exhausted = False
            watermark = _time(0.0)

            def read(self):
                return None

            def close(self):
                return None

        source = PacketSequenceSource(_stream(), _packets())
        plan = _plan(("source.blocker",))
        engine = ExecutionEngine(
            plan=plan,
            sources=(
                SourceBinding(
                    _binding("source.synthetic", source, EquivalenceLevel.BITWISE)
                ),
                SourceBinding(
                    _binding("source.blocker", BlockingSource(), EquivalenceLevel.TRACE)
                ),
            ),
            components=_components(),
            scheduling=SchedulingPolicy(
                EXECUTION_CLOCK,
                max_pending_packets=1,
                backpressure=BackpressurePolicy.REJECT_NEWEST,
                max_idle_cycles=1,
            ),
        )

        result = engine.run()
        backpressure = [value for value in result.work if value.reason_code == "backpressure"]
        pending = [value for value in result.work if value.status == WorkStatus.PENDING]
        self.assertEqual(result.status, EngineStatus.PARTIAL)
        self.assertEqual(len(backpressure), 2)
        self.assertEqual(len(pending), 1)

    def test_cancellation_is_explicit(self) -> None:
        engine = _engine()
        engine.cancel()
        result = engine.run()
        self.assertEqual(result.status, EngineStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
