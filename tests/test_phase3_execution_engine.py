from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

import numpy as np

from eegle._domain import ComponentKind, EquivalenceLevel, ExecutionMode, WorkStatus
from eegle.actions import ActionCommand, ObserveOnlyPolicy, SimulatedActuator
from eegle.compiler import ExecutionPlan, LockedPlugin, PlannedComponent, canonical_hash
from eegle.models import MeanThresholdModel, ModelRole, ModelRoleKind
from eegle.processing import (
    ContinuousWindowBuilder,
    DenseWindow,
    FiniteQualityGate,
    IdentityTransform,
)
from eegle.recording import EvidenceReader, Session, persist_engine_run
from eegle.replay import (
    BundleReplayRunner,
    EquivalencePolicy,
    ReplayMode,
    ReplayRunner,
    ReplaySource,
    compare_runs,
)
from eegle.runtime import (
    BackpressurePolicy,
    ComponentBinding,
    ComponentPlacement,
    EngineCheckpoint,
    EngineCheckpointError,
    EngineComponents,
    EngineStatus,
    ExecutionEngine,
    ModelBinding,
    Outcome,
    OutcomeRoutingPolicy,
    OutcomeUse,
    PendingPredictionOverflow,
    ProcessBoundary,
    ScheduledTrigger,
    SchedulingPolicy,
    SourceBinding,
    StateTransition,
    StateTriggerRule,
    TransitionStatus,
    TriggerBinding,
    TriggerDisposition,
    TriggerResult,
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


def _outcome(
    outcome_id: str,
    available: float,
    prediction_ids: tuple[str, ...],
    *,
    uses: frozenset[OutcomeUse] = frozenset({OutcomeUse.METRICS}),
    clock: str = EXECUTION_CLOCK,
) -> Outcome:
    return Outcome(
        outcome_id=outcome_id,
        subject_id="subject.synthetic",
        source_id="source.outcomes",
        value={"target": "positive"},
        event_time=_time(0.5, clock),
        available_time=_time(available, clock),
        permitted_uses=uses,
        prediction_ids=prediction_ids,
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


def _plan(
    extra_sources: tuple[str, ...] = (),
    extra_components: dict[str, ComponentKind] | None = None,
) -> ExecutionPlan:
    kinds = dict(_COMPONENT_KINDS)
    for source_id in extra_sources:
        kinds[source_id] = ComponentKind.SOURCE
    kinds.update(extra_components or {})
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
    primary_may_request_actions: bool = False,
    policy: object | None = None,
    trigger_handlers: tuple[tuple[str, object], ...] = (),
    actuator: object | None = None,
) -> EngineComponents:
    primary_role = ModelRole(
        "primary",
        ModelRoleKind.PRIMARY,
        scheduling_priority=100,
        may_request_actions=primary_may_request_actions,
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
            "policy.observe", policy or ObserveOnlyPolicy(), EquivalenceLevel.SEMANTIC
        ),
        trigger_handlers=tuple(
            TriggerBinding(_binding(component_id, handler, EquivalenceLevel.SEMANTIC))
            for component_id, handler in trigger_handlers
        ),
        actuator=None
        if actuator is None
        else _binding("actuator.simulated", actuator, EquivalenceLevel.SEMANTIC),
    )


