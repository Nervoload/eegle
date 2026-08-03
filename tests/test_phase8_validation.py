from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eegle._domain import EquivalenceLevel
from eegle.actions import ActionRequest
from eegle.compiler import CompilationError
from eegle.operations import (
    compile_project,
    create_project,
    inspect_session,
    run_locked_plan,
    validate_target,
)
from eegle.recording import (
    EvidenceReader,
    EvidenceRecord,
    EvidenceStatus,
    EvidenceWriter,
    IncrementalEngineEvidenceSink,
    Session,
    SessionStatus,
)
from eegle.replay import EquivalencePolicy, compare_runs
from eegle.runtime import EngineStatus, ExecutionEngine
from eegle.runtime.model_runtime import (
    ModelResultDisposition,
    ModelResultDispositionStatus,
)
from eegle.specs import DeploymentSpec, ProtocolSpec, SuiteSpec
from eegle.streams import TimePoint
from eegle.validation import (
    EvidenceReference,
    ValidationLayer,
    ValidationObservation,
    ValidationReport,
    ValidationResult,
    ValidationSeverity,
    ValidationStatus,
    validate_evidence,
    validate_evidence_payloads,
    validate_evidence_reference_payload,
    validate_validation_observation_payload,
    validate_validation_report_payload,
    validate_validation_result_payload,
)
from tests.fixtures.phase5_model_components import compile_phase5_suite
from tests.test_phase5_plan_execution import _payload, _registry


class Phase8ValidationContractsTests(unittest.TestCase):
    def test_result_contracts_round_trip_and_validate_their_json_schemas(self) -> None:
        reference = EvidenceReference(
            "session.validation",
            bundle_id="bundle.validation",
            record_id="evidence.0",
            sequence=0,
            path="$.payload",
        )
        observation = ValidationObservation(
            "coverage.prediction",
            ValidationStatus.INSUFFICIENT_EVIDENCE,
            None,
            0,
            "no admitted windows",
            (reference,),
        )
        result = ValidationResult(
            "protocol.coverage",
            ValidationLayer.PROTOCOL,
            ValidationStatus.INSUFFICIENT_EVIDENCE,
            ValidationSeverity.WARNING,
            "Prediction coverage lacks a denominator.",
            (observation,),
            (reference,),
        )
        report = ValidationReport(
            "validation.session.validation",
            "session.validation",
            (result,),
            {"read_only": True},
        )

        validate_evidence_reference_payload(reference.to_payload())
        validate_validation_observation_payload(observation.to_payload())
        validate_validation_result_payload(result.to_payload())
        validate_validation_report_payload(report.to_payload())
        self.assertEqual(ValidationReport.from_payload(report.to_payload()), report)
        self.assertEqual(report.status, ValidationStatus.INSUFFICIENT_EVIDENCE)
        self.assertFalse(report.passed)

    def test_validation_target_with_non_identifier_path_returns_a_typed_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="eegle validation target ") as root:
            report = validate_target(Path(root) / "missing session with spaces")

        self.assertEqual(report.status, ValidationStatus.FAIL)
        validate_validation_report_payload(report.to_payload())

    def test_compiled_project_without_a_session_is_insufficient_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            project = create_project(Path(root) / "project", project_id="no-evidence")
            compile_project(project.root)

            report = validate_target(project.root)

        self.assertEqual(report.status, ValidationStatus.INSUFFICIENT_EVIDENCE)
        self.assertFalse(report.passed)


