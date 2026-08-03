from __future__ import annotations

import tempfile
import tracemalloc
import unittest
from copy import deepcopy
from pathlib import Path
from time import perf_counter

from eegle.compiler.lock import canonical_hash
from eegle.integrations.lsl import LslSource
from eegle.recording import (
    EvidenceReader,
    EvidenceRecord,
    EvidenceStatus,
    EvidenceWriter,
    IncrementalEngineCaptureSink,
    IntegrityIssueCode,
    IntegrityStatus,
    Session,
    read_framed_sample_store,
)
from eegle.recording.ledgers import EvidenceLedgerWriter, read_evidence_ledger
from eegle.runtime import EngineStatus, ExecutionEngine
from eegle.runtime.queueing import EventQueue, QueuedEvent
from eegle.streams import DenseSampleBatch, StreamSpec, TimePoint
from eegle.validation import (
    FaultOutcome,
    QualificationProfile,
    ValidationStatus,
    performance_fault_qualification_profile,
    validate_evidence,
    validate_qualification_profile_payload,
)
from tests.test_phase5_plan_execution import (
    _compile,
    _dense_packet,
    _payload,
    _recording_suite,
)
from tests.test_phase7_lsl_integration import (
    _compiled_lsl_recording,
    _network,
)


class _OnDemandDenseSource:
    """Generate a large finite workload without pre-retaining its packets."""

    def __init__(self, stream_spec: StreamSpec, packet_count: int) -> None:
        self.stream_spec = stream_spec
        self.packet_count = int(packet_count)
        self.index = 0
        self.exhausted = self.packet_count == 0
        self.watermark: TimePoint | None = None

    def read(self) -> DenseSampleBatch | None:
        if self.exhausted:
            return None
        index = self.index
        self.index += 1
        self.exhausted = self.index >= self.packet_count
        packet = _dense_packet(
            f"batch.performance.{index}",
            (index + 1) * 0.04,
            sequence_start=index * 4,
        )
        self.watermark = packet.available_time
        return packet

    def close(self) -> None:
        self.exhausted = True

    def snapshot_state(self) -> dict[str, object]:
        return {
            "index": self.index,
            "exhausted": self.exhausted,
            "watermark": (
                None if self.watermark is None else self.watermark.to_payload()
            ),
        }

    def restore_state(self, state: dict[str, object]) -> None:
        self.index = int(state["index"])
        self.exhausted = bool(state["exhausted"])
        payload = state["watermark"]
        self.watermark = (
            None if payload is None else TimePoint.from_payload(payload)
        )