def _engine(
    packets: tuple[DenseSampleBatch, ...] = (),
    *,
    components: EngineComponents | None = None,
    scheduling: SchedulingPolicy | None = None,
    execution_id: str = "execution.live",
    source: object | None = None,
    plan: ExecutionPlan | None = None,
    outcomes: tuple[Outcome, ...] = (),
    outcome_policy: OutcomeRoutingPolicy | None = None,
    scheduled_triggers: tuple[ScheduledTrigger, ...] = (),
    state_triggers: tuple[StateTriggerRule, ...] = (),
    clock_mapping_revisions: dict[str, int] | None = None,
) -> ExecutionEngine:
    stream = _stream()
    actual_source = source or PacketSequenceSource(stream, packets or _packets())
    actual_components = components or _components()
    extra_components = {
        value.component.component_id: ComponentKind.ADAPTER
        for value in actual_components.trigger_handlers
    }
    if actual_components.actuator is not None:
        extra_components[actual_components.actuator.component_id] = ComponentKind.ACTUATOR
    return ExecutionEngine(
        plan=plan or _plan(extra_components=extra_components),
        sources=(
            SourceBinding(
                _binding("source.synthetic", actual_source, EquivalenceLevel.BITWISE)
            ),
        ),
        components=actual_components,
        scheduling=scheduling or SchedulingPolicy(EXECUTION_CLOCK),
        execution_id=execution_id,
        outcomes=outcomes,
        outcome_policy=outcome_policy,
        scheduled_triggers=scheduled_triggers,
        state_triggers=state_triggers,
        clock_mapping_revisions=clock_mapping_revisions,
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

    def test_plan_bearing_evidence_bundle_replays_without_in_memory_run_result(self) -> None:
        reference = _engine().run()
        plan = _plan()
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.create(Path(tmp) / "session", session_id="session.bundle-replay")
            bundle = persist_engine_run(
                session,
                reference,
                plan=plan,
                streams=(_stream(),),
            )
            reader = EvidenceReader.open(Session.open(session.root), bundle.bundle_id)

            def factory(
                restored_plan: ExecutionPlan,
                packets: tuple[DenseSampleBatch, ...],
                streams: tuple[StreamSpec, ...],
                mode: ReplayMode,
            ) -> ExecutionEngine:
                self.assertEqual(restored_plan, plan)
                self.assertEqual(streams, (_stream(),))
                return _engine(
                    packets,
                    source=ReplaySource(streams[0], packets, mode=mode),
                    execution_id="execution.bundle-replay",
                )

            replayed = BundleReplayRunner(factory).run(
                reader,
                policy=EquivalencePolicy(requested_level=EquivalenceLevel.SEMANTIC),
            )

            self.assertTrue(replayed.equivalence.equivalent, replayed.equivalence.divergences)
            self.assertEqual(replayed.result.status, EngineStatus.COMPLETE)

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


class DelayedOutcomeTests(unittest.TestCase):
    def test_outcome_waits_for_availability_matches_identity_and_stays_label_blind(self) -> None:
        outcome = _outcome(
            "outcome.match",
            2.0,
            ("prediction.00000001",),
            uses=frozenset({OutcomeUse.METRICS, OutcomeUse.ADAPTATION}),
        )
        result = _engine(outcomes=(outcome,)).run()

        matched = next(
            record for record in result.evidence if record.record_type == "outcome_matched"
        )
        prediction = next(
            record for record in result.evidence if record.record_type == "prediction"
        )
        eligibility = next(
            record
            for record in result.evidence
            if record.record_type == "adaptation_eligibility"
        )
        self.assertEqual(matched.emitted_time, _time(2.0))
        self.assertEqual(
            matched.payload["prediction_ids"], ("prediction.00000001",)
        )
        self.assertNotIn("outcome", prediction.payload["prediction"])
        self.assertNotIn("correctness", prediction.payload["prediction"])
        self.assertNotIn("condition", prediction.payload["prediction"])
        self.assertTrue(eligibility.payload["eligible"])
        self.assertFalse(eligibility.payload["adaptation_applied"])

    def test_duplicate_unmatched_malformed_and_disallowed_outcomes_are_accounted(self) -> None:
        uses = frozenset({OutcomeUse.METRICS, OutcomeUse.ADAPTATION})
        result = _engine(
            outcomes=(
                _outcome("outcome.duplicate", 2.0, ("prediction.00000001",), uses=uses),
                _outcome("outcome.duplicate", 2.1, ("prediction.00000001",), uses=uses),
                _outcome("outcome.unmatched", 2.2, ("prediction.missing",)),
                _outcome("outcome.no_identity", 2.3, ()),
                _outcome(
                    "outcome.bad_clock",
                    2.4,
                    ("prediction.00000002",),
                    clock="clock.other",
                ),
            ),
            outcome_policy=OutcomeRoutingPolicy(
                allowed_uses=frozenset({OutcomeUse.METRICS})
            ),
        ).run()

        record_types = [record.record_type for record in result.evidence]
        self.assertIn("outcome_duplicate", record_types)
        self.assertIn("outcome_unmatched", record_types)
        rejected_ids = {
            record.payload["outcome_id"]
            for record in result.evidence
            if record.record_type == "outcome_rejected"
        }
        self.assertEqual(
            rejected_ids, {"outcome.no_identity", "outcome.bad_clock"}
        )
        disallowed = [
            work
            for work in result.work
            if work.reason_code == "disallowed_outcome_use"
        ]
        self.assertEqual(len(disallowed), 1)
        self.assertEqual(disallowed[0].details["use"], "adaptation")
        self.assertTrue(
            all(
                work.status != WorkStatus.PENDING
                for work in result.work
                if work.stage == "outcome"
            )
        )

    def test_pending_prediction_overflow_and_expiry_are_bounded_and_deterministic(self) -> None:
        result = _engine(
            outcome_policy=OutcomeRoutingPolicy(
                max_pending_predictions=1,
                prediction_ttl_seconds=0.25,
                overflow=PendingPredictionOverflow.EXPIRE_OLDEST,
            )
        ).run()

        pending_counts = [
            int(record.payload["pending_count"])
            for record in result.evidence
            if record.record_type == "prediction_pending"
        ]
        overflowed = [
            record.payload["prediction_id"]
            for record in result.evidence
            if record.record_type == "prediction_overflowed"
        ]
        expired = [
            record.payload["prediction_id"]
            for record in result.evidence
            if record.record_type == "prediction_expired"
        ]
        self.assertTrue(pending_counts)
        self.assertLessEqual(max(pending_counts), 1)
        self.assertEqual(
            overflowed,
            ["prediction.00000001", "prediction.00000003"],
        )
        self.assertEqual(expired, ["prediction.00000002"])
        self.assertEqual(
            sum(
                record.record_type == "prediction_pending_at_end"
                for record in result.evidence
            ),
            1,
        )

    def test_outcome_lifecycle_replays_through_the_same_engine(self) -> None:
        outcomes = (
            _outcome("outcome.replay", 2.5, ("prediction.00000001",)),
        )
        reference = _engine(outcomes=outcomes).run()

        def factory(
            packets: tuple[DenseSampleBatch, ...], mode: ReplayMode
        ) -> ExecutionEngine:
            return _engine(
                packets,
                source=ReplaySource(_stream(), packets, mode=mode),
                execution_id="execution.outcome.replay",
                outcomes=outcomes,
            )

        replayed = ReplayRunner(factory).run(
            reference,
            policy=EquivalencePolicy(requested_level=EquivalenceLevel.SEMANTIC),
        )
        self.assertTrue(replayed.equivalence.equivalent, replayed.equivalence.divergences)


class TriggerAndCheckpointTests(unittest.TestCase):
    def test_packet_outcome_and_trigger_ties_have_one_locked_order(self) -> None:
        class Handler:
            def handle_trigger(self, trigger, context):
                return TriggerResult()

        components = _components(
            trigger_handlers=(("adapter.trigger", Handler()),)
        )
        result = _engine(
            components=components,
            outcomes=(
                _outcome("outcome.tie", 2.0, ("prediction.00000001",)),
            ),
            scheduled_triggers=(
                ScheduledTrigger("trigger.tie", "adapter.trigger", _time(2.0)),
            ),
        ).run()

        ordered = [
            record.record_type
            for record in result.evidence
            if record.emitted_time == _time(2.0)
            and record.record_type
            in {"packet_produced", "outcome_matched", "trigger_fired"}
        ]
        self.assertEqual(
            ordered, ["packet_produced", "outcome_matched", "trigger_fired"]
        )

    def test_time_trigger_waits_for_a_causally_safe_source_frontier(self) -> None:
        class Handler:
            def handle_trigger(self, trigger, context):
                raise AssertionError("unsafe trigger fired")

        class BlockingSource:
            stream_spec = _stream()
            exhausted = False
            watermark = _time(0.0)

            def read(self):
                return None

            def close(self):
                return None

        components = _components(
            trigger_handlers=(("adapter.trigger", Handler()),)
        )
        result = _engine(
            components=components,
            source=BlockingSource(),
            scheduled_triggers=(
                ScheduledTrigger("trigger.blocked", "adapter.trigger", _time(1.0)),
            ),
        ).run()

        self.assertEqual(result.status, EngineStatus.PARTIAL)
        self.assertFalse(
            any(record.record_type == "trigger_fired" for record in result.evidence)
        )
        self.assertTrue(
            any(
                work.stage == "trigger"
                and work.status == WorkStatus.PENDING
                and work.reason_code == "awaiting_watermark"
                for work in result.work
            )
        )

    def test_state_trigger_reschedule_cancellation_and_failure_are_explicit(self) -> None:
        class Handler:
            def handle_trigger(self, trigger, context):
                mode = trigger.payload.get("mode")
                if mode == "fail":
                    raise RuntimeError("scheduled failure")
                if mode == "reschedule":
                    return TriggerResult(
                        TriggerDisposition.RESCHEDULED,
                        next_trigger=ScheduledTrigger(
                            "trigger.rescheduled",
                            "adapter.trigger",
                            _time(context.current_time.seconds + 0.05),
                            payload={"mode": "complete"},
                            parent_trigger_id=trigger.trigger_id,
                        ),
                    )
                if mode == "transition":
                    transition = StateTransition(
                        transition_id=context.next_id("transition"),
                        component_id="adapter.trigger",
                        status=TransitionStatus.APPLIED,
                        transition_kind="fixture_advanced",
                        transition_time=context.current_time,
                        prior_state_hash=canonical_hash({"step": 0}),
                        resulting_state_hash=canonical_hash({"step": 1}),
                        trigger_ids=(trigger.trigger_id,),
                    )
                    return TriggerResult(transition=transition)
                return TriggerResult()

        components = _components(
            trigger_handlers=(("adapter.trigger", Handler()),)
        )
        engine = _engine(
            components=components,
            scheduling=SchedulingPolicy(EXECUTION_CLOCK, fail_fast=False),
            scheduled_triggers=(
                ScheduledTrigger(
                    "trigger.transition",
                    "adapter.trigger",
                    _time(1.1),
                    payload={"mode": "transition"},
                ),
                ScheduledTrigger(
                    "trigger.timeout",
                    "adapter.trigger",
                    _time(1.15),
                    deadline_time=_time(1.16),
                ),
                ScheduledTrigger(
                    "trigger.reschedule",
                    "adapter.trigger",
                    _time(1.2),
                    payload={"mode": "reschedule"},
                ),
                ScheduledTrigger(
                    "trigger.cancel",
                    "adapter.trigger",
                    _time(1.3),
                    deadline_time=_time(1.4),
                ),
                ScheduledTrigger(
                    "trigger.fail",
                    "adapter.trigger",
                    _time(1.4),
                    payload={"mode": "fail"},
                ),
            ),
            state_triggers=(
                StateTriggerRule(
                    "rule.after_transition",
                    "adapter.trigger",
                    transition_kind="fixture_advanced",
                    payload={"mode": "complete"},
                ),
            ),
        )
        engine.cancel_trigger("trigger.cancel")
        result = engine.run()

        record_types = [record.record_type for record in result.evidence]
        self.assertIn("state_transition", record_types)
        self.assertIn("state_trigger_scheduled", record_types)
        self.assertIn("trigger_rescheduled", record_types)
        self.assertIn("trigger_timed_out", record_types)
        self.assertIn("trigger_cancelled", record_types)
        self.assertIn("trigger_failed", record_types)
        self.assertEqual(result.status, EngineStatus.COMPLETE)
        self.assertTrue(
            any(
                work.stage == "trigger" and work.status == WorkStatus.FAILED
                for work in result.work
            )
        )

    def test_fresh_engine_checkpoint_restore_matches_uninterrupted_semantics(self) -> None:
        class TransitionHandler:
            def handle_trigger(self, trigger, context):
                return TriggerResult(
                    transition=StateTransition(
                        transition_id=context.next_id("transition"),
                        component_id="adapter.checkpoint",
                        status=TransitionStatus.APPLIED,
                        transition_kind="checkpoint_fixture_advanced",
                        transition_time=context.current_time,
                        prior_state_hash=canonical_hash({"step": 0}),
                        resulting_state_hash=canonical_hash({"step": 1}),
                        trigger_ids=(trigger.trigger_id,),
                    )
                )

        def components() -> EngineComponents:
            return _components(
                trigger_handlers=(("adapter.checkpoint", TransitionHandler()),)
            )

        outcomes = (
            _outcome("outcome.after_checkpoint", 2.5, ("prediction.00000001",)),
        )
        triggers = (
            ScheduledTrigger(
                "trigger.after_checkpoint", "adapter.checkpoint", _time(2.6)
            ),
        )
        uninterrupted = _engine(
            components=components(),
            outcomes=outcomes,
            scheduled_triggers=triggers,
        ).run()
        partial = _engine(
            components=components(),
            outcomes=outcomes,
            scheduled_triggers=triggers,
        ).run(checkpoint_after_semantic_items=2)

        self.assertEqual(partial.status, EngineStatus.PARTIAL)
        self.assertIsNotNone(partial.checkpoint)
        checkpoint = partial.checkpoint
        assert checkpoint is not None
        self.assertEqual(
            EngineCheckpoint.from_payload(checkpoint.to_payload()), checkpoint
        )
        self.assertEqual(checkpoint.state["run_status"], EngineStatus.PARTIAL.value)
        resumed_engine = _engine(
            components=components(),
            outcomes=outcomes,
            scheduled_triggers=triggers,
        )
        resumed_engine.restore_checkpoint(checkpoint)
        resumed = resumed_engine.run()
        comparison = compare_runs(
            uninterrupted,
            resumed,
            EquivalencePolicy(requested_level=EquivalenceLevel.SEMANTIC),
        )

        self.assertEqual(resumed.status, EngineStatus.COMPLETE)
        self.assertTrue(comparison.equivalent, comparison.divergences)
        self.assertEqual(
            [value.prediction_id for value in resumed.predictions],
            [value.prediction_id for value in uninterrupted.predictions],
        )
        self.assertEqual(
            [value.work_id for value in resumed.work],
            [value.work_id for value in uninterrupted.work],
        )
        self.assertEqual(
            [
                record.payload["transition"]["transition_id"]
                for record in resumed.evidence
                if record.record_type == "state_transition"
            ],
            [
                record.payload["transition"]["transition_id"]
                for record in uninterrupted.evidence
                if record.record_type == "state_transition"
            ],
        )
        self.assertEqual(
            [record.record_hash for record in resumed.evidence[: len(checkpoint.state["evidence_prefix"])]],
            [record["record_hash"] for record in checkpoint.state["evidence_prefix"]],
        )

    def test_checkpoint_rejects_integrity_plan_and_source_mismatches(self) -> None:
        partial = _engine().run(checkpoint_after_semantic_items=1)
        checkpoint = partial.checkpoint
        assert checkpoint is not None

        tampered = checkpoint.to_payload()
        tampered["state"]["semantic_items"] = 99
        with self.assertRaisesRegex(ValueError, "checkpoint hash mismatch"):
            EngineCheckpoint.from_payload(tampered)

        different_plan = replace(
            _plan(), spec_hashes={"suite": canonical_hash({"suite": "different"})}
        )
        with self.assertRaisesRegex(EngineCheckpointError, "plan mismatch"):
            _engine(plan=different_plan).restore_checkpoint(checkpoint)

        with self.assertRaisesRegex(EngineCheckpointError, "scheduling policy mismatch"):
            _engine(
                scheduling=SchedulingPolicy(EXECUTION_CLOCK, max_idle_cycles=2)
            ).restore_checkpoint(checkpoint)

        with self.assertRaisesRegex(EngineCheckpointError, "clock-mapping revisions mismatch"):
            _engine(clock_mapping_revisions={"mapping.host": 2}).restore_checkpoint(
                checkpoint
            )

        changed_packets = (
            _batch("input.1", 0, 1.0, (-9.0, -9.0)),
            *_packets()[1:],
        )
        with self.assertRaisesRegex(EngineCheckpointError, "source.synthetic restore failed"):
            _engine(changed_packets).restore_checkpoint(checkpoint)


class SimulatedActuatorTests(unittest.TestCase):
    def test_simulated_action_receipts_replay_and_survive_checkpoint_restore(self) -> None:
        class ActionPolicy:
            def decide(self, prediction, state, context):
                return ActionCommand(
                    command_id=context.next_id("command"),
                    capability="fixture.pulse",
                    requested_by="policy.action",
                    parameters={"decision": prediction.outputs["label"]},
                    requested_time=context.current_time,
                    available_time=context.current_time,
                    prediction_id=prediction.prediction_id,
                )

        def components() -> EngineComponents:
            return _components(
                primary_may_request_actions=True,
                policy=ActionPolicy(),
                actuator=SimulatedActuator(),
            )

        reference = _engine(components=components()).run()
        partial = _engine(components=components()).run(
            checkpoint_after_semantic_items=2
        )
        checkpoint = partial.checkpoint
        assert checkpoint is not None
        restored_engine = _engine(components=components())
        restored_engine.restore_checkpoint(checkpoint)
        restored = restored_engine.run()
        restored_comparison = compare_runs(
            reference,
            restored,
            EquivalencePolicy(requested_level=EquivalenceLevel.SEMANTIC),
        )

        def replay_factory(
            packets: tuple[DenseSampleBatch, ...], mode: ReplayMode
        ) -> ExecutionEngine:
            return _engine(
                packets,
                source=ReplaySource(_stream(), packets, mode=mode),
                components=components(),
                execution_id="execution.action.replay",
            )

        replayed = ReplayRunner(replay_factory).run(
            reference,
            policy=EquivalencePolicy(requested_level=EquivalenceLevel.SEMANTIC),
        )
        commands = [
            record.payload["command"]["command_id"]
            for record in reference.evidence
            if record.record_type == "action_command"
        ]
        receipts = [
            record.payload["receipt"]
            for record in reference.evidence
            if record.record_type == "action_receipt"
        ]
        restored_commands = [
            record.payload["command"]["command_id"]
            for record in restored.evidence
            if record.record_type == "action_command"
        ]

        self.assertTrue(commands)
        self.assertEqual(commands, restored_commands)
        self.assertEqual(
            [receipt["command_id"] for receipt in receipts], commands
        )
        self.assertTrue(all(receipt["details"]["simulated"] for receipt in receipts))
        self.assertTrue(restored_comparison.equivalent, restored_comparison.divergences)
        self.assertTrue(replayed.equivalence.equivalent, replayed.equivalence.divergences)


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