class Phase8MetricRegistryTests(unittest.TestCase):
    @staticmethod
    def _compile(protocol_payload: dict, suite_payload: dict | None = None):
        return compile_phase5_suite(
            ProtocolSpec.from_payload(protocol_payload),
            SuiteSpec.from_payload(suite_payload or _payload("suite.json")),
            DeploymentSpec.from_payload(_payload("deployment.json")),
            _registry(),
        )

    def test_unsupported_metric_is_rejected_during_compilation(self) -> None:
        protocol = _payload("protocol.json")
        protocol["metrics"][0]["measure"] = "perfect_when_empty"

        with self.assertRaises(CompilationError) as caught:
            self._compile(protocol)

        self.assertIn(
            "metric.measure_unsupported",
            {value.code for value in caught.exception.diagnostics},
        )

    def test_active_metric_requires_typed_parameters_and_active_references(self) -> None:
        protocol = _payload("protocol.json")
        suite = _payload("suite.json")
        suite["phases"][0]["acceptance_criteria"] = ["accept.coverage"]
        protocol["metrics"][0]["parameters"] = {}

        with self.assertRaises(CompilationError) as missing:
            self._compile(protocol, suite)
        self.assertIn(
            "metric.parameter_required",
            {value.code for value in missing.exception.diagnostics},
        )

        protocol["metrics"][0]["parameters"] = {
            "prediction_component": "model.primary",
            "window_component": "window.missing",
            "prediction_port": "not-a-port",
            "window_port": "windows",
        }
        with self.assertRaises(CompilationError) as invalid:
            self._compile(protocol, suite)
        codes = {value.code for value in invalid.exception.diagnostics}
        self.assertIn("metric.component_reference", codes)
        self.assertIn("metric.port_reference", codes)

    def test_empty_execution_records_insufficient_coverage_and_fails_closed(self) -> None:
        protocol = _payload("protocol.json")
        protocol["metrics"][0]["parameters"] = {
            "prediction_component": "model.primary",
            "window_component": "window.continuous",
            "prediction_port": "prediction",
            "window_port": "windows",
        }
        suite = _payload("suite.json")
        suite["phases"][0]["acceptance_criteria"] = ["accept.coverage"]
        deployment = deepcopy(_payload("deployment.json"))
        deployment["component_bindings"][0]["config"]["packets"] = []
        registry = _registry()
        compiled = compile_phase5_suite(
            ProtocolSpec.from_payload(protocol),
            SuiteSpec.from_payload(suite),
            DeploymentSpec.from_payload(deployment),
            registry,
        )

        run = ExecutionEngine.from_plan(compiled.plan, registry).run()

        self.assertEqual(run.status, EngineStatus.FAILED)
        acceptance = next(
            value for value in run.evidence if value.record_type == "acceptance_result"
        )
        self.assertEqual(
            acceptance.payload["status"],
            ValidationStatus.INSUFFICIENT_EVIDENCE.value,
        )
        self.assertIsNone(acceptance.payload["observed"])
        self.assertFalse(acceptance.payload["passed"])
        report = validate_evidence(
            run.evidence,
            subject_id="session.empty",
            integrity_status="valid",
        )
        protocol_result = next(
            value for value in report.results if value.result_id == "protocol.acceptance"
        )
        self.assertEqual(
            protocol_result.status,
            ValidationStatus.INSUFFICIENT_EVIDENCE,
        )


class Phase8EvidenceAndReplayTests(unittest.TestCase):
    @staticmethod
    def _disposition(status: ModelResultDispositionStatus) -> ModelResultDisposition:
        emitted = status == ModelResultDispositionStatus.EMITTED
        return ModelResultDisposition(
            "disposition.model.result",
            "model.primary",
            "prediction",
            status,
            TimePoint(1.0, "boundary.clock"),
            result_digest="sha256:" + "a" * 64 if emitted else None,
            prediction_id="prediction.primary.1" if emitted else None,
            reason_code=None if emitted else f"result.{status.value}",
        )

    @classmethod
    def _run_with_disposition(cls, status: ModelResultDispositionStatus):
        record = EvidenceRecord(
            "evidence.model.0",
            "model_result_disposition",
            0,
            TimePoint(1.0, "boundary.clock"),
            cls._disposition(status).to_payload(),
        )
        return SimpleNamespace(
            evidence=(record,),
            equivalence_ceiling=EquivalenceLevel.SEMANTIC,
        )

    def test_replay_compares_every_terminal_model_result_disposition(self) -> None:
        statuses = tuple(ModelResultDispositionStatus)
        for left, right in product(statuses, repeat=2):
            with self.subTest(reference=left.value, candidate=right.value):
                comparison = compare_runs(
                    self._run_with_disposition(left),
                    self._run_with_disposition(right),
                    EquivalencePolicy(EquivalenceLevel.SEMANTIC),
                )
                self.assertEqual(comparison.compared_record_count, 1)
                self.assertEqual(comparison.equivalent, left == right)

    def test_replay_without_comparable_evidence_is_not_equivalent(self) -> None:
        empty = SimpleNamespace(
            evidence=(),
            equivalence_ceiling=EquivalenceLevel.SEMANTIC,
        )

        comparison = compare_runs(
            empty,
            empty,
            EquivalencePolicy(EquivalenceLevel.SEMANTIC),
        )

        self.assertFalse(comparison.equivalent)
        self.assertEqual(comparison.compared_record_count, 0)
        self.assertEqual(comparison.divergences[0].path, "$.evidence")

    def test_real_action_payload_is_semantically_valid_and_inspection_keeps_identity(self) -> None:
        request = ActionRequest(
            "request.feedback.1",
            "feedback.visual",
            "policy.feedback",
            {"intensity": 0.5},
            TimePoint(1.0, "boundary.clock"),
            TimePoint(1.0, "boundary.clock"),
        )
        record = EvidenceRecord(
            "evidence.action.0",
            "action_request",
            0,
            TimePoint(1.0, "boundary.clock"),
            {
                "policy_component_id": request.requested_by,
                "request": request.to_payload(),
            },
        )
        result = validate_evidence_payloads(
            (record,),
            subject_id="session.action",
        )
        self.assertEqual(result.status, ValidationStatus.PASS)

        tampered = EvidenceRecord(
            record.record_id,
            record.record_type,
            record.sequence,
            record.emitted_time,
            {
                "policy_component_id": "policy.other",
                "request": request.to_payload(),
            },
        )
        self.assertEqual(
            validate_evidence_payloads(
                (tampered,), subject_id="session.action"
            ).status,
            ValidationStatus.FAIL,
        )

        with tempfile.TemporaryDirectory() as root:
            session = Session.create(Path(root) / "session", session_id="session.action")
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.action",
                plan_hash="sha256:" + "b" * 64,
                created_time=TimePoint(1.0, "boundary.clock"),
            )
            writer.append(record)
            writer.finalize(
                status=EvidenceStatus.COMPLETE,
                completed_time=TimePoint(2.0, "boundary.clock"),
            )
            session.finalize(SessionStatus.COMPLETE)

            inspection = inspect_session(session.root)

        self.assertTrue(inspection.valid)
        self.assertEqual(inspection.actions["record_count"], 1)
        self.assertEqual(
            inspection.actions["records"][0]["request_id"],
            request.request_id,
        )