class Phase8PerformanceQualificationTests(unittest.TestCase):
    def test_packaged_qualification_profile_is_typed_hashed_and_schema_valid(self) -> None:
        profile = performance_fault_qualification_profile()

        validate_qualification_profile_payload(profile.to_payload())
        self.assertEqual(
            [value.budget_id for value in profile.performance_budgets],
            sorted(value.budget_id for value in profile.performance_budgets),
        )
        self.assertEqual(
            [value.scenario_id for value in profile.fault_scenarios],
            sorted(value.scenario_id for value in profile.fault_scenarios),
        )
        self.assertEqual(len(profile.profile_hash), 71)
        tampered = profile.to_payload()
        tampered["performance_budgets"][0]["threshold"] = 999.0
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            QualificationProfile.from_payload(tampered)

    def test_compact_long_session_meets_memory_throughput_and_latency_budgets(
        self,
    ) -> None:
        profile = performance_fault_qualification_profile()
        packet_count = 5000
        suite = _recording_suite()
        sink_spec = next(
            value for value in suite["components"] if value["component_id"] == "sink.dense"
        )
        sink_spec["config"] = {"retention_limit": 0}
        deployment = deepcopy(_payload("deployment.json"))
        stream = StreamSpec.from_payload(
            deployment["component_bindings"][0]["config"]["stream_spec"]
        )
        compiled, registry = _compile(suite, deployment)
        source = _OnDemandDenseSource(stream, packet_count)
        evidence_count = 0
        capture_count = 0

        def count_evidence(_: EvidenceRecord) -> None:
            nonlocal evidence_count
            evidence_count += 1

        def count_capture(_: DenseSampleBatch) -> None:
            nonlocal capture_count
            capture_count += 1

        engine = ExecutionEngine.from_plan(
            compiled.plan,
            registry,
            component_overrides={"source.neural": source},
            evidence_sink=count_evidence,
            capture_sink=count_capture,
            retain_evidence=False,
            retain_phase_details=False,
        )

        tracemalloc.start()
        started = perf_counter()
        result = engine.run()
        elapsed = perf_counter() - started
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        throughput = packet_count / elapsed
        mean_latency = elapsed / packet_count
        peak_mebibytes = peak_bytes / (1024 * 1024)
        self.assertEqual(result.status, EngineStatus.COMPLETE, result.failure)
        self.assertEqual(capture_count, packet_count)
        self.assertGreater(evidence_count, packet_count)
        self.assertEqual(result.captured_packets, ())
        self.assertEqual(result.work, ())
        sink = engine.runtime.node("sink.dense").component
        self.assertEqual(sink.total_count, packet_count)
        self.assertEqual(sink.records, ())
        self.assertTrue(
            profile.budget("engine.compact.packet_throughput").accepts(throughput),
            throughput,
        )
        self.assertTrue(
            profile.budget("engine.compact.mean_dispatch_latency").accepts(
                mean_latency
            ),
            mean_latency,
        )
        self.assertTrue(
            profile.budget("engine.compact.peak_memory").accepts(peak_mebibytes),
            peak_mebibytes,
        )

    def test_incremental_capture_is_durable_and_bounded(self) -> None:
        profile = performance_fault_qualification_profile()
        packet_count = 250
        stream = StreamSpec.from_payload(
            _payload("deployment.json")["component_bindings"][0]["config"][
                "stream_spec"
            ]
        )
        with tempfile.TemporaryDirectory() as root:
            session = Session.create(
                Path(root) / "session",
                session_id="session.performance.capture",
            )
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.performance.capture",
                plan_hash=canonical_hash({"plan": "performance-capture"}),
                created_time=TimePoint(0.0, "boundary.clock"),
                durable=True,
            )
            writer.append(
                EvidenceRecord(
                    "evidence.capture.started",
                    "run_started",
                    0,
                    TimePoint(0.0, "boundary.clock"),
                    {},
                )
            )
            sink = IncrementalEngineCaptureSink(writer, (stream,), durable=True)

            started = perf_counter()
            for index in range(packet_count):
                sink(
                    _dense_packet(
                        f"batch.capture.{index}",
                        (index + 1) * 0.04,
                        sequence_start=index * 4,
                    )
                )
            reference = sink.close()
            elapsed = perf_counter() - started
            writer.append(
                EvidenceRecord(
                    "evidence.capture.finished",
                    "run_completed",
                    1,
                    TimePoint(11.0, "boundary.clock"),
                    {},
                )
            )
            bundle = writer.finalize(
                status=EvidenceStatus.COMPLETE,
                completed_time=TimePoint(11.0, "boundary.clock"),
            )

            self.assertIsNotNone(reference)
            self.assertEqual(sink.packet_count, packet_count)
            self.assertEqual(len(bundle.execution_captures), 1)
            captured = read_framed_sample_store(
                session.artifacts.resolve(bundle.execution_captures[0])
            )
            self.assertEqual(len(captured.packets), packet_count)
            self.assertEqual(captured.integrity.status, IntegrityStatus.VALID)
            throughput = packet_count / elapsed
            self.assertTrue(
                profile.budget("recording.incremental.packet_throughput").accepts(
                    throughput
                ),
                throughput,
            )

    def test_queue_accounting_is_constant_time_and_backpressure_safe(self) -> None:
        profile = performance_fault_qualification_profile()
        queue = EventQueue(10001)
        for index in range(10000):
            queue.push(
                QueuedEvent(
                    (index,),
                    "work",
                    f"component.{index % 20}",
                    "input",
                    index,
                    TimePoint(float(index), "boundary.clock"),
                )
            )

        started = perf_counter()
        observed = 0
        for index in range(100000):
            observed += queue.count_component(
                f"component.{index % 20}", kind="work"
            )
        elapsed = perf_counter() - started

        self.assertEqual(observed, 50_000_000)
        self.assertTrue(
            profile.budget("engine.queue.component_count_throughput").accepts(
                100000 / elapsed
            ),
            100000 / elapsed,
        )
        selected = queue.pop_oldest_component("component.3", kind="work")
        self.assertIsNotNone(selected)
        self.assertEqual(selected.value, 3)
        self.assertEqual(queue.count_component("component.3", kind="work"), 499)

        bounded = EventQueue(1, reject_newest=True)
        event = QueuedEvent(
            (0,),
            "work",
            "component.backpressure",
            "input",
            0,
            TimePoint(0.0, "boundary.clock"),
        )
        self.assertTrue(bounded.push(event))
        self.assertFalse(bounded.push(event))
        self.assertEqual(bounded.count_component("component.backpressure"), 1)
        self.assertIs(bounded.pop(), event)
        self.assertEqual(bounded.count_component("component.backpressure"), 0)
        self.assertEqual(
            profile.fault("queue.reject_newest").expected_outcome,
            FaultOutcome.REJECTED,
        )