class Phase8IncrementalEvidenceTests(unittest.TestCase):
    def test_unsupervised_keyboard_interrupt_publishes_a_partial_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory) / "project"
            session_root = Path(directory) / "cancelled-session"
            create_project(project_root, project_id="incremental-cancellation")
            compiled = compile_project(project_root)

            class InterruptedEngine:
                def __init__(self, evidence_sink):
                    self.evidence_sink = evidence_sink

                def run(self):
                    self.evidence_sink(
                        EvidenceRecord(
                            "evidence.cancelled.0",
                            "phase_started",
                            0,
                            TimePoint(0.0, "boundary.clock"),
                            {"phase_id": "phase.record"},
                        )
                    )
                    raise KeyboardInterrupt()

            def construct(*args, **kwargs):
                return InterruptedEngine(kwargs["evidence_sink"])

            with patch(
                "eegle.operations.projects.ExecutionEngine.from_plan",
                side_effect=construct,
            ), self.assertRaises(KeyboardInterrupt):
                run_locked_plan(
                    compiled.project.path_for("execution_plan"),
                    compiled.project.path_for("execution_lock"),
                    session_root,
                    session_id="session.incremental.cancelled",
                )

            session = Session.open(session_root, read_only=True, allow_legacy=False)
            self.assertEqual(session.status, SessionStatus.PARTIAL)
            self.assertEqual(len(session.bundle_paths), 1)
            reader = EvidenceReader.open(session, session.bundle_paths[0])
            self.assertTrue(reader.verify().valid)
            self.assertEqual(reader.bundle.status, EvidenceStatus.PARTIAL)
            self.assertEqual(
                tuple(value.record_type for value in reader.records()),
                ("phase_started",),
            )

    def test_locked_run_can_resume_streamed_evidence_after_process_boundary_loss(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory) / "project"
            session_root = Path(directory) / "crashed-session"
            create_project(project_root, project_id="incremental-crash-recovery")
            compiled = compile_project(project_root)
            token = "phase-8-supervisor-retained-recovery-token"

            class SimulatedProcessLoss(BaseException):
                pass

            class CrashedEngine:
                def __init__(self, evidence_sink):
                    self.evidence_sink = evidence_sink

                def run(self):
                    self.evidence_sink(
                        EvidenceRecord(
                            "evidence.crash.0",
                            "phase_started",
                            0,
                            TimePoint(0.0, "boundary.clock"),
                            {"phase_id": "phase.record"},
                        )
                    )
                    raise SimulatedProcessLoss()

            def construct(*args, **kwargs):
                return CrashedEngine(kwargs["evidence_sink"])

            with patch(
                "eegle.operations.projects.ExecutionEngine.from_plan",
                side_effect=construct,
            ), self.assertRaises(SimulatedProcessLoss):
                run_locked_plan(
                    compiled.project.path_for("execution_plan"),
                    compiled.project.path_for("execution_lock"),
                    session_root,
                    session_id="session.incremental.crash",
                    evidence_resume_token=token,
                )

            session = Session.open(session_root)
            resumed = EvidenceWriter.resume(
                session,
                bundle_id=next(
                    path.name for path in (session.root / "bundles").iterdir()
                ),
                resume_token=token,
                durable=True,
            )
            sink = IncrementalEngineEvidenceSink(resumed)
            sink(
                EvidenceRecord(
                    "evidence.crash.1",
                    "phase_finished",
                    1,
                    TimePoint(1.0, "boundary.clock"),
                    {"phase_id": "phase.record", "status": "complete"},
                )
            )
            bundle = resumed.finalize(
                status=EvidenceStatus.COMPLETE,
                completed_time=TimePoint(1.0, "boundary.clock"),
            )
            reader = EvidenceReader.open(session, bundle.bundle_id)

            self.assertTrue(reader.verify().valid)
            self.assertEqual(
                tuple(value.record_type for value in reader.records()),
                ("phase_started", "phase_finished"),
            )

    def test_incremental_engine_sink_resumes_at_the_proven_ledger_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Session.create(
                Path(directory) / "session",
                session_id="session.incremental.resume",
            )
            token = "phase-8-incremental-resume-token"
            writer = EvidenceWriter(
                session,
                bundle_id="bundle.incremental.resume",
                plan_hash="sha256:" + "c" * 64,
                created_time=TimePoint(0.0, "boundary.clock"),
                durable=True,
                resume_token=token,
            )
            sink = IncrementalEngineEvidenceSink(writer)
            sink(
                EvidenceRecord(
                    "evidence.resume.0",
                    "phase_started",
                    0,
                    TimePoint(0.0, "boundary.clock"),
                    {"phase_id": "phase.record"},
                )
            )
            writer.close_unfinalized()

            resumed = EvidenceWriter.resume(
                session,
                bundle_id="bundle.incremental.resume",
                resume_token=token,
                durable=True,
            )
            resumed_sink = IncrementalEngineEvidenceSink(resumed)
            resumed_sink(
                EvidenceRecord(
                    "evidence.resume.1",
                    "phase_finished",
                    1,
                    TimePoint(1.0, "boundary.clock"),
                    {"phase_id": "phase.record", "status": "complete"},
                )
            )
            bundle = resumed.finalize(
                status=EvidenceStatus.COMPLETE,
                completed_time=TimePoint(1.0, "boundary.clock"),
            )
            reader = EvidenceReader.open(session, bundle.bundle_id)

            self.assertTrue(reader.verify().valid)
            self.assertEqual(
                tuple((value.sequence, value.record_type) for value in reader.records()),
                ((0, "phase_started"), (1, "phase_finished")),
            )

    def test_engine_can_stream_graph_evidence_without_retaining_phase_logs(self) -> None:
        registry = _registry()
        compiled = compile_phase5_suite(
            ProtocolSpec.from_payload(_payload("protocol.json")),
            SuiteSpec.from_payload(_payload("suite.json")),
            DeploymentSpec.from_payload(_payload("deployment.json")),
            registry,
        )
        streamed: list[EvidenceRecord] = []
        engine = ExecutionEngine.from_plan(
            compiled.plan,
            registry,
            evidence_sink=streamed.append,
            retain_evidence=False,
        )

        run = engine.run()

        self.assertEqual(run.status, EngineStatus.COMPLETE)
        self.assertGreater(len(streamed), 0)
        self.assertTrue(all(not value.evidence for value in run.phase_results))
        self.assertLess(len(run.evidence), len(streamed))
        self.assertEqual(
            [value.sequence for value in streamed],
            list(range(len(streamed))),
        )

    def test_locked_run_finalizes_streamed_evidence_when_execution_is_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory) / "project"
            session_root = Path(directory) / "interrupted-session"
            create_project(project_root, project_id="incremental-interruption")
            compiled = compile_project(project_root)

            class InterruptedEngine:
                def __init__(self, evidence_sink):
                    self.evidence_sink = evidence_sink

                def run(self):
                    self.evidence_sink(
                        EvidenceRecord(
                            "evidence.interrupted.0",
                            "phase_started",
                            0,
                            TimePoint(0.0, "boundary.clock"),
                            {"phase_id": "phase.record"},
                        )
                    )
                    raise RuntimeError("simulated process boundary interruption")

            def construct(*args, **kwargs):
                return InterruptedEngine(kwargs["evidence_sink"])

            with patch(
                "eegle.operations.projects.ExecutionEngine.from_plan",
                side_effect=construct,
            ), self.assertRaisesRegex(RuntimeError, "simulated process"):
                run_locked_plan(
                    compiled.project.path_for("execution_plan"),
                    compiled.project.path_for("execution_lock"),
                    session_root,
                    session_id="session.incremental.interrupted",
                )

            session = Session.open(session_root, read_only=True, allow_legacy=False)
            self.assertEqual(session.status, SessionStatus.FAILED)
            self.assertEqual(len(session.bundle_paths), 1)
            reader = EvidenceReader.open(session, session.bundle_paths[0])
            self.assertTrue(reader.verify().valid)
            self.assertEqual(
                tuple(value.record_type for value in reader.records()),
                ("phase_started",),
            )
            self.assertEqual(reader.bundle.status, EvidenceStatus.FAILED)


if __name__ == "__main__":
    unittest.main()