class Phase8FaultQualificationTests(unittest.TestCase):
    def test_corrupt_and_truncated_ledgers_are_distinguished(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            source = directory / "semantic.eegle"
            with EvidenceLedgerWriter(source) as writer:
                writer.append(
                    EvidenceRecord(
                        "evidence.0",
                        "run_started",
                        0,
                        TimePoint(0.0, "boundary.clock"),
                        {},
                    )
                )
                writer.append(
                    EvidenceRecord(
                        "evidence.1",
                        "run_completed",
                        1,
                        TimePoint(1.0, "boundary.clock"),
                        {},
                    )
                )
            original = source.read_bytes()
            truncated = directory / "truncated.eegle"
            truncated.write_bytes(original[:-9])
            corrupted = directory / "corrupted.eegle"
            changed = bytearray(original)
            changed[-1] ^= 0x01
            corrupted.write_bytes(changed)

            truncated_report = read_evidence_ledger(truncated).integrity
            corrupt_report = read_evidence_ledger(corrupted).integrity

        self.assertEqual(truncated_report.status, IntegrityStatus.RECOVERABLE)
        self.assertTrue(
            {value.code for value in truncated_report.issues}
            <= {
                IntegrityIssueCode.TRUNCATED_FRAME_LENGTH,
                IntegrityIssueCode.TRUNCATED_FRAME_PAYLOAD,
            }
        )
        self.assertEqual(corrupt_report.status, IntegrityStatus.UNRECOVERABLE)
        self.assertIn(
            IntegrityIssueCode.CHECKSUM_MISMATCH,
            {value.code for value in corrupt_report.issues},
        )

    def test_missing_artifact_is_unrecoverable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            session = Session.create(
                Path(root) / "session",
                session_id="session.fault.missing-artifact",
            )
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.fault.missing-artifact",
                plan_hash=canonical_hash({"plan": "missing-artifact"}),
                created_time=TimePoint(0.0, "boundary.clock"),
            )
            writer.append(
                EvidenceRecord(
                    "evidence.0",
                    "run_started",
                    0,
                    TimePoint(0.0, "boundary.clock"),
                    {},
                )
            )
            bundle = writer.finalize(
                status=EvidenceStatus.COMPLETE,
                completed_time=TimePoint(1.0, "boundary.clock"),
            )
            session.artifacts.resolve(bundle.semantic_log).unlink()

            report = EvidenceReader.open(session, bundle.bundle_id).verify()

        self.assertEqual(report.status, IntegrityStatus.UNRECOVERABLE)
        self.assertIn(
            IntegrityIssueCode.ARTIFACT_MISSING,
            {value.code for value in report.issues},
        )

    def test_source_silence_times_out_without_discarding_final_evidence(self) -> None:
        compiled, registry, config = _compiled_lsl_recording(timeout_seconds=0.01)
        pylsl, dense, _, _ = _network()
        dense.chunks.clear()
        source = LslSource(config, pylsl_module=pylsl)

        result = ExecutionEngine.from_plan(
            compiled.plan,
            registry,
            component_overrides={"source.neural": source},
        ).run()

        self.assertEqual(result.status, EngineStatus.TIMED_OUT)
        self.assertEqual(result.captured_packets, ())
        self.assertIn("phase_timeout", {value.record_type for value in result.evidence})
        health = next(
            value
            for value in validate_evidence(
                result.evidence,
                subject_id="session.fault.silence",
                required_source_observations=("source_clock_observation",),
            ).results
            if value.result_id == "source.acquisition_observations"
        )
        # A silent inlet can still yield a clock-correction observation. The
        # phase timeout, rather than missing clock evidence, is authoritative.
        self.assertEqual(health.status, ValidationStatus.PASS)

    def test_reconnect_and_gap_are_recorded_by_the_compiled_engine(self) -> None:
        compiled, registry, config = _compiled_lsl_recording(timeout_seconds=0.03)
        pylsl, dense, _, _ = _network()
        pylsl.clock = -0.02
        dense.failures = 1
        source = LslSource(config, pylsl_module=pylsl)

        result = ExecutionEngine.from_plan(
            compiled.plan,
            registry,
            component_overrides={"source.neural": source},
        ).run()

        record_types = {value.record_type for value in result.evidence}
        self.assertIn("source_reconnect", record_types)
        self.assertIn("source_packet_loss", record_types)
        self.assertIn("source_clock_observation", record_types)

    def test_clock_drift_exceeding_compiled_uncertainty_warns(self) -> None:
        records = tuple(
            EvidenceRecord(
                f"evidence.clock.{index}",
                "source_clock_observation",
                index,
                TimePoint(float(index), "boundary.clock"),
                {
                    "component_id": "source.neural",
                    "source_clock_id": "device.clock",
                    "target_clock_id": "boundary.clock",
                    "correction_seconds": correction,
                    "measured_at_seconds": float(index),
                    "timestamp_mode": "raw_with_mapping",
                },
            )
            for index, correction in enumerate((0.001, 0.020))
        )

        report = validate_evidence(
            records,
            subject_id="session.fault.clock-drift",
            required_source_observations=("source_clock_observation",),
            source_clock_drift_tolerance_seconds=0.005,
        )
        health = next(
            value
            for value in report.results
            if value.result_id == "source.acquisition_observations"
        )
        observations = {
            value.observation_id: value for value in health.observations
        }

        self.assertEqual(health.status, ValidationStatus.WARNING)
        self.assertTrue(health.details["clock_drift_detected"])
        self.assertAlmostEqual(
            observations["source.clock_correction_span_seconds"].value,
            0.019,
        )
        self.assertEqual(
            observations["source.clock_correction_span_seconds"].status,
            ValidationStatus.WARNING,
        )


if __name__ == "__main__":
    unittest.main()
